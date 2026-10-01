"""i18n tests — language switch, UserLanguageMiddleware, German catalog, cookie banner."""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db

MO_DE = Path(__file__).resolve().parent.parent / "locale" / "de" / "LC_MESSAGES" / "django.mo"

needs_catalog = pytest.mark.skipif(
    not MO_DE.exists(),
    reason="compiled German catalog missing (run: python manage.py compilemessages)",
)


def _switch_language(client, code):
    response = client.post(reverse("set_language"), {"language": code, "next": "/"})
    assert response.status_code == 302
    return response


# ---------------------------------------------------------------------------
# Language switch endpoint
# ---------------------------------------------------------------------------
def test_language_switch_sets_cookie_and_redirects(db):
    response = _switch_language(Client(), "en")
    assert settings.LANGUAGE_COOKIE_NAME in response.cookies
    assert response.cookies[settings.LANGUAGE_COOKIE_NAME].value == "en"


def test_language_switch_rejects_unknown_language(db):
    response = Client().post(reverse("set_language"), {"language": "xy", "next": "/"})
    assert response.status_code == 302  # Django falls back, cookie not set to xy
    assert response.cookies.get(settings.LANGUAGE_COOKIE_NAME) is None


# ---------------------------------------------------------------------------
# English (source strings) / German (catalog)
# ---------------------------------------------------------------------------
def test_english_ui_renders_source_strings(admin_client):
    _switch_language(admin_client, "en")
    # Switching redirects to the translated URL (/en/...) — follow like a browser.
    content = admin_client.get(reverse("dashboard:index"), follow=True).content.decode()
    assert "Financial overview" in content
    assert "Finanzübersicht" not in content


def test_default_language_is_german_with_catalog(admin_client):
    """Without an explicit choice the UI follows DJANGO_LANGUAGE_CODE=de."""
    if not MO_DE.exists():
        pytest.skip("compiled German catalog missing (run: python manage.py compilemessages)")
    content = admin_client.get(reverse("dashboard:index")).content.decode()
    assert "Finanzübersicht" in content
    assert "Financial overview" not in content


@needs_catalog
def test_german_login_page(admin_client):  # client type irrelevant; uses fresh client below
    client = Client()
    _switch_language(client, "de")
    content = client.get(reverse("account_login")).content.decode()
    assert "Anmelden" in content


@needs_catalog
def test_german_signup_page(db):
    """The registration page must be fully German (template strings + button)."""
    client = Client()
    _switch_language(client, "de")
    content = client.get(reverse("account_signup")).content.decode()
    assert "Konto erstellen" in content
    assert "Create an account" not in content
    assert "Registrieren" in content  # submit button
    assert "Already have an account?" not in content


@needs_catalog
def test_german_matchday_team_selector(admin_client, team):
    """The matchday team dropdown must offer the German 'All teams' option."""
    _switch_language(admin_client, "de")
    content = admin_client.get(reverse("matchdays:matchday_list")).content.decode()
    assert "Alle Mannschaften" in content
    assert "All teams" not in content


@needs_catalog
def test_german_financial_overview(admin_client):
    _switch_language(admin_client, "de")
    content = admin_client.get(reverse("dashboard:financial_overview")).content.decode()
    assert "Finanzübersicht" in content


@needs_catalog
def test_german_new_ui_strings(admin_client, matchday):
    """Combined Teams & Players page + participants editor must be German."""
    _switch_language(admin_client, "de")
    content = admin_client.get(reverse("teams:team_list")).content.decode()
    assert "Mannschaften & Spieler" in content
    assert "Zuordnungen speichern" in content
    content = admin_client.get(
        reverse("matchdays:matchday_detail", args=[matchday.pk])
    ).content.decode()
    assert "Spieler hinzufügen" in content
    assert "Teilnehmer speichern" in content


@needs_catalog
def test_german_today_badge(admin_client, team):
    """The badge of TODAY's matchday must read 'Heute' (source: 'Today')."""
    from django.utils import timezone

    from app.matchdays.models import Matchday

    Matchday.objects.create(
        team=team, opponent="SV Punctual", venue="home", date=timezone.localdate()
    )
    _switch_language(admin_client, "de")
    content = admin_client.get(reverse("matchdays:matchday_list")).content.decode()
    assert 'badge text-bg-success">Heute</span>' in content


