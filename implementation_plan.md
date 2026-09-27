# Implementation Plan

Root: `c:\Daten\Tobias\dev\DartsPenaltySystem\DartsPenaltySystem` (Django 5.2, server-rendered Bootstrap 5 templates, django-allauth, pytest + ruff).

**Goal:** Simplify and restructure the UI: (1) remove language/theme switchers from the navbar for authenticated users (Settings page is enough; anonymous pages keep them), (2) reorder the Administration menu to Seasons → Users → Audit, (3) sort matchday totals ascending in the financial overview, (4) allow editing matchday participants directly on the matchday detail page via an "Add players" button with a checkbox table + confirmation, (5) remove the redundant Dashboard/Übersicht page so the start page (`dashboard:index`) is the Financial overview, and (6) merge the separate Teams and Players pages into one combined page with team management on top and a player↔team assignment matrix (players as rows, teams as columns, checkboxes) below.

No database schema changes and no new third-party dependencies. All existing services (`assign_teams`, `teams_for`, `teams_for_season`, `player_count_annotation`, `season_for_request`, `log_action`) are reused. The changes are primarily views, URLs, templates, and affected tests.

**Confirmed decisions (from Q&A):**
- Combined page: Admin gets the full matrix; Captains see ALL players but can only toggle their own team's column; the team create/edit/delete section is read-only for captains.
- Participants editing lives on the **matchday detail page** (button "Spieler hinzufügen" → collapsible checkbox table → confirm save).
- Language/theme switchers are removed from the navbar **only for authenticated users**; anonymous pages (login/signup) keep them.
- Old standalone pages (Teams list, Team detail, Players list) are removed; their URLs **redirect** to the new locations so no link breaks.

---

## [Types]

No model/type/migration changes. New data contracts:

1. **Combined page context** (rendered by `teams:team_list`):
   - `teams` — `QuerySet[Team]` (season-scoped, annotated with `captain_count`, `player_count`), as today.
   - `matrix_teams` — `list[Team]`: columns = `teams_for_season(season).order_by("name")`.
   - `matrix_players` — paginated `Player` queryset (25/page) with `prefetch_season_assignments(season)` exposing `player.season_assignments`.
   - `editable_team_pks` — `set[int]`: admin → all column pks; captain → `{request.user.team_id}` (intersected with the columns); player role never reaches the page (403).
   - `season`, `season_teams_scoped` — unchanged semantics from today's team list.

2. **Matrix POST payload** (one form, one save button = confirmation):
   - `players` — repeated hidden input, one `player.pk` per rendered row (defines the diff scope; players on other pages are untouched).
   - `assign` — repeated checkbox value `f"{player.pk}:{team.pk}"` for every checked cell.
   - `csrfmiddlewaretoken`; redirect back to `teams:team_list`.

3. **Participants POST payload** (`matchdays:matchday_participants`, POST-only):
   - `participants` — list of `player.pk` (checkboxes pre-filled with the current participants).

4. **New context on `matchdays:matchday_detail`:**
   - `eligible_players` — active roster of the matchday's team for the matchday's season: `Player.objects.in_team(matchday.team_id, roster_season).filter(active=True).order_by("name")`, where `roster_season = matchday.season or season_for_request(request)` (same rule as `MatchdayForm`, see `app/matchdays/forms.py:127`).

---

## [Files]

### Modified files

