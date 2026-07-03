"""Deterministic context rehydration for fresh gateway sessions.

This module intentionally avoids LLM calls.  It builds a bounded packet from
trusted local gateway state (room policy + exact session lane history) so a
fresh/reset Telegram topic or Slack thread can restart with useful context
without asking the user to retell the story.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional
import logging
import re
import time

from gateway.config import ContextRehydrationConfig, GatewayConfig, Platform
from gateway.platforms.base import resolve_channel_prompt
from gateway.session import SessionSource

logger = logging.getLogger(__name__)


@dataclass
class RehydrationPacket:
    text: str = ""
    source_counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class LaneSessionSnippet:
    session_id: str
    title: str = ""
    started_at: float = 0.0
    last_active: float = 0.0
    messages: list[dict[str, Any]] = field(default_factory=list)


_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*\S+"),
    re.compile(r"\b(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.I),
)


def _clean_text(value: Any, *, max_chars: int = 700) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.strip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)}[REDACTED]", text)
    if max_chars and len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def _format_ts(ts: float) -> str:
    if not ts:
        return "unknown time"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "unknown time"


def _thread_matches(row_value: Any, source_value: Any) -> bool:
    if row_value is None and source_value is None:
        return True
    return str(row_value or "") == str(source_value or "")


class ContextRehydrator:
    def __init__(
        self,
        gateway_config: GatewayConfig,
        session_db: Any = None,
        log: Optional[logging.Logger] = None,
    ) -> None:
        self.gateway_config = gateway_config
        self.config: ContextRehydrationConfig = gateway_config.context_rehydration
        self.session_db = session_db
        self.log = log or logger

    def should_rehydrate(
        self,
        *,
        is_new_session: bool,
        was_auto_reset: bool,
        source: SessionSource,
    ) -> bool:
        cfg = self.config
        if not cfg.enabled:
            return False
        if cfg.trigger == "manual_only":
            return False
        if cfg.trigger == "fresh_or_reset_session":
            return bool(is_new_session or was_auto_reset)
        if cfg.trigger == "every_session_after_idle":
            return bool(is_new_session or was_auto_reset)
        return bool(is_new_session or was_auto_reset)

    def build_packet(
        self,
        *,
        source: SessionSource,
        current_text: str = "",
        room_policy: str | None = None,
        session_id: str | None = None,
    ) -> RehydrationPacket:
        cfg = self.config
        if not cfg.enabled:
            return RehydrationPacket()

        warnings: list[str] = []
        counts: dict[str, int] = {}
        sections: list[str] = [
            "[CONTEXT REHYDRATION — trusted local gateway context]",
            f"Scope: {self._scope_label(source)}",
        ]

        if cfg.include_room_policy:
            policy = room_policy or self.resolve_room_policy(source)
            if policy:
                counts["room_policy"] = 1
                sections.extend(["", "## Room policy", _clean_text(policy, max_chars=1200)])

        if cfg.include_session_lane_recap:
            try:
                snippets = self.fetch_lane_snippets(source=source, exclude_session_id=session_id)
                if snippets:
                    counts["session_db"] = len(snippets)
                    sections.extend(["", "## Recent lane recap"])
                    sections.extend(self._format_lane_snippets(snippets))
            except Exception as exc:  # pragma: no cover - defensive runtime guard
                warnings.append(f"session_db:{exc.__class__.__name__}")
                self.log.debug("context rehydration session-db lookup failed", exc_info=True)

        if warnings:
            sections.extend(["", "## Rehydration warnings", ", ".join(warnings)])

        sections.append("[/CONTEXT REHYDRATION]")
        text = "\n".join(part for part in sections if part is not None).strip()
        text = self._cap_packet(text, cfg.max_chars)
        return RehydrationPacket(text=text, source_counts=counts, warnings=warnings)

    def resolve_room_policy(self, source: SessionSource) -> str | None:
        platform_cfg = self.gateway_config.platforms.get(source.platform)
        if not platform_cfg:
            return None
        extra = platform_cfg.extra or {}
        keys: list[str] = []
        if source.thread_id and source.platform == Platform.TELEGRAM:
            keys.append(str(source.thread_id))
        if source.thread_id:
            keys.append(str(source.thread_id))
        if source.chat_id:
            keys.append(str(source.chat_id))
        parent_id = source.parent_chat_id or source.chat_id
        for key in keys:
            prompt = resolve_channel_prompt(extra, key, str(parent_id) if parent_id else None)
            if prompt:
                return prompt
        prompt = resolve_channel_prompt(extra, str(source.chat_id), str(parent_id) if parent_id else None)
        return prompt

    def fetch_lane_snippets(
        self,
        *,
        source: SessionSource,
        exclude_session_id: str | None = None,
    ) -> list[LaneSessionSnippet]:
        db = self.session_db
        if db is None or not hasattr(db, "_conn"):
            return []
        settings = self.config.session_db
        cutoff = time.time() - max(settings.lookback_days, 1) * 86400
        params: list[Any] = [
            source.platform.value,
            str(source.chat_id),
            str(source.chat_type or ""),
            cutoff,
            max(settings.max_sessions * 4, settings.max_sessions),
        ]
        query = """
            SELECT s.*,
                   COALESCE((SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = s.id), s.started_at) AS last_active
            FROM sessions s
            WHERE s.source = ?
              AND s.chat_id = ?
              AND COALESCE(s.chat_type, '') = ?
              AND s.started_at >= ?
            ORDER BY last_active DESC, s.started_at DESC
            LIMIT ?
        """
        with db._lock:
            rows = db._conn.execute(query, params).fetchall()
        snippets: list[LaneSessionSnippet] = []
        for row in rows:
            row_dict = dict(row)
            sid = row_dict.get("id")
            if not sid or sid == exclude_session_id:
                continue
            if not _thread_matches(row_dict.get("thread_id"), source.thread_id):
                continue
            messages = self._select_snippet_messages(db.get_messages(sid))
            if not messages:
                continue
            snippets.append(
                LaneSessionSnippet(
                    session_id=sid,
                    title=row_dict.get("title") or "",
                    started_at=float(row_dict.get("started_at") or 0),
                    last_active=float(row_dict.get("last_active") or row_dict.get("started_at") or 0),
                    messages=messages,
                )
            )
            if len(snippets) >= settings.max_sessions:
                break
        return snippets

    def _select_snippet_messages(self, messages: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        limit = max(self.config.session_db.max_messages_per_session, 1)
        usable = [
            m for m in messages
            if m.get("role") in {"user", "assistant"} and _clean_text(m.get("content"), max_chars=1)
        ]
        if not usable:
            return []
        if len(usable) <= limit:
            return usable
        if not self.config.session_db.include_bookends:
            return usable[-limit:]
        head_count = min(2, limit)
        tail_count = max(limit - head_count, 0)
        return usable[:head_count] + usable[-tail_count:]

    def _format_lane_snippets(self, snippets: list[LaneSessionSnippet]) -> list[str]:
        lines: list[str] = []
        for snippet in snippets:
            title = f" — {snippet.title}" if snippet.title else ""
            lines.append(f"- Session {snippet.session_id} ({_format_ts(snippet.last_active)}){title}")
            for msg in snippet.messages:
                role = msg.get("role", "message")
                content = _clean_text(msg.get("content"), max_chars=420)
                if content:
                    lines.append(f"  - {role}: {content}")
        return lines

    def _scope_label(self, source: SessionSource) -> str:
        parts = [source.platform.value, source.chat_type or "chat", str(source.chat_id)]
        if source.thread_id:
            parts.append(f"thread {source.thread_id}")
        if source.chat_name:
            parts.append(_clean_text(source.chat_name, max_chars=120))
        return " / ".join(parts)

    @staticmethod
    def _cap_packet(text: str, max_chars: int) -> str:
        if max_chars <= 0 or len(text) <= max_chars:
            return text
        suffix = "\n[...context rehydration truncated...]\n[/CONTEXT REHYDRATION]"
        budget = max(max_chars - len(suffix), 0)
        return text[:budget].rstrip() + suffix
