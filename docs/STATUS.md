# Build status

One line per milestone. Updated in the same pull request that changes it.

| Milestone | State | Notes |
| --- | --- | --- |
| 0 Foundation | Built, waiting for the owner's check | Owner deploys with docs/DEPLOY.md and confirms `/healthz` and `/readyz` answer. |
| 1 WhatsApp text | Built, waiting for the owner's check | Owner follows docs/SETUP-WHATSAPP.md and runs the six checks in part 5. |
| 2 Router and budget | Built, waiting for the owner's check | Owner runs the checks at the end of docs/MODELS-AND-BUDGET.md. |
| 3 Voice notes | Built, waiting for the owner's check | Owner runs the checks in docs/VOICE-NOTES.md. |
| 4 Obsidian memory | Not started | Needs Obsidian Sync and a new vault for Tanios. |
| 5 Gmail | Not started | Needs a Google Cloud project with the Gmail API. |
| 6 Files | Not started | |
| 7 Testers | Not started | |

Milestones 1, 2 and 3 were built at the owner's request before the owner's server
checks of the milestones before them. CI has built the Docker image and confirmed it
answers `/healthz`, but as of milestone 3 nothing has run against the real Meta or
OpenRouter services. The first live test covers four milestones at once, so expect to
find things.

## What has been verified, and how

### Milestone 0

- Unit tests, lint and format checks pass.
- The database tests pass against a real Postgres (16 in the build workspace, 17 in CI).
- CI builds the Docker image and confirms the container answers `/healthz`.
- Not yet verified: `docker compose up` with all three containers, and HTTPS through
  Caddy. These run for the first time on the owner's server.

### Milestone 1

Requirements covered: W1, W3, and the command handling of W4.

- 98 automated tests passed at the time, including the database store and its migration against a
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

- W4's `status` showed only active or paused in this milestone. Spend against the cap
  was added in milestone 2, which completes W4.
- `forget ...` is recognised and answers honestly that there is no memory yet
  (milestone 4).
- Each question is answered on its own; there is no conversation history yet.
- Two messages sent close together are handled at the same time, so their answers can
  arrive out of order.
- The table of handled message IDs is never trimmed. It needs a clean-up job before
  heavy use.

### Milestone 2

Requirements covered: R1 to R5, and the rest of W4.

- 167 automated tests pass, including the ledger and the 0001 to 0002 migration
  against a real Postgres. Tests that cover a requirement carry its ID in their name.
- Breaking the privacy rule, the cap, the retry rule or the 80% threshold on purpose
  makes tests fail.
- The real server was run against Postgres with stand-in servers for Meta and the
  model provider, on a database that still held milestone 1's data. The migration
  applied, three answers came from the small tier, the 80% warning arrived once, an
  empty answer was retried once on the medium tier, the cap message arrived once,
  the next answer came from the free router, and `status` matched the ledger.
- Not yet verified: anything against the real OpenRouter. In particular, that the
  model IDs in `config/models.toml` answer as expected, and that OpenRouter's reported
  cost is what Tanios reads. That is the owner's check.

How the requirements are met, and where they stop short:

- **R1.** A task's tier comes from `[tasks]` in the models file. The router does not
  judge how hard a question is: every chat message starts on the small tier.
- **R2.** Extra providers are added in the models file with a key in `.env`. The
  spec's check mentions the setup page, which is milestone 7.
- **R3.** Enforced in the router and tested, but no task is private yet. Chat messages
  are not treated as private content, as the spec lists email, vault and files.
- **R4.** The owner's cap comes from `TANIOS_MONTHLY_CAP_USD` until the setup page
  exists. Months are UTC calendar months. The cap is checked before each call, so the
  last call can overshoot it.
- **R5.** A provider error also uses the one retry. At the top tier there is nowhere
  to retry, and the user gets the apology.
- All users share the server's model keys for now. Per-user keys arrive with the
  setup page in milestone 7, and matter once testers join.

### Milestone 3

Requirement covered: W2, except the "find the note" half of its check, which needs
milestone 4.

- 242 automated tests pass. The audio conversion tests run real ffmpeg on real audio:
  an Ogg Opus voice note to 16 kHz mono WAV, and MP3 and raw PCM back to Ogg Opus.
- Breaking any of these on purpose makes tests fail: the rule that the access token
  only goes to Meta's hosts, the text copy for links and numbers, the cap on speech,
  the voice-message flag, and the PCM input format.
- The real server was run with stand-in servers for Meta and OpenRouter and a real
  Ogg Opus recording. It downloaded the recording, sent valid WAV for transcription,
  asked the chat model with the read-aloud wording, converted the returned MP3, and
  uploaded a valid mono Ogg Opus file marked as a voice message. `status` showed all
  three ledger lines. No secret, transcript or temporary file was left behind.
- That run found one real weakness: after a failed upload the fallback text was lost
  to a dropped connection. Message sends now retry once after a dropped connection
  (not after a timeout).
- Not yet verified: anything against the real services. In particular:
  - that `openai/whisper-large-v3-turbo` accepts the WAV Tanios sends and reports cost
    in `usage.cost`;
  - that `openai/gpt-4o-mini-tts-2025-12-15` accepts the voice `alloy` through
    OpenRouter and returns MP3;
  - that WhatsApp shows the reply with the play button and waveform;
  - the Docker image's own ffmpeg (CI checks that it has the Opus encoder).

Where it stops short:

- The cost of a voice reply is an estimate. The speech endpoint returns audio with no
  cost, so Tanios multiplies the text length by `price_per_million_chars`, and that
  price in the shipped models file is itself an estimate, not a published figure.
- "Anything you need to copy also comes as text" is a simple rule: links, email
  addresses, numbers of five digits or more, and answers of three lines or more.
- One model per speech step. If it fails there is no second model to try: a failed
  transcription gets an apology, and a failed voice reply falls back to text.
- The voice is a stock voice set in the models file. A voice of the owner's own is a
  phase 2 item.

Checked against current documentation on 15 November 2026:

- Meta sends the sender's phone number in `from` and a business-scoped user ID in
  `from_user_id`. The phone number can be missing when the sender uses a WhatsApp
  username. Tanios learns the user ID from the first message that carries both and
  recognises later messages by it.
- Since 1 October 2026 Meta gives each business number 1,000 free service messages a
  month and charges beyond that. Every reply Tanios sends counts as one.
- OpenRouter model IDs in `config/models.toml` (`openrouter/free` and the three
  `~anthropic/claude-*-latest` aliases) and their prices were read from openrouter.ai.
- Meta's audio rules: a voice message must be Ogg with the Opus codec, mono, sent with
  `"voice": true`, and 512 KB or smaller to show the play button. Incoming audio is
  fetched by media ID, then from the address Meta returns, using the access token.
- OpenRouter's audio endpoints: `/audio/transcriptions` takes base64 audio as JSON and
  returns text with usage; `/audio/speech` returns MP3 or raw PCM bytes.
