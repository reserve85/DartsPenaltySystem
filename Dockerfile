# syntax=docker/dockerfile:1
FROM python:3.11-slim

# Build metadata — injected by CI / docker compose build args (see version.py)
ARG GIT_COMMIT=unknown
ARG BUILD_DATE=unknown
ARG APP_VERSION=0.1.0

ENV GIT_COMMIT=${GIT_COMMIT} \
    BUILD_DATE=${BUILD_DATE} \
    APP_VERSION=${APP_VERSION}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# gettext: compilemessages during build (offline runtime)
RUN apt-get update \
    && apt-get install -y --no-install-recommends gettext \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

# Static files + message catalogs are prepared at build time.
# sed: strip CRLF just in case the build context came from a Windows checkout.
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh \
    && python manage.py collectstatic --noinput \
    && python manage.py compilemessages

# Non-root runtime user; /app/data (SQLite volume) is owned by this user.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app/data

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/', timeout=3)"

ENTRYPOINT ["/app/entrypoint.sh"]
