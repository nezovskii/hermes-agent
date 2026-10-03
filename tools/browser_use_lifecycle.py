"""Explicit lifecycle ownership for isolated Browser Use harness sessions.

Only a task that owns an isolated private browser receives a lease. Shared/default
and explicit persistent sessions never enter this registry. Failed releases retain
ownership in memory and leave a small on-disk receipt so a worker loss cannot look
like a confirmed shutdown.
"""

from __future__ import annotations

import errno
import getpass
import hashlib
import json
import logging
import os
import socket
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import psutil

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_IPC_TIMEOUT_S = 2.0
_SHUTDOWN_EXIT_TIMEOUT_S = 6.0
_MAX_SHUTDOWN_ATTEMPTS = 3
_SHUTDOWN_RETRY_DELAY_S = 0.1
_RECEIPT_NAME = "hermes-task-release.json"


@dataclass
class _TaskSessionLease:
    task_id: str
    session: str
    runtime_dir: Path
    active_calls: int = 0
    release_requested: bool = False
    finishing: bool = False
    shutdown_attempts: int = 0
    last_shutdown_error: Optional[str] = None


_lock = threading.RLock()
_leases: Dict[tuple[str, str], _TaskSessionLease] = {}
_session_owners: Dict[str, str] = {}


def _runtime_dir(task_id: str, session: str) -> Path:
    digest = hashlib.sha256(f"{task_id}\0{session}".encode("utf-8")).hexdigest()[:24]
    home_path = Path(get_hermes_home()) / "cache" / "browser-use" / "task-runtime" / digest
    # AF_UNIX has a short platform-dependent pathname limit. A deeply nested
    # test/profile home can exceed it even though the runtime itself is safe.
    if len(os.fsencode(str(home_path / "bu.sock"))) < 100:
        return home_path
    getuid = getattr(os, "getuid", None)
    user_key = str(getuid()) if callable(getuid) else hashlib.sha256(
        getpass.getuser().encode("utf-8")
    ).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"hermes-bu-{user_key}" / digest


def _receipt_path(lease: _TaskSessionLease) -> Path:
    return lease.runtime_dir / _RECEIPT_NAME


def _write_release_receipt(lease: _TaskSessionLease, state: str, error: Optional[str] = None) -> None:
    """Persist release evidence without changing any vendor IPC artifact."""
    payload = {
        "state": state,
        "task_id": lease.task_id,
        "session": lease.session,
        "attempts": lease.shutdown_attempts,
        "error": error,
        "recorded_at": time.time(),
        "pid_path": str(lease.runtime_dir / "bu.pid"),
        "socket_path": str(lease.runtime_dir / "bu.sock"),
    }
    try:
        lease.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = _receipt_path(lease)
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    except OSError as exc:
        logger.warning("Could not persist Browser Use task release receipt for %s: %s", lease.session, exc)


def _clear_release_receipt(lease: _TaskSessionLease) -> None:
    try:
        _receipt_path(lease).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not clear Browser Use task release receipt for %s: %s", lease.session, exc)


def acquire_task_session(task_id: Optional[str], session: str) -> tuple[Optional[_TaskSessionLease], Optional[str]]:
    """Acquire one in-process task lease, refusing concurrent cross-task reuse."""
    if not task_id:
        return None, "task-scoped Browser Use sessions require a task_id"
    if not session:
        return None, "task-scoped Browser Use sessions require a non-empty session name"

    key = (str(task_id), session)
    with _lock:
        owner = _session_owners.get(session)
        if owner is not None and owner != key[0]:
            return None, f"Browser Use session {session!r} is active in another task; use a distinct session name"
        lease = _leases.get(key)
        if lease is None:
            runtime_dir = _runtime_dir(*key)
            try:
                runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                if runtime_dir.is_symlink() or not runtime_dir.is_dir():
                    return None, "task Browser Use runtime directory is not a safe directory"
            except OSError as exc:
                return None, f"could not prepare task Browser Use runtime: {exc}"
            lease = _TaskSessionLease(task_id=key[0], session=session, runtime_dir=runtime_dir)
            _leases[key] = lease
            _session_owners[session] = key[0]
        if lease.release_requested:
            return None, f"Browser Use task session {session!r} is closing"
        lease.active_calls += 1
        return lease, None


def release_call(lease: Optional[_TaskSessionLease]) -> None:
    """Mark one CLI invocation complete and finish a requested release after the last call."""
    if lease is None:
        return
    with _lock:
        lease.active_calls = max(0, lease.active_calls - 1)
    _finish_lease(lease)


def release_task_sessions(task_id: Optional[str]) -> None:
    """Request release of every task-owned lease for ``task_id``.

    Active CLI calls are never interrupted. Failed shutdown retains the lease and
    owner mapping; a bounded subsequent finalizer invocation can retry it. Persistent
    and shared/default sessions have no lease and are deliberately invisible here.
    """
    if not task_id:
        return
    with _lock:
        leases = [lease for lease in _leases.values() if lease.task_id == str(task_id)]
        for lease in leases:
            lease.release_requested = True
    for lease in leases:
        _finish_lease(lease)


def _ipc(socket_path: Path, request: dict, timeout_s: float = _IPC_TIMEOUT_S) -> dict:
    if socket_path.is_symlink() or not socket_path.exists():
        raise RuntimeError(f"Browser Use harness socket unavailable: {socket_path}")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout_s)
    try:
        client.connect(str(socket_path))
        client.sendall((json.dumps(request) + "\n").encode("utf-8"))
        received = b""
        while not received.endswith(b"\n"):
            chunk = client.recv(65536)
            if not chunk:
                break
            received += chunk
    finally:
        client.close()
    response = json.loads(received or b"{}")
    if not isinstance(response, dict):
        raise RuntimeError("Browser Use harness returned a non-object IPC response")
    return response


