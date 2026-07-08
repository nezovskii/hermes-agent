from __future__ import annotations

import resource

from gateway.fd_limits import raise_fd_limit


def test_raise_fd_limit_raises_soft_limit_to_target(monkeypatch):
    calls: list[tuple[int, int]] = []

    monkeypatch.setattr(resource, "getrlimit", lambda which: (256, 65536))
    monkeypatch.setattr(
        resource,
        "setrlimit",
        lambda which, limits: calls.append(limits),
    )

    raise_fd_limit(target=16384)

    assert calls == [(16384, 65536)]


def test_raise_fd_limit_caps_target_at_hard_limit(monkeypatch):
    calls: list[tuple[int, int]] = []

    monkeypatch.setattr(resource, "getrlimit", lambda which: (256, 4096))
    monkeypatch.setattr(
        resource,
        "setrlimit",
        lambda which, limits: calls.append(limits),
    )

    raise_fd_limit(target=16384)

    assert calls == [(4096, 4096)]


def test_raise_fd_limit_noops_when_soft_already_high_enough(monkeypatch):
    calls: list[tuple[int, int]] = []

    monkeypatch.setattr(resource, "getrlimit", lambda which: (32768, 65536))
    monkeypatch.setattr(
        resource,
        "setrlimit",
        lambda which, limits: calls.append(limits),
    )

    raise_fd_limit(target=16384)

    assert calls == []


def test_raise_fd_limit_never_raises_on_setrlimit_failure(monkeypatch):
    monkeypatch.setattr(resource, "getrlimit", lambda which: (256, 65536))

    def _boom(which, limits):
        raise OSError("not allowed")

    monkeypatch.setattr(resource, "setrlimit", _boom)

    raise_fd_limit(target=16384)
