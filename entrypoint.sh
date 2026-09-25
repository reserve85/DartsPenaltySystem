#!/bin/sh
# Container entrypoint: ensure data dir -> migrate -> bootstrap -> gunicorn.
set -e

# Named volume: ensure it exists; when running as root (e.g. `docker run
# --user root`) make it writable for the non-root app user as well.
mkdir -p /app/data
if [ "$(id -u)" = "0" ]; then
    chown -R appuser:appuser /app/data || true
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