@needs_catalog
def test_german_cookie_banner_copy(admin_client):
    _switch_language(admin_client, "de")
    content = admin_client.get(reverse("dashboard:index")).content.decode()
    assert "Diese Website setzt nur notwendige Cookies" in content


def test_cookie_banner_present_in_english(admin_client):
    _switch_language(admin_client, "en")
    content = admin_client.get(reverse("dashboard:index"), follow=True).content.decode()
    assert 'id="cookie-consent"' in content
    assert "This site sets only essential cookies" in content


@needs_catalog
def test_german_payment_button(
    admin_client, matchday_with_players, catalog_normal, admin_user, cashier
):
    """The "Bezahlen" (record payment) action must be translated in German."""
    from app.penalties.services import assign_penalty

    md, players = matchday_with_players(2)
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    _switch_language(admin_client, "de")
    content = admin_client.get(reverse("dashboard:financial_overview")).content.decode()
    assert "Bezahlen" in content


@needs_catalog
def test_german_cash_box_menu_entry(admin_client, team):
    """Administration dropdown: the plain 'Cash box' / 'Kasse' entry.

    Language-tolerant (the local ``.mo`` is stale for new msgids) and the
    entry NEVER carries the money marker (decision 6 — the emoji is not part
    of a translatable string and not part of the menu).
    """
    _switch_language(admin_client, "de")
    content = admin_client.get(reverse("teams:team_list")).content.decode()
    assert reverse("teams:cashier_list") in content
    assert "Kasse" in content or "Cash box" in content
    assert "💲" not in content  # no marker in the navigation


def test_cashier_column_is_language_tolerant(
    admin_client, matchday_with_players, catalog_normal, admin_user, cashier
):
    """Payments table column: 'Cashier' (en) / 'Kassier' (de).

    Direction-neutral on purpose: the row's cashier snapshot is correct for
    BOTH a payment (money in) and a payout (money out) — "Received by"
    would be wrong for payout rows.
    """
    from app.penalties.services import assign_penalty, record_payment

    md, players = matchday_with_players(2)
    assign_penalty(
        matchday=md,
        player=players[0],
        catalog_item=catalog_normal,
        description_snapshot="Late arrival",
        actor=admin_user,
    )
    record_payment(player=players[0], team=md.team, amount_eur=2, actor=admin_user)

    _switch_language(admin_client, "de")
    content = admin_client.get(reverse("dashboard:financial_overview")).content.decode()
    assert "<th>Cashier</th>" in content or "<th>Kassier</th>" in content
    assert admin_user.email in content  # the cashier of THAT payment


@needs_catalog
def test_german_footer_legal_links(db):
    # Anonymous client: django-allauth redirects already logged-in users away
    # from the login page — the footer must be right for visitors.
    client = Client()
    _switch_language(client, "de")
    content = client.get(reverse("account_login")).content.decode()
    assert "Impressum" in content
    assert "Datenschutzerklärung" in content


# ---------------------------------------------------------------------------
# UserLanguageMiddleware — preference & precedence
# ---------------------------------------------------------------------------
def test_preferred_language_applies_without_cookie(admin_user):
    admin_user.preferred_language = "en"
    admin_user.save(update_fields=["preferred_language"])
    client = Client()
    client.force_login(admin_user)
    # Unprefixed default URL -> redirected to the /en/ equivalent, then English.
    content = client.get(reverse("dashboard:index"), follow=True).content.decode()
    assert "Financial overview" in content


def test_explicit_cookie_overrides_user_preference(admin_user):
    admin_user.preferred_language = "de"
    admin_user.save(update_fields=["preferred_language"])
    client = Client()
    client.force_login(admin_user)
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
    content = client.get(reverse("dashboard:index"), follow=True).content.decode()
    assert "Financial overview" in content  # cookie beats stored preference


def test_settings_language_change_writes_cookie(captain_client, captain_user):
    """The Settings page is the ONLY language lever for signed-in users.

    Saving must refresh the cookie, otherwise a previously set one would keep
    overriding the new preference (navbar switcher is gone).
    """
    response = captain_client.post(
        reverse("accounts:settings"),
        {
            "preferred_language": "en",
            "preferred_theme": "auto",
            "penalty_notify_mode": "daily",
            "penalty_notify_time": "08:00",
            "repayment_notify": "True",
            "club_news_optin": "True",
        },
    )
    assert response.status_code == 302
    assert response.cookies[settings.LANGUAGE_COOKIE_NAME].value == "en"


