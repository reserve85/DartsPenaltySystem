from django.apps import AppConfig


class PlayersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "app.players"
    label = "players"
    verbose_name = "Players"
