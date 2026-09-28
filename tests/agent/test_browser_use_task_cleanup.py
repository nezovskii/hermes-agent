from types import SimpleNamespace

from agent import chat_completion_helpers as helpers


def test_task_finalizer_releases_browser_use_lease_on_error_or_cancel(monkeypatch):
    """The common resource-finalizer is used by normal, failed, timeout and cancel exits."""
    calls = []
    monkeypatch.setattr(helpers, "is_persistent_env", lambda _task_id: False)
    monkeypatch.setattr(
        helpers, "_ra", lambda: SimpleNamespace(cleanup_vm=lambda _task_id: None, cleanup_browser=lambda _task_id: None),
    )
    monkeypatch.setattr("tools.browser_use_lifecycle.release_task_sessions", calls.append)

    helpers.cleanup_task_resources(SimpleNamespace(verbose_logging=False), "task-timeout-or-cancel")

    assert calls == ["task-timeout-or-cancel"]