| File | Change |
|---|---|
| `c:\Daten\...\app\templates\base.html` | Navbar: wrap `{% include "includes/_language_switcher.html" %}` and `{% include "includes/_theme_switcher.html" %}` (lines 132–133) in `{% if not request.user.is_authenticated %}` so they render only for anonymous visitors. Remove the "Dashboard" nav item (lines 50–53); the "Financial" item becomes the first item. Team-area dropdown: replace the three entries (Teams / My Team / Players) with a single "Teams & Players" item → `{% url 'teams:team_list' %}` for admins and captains; keep "Matchdays". Administration dropdown: reorder to Seasons → Users → Audit. |
| `c:\Daten\...\app\dashboard\urls.py` | `path("", views.FinancialOverviewView.as_view(), name="index")` (was `DashboardView`); keep `path("financial/", ..., name="financial_overview")` on the same view. |
| `c:\Daten\...\app\dashboard\views.py` | Delete `DashboardView`; remove now-unused imports (`Count`, `player_count_annotation`, `teams_for`, `Matchday`, `Player`, and whichever role helpers `FinancialOverviewView` no longer needs). |
| `c:\Daten\...\app\core\context_processors.py` | `_nav_section`: `dashboard` namespace always returns `_FINANCIAL`; remove the `_OVERVIEW` constant and its docstring mention. |
| `c:\Daten\...\app\penalties\services.py` | `matchday_totals()` (line 433): order by `("date", "created_at")` ascending instead of `("-date", "-created_at")`; update docstring ("first matchday on top"). |
| `c:\Daten\...\app\teams\views.py` | `TeamListView`: groups `[GROUP_ADMIN, GROUP_CAPTAIN]`, add matrix context in `get_context_data`, add `post()` for matrix saves (see [Functions]). Remove `TeamDetailView` (replaced by redirect). `TeamDeleteView`: guard-failure redirect → `teams:team_list`. |
| `c:\Daten\...\app\teams\urls.py` | Keep `name="team_list"` (path `/teams/`) as the combined page; replace the `team_detail` path with a `RedirectView` to `dashboard:financial_overview?team=<pk>`; keep create/update/delete paths unchanged. |
| `c:\Daten\...\app\players\views.py` | Remove `PlayerListView`; replace with a redirect view to `teams:team_list`. Change `success_url` of `PlayerCreateView`/`PlayerUpdateView` and the redirect of `PlayerDeactivateView` → `teams:team_list`. |
| `c:\Daten\...\app\players\urls.py` | `path("", views.PlayerListRedirectView.as_view(), name="player_list")` — name kept for reverse()/old links. |
| `c:\Daten\...\app\matchdays\views.py` | Add `MatchdayParticipantsView` (POST-only). Extend `MatchdayDetailView.get_context_data` with `eligible_players` (see [Types] §4). |
| `c:\Daten\...\app\matchdays\urls.py` | Add `path("<int:pk>/participants/", views.MatchdayParticipantsView.as_view(), name="matchday_participants")`. |
| `c:\Daten\...\app\accounts\views.py` | `SettingsView.post`: after a valid save, attach the language cookie to the redirect (`settings.LANGUAGE_COOKIE_NAME` = new `preferred_language`, `max_age=settings.LANGUAGE_COOKIE_AGE`, `samesite="Lax"`). Reason: `UserLanguageMiddleware` lets an existing cookie beat the stored preference (`app/core/middleware.py`), and with the navbar switcher gone the Settings page must change the language immediately. |
| `c:\Daten\...\app\templates\teams\team_list.html` | Rewrite as the combined page: H1 "Teams & Players"; top card = existing team table (Name → `dashboard:financial_overview?team=`, Players, Captains, Edit/Delete buttons admin-only, "New team" button admin-only, keep `season_teams_scoped` hint); bottom card = player management: "New player" button, table with columns **Player \| team columns… \| Active \| Actions**, team cells = checkboxes `<input type="checkbox" name="assign" value="{{ player.pk }}:{{ team.pk }}"` (disabled unless `team.pk in editable_team_pks`), one form with CSRF + hidden `players` inputs + submit "Save assignments" (`data-confirm`). Pagination via `includes/_pagination.html`; empty states for no teams / no players. |
| `c:\Daten\...\app\templates\matchdays\matchday_detail.html` | Replace the static participant chip block (lines 17–24): heading "Participants (n)" + chips as today, plus button "Add players" (`data-bs-toggle="collapse" data-bs-target="#participantEditor"`); collapse contains a form → `matchdays:matchday_participants`, a small table (checkbox + name + active badge per `eligible_players`, checked = current participant), submit "Save participants" with `data-confirm` confirmation. |
| `c:\Daten\...\app\templates\players\player_form.html` | Cancel link (line 24): `players:player_list` → `teams:team_list`. |
| `c:\Daten\...\app\templates\players\player_detail.html` | Team links (line 32): `teams:team_detail` → `dashboard:financial_overview?team={{ row.team.pk }}`. |
| `c:\Daten\...\app\templates\teams\team_confirm_delete.html` | Cancel link (line 18): `teams:team_detail` → `teams:team_list`. |
| `c:\Daten\...\locale\de\LC_MESSAGES\django.po` | Add new msgids ("Teams & Players", "Add players", "Save participants", "Save assignments", …); run `python manage.py compilemessages`. |

