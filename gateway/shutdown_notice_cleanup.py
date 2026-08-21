"""Durable cleanup for transient gateway lifecycle notifications.

The process that posts a shutdown/restart banner often exits before an in-memory
TTL task can retract it.  Persist message ids per profile, delete old notices
when the next gateway connects, and let the healthy process retract its own
startup confirmation after a short delay.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, cast

from utils import atomic_json_write

logger = logging.getLogger(__name__)

# Keep the original filename so queues written by FP-2026-002 are migrated in
# place instead of orphaned during rollout.
_QUEUE_FILENAME = "gateway-shutdown-notice-cleanup.json"
_LIFECYCLE_DELETE_WINDOW_SECONDS = 47 * 3600
_SUPPORTED_PLATFORMS = frozenset({"telegram", "slack"})
_QUEUE_LOCK = threading.RLock()


def cleanup_queue_path(hermes_home: Path) -> Path:
    return hermes_home / "state" / _QUEUE_FILENAME


def _fresh_notice(item: object, now: float) -> dict[str, object] | None:
    if not isinstance(item, dict):
        return None
    try:
        created_at = float(item.get("created_at", 0.0))
    except (TypeError, ValueError):
        return None
    if now - created_at >= _LIFECYCLE_DELETE_WINDOW_SECONDS:
        return None
    platform = str(item.get("platform") or "").strip().lower()
    chat_id = str(item.get("chat_id") or "").strip()
    message_id = str(item.get("message_id") or "").strip()
    if not platform or not chat_id or not message_id:
        return None
    return item


def _notice_identity(item: Mapping[str, object]) -> tuple[str, str, str]:
    return (
        str(item.get("platform") or "").strip().lower(),
        str(item.get("chat_id") or ""),
        str(item.get("message_id") or ""),
    )


def _read_notices_locked(path: Path, now: float) -> list[dict[str, object]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception as exc:
        logger.warning("Gateway lifecycle cleanup queue is unreadable: %s", exc)
        return []
    raw_notices = payload.get("notices") if isinstance(payload, dict) else []
    if not isinstance(raw_notices, list):
        return []
    return [
        fresh
        for item in raw_notices
        if (fresh := _fresh_notice(item, now)) is not None
    ]


def _write_notices_locked(path: Path, notices: list[dict[str, object]]) -> None:
    if notices:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, {"version": 2, "notices": notices}, indent=2)
    else:
        path.unlink(missing_ok=True)


def record_lifecycle_notice(
    hermes_home: Path,
    platform: object,
    chat_id: str,
    result: object,
    *,
    kind: str,
    now: float | None = None,
) -> bool:
    """Persist one bot-posted Telegram/Slack lifecycle notice for deletion."""
    platform_name = str(getattr(platform, "value", platform) or "").strip().lower()
    message_id = getattr(result, "message_id", None)
    if platform_name not in _SUPPORTED_PLATFORMS or message_id in (None, ""):
        return False

    path = cleanup_queue_path(hermes_home)
    timestamp = time.time() if now is None else now
    try:
        with _QUEUE_LOCK:
            notices = _read_notices_locked(path, timestamp)
            record: dict[str, object] = {
                "platform": platform_name,
                "chat_id": str(chat_id),
                "message_id": str(message_id),
                "kind": str(kind),
                "created_at": timestamp,
            }
            identity = _notice_identity(record)
            if not any(_notice_identity(item) == identity for item in notices):
                notices.append(record)
            _write_notices_locked(path, notices)
        return True
    except Exception as exc:
        logger.debug("Could not persist gateway lifecycle cleanup record: %s", exc)
        return False


def record_shutdown_notice(
    hermes_home: Path,
    platform: object,
    chat_id: str,
    result: object,
    *,
    now: float | None = None,
) -> bool:
    """Backward-compatible wrapper for shutdown banner persistence."""
    return record_lifecycle_notice(
        hermes_home,
        platform,
        chat_id,
        result,
        kind="shutdown",
        now=now,
    )


def forget_lifecycle_notice(
    hermes_home: Path,
    platform: object,
    chat_id: str,
    message_id: str,
    *,
    now: float | None = None,
) -> bool:
    """Remove one successfully deleted notice from the durable queue."""
    identity = (
        str(getattr(platform, "value", platform) or "").strip().lower(),
        str(chat_id),
        str(message_id),
    )
    path = cleanup_queue_path(hermes_home)
    timestamp = time.time() if now is None else now
    try:
        with _QUEUE_LOCK:
            notices = _read_notices_locked(path, timestamp)
            remaining = [item for item in notices if _notice_identity(item) != identity]
            changed = len(remaining) != len(notices)
            _write_notices_locked(path, remaining)
        return changed
    except Exception as exc:
        logger.debug("Could not update gateway lifecycle cleanup queue: %s", exc)
        return False


async def delete_recorded_lifecycle_notice(
    hermes_home: Path,
    platform: object,
    adapter: Any,
    chat_id: str,
    message_id: str,
    *,
    delay_seconds: float = 0,
) -> bool:
    """Delete one recorded notice and forget it only after provider success."""
    try:
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        delete_fn = getattr(adapter, "delete_message", None)
        if not callable(delete_fn):
            return False
        delete_call = cast(Callable[[str, str], Awaitable[bool]], delete_fn)
        ok = await delete_call(str(chat_id), str(message_id))
        if not ok:
            return False
        await asyncio.to_thread(
            forget_lifecycle_notice,
            hermes_home,
            platform,
            str(chat_id),
            str(message_id),
        )
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("Gateway lifecycle notice delete failed: %s", exc)
        return False


async def cleanup_shutdown_notices(
    hermes_home: Path,
    adapters: Mapping[Any, Any],
    *,
    now: float | None = None,
) -> int:
    """Delete queued Telegram/Slack lifecycle notices after adapters reconnect.

    Failed recent deletes remain queued for the next restart. Expired or
    malformed records are dropped without making a provider API request.
    The historical function name is retained for import compatibility.
    """
    path = cleanup_queue_path(hermes_home)
    if not path.exists():
        return 0

    timestamp = time.time() if now is None else now
    with _QUEUE_LOCK:
        notices = _read_notices_locked(path, timestamp)

    adapters_by_name = {
        str(getattr(platform, "value", platform) or "").strip().lower(): adapter
        for platform, adapter in adapters.items()
    }
    deleted_identities: set[tuple[str, str, str]] = set()
    for notice in notices:
        identity = _notice_identity(notice)
        adapter = adapters_by_name.get(identity[0])
        delete_fn = getattr(adapter, "delete_message", None) if adapter else None
        if not callable(delete_fn):
            continue
        try:
            delete_call = cast(Callable[[str, str], Awaitable[bool]], delete_fn)
            ok = await delete_call(identity[1], identity[2])
        except Exception as exc:
            logger.debug("Gateway lifecycle cleanup failed: %s", exc)
            ok = False
        if ok:
            deleted_identities.add(identity)

    try:
        # Re-read under the lock before committing so a concurrent restart ack
        # appended while provider deletes were in flight is never overwritten.
        with _QUEUE_LOCK:
            current = _read_notices_locked(path, timestamp)
            remaining = [
                item
                for item in current
                if _notice_identity(item) not in deleted_identities
            ]
            _write_notices_locked(path, remaining)
    except Exception as exc:
        logger.debug("Could not update gateway lifecycle cleanup queue: %s", exc)

    deleted = len(deleted_identities)
    if deleted:
        logger.info("Deleted %d stale gateway lifecycle notification(s)", deleted)
    return deleted
