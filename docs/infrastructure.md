# The machines, and how they are configured

What exists, where, and why it is arranged this way. Written 2026-09-11,
after the day the site went public.

`deploy/README.md` says how to *install* things. This says what is actually
running and what each piece is protecting against, so somebody reading it
cold can tell a deliberate choice from an accident.

---

## The shape

```
                        the internet
                             │
                        443 / 80  (and 22)
                             ▼
        ┌────────────────────────────────────────┐
        │  WEB BOX  172.104.237.127              │
        │  chopin.weefeen.com · Ubuntu 24.04     │
        │  2 cores · 3.9 GB · 79 GB              │
        │                                        │
        │  apache2  ──▶ gunicorn 127.0.0.1:5000  │
        │  rabbitmq 127.0.0.1 + 10.0.0.2:5672    │
        │  grafana 127.0.0.1:3000                │
        │  prometheus 127.0.0.1:9090             │
        │  node_exporter 127.0.0.1:9100          │
        │  vsw-worker    (renders, for now)      │
        │  vsw-scaler    (shadow: creates none)  │
        │  jobs.sqlite   /srv/vsw/shared/var     │
        └───────────┬────────────────────────────┘
                    │ VLAN eth1 · 10.0.0.0/24 · account-isolated
                    │ (free, and off the transfer quota)
                    ▼
        ┌────────────────────────────────────────┐
        │  COMPUTE NODE  10.0.0.3   NOT YET LIVE │
        │  created from image, destroyed after   │
        │  vsw-worker only                       │
        └───────────┬────────────────────────────┘
                    │ HTTPS
                    ▼
        Linode Object Storage · eu-central · bucket `video-sync`
```

---

## Web box

**Why one box for everything.** At this traffic a second machine is a second
thing to patch, monitor and pay for. The split that matters is the *render*,
because it saturates the CPU for ten minutes and its cost scales with
visitors; everything else is idle most of the day.

| service | binds | why there |
|---|---|---|
| `apache2` | `*:80`, `*:443` | TLS, and the only thing facing the internet |
| `gunicorn` | `127.0.0.1:5000` | never public; Apache decides what reaches it |
| `rabbitmq` | `127.0.0.1:5672`, `10.0.0.2:5672` | loopback for this box, VLAN for the compute node, **never the public interface** |
| `grafana` | `127.0.0.1:3000` | reached over an SSH tunnel; it ships with a default login |
| `prometheus` | `127.0.0.1:9090` | queue depth and failure counts describe the business |
| `node_exporter` | `127.0.0.1:9100` | reports the machine's memory, disks and network |
| `epmd` | `127.0.0.1:4369` | was on every interface, handing out Erlang node names |

**Four endpoints are blocked at Apache** and not merely unadvertised:
`/metrics`, `/api/failures`, `/api/compute`, `/api/visitors`. The last
carries visitors' IP addresses — the most personal thing the application
holds. A plain `ProxyPass /` publishes all four, so each is denied *before*
the proxy and `ProxyPass … !` keeps it from reaching the app at all. The
installer verifies from outside **and** from loopback, because "I wrote a
Location block" and "the endpoint is unreachable" are different claims.

**`TRUST_PROXY=true`, and it had to be turned on at the same moment Apache
appeared.** With a proxy in front and this off, `remote_addr` is `127.0.0.1`
for every visitor: one shared rate-limit bucket and a visitor list with one
row in it. With it on and *no* proxy, anyone can forge the header and spend
everyone else's quota. Neither setting is safe on its own.

**`ProxyTimeout 3600` and gunicorn `--timeout 3600`.** The default proxy
timeout is 60 *seconds*; 4 GB at 20 Mbit/s takes about 27 minutes. They have
to match, or an upload dies at whichever is shorter.

**Firewall:** 22, 80, 443. Nothing else. The value is not today's ports — it
is that anything installed later which starts listening is blocked by
decision rather than reachable by default. The rule for 22 is added and
*verified present* before enabling; `ufw enable` without it locks everybody
out of a machine reachable only by SSH.

---

## The private network

`eth1` at `10.0.0.2/24`, VLAN `vsw-private`. Account-isolated Layer 2: only
machines on this account, in this region, attached to this VLAN can see it.
Free, and its traffic does not count against the transfer quota.

**Adding it required a reboot** — a network interface can only be attached
while the machine is off. Done while the queue was empty and nobody knew the
URL. Compute nodes never reboot for this: their interface is part of the
create call, with `10.0.0.3` fixed there.

**The alternative rejected:** a public broker listener with TLS. The compute
node's public address changes on *every* create, so a firewall rule naming it
is wrong for a window on each one — the queue would be protected by a
password alone.

**Broker users:**

| user | may |
|---|---|
| `vsw` | everything, over loopback — the web app and the local worker |
| `vsw-compute` | `configure ^$` — **cannot declare, delete or reshape anything**; read and write limited to `vsw.render` and `vsw.events` |

