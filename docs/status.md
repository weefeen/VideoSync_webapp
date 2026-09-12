# Where this is, and what is left

Written 2026-09-11, **corrected 2026-09-12** after a design-versus-code review
found this file claiming things the code contradicted. `docs/history.md` says
why things are the way they are; `docs/infrastructure.md` says what is running
where. This says what is done and what is not.

**How to read this file.** Every claim here is one of three things, and they
are never mixed: **verified** (someone ran it and saw the result),
**code-correct** (the code does the right thing, but it has not been run
against the real provider, broker or browser), or **not built**. The previous
version of this file said "Everything exists" about a compute node that had
four independent defects, each of which stopped it from taking a single task.
That sentence was read back as "it works", and it cost a day. Hence the rule.

---

## Live now, at chopin.weefeen.com — verified

| | |
|---|---|
| site | HTTPS, Let's Encrypt, renews itself |
| upload to recognise to render to email to download | **working, on the web box** (see the note below) |
| recognition | Op. 39 Scherzo, consensus 1.00 over 8 windows |
| alignment | all 649 measures, crowding 0.000, span 1.000 on a full recording |
| a 7-minute render | 557 s on the web box, 448 s on an 8 GB node |
| output format | H.264 High, faststart, AAC 48 kHz stereo, 23-60 fps - postable to FB/IG/YouTube |
| results | in Linode Object Storage, confirmed by HEAD before a job is called done |
| the whole project kept | manifest, alignment, chroma, verdict - 220 KB per job |
| mail | queued + ready, `dkim=pass spf=pass dmarc=pass` |
| bot check | Cloudflare Turnstile, **enforced and confirmed live** |
| monitoring | Grafana, 35 panels, two memory alerts emailing you |
| backups | nightly, verified readable, copied to the bucket |
| firewall | 22, 80, 443; SSH password auth off |
| visitor list | addresses with city, and what each person played |

**"End to end" means the web box.** The rendering happens on the always-on
machine, with the worker reading the upload off the shared disk. That is the
FALLBACK path, not the architecture that was chosen. The selected design -
render on a disposable node that is created and destroyed - has never run a
live job. Do not read the row above as saying it has.

**Cap: 8 minutes.** Not 25. The aligner allocates the full N x M DTW matrix,
so memory grows with the square of duration - 2.43 GB measured for a
7.1-minute recording, 4.62 GB for 14.1, on a 3.9 GB box. See history for the
numbers.

---

## The compute node - code-correct, never run

The honest state, replacing "Built, not switched on".

**What is real, and was proven by hand:** a restricted Linode user whose token
can create machines and see nothing else; a captured image; a driver that
refuses to delete anything not named `vsw-compute...`; a scaler running as a
service, reconciling against Linode every thirty seconds; and **a node that
rendered a video correctly** in a supervised test.

**What was not real:** the automated path. A review on 2026-09-12 found four
independent defects, each fatal on its own, and each verified in the code:

| | defect | fixed in |
|---|---|---|
| 1 | node read `current/.env`; cloud-init wrote `shared/.env`; nothing bridged them, so the worker booted with no `RABBITMQ_URL` and restart-looped | `3f50d03` |
| 2 | `driver.create` sent no `interfaces`, so a node had only a public NIC and no route to the broker | `3f50d03` |
| 3 | the transport declared the topology on connect, which the node's narrow `vsw-compute` user is forbidden to do - `ACCESS_REFUSED`, then a reconnect loop | `3f50d03` |
| 4 | `user_data` was sent as raw text where the Metadata service requires base64 | `3f50d03` |

Plus: the web box's own worker never stood aside, so its already-connected
consumer would take the task and the created node would idle for a paid hour.
Also fixed in `3f50d03`.

The earlier hand-run tests passed **because a person fixed these by hand** -
the input file was placed on the node, the environment written, the plumbing
adjusted. That is why those tests were real and the automation still did not
work. Both statements are true at once, and keeping them apart is the point of
this section.

**Input to bucket to node** (the other missing half) is built and **verified
against the real Linode bucket**: staged on the web side, fetched on a host
with no access to the shared disk, bytes identical (`982a223`).

