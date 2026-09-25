from django.apps import AppConfig


class DashboardConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "app.dashboard"
    label = "dashboard"
    verbose_name = "Dashboard"