A compromised compute node can take work and report results. It cannot
remove anybody else's.

---

## Object storage

Linode Object Storage, `eu-central` (same region as the machines, so
transfer is free and fast — measured at 199 MB/s), bucket `video-sync`.

**$5/month flat for 250 GB and 1 TB of transfer**, $0.02/GB beyond. At about
148 MB kept per job that is roughly **1,700 videos** before storage costs
anything extra.

**There is no cold tier.** Linode Object Storage lifecycle rules do expiry
and abandoned-upload cleanup only — no Glacier equivalent to transition
into. Archiving to AWS was considered on 2026-09-11 and deferred: it saves
nothing until the low terabytes and costs an account, a second set of
credentials, and a 3–5 hour retrieval. `vsw_stored_bytes` against
`vsw_storage_included_bytes` on the dashboard is what will say when to
revisit.

**What is kept per job:**

```
jobs/<id>/output/<id>_PROCESSED.mp4    the video
jobs/<id>/work/manifest.json           score, engraving hash, mode, versions
jobs/<id>/work/recognition.json        verdict + accepted/overridden
jobs/<id>/work/measures.data           the alignment
jobs/<id>/work/sync/performance.npy    the chroma
jobs/<id>/work/sync/reference_measures.data
jobs/<id>/work/sync/result.json
jobs/<id>/work/render.attempt1.json
backups/jobs-<timestamp>.sqlite.gz     nightly database snapshot
```

220 KB of derived data against a 131 MB video, and it is the half that
cannot be recreated once the machine is gone.

---

## Credentials, and where each one lives

Everything is in `/srv/vsw/shared/.env`, `0600 vsw:vsw`, outside every
release directory so a deploy replaces the code and leaves it alone. It is
in `.gitignore` and untracked.

| credential | reaches | never reaches |
|---|---|---|
| `LINODE_TOKEN` | the scaler, on the web box | a compute node, an image, git |
| `OBJECT_KEY/SECRET` | web box **and** compute nodes | an image |
| `SMTP_PASSWORD` | the web box | a compute node — only the web side sends mail |
| `compute-broker.pass` | handed to each node in `user_data` | an image |

**The Linode token is the only credential that can spend money without
limit.** It belongs to a *restricted user* (`vsw-scaler`) holding one role,
`account_linode_creator`. Verified: it lists **0** instances where the
unrestricted token listed 3, and cannot read the account. It can create
machines and see nothing else.

A compute image contains **no credentials at all**, so a leaked image gives
an attacker this software and no access to anything.

---

## Mail

`info@weefeen.com` through `mail.weefeen.com:465` (A2 Hosting, implicit TLS,
certificate verified). A2 relays outbound through MailChannels.

All four authentication checks pass: **SPF, DKIM (2048-bit, selector
`default`), DMARC (`p=none`), and PTR.**

Three fixes were needed on our side, and all three had been silently wrong:

- **no `Date` header** — mandatory under RFC 5322, and `smtplib.send_message`
  does not add one
- **no `Message-ID`** — same omission; also breaks threading
- **the body was being rewritten in transit**, breaking DKIM. A single
  em-dash forced quoted-printable, which soft-wraps long lines, and a relay
  re-wrapping them changes the body the signature covers. Bodies are now
  7-bit ASCII hard-wrapped at 72 characters, with accents transliterated
  (`3ème` → `3eme`) rather than replaced by `?`.

Also: the SMTP greeting announced `HELO [127.0.0.1]`, because the box's
hostname is literally `localhost`. It now uses `chopin.weefeen.com`.

---

## Backups

`vsw-backup.timer`, nightly at 03:30 UTC with a random delay.

Uses sqlite's `.backup`, **never `cp`** — the database is in WAL mode and
written to while the copy runs, so a plain file copy yields a torn snapshot
that restores as corruption at the worst possible moment.

Then gunzips what it just wrote, runs `PRAGMA integrity_check`, counts the
rows, and uploads to the bucket. **An unverified backup is a hope.** Kept
three weeks locally; the bucket holds the longer history.

---

## What is deliberately NOT here

**No compute node yet.** `vsw-scaler` runs in shadow: it reconciles against
Linode, decides, records what it would do, and creates nothing.
`COMPUTE_ENABLED=false`.

**No bot check.** Uploads are unauthenticated. The defences are the rate
limits and the duration cap; Cloudflare Turnstile is pending.

**No auto-deploy.** The GitHub workflow is `workflow_dispatch` only. Deploys
are done by hand, and from Windows that means the server clones from GitHub
rather than receiving an rsync — Git Bash has no rsync, and shipping the
Windows working tree carries CRLF, which makes a shell script fail on Linux
with `$'\r': command not found`.
