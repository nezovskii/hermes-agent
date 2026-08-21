"""Regression tests for durable gateway lifecycle-message cleanup."""

from __future__ import annotations

import asyncio
import json

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.shutdown_notice_cleanup import (
    cleanup_queue_path,
    cleanup_shutdown_notices,
    delete_recorded_lifecycle_notice,
    record_lifecycle_notice,
)
from tests.gateway.restart_test_helpers import make_restart_runner


class _DeleteAdapter:
    def __init__(self, *, ok: bool = True):
        self.ok = ok
        self.deleted: list[tuple[str, str]] = []

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        self.deleted.append((chat_id, message_id))
        return self.ok


def _payload(home):
    return json.loads(cleanup_queue_path(home).read_text(encoding="utf-8"))


def test_records_slack_lifecycle_notice_by_default(tmp_path):
    result = SendResult(success=True, message_id="171234.567")

    assert record_lifecycle_notice(
        tmp_path,
        Platform.SLACK,
        "C123",
        result,
        kind="shutdown",
        now=10.0,
    )

    assert _payload(tmp_path) == {
        "version": 2,
        "notices": [
            {
                "platform": "slack",
                "chat_id": "C123",
                "message_id": "171234.567",
                "kind": "shutdown",
                "created_at": 10.0,
            }
        ],
    }


def test_unsupported_platform_is_not_queued(tmp_path):
    result = SendResult(success=True, message_id="m1")

    assert not record_lifecycle_notice(
        tmp_path,
        Platform.DISCORD,
        "C123",
        result,
        kind="shutdown",
    )
    assert not cleanup_queue_path(tmp_path).exists()


@pytest.mark.asyncio
async def test_restart_loop_cleanup_deletes_telegram_and_slack_notices(tmp_path):
    telegram = _DeleteAdapter()
    slack = _DeleteAdapter()
    for platform, chat_id, message_id, kind in (
        (Platform.TELEGRAM, "42", "tg-shutdown", "shutdown"),
        (Platform.TELEGRAM, "42", "tg-restart", "restart-request"),
        (Platform.SLACK, "C123", "slack-online", "startup-online"),
    ):
        assert record_lifecycle_notice(
            tmp_path,
            platform,
            chat_id,
            SendResult(success=True, message_id=message_id),
            kind=kind,
        )

    deleted = await cleanup_shutdown_notices(
        tmp_path,
        {Platform.TELEGRAM: telegram, Platform.SLACK: slack},
    )

    assert deleted == 3
    assert telegram.deleted == [
        ("42", "tg-shutdown"),
        ("42", "tg-restart"),
    ]
    assert slack.deleted == [("C123", "slack-online")]
    assert not cleanup_queue_path(tmp_path).exists()


@pytest.mark.asyncio
async def test_delayed_delete_forgets_notice_only_after_provider_success(tmp_path):
    result = SendResult(success=True, message_id="startup-1")
    assert record_lifecycle_notice(
        tmp_path,
        Platform.SLACK,
        "C123",
        result,
        kind="startup-online",
    )
    failing = _DeleteAdapter(ok=False)

    assert not await delete_recorded_lifecycle_notice(
        tmp_path,
        Platform.SLACK,
        failing,
        "C123",
        "startup-1",
    )
    assert cleanup_queue_path(tmp_path).exists()

    healthy = _DeleteAdapter(ok=True)
    assert await delete_recorded_lifecycle_notice(
        tmp_path,
        Platform.SLACK,
        healthy,
        "C123",
        "startup-1",
    )
    assert not cleanup_queue_path(tmp_path).exists()


@pytest.mark.asyncio
async def test_startup_cleanup_does_not_overwrite_concurrent_new_notice(tmp_path):
    assert record_lifecycle_notice(
        tmp_path,
        Platform.SLACK,
        "C123",
        SendResult(success=True, message_id="old"),
        kind="shutdown",
    )

    class _AppendDuringDelete(_DeleteAdapter):
        async def delete_message(self, chat_id: str, message_id: str) -> bool:
            assert record_lifecycle_notice(
                tmp_path,
                Platform.SLACK,
                chat_id,
                SendResult(success=True, message_id="new"),
                kind="startup-online",
            )
            return await super().delete_message(chat_id, message_id)

    adapter = _AppendDuringDelete()
    assert await cleanup_shutdown_notices(
        tmp_path,
        {Platform.SLACK: adapter},
    ) == 1

    notices = _payload(tmp_path)["notices"]
    assert [notice["message_id"] for notice in notices] == ["new"]


@pytest.mark.asyncio
async def test_healthy_startup_notice_is_deleted_and_forgotten(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner, adapter = make_restart_runner()
    adapter.delete_message = _DeleteAdapter().delete_message

    assert runner._record_and_schedule_lifecycle_notice(
        Platform.TELEGRAM,
        adapter,
        "42",
        SendResult(success=True, message_id="ready-1"),
        kind="restart-complete",
        delay_seconds=0,
    )
    await asyncio.gather(*runner._background_tasks)

    assert not cleanup_queue_path(tmp_path).exists()
