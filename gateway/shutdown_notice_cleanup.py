"""Durable cleanup for transient gateway shutdown notifications."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, cast

from utils import atomic_json_write

logger = logging.getLogger(__name__)

_QUEUE_FILENAME = "gateway-shutdown-notice-cleanup.json"
_TELEGRAM_DELETE_WINDOW_SECONDS = 47 * 3600


def cleanup_queue_path(hermes_home: Path) -> Path:
    return hermes_home / "state" / _QUEUE_FILENAME


def _fresh_notice(item: object, now: float) -> dict[str, object] | None:
    if not isinstance(item, dict):
        return None
    try:
        created_at = float(item.get("created_at", 0.0))
    except (TypeError, ValueError):
        return None
    if now - created_at >= _TELEGRAM_DELETE_WINDOW_SECONDS:
        return None
    return item


def record_shutdown_notice(
    hermes_home: Path,
    platform: object,
    chat_id: str,
    result: object,
    *,
    now: float | None = None,
) -> bool:
    """Persist one bot-posted Telegram banner for the next process to delete."""
    platform_name = str(getattr(platform, "value", platform))
    message_id = getattr(result, "message_id", None)
    if platform_name != "telegram" or message_id in (None, ""):
        return False

    path = cleanup_queue_path(hermes_home)
    timestamp = time.time() if now is None else now
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        raw_notices = payload.get("notices") if isinstance(payload, dict) else []
        if not isinstance(raw_notices, list):
            raw_notices = []
        notices = [
            fresh
            for item in raw_notices
            if (fresh := _fresh_notice(item, timestamp)) is not None
        ]
        record: dict[str, object] = {
            "platform": platform_name,
            "chat_id": str(chat_id),
            "message_id": str(message_id),
            "created_at": timestamp,
        }
        identity = (platform_name, str(chat_id), str(message_id))
        if not any(
            (
                str(item.get("platform")),
                str(item.get("chat_id")),
                str(item.get("message_id")),
            )
            == identity
            for item in notices
        ):
            notices.append(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, {"version": 1, "notices": notices}, indent=2)
        return True
    except Exception as exc:
        logger.debug("Could not persist shutdown-notice cleanup record: %s", exc)
        return False


async def cleanup_shutdown_notices(
    hermes_home: Path,
    adapters: Mapping[Any, Any],
    *,
    now: float | None = None,
) -> int:
    """Delete queued Telegram banners after adapters reconnect.

    Failed recent deletes remain queued for the next restart. Expired or
    malformed records are dropped without making a Bot API request.
    """
    path = cleanup_queue_path(hermes_home)
    if not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_notices = payload.get("notices") if isinstance(payload, dict) else []
        if not isinstance(raw_notices, list):
            raw_notices = []
    except Exception as exc:
        logger.warning("Shutdown-notice cleanup queue is unreadable: %s", exc)
        return 0

    timestamp = time.time() if now is None else now
    adapters_by_name = {
        str(getattr(platform, "value", platform)): adapter
        for platform, adapter in adapters.items()
    }
    remaining: list[dict[str, object]] = []
    deleted = 0
    for item in raw_notices:
        notice = _fresh_notice(item, timestamp)
        if notice is None:
            continue
        adapter = adapters_by_name.get(str(notice.get("platform") or ""))
        delete_fn = getattr(adapter, "delete_message", None) if adapter else None
        if not callable(delete_fn):
            remaining.append(notice)
            continue
        try:
            delete_call = cast(Callable[[str, str], Awaitable[bool]], delete_fn)
            ok = await delete_call(
                str(notice.get("chat_id") or ""),
                str(notice.get("message_id") or ""),
            )
        except Exception as exc:
            logger.debug("Shutdown-notice cleanup failed: %s", exc)
            ok = False
        if ok:
            deleted += 1
        else:
            remaining.append(notice)

    try:
        if remaining:
            atomic_json_write(path, {"version": 1, "notices": remaining}, indent=2)
        else:
            path.unlink(missing_ok=True)
    except Exception as exc:
        logger.debug("Could not update shutdown-notice cleanup queue: %s", exc)
    if deleted:
        logger.info("Deleted %d stale gateway shutdown notification(s)", deleted)
    return deleted
