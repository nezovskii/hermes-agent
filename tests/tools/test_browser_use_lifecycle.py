import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from tools import browser_use_lifecycle as lifecycle


_HARNESS_PROGRAM = r'''
import json, os, socket, sys
runtime = sys.argv[1]
exit_on_shutdown = sys.argv[2] == "1"
reject_shutdowns = int(sys.argv[3])
sock_path = os.path.join(runtime, "bu.sock")
pid_path = os.path.join(runtime, "bu.pid")
requests_path = os.path.join(runtime, "requests.jsonl")
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(sock_path)
server.listen(4)
open(pid_path, "w").write(str(os.getpid()))
while True:
    client, _ = server.accept()
    with client:
        req = json.loads(client.recv(65536).decode())
        with open(requests_path, "a") as f:
            f.write(json.dumps(req) + "\n")
        if req.get("meta") == "ping":
            response = {"pong": True, "pid": os.getpid()}
        elif req.get("meta") == "shutdown":
            previous_shutdowns = sum(1 for line in open(requests_path) if '"shutdown"' in line)
            response = {"ok": previous_shutdowns > reject_shutdowns}
        elif req.get("meta") == "test_stop":
            response = {"ok": True}
        else:
            response = {"ok": False}
        client.sendall((json.dumps(response) + "\n").encode())
    if (req.get("meta") == "shutdown" and exit_on_shutdown and response["ok"]) or req.get("meta") == "test_stop":
        server.close()
        os.unlink(sock_path)
        os.unlink(pid_path)
        raise SystemExit(0)
'''


class _HarnessChild:
    def __init__(self, runtime_dir: Path, *, exit_on_shutdown: bool = True, reject_shutdowns: int = 0):
        self.runtime_dir = runtime_dir
        self.requests_path = runtime_dir / "requests.jsonl"
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _HARNESS_PROGRAM, str(runtime_dir), "1" if exit_on_shutdown else "0", str(reject_shutdowns)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 2
        while not (runtime_dir / "bu.sock").exists():
            if self.proc.poll() is not None or time.monotonic() >= deadline:
                stderr = self.proc.stderr.read() if self.proc.stderr else ""
                raise RuntimeError(f"test harness did not become ready: {stderr}")
            time.sleep(0.01)

    @property
    def requests(self):
        if not self.requests_path.exists():
            return []
        return [json.loads(line) for line in self.requests_path.read_text(encoding="utf-8").splitlines()]

    def reap(self):
        try:
            self.proc.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            # The test owns this child. Ask its test-only IPC endpoint to exit;
            # never signal a process, mirroring the production ownership rule.
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                client.settimeout(1)
                client.connect(str(self.runtime_dir / "bu.sock"))
                client.sendall(b'{"meta":"test_stop"}\n')
                client.recv(65536)
            finally:
                client.close()
            self.proc.wait(timeout=2)
        finally:
            if self.proc.stderr:
                self.proc.stderr.close()


