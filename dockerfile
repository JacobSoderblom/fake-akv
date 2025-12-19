FROM ghcr.io/astral-sh/uv:python3.13-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1 \
  UV_LINK_MODE=copy \
  UV_SYSTEM_PYTHON=1 \
  UV_PYTHON=/usr/local/bin/python

RUN set -eux; \
  if [ -f /etc/apt/sources.list ]; then \
  sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list; \
  elif [ -f /etc/apt/sources.list.d/debian.sources ]; then \
  sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources; \
  fi; \
  printf 'Acquire::Retries "5";\nAcquire::http::Timeout "30";\nAcquire::https::Timeout "30";\n' \
  > /etc/apt/apt.conf.d/80retries; \
  apt-get update -y; \
  apt-get install -y --no-install-recommends curl ca-certificates; \
  rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock* README.md ./
COPY src ./src

# Try frozen first; fall back if lock doesn't match
RUN uv sync --no-dev --frozen --python=/usr/local/bin/python || \
  uv sync --no-dev --python=/usr/local/bin/python

RUN rm -rf /root/.cache

# ---- runtime image ----
FROM ghcr.io/astral-sh/uv:python3.13-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1 \
  PORT=8443 \
  FAKE_AKV_STORAGE=sqlite \
  FAKE_AKV_SQLITE_PATH=/data/akv.sqlite

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

VOLUME ["/data"]
EXPOSE 8443

RUN set -eux; \
  if [ -f /etc/apt/sources.list ]; then \
  sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list; \
  elif [ -f /etc/apt/sources.list.d/debian.sources ]; then \
  sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources; \
  fi; \
  printf 'Acquire::Retries "5";\nAcquire::http::Timeout "30";\nAcquire::https::Timeout "30";\n' \
  > /etc/apt/apt.conf.d/80retries; \
  apt-get update -y; \
  apt-get install -y --no-install-recommends ca-certificates curl; \
  rm -rf /var/lib/apt/lists/*

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD \
  curl -fsS -k "https://127.0.0.1:${PORT}/?api-version=7.6" >/dev/null || exit 1

# SSL paths and auth are provided at runtime
CMD ["/bin/sh", "-lc", "\
  if [ -z \"$FAKE_AKV_SSL_CERTFILE\" ] || [ -z \"$FAKE_AKV_SSL_KEYFILE\" ]; then \
  echo 'ERROR: FAKE_AKV_SSL_CERTFILE and FAKE_AKV_SSL_KEYFILE must be set for HTTPS.' >&2; exit 64; \
  fi; \
  exec ./.venv/bin/python -m uvicorn \
  --app-dir src fake_akv.main:app \
  --host 0.0.0.0 --port ${PORT} \
  --ssl-certfile \"$FAKE_AKV_SSL_CERTFILE\" \
  --ssl-keyfile \"$FAKE_AKV_SSL_KEYFILE\" \
  "]
