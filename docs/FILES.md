# Saving files

Tanios can keep a file in the `Files` folder of its vault, under a name you choose, and
add a line for it to the `Index` note there.

Memory must be on first (see [MEMORY.md](MEMORY.md)). Saving from an email also needs
Gmail connected (see [SETUP-GMAIL.md](SETUP-GMAIL.md)).

## What you can do

| You do | What happens |
| --- | --- |
| Send a PDF on WhatsApp with the caption `save this as my lease` | It is saved as `Files/My lease.pdf` and listed in `Files/Index`. |
| Send a file with no caption, then send `save this as my lease` | The same. Tanios holds the file for an hour. |
| Send a file with the caption `save` | It is saved under its own name, tidied up. |
| `save the attachment from 2 as my lease` | The file attached to email 2 in your last `what's new?` list is saved. |
| `save lease-scan.pdf from 2` | The same, when the email has several attachments and you name one. |

Good to know:

- **Nothing is saved unless you say so.** A file sent without `save` is held for an hour
  and then dropped.
- **Tanios does not read files.** It stores them exactly as they are. Asking "what does
  this say?" about a file does not work yet.
- **Saving uses no model.** It costs nothing and works when your monthly cap is reached.
- **Names never clash.** A second file called `My lease.pdf` is saved as `My lease 2.pdf`.
- **Which types are kept:** PDFs, Word, Excel and PowerPoint files, text and CSV files,
  pictures, audio and video. Programs, scripts, web pages, archives such as `.zip`, and
  Markdown files are refused.
- **Size:** up to 25 MB. To change it, add `TANIOS_MAX_FILE_MB=10` (or another number,
  up to 100) to `.env` and restart.
- **Suspicious emails:** Tanios will not save an attachment from an email it labelled
  Suspicious.
- **Finding a file later:** ask `where is my lease?`. Tanios answers from the `Index`
  note.
- **While paused,** files are neither held nor saved.

## Turn it on

There is nothing new to set. Update the server:

```sh
cd /opt/tanios
git pull
docker compose up -d --build
```

## If you use Obsidian Sync

Obsidian Sync only carries the file types you switch on, and it starts with some of
them off.

**On the server,** tell the sync helper to carry them all:

```sh
cd /opt/tanios
docker compose --profile sync run --rm vault-sync ob sync-config --path /vaults/user-1 --file-types image,audio,video,pdf,unsupported
docker compose restart vault-sync
```

`unsupported` is Obsidian's word for everything else, such as Word and Excel files.

**On your phone and computer,** open Obsidian's settings, go to **Sync**, and switch on
the same file types under selective sync.

Obsidian Sync also has a size limit per file that depends on your plan. A file over
that limit stays on the server and does not reach your phone.

## Checks

Tell me which pass and which fail.

| # | Requirement | Do this | Expect |
| --- | --- | --- | --- |
| 1 | M6 | Send a PDF with the caption `save this as my lease`. | Tanios confirms. The PDF is in `Files` in the vault and `Files/Index` has a line for it. |
| 2 | M6 | Send a photo with no caption. Then send `save this as passport photo`. | First Tanios says it is holding the picture. Then it is saved and listed. |
| 3 | M6 | Send the same PDF again with the same caption. | It is saved as `My lease 2.pdf`. The first one is unchanged. |
| 4 | M6 | Email yourself a PDF from another account. Send `what's new?`, then `save the attachment from 1 as test invoice` (use the email's number). | It is saved and listed, with the sender's name in the index line. Nothing changed in Gmail. |
| 5 | M6 | Send a `.zip` or `.exe` file with the caption `save`. | Tanios refuses and says which types it keeps. |
| 6 | M8 | Open today's note in `Log`. | One `Saved` line per file. |
| 7 | M4 | Ask `where is my lease?` | The answer points to the file and ends with `From your notes: Index`. |

Checks 1 to 3 and 5 to 7 work on the server alone:
`docker compose exec app ls -l /vaults/user-1/Files`. Seeing the files on your phone
needs the Obsidian Sync step above.

## If something is wrong

| What you see | What to do |
| --- | --- |
| `Memory is not set up on this server yet, so I have nowhere to save files.` | Do part 1 of [MEMORY.md](MEMORY.md). |
| `There is no file waiting.` | Send the file first, or send it with the caption. A held file is dropped after an hour, and after a server restart. |
| `That file is too large.` | Raise `TANIOS_MAX_FILE_MB` in `.env`, then `docker compose up -d`. |
| `I do not save ".zip" files.` | That type is refused on purpose. Unpack it and send what is inside. |
| `Sorry, I could not fetch that file from WhatsApp.` | Send it again. |
| The file is on the server but not on your phone | The sync step above: the file type is switched off, or the file is over your plan's size limit. |

## For developers

- `src/tanios/files.py` holds the rules: the list of extensions that are kept, how a
  save request is recognised, and how a size is shown. No model is involved.
- `Memory.keep_file` in `src/tanios/memory.py` stores the bytes and adds the index
  line. Both the name the user typed and the file's own name pass through `note_title`,
  so neither can carry a path. If the index line cannot be written, the file is taken
  back out.
- The bytes of a file are never read, parsed, opened or run by Tanios.
- A file that arrives without `save` is remembered in the server's memory only, by
  WhatsApp's media ID. It is not fetched until the user asks.
- Gmail attachments are read with one extra allowed call,
  `GET /messages/<id>/attachments/<id>`.
