"""Player forms — name/active only; the team assignment lives in the matrix.

Creating or editing a player never touches its team assignment: the
season-scoped assignment is edited exclusively on the combined
**Teams & Players** page (one checkbox per player × team cell).
"""

from typing import ClassVar

from django import forms

from app.players.models import Player


class PlayerForm(forms.ModelForm):
    """Create and edit a player (name + active flag).

    The ``season`` kwarg (the globally active season of the request) is kept
    only for the audit metadata the views log — no assignment is made here.
    """

    class Meta:
        model = Player
        fields: ClassVar[list] = ["name", "active"]

    def __init__(self, *args, season=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.season = season