### Deleted files

- `c:\Daten\...\app\templates\dashboard\dashboard.html` (Dashboard/Übersicht removed).
- `c:\Daten\...\app\templates\teams\team_detail.html` (URL redirects to the financial overview).
- `c:\Daten\...\app\templates\players\player_list.html` (merged into the combined page).

### Unchanged (verified sufficient)

- `app/templates/accounts/settings.html` + `SettingsForm` — already contain `preferred_language`/`preferred_theme` selects; the single place for language/theme now.
- `app/templates/includes/_language_switcher.html` / `_theme_switcher.html` — still rendered for anonymous users.
- `app/static/js/theme.js` — binds `[data-theme-choice-btn]` wherever they appear; no-op when absent.
- `config/settings.py` — `LOGIN_REDIRECT_URL = "dashboard:index"` now lands on the financial overview (desired).

---

## [Functions]

1. **`matchday_totals(team, *, season=None)`** — `c:\Daten\...\app\penalties\services.py:433`
   - Change `matchdays = list(matchday_qs.order_by("-date", "-created_at"))` → `.order_by("date", "created_at")`; update docstring to "ordered by date ascending (first matchday on top)".

2. **`TeamListView.post(self, request)`** — `c:\Daten\...\app\teams\views.py`
   - `assert_admin_or_captain(request.user)`; `season = season_for_request(request)`.
   - `columns = list(teams_for_season(season))`; `column_pks = {t.pk for t in columns}`.
   - `editable = column_pks if user_is_admin(request.user) else ({request.user.team_id} & column_pks)`.
   - For each player referenced by the repeated `players` hidden field (pks validated to exist):
     - `current = {t.pk for t in teams_for(player, season)}`
     - `desired` = checked `assign` pairs for that player
     - `final_pks = (current - editable) | (desired & editable)` — captain: foreign columns ignored; admin: all columns editable, while teams outside an explicitly scoped season selection survive via `current - editable`.
     - Build the final team list from existing assignments kept (`current ∩ final_pks`) plus new ones (`columns queryset.filter(pk__in=final_pks - current)`); call `assign_teams(player, final, season=season)` only when changed.
   - Audit once per submit: `log_action(AuditAction.PLAYER_UPDATED, user=request.user, metadata={"season_id": season.pk if season else None, "changed": changed_count})`.
   - `messages.success(...)` + `redirect("teams:team_list")`.

3. **`MatchdayParticipantsView.post(self, request, pk)`** — `c:\Daten\...\app\matchdays\views.py` (new)
   - `assert_admin_or_captain(request.user)`; `get_object_or_404(Matchday, pk=pk)`; `if not user_can_manage_team(request.user, matchday.team): raise PermissionDenied`.
   - `eligible` = same queryset as [Types] §4; `selected = eligible.filter(pk__in=request.POST.getlist("participants"))`.
   - Safety: any submitted id not in `eligible` → treat as invalid (reject, redirect back with an error message) — blocks cross-team/cross-season injection.
   - Empty selection → `messages.error(request, _("Select at least one participant."))` + redirect back (mirrors the `MatchdayForm.participants` required rule).
   - `transaction.atomic()`: delete all `MatchdayPlayer` rows of the matchday, bulk-create the new set.
   - `log_action(AuditAction.MATCHDAY_UPDATED, user=request.user, target=matchday, metadata={"participants": selected.count()})`; success message; `redirect("matchdays:matchday_detail", pk=pk)`.

4. **`MatchdayDetailView.get_context_data`** — `c:\Daten\...\app\matchdays\views.py:173`
   - Add `context["eligible_players"]` ([Types] §4).

5. **`SettingsView.post`** — `c:\Daten\...\app\accounts\views.py:230`
   - On valid save: `response = redirect("accounts:settings")`; `response.set_cookie(settings.LANGUAGE_COOKIE_NAME, form.cleaned_data["preferred_language"], max_age=settings.LANGUAGE_COOKIE_AGE, samesite="Lax")`; return it.

6. **`_nav_section(request)`** — `c:\Daten\...\app\core\context_processors.py:19`
   - `if namespace == "dashboard": return _FINANCIAL`; drop the `_OVERVIEW` constant.

