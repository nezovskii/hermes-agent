"""Raise the process open-file-descriptor limit at gateway startup.

On macOS, processes launched from launchd / a GUI app inherit a soft
``RLIMIT_NOFILE`` of only **256** (``launchctl limit maxfiles`` → ``256``),
far below the kernel per-process cap (``kern.maxfilesperproc``, typically
tens of thousands). A long-lived gateway running several platform adapters —
each holding persistent httpx keep-alive pools, plus SQLite handles, MCP
clients and the LLM client — sits close enough to 256 that any slow leak or
reconnect churn (see the Telegram connection-pool history: #14210, #18451)
exhausts the table and every ``open()`` starts raising
``OSError: [Errno 24] Too many open files``.

Nothing in the launch path raised this limit before. Bumping the soft limit
toward the hard cap at startup is cheap, needs no privileges (a process may
always raise its soft limit up to its hard limit), and gives comfortable
headroom for the whole multi-adapter process. It does NOT substitute for
fixing real leaks — it converts "dies in days" into "essentially never" and
widens the diagnostic window.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Comfortable headroom for a multi-adapter gateway. Well under macOS's
# ``kern.maxfilesperproc`` (61440 on current macOS) and Linux defaults, so the
# raise succeeds without hitting the kernel ceiling.
_DEFAULT_TARGET = 16384


def raise_fd_limit(target: int = _DEFAULT_TARGET) -> None:
    """Best-effort raise of the soft ``RLIMIT_NOFILE`` toward the hard limit.

    Never raises: a failure to bump the limit must not stop the gateway from
    starting. The chosen soft value is ``min(target, hard)`` so we never exceed
    the hard cap (which would itself raise ``ValueError``).
    """
    try:
        import resource
    except ImportError:  # pragma: no cover — non-Unix
        return

    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, ValueError) as exc:  # pragma: no cover — very unusual
        logger.debug("Could not read RLIMIT_NOFILE: %s", exc)
        return

    cap = target if hard == resource.RLIM_INFINITY else min(target, hard)
    if soft != resource.RLIM_INFINITY and soft >= cap:
        logger.debug("RLIMIT_NOFILE soft limit already %d (>= target %d)", soft, cap)
        return

    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (cap, hard))
    except (OSError, ValueError) as exc:
        logger.warning(
            "Could not raise RLIMIT_NOFILE from %s to %d: %s", soft, cap, exc
        )
        return

    logger.info("Raised RLIMIT_NOFILE soft limit %s -> %d (hard %s)", soft, cap, hard)
