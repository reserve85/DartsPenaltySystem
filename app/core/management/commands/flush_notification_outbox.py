"""``manage.py flush_notification_outbox`` — deliver due outbox rows.

The in-process scheduler (``NOTIFICATIONS_SCHEDULER``) already ticks every
30 s; this command is the CRON escape hatch for setups that prefer an
external trigger (host cron, Synology Task Scheduler, systemd timer)::

    * * * * * docker exec darts manage.py flush_notification_outbox

Both paths share the same atomic claim in
``app.notifications.services.flush_outbox()``, so running them side by side
can never send a message twice. Idempotent — running it when nothing is due
just prints 0.
"""

from django.core.management.base import BaseCommand

from app.notifications.services import flush_outbox


class Command(BaseCommand):
    help = "Send all due outbox notifications (daily digest + coalesced penalty mails)."

    def handle(self, *args, **options):
        sent = flush_outbox()
        self.stdout.write(self.style.SUCCESS(f"{sent} notification(s) sent."))
