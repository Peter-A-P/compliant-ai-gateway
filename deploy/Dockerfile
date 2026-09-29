# The proxy as it runs on the VPS (docs/deploy.md). Built from a tagged checkout, so the
# image is the release the tag names and nothing else: the lock file is used as written.
FROM python:3.13-slim

# uv from its own image, pinned to the version the lock file was written with.
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY boundary ./boundary
COPY config ./config
# The server extra only: the proxy's default detector is the rules, which need nothing
# more, and the cache is not switched on in the proxy yet. Everything world-readable, since
# a checkout's file modes are whatever the machine that wrote them used.
RUN uv sync --frozen --no-dev --extra server && chmod -R a+rX /app /opt/venv

# Never root. The ledger and the audit chain live on the /data volume.
RUN useradd --system --uid 10001 --home /data boundary && mkdir -p /data && chown boundary /data
USER boundary

EXPOSE 8080
# 0.0.0.0 inside the container only: compose publishes no port for this service, so the
# proxy is reachable from Caddy on the compose network and from nothing else.
CMD ["boundary", "--config", "/app/config/boundary.yaml", "serve", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--teams", "/run/boundary/teams.yaml", \
     "--ledger", "/data/boundary.proxy.sqlite"]
