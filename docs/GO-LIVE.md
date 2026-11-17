# Going live: merge, deploy, and test everything

All eight milestones are built, but none has run against the real WhatsApp, OpenRouter,
Google or Obsidian yet. This guide takes you from the open pull requests to a working
Tanios on your phone, one milestone at a time.

**The rule:** test in the order below, and stop at the first check that fails. Send me
the check number and what you saw. A fault early on (WhatsApp, say) makes every later
check fail for the same reason.

Allow a few hours, spread over two days: one check needs 25 quiet hours.

## Step 1: merge the pull requests (10 minutes)

The pull requests build on each other, so they go in order: #1, then #2, and so on to #8.

1. Open https://github.com/mhoweis/Tanios/pulls
2. Open **#1** (Milestone 0). At the bottom, choose **Merge pull request**, then
   **Confirm merge**. Use the plain merge, not "Squash".
3. Open **#2**. If the top says it wants to merge into `milestone-0-foundation`, choose
   **Edit** next to the title, change the base to `main`, and choose **Change base**.
   Then **Merge pull request**, **Confirm merge**.
4. Do the same for **#3, #4, #5, #6, #7 and #8**, in that order.
5. Open the **Actions** tab. The newest run on `main` should finish with a green tick.

If GitHub says a pull request has conflicts, stop and tell me the number.

## Step 2: the server (30 minutes)

Follow [DEPLOY.md](DEPLOY.md) all the way through. At the end,
`https://YOUR_DOMAIN/healthz` must answer.

If your server already runs an older version:

```sh
cd /opt/tanios
git pull
docker compose up -d --build
```

## Step 3: what to collect, and the `.env` file

Each guide below says where a value comes from. The `.env` file on the server ends up
with these, filled in one guide at a time:

| Setting | From | Guide |
| --- | --- | --- |
| `TANIOS_DOMAIN`, `POSTGRES_PASSWORD` | you | DEPLOY |
| `TANIOS_OWNER_NUMBER` | your WhatsApp number, digits only | SETUP-WHATSAPP |
| `TANIOS_WHATSAPP_...` (four values) | Meta | SETUP-WHATSAPP |
| `TANIOS_OPENROUTER_API_KEY` | OpenRouter | SETUP-WHATSAPP |
| `TANIOS_MONTHLY_CAP_USD` | you, for example `10` | MODELS-AND-BUDGET |
| `TANIOS_TIMEZONE` | you, for example `Asia/Amman` | MEMORY |
| `TANIOS_ENCRYPTION_KEY` | `openssl rand -base64 32 \| tr '+/' '-_'` | SETUP-GMAIL |
| `TANIOS_GOOGLE_CLIENT_ID`, `..._SECRET` | Google Cloud | SETUP-GMAIL |
| `TANIOS_WHATSAPP_ALERT_TEMPLATE` (optional) | Meta, after approval | TESTERS |

After every change to `.env`:

```sh
cd /opt/tanios
docker compose up -d
```

Never paste these values into a chat with Claude, an email or WhatsApp.

## Step 4: test, milestone by milestone

Do each guide in turn and run its checks before moving on.

| # | Guide | What it proves | Checks |
| --- | --- | --- | --- |
| 1 | [SETUP-WHATSAPP.md](SETUP-WHATSAPP.md) | Texting Tanios gets an answer; strangers are turned away. | Part 5, six checks |
| 2 | [MODELS-AND-BUDGET.md](MODELS-AND-BUDGET.md) | Models are chosen by task; the cap warns and stops. | The checks at the end |
| 3 | [VOICE-NOTES.md](VOICE-NOTES.md) | A voice note gets a voice reply. | The checks at the end |
| 4 | [MEMORY.md](MEMORY.md) | `remember`, `note:`, `forget`, Instructions. | Eight checks; four need no Obsidian |
| 5 | [SETUP-GMAIL.md](SETUP-GMAIL.md) | Mail is labelled; drafts wait for `send`. | Part 6, ten checks |
| 6 | [FILES.md](FILES.md) | Files from WhatsApp and Gmail land in the vault. | Seven checks |
| 7 | [TESTERS.md](TESTERS.md) | A tester sets up alone; their data stays theirs and can be deleted. | Eight checks |

Milestone 5 is the first that can send email to other people. For its checks, use a
second email account of your own as the "other person", not a real contact.

## Step 5: tell me the results

Send one message like this:

```
1: all pass
2: all pass
3: check 2 fails - the reply came as text, not a voice note
```

I fix what failed, you update with `git pull` and `docker compose up -d --build`, and
re-run just those checks.

## If you get stuck

| What you see | What to do |
| --- | --- |
| A step in a Meta or Google dashboard does not match the guide | Both change their dashboards often. Each guide links the provider's own walkthrough. Tell me what you see instead. |
| Something fails and you are not sure why | `docker compose logs --tail 100 app` and send me the lines. They never contain your messages, keys or tokens. |
| You want to undo everything | "Removing everything" in [DEPLOY.md](DEPLOY.md). |
