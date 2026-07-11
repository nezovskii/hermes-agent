"""Regression tests for the Telegram httpx connection-pool fd-leak.

A long-running gateway accumulated ~200 ESTABLISHED sockets to Telegram's
Bot API per process until it hit ``OSError: [Errno 24] Too many open files``.
Root cause: ``PlatformAdapter.disconnect()`` tore the app down with the
init-gated ``Application.shutdown()`` / ``Bot.shutdown()``. PTB gates those on
the app's ``_initialized`` / ``_requests_initialized`` flags, so an adapter
torn down before ``initialize()`` completed (the reconnect-churn case) no-ops
and leaks the eagerly-built httpx connection pool. Each orphaned pool kept its
keep-alive sockets open until process exit.

The fix force-closes the request transports directly (``HTTPXRequest.shutdown``
gates only on ``client.is_closed``) from a ``finally`` in ``disconnect()``, so
the pool is released regardless of PTB init state. These tests pin that.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import (
    TelegramAdapter,
    _force_close_app_transports,
)


def _make_adapter() -> TelegramAdapter:
    return TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))


def _mock_app_with_request_transports():
    """A PTB app whose clean shutdown() no-ops (simulating an uninitialized
    app) but whose bot exposes two request transports to be force-closed."""
    req_get_updates = MagicMock()
    req_get_updates.shutdown = AsyncMock()
    req_general = MagicMock()
    req_general.shutdown = AsyncMock()

    app = MagicMock()
    app.updater = None          # skip the updater.stop() path
    app.running = False         # skip the app.stop() path
    app.shutdown = AsyncMock()  # init-gated no-op: "succeeds" yet closes nothing
    app.bot._request = (req_get_updates, req_general)
    return app, req_get_updates, req_general


@pytest.mark.asyncio
async def test_force_close_app_transports_shuts_down_every_request():
    app, req_get_updates, req_general = _mock_app_with_request_transports()

    await _force_close_app_transports(app)

    req_get_updates.shutdown.assert_awaited_once()
    req_general.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_force_close_app_transports_tolerates_missing_bot():
    # No bot / no _request must not raise (best-effort contract).
    app = MagicMock()
    app.bot = None
    await _force_close_app_transports(app)
    await _force_close_app_transports(None)


@pytest.mark.asyncio
async def test_disconnect_force_closes_transports_when_app_uninitialized():
    """The core regression: disconnect() must release the httpx pool even when
    app.shutdown() no-ops because the app never finished initialize()."""
    adapter = _make_adapter()
    app, req_get_updates, req_general = _mock_app_with_request_transports()
    adapter._app = app

    # Isolate the teardown from unrelated disconnect() housekeeping.
    adapter._set_status_indicator = AsyncMock()
    adapter._cancel_pending_delivery_tasks = AsyncMock()
    adapter._release_platform_lock = MagicMock()

    await adapter.disconnect()

    # Clean path attempted first...
    app.shutdown.assert_awaited_once()
    # ...but the finally force-closed the transports regardless of init state,
    # which is what actually releases the leaked connection pool.
    req_get_updates.shutdown.assert_awaited_once()
    req_general.shutdown.assert_awaited_once()
    assert adapter._app is None


@pytest.mark.asyncio
async def test_disconnect_force_closes_transports_even_when_shutdown_raises():
    """A raising app.shutdown() must not prevent the pool from being released."""
    adapter = _make_adapter()
    app, req_get_updates, req_general = _mock_app_with_request_transports()
    app.shutdown = AsyncMock(side_effect=RuntimeError("boom"))
    adapter._app = app

    adapter._set_status_indicator = AsyncMock()
    adapter._cancel_pending_delivery_tasks = AsyncMock()
    adapter._release_platform_lock = MagicMock()

    await adapter.disconnect()

    req_get_updates.shutdown.assert_awaited_once()
    req_general.shutdown.assert_awaited_once()
    assert adapter._app is None
