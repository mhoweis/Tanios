# Build status

One line per milestone. Updated in the same pull request that changes it.

| Milestone | State | Notes |
| --- | --- | --- |
| 0 Foundation | Built, waiting for the owner's check | Owner deploys with docs/DEPLOY.md and confirms `/healthz` and `/readyz` answer. |
| 1 WhatsApp text | Built, waiting for the owner's check | Owner follows docs/SETUP-WHATSAPP.md and runs the six checks in part 5. |
| 2 Router and budget | Not started | |
| 3 Voice notes | Not started | |
| 4 Obsidian memory | Not started | Needs Obsidian Sync and a new vault for Tanios. |
| 5 Gmail | Not started | Needs a Google Cloud project with the Gmail API. |
| 6 Files | Not started | |
| 7 Testers | Not started | |

Milestone 1 was started on 15 November 2026 at the owner's request, before the owner's
server check of milestone 0. By then CI had built the Docker image and confirmed it
answers `/healthz`.

## What has been verified, and how

### Milestone 0

- Unit tests, lint and format checks pass.
- The database tests pass against a real Postgres (16 in the build workspace, 17 in CI).
- CI builds the Docker image and confirms the container answers `/healthz`.
- Not yet verified: `docker compose up` with all three containers, and HTTPS through
  Caddy. These run for the first time on the owner's server.

### Milestone 1

Requirements covered: W1, W3, and the command handling of W4.

- 98 automated tests pass, including the database store and its migration against a
  real Postgres. Tests that cover a requirement carry its ID in their name.
- Breaking the signature check, or the stranger rule, on purpose makes tests fail.
- The real server was run against Postgres with stand-in servers in place of Meta and
  the model provider. Signed messages were answered, a forged one got 401 and no
  reply, a stranger got one line and then silence (also after a restart), a slow
  answer was preceded by `Working on it...`, pause survived a restart, and a message
  delivered twice was answered once. No secret or message text appeared in the logs.
- Not yet verified: anything against the real Meta and OpenRouter services. That needs
  the owner's accounts and is the owner's check.

Not done in this milestone, on purpose:

- W4 says `status` shows this month's spend against the cap. Spend does not exist
  until milestone 2, so `status` reports active or paused and says spend tracking is
  not switched on yet. W4 is complete only after milestone 2.
- `forget ...` is recognised and answers honestly that there is no memory yet
  (milestone 4).
- Each question is answered on its own; there is no conversation history yet.
- Two messages sent close together are handled at the same time, so their answers can
  arrive out of order.
- The table of handled message IDs is never trimmed. It needs a clean-up job before
  heavy use.

Checked against current documentation on 15 November 2026:

- Meta sends the sender's phone number in `from` and a business-scoped user ID in
  `from_user_id`. The phone number can be missing when the sender uses a WhatsApp
  username. Tanios learns the user ID from the first message that carries both and
  recognises later messages by it.
- Since 1 October 2026 Meta gives each business number 1,000 free service messages a
  month and charges beyond that. Every reply Tanios sends counts as one.
