import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


def _adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test", extra={}))
    adapter._bot = AsyncMock()
    return adapter


@pytest.mark.asyncio
async def test_attach_cron_feedback_edits_existing_message(monkeypatch):
    built = []

    class Button:
        def __init__(self, text, callback_data=None):
            self.text = text
            self.callback_data = callback_data
            built.append((text, callback_data))

    class Markup:
        def __init__(self, rows):
            self.inline_keyboard = rows

    import plugins.platforms.telegram.adapter as tg
    monkeypatch.setattr(tg, "InlineKeyboardButton", Button)
    monkeypatch.setattr(tg, "InlineKeyboardMarkup", Markup)

    adapter = _adapter()
    adapter._bot.edit_message_reply_markup = AsyncMock()
    result = await adapter.attach_cron_feedback("-1001", "77", "abc123def0")

    assert result.success
    assert ("❤ Интересно", "cf:abc123def0:p") in built
    assert ("🚫 Неинтересно", "cf:abc123def0:n") in built
    adapter._bot.edit_message_reply_markup.assert_awaited_once()


@pytest.mark.asyncio
async def test_feedback_callback_is_nonblocking_and_shows_detail_options(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from cron import feedback

    request = feedback.create_request(
        job_id="job1", job_name="Brief", platform="telegram", chat_id="-1001"
    )
    adapter = _adapter()
    query = AsyncMock()
    query.data = f"cf:{request['id']}:p"
    query.message = MagicMock(chat_id=-1001, message_thread_id=5)
    query.message.chat = MagicMock(type="supergroup")
    query.from_user = MagicMock(id="42", first_name="K")
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    update = MagicMock(callback_query=query)

    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
        await adapter._handle_callback_query(update, MagicMock())

    state = feedback.load_request(request["id"])
    assert state["sentiment"] == "positive"
    query.answer.assert_awaited_once()
    query.edit_message_reply_markup.assert_awaited_once()


@pytest.mark.asyncio
async def test_feedback_callback_rejects_forwarded_keyboard_from_other_chat(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from cron import feedback

    request = feedback.create_request(
        job_id="job1", job_name="Brief", platform="telegram", chat_id="-1001"
    )
    adapter = _adapter()
    query = AsyncMock()
    query.data = f"cf:{request['id']}:p"
    query.message = MagicMock(chat_id=-2002, message_thread_id=None)
    query.message.chat = MagicMock(type="supergroup")
    query.from_user = MagicMock(id="42", first_name="K")
    query.answer = AsyncMock()
    update = MagicMock(callback_query=query)

    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
        await adapter._handle_callback_query(update, MagicMock())

    assert feedback.load_request(request["id"])["sentiment"] is None
    assert "another chat" in query.answer.await_args.kwargs["text"]
