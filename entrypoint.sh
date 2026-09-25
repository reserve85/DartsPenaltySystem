#!/bin/sh
# Container entrypoint: ensure data dir -> migrate -> bootstrap -> gunicorn.
set -e

# ---------------------------------------------------------------------------
# PUID/PGID (linuxserver-style convention): run everything as the HOST user
# that owns the bind-mounted data dir, e.g. on a Synology:
#   PUID=1026  PGID=100     -> the "reserve" user who owns volume2/.../data
# The image itself would run as uid 1000 ('appuser'); when started as root we
# chown the data dir to the requested ids and re-execute this script
# unprivileged, so SQLite writes (login/session) never hit a foreign owner.
# ---------------------------------------------------------------------------
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
if [ "$(id -u)" = "0" ]; then
    echo "[entrypoint] PUID=$PUID PGID=$PGID — /app/data is chowned to this user."
    chown -R "$PUID:$PGID" /app/data || true
    if ! command -v setpriv >/dev/null 2>&1; then
        echo "[FATAL] 'setpriv' missing — cannot drop privileges to $PUID:$PGID." >&2
        exit 1
    fi
    # Re-run this script as the unprivileged user (gunicorn must never be root).
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups /bin/sh "$0" "$@"
fi

# Bind mounts copied from another host keep foreign ownership -> SQLite then
# dies with "attempt to write a readonly database" at the FIRST write (login),
# while every read-only boot step still looks healthy. Test writability early.
mkdir -p /app/data
if ! touch /app/data/.write-test 2>/dev/null; then
    echo "[FATAL] /app/data is NOT writable by uid $(id -u)." >&2
    echo "[FATAL] SQLite will 500 on the first write (login/session/migrations)." >&2
    echo "[FATAL] Fix: chown -R the data dir to uid $(id -u) — or set PUID/PGID" >&2
    echo "[FATAL] in the compose environment to the owner id of the data dir." >&2
else
    rm -f /app/data/.write-test
fi

python manage.py migrate --noinput
python manage.py bootstrap
# Surface configuration warnings (e.g. darts.W001 insecure SECRET_KEY) at boot.
python manage.py check

# EXACTLY ONE worker — SQLite does not tolerate concurrent writers.
exec gunicorn config.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers 1 \
    --access-logfile - \
    --error-logfile -
