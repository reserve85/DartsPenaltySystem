"""In-process outbox flusher — the container needs no cron.

A single daemon thread ticks every ``NOTIFICATIONS_SCHEDULER_INTERVAL``
seconds (default 30) and calls
``app.notifications.services.flush_outbox()``, which mails every due outbox
row (daily digest at the user's time, quick mode after the coalescing
window).

Safety properties:

* the thread is a DAEMON — it never blocks ``gunicorn``/``manage.py`` exit,
* it only runs when ``NOTIFICATIONS_SCHEDULER`` is true (never under tests),
* the flush itself claims rows atomically, so running this thread NEXT to an
  external ``manage.py flush_notification_outbox`` cron can never send twice,
* every failure is logged — a notification problem never breaks a request
  (project rule), and DB connections opened by the thread are closed after
  each tick so the SQLite pool does not leak.
"""

import logging
import sys
import threading
import time

logger = logging.getLogger("app.notifications")

_lock = threading.Lock()
_thread: threading.Thread | None = None


def _needs_flusher() -> bool:
    """``False`` for one-shot management commands — they exit immediately.

    ``sys.argv`` is the only signal available before the server is up:
    ``manage.py <command>`` must not spawn a 30 s flusher (the command itself
    either flushes once — ``flush_notification_outbox`` — or does not need a
    flusher at all). ``manage.py runserver`` keeps it, and so does every other
    entry point (gunicorn/WSGI, the test client).
    """
    argv = [str(arg).lower() for arg in sys.argv]
    if not argv or not argv[0].endswith(("manage.py", "manage.exe")):
        return True
    return len(argv) > 1 and argv[1] == "runserver"


def start_scheduler() -> threading.Thread | None:
    """Start the flusher thread once per process; returns the (running) thread."""
    global _thread
    if not _needs_flusher():
        logger.debug("Outbox scheduler not started (one-shot management command).")
        return None
    with _lock:
        if _thread is not None and _thread.is_alive():
            return _thread
        _thread = threading.Thread(target=_run, daemon=True, name="notification-outbox-flush")
        _thread.start()
        logger.info("Notification outbox scheduler started (in-process flusher).")
        return _thread


def _run() -> None:
    from django.conf import settings

    interval = max(5, int(getattr(settings, "NOTIFICATION_SCHEDULER_INTERVAL", 30)))
    while True:
        time.sleep(interval)
        try:
            from app.notifications.services import flush_outbox, prune_outbox

            sent = flush_outbox()
            if sent:
                logger.info("Outbox scheduler delivered %s notification(s).", sent)
            # Bounded table (L4): delivered rows older than the retention
            # window are dropped on the same tick — never pending ones.
            prune_outbox()
        except Exception:  # pragma: no cover — a dead thread must never be silent
            logger.exception("Outbox flush failed.")
        finally:
            # The thread owns its own (thread-local) connections — close them
            # so repeated ticks do not leave idle handles behind.
            from django.db import connections

            try:
                connections.close_all()
            except Exception:  # pragma: no cover — defensive
                logger.debug("Closing outbox scheduler connections failed.", exc_info=True)
