"""Self-registration name field: storage, approval display, player matching.

Covers the "Konto erstellen" feature: the required "Vorname + Name" field at
signup, its display on the approval screen, and the automatic pre-selection
of the matching unlinked player (exact -> fuzzy -> empty).
"""

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from app.accounts.models import ApprovalStatus
from app.accounts.services import _normalize_person_name, match_player_for_name
from app.players.models import Player

pytestmark = pytest.mark.django_db

User = get_user_model()

SIGNUP_NAME = "Max Mustermann"
EN_LABEL = "First and last name"
DE_LABEL = "Vorname + Name"


def _signup(client, *, email="newbie@example.com", full_name=SIGNUP_NAME, **extra):
    """Perform a self-registration; ``full_name=None`` omits the new field."""
    # Isolate from allauth's confirmation-mail throttle (shared LocMem cache).
    cache.clear()
    payload = {
        "email": email,
        "password1": "Strong-Pass-123!",
        "password2": "Strong-Pass-123!",
    }
    if full_name is not None:
        payload["full_name"] = full_name
    payload.update(extra)
    return client.post(reverse("account_signup"), payload)


def _pending(name, email):
    return User.objects.create_user(email=email, password="pw", full_name=name)


def _selected_player(form):
    """The player pk pre-selected in the approval form (or ``None``)."""
    value = form["player"].value()
    return None if value in (None, "") else str(value)


# ---------------------------------------------------------------------------
# Matcher units
# ---------------------------------------------------------------------------
def test_normalize_person_name_invariance():
    assert _normalize_person_name("  Max   Mustermann ") == "max mustermann"
    assert _normalize_person_name("Müller") == _normalize_person_name("Mueller")
    assert _normalize_person_name("Élodie") == "elodie"
    assert _normalize_person_name("Max Mustermann!") == "max mustermann"
    assert _normalize_person_name("") == ""
    assert _normalize_person_name(None) == ""
    assert _normalize_person_name("...") == ""


def test_match_exact_unique_hit(db):
    player = Player.objects.create(name=SIGNUP_NAME)
    assert match_player_for_name(SIGNUP_NAME) == player


def test_match_exact_hit_ignores_case_and_umlauts(db):
    player = Player.objects.create(name="Max Müller")
    assert match_player_for_name("max mueller") == player
    assert match_player_for_name("  MAX MÜLLER ") == player


def test_match_fuzzy_close_name(db):
    player = Player.objects.create(name=SIGNUP_NAME)
    # One character off — close enough for the similarity threshold.
    assert match_player_for_name("Max Musterman") == player


def test_match_unrelated_name_returns_none(db):
    Player.objects.create(name=SIGNUP_NAME)
    assert match_player_for_name("Xyzzy Nobody") is None


def test_match_short_input_never_fuzzy(db):
    Player.objects.create(name="Joe Example")
    assert match_player_for_name("Jo") is None  # no exact hit, fuzzy disabled
    li = Player.objects.create(name="Li")
    assert match_player_for_name("Li") == li  # exact still works


def test_match_only_considers_unlinked_players(db):
    linked = Player.objects.create(name=SIGNUP_NAME)
    holder = User.objects.create_user(
        email="holder@example.com", password="pw", approval_status="approved"
    )
    holder.player_link = linked
    holder.save(update_fields=["player_link"])
    other = Player.objects.create(name="Erika Muster")
    # The linked twin must never win; the unrelated unlinked player is too
    # far away to be a fuzzy suggestion -> empty dropdown.
    assert match_player_for_name(SIGNUP_NAME) is None
    assert match_player_for_name("Erika Muster") == other


def test_match_empty_input_returns_none(db, pending_user):
    Player.objects.create(name=SIGNUP_NAME)
    assert match_player_for_name("") is None
    assert match_player_for_name(pending_user.full_name) is None  # legacy row


# ---------------------------------------------------------------------------
# Signup flow
# ---------------------------------------------------------------------------
def test_signup_renders_name_field_label_help_and_order(db):
    content = Client().get(reverse("account_signup")).content.decode()
    assert EN_LABEL in content or DE_LABEL in content
    assert (
        "Required to assign your account" in content
        or "Wird für die Zuordnung des Spielers benötigt." in content
    )
    # Render order: the name sits between e-mail and the passwords.
    assert content.index('name="full_name"') < content.index('name="password1"')