7. **Removed functions/views** (migration strategy):
   - `DashboardView.get` — removed; the financial overview becomes the start page. Dashboard role tests are deleted or re-pointed at `dashboard:index` asserting financial context.
   - `PlayerListView` — removed; replaced by `PlayerListRedirectView` (name `players:player_list` still reverses, responds 302 → `teams:team_list`).
   - `TeamDetailView` — removed; replaced by `TeamDetailRedirectView` (302 → `dashboard:financial_overview?team=<pk>`), whose target shows the same balances that lived on that page.



---

## [Classes]

1. **`TeamListView` (modified)** — `c:\Daten\...\app\teams\views.py:33`
   - `groups = [GROUP_ADMIN, GROUP_CAPTAIN]` (was `[GROUP_ADMIN]`); player role stays 403.
   - New `get_context_data` → matrix context ([Types] §1) and new `post()` ([Functions] §2); `get_queryset` unchanged (season-scoped counts).

2. **`TeamDetailRedirectView` (new)** — `c:\Daten\...\app\teams\views.py`
   - `RedirectView` subclass: `permanent = False`, `query_string = False`; `get_redirect_url(pk)` → `reverse("dashboard:financial_overview") + f"?team={pk}"`. Keeps URL name `teams:team_detail` alive for old links/tests.

3. **`PlayerListRedirectView` (new)** — `c:\Daten\...\app\players\views.py`
   - `RedirectView`: `permanent = False`, fixed target `reverse("teams:team_list")`; keeps name `players:player_list`.

4. **`MatchdayParticipantsView` (new)** — `c:\Daten\...\app\matchdays\views.py`
   - `LoginRequiredMixin, View` with only `post` (GET → 405); behaviour in [Functions] §3.

5. **Removed classes:** `DashboardView` (`app/dashboard/views.py`), `PlayerListView` (`app/players/views.py`), `TeamDetailView` (`app/teams/views.py`) — replaced by the redirect views above / the financial overview.

---

## [Dependencies]

- **None added.** Only facilities already in use: Django `RedirectView`, plain checkbox inputs + Bootstrap `collapse`, the existing `data-confirm` handler in `app/static/js/base.js`.
- CI gate (`.github/workflows/test.yml`): `ruff check .`, `ruff format --check .`, `python manage.py compilemessages`, `pytest --cov=config --cov=app --cov-fail-under=80` — everything must stay green with ≥ 80 % coverage.


---

## [Testing]

Run: `python manage.py compilemessages` (after .po updates), then `pytest`, `ruff check .`, `ruff format --check .`.

### Tests to update

- `tests/test_dashboard.py` — the "Dashboard role matrix" block (lines 22–89) asserts `response.context["role"]`/`teams` on `dashboard:index`; replace with tests that `dashboard:index` renders the financial overview (200, `selected_team is None` for admin, `own_mode` for player role) or move surviving assertions to `dashboard:financial_overview`. The login-redirect test stays valid.
- `tests/test_i18n.py` — `test_english_ui_renders_source_strings` / `test_default_language_is_german_with_catalog` assert dashboard-only copy ("Logged in as"/"Angemeldet als"); change to strings present on the financial overview ("Financial overview" / "Finanzübersicht"). Add: saving Settings with a new `preferred_language` sets the language cookie on the response.
- `tests/test_templates.py` — nav tests: expect `teams:team_list` in the team area instead of `players:player_list`; `test_detail_pages_200` / `test_captain_detail_pages_200`: `teams:team_detail` now expects **302** to the financial overview; `ADMIN_URLS`/`CAPTAIN_URLS`: `players:player_list` now 302 (remove from the 200 lists); `test_dashboard_links_team_name_to_financial_overview` → move its assertion to the combined page; add: authenticated pages contain **no** `set_language` form and no `data-theme-choice-btn`; add Administration menu order assertion (Seasons before Users before Audit).
- `tests/test_theme.py` — `test_theme_switcher_buttons_rendered` (line 57) checks `dashboard:index`; retarget to the anonymous login page (buttons still exist there) and add "not present when authenticated".
- `tests/test_teams.py` — replace `team_detail` 200/403 tests with redirect tests; `test_team_detail_links_players_with_balance` → assert the same content on `dashboard:financial_overview?team=`; `test_team_list_shows_counts` keeps working, extended for captain access (200, no edit/delete buttons).
- `tests/test_season_assignments.py` — `test_team_detail_roster_is_season_scoped`, `test_player_list_shows_active_season_teams`, `test_captain_player_list_is_season_scoped` → re-point to the combined page (`teams:team_list` matrix cells) / financial overview.
- `tests/test_players.py:229` — content assertion on `teams:team_detail` → expect a `dashboard:financial_overview?team=` link instead.
- `tests/test_financial.py` — add an explicit ordering assertion: `matchday_totals(team)` returns matchdays by `date` ascending; existing dict-based assertions remain valid.

