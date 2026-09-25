"""Idempotent bootstrap — role groups, superuser from env, is_staff sync, catalog.

Run on every container start (see ``entrypoint.sh``); safe to run repeatedly:

* ensures the ``Admin``/``Captain``/``Player`` groups exist,
* creates the superuser from ``DJANGO_SUPERUSER_EMAIL``/``DJANGO_SUPERUSER_PASSWORD``
  when it does not exist yet and adds it to the ``Admin`` group (H1),
* re-syncs ``is_staff`` for every user against current group membership (H1),
* inserts the default penalty catalog when the catalog is empty.

``--with-demo`` seeds a small demo dataset (2 teams, 8 players, 3 matchdays,
sample penalties incl. one group penalty) — skipped when teams already exist.
"""

import os
from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand
from django.utils import timezone

from app.core.permissions import GROUP_ADMIN, GROUP_CAPTAIN, GROUP_PLAYER
from app.matchdays.models import Matchday, MatchdayPlayer, Season
from app.penalties.models import PenaltyCatalogItem, PenaltyType
from app.penalties.services import assign_group_penalty, assign_penalty
from app.players.models import Player
from app.teams.models import Team

User = get_user_model()

# Fresh installs start with ONLY the "Manual" entry — each club adds its own
# catalog items (admin UI). "Manual" is the catch-all for individual amounts.
DEFAULT_CATALOG = [
    ("Manual", "1", PenaltyType.MANUAL),
]


class Command(BaseCommand):
    help = "Idempotent bootstrap: role groups, superuser from env, is_staff sync, default catalog."

    def add_arguments(self, parser):
        parser.add_argument(
            "--with-demo",
            action="store_true",
            help="Seed demo teams/players/matchdays/penalties (skipped if teams exist).",
        )

    def handle(self, *args, **options):
        self._ensure_groups()
        self._ensure_superuser()
        self._sync_is_staff()
        self._ensure_season()
        self._ensure_catalog()
        if options["with_demo"]:
            self._seed_demo()
        self.stdout.write(self.style.SUCCESS("Bootstrap complete."))

    # ------------------------------------------------------------------
    def _ensure_groups(self) -> None:
        for name in (GROUP_ADMIN, GROUP_CAPTAIN, GROUP_PLAYER):
            Group.objects.get_or_create(name=name)

    def _ensure_superuser(self) -> None:
        email = (os.environ.get("DJANGO_SUPERUSER_EMAIL") or "").strip().lower()
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD") or ""
        if not email or not password:
            return
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            user = User(
                email=email,
                is_superuser=True,
                is_staff=True,
                is_active=True,
                # The system account is never subject to the approval workflow.
                approval_status="approved",
            )
            user.set_password(password)
            user.save()
            self.stdout.write(f"Created superuser {email}.")
        user.groups.add(Group.objects.get(name=GROUP_ADMIN))

    def _sync_is_staff(self) -> None:
        """Re-derive is_staff for all users from group membership (H1)."""
        admin_ids = set(Group.objects.get(name=GROUP_ADMIN).user_set.values_list("pk", flat=True))
        for user in User.objects.all().iterator():
            desired = bool(user.is_superuser or user.pk in admin_ids)
            if user.is_staff != desired:
                # Plain queryset update: bypasses signals, result identical.
                User.objects.filter(pk=user.pk).update(is_staff=desired)

    def _ensure_season(self) -> None:
        """Fresh installs start with one season (a closed cash box)."""
        if Season.objects.exists():
            return
        year = timezone.now().year
        season = Season.objects.create(name=f"{year}/{year + 1}")
        # Assignments created before the first season existed become the
        # roster of that first season (later seasons start empty on purpose).
        from app.players.services import bind_unassigned_memberships

        bind_unassigned_memberships(season)
        self.stdout.write("Seeded initial season.")

    def _ensure_catalog(self) -> None:
        if PenaltyCatalogItem.objects.filter(type=PenaltyType.MANUAL).exists():
            return
        if PenaltyCatalogItem.objects.exists():
            # Existing catalog from an older version: only add the Manual entry.
            PenaltyCatalogItem.objects.create(
                description="Manual", amount_eur="1", type=PenaltyType.MANUAL
            )
            self.stdout.write("Seeded Manual catalog item.")
            return
        for description, amount, penalty_type in DEFAULT_CATALOG:
            PenaltyCatalogItem.objects.create(
                description=description, amount_eur=amount, type=penalty_type
            )
        self.stdout.write(f"Seeded {len(DEFAULT_CATALOG)} default catalog items.")

    # ------------------------------------------------------------------
    def _seed_demo(self) -> None:
        if Team.objects.exists():
            self.stdout.write("Demo data skipped (teams already exist).")
            return

        team1 = Team.objects.create(name="Wildboars 1")
        team2 = Team.objects.create(name="Wildboars 2")
        season = Season.objects.order_by("-name").first()
        players1 = [
            Player.objects.create(name=name)
            for name in ("Anna Beispiel", "Berti Beispiel", "Chris Beispiel", "Dana Beispiel")
        ]
        players2 = [
            Player.objects.create(name=name)
            for name in ("Emil Beispiel", "Fiona Beispiel", "Gero Beispiel", "Hanna Beispiel")
        ]
        # Season-dependent assignment: the demo roster belongs to the demo season.
        for player in players1:
            player.teams.add(team1, through_defaults={"season": season})
        for player in players2:
            player.teams.add(team2, through_defaults={"season": season})
        # Showcase multi-team players: Anna also plays for Wildboars 2.
        players1[0].teams.add(team2, through_defaults={"season": season})

        matchday1 = Matchday.objects.create(
            team=team1,
            season=season,
            opponent="SV Eichenberg",
            venue="home",
            date=date(2026, 9, 24),
            description="Demo: season opener",
        )
        matchday2 = Matchday.objects.create(
            team=team1,
            season=season,
            opponent="TSV Musterstadt",
            venue="away",
            date=date(2026, 9, 29),
        )
        matchday3 = Matchday.objects.create(
            team=team2,
            season=season,
            opponent="SV Eichenberg II",
            venue="home",
            date=date(2026, 9, 25),
        )
        for matchday, players in (
            (matchday1, players1),
            (matchday2, players1),
            (matchday3, players2),
        ):
            for player in players:
                MatchdayPlayer.objects.create(matchday=matchday, player=player)

        # Sample catalog items + penalties for the demo (defaults only seed "Manual").
        late, _ = PenaltyCatalogItem.objects.get_or_create(
            description="Late arrival",
            defaults={"amount_eur": "5", "type": PenaltyType.NORMAL},
        )
        group_180, _ = PenaltyCatalogItem.objects.get_or_create(
            description="180",
            defaults={"amount_eur": "1", "type": PenaltyType.PER_ALL_OTHER_MATCHDAY_PLAYERS},
        )
        assign_penalty(
            matchday=matchday1,
            player=players1[1],
            catalog_item=late,
            description_snapshot=late.description,
            actor=None,
        )
        assign_group_penalty(
            matchday=matchday1, trigger_player=players1[0], catalog_item=group_180, actor=None
        )
        assign_penalty(
            matchday=matchday2,
            player=players1[2],
            catalog_item=PenaltyCatalogItem.objects.get(type=PenaltyType.MANUAL),
            amount_eur=3,
            description_snapshot="Demo: forgot the darts",
            actor=None,
        )
        self.stdout.write(
            f"Seeded demo data: 2 teams, {Player.objects.count()} players, "
            f"{Matchday.objects.count()} matchdays, penalties incl. one group penalty."
        )