def test_signup_without_the_name_is_rejected(db):
    response = _signup(Client(), full_name=None)
    assert response.status_code == 200  # re-rendered with a field error
    text = response.content.decode()
    assert "Please enter your first and last name." in text or ("Vor- und Nachnamen" in text)
    assert not User.objects.filter(email="newbie@example.com").exists()


def test_signup_stores_the_trimmed_name(db):
    response = _signup(Client(), full_name=f"  {SIGNUP_NAME}  ")
    assert response.status_code in (200, 302)
    user = User.objects.get(email="newbie@example.com")
    assert user.full_name == SIGNUP_NAME
    assert user.approval_status == ApprovalStatus.PENDING


# ---------------------------------------------------------------------------
# Approval screen: display + pre-selection
# ---------------------------------------------------------------------------
def test_approval_screen_shows_the_entered_name(admin_client, db):
    pending = _pending(SIGNUP_NAME, "named@example.com")
    response = admin_client.get(reverse("accounts:approval", args=[pending.pk]))
    assert response.status_code == 200
    assert SIGNUP_NAME in response.content.decode()


def test_approval_screen_without_a_name_has_no_preselect(admin_client, pending_user):
    """Legacy pending users (no name) render as before — empty dropdown."""
    response = admin_client.get(reverse("accounts:approval", args=[pending_user.pk]))
    assert response.status_code == 200
    assert _selected_player(response.context["form"]) is None


def test_exact_name_preselects_the_matching_player(admin_client, db):
    player = Player.objects.create(name=SIGNUP_NAME)
    pending = _pending(SIGNUP_NAME, "exact@example.com")
    response = admin_client.get(reverse("accounts:approval", args=[pending.pk]))
    assert _selected_player(response.context["form"]) == str(player.pk)


def test_fuzzy_name_preselects_the_closest_player(admin_client, db):
    player = Player.objects.create(name=SIGNUP_NAME)
    pending = _pending("Max Musterman", "fuzzy@example.com")
    response = admin_client.get(reverse("accounts:approval", args=[pending.pk]))
    assert _selected_player(response.context["form"]) == str(player.pk)


def test_unrelated_name_preselects_nothing(admin_client, db):
    Player.objects.create(name=SIGNUP_NAME)
    pending = _pending("Xyzzy Nobody", "none@example.com")
    response = admin_client.get(reverse("accounts:approval", args=[pending.pk]))
    assert _selected_player(response.context["form"]) is None


def test_linked_player_is_never_preselected(admin_client, db):
    linked = Player.objects.create(name=SIGNUP_NAME)
    holder = User.objects.create_user(
        email="holder@example.com", password="pw", approval_status="approved"
    )
    holder.player_link = linked
    holder.save(update_fields=["player_link"])
    Player.objects.create(name="Erika Muster")  # unlinked, but too far away
    pending = _pending(SIGNUP_NAME, "taken@example.com")
    response = admin_client.get(reverse("accounts:approval", args=[pending.pk]))
    assert _selected_player(response.context["form"]) is None


def test_approval_post_links_the_preselected_player(admin_client, db):
    """End-to-end: the suggestion is a valid option the POST accepts."""
    player = Player.objects.create(name=SIGNUP_NAME)
    pending = _pending(SIGNUP_NAME, "e2e@example.com")
    # GET first — exactly like the admin flow (pre-select runs on GET).
    admin_client.get(reverse("accounts:approval", args=[pending.pk]))
    response = admin_client.post(
        reverse("accounts:approval", args=[pending.pk]),
        {"action": "approve", "player": player.pk},
    )
    assert response.status_code == 302
    pending.refresh_from_db()
    assert pending.player_link == player
    assert pending.approval_status == ApprovalStatus.APPROVED


# ---------------------------------------------------------------------------
# Pending list
# ---------------------------------------------------------------------------
def test_pending_list_shows_the_name_column(admin_client, db):
    _pending(SIGNUP_NAME, "listed@example.com")
    content = admin_client.get(reverse("accounts:approval_list")).content.decode()
    assert SIGNUP_NAME in content
