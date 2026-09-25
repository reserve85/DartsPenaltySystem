from typing import ClassVar

from django.db import models
from django.utils.translation import gettext_lazy as _


class Team(models.Model):
    name = models.CharField(max_length=120, unique=True, verbose_name=_("name"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("created at"))

    def deletable(self) -> bool:
        """B2: a team may be hard-deleted only when it holds no history.

        Players, matchdays and linked captain accounts all block deletion.
        """
        from django.contrib.auth import get_user_model

        User = get_user_model()
        return not (
            self.players.exists()
            or self.matchdays.exists()
            or User.objects.filter(team=self).exists()
        )

    def __str__(self):
        return self.name

    class Meta:
        ordering: ClassVar[list] = ["name"]
        verbose_name = _("team")
        verbose_name_plural = _("teams")
