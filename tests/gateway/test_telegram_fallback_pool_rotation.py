"""Regression tests for Telegram fallback pool accumulation after reconnects."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram import telegram_network as tnet
from plugins.platforms.telegram.adapter import TelegramAdapter


class _RecordingTransport:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.__class__.instances.append(self)

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_fallback_reset_rotates_and_closes_inner_pools(monkeypatch):
    _RecordingTransport.instances = []
    monkeypatch.setattr(tnet.httpx, "AsyncHTTPTransport", _RecordingTransport)
    transport = tnet.TelegramFallbackTransport(
        ["149.154.167.220", "149.154.166.110"],
        limits=MagicMock(),
    )
    old_primary = transport._primary
    old_fallbacks = dict(transport._fallbacks)
    transport._sticky_ip = "149.154.167.220"

    await transport.reset()

    assert transport._primary is not old_primary
    assert all(
        transport._fallbacks[ip] is not old
        for ip, old in old_fallbacks.items()
    )
    assert getattr(old_primary, "closed", False) is True
    assert all(getattr(old, "closed", False) is True for old in old_fallbacks.values())
    assert transport._sticky_ip is None
    await transport.aclose()


@pytest.mark.asyncio
async def test_polling_drain_rotates_fallback_without_rebuilding_ptb_client(monkeypatch):
    _RecordingTransport.instances = []
    monkeypatch.setattr(tnet.httpx, "AsyncHTTPTransport", _RecordingTransport)
    transport = tnet.TelegramFallbackTransport(["149.154.167.220"])
    transport.reset = AsyncMock(wraps=transport.reset)

    polling_request = MagicMock()
    polling_request._client._transport = transport
    polling_request.shutdown = AsyncMock()
    polling_request.initialize = AsyncMock()

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    app = MagicMock()
    app.bot._request = (polling_request, MagicMock())
    adapter._app = app

    await adapter._drain_polling_connections()

    transport.reset.assert_awaited_once()
    polling_request.shutdown.assert_not_awaited()
    polling_request.initialize.assert_not_awaited()
    await transport.aclose()
