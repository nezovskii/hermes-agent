from gateway.config import GatewayConfig, Platform
from gateway.context_rehydration import ContextRehydrator
from gateway.session import SessionSource


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
            "include": {"room_policy": True, "session_lane_recap": False},
        },
    })


def test_room_policy_uses_explicit_runtime_policy_first():
    cfg = _cfg(extra={"channel_prompts": {"5": "configured topic policy"}})
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        thread_id="5",
        user_id="u1",
    )

    packet = ContextRehydrator(cfg).build_packet(
        source=source,
        room_policy="runtime event policy",
    )

    assert "runtime event policy" in packet.text
    assert "configured topic policy" not in packet.text
    assert packet.source_counts["room_policy"] == 1


def test_room_policy_uses_topic_specific_prompt_before_parent():
    cfg = _cfg(extra={
        "channel_prompts": {
            "-1001": "parent group policy",
            "5": "briefings topic policy",
        }
    })
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        thread_id="5",
        user_id="u1",
    )

    packet = ContextRehydrator(cfg).build_packet(source=source)

    assert "briefings topic policy" in packet.text
    assert "parent group policy" not in packet.text


def test_slack_thread_uses_parent_channel_prompt():
    cfg = _cfg(extra={"channel_prompts": {"C123": "team-only slack policy"}})
    source = SessionSource(
        platform=Platform.SLACK,
        chat_id="C123",
        chat_type="channel",
        thread_id="171.123",
        parent_chat_id="C123",
        user_id="U1",
    )

    packet = ContextRehydrator(cfg).build_packet(source=source)

    assert "team-only slack policy" in packet.text


def test_room_policy_can_be_disabled():
    cfg = _cfg(
        extra={"channel_prompts": {"5": "briefings topic policy"}},
        context_rehydration={
            "enabled": True,
            "include": {"room_policy": False, "session_lane_recap": False},
        },
    )
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        thread_id="5",
        user_id="u1",
    )

    packet = ContextRehydrator(cfg).build_packet(source=source)

    assert "briefings topic policy" not in packet.text
    assert "room_policy" not in packet.source_counts
