from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "app.notifications"
    # The package label "notifications" belongs to django-notifications-hq;
    # this app has no models, so the rename is migration-free.
    label = "app_notifications"
    verbose_name = "Notifications"
