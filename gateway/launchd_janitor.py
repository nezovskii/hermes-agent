"""Reap stray transient launchd jobs at gateway startup (macOS).

Hermes' autonomous maintenance flows sometimes register a *run-once* helper
script via ``launchctl submit`` (e.g. "restart the gateway once to apply a
config change" or "rehome the kanban dispatcher"). ``launchctl submit`` defaults
to ``KeepAlive=true``, so a script that ends in
``launchctl kickstart -k gui/<uid>/ai.hermes.gateway`` is re-run forever — the
gateway is SIGTERM'd every ~20-30s in an unbreakable loop (and SQLite DBs it
kills mid-write get corrupted, which spawns yet more "fix it" jobs). Two such
jobs occurred on 2026-07-08 (``ai.hermes.deferred-default-restart-<ts>`` and
``ai.hermes.rehome-kanban-dispatcher-<ts>``).

Source-level guards (``tools.approval`` DANGEROUS_PATTERNS,
``cron.lifecycle_guard``) reduce how often the agent creates these, but cannot
cover every path: a ``launchctl submit`` buried inside an agent-authored script,
an external monitor, or a label that lacks the ``gateway`` token the label-based
guards key on. This janitor is the catch-all backstop — it observes the *effect*
(a transient, timestamp-suffixed, KeepAlive ``ai.hermes.*`` job that is not one
of the known persistent gateway services) and boots it out.

Why it runs at boot rather than on a timer: during an active restart loop the
gateway lives only ~20s per cycle, so a 60s housekeeping tick never fires.
Sweeping on every boot means each restart reaps the offender, so the loop
self-heals within a cycle or two.

Never raises: a failure here must not stop the gateway from starting.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys

logger = logging.getLogger(__name__)

# Transient job labels end in a separator ('.' or '-') followed by a
# unix-timestamp-ish run of digits, e.g.
#   ai.hermes.rehome-kanban-dispatcher-1783505635
#   ai.hermes.deferred-default-gateway-restart.1783501411
_TRANSIENT_LABEL_RE = re.compile(r"^ai\.hermes\..*[._-]\d{9,}$")

# Persistent services that must NEVER be reaped: the default gateway and any
# per-profile gateway (ai.hermes.gateway-<profile>). Anchored so only exact
# gateway service labels match — a label that merely *contains* "gateway"
# (e.g. ai.hermes.deferred-default-gateway-restart.<ts>) is NOT protected.
_PROTECTED_LABEL_RE = re.compile(r"^ai\.hermes\.gateway(?:-[a-z0-9][a-z0-9-]*)?$")


def _run(args, timeout=10):
    """Thin, test-seam wrapper around subprocess.run (never raises on nonzero)."""
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False
    )


def _list_labels():
    """Return the set of currently-loaded launchd job labels.

    ``launchctl list`` prints ``PID<TAB>Status<TAB>Label`` rows (with a header);
    the label is the last column. The header row's label is ``Label``, which the
    transient regex ignores, so we do not special-case it.
    """
    out = _run(["launchctl", "list"]).stdout or ""
    labels = set()
    for line in out.splitlines():
        parts = line.split("\t") if "\t" in line else line.split()
        if parts:
            labels.add(parts[-1].strip())
    return labels


def _is_keepalive(uid, label):
    """True only if launchd positively reports this job as KeepAlive.

    Conservative: any uncertainty (print fails, label vanished) returns False so
    we never reap a job we could not verify.
    """
    res = _run(["launchctl", "print", f"gui/{uid}/{label}"])
    if res.returncode != 0:
        return False
    return "keepalive" in (res.stdout or "").lower()


def reap_stray_transient_launchd_jobs() -> int:
    """Boot out stray transient KeepAlive ``ai.hermes.*`` jobs.

    Returns the number of jobs reaped. Never raises — a failure to reap must not
    stop the gateway from starting. No-op on non-macOS platforms.
    """
    if sys.platform != "darwin":
        return 0
    getuid = getattr(os, "getuid", None)
    if getuid is None:  # pragma: no cover — non-Unix
        return 0
    uid = getuid()

    try:
        labels = _list_labels()
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("launchd janitor: could not list jobs: %s", exc)
        return 0

    reaped = 0
    for label in sorted(labels):
        try:
            if _PROTECTED_LABEL_RE.match(label):
                continue
            if not _TRANSIENT_LABEL_RE.match(label):
                continue
            if not _is_keepalive(uid, label):
                continue
            res = _run(["launchctl", "bootout", f"gui/{uid}/{label}"])
            if res.returncode != 0:
                # Legacy fallback for launchctl-submit-created jobs.
                res = _run(["launchctl", "remove", label])
            if res.returncode == 0:
                reaped += 1
                logger.warning(
                    "launchd janitor: reaped stray transient KeepAlive job %r "
                    "(a run-once maintenance script mis-submitted with KeepAlive "
                    "that SIGTERM-loops the gateway).",
                    label,
                )
            else:
                logger.warning(
                    "launchd janitor: found stray transient job %r but could not "
                    "remove it (bootout/remove failed: %s).",
                    label, (res.stderr or "").strip(),
                )
        except Exception as exc:  # pragma: no cover — one bad job must not abort the sweep
            logger.debug("launchd janitor: error handling %r: %s", label, exc)

    if reaped:
        logger.warning(
            "launchd janitor: reaped %d stray launchd job(s) at boot — see "
            "gateway/launchd_janitor.py for why these form restart loops.",
            reaped,
        )
    return reaped
