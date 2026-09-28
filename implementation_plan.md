# Implementation Plan

## Overview

Extend the penalty catalog of the Django app "DartsPenaltySystem" with per-team activation flags, let captains create/edit catalog entries, **remove the global "active" switch entirely** (activation exists per team only), remove the misleading global "Amount" column from the catalog overview, **merge the separate "Team fees" page into the catalog edit form** (single "Edit" button per row — no two buttons anymore), add a **guarded delete** for catalog entries (Admin + Captain, only while no penalty references the item), add per-team fee configuration (incl. an "apply fee to all teams" checkbox) to that form, allow a new team to be assigned to a season directly in the team form, and show *all* teams (with active-state highlighting) in the Teams & Players management page.

Scope: apps `penalties`, `teams`, `core` (one audit action), plus templates, migrations, locale catalogs, README permission table, and the pytest suite. Effective availability of a penalty for a team is purely **per-team**: `row.active` if a `TeamCatalogAmount` row exists, otherwise `True` (default).

High-level approach: turn the existing `TeamCatalogAmount` row into a full per-team catalog configuration row (active flag + optional fee override, amount now nullable), drop `PenaltyCatalogItem.active` (with a data backfill), drive create/edit forms with dynamic per-team fields **including the team fees (formerly the dedicated fee page)**, filter the assignment form by the matchday's team, add a POST-only delete view with a "no linked penalties" guard, and widen the relevant permission gates from admin-only to admin+captain with server-side team scoping.

## Types

- **`TeamCatalogAmount`** (existing model, extended — becomes the per-team catalog config row):
  - `active: models.BooleanField(default=True)` — *new*
  - `amount_eur: models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)` — *changed to nullable*; `NULL` = "use the catalog default amount"
  - unique `(team, catalog_item)` unchanged
  - check constraint `team_catalog_amount_eur_positive` changes to `Q(amount_eur__gt=0) | Q(amount_eur__isnull=True)`
- **`PenaltyCatalogItem.active` is REMOVED** — there is no global switch anymore. The migration first backfills globally-inactive items into per-team `active=False` rows (existing rows are flagged `active=False`, missing rows are created with `amount_eur=NULL`), then drops the column.
- **Effective state semantics** (new helper contract):
  - *no row* → team is **active**, fee = catalog default (makes "new team ⇒ all penalties active" work without a data migration)
  - *row with `active=True`* → active, fee = `row.amount_eur` if set, else default
  - *row with `active=False`* → deactivated for that team
- **New audit actions** in `app/core/models.py` (one `core` migration `AlterField` on `AuditLog.action`, same pattern as `core/migrations/0006_alter_auditlog_action.py`):
  - `CATALOG_ITEM_CREATED = "catalog_item_created", _("Catalog item created")`
  - `CATALOG_ITEM_UPDATED = "catalog_item_updated", _("Catalog item updated")` — also covers per-team fee/active changes made in the integrated edit form
  - `CATALOG_ITEM_DELETED = "catalog_item_deleted", _("Catalog item deleted")`
  - `EMAIL_SENT = "email_sent", _("E-mail sent")`
  - `EMAIL_FAILED = "email_failed", _("E-mail failed")`
- **E-mail audit metadata contract** (used by both hooks below): `success: bool`, `recipients: list[str]`, `recipient_count: int`, `bcc: list[str]` (optional), `template: str`, `subject: str`, `language: str|null`, `error: str|null` (only on failure). Email audit rows carry `user=None` (system/actor is not threaded through the async mail service).
- **Catalog audit metadata contract**:
  - created: `{"description", "amount_eur", "type", "active_teams": [team pks], "team_amounts": {team_pk: "x.xx" | null}}`
  - updated: same keys **plus** `"changes": {"<field>": {"from": …, "to": …}}` for core fields *and* per-team rows (e.g. `"team_3_active"`, `"team_3_amount"`) — one entry documents the complete edit incl. fee/flag changes.
  - deleted: `{"description"}` (already planned).
- **Catalog form dynamic field naming** (convention used by form + template + JS):
  - `team_active_<team_pk>` → per-team active checkbox
  - `team_amount_<team_pk>` → per-team fee (`DecimalField`, required=False, empty = default)
  - `apply_all` → checkbox "Apply fee to all teams"
- **Context structures**:
  - `CatalogListView`: per item `item.team_config_list` = list of dicts `{team, amount, active, has_override}` for **all** teams
  - `TeamListView`: teams annotated with `active_in_season: bool`; new context key `matrix_teams` (season-active subset)

