"""Regression tests for ``estimate_request_context_tokens``.

The estimate selects a Codex stream-watchdog timeout tier (10k / 50k / 100k
boundaries) and must NOT build a full ``str()`` repr of the entire ``input`` /
``tools`` list on every API call (gratuitous per-call CPU/allocation held under
the GIL). These tests pin the tier magnitude across the Chat Completions
(``messages``) and Responses (``input``) payload shapes, including nested
tool-call structures, so the cheaper per-item walk doesn't shift a payload into
a shorter (more reconnect-prone) timeout tier.
"""

from __future__ import annotations

import sys
import types

# Stub optional heavy imports so the helper module imports cleanly in isolation.
sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())

from agent.chat_completion_helpers import estimate_request_context_tokens as est


def test_bare_string_input_is_len_over_four():
    # A bare string input is length//4 regardless of shape (unchanged behavior).
    assert est({"input": "x" * 44_000}) == 11_000


def test_bare_messages_list_shape():
    assert est([{"role": "user", "content": "y" * 40_000}]) > 9_000


def test_list_input_matches_message_shape_magnitude():
    """Equivalent text in ``messages`` vs ``input`` shape lands within the same
    tier and close in magnitude — the estimate is content-driven, not shape."""
    text = "hello world " * 1_000  # ~12k chars
    as_messages = {"messages": [{"role": "user", "content": text}]}
    as_input = {
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": text}]}
        ]
    }
    em = est(as_messages)
    ei = est(as_input)
    assert em > 0 and ei > 0
    assert abs(em - ei) / max(em, ei) < 0.25


def test_large_input_list_stays_in_100k_tier():
    # 200 messages * ~2200 chars each ~= 440k chars ~= 110k tokens: must stay
    # above the 100k boundary so the widest (180s) idle tier is selected.
    big = [
        {"role": "user", "content": [{"type": "input_text", "text": "y" * 2_200}]}
        for _ in range(200)
    ]
    assert est({"input": big}) > 100_000


def test_nested_tool_schema_is_counted():
    """A large nested tool schema must still push the estimate above the 10k
    TTFB gate (tools are summed, not dropped)."""
    tools = [
        {
            "type": "function",
            "function": {"name": "f", "parameters": {"x": "z" * 44_000}},
        }
    ]
    payload = {"input": [{"role": "user", "content": "hi"}], "tools": tools}
    assert est(payload) > 10_000


def test_messages_shape_tools_are_counted():
    tools = [{"type": "function", "function": {"name": "g", "parameters": {"y": "w" * 44_000}}}]
    payload = {"messages": [{"role": "user", "content": "hi"}], "tools": tools}
    assert est(payload) > 10_000
