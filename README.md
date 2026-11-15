# Tanios

A personal AI assistant you reach on WhatsApp. It sorts and drafts your Gmail, keeps
its memory in its own Obsidian vault, files what you send it, and uses the cheapest
model that can do each job.

- What it must do: [docs/SPEC.md](docs/SPEC.md)
- Where the build stands: [docs/STATUS.md](docs/STATUS.md)
- How to put it on a server: [docs/DEPLOY.md](docs/DEPLOY.md)

## Run it on your own computer

You need [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer.

```sh
uv sync
uv run uvicorn tanios.main:create_app --factory --reload
```

Then open http://127.0.0.1:8000/healthz. Without a database configured, `/healthz`
answers `ok` and `/readyz` answers `not ready`; that is expected.

## Check the code

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

## Layout

```
src/tanios/     the application
tests/          automated tests
docs/           spec, status, deploy guide
Dockerfile, docker-compose.yml, Caddyfile    the server setup
```
