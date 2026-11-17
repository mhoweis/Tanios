# Putting Tanios on a server

This takes about 30 minutes the first time. You do it once; updates are one command.

You need:

- A Linux server that stays on, with 2 GB of memory. Ubuntu 24.04 is assumed below.
- A web address (a domain or subdomain) you control, for example `tanios.yourname.com`.

## 1. Point the web address at the server

Where you manage your domain, add an **A record** for the name you chose, with the
server's IP address as its value. It can take a few minutes to take effect.

## 2. Connect to the server and install Docker

```sh
ssh root@YOUR_SERVER_IP
curl -fsSL https://get.docker.com | sh
```

Open the firewall for web traffic if your provider has one: ports **80** and **443**
(and 22, which you are using now).

## 3. Get the code

If the repo is private, give the server read-only access first:

```sh
ssh-keygen -t ed25519 -f ~/.ssh/tanios_deploy -N ""
cat ~/.ssh/tanios_deploy.pub
```

Copy the line it prints. On GitHub open the repo, then **Settings**, **Deploy keys**,
**Add deploy key**, paste it, and leave "Allow write access" unticked. Then:

```sh
GIT_SSH_COMMAND="ssh -i ~/.ssh/tanios_deploy" git clone git@github.com:mhoweis/Tanios.git /opt/tanios
cd /opt/tanios
git config core.sshCommand "ssh -i ~/.ssh/tanios_deploy"
```

If the repo is public, this is enough:

```sh
git clone https://github.com/mhoweis/Tanios.git /opt/tanios
cd /opt/tanios
```

## 4. Fill in the settings

```sh
cp .env.example .env
echo "Password to use: $(openssl rand -hex 24)"
nano .env
```

Set `TANIOS_DOMAIN` to your web address and `POSTGRES_PASSWORD` to the password that
was just printed. Leave the WhatsApp and model lines empty for now. Save with Ctrl+O, Enter, then exit with Ctrl+X.

The `.env` file holds secrets. It stays on the server and is never committed.

## 5. Start it

```sh
docker compose up -d --build
```

The first start takes a few minutes. Then check, from any browser or phone:

- `https://YOUR_DOMAIN/healthz` should show `{"status":"ok","version":"..."}`
- `https://YOUR_DOMAIN/readyz` should start with `{"status":"ready","database":"ok"`

When both answer, milestone 0 is done. `/readyz` also shows `"whatsapp":"off"` until you
follow [SETUP-WHATSAPP.md](SETUP-WHATSAPP.md).

## Updating later

```sh
cd /opt/tanios
git pull
docker compose up -d --build
```

## If something is wrong

| What you see | What to do |
| --- | --- |
| The browser cannot connect | Check the A record points at the server and ports 80 and 443 are open. Then `docker compose logs caddy`. |
| `/healthz` works but `/readyz` says `down` | `docker compose logs db`. If you changed `POSTGRES_PASSWORD` after the first start, the database still has the old one. |
| `set POSTGRES_PASSWORD in .env` | The `.env` file is missing or the value is empty. |
| Anything else | `docker compose ps` shows what is running; `docker compose logs app` shows the app's messages. |

## Backups

Two things hold your data: the database (usage, drafts, sign-ins, keys) and the vaults
(notes and saved files). This makes a copy of both in `/opt/tanios-backups`:

```sh
mkdir -p /opt/tanios-backups && cd /opt/tanios
docker compose exec -T db pg_dump -U tanios tanios | gzip > /opt/tanios-backups/db-$(date +%F).sql.gz
docker compose exec -T app tar czf - -C /vaults . > /opt/tanios-backups/vaults-$(date +%F).tgz
ls -lh /opt/tanios-backups
```

Both files hold private data, and the database copy holds sign-ins and keys (stored
encrypted, with the key in `.env`). Keep them on the server or somewhere as private.
Copy `.env` somewhere safe too: without `TANIOS_ENCRYPTION_KEY` the stored sign-ins
and keys cannot be opened, and everyone has to connect again.

## Removing everything

To delete Tanios and every piece of data it holds on this server:

```sh
cd /opt/tanios
docker compose --profile sync down -v
```

The `-v` deletes the database and the vaults. It cannot be undone. Then remove Tanios's
access in each Google account at https://myaccount.google.com/permissions, and delete
the keys you made at OpenRouter and Meta.

To remove only a tester, send `remove` and their number on WhatsApp instead (see
[TESTERS.md](TESTERS.md)).
