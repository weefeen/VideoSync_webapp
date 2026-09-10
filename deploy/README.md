# deploy/

How code gets onto a machine, and what has to exist there first.

| | |
|---|---|
| `bootstrap.sh` | a bare Ubuntu box → system packages, the `vsw` user, the layout, the shared venv. Safe to re-run. |
| `deploy.sh` | runs **on the server**: link shared state, install dependencies, check the release starts, swap `current`, restart, health-check, roll back if it fails. |
| `vsw-worker.service` | the renderer. The only process that consumes `vsw.render`. |
| `vsw-web.service` | serves, applies what the worker reports, sweeps. Does **not** render when a broker is configured. |
| `install-units.sh` | installs and enables both units. Safe to re-run. |

## The layout

```
/srv/vsw/
  releases/<timestamp>/   one deploy's code
  current -> releases/…   what the units run
  shared/.env             configuration and secrets, never in the repo
  shared/var/             jobs.sqlite, uploads, finished videos
  venv/                   ONE environment, shared by every release
  scores/                 score packages — build products, in no repository
```

`/srv/vsw` is the path on **every** host. On production the storage lives on
a mounted volume, and `/srv/vsw` is a symlink to `/mnt/volume_1/vsw`. One
path everywhere means one set of unit files instead of two that drift apart.

**The venv is shared, not per-release.** That departs from the script this
was adapted from, deliberately: it is 1.5 GB because of torch, so keeping
five releases would be 7.5 GB of near-identical copies, and its heavy half is
dictated by two read-only engine repositories that do not change when this
app's code does. What a rollback has to undo is the code.

## Setting up a host, once

```bash
sudo deploy/bootstrap.sh              # packages, user, layout, venv
sudo cp .env.prod /srv/vsw/shared/.env
sudo -e /srv/vsw/shared/.env          # the SMTP and broker passwords
sudo chown vsw:vsw /srv/vsw/shared/.env && sudo chmod 600 /srv/vsw/shared/.env
sudo deploy/install-units.sh
```

### The title-panel fonts

Licensed, so they are in no repository and no release. Put them in `shared/`
beside `.env`, where they survive deploys:

```bash
sudo install -d -o vsw -g vsw /srv/vsw/shared/fonts
sudo cp /path/to/FuturaLTPro/* /srv/vsw/shared/fonts/
sudo chown -R vsw:vsw /srv/vsw/shared/fonts
```

`FONT_DIR` in `.env` points there. **Miss this and only renders with a title
panel fail** — minutes in, as "No Futura LT Pro font in …" — while renders
without one succeed all day. That is exactly how it went unnoticed on the
test node: the default is no panel. `tools/doctor.py` and startup both say
so now.

Then the broker, if this host runs one:

```bash
sudo apt install rabbitmq-server
sudo rabbitmqctl add_vhost vsw
sudo rabbitmqctl add_user vsw '<a real password>'
sudo rabbitmqctl set_permissions -p vsw vsw ".*" ".*" ".*"
sudo rabbitmqctl delete_user guest        # the default account
```

**Check what it listens on.** The package binds 5672 *and* 25672 to
`0.0.0.0`. On a host with a public address that exposes the broker and the
Erlang distribution port — the latter protected only by the Erlang cookie.
`/etc/rabbitmq/rabbitmq.conf`:

```
listeners.tcp.default = 127.0.0.1:5672
distribution.listener.interface = 127.0.0.1
```

### The deploy user needs to restart two units, and nothing else

`deploy.sh` runs as `vsw` over SSH and has to restart the services. Give it
exactly that and no more — `/etc/sudoers.d/vsw-deploy`, mode 0440:

```
vsw ALL=(root) NOPASSWD: /usr/bin/systemctl restart vsw-worker vsw-web, \
                         /usr/bin/systemctl restart vsw-worker, \
                         /usr/bin/systemctl restart vsw-web
```

Validate it with `visudo -cf /etc/sudoers.d/vsw-deploy` before trusting it;
a broken sudoers file can lock the box out of `sudo` entirely.

### The key GitHub deploys with

Generate it on the server, keep the private half off this repository and out
of any transcript:

```bash
sudo -u vsw ssh-keygen -t ed25519 -N '' -C 'github-deploy' -f /srv/vsw/.ssh/deploy
sudo -u vsw tee -a /srv/vsw/.ssh/authorized_keys < /srv/vsw/.ssh/deploy.pub
sudo -u vsw chmod 600 /srv/vsw/.ssh/authorized_keys
```

