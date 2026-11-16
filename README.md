# Tanios

A personal AI assistant you reach on WhatsApp. It sorts and drafts your Gmail, keeps
its memory in its own Obsidian vault, files what you send it, and uses the cheapest
model that can do each job.

- What it must do: [docs/SPEC.md](docs/SPEC.md)
- Where the build stands: [docs/STATUS.md](docs/STATUS.md)
- How to put it on a server: [docs/DEPLOY.md](docs/DEPLOY.md)
- How to connect WhatsApp and a model: [docs/SETUP-WHATSAPP.md](docs/SETUP-WHATSAPP.md)
- Models and your monthly budget: [docs/MODELS-AND-BUDGET.md](docs/MODELS-AND-BUDGET.md)
- Voice notes: [docs/VOICE-NOTES.md](docs/VOICE-NOTES.md)
- Memory and Obsidian: [docs/MEMORY.md](docs/MEMORY.md)
- Email with Gmail: [docs/SETUP-GMAIL.md](docs/SETUP-GMAIL.md)
- Saving files: [docs/FILES.md](docs/FILES.md)

## Run it on your own computer

You need [uv](https://docs.astral.sh/uv/), Python 3.12 or newer, and `ffmpeg` for
voice notes.

```sh
uv sync
uv run uvicorn tanios.main:create_app --factory --reload
```

Then open http://127.0.0.1:8000/healthz. Without a database configured, `/healthz`
answers `ok` and `/readyz` answers `not ready`; that is expected. WhatsApp chat stays
off until every setting in `.env.example` is filled in. Memory stays off until
`TANIOS_VAULTS_DIR` names a folder for the vaults. Email stays off until the three
Google settings are filled in.

## Check the code

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

## Layout

```
src/tanios/     the application
config/         which models are used (models.toml)
tests/          automated tests
docs/           spec, status, deploy guide
docker/         the vault-sync container (Obsidian Headless)
Dockerfile, docker-compose.yml, Caddyfile    the server setup
```
