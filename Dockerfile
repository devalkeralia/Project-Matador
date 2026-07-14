# Matador tennis value-alert bot -- always-on paper-test process. PAPER ONLY; never places orders.
# Secrets, data, and config are NEVER baked into the image: they are bind-mounted read-only/writable
# at runtime by docker-compose. See README "Run as a service".
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Install deps + the matador package (non-editable). Only source needed at runtime is copied;
# config/secrets/data/logs are mounted, not COPYed (see .dockerignore + docker-compose volumes).
COPY pyproject.toml ./
COPY matador/ ./matador/
COPY scripts/ ./scripts/
COPY reference/ ./reference/
RUN pip install --no-cache-dir .

# Run as a non-root user (uid 1000). NOTE: this chown covers the image's /app source only -- a bind
# mount takes the HOST dir's ownership at runtime, so data/ and logs/ must be writable by uid 1000 on
# the host (they are on a standard single-user Linux/WSL box; see README "Run as a service" for the
# non-uid-1000 escape hatch). Logging degrades to console-only if logs/ isn't writable (never crashes).
RUN useradd --create-home --uid 1000 matador && chown -R matador:matador /app
USER matador

ENTRYPOINT ["python", "scripts/bot.py"]
