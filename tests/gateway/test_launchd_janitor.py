"""Tests for the boot-time launchd janitor (gateway/launchd_janitor.py).

The janitor reaps stray *transient* KeepAlive ``ai.hermes.*-<timestamp>`` jobs —
run-once maintenance scripts mis-submitted with ``launchctl submit`` (KeepAlive
default) that SIGTERM-loop the gateway. It must reap those while never touching
the persistent default/profile gateway services.
"""

from __future__ import annotations

from types import SimpleNamespace

from gateway import launchd_janitor as j


def _result(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _make_fake_run(list_output, keepalive_labels, bootout_ok=True, print_missing=()):
    """Build a fake ``_run`` plus a record of bootout/remove targets."""
    calls = {"bootout": [], "remove": [], "print": []}

    def fake(args, timeout=10):
        verb = args[1] if len(args) > 1 else ""
        if verb == "list":
            return _result(stdout=list_output)
        if verb == "print":
            label = args[2].split("/")[-1]
            calls["print"].append(label)
            if label in print_missing:
                return _result(returncode=1, stderr="Could not find service")
            props = "keepalive" if label in keepalive_labels else ""
            return _result(stdout=f"state = running\n\tproperties = {props}\n")
        if verb == "bootout":
            calls["bootout"].append(args[2])
            return _result(returncode=0 if bootout_ok else 1, stderr="" if bootout_ok else "boom")
        if verb == "remove":
            calls["remove"].append(args[2])
            return _result(returncode=0)
        return _result(returncode=1, stderr="unknown verb")

    return fake, calls


_LIST = (
    "PID\tStatus\tLabel\n"
    "501\t0\tai.hermes.gateway\n"                                    # protected (default)
    "502\t0\tai.hermes.gateway-eve\n"                                # protected (profile)
    "503\t0\tai.hermes.rehome-kanban-dispatcher-1783505635\n"        # STRAY (dash sep)
    "504\t0\tai.hermes.deferred-default-gateway-restart.1783501411\n"  # STRAY (dot sep, has 'gateway')
    "505\t0\tai.hermes.some-oneshot-1783509999\n"                    # transient but NOT keepalive -> keep
    "506\t0\tcom.apple.WindowServer\n"                               # unrelated
)
_STRAY_DASH = "ai.hermes.rehome-kanban-dispatcher-1783505635"
_STRAY_DOT = "ai.hermes.deferred-default-gateway-restart.1783501411"


def test_reaps_only_transient_keepalive_hermes_jobs(monkeypatch):
    monkeypatch.setattr(j.sys, "platform", "darwin")
    monkeypatch.setattr(j.os, "getuid", lambda: 501, raising=False)
    keepalive = {_STRAY_DASH, _STRAY_DOT, "ai.hermes.gateway"}  # gateway protected regardless
    fake, calls = _make_fake_run(_LIST, keepalive)
    monkeypatch.setattr(j, "_run", fake)

    n = j.reap_stray_transient_launchd_jobs()

    assert n == 2
    assert set(calls["bootout"]) == {
        f"gui/501/{_STRAY_DASH}",
        f"gui/501/{_STRAY_DOT}",
    }
    # Protected gateway services are never even probed for KeepAlive.
    assert "ai.hermes.gateway" not in calls["print"]
    assert "ai.hermes.gateway-eve" not in calls["print"]


def test_transient_but_not_keepalive_is_left_alone(monkeypatch):
    monkeypatch.setattr(j.sys, "platform", "darwin")
    monkeypatch.setattr(j.os, "getuid", lambda: 501, raising=False)
    # Only 505 is transient-here and it is NOT keepalive -> must not be reaped.
    fake, calls = _make_fake_run(
        "PID\tStatus\tLabel\n505\t0\tai.hermes.some-oneshot-1783509999\n",
        keepalive_labels=set(),
    )
    monkeypatch.setattr(j, "_run", fake)
    assert j.reap_stray_transient_launchd_jobs() == 0
    assert calls["bootout"] == []


def test_bootout_failure_falls_back_to_remove(monkeypatch):
    monkeypatch.setattr(j.sys, "platform", "darwin")
    monkeypatch.setattr(j.os, "getuid", lambda: 501, raising=False)
    fake, calls = _make_fake_run(
        f"PID\tStatus\tLabel\n503\t0\t{_STRAY_DASH}\n",
        keepalive_labels={_STRAY_DASH},
        bootout_ok=False,
    )
    monkeypatch.setattr(j, "_run", fake)
    n = j.reap_stray_transient_launchd_jobs()
    assert n == 1
    assert calls["bootout"] == [f"gui/501/{_STRAY_DASH}"]
    assert calls["remove"] == [_STRAY_DASH]  # legacy fallback uses the bare label


def test_unverifiable_job_is_not_reaped(monkeypatch):
    """If `launchctl print` fails, we cannot confirm KeepAlive -> do not reap."""
    monkeypatch.setattr(j.sys, "platform", "darwin")
    monkeypatch.setattr(j.os, "getuid", lambda: 501, raising=False)
    fake, calls = _make_fake_run(
        f"PID\tStatus\tLabel\n503\t0\t{_STRAY_DASH}\n",
        keepalive_labels={_STRAY_DASH},
        print_missing=(_STRAY_DASH,),
    )
    monkeypatch.setattr(j, "_run", fake)
    assert j.reap_stray_transient_launchd_jobs() == 0
    assert calls["bootout"] == []


def test_noop_off_darwin(monkeypatch):
    monkeypatch.setattr(j.sys, "platform", "linux")
    called = {"n": 0}
    monkeypatch.setattr(j, "_run", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or _result())
    assert j.reap_stray_transient_launchd_jobs() == 0
    assert called["n"] == 0  # never even shells out on non-macOS


def test_label_regexes():
    # Transient labels (dash and dot separators) match; persistent do not.
    assert j._TRANSIENT_LABEL_RE.match(_STRAY_DASH)
    assert j._TRANSIENT_LABEL_RE.match(_STRAY_DOT)
    assert not j._TRANSIENT_LABEL_RE.match("ai.hermes.gateway")
    assert not j._TRANSIENT_LABEL_RE.match("ai.hermes.gateway-eve")
    # Protected regex is anchored: exact gateway services only.
    assert j._PROTECTED_LABEL_RE.match("ai.hermes.gateway")
    assert j._PROTECTED_LABEL_RE.match("ai.hermes.gateway-actvox-slack")
    assert not j._PROTECTED_LABEL_RE.match(_STRAY_DOT)  # has 'gateway' but not a gateway service