def test_anonymous_default_language_renders(admin_user):
    content = Client().get(reverse("account_login")).content.decode()
    assert "Darts Penalty Manager" in content


# ---------------------------------------------------------------------------
# Catalog completeness + the msgid-drift regression (Teams & Players hint)
# ---------------------------------------------------------------------------
# The German catalog is maintained BY HAND on the dev machine (no xgettext,
# see README). A source string that is reworded without updating its msgid
# silently falls back to ENGLISH under a German UI — exactly what happened to
# the season hint on the Teams & Players page (2026-09). These helpers mirror
# the extraction rules of `makemessages` closely enough to catch that drift.
PO_DE = Path(__file__).resolve().parent.parent / "locale" / "de" / "LC_MESSAGES" / "django.po"
APP_DIR = Path(__file__).resolve().parent.parent / "app"
TEMPLATES_DIR = APP_DIR / "templates"

_TRANSLATE_RE = re.compile(
    r"""\{%\s*(?:translate|trans)\s+("([^"\\]|\\.)*"|'([^'\\]|\\.)*')(?:\s+as\s+\w+)?\s*%\}"""
)
_BLOCKTRANS_RE = re.compile(
    r"\{%\s*blocktrans(?:late)?(?:\s+[^%]*?)?%\}(.*?)\{%\s*endblocktrans(?:late)?\s*%\}", re.DOTALL
)
_VAR_RE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")
_PY_FN_RE = re.compile(r"(?<![\w.])(?:_|gettext|ngettext|pgettext|ugettext)\s*\(")
_PY_LIT_RE = re.compile(r"""\s*(?P<lit>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')""")


def _norm(text: str) -> str:
    return " ".join(text.split())


def _unquote(literal: str) -> str:
    return literal[1:-1].replace('\\"', '"').replace("\\'", "'")


def _var_to_percent(match) -> str:
    # {{ name }} -> %(name)s, exactly like xgettext's Django templatize.
    return f"%({match.group(1).split('.')[-1]})s"


def _po_msgids(path: Path) -> set[str]:
    """Every msgid of a .po file (multi-line strings supported)."""
    msgids: list[str] = []
    buf: list[str] = []
    collecting = False

    def flush() -> None:
        nonlocal buf, collecting
        if collecting and buf:
            msgids.append("".join(buf))
        buf, collecting = [], False

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("msgid "):
            flush()
            collecting = True
            buf = [stripped[6:].strip()[1:-1]]
        elif stripped.startswith("msgstr"):
            flush()
        elif stripped.startswith('"') and collecting:
            buf.append(stripped[1:-1])
        elif not stripped:
            flush()
    flush()
    return {_norm(mid.replace('\\"', '"').replace("\\n", " ")) for mid in msgids}


def _template_msgids(text: str) -> set[str]:
    out: set[str] = set()
    for match in _TRANSLATE_RE.finditer(text):
        out.add(_unquote(match.group(1)))
    for match in _BLOCKTRANS_RE.finditer(text):
        body = _VAR_RE.sub(_var_to_percent, match.group(1))
        if "{{" in body:  # {{ value|filter }} cannot be mapped to %(…)s — skip
            continue
        out.add(body)
    return out


def _python_msgids(text: str) -> set[str]:
    """``_("a" "b")`` is ONE msgid (implicit concatenation); gettext/ngettext
    arguments count separately (singular + plural)."""
    out: set[str] = set()
    for match in _PY_FN_RE.finditer(text):
        fn = match.group(0).split("(")[0].strip()
        pos = match.end()
        lits: list[str] = []
        while True:
            lit = _PY_LIT_RE.match(text, pos)
            if lit:
                lits.append(_unquote(lit.group("lit")))
                pos = lit.end()
                continue
            rest = text[pos:]
            if rest.lstrip().startswith(",") and fn != "_":
                pos += rest.index(",") + 1
                continue
            break
        if not lits:
            continue
        if fn == "_":
            out.add("".join(lits))
        else:
            out.update(lits)
    return out


