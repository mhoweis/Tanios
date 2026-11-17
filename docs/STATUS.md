# Build status

One line per milestone. Updated in the same pull request that changes it.

| Milestone | State | Notes |
| --- | --- | --- |
| 0 Foundation | Built, waiting for the owner's check | Owner deploys with docs/DEPLOY.md and confirms `/healthz` and `/readyz` answer. |
| 1 WhatsApp text | Not started | Needs a Meta developer account, a spare phone number and an OpenRouter key. |
| 2 Router and budget | Not started | |
| 3 Voice notes | Not started | |
| 4 Obsidian memory | Not started | Needs Obsidian Sync and a new vault for Tanios. |
| 5 Gmail | Not started | Needs a Google Cloud project with the Gmail API. |
| 6 Files | Not started | |
| 7 Testers | Not started | |

## What has been verified, and how

Milestone 0:

- Unit tests, lint and format checks pass.
- The database tests passed against a real Postgres 16 in the build workspace.
- The server was started against that Postgres: `/healthz` and `/readyz` answered
  correctly with the database up, and `/readyz` reported `down` once it was stopped.
- Not yet verified: the Docker image build, `docker compose up`, HTTPS through Caddy,
  and the GitHub Actions workflow. The build workspace has no Docker daemon, so these
  run for the first time in CI and on the owner's server.