**Still required before `COMPUTE_ENABLED=true`:**

1. **Rebuild the image.** A node boots the code baked in at image-build time -
   `compute-image-build.sh` rsyncs `/srv/vsw/current` onto a machine and the
   image is captured from it; cloud-init pulls no code at boot. The current
   image predates every fix above, so a node created from it still fails. This
   is the step that looks automatic and is not.
2. **Create the scoped object key** into `OBJECT_KEY_COMPUTE` /
   `OBJECT_SECRET_COMPUTE`. Empty today, so a node would be handed the
   full-bucket key. cloud-init marks that in the node's environment, but the
   containment is the point.
3. **One supervised create, render, destroy.** This is where the three things
   code cannot prove get proven: that Linode accepts the create body, that the
   `vsw-compute` user consumes cleanly without declaring, and that the VLAN
   actually routes.

---

## Built since the last version of this file

- **Input to bucket to node** (`982a223`) - verified against the real bucket.
- **Bot check** - Turnstile, live and enforced; the widget now clears itself
  once it succeeds instead of sitting on screen (`0f1c358`).
- **Transparent band over the video** (`0f1c358`) - at maximum transparency
  the notes floated over the backdrop instead of the performance, and the
  preview mirrored the bug rather than catching it.
- **Sharing**, all three parts: a mark burned into the pixels (`ae4007f`), an
  Open Graph card built from a real render still (`18b852f`), and a
  copy-ready caption with hashtags beside the download (`3098956`).

---

## Not built

**Per-job Open Graph.** The card is generic. A card showing *this* piece and
*this* performance needs the job id in the URL **path**: a crawler never sends
the `#fragment` the app uses today.

**Auto-deploy.** The workflow is `workflow_dispatch` only; the `push:` trigger
is still commented out.

**Glacier.** Considered and deferred: Linode Object Storage has no cold tier,
and $5/month covers 250 GB - about 1,700 videos - so archiving elsewhere saves
nothing until the low terabytes. `ARCHIVE_TRANSITION_DAYS` is still read from
the environment and used by nothing: dead configuration that reads as a
feature.

**The job tree of `queue-design.md` section 4.2.** `app/paths.py` describes
it, `pipeline.cleanup()` exists and is **called from nowhere**, and uploads
still land in `uploads/<id>.<ext>`. Nothing has ever been deleted except
expired outputs.

---

## Decisions waiting on you

**The 25-minute cap.** Restoring it needs either a 32 GB plan (about
$288/month if permanent, or $0.432/hour on demand) or a **banded DTW in
VideoScoreSync**, which would make memory linear rather than quadratic and put
25 minutes on a small machine. That change is in a read-only dependency and
you have deferred it.

**`transit/` versus keeping uploads.** `queue-design.md` section 10 says
uploads live under `transit/` and are deleted after 48 hours. You have said
uploads are kept for training. Both cannot be true. Related: the input staged
for a compute node is written to `jobs/<id>/work/input.<ext>` and is never
deleted either.

**Two-tier machines.** Your library is 249 recordings, median 3.5 minutes,
78% under 7 minutes, with a single 31-minute outlier. A 4 GB node at
$0.054/hour covers most of it; the outlier needs 32-64 GB. Whether to build
one tier or two is a cost decision, not a technical one.

---

## Known gaps in the checks

43 checks pass on Windows and Linux. What they do **not** cover:

- **anything visual.** `check_the_page_is_actually_styled` verifies that every
  class a script creates has a rule and that the CSS braces balance. It cannot
  see a layout, a colour, or an element positioned off-screen. The
  transparent-band bug lived in exactly this blind spot.
- **the compute node end to end.** The driver is tested against a fake and the
  bucket against an in-memory stand-in. Whether Linode accepts the create
  body, whether the broker admits the node's user, and whether the VLAN routes
  are **not** covered, and cannot be without spending money.
- **mail delivery.** The shape of a message is checked; whether it arrives is
  not, and cannot be.
- **the browser.** Turnstile's widget, the share buttons and the clipboard
  path are checked for structure, not for behaviour in a real browser.
