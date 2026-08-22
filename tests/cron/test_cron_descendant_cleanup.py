"""Regression tests for cron cleanup across nested POSIX sessions."""
from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

import cron.scheduler as scheduler


def test_snapshot_follows_descendants_across_process_groups(monkeypatch):
    ps_output = "\n".join(
        [
            "100 1 100",
            "101 100 101",
            "102 101 102",
            "999 1 999",
        ]
    )
    monkeypatch.setattr(
        scheduler.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=ps_output),
    )

    pids, groups = scheduler._snapshot_posix_descendant_groups({100})

    assert pids == {100, 101, 102}
    assert groups == {100, 101, 102}


def test_snapshot_captures_incident_sized_escaped_tree(monkeypatch):
    # Outer cron script stays in PGID 100. The GBrain wrapper starts a nested
    # session at PID/PGID 101, then 809 same-group descendants form the exact
    # 810-process incident shape.
    rows = ["100 1 100", "101 100 101"]
    parent = 101
    for pid in range(102, 911):
        rows.append(f"{pid} {parent} 101")
        parent = pid
    monkeypatch.setattr(
        scheduler.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="\n".join(rows),
        ),
    )

    pids, groups = scheduler._snapshot_posix_descendant_groups({100})

    assert len(pids) == 811
    assert groups == {100, 101}
    assert 910 in pids


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
def test_terminate_kills_child_that_started_a_new_session(tmp_path):
    """Exercise the real kill path in a fresh interpreter.

    The repository-wide pytest live-system guard intentionally blocks signals
    to a reparented process group. A subprocess imports scheduler without that
    monkeypatch while keeping the entire fixture isolated to processes it owns.
    """
    helper = tmp_path / "exercise_cleanup.py"
    helper.write_text(
        """
import os
import signal
import subprocess
import sys
import time

from cron.scheduler import _terminate_cron_script_process

pid_file = sys.argv[1]
child_code = (
    "import os,signal,sys,time;"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
    "open(sys.argv[1], 'w').write(str(os.getpid()));"
    "time.sleep(60)"
)
parent_code = (
    "import subprocess,sys,time;"
    "subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1]],"
    "start_new_session=True,stdin=subprocess.DEVNULL,"
    "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
    "time.sleep(60)"
)
proc = subprocess.Popen(
    [sys.executable, "-c", parent_code, pid_file, child_code],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
)
child_pid = 0
try:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if os.path.exists(pid_file):
            child_pid = int(open(pid_file).read())
            break
        time.sleep(0.05)
    assert child_pid > 0
    assert os.getpgid(child_pid) != os.getpgid(proc.pid)
    _terminate_cron_script_process(proc)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(child_pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
        if not state or state.startswith("Z"):
            break
        time.sleep(0.05)
    assert not state or state.startswith("Z"), state
    assert proc.poll() is not None
finally:
    for pgid in {proc.pid, child_pid} - {0}:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(helper), str(tmp_path / "child.pid")],
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
