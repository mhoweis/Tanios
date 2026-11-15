FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

RUN pip install --no-cache-dir uv

WORKDIR /app

# Dependencies first, so this layer is reused until they change.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY config ./config
RUN uv sync --frozen --no-dev --no-editable

RUN useradd --system --no-create-home tanios
USER tanios

EXPOSE 8000

# Port 8000 is reachable only from the Caddy container (see docker-compose.yml), so the
# forwarded headers it sets can be trusted. Database tables are brought up to date
# before the app starts.
CMD ["sh", "-c", "/app/.venv/bin/python -m tanios.migrate && exec /app/.venv/bin/uvicorn tanios.main:create_app --factory --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*'"]
