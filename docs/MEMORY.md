# Memory and Obsidian

Tanios keeps what you tell it as plain notes in a vault of its own. This guide covers
what you can say, how to switch memory on, and how to see the notes in Obsidian on your
phone.

Memory works in two steps, and the second is optional:

1. **Memory on the server.** Works as soon as you update. Notes live on your server.
2. **Obsidian Sync.** Makes the same notes appear in Obsidian on your phone and
   computer, and lets you edit them there. Needs a paid Obsidian Sync subscription.

## What you can say

| You send | What Tanios does |
| --- | --- |
| `remember my accountant is Rana` | Saves one note in `Memory/`. |
| `note: call the bank about the transfer` | Saves a dated note in `Inbox/`. |
| `who is my accountant?` | Answers from your notes and names the note it used. |
| `forget my accountant` | Deletes that memory and tells you which note it removed. |

The same words work in a voice note: "Tanios, please make a note to call the bank".
Tanios answers a voice note with a voice note.

Good to know:

- **The first word decides.** A message that starts with `remember` or `note` is saved,
  not answered. Tanios does not guess from the middle of a sentence. A question such as
  "remember when we went to Petra?" is still treated as a question.
- **Saving and forgetting use no model.** They cost nothing and work even when your
  monthly cap is reached.
- **`forget` only removes notes in `Memory/`.** If more than one memory matches, Tanios
  removes nothing and lists them. Send `forget` with the full name of the one you mean.
- **While paused,** Tanios saves nothing. `forget` still works.
- **Each question stands on its own.** Tanios does not yet remember the previous message
  in a chat. It remembers what is in the vault.

## What is in the vault

```
Instructions.md     how you want Tanios to work (you edit this)
Memory/             one note per thing you asked it to remember
Inbox/              quick notes, named with the date
Files/              files you ask it to save, and an Index note listing them (see FILES.md)
Log/                one note per day listing what Tanios did
```

Tanios reads and writes inside this vault and nowhere else. It never sees your personal
vault.

### The Instructions note

Open `Instructions.md` and write under the line, in plain English. For example:

```
Always answer in bullet points.
Keep answers under three sentences unless I ask for more.
```

Tanios reads this note before every answer, so a change applies from your next message.
It shapes how Tanios answers. It cannot make Tanios send, delete or share anything.

### Privacy

- Notes are stored as plain files on your server.
- When a question is answered from your notes, the matching notes are sent to the model
  that writes the answer. That is always a paid model. Free models never see your notes
  or your Instructions unless you set `TANIOS_ALLOW_FREE_FOR_PRIVATE=true`.
- When your monthly cap is reached, Tanios can still answer with a free model, but
  without your notes, and it tells you so.

## Part 1: switch memory on

On the server:

```sh
cd /opt/tanios
nano .env
```

Add this line, with your own time zone. It decides the date on your notes and log.
Names look like `Asia/Amman`, `Europe/London` or `America/New_York`.

```
TANIOS_TIMEZONE=Asia/Amman
```

Save (Ctrl+O, Enter, Ctrl+X), then update and restart:

```sh
git pull
docker compose up -d --build
docker compose logs app | grep memory
```

You should see: `memory is on: the owner's vault is the folder user-1`.

Now try it on WhatsApp:

1. Send `remember my accountant is Rana`. Reply: `Saved to memory: "My accountant is Rana".`
2. Send `who is my accountant?`. The reply names Rana and ends with
   `From your notes: My accountant is Rana`.

To look at the notes on the server:

```sh
docker compose exec app ls -R /vaults/user-1
```

## Part 2: see the notes in Obsidian

You need:

- An Obsidian account with an **Obsidian Sync** subscription. See https://obsidian.md/sync
  for the current price.
- Obsidian installed on your phone.

You will type your Obsidian password into the server's terminal. Never paste it into a
chat, an email or a WhatsApp message.

### Step 1: build the sync helper

```sh
cd /opt/tanios
docker compose --profile sync build vault-sync
```

This installs Obsidian Headless, Obsidian's own tool for syncing without a screen. It
is in open beta, so expect rough edges.

### Step 2: sign in to Obsidian

```sh
docker compose --profile sync run --rm vault-sync ob login
```

Type your Obsidian email and password when asked, and your 2FA code if you use one.

### Step 3: create a new vault for Tanios

```sh
docker compose --profile sync run --rm vault-sync ob sync-create-remote --name Tanios --encryption end-to-end
```

