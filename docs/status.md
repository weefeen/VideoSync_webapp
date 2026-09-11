# Where this is, and what is left

Written 2026-09-11. `docs/history.md` says why things are the way they are;
`docs/infrastructure.md` says what is running where. This says what is done
and what is not.

---

## Live now, at chopin.weefeen.com

| | |
|---|---|
| site | HTTPS, Let's Encrypt, renews itself |
| upload → recognise → render → download | working end to end |
| recognition | Op. 39 Scherzo at 98%, consensus 1.00 over 8 windows |
| a 7-minute render | 557 s on the web box, 448 s on an 8 GB node |
| results | in Linode Object Storage, confirmed before a job is called done |
| the whole project kept | manifest, alignment, chroma, verdict — 220 KB per job |
| mail | queued + ready, `dkim=pass spf=pass dmarc=pass` |
| monitoring | Grafana with 30 panels, two memory alerts emailing you |
| backups | nightly, verified readable, copied to the bucket |
| firewall | 22, 80, 443 |
| visitor list | addresses with city, and what each person played |

**Cap: 8 minutes.** Not 25. The aligner allocates the full N×M DTW matrix,
so memory grows with the square of duration — 2.43 GB measured for a
7.1-minute recording on a 3.9 GB box. See history for the numbers.

---

## Built, not switched on

**The compute node.** Everything exists: a restricted Linode user whose
token can create machines and see nothing else, a captured image
(`vsw-compute-2026-09`), a driver that refuses to delete anything not named
`vsw-compute…`, cloud-init, and a scaler running as a service.

`COMPUTE_ENABLED=false`. The scaler reconciles against Linode every thirty
seconds, decides, records what it would have done, and creates nothing.

**Before turning it on:**

1. leave it in shadow through real traffic — the decision log shows whether
   its create/destroy timing matches what you would have wanted
2. one supervised create → render → destroy
3. then unattended

---

## Not built

**Input → S3.** A compute node cannot fetch a recording it has no access
to. The output half is done; this is the other half, and it blocks the
compute node from doing real work.

**Bot check.** Uploads are unauthenticated and the site is public. The
defences today are the rate limits and the duration cap. Cloudflare
Turnstile is free and does not require the domain to be on Cloudflare;
it needs a site key and a secret from your account.

**Sharing.** Open Graph tags so a pasted job link shows a card with the
piece name and a thumbnail, share buttons that pre-fill a hashtag, and a
small visible watermark. A watermark makes a video recognisable; only a
hashtag makes it searchable.

**Auto-deploy.** The workflow is `workflow_dispatch` only; the `push:`
trigger is still commented out.

**Rebuild the image.** The current one lacks the `vsw` user and the worker
unit, so cloud-init creates them at every boot — seconds on each create, and
two more things that can fail while a visitor waits.
`deploy/compute-image-build.sh` bakes them in.

**Glacier.** Considered and deferred: Linode Object Storage has no cold
tier, and $5/month covers 250 GB — about 1,700 videos — so archiving
elsewhere saves nothing until the low terabytes.

---

## Decisions waiting on you

**The 25-minute cap.** Restoring it needs either a 32 GB plan (~$288/month
if permanent, or $0.432/hour on demand) or a **banded DTW in
VideoScoreSync**, which would make memory linear rather than quadratic and
put 25 minutes on a small machine. That change is in a read-only dependency
and you have deferred it.

**`transit/` versus keeping uploads.** `queue-design.md` §10 says uploads
live under `transit/` and are deleted after 48 hours. You have said uploads
are kept for training. Both cannot be true.

**Two-tier machines.** Your library is 249 recordings, median 3.5 minutes,
78% under 7 minutes, with a single 31-minute outlier. A 4 GB node at
$0.054/hour covers most of it; the outlier needs 32–64 GB. Whether to build
one tier or two is a cost decision, not a technical one.

---

## Known gaps in the checks

27 checks pass on Windows and Linux. What they do **not** cover:

- **anything visual.** `check_the_page_is_actually_styled` verifies that
  every class a script creates has a rule and that the CSS braces balance.
  It cannot see a layout, a colour, or an element positioned off-screen.
- **the compute node end to end.** The driver is tested against a fake. The
  real path has been exercised by hand, not by a check.
- **mail delivery.** The shape of a message is checked; whether it arrives
  is not, and cannot be.
