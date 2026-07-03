from gateway.config import GatewayConfig


def test_context_rehydration_defaults_disabled():
    cfg = GatewayConfig.from_dict({})

    assert cfg.context_rehydration.enabled is False
    assert cfg.context_rehydration.trigger == "fresh_or_reset_session"
    assert cfg.context_rehydration.include_session_lane_recap is True


def test_context_rehydration_parses_nested_config():
    cfg = GatewayConfig.from_dict({
        "context_rehydration": {
            "enabled": "true",
            "trigger": "manual_only",
            "max_chars": "1234",
            "include": {
                "session_lane_recap": "true",
                "sibling_thread_recap": "true",
                "gbrain_private": "true",
                "gbrain_team": "false",
            },
            "session_db": {
                "lookback_days": "9",
                "max_sessions": "2",
                "max_messages_per_session": "7",
                "include_bookends": "false",
            },
            "gbrain": {
                "max_results": "3",
                "max_chars": "777",
                "query_from": ["current_user_message"],
            },
            "privacy": {
                "private_brain_home": "~/.gbrain-personal-eve",
                "allow_team_gbrain": "true",
                "allow_private_gbrain": "true",
                "forbidden_private_brains": "~/.gbrain-personal-konstantin",
            },
        }
    })

    cr = cfg.context_rehydration
    assert cr.enabled is True
    assert cr.trigger == "manual_only"
    assert cr.max_chars == 1234
    assert cr.include_session_lane_recap is True
    assert cr.include_sibling_thread_recap is True
    assert cr.include_gbrain_private is True
    assert cr.include_gbrain_team is False
    assert cr.session_db.lookback_days == 9
    assert cr.session_db.max_sessions == 2
    assert cr.session_db.max_messages_per_session == 7
    assert cr.session_db.include_bookends is False
    assert cr.gbrain.max_results == 3
    assert cr.gbrain.max_chars == 777
    assert cr.gbrain.query_from == ("current_user_message",)
    assert cr.privacy.private_brain_home == "~/.gbrain-personal-eve"
    assert cr.privacy.allow_team_gbrain is True
    assert cr.privacy.allow_private_gbrain is True
    assert cr.privacy.forbidden_private_brains == ("~/.gbrain-personal-konstantin",)


def test_context_rehydration_invalid_trigger_falls_back():
    cfg = GatewayConfig.from_dict({
        "context_rehydration": {"enabled": True, "trigger": "surprise_me"}
    })

    assert cfg.context_rehydration.trigger == "fresh_or_reset_session"