It asks you to invent an **encryption password**. Write it down somewhere safe: you
need it again in the next step and once on each phone or computer.

Create a new vault here. Do not connect Tanios to the vault that holds your personal
notes.

### Step 4: connect the server's folder to that vault

```sh
docker compose --profile sync run --rm vault-sync ob sync-setup --vault Tanios --path /vaults/user-1 --device-name tanios-server
```

Enter the encryption password from step 3.

### Step 5: sync once and look at the result

```sh
docker compose --profile sync run --rm vault-sync ob sync --path /vaults/user-1
```

It should list the notes it uploaded and finish without an error.

### Step 6: keep it syncing

```sh
nano .env
```

Add this line, save, and start everything:

```
COMPOSE_PROFILES=sync
```

```sh
docker compose up -d
docker compose logs --tail 20 vault-sync
```

From now on the sync helper starts with the rest of Tanios.

### Step 7: open the vault on your phone

1. Open Obsidian. On a fresh install, choose **Sign in** next to **Open vault from
   Obsidian Sync**. If you already use Obsidian, open the vault list and add a vault
   from Obsidian Sync there.
2. Sign in, pick the vault named **Tanios**, and choose **Connect**.
3. Enter the encryption password from step 3.

Obsidian's own guide, with pictures: https://obsidian.md/help/sync/setup

After a minute you should see `Instructions`, `Memory`, `Inbox`, `Files` and `Log`.

## Checks

Tell me which pass and which fail.

| # | Requirement | Do this | Expect |
| --- | --- | --- | --- |
| 1 | M1 | Open the Tanios vault on your phone. | The four folders and `Instructions` are there. Your personal vault is unchanged. |
| 2 | M2 | Send `remember my accountant is Rana`. Next day ask `who is my accountant?`. | The answer says Rana. |
| 3 | M3 | Send `note: call the bank about the transfer`. | The note appears in `Inbox` in Obsidian, with today's date. |
| 4 | M4 | Ask about something you saved. | The reply ends with `From your notes:` and the note's name. |
| 5 | M5 | In Obsidian, add `Always answer in bullet points.` to `Instructions`. Wait for the sync, then ask anything. | The reply uses bullet points. |
| 6 | M7 | Send `forget my accountant`. | Tanios names the note it removed, the note is gone from Obsidian, and today's `Log` note has a `Forgot` line. |
| 7 | M8 | After a day of use, open today's note in `Log`. | One line for each thing saved or forgotten, with the time. |
| 8 | W2 | Send a 20-second voice note that starts "Tanios, please make a note to ...". | You hear a voice reply, and the note is in `Inbox`. |

Checks 2, 4, 6 and 7 can be done after part 1, on the server alone. The rest need part 2.

## If something goes wrong

| What you see | What to do |
| --- | --- |
| `Memory is not set up on this server yet` | The app did not get the update. Run `docker compose up -d --build`, then `docker compose logs app \| grep memory`. |
| `Sorry, I could not reach your notes just now` | The app cannot read or write the vault folder. Run `docker compose logs app \| grep vault` and send me the lines. |
| The app does not start after adding `TANIOS_TIMEZONE` | The name is not a real time zone. Use the form `Asia/Amman`. |
| Notes do not reach the phone | `docker compose logs --tail 50 vault-sync`. If it says you are not logged in, repeat steps 2, 4 and 5. |
| `vault-sync` keeps restarting | It was started before steps 2 to 5 were done. Do them, then `docker compose up -d`. |
| A reply apologises instead of answering from notes | The model did not say which notes it used, twice. Try again, and tell me if it keeps happening. |
| You want to stop syncing | Remove `COMPOSE_PROFILES=sync` from `.env`, then `docker compose --profile sync stop vault-sync`. The notes stay on the server and in Obsidian. |

## For developers

- `src/tanios/vault.py` is the only code that touches the vault folder. It refuses any
  path that leaves the folder, follows no links out of it, and ignores hidden files.
- `src/tanios/memory.py` holds the layout and the save, find and forget rules. Finding
  is plain keyword matching, with no model and no index.
- Each user's vault is the folder `user-<id>` under `TANIOS_VAULTS_DIR`. The compose
  file sets that to `/vaults`, a volume shared with the `vault-sync` container. Both
  containers run as user number 10001.
- The `vault-sync` container gets none of the settings in `.env`. The Obsidian sign-in
  lives in its own volume, `vault-sync-state`.
- Only the owner's vault (`user-1`) is synced. Vaults for testers come with milestone 7.
