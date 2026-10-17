# tangerine

Hosts a private document archive behind a login page. Only the users you list can get in, and every login is logged with the time, IP and location.

```
browser ──https──▶ nginx ──asks──▶ tangerine-gate   login page, sessions, door log
                     │
                     └── if signed in ──▶ tangerine-server   the archive
```

Both backends listen on localhost only, so nginx is the only way in.

## What's here

| | |
|---|---|
| `config.example.yaml` | Every setting: domain, data paths, users, login page text, alerts, TLS |
| `build.sh` | Builds the archive server (`tangerine-server`) from its Rust source tarball |
| `install.py` | Renders `templates/` from your config and installs everything (idempotent) |
| `gate/tangerine-gate` | The login gate and its admin CLI (Python + PyYAML) |
| `gate/tangerine-geoip-update` | Monthly IP-location database refresh |
| `ask/` | `tangerine-ask` (LLM questions about a document) and the overlay it adds to document pages |
| `templates/` | nginx, systemd and fail2ban files with `{{placeholders}}` |

## Setup

Needs Linux with systemd, nginx, certbot, python3-pyyaml, and optionally python3-maxminddb for locations.
The build also needs Rust, podman or docker.

```sh
sudo dnf install nginx certbot python3-pyyaml python3-maxminddb    # Debian: apt install nginx certbot python3-yaml python3-maxminddb

./build.sh /path/to/server-source.tar.gz
cp config.example.yaml config.yaml && $EDITOR config.yaml          # domain, index.json paths, users
sudo ./install.py
sudo tangerine-gate passwd alice                                   # once per user
```

Before running `install.py`, point the domain's DNS at the machine and make sure ports 80 and 443 reach it. The installer gets the Let's Encrypt certificate.
To see exactly what it would install without changing anything, run `./install.py --render-only /tmp/preview`.

## Day to day

**Add or remove a person:** edit `users:` in `/etc/tangerine/config.yaml` (or in your `config.yaml`, then re-run `install.py`). Then set their password:

```sh
sudo tangerine-gate passwd bob --random    # prints a generated password to hand over
```

Removing someone from `users:` signs them out within seconds. So does changing their password.

**How long sign-ins last:** a day, or two weeks if "Remember me" is ticked on the login page (`gate.session_hours`, `gate.remember_days`). Both count from the moment of signing in.

**See who's been in:**

```sh
sudo tangerine-gate users                  # everyone, with last login time and place
sudo tangerine-gate log --user bob         # door log: logins, failures, logouts, IP changes
sudo tangerine-gate sessions               # who's signed in right now
sudo tangerine-gate revoke --user bob      # kick someone
```

Admins (`admin: true`) can also see and kick sessions at `https://<domain>/_gate/admin`.
Set `gate.notify_url` (for example an ntfy.sh topic) to get a push notification on every sign-in and whenever someone starts guessing passwords.

**Ask about a document** (`ask:` in the config): an "Ask" button on the pages of the collections in `ask.indexes` opens a chat about the document being viewed (everything under one `/A/B/C/` path). Each person picks OpenAI or Anthropic, pastes their own API key and chooses a model; the picker shows each model's list price per million tokens. They pay their provider for what they ask. The first question about a document reads all of its pages into a search index, which takes about a minute. It reads from `tangerine-crawl`, a second copy of the archive server without rate limits that only tangerine-ask can use (localhost only, about 1 GB of RAM). The model then searches and reads pages and cites them, and the figures of cited pages show under the answer. Follow-up questions carry the conversation so far. Conversations are kept only in each person's browser, never on the server. Questions and the excerpts needed to answer them go to the chosen provider; the panel says so before first use. The index is capped at `ask.index_max_gb` (30 GB). Past 90% of that, the least-asked documents are dropped and re-read the next time someone asks about them. The panel's wording, its example questions and the model's instructions can be set with `ask.noun`, `ask.examples` and `ask.system_prompt`.

**Change anything else** (domain, login page text, limits): edit the config, then `sudo ./install.py`.

**Logs:** `journalctl -u tangerine-gate` / `-u tangerine` / `-u tangerine-ask` / `-u tangerine-crawl`, and `/var/log/nginx/tangerine.access.log`, which tags every request with the username.

## Files on the server

| | |
|---|---|
| `/etc/tangerine/config.yaml` | Installed config (read by the gate) |
| `/etc/tangerine/passwords` | scrypt password hashes, written by `tangerine-gate passwd` |
| `/var/lib/tangerine-gate/` | `audit.db` (door log), session signing key, GeoIP database |
| `/etc/tangerine/ask.yaml` | tangerine-ask's own settings, rendered from `ask:` |
| `/var/lib/tangerine-ask/` | `index.db` (search index, capped by `ask.index_max_gb`), `ask.db` (each person's provider, model and encrypted API key), `keys.secret` (the key that encrypts them) |
| `/opt/tangerine-ask/venv` | Python environment with the OpenAI and Anthropic SDKs |

`config.yaml` holds no secrets, since passwords live on the server. It's git-ignored because it's machine-specific; commit it if you want it tracked.

IP geolocation by [DB-IP](https://db-ip.com) (CC BY 4.0).

## License

[CC BY-SA 4.0](LICENSE). This covers the code in this repo only, not the server built by `build.sh` or the data it serves.