## Files

### New files
- `app\penalties\migrations\0007_team_config_active_nullable_amount_drop_global_active.py` — (a) add `TeamCatalogAmount.active`, alter `amount_eur` to nullable, replace the check constraint, (b) **data migration**: for every `PenaltyCatalogItem` with `active=False` create/flag per-team rows `active=False` for **all** teams (preserves current behaviour), (c) `RemoveField PenaltyCatalogItem.active`.
- `app\core\migrations\0007_alter_auditlog_action.py` — auto-generated after adding the five audit choices (catalog created/updated/deleted + email sent/failed).
- `tests\test_catalog_team_active.py` — new tests for per-team activation + delete guard + catalog audit entries (see Testing).
- `tests\test_email_audit.py` — new tests for `EMAIL_SENT` / `EMAIL_FAILED` audit rows (see Testing).

### Existing files to modify
- `app\penalties\models.py`
  - `TeamCatalogAmount`: add `active`, make `amount_eur` nullable, update `Meta.constraints`, tolerate `NULL` amount in `__str__`.
  - `PenaltyCatalogItem`: **remove `active` field**; `amount_for_team(team)` returns override only when the row exists **and** `row.amount_eur is not None`.
  - New helper `PenaltyCatalogItem.is_active_for_team(team)` → `no row or row.active` (purely per-team, no global component).
- `app\penalties\forms.py`
  - `CatalogItemForm`: `Meta.fields` **without `active`**; dynamic per-team fields (`team_active_<pk>`, `team_amount_<pk>`), `apply_all`, role-scoped `__init__(…, user=…)`, `clean()`, custom `save()` persisting team rows.
  - `PenaltyAssignForm.__init__`: narrow `catalog_item` queryset to the matchday's team (per-team active via `Q(team_amounts__isnull=True) | Q(team_amounts__team=team, team_amounts__active=True)`, `distinct()`) — no global filter anymore.
- `app\penalties\views.py`
  - `CatalogListView.get`: build `team_config_list` for all teams, add `can_create` (admin OR captain); **`can_manage` becomes redundant** (Edit is shown to admin + captain like "New item") — drop it from the context or alias it to `can_create`.
  - `CatalogCreateView` / `CatalogUpdateView`: `groups = [GROUP_ADMIN, GROUP_CAPTAIN]`, pass `user=request.user` into the form; **these views now own the team fees too** (no separate fee page).
  - **Remove `CatalogTeamAmountsView`** entirely (fees + active flags live in `CatalogItemForm` now).
  - **Remove `CatalogToggleActiveView`** (obsolete with the global flag).
  - **New `CatalogDeleteView`** (POST-only, `assert_admin_or_captain`): guard `Penalty.all_objects().filter(catalog_item=item).exists()` → error message + redirect when blocked; else `log_action(AuditAction.CATALOG_ITEM_DELETED, …)` + `item.delete()` (cascades team rows) + success + redirect to `penalties:catalog_list`.
- `app\penalties\urls.py` — remove the `catalog_toggle_active` **and** `catalog_team_amounts` routes; add `penalties/catalog/<int:pk>/delete/` → `CatalogDeleteView`, name `catalog_delete`.
- `app\core\models.py` — add the five `AuditAction` choices `CATALOG_ITEM_CREATED`, `CATALOG_ITEM_UPDATED`, `CATALOG_ITEM_DELETED`, `EMAIL_SENT`, `EMAIL_FAILED` (triggers the single `core` migration).
- `app\core\services.py` — **new helper `log_email_outcome(*, success, recipients, bcc=None, template, subject, language=None, error=None)`**: builds the e-mail metadata dict and calls `log_action(EMAIL_SENT | EMAIL_FAILED, user=None, metadata=…)`; wrapped in `try/except` + logger warning so a failing audit write can never break an outgoing e-mail (mirror of the "mail problems never break business transactions" rule).
- `app\notifications\services.py` — e-mail audit hooks (see Functions): `NotificationService._deliver` (success + template/SMTP failures) and `send_templated` (unconfigured SMTP early-exit).
- `app\accounts\adapters.py` — allauth `send_mail` override: audit `EMAIL_SENT` on success / `EMAIL_FAILED` on exception with `template_prefix` + target address (the only e-mail path outside `NotificationService`).
- `app\penalties\admin.py` — `PenaltyCatalogItemAdmin`: drop `active` from `list_display`/`list_filter` (keep `type` filter); `TeamCatalogAmountAdmin.list_display` += `active`, `list_filter` += `active`.
- `app\teams\forms.py`
  - `TeamForm.__init__(…, include_season=False)`: when `include_season`, add `season = forms.ModelChoiceField(queryset=Season.objects.all().order_by("-name"), required=False, label=_("Assign to season"))`.
