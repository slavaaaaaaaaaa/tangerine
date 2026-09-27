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

**See who's been in:**

```sh
sudo tangerine-gate users                  # everyone, with last login time and place
sudo tangerine-gate log --user bob         # door log: logins, failures, logouts, IP changes
sudo tangerine-gate sessions               # who's signed in right now
sudo tangerine-gate revoke --user bob      # kick someone
```

Admins (`admin: true`) can also see and kick sessions at `https://<domain>/_gate/admin`.
Set `gate.notify_url` (for example an ntfy.sh topic) to get a push notification on every sign-in and whenever someone starts guessing passwords.

**Change anything else** (domain, login page text, limits): edit the config, then `sudo ./install.py`.

**Logs:** `journalctl -u tangerine-gate` / `-u tangerine`, and `/var/log/nginx/tangerine.access.log`, which tags every request with the username.

## Files on the server

| | |
|---|---|
| `/etc/tangerine/config.yaml` | Installed config (read by the gate) |
| `/etc/tangerine/passwords` | scrypt password hashes, written by `tangerine-gate passwd` |
| `/var/lib/tangerine-gate/` | `audit.db` (door log), session signing key, GeoIP database |

`config.yaml` holds no secrets, since passwords live on the server. It's git-ignored because it's machine-specific; commit it if you want it tracked.

IP geolocation by [DB-IP](https://db-ip.com) (CC BY 4.0).

## License

[CC BY-SA 4.0](LICENSE). This covers the code in this repo only, not the server built by `build.sh` or the data it serves.
