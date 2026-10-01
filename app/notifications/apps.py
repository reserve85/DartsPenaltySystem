from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "app.notifications"
    # The package label "notifications" belongs to django-notifications-hq —
    # THIS app owns the models (NotificationOutbox) from now on.
    label = "app_notifications"
    verbose_name = "Notifications"

    def ready(self):
        """Start the in-process outbox flusher (digest + coalescing window).

        Opt-out via ``NOTIFICATIONS_SCHEDULER=False`` (e.g. when an external
        cron already runs ``manage.py flush_notification_outbox``). Tests never
        start it — see ``config/settings.py`` (``TESTING``).
        """
        from django.conf import settings

        if getattr(settings, "NOTIFICATION_SCHEDULER", False):
            from app.notifications.scheduler import start_scheduler

            start_scheduler()
