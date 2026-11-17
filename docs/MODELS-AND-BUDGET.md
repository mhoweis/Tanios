# Models and your monthly budget

Tanios never calls a model directly. Every call goes through its router, which:

- picks the cheapest model that suits the kind of task,
- keeps your email, notes and files away from free models,
- stops paid models when you reach your monthly cap,
- tries once more on a stronger model when an answer comes back unusable,
- writes every call to a ledger, which is where `status` gets its numbers.

## Set your monthly cap

On the server:

```sh
cd /opt/tanios
nano .env
```

Set `TANIOS_MONTHLY_CAP_USD` to the most you want to spend on models in a calendar
month, in US dollars. `5` means five dollars. Leave it empty for no cap. Save, then:

```sh
docker compose up -d
```

What happens as you spend:

| Spend | What Tanios does |
| --- | --- |
| Under 80% of the cap | Nothing. Send `status` any time to see where you are. |
| 80% | One message: `Heads up: you have used $4.10 of your $5.00 monthly cap (82%).` |
| 100% | One message saying the cap is reached. Paid models stay off until the 1st of next month. |
| After that | Commands still work. Typed questions are answered by a free model, which is less capable. Voice notes are off. |

Three things to know:

- The month is the calendar month in UTC, so it turns over at midnight UTC on the 1st.
- The cap can be overshot by the cost of the last call, usually a fraction of a cent.
- This cap covers model costs only. Meta's WhatsApp charges are separate
  (see [SETUP-WHATSAPP.md](SETUP-WHATSAPP.md)).

## What `status` shows

```
Tanios is active.
November spend: $0.42 of $5.00 (8%).
- chat: $0.42 in 37 calls
Models used: ~anthropic/claude-haiku-latest (37)
Model keys: openrouter
Free models for private content: off
```

Sending `status` costs nothing: it reads the ledger and calls no model.

## Which models are used

The list is in [`config/models.toml`](../config/models.toml). It has four tiers, from
cheapest to strongest: `free`, `small`, `medium`, `top`. Each kind of task starts on one
tier. Ordinary chat starts on `small`.

As shipped, every model is reached through OpenRouter:

| Tier | Model | Used for |
| --- | --- | --- |
| free | OpenRouter's free router | Simple sorting later on, and chat after the cap is reached |
| small | Claude Haiku (latest), then Gemini 3.1 Flash Lite | Chat, and later labelling and filing |
| medium | Claude Sonnet (latest) | The retry for a failed small answer, and later drafting replies |
| top | Claude Opus (latest) | The retry for a failed medium answer, and later long, multi-step jobs |

These were checked on openrouter.ai on 15 November 2026. Prices and model names change.

The same file also names the two speech models used for voice notes. See
[VOICE-NOTES.md](VOICE-NOTES.md).

### Change a model

Do not edit `models.toml` itself, or the next update will collide with your changes.

```sh
cd /opt/tanios/config
cp models.toml models.local.toml
nano models.local.toml
```

Tanios reads `models.local.toml` whenever it exists. Replace a `model = "..."` line
with any ID from https://openrouter.ai/models, save, then:

```sh
cd /opt/tanios
docker compose restart app
```

Within a tier, Tanios uses the first model listed whose provider has a key, so put the
cheapest first.

If the file has a mistake, Tanios keeps running but WhatsApp chat goes off.
`docker compose logs app` then shows a line starting
`WhatsApp chat is off. The models file needs fixing:` that says what is wrong.

### Add another provider

Any service that speaks the OpenAI chat format works, for example DeepSeek's own API.

1. In `models.local.toml`, add the provider and use it in a tier:

   ```toml
   [providers.deepseek]
   api_base = "https://api.deepseek.com/v1"

   [[tiers.small]]
   provider = "deepseek"
   model = "the-model-id-from-their-docs"
   input_price = 0.14
   output_price = 0.28
   ```

   Put the entry above the others in that tier if it should be tried first. The two
   prices are per million tokens; Tanios uses them only when the provider does not
   report what a call cost.

2. In `.env`, add the key. The name after `TANIOS_KEYS__` is the provider's name:

   ```
   TANIOS_KEYS__DEEPSEEK=your-key-here
   ```

3. Restart with `docker compose up -d`. `status` now lists both under `Model keys`.

## Free models and your private content

Free models cost nothing, but they can come with terms that let the provider use what
you send. So Tanios never sends email, note or file content to a free model. Ordinary
chat messages are not in that group.

To allow it anyway, set `TANIOS_ALLOW_FREE_FOR_PRIVATE=true` in `.env` and restart.
`status` shows the current setting on its last line.

## The checks for milestone 2

| Check | Do this | You should see |
| --- | --- | --- |
| W4, R1 | Ask a question, then send `status` | This month's spend against your cap, and the model under `Models used`. |
| R2 | Send `status` | `Model keys: openrouter` |
| R4 | Set `TANIOS_MONTHLY_CAP_USD=0.01`, run `docker compose up -d`, then ask a few questions | The `Heads up` message, then the cap message. Later answers still arrive, from a free model. |
| R4 | Send `help` after the cap message | The command list: commands still work. |
| R4 | Put your real cap back and run `docker compose up -d` | `status` shows the new cap, and paid models answer again. |

Two requirements cannot be checked from a phone yet, and are covered by automated
tests instead:

- **R3** (private content never reaches a free model): no task handles private content
  until email and notes arrive in later milestones.
- **R5** (one retry on the next tier): it only happens when a model returns an unusable
  answer, which cannot be forced from WhatsApp.

When the checks pass, milestone 2 is done.
