# DartsPenaltySystem — Dart Penalty Manager

A Django 5.2 (LTS) web application for dart clubs that manages **teams, players,
matchdays, individual and group penalties, player/team balances and financial
reporting** — with a complete audit trail, German/English UI, dark mode,
mobile-first responsive design and one-club/multi-team support.

## Features

- **Players** and **teams**: one club, multiple teams — a player can play in
  **several teams at once** (e.g. Wildboars 1 + Wildboars 2); admins manage
  the membership, captains only see/toggle their own team's roster.
- **Matchdays** belong to one team + free-text opponent + home/away label
  (`24.09.2026 - Wildboars 1 - SV Eichenberg`), participants as touch-friendly
  checkbox chips.
- **Penalties**
  - catalog-driven *normal* penalties — the amount ALWAYS comes from the
    catalog (optionally team-specific); no amount is asked when assigning,
  - **team-specific catalog fees**: every team can override a catalog fee
    (e.g. "3 or less" costs 3 € in the 1st team but only 1 € in the 2nd) or
    keep the default,
  - *group* penalties (`180` → one row for every **other** participant),
  - **manual penalties**: via the built-in catalog entry **"Manual"** (type
    *Manual*) — individual amount + required comment per assignment
    (e.g. "said stupid stuff → 3.00 €"); fresh installs start with this
    entry only, each club adds its own catalog items.
- **Payments**: Admin or Captain records **partial payments per player and
  team** ("Bezahlen" in the financial overview — e.g. 25 € owed, 20 € paid →
  5 € left), fully audited and revertible; balances are
  **Σ penalties − Σ payments**.
- **Seasons** (global dropdown in the navbar, admin manages them at
  `/seasons/`): every season is a **closed cash box** — matchdays and
  payments belong to exactly one season, all tables/money views follow the
  selected season, and **open amounts are never carried over** to the next
  season. Fresh installs start with `YYYY/YYYY` automatically; existing
  data is migrated into one initial season.
- **Money model**: a positive balance = **debt owed to the club pot**; totals
  are scoped by the matchday's team **and the active season**; soft-deleted
  penalties are excluded from aggregates but preserved for audit (hard delete
  of matchdays with penalty history is blocked). Every balance is shown as a
  trio: **Strafe gesamt / Strafe getilgt / Strafe noch offen**.
- **Financial overview** (tables, no chart library): per-player totals with
  payment forms and payment history (active and inactive sections), matchday
  totals, most-common penalties, full listing.
- **Audit log** (admin-only): one entry per generated row, group edits/deletes
  write one entry per affected row.
- **Accounts & e-mail notifications** (django-allauth): self-registration with
  e-mail verification, **admin approval workflow** (pending → approved/rejected,
  see below), password reset/change — and a central `NotificationService` that
  sends HTML + plain-text e-mails for registrations, approvals, new penalties
  and repayments (players **without** a user account stay fully manageable and
  simply receive no e-mails).
