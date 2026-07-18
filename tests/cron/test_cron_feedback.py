import json


def test_feedback_request_supports_multiselect_and_finalize(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from cron import feedback

    request = feedback.create_request(
        job_id="job123",
        job_name="Morning Brief",
        platform="telegram",
        chat_id="-1001",
        thread_id="5",
    )

    positive = feedback.apply_action(request["id"], "positive", user_id="42")
    assert positive["sentiment"] == "positive"
    assert positive["finalized_at"] is None

    selected = feedback.apply_action(request["id"], "toggle:content", user_id="42")
    assert selected["selections"] == ["content"]
    selected = feedback.apply_action(request["id"], "toggle:format", user_id="42")
    assert selected["selections"] == ["content", "format"]
    selected = feedback.apply_action(request["id"], "toggle:content", user_id="42")
    assert selected["selections"] == ["format"]

    done = feedback.apply_action(request["id"], "done", user_id="42")
    assert done["finalized_at"]

    rows = [json.loads(line) for line in feedback.feedback_log_path().read_text().splitlines()]
    assert rows[-1]["job_id"] == "job123"
    assert rows[-1]["sentiment"] == "positive"
    assert rows[-1]["selections"] == ["format"]


def test_negative_reasons_are_distinct_from_positive(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from cron import feedback

    request = feedback.create_request(
        job_id="job456",
        job_name="Body Lawyer",
        platform="telegram",
        chat_id="1",
    )
    state = feedback.apply_action(request["id"], "negative", user_id="42")

    assert state["sentiment"] == "negative"
    assert "not_applicable" in feedback.options_for(state)
    assert "content" not in feedback.options_for(state)


def test_scheduler_attaches_to_last_delivered_chunk(monkeypatch):
    from gateway.platforms.base import SendResult
    from cron import scheduler
    import agent.async_utils
    import cron.feedback

    calls = []

    class Adapter:
        async def attach_cron_feedback(self, chat_id, message_id, feedback_id):
            calls.append((chat_id, message_id, feedback_id))
            return SendResult(success=True, message_id=message_id)

    class Future:
        def __init__(self, coro):
            self.coro = coro

        def result(self, timeout):
            import asyncio
            return asyncio.run(self.coro)

    def schedule(coro, loop):
        return Future(coro)

    monkeypatch.setattr(agent.async_utils, "safe_schedule_threadsafe", schedule)
    monkeypatch.setattr(
        cron.feedback,
        "create_request",
        lambda **kwargs: {"id": "feed123abc"},
    )
    sent = SendResult(
        success=True,
        message_id="11",
        raw_response={"message_ids": ["11", "12"]},
    )

    assert scheduler._maybe_attach_cron_feedback(
        {"id": "job1", "name": "Brief", "feedback": True},
        Adapter(),
        "-1001",
        "5",
        "telegram",
        sent,
        object(),
    )
    assert calls[0] == ("-1001", "12", "feed123abc")
