"""Durable, non-blocking feedback state for cron deliveries."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_LOCK = threading.RLock()

POSITIVE_OPTIONS = ("content", "delivery", "structure", "format", "practical")
NEGATIVE_OPTIONS = (
    "not_applicable",
    "topic",
    "writing",
    "format",
    "repetition",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def feedback_dir() -> Path:
    path = _home() / "cron" / "feedback"
    path.mkdir(parents=True, exist_ok=True)
    return path


def feedback_log_path() -> Path:
    return feedback_dir() / "events.jsonl"


def _request_path(feedback_id: str) -> Path:
    if not feedback_id or any(c not in "0123456789abcdef" for c in feedback_id):
        raise ValueError("invalid feedback id")
    return feedback_dir() / f"{feedback_id}.json"


def _write(record: Dict[str, Any]) -> None:
    path = _request_path(record["id"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_request(feedback_id: str) -> Optional[Dict[str, Any]]:
    path = _request_path(feedback_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def create_request(
    *,
    job_id: str,
    job_name: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "id": uuid.uuid4().hex[:10],
        "job_id": str(job_id),
        "job_name": str(job_name),
        "platform": str(platform),
        "chat_id": str(chat_id),
        "thread_id": str(thread_id) if thread_id is not None else None,
        "created_at": _now(),
        "sentiment": None,
        "selections": [],
        "user_id": None,
        "finalized_at": None,
    }
    with _LOCK:
        _write(record)
    return record


def options_for(record: Dict[str, Any]) -> List[str]:
    if record.get("sentiment") == "positive":
        return list(POSITIVE_OPTIONS)
    if record.get("sentiment") == "negative":
        return list(NEGATIVE_OPTIONS)
    return []


def apply_action(feedback_id: str, action: str, *, user_id: str) -> Dict[str, Any]:
    with _LOCK:
        record = load_request(feedback_id)
        if record is None:
            raise KeyError(feedback_id)
        if record.get("finalized_at"):
            return record

        if action in {"positive", "negative"}:
            record["sentiment"] = action
            record["selections"] = []
        elif action.startswith("toggle:"):
            option = action.split(":", 1)[1]
            if option not in options_for(record):
                raise ValueError("invalid feedback option")
            selections = list(record.get("selections") or [])
            if option in selections:
                selections.remove(option)
            else:
                selections.append(option)
            record["selections"] = selections
        elif action == "done":
            if record.get("sentiment") not in {"positive", "negative"}:
                raise ValueError("sentiment required")
            record["finalized_at"] = _now()
        else:
            raise ValueError("invalid feedback action")

        record["user_id"] = str(user_id)
        _write(record)
        if action == "done":
            with feedback_log_path().open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record