- `app\teams\views.py`
  - `TeamCreateView`: instantiate form with `include_season=True`; in `form_valid`, after saving, `season.teams.add(team)` when selected; add `season_id` to audit metadata.
  - `TeamUpdateView`: form without `season` field (create-only feature).
  - `TeamListView.get_queryset`: return **all** teams annotated with `active_in_season` (mirrors `teams_for_season`: season with no explicit selection ⇒ every team active; no season ⇒ every team active) plus existing `captain_count` / `player_count`.
  - `TeamListView.get_context_data`: add `matrix_teams = [t for t in teams if t.active_in_season]`, keep `editable_team_pks` intersected with matrix columns.
- `app\templates\penalties\catalog_list.html`
  - Remove the "Amount" `<th>`/`<td>` **and** the global "Active" `<th>`/`<td>` (flag is per-team now); columns: Description | Type | Team fees | actions; empty-state `colspan` = 4.
  - "Team fees" column renders **every** team (read-only status: `name: <amount or "Default" €>` + ✓/✗).
  - **Actions: a single "Edit" button** (admin + captain) linking to `penalties:catalog_update` — the separate "Team fees" button/`catalog_team_amounts` link is removed.
  - "New item" header button condition changes from `can_manage` to `can_create` (admin + captain).
- `app\templates\penalties\catalog_form.html`
  - Render model fields (description, default amount, type — **no active field**), then the **integrated team fees section**: a table "Team | Active | Fee (EUR)" built from the dynamic `team_*` fields (admin: all teams, captain: own team), then the `apply_all` checkbox; inline JS: checking `apply_all` copies the default amount into every `team_amount_*` input (server-side enforcement in `clean()` as well).
  - Blank team fee = fall back to the default amount (row is kept, `amount_eur=NULL`).
  - **Edit mode only** (`view.object.pk`): delete button as POST form to `penalties:catalog_delete` with `data-confirm` (same pattern as `season_form.html` / `penalty_form.html`); visible for admin and captain (both are allowed by the view).
- `app\templates\penalties\catalog_team_amounts.html` — **delete file** (page merged into `catalog_form.html`).
- `app\templates\teams\team_form.html`
  - No structural change required (generic field loop renders the new optional `season` field); verify label/help text rendering.
- `app\templates\teams\team_list.html`
  - Top table: iterate all `teams`; new column "Active in season" (badge ✓/✗ or row highlight for active teams); empty-state `colspan` updated.
  - Assignment matrix header/body: iterate `matrix_teams` instead of `teams` (matrix stays season-scoped, per decision).
  - Adjust the note about season scoping (top table now shows all teams).
- `README.md` — permission matrix: "catalog items" moves from Admin-only to Admin + Captain (create/edit/delete while unused; per-team activation for own team).
- `locale\de\LC_MESSAGES\django.po` (and `en` if msgids are extracted) — new user-facing strings (delete button/confirm, blocked-delete message, "Active in season" column, season field, apply-all checkbox, per-team form labels) **plus the five new audit action labels** via `makemessages` + `compilemessages`.
- `tests\test_audit.py` — the new choices flow into the audit list filter automatically (`AuditListView` renders `AuditAction.choices`); extend `test_audit_action_labels_are_translated_pairs` to cover all five new actions (labels non-empty, no raw enum leakage — `tests/test_security.py` also iterates all choices).

### Files to delete / move
- `app\templates\penalties\catalog_team_amounts.html` — deleted (page merged into `catalog_form.html`).
- Code removed (not moved): `CatalogTeamAmountsView` (`app\penalties\views.py`), `CatalogToggleActiveView` (`app\penalties\views.py`), routes `catalog_team_amounts` + `catalog_toggle_active` (`app\penalties\urls.py`).

### Configuration updates
- URL config: `app\penalties\urls.py` loses the `catalog_toggle_active` **and** `catalog_team_amounts` routes and gains `catalog_delete` (no settings changes).

## Functions

