# Testers, the setup page, and alerts

This milestone lets two or three people besides you use Tanios, each with their own
keys, mail, notes and budget. It also lets Tanios reach you after a quiet day.

Do [DEPLOY.md](DEPLOY.md) and [SETUP-WHATSAPP.md](SETUP-WHATSAPP.md) first.

## Part 1: switch on the setup page

The setup page needs two settings. `TANIOS_DOMAIN` is already set from the deploy guide.
If you followed [SETUP-GMAIL.md](SETUP-GMAIL.md), `TANIOS_ENCRYPTION_KEY` is set too.
If not, make one now:

```sh
openssl rand -base64 32 | tr '+/' '-_'
```

and put it in `.env` as `TANIOS_ENCRYPTION_KEY=...`. Then update:

```sh
cd /opt/tanios
git pull
docker compose up -d --build
docker compose logs app | grep "setup page"
```

You should see `the setup page is on`.

## Part 2: invite a tester

1. **Let Meta's test number talk to them.** In your Meta app, open **WhatsApp**, then
   **API Setup**, and add the tester's number in the **To** box, as you did for your own.
   They confirm the code Meta sends them. The test number allows only a few numbers.
2. **Invite them.** On WhatsApp, send Tanios:

   ```
   invite +962 79 000 0002
   ```

   (their full number with country code). Tanios replies that they are invited.
3. **Ask them to write.** WhatsApp does not let Tanios message someone first, so ask the
   tester to send any message to the Tanios number, then `setup`.
4. **They set up on their own page.** The link Tanios sends works once, for 15 minutes,
   and the page stays open for 30. On it they:
   - add their own model key (most people need one OpenRouter key: see part 1 of
     [SETUP-WHATSAPP.md](SETUP-WHATSAPP.md));
   - connect Gmail, if you set up email. Their Gmail address must also be on the
     **Test users** list in your Google project (part 2 of
     [SETUP-GMAIL.md](SETUP-GMAIL.md));
   - set their monthly cap.

A new tester starts with a $5 cap. To change that starting value, set
`TANIOS_TESTER_CAP_USD` in `.env`.

**Who pays.** Testers use only the keys they add themselves, so their model costs go to
their own accounts. They never use your server keys. Meta's messaging costs are on
your WhatsApp business account, for everyone.

Other owner commands: `testers` lists them, and `remove +962 79 000 0002` removes a
tester and deletes everything the server holds about them.

## What each user can do

| Send | What happens |
| --- | --- |
| `setup` | A one-time link to your private setup page. |
| `delete my data` | Tanios says what will be deleted. Send `yes delete everything` within 10 minutes to go ahead. |

`delete my data` removes your notes and saved files on the server, your keys, your
Gmail sign-in (and cancels Tanios's access at Google), your drafts and mail summaries,
and your usage record. Gmail itself is not touched. The number is then no longer
registered. The confirmation must be typed: a voice note saying it is refused.

As the owner you cannot `delete my data` yourself, since you run the server. To wipe
everything, see "Removing everything" in [DEPLOY.md](DEPLOY.md).

**What stays separate.** Each user's keys, spend and cap, Gmail, drafts, notes and files
are their own. A tester cannot see your mail or notes, and you never see their keys.
You, as the person running the server, can still reach anything on it from the server's
command line. Tell testers that.

**Obsidian for testers.** Testers' notes and files are kept on the server, but only
your vault is synced to Obsidian in this version.

## Part 3: alerts after a quiet day (optional)

WhatsApp lets Tanios write to someone first only within 24 hours of their last message.
After that, Meta allows only messages built from an approved template.

With a template set up, Tanios sends **one** alert when new mail needs a reply, or when
Gmail access was lost, while you have been quiet for over a day. It sends no other
until you write again. The alert carries only a count, never a sender or subject.

Without one, nothing changes: the mail is still sorted, and you see it when you next
ask `what's new?`.

### Create the template

1. Open https://business.facebook.com and go to **WhatsApp Manager**, then
   **Message templates**, then **Create template**.
2. Category: **Utility**. Name: `tanios_alert`. Language: **English**.
3. Body:

   ```
   Tanios has an update for you: {{1}}. Reply to this message to see it.
   ```

   For the sample value of `{{1}}`, enter `2 new emails need a reply`.
4. Submit, and wait for Meta to approve it. The status changes to **Active**.

Meta changes this dashboard often. Its own guide:
https://developers.facebook.com/documentation/business-messaging/whatsapp/templates/overview

### Tell Tanios its name

In `.env`:

```
TANIOS_WHATSAPP_ALERT_TEMPLATE=tanios_alert
TANIOS_WHATSAPP_TEMPLATE_LANGUAGE=en
```

If you picked **English (US)** as the language, use `en_US` instead of `en`. Then
`docker compose up -d`.

**Cost.** Meta charges for a template sent outside the 24-hour window. Tanios sends at
most one per quiet stretch, per user.

## Checks

Tell me which pass and which fail.

| # | Requirement | Do this | Expect |
| --- | --- | --- | --- |
| 1 | U1 | Invite a second number. From that phone, send `hello`, then `setup`. | `hello` gets "You have no model key yet". `setup` gets a link. |
| 2 | U1 | On the tester's phone, open the link, add an OpenRouter key and a $3 cap. Then ask a question. | The page says the key is saved and never shows it. The question is answered. |
| 3 | U1 | Open the same link again. | "This page has expired". |
| 4 | U2 | As the tester, send `status`. | Their own spend against their $3 cap, and `openrouter (added by you)`. |
| 5 | U2 | As the tester, send `check mail` and `send`. | They see nothing of yours. Nothing of yours is sent. |
| 6 | U2 | As the tester, `remember my locker is 42`. As yourself, ask `what is my locker number?` | Your answer does not know it. |
| 7 | U3 | As the tester, send `delete my data`, then `yes delete everything`. Then `hello`. | Tanios confirms the deletion. `hello` gets the private-assistant line or nothing. On the server, `docker compose exec app ls /vaults` no longer lists their folder. |
| 8 | W5 | With the template approved and set, stay silent for 25 hours, then have someone email you a question. Wait for the next hourly check. | One template alert arrives, saying how many emails need a reply. |

Checks 1 to 7 need only part 1 and 2. Check 8 needs part 3, and email set up.

## If something is wrong

| What you see | What to do |
| --- | --- |
| `The setup page is not switched on` | Part 1: `TANIOS_DOMAIN` or `TANIOS_ENCRYPTION_KEY` is missing. |
| The tester gets nothing at all | Their number is not in the **To** box in Meta (part 2, step 1), or they have not been invited. |
| The tester gets "This is a private assistant" | Invite them first, with the number exactly as WhatsApp shows it, with country code. |
| "This page has expired" | Links work once, for 15 minutes. Send `setup` again. |
| "That does not look like a key" | Copy the key again with nothing before or after it. |
| The tester's Gmail says `Access blocked` | Add their Gmail address to **Test users** in your Google project. |
| No alert after a quiet day | The template is not approved yet, or its name or language in `.env` does not match. `docker compose logs app \| grep template` shows Meta's answer. |