@pytest.fixture(autouse=True)
def _clean_leases(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    # Keep test sockets inside the same AF_UNIX path budget as production.
    test_runtime_root = Path(tempfile.gettempdir()) / f"bu-test-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(lifecycle, "_runtime_dir", lambda task_id, session: test_runtime_root / task_id / session)
    lifecycle.reset_task_session_leases_for_tests()
    yield
    lifecycle.reset_task_session_leases_for_tests()


def test_end_session_uses_vendor_meta_shutdown_and_verifies_real_child_exit():
    lease, error = lifecycle.acquire_task_session("task-exit", "owned-exit")
    assert error is None and lease is not None
    harness = _HarnessChild(lease.runtime_dir)

    lifecycle.release_call(lease)
    lifecycle.release_task_sessions("task-exit")
    harness.reap()

    assert harness.proc.returncode == 0
    assert harness.requests == [{"meta": "ping"}, {"meta": "shutdown"}]
    assert not (lease.runtime_dir / "bu.sock").exists()
    assert not (lease.runtime_dir / "bu.pid").exists()
    replacement, error = lifecycle.acquire_task_session("task-next", "owned-exit")
    assert error is None and replacement is not None


def test_release_waits_for_active_call_before_shutdown():
    lease, error = lifecycle.acquire_task_session("task-wait", "owned-wait")
    assert error is None and lease is not None
    harness = _HarnessChild(lease.runtime_dir)

    lifecycle.release_task_sessions("task-wait")
    assert harness.requests == []
    lifecycle.release_call(lease)
    harness.reap()

    assert harness.requests == [{"meta": "ping"}, {"meta": "shutdown"}]


def test_common_task_finalizer_retries_failed_shutdown_and_releases_on_vendor_retry(monkeypatch):
    monkeypatch.setattr(lifecycle, "_SHUTDOWN_RETRY_DELAY_S", 0)
    lease, error = lifecycle.acquire_task_session("task-retry", "owned-retry")
    assert error is None and lease is not None
    harness = _HarnessChild(lease.runtime_dir, reject_shutdowns=1)

    lifecycle.release_call(lease)
    lifecycle.release_task_sessions("task-retry")
    harness.reap()

    assert [req["meta"] for req in harness.requests] == ["ping", "shutdown", "ping", "shutdown"]
    replacement, error = lifecycle.acquire_task_session("task-next", "owned-retry")
    assert error is None and replacement is not None


def test_failed_shutdown_keeps_owner_and_persists_bounded_retry_receipt(monkeypatch):
    monkeypatch.setattr(lifecycle, "_SHUTDOWN_EXIT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(lifecycle, "_SHUTDOWN_RETRY_DELAY_S", 0)
    lease, error = lifecycle.acquire_task_session("task-fail", "owned-fail")
    assert error is None and lease is not None
    harness = _HarnessChild(lease.runtime_dir, exit_on_shutdown=False)
    try:
        lifecycle.release_call(lease)
        lifecycle.release_task_sessions("task-fail")

        blocked, error = lifecycle.acquire_task_session("task-other", "owned-fail")
        assert blocked is None and error is not None and "another task" in error
        receipt = json.loads((lease.runtime_dir / "hermes-task-release.json").read_text(encoding="utf-8"))
        assert receipt["state"] == "failed"
        assert receipt["attempts"] == lifecycle._MAX_SHUTDOWN_ATTEMPTS
        assert "did not exit" in receipt["error"]
        assert len([req for req in harness.requests if req.get("meta") == "shutdown"]) == lifecycle._MAX_SHUTDOWN_ATTEMPTS
    finally:
        harness.reap()


@pytest.mark.parametrize("pid_record", ["missing", "symlink"])
def test_socket_without_safe_pid_record_fails_closed_and_retains_owner(monkeypatch, pid_record):
    monkeypatch.setattr(lifecycle, "_SHUTDOWN_RETRY_DELAY_S", 0)
    task_id, session = ("m", "m") if pid_record == "missing" else ("s", "s")
    lease, error = lifecycle.acquire_task_session(task_id, session)
    assert error is None and lease is not None
    harness = _HarnessChild(lease.runtime_dir)
    try:
        pid_path = lease.runtime_dir / "bu.pid"
        pid_path.unlink()
        if pid_record == "symlink":
            pid_path.symlink_to(lease.runtime_dir / "not-a-pid")

        lifecycle.release_call(lease)
        lifecycle.release_task_sessions(lease.task_id)

        blocked, error = lifecycle.acquire_task_session("task-other", lease.session)
        assert blocked is None and error is not None and "another task" in error
        receipt = json.loads((lease.runtime_dir / "hermes-task-release.json").read_text(encoding="utf-8"))
        assert receipt["attempts"] == lifecycle._MAX_SHUTDOWN_ATTEMPTS
        assert "ambiguous" in receipt["error"]
        assert harness.requests == []
    finally:
        harness.reap()


def test_cross_task_concurrent_reuse_is_refused_but_persistent_is_not_registered():
    lease, error = lifecycle.acquire_task_session("task-a", "owned")
    assert error is None and lease is not None
    other, error = lifecycle.acquire_task_session("task-b", "owned")
    assert other is None
    assert error is not None
    assert "another task" in error

    # The lifecycle registry has no default/persistent acquisition path: only callers
    # explicitly or automatically selecting task scope can ever be shutdown.
    lifecycle.release_task_sessions("unrelated-task")
    still_owned, error = lifecycle.acquire_task_session("task-a", "owned")
    assert error is None and still_owned is lease
    lifecycle.release_call(lease)
    lifecycle.release_call(still_owned)


def test_missing_harness_releases_lease_without_process_kill(caplog):
    lease, error = lifecycle.acquire_task_session("task-missing", "owned-missing")
    assert error is None and lease is not None

    lifecycle.release_call(lease)
    lifecycle.release_task_sessions("task-missing")

    assert "shutdown attempt" not in caplog.text
    # No socket/pid means no external process was created or signalled.
    assert not (lease.runtime_dir / "bu.sock").exists()
    assert not (lease.runtime_dir / "bu.pid").exists()
    replacement, error = lifecycle.acquire_task_session("task-next", "owned-missing")
    assert error is None and replacement is not None