### `app/penalties/models.py`
- **Modified** `PenaltyCatalogItem` — remove the `active` field (see migration in Files).
- **Modified** `PenaltyCatalogItem.amount_for_team(self, team)` — return `row.amount_eur` only when not `None`; fall back to `self.amount_eur`.
- **New** `PenaltyCatalogItem.is_active_for_team(self, team)` — `team is None → True`, else `no row or row.active` (no global component).
- **New queryset helper** `active_for_team(team)` (module function or custom queryset method) used by `PenaltyAssignForm`:
  ```
  Q(team_amounts__isnull=True) | Q(team_amounts__team=team, team_amounts__active=True)
  ```
  applied with `.distinct()` to avoid join duplicates.

### `app/penalties/forms.py`
- **Modified** `CatalogItemForm.__init__(self, *args, user=None, **kwargs)`
  - admin (or superuser/system): editable teams = **all teams** (the old fee page's season scope is dropped — fees/flags are edited where the item lives, for every team).
  - captain: editable teams = own team only (`user.team_id`); initial values loaded from existing rows when `instance.pk` is set.
  - **no `active` model field anymore**; adds `apply_all` checkbox and `team_active_<pk>` / `team_amount_<pk>` per editable team.
- **Modified** `CatalogItemForm.clean()` — validate every `team_amount_*` is positive when given; if `apply_all` is checked and the default `amount_eur` is valid, overwrite every `team_amount_*` cleaned value with it (server-side twin of the JS).
- **Modified** `CatalogItemForm.save(...)` (override) — **this now replaces the former `CatalogTeamAmountsView.post` logic**:
  - blank `team_amount_<pk>` → `amount_eur=None` (keeps the row, falls back to the default; the row is never deleted because it carries the active flag).
  - `team_active_<pk>` missing in POST ⇒ `active=False`.
  - **Create, admin**: upsert one `TeamCatalogAmount` per team with the submitted active flag + amount (`None` when blank).
  - **Create, captain**: own row from the form; **all other teams** get explicit rows `{active: False, amount_eur: None}` ("only my team is active for a new entry").
  - **Update, admin**: upsert all rows from the form.
  - **Update, captain**: only the own-team row is written; other rows untouched; description/amount/type editable for any item (per decision), deletion stays admin+captain with the linked-penalty guard.
- **Modified** `PenaltyAssignForm.__init__(self, *args, matchday=None, **kwargs)` — after the existing player filtering, restrict `catalog_item.queryset` to items active for `matchday.team` (per-team condition only — there is no global flag anymore; no matchday ⇒ keep the plain `all()`/current behaviour so direct form construction still works); `self.manual_ids` computed **after** this narrowing.

### `app/penalties/views.py`
- **Modified** `CatalogListView.get(request)` — preload `Team.objects.all()`, attach `item.team_config_list`, add `can_create` (admin OR captain); drop the now-redundant `can_manage`.
- **Modified** `CatalogCreateView` / `CatalogUpdateView` — groups `[GROUP_ADMIN, GROUP_CAPTAIN]`; `get_form_kwargs()` adds `user=self.request.user`; the form's `save()` handles the per-team fees/flags (former fee page logic).
- **Removed** `CatalogTeamAmountsView.get/post` — functionality absorbed by `CatalogItemForm` (integrated fees section on the edit form).
- **Removed** `CatalogToggleActiveView.get` — delete class entirely (global flag gone).
- **Modified** `CatalogCreateView.form_valid(form)` — after `super().form_valid` (item + team rows persisted): `log_action(AuditAction.CATALOG_ITEM_CREATED, user=request.user, target=self.object, metadata=form.created_metadata())`.
- **Modified** `CatalogUpdateView.form_valid(form)` — capture the pre-save state, then after save: `log_action(AuditAction.CATALOG_ITEM_UPDATED, user=request.user, target=self.object, metadata=form.updated_metadata(before=…))` including per-team fee/active diffs (the audit UI action filter picks the new choices up automatically — `AuditListView` renders `AuditAction.choices`).
- **New** `CatalogDeleteView.post(request, pk)` — `assert_admin_or_captain(request.user)`; `get_object_or_404(PenaltyCatalogItem, pk)`; if `Penalty.all_objects().filter(catalog_item=item).exists()` → `messages.error` ("cannot be deleted — still referenced by penalties") + redirect to `penalties:catalog_list`; else inside `transaction.atomic()`: `log_action(AuditAction.CATALOG_ITEM_DELETED, user=request.user, target=item, metadata={"description": item.description})`, `item.delete()`; success message; redirect to `penalties:catalog_list`. GET is not served (button is a POST form).

### `app/penalties/forms.py` (audit metadata helpers)
- **New** `CatalogItemForm.created_metadata() -> dict` — snapshot per the "created" contract (`description`, `amount_eur`, `type`, `active_teams`, `team_amounts`).
- **New** `CatalogItemForm.updated_metadata(*, before: dict) -> dict` — same snapshot **plus** `changes` diff computed against `before` (core fields + per-team rows keyed `team_<pk>_active` / `team_<pk>_amount`). `before` is captured in `CatalogUpdateView.get_form`/`form_valid` from the instance + existing `TeamCatalogAmount` rows.

### `app/core/services.py`
- **New** `log_email_outcome(*, success, recipients, bcc, template, subject, language, error=None)` — best-effort `log_action(EMAIL_SENT | EMAIL_FAILED, user=None, metadata={success, recipients, recipient_count, bcc, template, subject, language, error})`; never raises (audit failure ⇒ logger.warning only).

### `app/notifications/services.py`
- **Modified** `NotificationService._deliver(...)` — single choke point both sync and async delivery pass through:
  - after `message.send(...)` succeeds → `log_email_outcome(success=True, recipients=to, bcc=bcc_list, template=template_base, subject=subject_line, language=lang, …)` (runs **before** the `finally: connections.close_all()` for background threads),
  - `TemplateDoesNotExist` → `log_email_outcome(success=False, error="template missing: <name>", …)`,
  - SMTP exception → `log_email_outcome(success=False, error=f"{type(exc).__name__}: {exc}", …)`.
- **Modified** `NotificationService.send_templated(...)` — `email_configured()` false → `log_email_outcome(success=False, error="SMTP backend not configured", recipients=to, …)` before returning 0.
- **Deliberately NOT audited** (stays logger-only): "no eligible recipient" skips (players without a user account are normal business, see `tests/test_notifications.py::test_missing_prerequisites_are_only_logged`).

### `app/accounts/adapters.py`
- **Modified** `DefaultAccountAdapter.send_mail(...)` (or the project's adapter class) — wrap `super().send_mail(...)`: success → `log_email_outcome(success=True, recipients=[email], template=f"allauth:{template_prefix}", subject=…)`, exception → `log_email_outcome(success=False, …, error=…)` then keep the existing `logger.exception` re-raise/handling behaviour unchanged.

### `app/teams/views.py`
- **Modified** `TeamCreateView.get_form_kwargs()` / `form_valid(form)` — `include_season=True`; after `super().form_valid`, if `form.cleaned_data.get("season")` → `season.teams.add(self.object)`; include `season_id` in audit metadata.
- **Modified** `TeamListView.get_queryset()` — `Team.objects.annotate(active_in_season=…, captain_count=…, player_count=…).order_by("name")`. `active_in_season`: `Value(True)` when season is `None` or `season.teams` is empty, else an `Exists` subquery over the season M2M.
- **Modified** `TeamListView.get_context_data()` — add `matrix_teams` (active subset), keep `editable_team_pks ⊆ matrix columns`.

### Removed functions
- `CatalogTeamAmountsView.get` / `.post` (`app/penalties/views.py`) — replaced by `CatalogItemForm` fields on the catalog create/edit form.
- `CatalogToggleActiveView.get` (`app/penalties/views.py`) — replaced by the per-team `team_active_<pk>` checkbox (global flag removed).
- No other functions are removed; all other call sites keep their signatures.

## Classes

- **Modified `TeamCatalogAmount`** (`app/penalties/models.py`): new field `active`, nullable `amount_eur`, adjusted constraint (see Types); cascade delete with team/item unchanged.
- **Modified `PenaltyCatalogItem`** (`app/penalties/models.py`): `active` field removed; new `is_active_for_team()` helper.
- **Modified `CatalogItemForm`** (`app/penalties/forms.py`): no `active` model field; dynamic per-team field set, role-aware, custom `save()`.
- **Modified `PenaltyAssignForm`** (`app/penalties/forms.py`): team-scoped catalog queryset (per-team only).
- **Modified `CatalogCreateView` / `CatalogUpdateView`** (`app/penalties/views.py`): gate admin+captain, form receives `user`; own the per-team fees/flags now.
- **Removed `CatalogTeamAmountsView`** (`app/penalties/views.py`): page merged into the catalog edit form — no replacement URL.
- **New `CatalogDeleteView`** (`app/penalties/views.py`): POST-only, admin+captain, linked-penalty guard, audit-logged hard delete.
- **Removed `CatalogToggleActiveView`** (`app/penalties/views.py`): obsolete with the global flag — no replacement.
- **Modified `AuditAction`** (`app/core/models.py`): five new choices — `CATALOG_ITEM_CREATED`, `CATALOG_ITEM_UPDATED`, `CATALOG_ITEM_DELETED`, `EMAIL_SENT`, `EMAIL_FAILED`.
- **New `log_email_outcome`** (`app/core/services.py`): best-effort e-mail audit helper (never raises).
- **Modified `NotificationService`** (`app/notifications/services.py`): `_deliver` + `send_templated` write `EMAIL_SENT`/`EMAIL_FAILED` audit rows; skip-case stays logger-only.
- **Modified allauth adapter** (`app/accounts/adapters.py`): audits its `send_mail` success/failure.
- **Modified `PenaltyCatalogItemAdmin`** (`app/penalties/admin.py`): `active` removed from display/filter.
- **Modified `TeamForm`** (`app/teams/forms.py`): optional `season` field when `include_season=True`.
- **Modified `TeamCreateView` / `TeamListView`** (`app/teams/views.py`): season assignment hook; all-teams queryset + matrix context.
- **Modified `TeamCatalogAmountAdmin`** (`app/penalties/admin.py`): `active` in `list_display`/`list_filter`.

## Dependencies

- No new packages. Django/pytest/ruff versions unchanged.
- Internal dependency note: `app/teams/forms.py` will import `app.matchdays.models.Season`. This is safe — `matchdays.models` references teams only via the lazy string `"teams.Team"` and does not import the `teams` package; `matchdays.forms` already imports `teams` in the other direction.
- Migrations generated with `python manage.py makemigrations core penalties` (audit choice + schema/data change), applied with `migrate`.

## Testing

Runner: `pytest` (see `.github/workflows/test.yml`), lint via `ruff check`.

**Existing tests that must be updated:**
- `tests/test_penalty_views.py::test_catalog_manage_admin_only` — captains are now allowed to GET/POST `catalog_create`; replace with: player still 403; captain allowed; assert the captain's created item has only the own team active and all other teams explicitly inactive.
- `tests/test_penalty_views.py::test_catalog_admin_crud_and_toggle` — rewrite: no `active` field in POST payloads (field removed); the `catalog_toggle_active` part disappears with the URL (reverse would raise `NoReverseMatch`).
- `tests/test_penalty_views.py::test_catalog_list_has_no_toggle_button` — obsolete (URL gone); replace with `test_catalog_list_has_delete_and_no_toggle`: `reverse("penalties:catalog_delete", …)` present in the edit page / no `catalog_toggle_active` reference possible.
- `tests/test_penalty_views.py::test_assign_form_excludes_inactive_items` — currently creates `PenaltyCatalogItem(active=False)`; rewrite to create a `TeamCatalogAmount(active=False)` for the matchday's team and assert exclusion, plus inclusion for a team without a row.
- `tests/test_penalty_views.py::test_catalog_form_rejects_non_positive` — payload drops `active`; otherwise unchanged.
- `tests/test_team_catalog_amounts.py` — **all tests retargeted to `penalties:catalog_update`** (the dedicated fee page is gone; optional: rename file to `test_catalog_form_team_fees.py`):
  - `test_admin_sees_all_teams_on_fee_page` → edit form (admin) exposes `team_active_<pk>` / `team_amount_<pk>` fields for **all** teams.
  - `test_captain_sees_and_sets_only_own_team` → captain GET/POST `catalog_update`: only own team's fields present; POST writes only the own row, foreign rows untouched.
  - `test_blank_input_clears_override_back_to_default` → blank `team_amount_<pk>` keeps the row with `amount_eur=NULL`; `amount_for_team` still falls back to the default (row may hold `active=False`).
  - `test_invalid_amount_is_rejected` → negative `team_amount_<pk>` → form error, re-render 200, no row written.
  - `test_player_cannot_open_fee_page` → player gets 403 on `catalog_update` (GET and POST).
  - anonymous-redirect test → anonymous GET `catalog_update` redirects to login.
  - `test_catalog_list_shows_team_fee_overrides` → list shows all teams' amounts + ✓/✗ status; assert **only one action button** per row (no `team-amounts` link literal in the HTML).
- `tests/test_teams.py` / `tests/test_season_assignments.py` — team list now contains teams *not* in the season; update assertions that expected the season-scoped list, add assertions for the active highlight / `active_in_season` annotation and for `matrix_teams` staying season-scoped.
- `tests/test_templates.py` — no assertion found on the removed "Amount"/"Active" global columns; add one asserting they are gone from `penalties:catalog_list`.
- `tests/conftest.py` — fixtures create items without `active` (already fine); any fixture/test constructing `PenaltyCatalogItem(..., active=False)` elsewhere must be migrated to per-team rows (grep hit: `tests/test_penalty_views.py:79`).

**New tests (`tests/test_catalog_team_active.py`):**
1. `PenaltyAssignForm` (matchday for team A) excludes items whose row is `active=False` for team A, includes them for team B's matchday, includes items without rows.
2. `penalty_create` view POST with an item deactivated for that team → form error / no `Penalty` row (server-side, not just UI).
3. Captain-created catalog entry: own team active, every other team row `active=False`.
4. Admin-created entry with `apply_all` checked: every team's `TeamCatalogAmount.amount_eur` equals the main amount.
5. New team created *after* an existing item → `is_active_for_team(new_team) is True` (default).
6. Team created via `teams:team_create` with `season=<pk>` → appears in `season.teams`; without season → not assigned; edit form offers no season field.
7. **Delete guard:** admin can delete an item with no penalties (row removed, `AuditLog` action `catalog_item_deleted` written); POST blocked (item still exists, error message) when a `Penalty` references it — also when the penalty is soft-deleted (`Penalty.all_objects`); captain can delete unused items; player gets 403; GET on the delete URL → 405/not served.
8. **Data migration:** item with `active=False` before migration ⇒ every team has an `active=False` row afterwards; active item ⇒ no rows created (default active).
9. **Catalog audit:** `catalog_create` POST → one `catalog_item_created` row with actor + metadata (description/amount/type/active_teams/team_amounts); `catalog_update` POST changing description **and** a team fee → one `catalog_item_updated` row whose `changes` contains both diffs; audit list page (admin) offers the new actions in its filter dropdown.

**New tests (`tests/test_email_audit.py`):**
1. Successful send (e.g. penalty assignment via `assign_penalty`, locmem backend) → one `email_sent` row: `success=True`, `recipients=[player-user address]`, `recipient_count`, `template="emails/penalty_created"`, non-empty `subject`, `user=None`.
2. SMTP failure → `mock.patch` `EmailMultiAlternatives.send` with `side_effect=OSError("boom")` → one `email_failed` row with `success=False` and `error` containing `OSError`; business transaction still completed (penalty exists).
3. Missing template → `email_failed` with `error` mentioning the template name; no mail in outbox.
4. Unconfigured SMTP (`EMAIL_BACKEND=smtp` + empty `EMAIL_HOST`) → `email_failed` with `error="SMTP backend not configured"`.
5. "No eligible recipient" (player without account) → **no** audit row, only the existing log line (`test_missing_prerequisites_are_only_logged` keeps passing).
6. Allauth adapter path: success/failure produces `email_sent`/`email_failed` with `template` prefixed `allauth:`.
7. Audit write is best-effort: patch `log_action` to raise → e-mail still sends, no exception propagates.
8. Existing suites (`test_notifications.py`, `test_audit.py`, `test_penalties.py`) stay green — verify no test asserts a global `AuditLog` count that the new rows would break (grep during implementation; adjust only with justification).

**Validation strategy (end of implementation):**
1. `python manage.py makemigrations --check --dry-run` (no missing migrations)
2. `ruff check .`
3. `pytest` full suite green (including previously passing catalog/team/season tests)
4. Manual smoke: create item as captain → only own team active; deactivate for team A → team A's penalty form no longer lists it; delete an unused item (works for admin + captain); try deleting an item with a penalty → blocked with message; create team with season → highlighted in Teams list, catalog rows default active. **Audit smoke:** the audit list shows `catalog_item_created`/`catalog_item_updated`/`catalog_item_deleted` entries with metadata and the new actions in the filter; a triggered notification mail shows an `email_sent` row (recipient, template, subject, `success=True`).

## Implementation Order

1. **Audit actions** — add all five choices (`CATALOG_ITEM_CREATED/UPDATED/DELETED`, `EMAIL_SENT`, `EMAIL_FAILED`) to `AuditAction` (`app/core/models.py`), run `makemigrations core`.
2. **Model + migration** — extend `TeamCatalogAmount` (`active`, nullable `amount_eur`, constraint), add `is_active_for_team` / `active_for_team` helpers, adjust `amount_for_team`, remove `PenaltyCatalogItem.active` (incl. RunPython backfill of globally-inactive items into per-team rows); run `makemigrations penalties`.
3. **`PenaltyAssignForm`** — team-scoped catalog queryset (unlocks the Strafvergabe requirement; independently testable).
4. **`CatalogItemForm` rework** — dynamic per-team fields, `apply_all`, role scoping, row persisting in `save()`, plus `created_metadata()` / `updated_metadata()` audit helpers.
5. **Catalog views** — groups admin+captain, `user` kwarg, `CatalogListView` context (`team_config_list`, `can_create`, drop `can_manage`), **remove `CatalogTeamAmountsView` + `CatalogToggleActiveView` and their routes, add `CatalogDeleteView` + route**, wire `log_action(CATALOG_ITEM_CREATED/UPDATED/…)`.
6. **Catalog templates** — `catalog_list.html` (drop Amount + global Active columns, single Edit button), `catalog_form.html` (integrated team table + apply-all JS + delete button), **delete `catalog_team_amounts.html`**.
7. **E-mail audit** — `log_email_outcome` in `app/core/services.py`, hooks in `NotificationService._deliver`/`send_templated`, hook in the allauth adapter.
8. **Teams app** — `TeamForm` season field + `TeamCreateView` wiring; `TeamListView` all-teams + `active_in_season` + `matrix_teams`; `team_list.html` highlighting/matrix split.
9. **Admin + README + locale** — admin registrations, permission matrix in README, `makemessages`/`compilemessages` for new strings incl. audit action labels.
10. **Tests** — update changed expectations, add `tests/test_catalog_team_active.py` (activation + delete guard + migration backfill + catalog audit) and `tests/test_email_audit.py`.
11. **Validation** — `makemigrations --check`, `ruff check`, full `pytest` run; fix fallout.

## Assumptions & Edge Cases (explicit)

- **No global activation anymore**: the former `PenaltyCatalogItem.active` column is dropped; a migration converts existing globally-inactive items into per-team `active=False` rows so no team loses/keeps the wrong state. "Deactivate everywhere" is no longer possible in one click — admin unchecks teams individually (per requirement).
- **Delete guard** uses `Penalty.all_objects()` (includes soft-deleted penalties): once *any* penalty row ever referenced the item, deletion is blocked so audit history (the `catalog_item` reference / `SET_NULL` FK) stays intact. The delete button lives on the catalog **edit page** (same pattern as team/season delete), is shown to admin and captain, and the view is POST-only with a `data-confirm` dialog.
- Captains may delete **any** catalog item as long as it is unreferenced (consistent with their edit rights on core fields); players get 403. If this should be narrowed to "own-team-relevant items only", the guard in `CatalogDeleteView` is the single place to change.
- **Team fees are edited exclusively in the catalog create/edit form**: the dedicated fee page (`CatalogTeamAmountsView` + `catalog_team_amounts.html` + its URL) is removed, and the list shows a **single "Edit" button** per row. The list's "Team fees" column remains as a read-only status display (amount + ✓/✗ per team). Admin edits fees/flags for **all** teams in the form (the old fee page's season-scoped team selection is dropped); captains only see their own team row — consistent with the create rule.
- Deactivating a penalty for a team only affects *future* assignments; already-assigned `Penalty` rows are untouched (they hold snapshots).
- A team created *after* a captain-created entry has no row ⇒ active by default (matches "new teams get all penalties active"); the explicit `active=False` rows only cover teams existing at creation time.
- A season with an empty team selection still means "every team active" (existing `teams_for_season` convention) — highlighting and `active_in_season` mirror that.
- The season field in the team form is create-only (per decision); editing a team offers no re-assignment (use the season form's team chips instead).
- Blank team fee in the integrated fees section (edit form) keeps the row with `amount_eur=NULL` instead of deleting it (the row now carries the active flag); the amount falls back to the default as before.
- Assignment matrix columns stay season-scoped (per decision); only the top team table lists every team.
- No per-item data migration needed for *active* items (missing rows already mean "active for everyone"); only globally-inactive items are backfilled.
- **E-mail audit decisions**: outcome rows are written at the real delivery point (`_deliver`), so async (`ASYNC_NOTIFICATIONS`) threads log the actual success/failure — not the queueing call; `user=None` because background threads have no request/actor context (the recipient addresses live in the metadata instead); "no eligible recipient" skips stay logger-only to avoid audit noise from account-less players; every audit write around e-mails is best-effort (`try/except`) so auditing can never break mail delivery or a business transaction; recipient addresses in audit metadata are visible only on the admin-only audit page.
- **Catalog audit**: `CATALOG_ITEM_CREATED/UPDATED` are written by the views (actor = requesting admin/captain); team fee/active edits travel inside the same `updated` entry's `changes` diff — no separate "team fee changed" action is introduced.