Then read `/srv/vsw/.ssh/deploy` yourself and paste it into GitHub. Four
secrets, under Settings → Secrets and variables → Actions:

| secret | value |
|---|---|
| `SSH_PRIVATE_KEY` | the contents of `/srv/vsw/.ssh/deploy` |
| `SSH_KNOWN_HOSTS` | `ssh-keyscan <host>` |
| `DEPLOY_HOST` | the address |
| `DEPLOY_USER` | `vsw` — never root |

Finally, uncomment the two `push:` lines in `.github/workflows/deploy.yml`.
Until then the workflow is manual-only, and its `configured` job skips
cleanly rather than failing when the secrets are absent.

### Geolocation, so the visitor list has a country and a city

```bash
sudo -u vsw deploy/fetch-geoip.sh          # ~60 MB, free, no account
```

Downloads DB-IP's City Lite database to `/srv/vsw/shared/geoip/` and points
`current.mmdb` at it. Then set `GEOIP_DB=/srv/vsw/shared/geoip/current.mmdb`
in `/srv/vsw/shared/.env` — it is in `.env.prod` already — and restart the
web unit.

Run it monthly; addresses get reassigned, and a year-old database quietly
reports the wrong city. Nothing breaks without it: the visitor list still
renders, with the country and city columns empty and the reason printed at
the top.

**The lookup is a read of that file.** No visitor's address is sent to a
geolocation service, which is what the privacy page says, so any change to
this that introduces a network call is a change to a published promise.

### Alerts that reach a person

```bash
sudo deploy/install-alert-mail.sh
```

Reads `SMTP_*` and `ALERT_EMAIL` out of `/srv/vsw/shared/.env`, writes them
into `grafana.ini`, provisions the contact point and the notification policy,
restarts Grafana and sends a test. Refuses with instructions if either is
missing, rather than half-configuring.

**Nothing secret is in this repository and nothing needs to be.** The app
already keeps `SMTP_*` in `.env` for the "your video is ready" mail, so the
alerts borrow the same credentials and the same place to rotate them.

Until `SMTP_HOST` is set, two things do not work and both are silent: alerts
fire and are visible under Alerting but email nobody, and no visitor is ever
told their video is ready.

### Deploying by hand, from a Windows machine

The workflow rsyncs from a GitHub runner. Git Bash on Windows has no `rsync`,
and shipping the Windows working tree directly is worse than inconvenient:
files there carry CRLF, and a shell script with CRLF fails on Linux with
`$'
': command not found`. Let the server take the code from GitHub instead,
where the committed blobs are LF:

```bash
REL="main-$(git rev-parse --short HEAD)-$(date -u +%Y%m%d%H%M%S)"
ssh root@<host> "
  git clone -q --depth 1 --branch main <repo-url> /srv/vsw/releases/$REL
  rm -rf /srv/vsw/releases/$REL/.git /srv/vsw/releases/$REL/.github
  chown -R vsw:vsw /srv/vsw/releases/$REL
  bash /srv/vsw/releases/$REL/deploy/deploy.sh $REL"
```

Push to `main` first — the server clones from there, so anything uncommitted
is not deployed. The rest is identical to the workflow: same release layout,
same health check, same rollback.

### Checking a download by hand

`/api/jobs/<id>/download` answers **302**, not the file. Any client that
checks it must follow redirects — `curl -L`, not plain `curl`, which reports
a successful zero-byte download and looks exactly like a broken render:

```bash
curl -sSL -o out.mp4 -w '%{http_code} %{size_download}
'      https://<host>/api/jobs/<id>/download
```

The signed link lives 15 minutes and carries the download filename, so the
browser saves it under the score's name rather than the object key.

## Rolling back

A release that does not answer its health check inside a minute is rolled
back automatically, by the same script, before it reports failure. To go
back by hand:

```bash
ls -1t /srv/vsw/releases | head            # what is kept
sudo ln -sfn /srv/vsw/releases/<older> /srv/vsw/current
sudo systemctl restart vsw-worker vsw-web
```

Five releases are kept. The one `current` points at is never pruned,
whatever its age.

**A rollback does not undo the database.** `shared/var/jobs.sqlite` is shared
by every release, and columns are only ever added, never removed — so an
older release ignores a column it does not know about. Removing one would
break rollback, and that is the reason not to.

## What a deploy does not touch

`shared/`, `venv/`, `scores/`, and anything belonging to another application
on the same box. The rsync excludes `.env`, `var`, `.git`, `.github` and
`__pycache__`; the restart names two units rather than matching a pattern.