def test_every_source_msgid_is_covered_by_the_german_catalog():
    """A msgid missing from the German catalog renders ENGLISH under de."""
    catalog = _po_msgids(PO_DE)
    missing: set[str] = set()

    def record(msgid: str, path: Path) -> None:
        key = _norm(msgid)
        if key and key not in catalog:
            missing.add(f"{path.relative_to(APP_DIR.parent)}: {msgid[:90]!r}")

    sources = sorted(TEMPLATES_DIR.rglob("*.html")) + sorted(TEMPLATES_DIR.rglob("*.txt"))
    for path in sources:
        for msgid in _template_msgids(path.read_text(encoding="utf-8")):
            record(msgid, path)
    for path in sorted(APP_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for msgid in _python_msgids(path.read_text(encoding="utf-8")):
            record(msgid, path)

    assert not missing, (
        "German catalog misses msgids (pages would fall back to English):\n"
        + "\n".join(sorted(missing))
    )


# ---------------------------------------------------------------------------
# Structural .po validation — msgfmt cannot run on this host, so a BROKEN
# catalog only surfaces inside the Docker/CI image build (`compilemessages`
# there aborts the whole build). Fail fast locally instead: every fatal error
# msgfmt reports (missing 'msgstr' section, stray msgstr, duplicate msgid) is
# reproduced here without needing the gettext binaries.
# ---------------------------------------------------------------------------
LOCALES_DIR = Path(__file__).resolve().parent.parent / "locale"


def _po_structural_errors(path: Path) -> list[str]:
    """Every syntax error msgfmt would abort on, as ``file:line: reason``."""
    errors: list[str] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    target: str | None = None  # inside an entry: 'msgid' or 'msgstr'
    parts: list[str] = []  # concatenated pieces of the current msgid
    entry_line = 0
    seen: dict[str, int] = {}

    def finish(line_no: int) -> None:
        """Close the open entry — complete it or report what is missing."""
        nonlocal target, parts, entry_line
        if target == "msgid":
            errors.append(f"{path.name}:{entry_line}: msgid {''.join(parts)!r} has no msgstr")
        elif target == "msgstr":
            key = "".join(parts)
            if key in seen:
                errors.append(
                    f"{path.name}:{entry_line}: duplicate msgid {key!r} (first at line {seen[key]})"
                )
            else:
                seen[key] = entry_line
        target, parts, entry_line = None, [], 0

    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line:
            finish(number)
        elif line.startswith("#"):
            continue
        elif line.startswith("msgid "):  # NOT msgid_plural (unsupported here)
            finish(number)
            target, parts, entry_line = "msgid", [line[6:].strip()[1:-1]], number
        elif line.startswith("msgstr"):
            if target is None:
                errors.append(f"{path.name}:{number}: msgstr without msgid")
            else:
                target = "msgstr"
        elif line.startswith('"'):
            if target is None:
                errors.append(f"{path.name}:{number}: orphan string {line[:40]!r}")
            elif target == "msgid":
                parts.append(line[1:-1])
        else:
            errors.append(f"{path.name}:{number}: unparsable line {line[:60]!r}")
    finish(len(lines) + 1)
    return errors


def test_po_files_have_no_structural_errors():
    """A hand-edited catalog with a stray/missing msgstr breaks the Docker build."""
    catalogs = sorted(LOCALES_DIR.glob("*/LC_MESSAGES/*.po"))
    assert catalogs, "no .po catalogs found next to the tests"
    problems = [msg for catalog in catalogs for msg in _po_structural_errors(catalog)]
    assert not problems, "broken .po catalog(s):\n" + "\n".join(problems)


@needs_catalog
def test_german_season_hint_on_team_list(admin_client, season, team):
    """Regression: the season-scoped hint on Teams & Players showed ENGLISH
    because the blocktranslate msgid in the hand-maintained catalog no longer
    matched the reworded template (msgid drift)."""
    season.teams.add(team)
    _switch_language(admin_client, "de")
    content = admin_client.get(reverse("teams:team_list")).content.decode()
    assert "Die Tabelle unten listet alle Mannschaften" in content
    assert "The table below lists every team" not in content
