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
  (`24.09.2026 - Wildboars 1 - SV Eichenberg`); the participants are assigned
  **only** on the matchday page via *Add players* (green row = selected).
- **Penalties**
  - catalog-driven *normal* penalties — the amount ALWAYS comes from the
    catalog (optionally team-specific); no amount is asked when assigning,
  - **per-team catalog fees**: every team carries its own amount for each
    catalog entry (e.g. "3 or less" costs 3 € in the 1st team but only 1 € in
    the 2nd) — seeded with the default amount when the entry or the team is
    created; every fee row is always filled,
  - *group* penalties (`180` → one row for every **other** participant),
  - **manual penalties**: via the built-in catalog entry **"Manual"** (type
    *Manual*) — individual amount + required comment per assignment
    (e.g. "said stupid stuff → 3.00 €"); fresh installs start with this
    entry only, each club adds its own catalog items.
- **Payments**: the team's **cashier** (Kasse) records **partial payments per
  player and team** ("Bezahlen" in the financial overview — e.g. 25 € owed,
  20 € paid → 5 € left), fully audited and revertible; balances are
  **Σ penalties − Σ payments**. Recording *and* reverting is the cashier's
  sole right — admins included (see *Kasse* below). Paying **more than owed**
  is allowed and creates a **credit** (negative balance); the cashier can pay
  that credit out again ("Auszahlung", capped at the available credit, same
  rules + a separate confirmation e-mail).
- **Seasons** (global dropdown in the navbar, admin manages them at
  `/seasons/`): every season is a **closed cash box** — matchdays and
  payments belong to exactly one season, all tables/money views follow the
  selected season, and **open amounts are never carried over** to the next
  season. Fresh installs start with `YYYY/YYYY` automatically; existing
  data is migrated into one initial season.
