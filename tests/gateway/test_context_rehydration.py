import time

from gateway.config import GatewayConfig, Platform
from gateway.context_rehydration import ContextRehydrator
from gateway.session import SessionSource, build_session_key
from hermes_state import SessionDB


def _cfg(extra=None, context_rehydration=None):
    return GatewayConfig.from_dict({
        "platforms": {
            "telegram": {
                "enabled": True,
                "token": "test-token",
                "extra": extra or {},
            },
            "slack": {
                "enabled": True,
                "token": "test-token",
                "extra": extra or {},
            },
        },
        "context_rehydration": context_rehydration or {
            "enabled": True,
            "max_chars": 4000,
            "session_db": {
                "lookback_days": 30,
                "max_sessions": 3,
                "max_messages_per_session": 4,
            },
        },
    })


def _source(thread_id="5", chat_id="-1001", platform=Platform.TELEGRAM, chat_type="group"):
    return SessionSource(
        platform=platform,
        chat_id=chat_id,
        chat_type=chat_type,
        thread_id=thread_id,
        user_id="u1",
        user_name="Konstantin",
    )


def _seed_session(db, source, session_id, messages):
    db.create_session(
        session_id,
        source.platform.value,
        user_id=source.user_id,
        session_key=build_session_key(source),
        chat_id=source.chat_id,
        chat_type=source.chat_type,
        thread_id=source.thread_id,
    )
    base = time.time() - 100
    for idx, (role, content) in enumerate(messages):
        db.append_message(session_id, role, content, timestamp=base + idx)


def test_session_lane_recap_stays_inside_telegram_topic(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    source_5 = _source(thread_id="5")
    source_6 = _source(thread_id="6")
    _seed_session(db, source_5, "s-topic-5", [("user", "briefings question"), ("assistant", "briefings answer")])
    _seed_session(db, source_6, "s-topic-6", [("user", "trading question"), ("assistant", "trading answer")])

    packet = ContextRehydrator(_cfg(), db).build_packet(
        source=source_5,
        current_text="what did we decide?",
        session_id="current-session",
    )

    assert "briefings question" in packet.text
    assert "briefings answer" in packet.text
    assert "trading question" not in packet.text
    assert packet.source_counts["session_db"] == 1


def test_current_session_is_excluded(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    source = _source(thread_id="5")
    _seed_session(db, source, "current-session", [("user", "current should not appear")])
    _seed_session(db, source, "older-session", [("user", "older should appear")])

    packet = ContextRehydrator(_cfg(), db).build_packet(
        source=source,
        current_text="continue",
        session_id="current-session",
    )

    assert "older should appear" in packet.text
    assert "current should not appear" not in packet.text


def test_tool_messages_are_not_included(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    source = _source(thread_id="5")
    db.create_session(
        "older-session",
        source.platform.value,
        user_id=source.user_id,
        session_key=build_session_key(source),
        chat_id=source.chat_id,
        chat_type=source.chat_type,
        thread_id=source.thread_id,
    )
    db.append_message("older-session", "tool", "raw tool output secret_token=abc")
    db.append_message("older-session", "user", "safe user text")

    packet = ContextRehydrator(_cfg(), db).build_packet(source=source, session_id="current")

    assert "safe user text" in packet.text
    assert "raw tool output" not in packet.text


def test_disabled_config_returns_empty_packet(tmp_path):
    cfg = _cfg(context_rehydration={"enabled": False})
    packet = ContextRehydrator(cfg, SessionDB(tmp_path / "state.db")).build_packet(source=_source())

    assert packet.text == ""
    assert packet.source_counts == {}