- **Legal pages**: Imprint (`/imprint/`) and Privacy Policy (`/privacy/`)
  driven by `CONTACT_*` env vars (same pattern as EloRankingSystem); footer
  links + version info (`v1.0.38 (a718e32) · Build …`) linking to the matching
  [GitHub release](https://github.com/reserve85/DartsPenaltySystem/releases).
- **i18n**: default German, English available, language switcher + per-user
  preference; **dark mode** (auto/light/dark) persisted per user; essential-
  cookies consent banner (no tracking).

## Quick start (Docker)

```bash
cp .env.example .env         # set DJANGO_SECRET_KEY + DJANGO_SUPERUSER_* !
docker compose up -d --build
# -> http://localhost:8000  (port via BIND_PORT in .env)
```

The entrypoint runs `migrate` + `bootstrap` on every start (idempotent).
Health check: `GET /health/` → `{"status": "ok"}`.

### Demo data

```bash
docker compose exec app python manage.py bootstrap --with-demo
```

Creates 2 teams, 8 players, 3 matchdays and sample penalties (incl. one group
penalty and one manual penalty). Skipped automatically when teams already exist.

### Portainer

Use `portainer_compose.yaml` (image `ghcr.io/reserve85/darts_penalty_system:latest`,
`.env` file + named volume `dart_data`). Images are published by CI on every
`v*` tag.

## Local development

```bash
python -m venv .venv && . .venv/Scripts/activate     # Windows
pip install -r requirements-dev.txt
python manage.py migrate
python manage.py bootstrap --with-demo
python manage.py runserver
```

Quality gates (same as CI):

```bash
ruff check .
ruff format --check .
python manage.py compilemessages     # needs gettext (Windows: Git's usr\bin on PATH)
pytest --cov=config --cov=app --cov-fail-under=80
```

> Translation catalogs live in `locale/*/LC_MESSAGES/*.po` (committed); `.mo`
> files are compiled via `compilemessages` in CI/Docker and git-ignored.

## CI / release (GitHub Actions)

Same workflow set as EloRankingSystem:

| Workflow | Trigger | What it does |
|---|---|---|
| `test.yml` (*Tests*) | push to `main`, PRs | `ruff check`, `ruff format --check`, `compilemessages`, `pytest` with coverage gate ≥ 80 % |
| `docker-publish.yml` (*Docker Publish*) | push to `main`, tags `v*` | builds + pushes `ghcr.io/reserve85/darts_penalty_system` tagged `main`, `<version>`, `<major>.<minor>`, `<sha>` and (on tags) `latest`; build args `GIT_COMMIT`/`BUILD_DATE`/`APP_VERSION` feed the footer version info |
| `release.yml` (*Release*) | release published | pushes the release image tags + uploads a `release-info-<version>` artifact (version, SHA, build date, release + image links) |
| `cleanup.yml` | daily (01:00 UTC) | deletes workflow runs/artifacts older than 3 days (keeps the 5 most recent runs / 3 artifacts) |

**Cutting a release**: create a tag `vX.Y.Z` (→ Docker image tags) and publish a
GitHub release for it (→ release workflow + release page). The footer version
`vX.Y.Z (abcdef1) · Build …` links to that release page
(`…/releases/tag/vX.Y.Z`).

## Roles & permissions

| Action | Admin | Captain | Player |
|---|---|---|---|
| Teams / users / audit / catalog items | all teams | – | – |
| Players + catalog team fees | all teams | own team | – |
| Matchdays, penalties (incl. manual) | all teams | own team | – |
| Financial overview | all teams | own team | own penalties + own team(s), read-only |
| Settings (password, language, theme) | ✔ | ✔ | ✔ |

Captains act exclusively on **their own team** (a user is linked to exactly one
team via `User.team`); admins act on all teams. The own-team rights include the
season roster (creating/editing players of that team) and the team's catalog
fee overrides.

Accounts without any role get HTTP 403 on the financial overview (the view
explicitly checks admin → captain → player instead of falling through).

`is_staff` (Django admin access) is auto-derived: Admin group → `True`,
everyone else → `False`, re-synced by forms, signals and `bootstrap`.

## User approval (registration → release)

Self-registered users are **not active immediately** — an admin releases them:

1. User registers at `/accounts/signup/` (django-allauth) → account is created
   with status **pending**, the user confirms the e-mail address.
2. The system e-mails all admins (`SUPPORT_EMAIL` + Admin group) —
   *"New registration: …"* with a direct link to the review page.
3. Admin opens **Administration → Users → Pending approvals**
   (`/accounts/users/pending/`) and reviews the user:
   - **Approve** — optionally link an existing player *or* create a new player
     directly. Approval + player assignment happen in **one single save**
     (one click, no page switching); the user automatically gets the *Player*
     role if no role is set yet.
   - **Reject** — the account is declined.
4. On approval the user automatically receives the activation e-mail and can
   log in; on rejection a rejection e-mail is sent. Both decisions are written
   to the audit log.

Login is blocked for `pending`/`rejected` accounts (clear error message on the
login form; a safety-net middleware also terminates stale sessions).
Admin-created accounts are approved immediately. The workflow is also
available in the Django admin (field + bulk actions).

**Spam protection** (no external services, no JS):

- **Honeypot** (`ACCOUNT_SIGNUP_FORM_HONEYPOT_FIELD`): an off-screen field on
  the signup form — bots that fill it receive the *same* "verification sent"
  response as real users, but **no account, e-mail or audit row is created**.
- **Rate limit** (`ACCOUNT_RATE_LIMITS`, default `10/m/ip,50/h/ip`): signup
  attempts per IP are throttled (django-allauth built-in, stored in the Django
  cache); offenders get the friendly `429.html` page. Override via the
  `SIGNUP_RATE_LIMIT` env var, e.g. `5/m/ip`.
- Behind a reverse proxy set `ALLAUTH_TRUSTED_PROXY_COUNT` (env) so the limit
  keys on the real client IP instead of the proxy address.

All e-mails go through the central `app.notifications.services.NotificationService`
(no mail code in views/models/forms). Rules: only players with a linked,
approved user account **and** a valid e-mail address are notified; everything
else is logged and skipped. Missing SMTP configuration is logged, never raised —
e-mail problems never break a business transaction. New e-mail types (club
information, round mails, reminders, dunning, newsletter) are added as a
`send_…` method + a template pair `app/templates/emails/<name>.txt`/`.html`
(`send_to_users()` is the round-mail entry point).

## Environment variables

See `.env.example` for the full list: `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`,
`DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`, `DJANGO_LANGUAGE_CODE`,
`DJANGO_TIME_ZONE`, `SQLITE_PATH`, `DJANGO_SUPERUSER_EMAIL`/`_PASSWORD`,
`BIND_PORT`, `CONTACT_*` (Imprint + Privacy Policy), `IMPRINT_NAME`/`IMPRINT_URL`.

**SMTP / e-mail** (never committed — `.env` only): `EMAIL_HOST`, `EMAIL_PORT`,
`EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS`, `DEFAULT_FROM_EMAIL`,
`SUPPORT_EMAIL`, `PUBLIC_SITE_URL` (absolute links inside e-mails),
`ACCOUNT_EMAIL_VERIFICATION` (`mandatory`/`optional`/`none`). Tests use the
in-memory e-mail backend automatically.

**HTTPS hardening** (enable behind a TLS reverse proxy / Portainer):
`DJANGO_SECURE_SSL_REDIRECT=True` turns on HTTP→HTTPS redirects plus secure
session/CSRF cookies; `DJANGO_SECURE_HSTS_SECONDS=31536000` enables HSTS once
the domain is permanently on HTTPS. With both set, `manage.py check --deploy`
reports zero issues. Always-on: HttpOnly session cookie, `X-Frame-Options:
DENY`, nosniff, `Referrer-Policy: same-origin`, strong password validators,
CSRF/clickjacking protection, explicit form field lists (no mass assignment)
and an audit entry for every user-initiated mutation.

## Stack

Python 3.11 · Django 5.2 LTS · django-allauth · gunicorn (single worker, SQLite) ·
WhiteNoise · Bootstrap 5.3 (vendored, offline) · pytest/pytest-django · ruff ·
coverage ≥ 80 · GitHub Actions (`test.yml`, `docker-publish.yml`,
`release.yml`, `cleanup.yml` → ghcr.io).