### New tests

- **Combined page:** admin sees all teams as columns and all players as rows; admin POST toggles assignments for the active season only (other seasons untouched); captain sees all players but non-own columns are disabled, and a forged POST touching another team is ignored server-side; team create/edit buttons hidden for captain; player role → 403.
- **Redirects:** `players:player_list` → 302 → `teams:team_list`; `teams:team_detail` → 302 → financial overview with `?team=`.
- **Participants:** admin adds/removes participants from the detail page; captain only for own team's matchday (403 for a foreign team); player role → 403; empty selection rejected; out-of-eligible ids rejected; roster is season-scoped (mirror of `test_matchday_form_offers_only_the_matchdays_season_roster`); audit entry `MATCHDAY_UPDATED` written; detail page renders `eligible_players` with current participants pre-checked.
- **Start page:** anonymous `dashboard:index` → login redirect; authenticated → financial overview content; navbar has no "Dashboard" item and no switchers for authenticated users.


---

## [Implementation Order]

1. **Financial matchday ordering** — one-line change in `app/penalties/services.py::matchday_totals` + test in `tests/test_financial.py`. Independent, no risk.
2. **Navbar language/theme removal** — wrap the two includes in `app/templates/base.html`; extend `SettingsView.post` with the language cookie; update/extend `tests/test_theme.py` + `tests/test_i18n.py`.
3. **Administration menu order** — reorder the dropdown in `app/templates/base.html`; add ordering assertion in `tests/test_templates.py`.
4. **Remove Dashboard/Übersicht** — delete `DashboardView` + `dashboard/dashboard.html`; point `dashboard:index` at `FinancialOverviewView`; update `_nav_section`; remove the "Dashboard" nav item, make "Financial" first; clean imports; fix `tests/test_dashboard.py`, `tests/test_i18n.py`, `tests/test_templates.py`, `tests/test_theme.py`, nav assertions in `tests/test_seasons.py` (context is still provided by the context processor — verify only).
5. **Combined Teams & Players page** — extend `TeamListView` (permissions, context, `post()`), rewrite `teams/team_list.html`, remove `TeamDetailView`/`PlayerListView` in favour of the redirect views, delete the two obsolete templates, update success/cancel links (`player_form`, `player_detail`, `team_confirm_delete`, view `success_url`s), update `teams/urls.py` + `players/urls.py`; fix `tests/test_teams.py`, `tests/test_players.py`, `tests/test_season_assignments.py`, `tests/test_templates.py`; add matrix/permission/redirect tests.
6. **Matchday participants editor** — `MatchdayParticipantsView` + URL + `MatchdayDetailView` context + `matchday_detail.html` collapse UI; add `tests/test_matchdays.py` cases.
7. **i18n + quality gate** — add new msgids to `locale/de/LC_MESSAGES/django.po`, `python manage.py compilemessages`, `ruff check .`, `ruff format --check .`, full `pytest --cov=config --cov=app --cov-fail-under=80`.

Steps 1–3 are small and independent; apply all `base.html` edits (steps 2–4) together if preferred; step 6 is fully independent of step 5.

---

### Assumptions & edge cases

- The participants editor requires **at least one** participant (same rule as `MatchdayForm`); removing the last one is rejected with a message.
- The matrix saves **per rendered page** (25 players/page) — the hidden `players` field defines the diff scope, so players on other pages are untouched.
- Captains' forged POSTs for foreign teams are ignored server-side (disabled checkboxes are never trusted).
- The "My Team" nav entry is dropped; captain finances remain one click away via Finanzen (auto-selects their team) and their roster is on the combined page.
- `teams:team_detail` / `players:player_list` URL names are retained as redirects so bookmarks, tests, and `reverse()` calls keep working.
- Player-role users still get 403 on the combined page (unchanged behaviour of both old pages).