def _pid_is_alive(pid: int) -> bool:
    """Check liveness without signaling; reap only an already-exited test child."""
    # A test-owned child can be a zombie between its vendor cleanup and its
    # parent reaping it. Reap only if it is our child; production daemons are
    # detached and ``waitpid`` raises ChildProcessError, then signal 0 remains
    # the non-mutating liveness check.
    try:
        reaped, _status = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except ChildProcessError:
        pass
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


def _wait_for_daemon_exit(lease: _TaskSessionLease, expected_pid: int) -> None:
    """Require the vendor daemon to remove its endpoint, pid record, and process."""
    pid_path = lease.runtime_dir / "bu.pid"
    socket_path = lease.runtime_dir / "bu.sock"
    deadline = time.monotonic() + _SHUTDOWN_EXIT_TIMEOUT_S
    while time.monotonic() < deadline:
        if not socket_path.exists() and not pid_path.exists() and not _pid_is_alive(expected_pid):
            return
        time.sleep(0.05)
    live = []
    if socket_path.exists():
        live.append("socket")
    if pid_path.exists():
        live.append("pid record")
    if _pid_is_alive(expected_pid):
        live.append(f"process {expected_pid}")
    raise RuntimeError("Browser Use harness acknowledged shutdown but did not exit (" + ", ".join(live or ["unknown state"]) + ")")


def _shutdown(lease: _TaskSessionLease) -> None:
    """Use only vendor IPC. Never signal a process, scan runtimes, or remove vendor files."""
    pid_path = lease.runtime_dir / "bu.pid"
    socket_path = lease.runtime_dir / "bu.sock"
    # A socket is evidence that something may still own the isolated runtime.
    # Never drop a lease merely because the PID record vanished or was replaced
    # by a symlink: that would make an unverified daemon look released.
    if pid_path.is_symlink():
        if socket_path.exists():
            raise RuntimeError("Browser Use harness runtime is ambiguous: socket exists but pid record is a symlink")
        return
    if not pid_path.exists():
        if socket_path.exists():
            raise RuntimeError("Browser Use harness runtime is ambiguous: socket exists but pid record is missing")
        return  # The CLI never started a harness, or it already exited.
    expected_pid = int(pid_path.read_text(encoding="utf-8").strip())
    if expected_pid <= 0:
        raise RuntimeError("Browser Use harness pid record is invalid")
    # The vendor removes its socket before the daemon process has necessarily
    # finished exiting. A retry can therefore arrive in that narrow window
    # after an acknowledged shutdown. Keep the exact recorded PID bound and
    # wait for all three liveness artifacts instead of treating the vanished
    # endpoint itself as a new shutdown failure.
    if not socket_path.exists():
        _wait_for_daemon_exit(lease, expected_pid)
        return
    pong = _ipc(socket_path, {"meta": "ping"})
    if pong.get("pong") is not True or pong.get("pid") != expected_pid:
        raise RuntimeError("Browser Use harness did not prove PID identity")
    response = _ipc(socket_path, {"meta": "shutdown"}, timeout_s=5.0)
    if response.get("ok") is not True:
        raise RuntimeError(f"Browser Use harness rejected shutdown: {response.get('error') or response}")
    _wait_for_daemon_exit(lease, expected_pid)


def _finish_lease(lease: _TaskSessionLease) -> None:
    """Release via vendor IPC, retrying a bounded number of failed attempts.

    ``cleanup_task_resources`` normally runs only once for a completed,
    cancelled, or timed-out task. Retrying here, rather than waiting for an
    unrelated second finalizer call, gives a failed release a real opportunity
    to recover while retaining ownership throughout.
    """
    while True:
        with _lock:
            key = (lease.task_id, lease.session)
            if (_leases.get(key) is not lease or not lease.release_requested or lease.active_calls
                    or lease.finishing):
                return
            if lease.shutdown_attempts >= _MAX_SHUTDOWN_ATTEMPTS:
                _write_release_receipt(lease, "failed", lease.last_shutdown_error or "retry budget exhausted")
                return
            lease.finishing = True
            lease.shutdown_attempts += 1
            _write_release_receipt(lease, "releasing")
        try:
            _shutdown(lease)
        except Exception as exc:
            error = str(exc)
            with _lock:
                lease.finishing = False
                lease.last_shutdown_error = error
                retries_remain = lease.shutdown_attempts < _MAX_SHUTDOWN_ATTEMPTS
            _write_release_receipt(lease, "failed", error)
            logger.warning("Task-owned Browser Use session %s shutdown attempt %s/%s was not confirmed: %s",
                           lease.session, lease.shutdown_attempts, _MAX_SHUTDOWN_ATTEMPTS, error)
            if not retries_remain:
                return
            time.sleep(_SHUTDOWN_RETRY_DELAY_S)
            continue

        with _lock:
            if _leases.get(key) is lease:
                _leases.pop(key, None)
                if _session_owners.get(lease.session) == lease.task_id:
                    _session_owners.pop(lease.session, None)
            lease.finishing = False
        _clear_release_receipt(lease)
        return


def reset_task_session_leases_for_tests() -> None:
    """Test-only reset. Production teardown must call ``release_task_sessions``."""
    with _lock:
        _leases.clear()
        _session_owners.clear()
