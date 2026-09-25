from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "app.core"
    label = "core"
    verbose_name = "Core"

    def ready(self):
        from app.core import checks  # noqa: F401 — registers darts.W001