- **Money model**: a positive balance = **debt owed to the club pot**, a
  negative balance = **credit** (shown green and labelled *Guthaben* /
  *Credit*, netted against future penalties of the same season); totals
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
  simply receive no e-mails). **New penalties are collected**: every player
  chooses *Off / Quick (collected) / daily digest at 08:00* on the Settings
  page, so five penalties in a row arrive as **one** e-mail instead of five.
  Every new registration also raises an **in-app
  notification** for all admins ([django-notifications-hq](https://github.com/django-notifications/django-notifications):
  🔔 badge in the navbar → notification list with a *Review* shortcut;
  approve/reject marks it read).
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

| Action | Admin | Captain | Player / User |
|---|---|---|---|
| Teams / users / audit | all teams | – | – |
| Catalog items (create/edit/delete while unused) | all teams | all items + all teams' fees/activation | – |
| Players | all teams | own team (write); read: everyone | read: everyone |
| Matchdays, penalties (incl. manual) | all teams | own team | – |
| Payments (record/revert, *Kasse*) | only when set as the team's **cashier** | only when set as the team's **cashier** (own team) | – |
| Financial overview | all teams | read: all teams; write: own team | read: all teams |
| Settings (password, language, theme) | ✔ | ✔ | ✔ |

**Read access is universal** for every signed-in account: all players, all
penalty entries, all seasons, all teams (financial overview + player pages).
Only WRITE actions are role-scoped: admins act on all teams, captains on
their own team (players/users never write). Captains act on **their own
team** (a user is linked to exactly one team via `User.team`); the own-team
rights include the season roster (creating/editing players of that team) and
matchday/penalty management. Catalog entries may be created, edited and
deleted (as long as no penalty references them) by admins **and** captains;
activation and fees are **per team** — every fee row is always filled, and
captains configure all teams (a captain's NEW entry defaults the other
teams to inactive).

The team link (`User.team`) is an **optional captaincy assignment**,
decoupled from the role: a Captain can be created *without* a team (such an
account then manages nothing), an Admin may additionally be assigned as
captain of one team, and the Player role never gets a team. Roles are
visualised with two emojis — the **👑 crown** for the captaincy of the
selected team and the **💲 emoji** (an emoji, **not** the ASCII character
`$`) for the team's cashier. **Every marker always shows ALL roles of that
account**: wherever one of the two appears, the other is added as well when
it applies — in the captains list under the team heading, in the dedicated
`Cashier:` block below it (label on its own line, the name as the list entry
underneath), in the roster row of a linked player, in the payment modal's
"Received by" line and on the Kasse page → `Kapt. X 👑 💲`. The team selector
stays unmarked.

### Kasse (cash box)

Every team has **exactly one cashier** — the only account that may record or
revert payments for that team, with **admins included**: an admin who wants
to record payments first sets themselves as cashier. Admins and the captain
of the respective team assign the cashier on **Administration → Cash box**
(`/teams/cashier/`, captains only for their own team, enforced server-side);
the page keeps the full **history** ("valid from – valid to" + who assigned
it), so the Kasse audit trail survives account deletions (name snapshot).
Each recorded payment stores its receiver (`Payment.received_by`, snapshot)
and the payment confirmation e-mail goes to **the player + the cashier** —
each of them can switch off only their own copy (Settings → *Payment
confirmation*). Money movements never stay silent in-app: recording,
reverting, adding, changing and deleting a penalty/payment each leaves an
in-app notification row for the affected accounts (always *in addition to*
e-mail).

Accounts **without any role** can sign in and read everything (financial
overview, players, entries) but have no write actions — they never see
create/edit affordances.

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

### Invitations (admin sends the account, no approval step)

Instead of waiting for a self-registration, an admin can invite a person
directly (**Administration → Users → Invitations**, `/accounts/users/invitations/`):

1. **New invitation** — e-mail address plus pre-assignment: role, optional team
   (captaincy, Captain/Admin only) and an optional existing player. The account
   is created immediately with status **Invited** (`requested`) and an
   *unusable* password — the address is reserved (a second invitation or a
   self-signup with the same address is blocked).
2. The invitee receives an e-mail with an acceptance link
   (valid `INVITE_EXPIRE_DAYS` days, default **14**; anti-phishing note +
   expiry date + sender included). On the invitations list an admin can
   **Copy link** (hand it over via WhatsApp/phone), **Resend** (new token,
   fresh timer — the old link dies) or **Cancel** (deletes the account again).
3. Clicking the link lets the invitee **set a password** — the account becomes
   *approved* with the pre-assigned player/role/team applied and the e-mail
   marked verified. **No approval step follows**; all admins are notified by
   e-mail (`Invited user completed registration: …`) and in-app bell.
4. While the invitation is open, the pre-assigned player stays selectable in
   every approval/create-user form. The moment the player is linked to any
   other user, the invitation becomes obsolete and is **deleted automatically**
   (audit log: `user_invite_obsoleted`).
5. Requested users cannot be edited or deactivated in the user list — a wrong
   entry is fixed with **Cancel** (the person may self-register later and then
   goes through the normal approval flow above).

Expiry/accepted states show as badges on the list (open / expires soon /
expired / accepted); the invitation row is kept after acceptance for history.

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

## Notifications & e-mails

| # | Template | Trigger | Recipient |
|---|---|---|---|
| 1 | `emails/account_registered` | new self-registration | `SUPPORT_EMAIL` (To) + all admins (Bcc) + 🔔 in-app |
| 2 | `emails/account_approved` | admin approves an account | the user |
| 3 | `emails/account_rejected` | admin declines an account | the user |
| 4 | `emails/invitation` | admin sends / resends an invitation | the invitee |
| 5 | `emails/invitation_completed` | invitation accepted | `SUPPORT_EMAIL` + admins (Bcc) + 🔔 in-app |
| 6 | `emails/penalty_created` / `emails/penalty_digest` | new penalty(s) — **collected** (outbox) | the player |
| 7 | `emails/penalty_repayment` | payment confirmation ("Bestätigung für die Zahlung") | the player + the cashier (one mail per recipient) |
| 8 | `emails/penalty_payout` | payout confirmation ("Bestätigung für die Auszahlung") | the player + the cashier (one mail per recipient) |
| 9 | `emails/support_request` | support form | `SUPPORT_EMAIL` |
| – | allauth (`allauth:account/…`) | e-mail verification, password reset, password changed | the user |

Per-user switches live on **Settings → Notifications**: penalties
(*Off* / *Quick* / *daily digest* + time), payment/payout confirmations and
club news.
Transactional mails (invitation, approval/rejection, password, verification)
are **always** delivered — they are not part of any opt-out.

### Outbox + daily digest (why 5 penalties = 1 e-mail)

New penalties are **never** mailed row by row. Each penalty is written to
`NotificationOutbox` (`app/notifications/models.py`) with a `send_after` stamp:

- **Quick** → `now + NOTIFICATION_COALESCE_SECONDS` (default 90 s), so a batch
  entered in one go arrives as ONE message;
- **Daily digest** → the next occurrence of the player's `penalty_notify_time`
  (default 08:00) — one e-mail per day listing every new position with the
  season-scoped open total;
- **Off** → nothing is queued at all.

`flush_outbox()` (`app/notifications/services.py`) claims due rows atomically
(`WHERE sent_at IS NULL` → the scheduler thread and an external cron can never
send twice) and groups everything **per user** into one message. Soft-deleted
penalties drop out of the batch, a delivery failure is retried up to
3 times and then abandoned with `last_error`. Two triggers:

- the **in-process scheduler** (daemon thread, every
  `NOTIFICATIONS_SCHEDULER_INTERVAL` seconds, default 30) — no cron needed;
- `python manage.py flush_notification_outbox` for setups that prefer an
  external cron/timer (`* * * * * docker exec <container> manage.py
  flush_notification_outbox`) — set `NOTIFICATIONS_SCHEDULER=False` then.
  Optional: `NOTIFICATION_QUIET_HOURS=22:00-07:00` delays *Quick*-mode
  messages that become due at night (digest times are self-chosen anyway).

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

**Invitations**: `INVITE_EXPIRE_DAYS` (default `14`) — days until an
admin-sent invitation link expires; Resend regenerates the token and resets
the timer.

**Outbox / digest** (collected penalty e-mails, see *Notifications & e-mails*):
`NOTIFICATION_COALESCE_SECONDS` (default `90`), `NOTIFICATIONS_SCHEDULER`
(default `True`), `NOTIFICATION_SCHEDULER_INTERVAL` (default `30`),
`NOTIFICATION_QUIET_HOURS` (e.g. `22:00-07:00`, empty = disabled).

**HTTPS hardening** (enable behind a TLS reverse proxy / Portainer):
`DJANGO_SECURE_SSL_REDIRECT=True` turns on HTTP→HTTPS redirects plus secure
session/CSRF cookies; `DJANGO_SECURE_HSTS_SECONDS=31536000` enables HSTS once
the domain is permanently on HTTPS; `DJANGO_SECURE_PROXY_SSL_HEADER=True`
makes Django trust the proxy's `X-Forwarded-Proto` (TLS terminated at the
proxy). With all three set, `manage.py check --deploy` reports zero issues.
Always-on: HttpOnly session cookie, `X-Frame-Options:
DENY`, nosniff, `Referrer-Policy: same-origin`, strong password validators,
CSRF/clickjacking protection, explicit form field lists (no mass assignment)
and an audit entry for every user-initiated mutation.

## Stack

Python 3.11 · Django 5.2 LTS · django-allauth · gunicorn (single worker, SQLite) ·
WhiteNoise · Bootstrap 5.3 (vendored, offline) · pytest/pytest-django · ruff ·
coverage ≥ 80 · GitHub Actions (`test.yml`, `docker-publish.yml`,
`release.yml`, `cleanup.yml` → ghcr.io).
