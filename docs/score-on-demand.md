# Scores engraved on demand — mostly not pursued

**Decided 2026-09-11, the day this was written.** The owner's answer was that
he will engrave the scores quickly himself. That removes the premise: this
design exists to ration an hour of engraving per score, and there is no
scarcity to manage.

**Dead — do not build:** the staging root (§3), the admin page (§4), the proof
render, and anything that parks a visitor's job or promises them a mail (§6).

**Alive:** the demand mail (§7), because knowing *what* to engrave is useful
however fast the engraving is. Revised per the owner: **every request mails
immediately, with no batching and no bookkeeping** — the second person never
generates a mail, because by then the score is published. See §7.

**Also alive, independent of all of it:** §1 is a set of verified findings
about the code, three of them live defects. Those are in `status.md`.

Written 2026-09-11 against `e4d2142` on `main`. Nothing here is built.

There are about 235 Chopin scores in principle and **one installed**. Engraving
the rest up front is an hour a piece for no one in particular. This is the loop
that engraves them in the order people actually ask for, with a human check
before each one starts serving the public — and none after.

The design was drafted by Fable and reviewed against the code; §1 is what that
review found, and §4, §5 and §10 carry amendments the draft did not have.

---

## 1. What the code does today

Read, not assumed. Four of these change the design, and one corrects something
this project's own notes had wrong.

**There is no authentication in the application.** `/metrics`, `/api/failures`,
`/api/compute` and `/api/visitors` return 403 from outside, and it is Apache
doing it — a `<LocationMatch>` with `Require all denied` in
`deploy/install-web.sh`, plus `ProxyPass … !` so they never reach gunicorn. The
only `Authorization` header anywhere in `app/` is the outbound Linode token in
`compute/driver.py`. An owner-facing page is therefore **net-new
authentication**, not a reuse of something already present, and it has to live
in the app, because Apache's denial is by URL and cannot know a token.

**The render path does not use the exact-match library.** `library.find` is a
dictionary lookup, as its docstring says. But `api_render` (`routes.py:843`)
resolves the requested score through `pipeline.find_package`, which walks
`settings.score_roots` itself and, failing an exact match, **falls back to a
substring match and returns the first hit** (`pipeline.py:136-149`). The worker
does the same. So a gate implemented in `library._Catalogue._scan` alone gates
the *picker* and nothing else: a visitor who knows or guesses a folder name
could render against a package the catalogue is hiding. `api_render` must move
to `library.find` as part of the gate, not after it.

**Nothing deletes a job row or an upload.** The only `unlink()` in the app is
`webside.reclaim_expired_outputs`, which removes `jobs.result` — the finished
video — 48 hours after `finished` (`webside.py:236-244`). Uploads stay under
`var/uploads/` indefinitely, which is what `status.md` records as the decision.
So a job parked for a week loses nothing, and its 48-hour window starts when it
finishes, not when it was uploaded. This is what makes parking cheap. It rests
on the open `transit/` question in `status.md`: if uploads ever become
deletable, parked ones must be exempt.

**The compute image freezes the score set.** `deploy/compute-image-build.sh`
rsyncs `/srv/vsw/scores` into the image; `compute/cloudinit.py` has no sync
step. A package published after `vsw-compute-2026-09` was captured does not
exist on a node built from it. Harmless while `COMPUTE_ENABLED=false` and the
worker shares the web box's disk; a direct contradiction of "automatic forever
after" the day it is switched on. That is §8 increment 5.

Smaller findings that shape details:

- `store.put_recognition` keeps `candidates` as `{piece_id, label, package,
  confidence, renderable}`. For an `unavailable` outcome `package` is `None`,
  so **nothing durable records which folder name to build.** The edition names
  are right there in `library.resolve`'s `editions`; adding them to the stored
  JSON is a one-line change in `_record_recognition`, and it is what makes the
  demand mail worth reading.
- `RenderTask` carries no priority and `transport._publish` is always called
  without one, so every task is published at 0. The queue declares
  `x-max-priority: 10` and nothing uses it. `jobs.priority` orders
  `store.waiting()` and the displayed queue position only — using it to
  sequence resumed work would show a position the broker does not honour.
- `_identifications` (`routes.py:522`) is in-memory. The `recognitions` table
  is the only durable copy of a verdict, so anything that waits must key off
  the table.
- `Registry.start` requires a `Style`. A visitor refused at recognition never
  reached the design screen and chose none — which is one
  reason nothing renders on a visitor's behalf.
- `limits.RULES["mail_email"]` is 4/week per address. Proof mails to the owner
  must not use it, or the fifth piece validated in a week gets no mail and
  nothing says so — the same starvation `history.md` records for the queued
  mail.
- A tar extracted straight into `/srv/vsw/scores` takes seconds, and the
  catalogue rescans every 10. A scan inside that window loads a package with
  half its bands, `package.load` accepts it, and a job started then renders
  with bands missing and no error. **The public root must only ever receive
  complete folders, by rename.**

---

## 2. The loop

1. Recognition names a piece and no edition is installed.
   `recognitions.outcome = 'unavailable'` — this already happens.
2. The owner is mailed: the piece, **the exact folder name to produce**, how
   often it has been asked, from where, and how the recordings measure against
   the 8-minute cap. Never anything the visitor typed.
3. The owner builds the package offline, in `music_line_extractor`.
4. The package lands in a **staging root** the public catalogue does not read.
5. The owner renders a proof from the admin page and watches the result.
6. He clicks Publish. The folder is renamed into the public root and is live
   within 10 seconds. Everyone who waited is emailed to come back.
7. Every later visitor who plays that piece is served with nobody involved.

Assumptions, stated so they can be falsified: the folder
`music_line_extractor` writes is named character-for-character as the pair_list
edition entry (the `library.py` docstring says so, and the single install is
consistent with it); the owner can edit `/srv/vsw/shared/.env`; and the owner
accepts watching a stranger's recording as the proof — see §10.

---

## 3. The gate: a staging root

**A package is public if and only if its folder is under
`SCORE_ROOT_DIGITAL`.** Publishing is `os.rename(staging/<name>,
scores/<name>)`.

This is chosen over the alternatives because it introduces no second source of
truth. There is no marker to contradict the disk, because there is no marker.
`library.py` does not change and its docstring — "dropping a new package on
disk makes it available" — stops being a hazard and becomes the definition of
publishing. A rename within one filesystem is atomic, so the public root only
ever sees complete folders, which is the §1 partial-extraction hazard closed as
a side effect. Withdrawing is the same rename backwards. A compute node built
by `compute-image-build.sh` rsyncs `scores/` and gets exactly the public set,
with nothing to reconcile.

**Amendment: the two roots must be on one filesystem.** `os.rename` is atomic
within a filesystem and fails with `EXDEV` across one, and a fallback that
copies would reintroduce the partial-folder window the design exists to close.
`/srv/vsw/scores` and `/srv/vsw/scores-staging` satisfy this today by accident
of layout. Make it deliberate: `bootstrap.sh` creates both, and the app checks
`os.stat(...).st_dev` on the two roots at startup and refuses to offer
publishing if they differ. A constraint the whole gate rests on should not be a
property of how the box happened to be partitioned.

Two costs, accepted:

- `pipeline.usable_packages()` must read the staging root too, so the worker
  can render a proof — and `api_render` must therefore move to `library.find`,
  or staging becomes renderable by the public through the substring fallback
  in §1. These are the same edit; neither is optional.
- `usable_packages()` deduplicates by name, so a re-engraving cannot be staged
  beside the package it replaces. Rule for now: staging refuses a name that is
  already public; re-engrave by Withdraw → upload → proof → Publish, with the
  piece unavailable in between. Publish and Withdraw both refuse while a job
  naming that score is queued or running, because renaming under a running
  render breaks its later band opens. The real fix is a `package_root` field on
  `RenderTask` — unknown fields are tolerated by `messages._load` — and it is
  not needed for the first increments.

### Rejected

**A marker file inside the package** (`.staged`, or a required `PUBLISHED`).
It travels with the folder, which is genuinely attractive. But it writes into a
package, which the README forbids for good reason; the state becomes invisible
to `ls` and has to be surfaced by `doctor.py`, `/api/library` and the admin
page; its meaning after a re-upload depends on whether the tar happened to
carry one; it still needs the `api_render` fix; and it does nothing about
partial writes, because the folder is sitting in the public root while it is
being written.

**A database table as the gate.** It can disagree with the disk in both
directions: a row saying public for a folder that has gone, which fails a
render late and confusingly, or a folder with no row, which is invisible until
registered — silently reversing today's behaviour and breaking the
tar-over-ssh fallback. The worker never opens `jobs.sqlite` by design, so a
compute node could not consult it anyway.

A table is still the right place for the **receipt**: `published(name,
published_at, proof_job, fingerprint)`, written at publish time and read by the
admin page in one query. It records *how* a package became public. The disk
stays the only truth about *what* is public.

---

## 4. The trigger

The owner is one person, on Windows, at 11pm, with a folder. The trigger lives
at **`https://chopin.weefeen.com/admin/`**.

**Amendment: the page and the file upload are two increments, not one.** The
draft justified a browser upload partly on package size, but the evidence cuts
the other way — `deployment-log.md` §6 records the one installed package
shipping as a tar over ssh at **2.8 MB gzipped**, bitmaps and the `.spj`
excluded by the owner's own rule. A file-upload endpoint that writes
client-supplied relative paths to disk is the largest new attack surface in
this design, on a site that today has no authenticated endpoint at all. So:

- **First**, the page with Wanted / Staged / Proof / Publish / Withdraw, and
  packages landing in `scores-staging/` over ssh. That delivers validation and
  the trigger, and the ssh step is one command against a directory that is
  inert by construction.
- **Then**, the upload box, if the ssh step turns out to be the annoying part.
  `<input type="file" webkitdirectory>` so there is no packaging step, with the
  page's script dropping `*.png`, `*.jpg` and `*.spj` before building the form
  and the server ignoring them again on receipt. Every relative path
  normalised, and refused if it contains `..`, is absolute, or its first
  component is not the package name. Received into
  `scores-staging/.incoming/<name>/` and renamed to `scores-staging/<name>`
  when complete — again, only complete folders by rename.

What the page shows:

- **Wanted** — `recognitions WHERE outcome='unavailable'` grouped by
  `piece_id`: title, the edition folder names to build, times asked, distinct
  uploads, countries, shortest/median/longest minutes against the 8-minute cap,
  last asked, how many people are waiting, and whether it is already staged.
- **Staged** — each folder in the staging root with `package.load()`'s verdict,
  the reference check (`reference/audio.wav` and `reference/measures.data`
  present, which is the README's contract), band count, vector or not, and a
  case-exactness check. That last one is the open item in `deployment-log.md`
  "Known, not yet addressed" — a package that works on Windows and vanishes on
  Linux — and this is the first place it becomes testable, because it is
  running on the server against a real package.
- **Public** — each folder in `scores/`, with `published_at`, the proof job,
  and renders since.

Authentication: `ADMIN_TOKEN` in `.env` only, generated with
`secrets.token_urlsafe(32)`, declared empty in both `.env.example` and
`.env.prod` so `check_linux_configuration_leaves_no_gaps` passes. **Empty means
every `/admin` route answers 404** — the same shape Apache gives the denied
endpoints, so the surface does not announce itself. Compared with
`hmac.compare_digest`. Failures counted in a new `admin_auth_ip` bucket
(10/hour) so a scanner gets a 429 rather than filling the log.

The token is pasted once and kept in `sessionStorage`, sent as
`Authorization: Bearer …` from script. Not a cookie and not HTTP Basic:
browsers attach both to cross-site requests, and `security-review.md` F7
records that nothing here checks `Origin`. A header set by script cannot be
forged cross-origin without a CORS preflight, which this app does not answer.
The mutating endpoints also check `Sec-Fetch-Site: same-origin` where the
browser sends it.

### Rejected

**GitHub Actions `workflow_dispatch`.** Packages are build products in no
repository, so the runner cannot reach the folder on the owner's PC — it would
have to be uploaded somewhere first, which is the problem this was meant to
solve. The workflow's deploy key can restart units, and a publish gesture
should not carry that authority.

**ssh/scp as the *primary* surface.** Kept as the documented path for getting a
package into staging, and it is how Op. 39 arrived. Rejected as the whole
answer because the folder name is
`Op.39_3ème Scherzo pour le Piano_(Breitkopf)__039-1-BH` — spaces, accents,
parentheses — and because it offers nowhere to see a layout check or a proof.
The validation step needs a surface; the file transfer does not.

**A watched folder over SFTP.** The ssh option with a GUI: hundreds of files
arriving over minutes with no completion signal, and still nowhere to show a
proof.

---

## 5. Amendment: the allowance is spent before the refusal

`limits.guard()` **counts on success**, not merely checks
(`limits.py:169-178`). `api_render` calls it at `routes.py:836`, and only then
looks the package up at `routes.py:841`, returning 400 when it is missing.

So today, asking to render a piece that is not installed spends one of that
person's three weekly renders and returns an error. With one score installed
this is rare enough to have gone unnoticed. Under this design "we do not have
it yet" becomes the *normal* first answer, and the people whose requests
trigger an engraving would be the ones charged for it. It also lands directly
in the new gate's path: refusing a staged package would bill the caller for the
refusal.

**Move the guard below the package resolution**, so an allowance is only spent
on a request that will actually queue. This is a small fix, it is worth making
on its own, and it belongs in increment 2 at the latest.

---

## 6. The visitor is told the truth and promised nothing

Today the page says the score is not in the library yet and suggests
something else. That stays exactly as it is. No address is taken, no job is
parked, and nobody is emailed when the piece later appears.

**Rejected: parking the job and mailing them when it is published.** This was
the draft's recommendation and the owner turned it down. The reasons are worth
keeping, because it is an attractive idea that will be proposed again:

- A promise with no date, from a one-person operation, rots. "We will email
  you when it can be made" is unbounded by construction — the engraving might
  happen tomorrow or never — and a broken promise is worse than no promise.
- It holds a personal detail for an indeterminate period against a maybe. Every
  other address in this system has a defined purpose and a defined life; this
  one would not.
- The machinery is real: a new job state, a `wanted` column, two rate-limit
  buckets, a sweep pass, and two branches in the page — for a benefit that is
  speculative.

What is genuinely lost: the person whose upload triggered an hour of engraving
never learns it happened. That is the whole cost, and it is accepted. Their
upload is kept regardless, the piece gets engraved anyway, and the next visitor
who plays it is served.

One consequence to keep in view: `recognitions` remains the only record that
anyone wanted a piece, and it is written before any of this. Nothing in §8
depends on parking.

---

## 7. The demand mail

Sent to `ALERT_EMAIL` from a new `app/demand.py`, called from
`routes._record_recognition` when the outcome is `unavailable`, on its own
thread as `_say_it_is_queued` does — the identification thread is the one the
visitor is polling, and an SMTP round-trip must not sit in front of their
answer. It must never be able to fail a recognition.

Every field, with where it comes from, so "nothing the uploader typed" can be
checked line by line:

| Field | Source |
|---|---|
| Subject: `Score wanted: Op. 25 · Etude No. 11` | `recognitions.title`, from `library._readable` |
| **the exact folder name(s) to produce** | `Edition.name` from `library.resolve`, once §1's one-line fix stores them |
| times asked, distinct uploads | `GROUP BY piece_id` on `recognitions` |
| countries | `recognitions.country`, resolved locally at upload, already classed as not personal — not city, the mail does not need it |
| shortest / median / longest minutes | `recognitions.duration`, with "over the 8-minute cap" spelled out when true |
| confidence and consensus of the latest ask | so an hour is not spent on a recognition that should not be trusted |
| a link to `/admin/` | |

Not in it: the visitor's filename, address, client address, or any field from
the `jobs` row. A check builds the body from a fixture whose name, email and
client are poison strings and asserts none of them appear — the same shape as
`check_the_courtesy_mail_cannot_starve_the_promise`.

**No batching, no bookkeeping — and the reason matters.** The draft grouped
repeat requests for a piece already reported: the first mailed at once, further
asks only after 24 hours and only if the count had grown, so a piece that went
viral produced one mail a day rather than forty. It proposed a `demand_mail`
table to track that.

The owner's answer removed the problem rather than the mechanism: **the second
person will not generate a mail, because by then the score is published.** He
engraves on the first mail, so a piece stops being `unavailable` before anyone
else asks for it. Repeats only exist inside the gap between the first request
and publication, and he intends that gap to be short.

So the rule is one line, with no state of its own:

> On an `unavailable` recognition, mail the owner.

A published piece is not `unavailable`, so silence after publication is
automatic and needs no check for it. There is no `demand_mail` table, no
timestamps, no count-has-grown comparison, and no count in the subject — a
count that reads "(1st request)" on almost every mail is noise, and in the rare
gap case two mails say the same thing a count would.

The one backstop kept is the `mail_demand` bucket at 20/day. Not to spare the
owner, who has asked for the volume, but because every mail bucket in this
system also protects the others: `history.md` records the queued notice
silently eating the ready mail when they shared one allowance, and a piece
going viral in the gap must not be able to do the same to the visitors' mail.

`unrecognised` outcomes are not demand. There is no piece to name.

---

## 8. Increments

Each is useful on its own and carries its own check in `tools/selftest.py`.

**1. The demand mail.** `app/demand.py`, `notify.send_demand`, the
`demand_mail` table, the `mail_demand` bucket, and edition names stored on the
recognition row. No visitor-facing change at all. Useful alone: the owner
learns what to engrave, and publishes by the existing tar over ssh. Checks: no
visitor words in the body; at most one mail per piece per day; nothing sent for
a staged or public piece.

**2. The staging root, the admin page, and the allowance fix.**
`SCORE_ROOT_STAGING`, `ADMIN_TOKEN`, `app/admin.py`, `app/staging.py`,
`api_render` moved to `library.find`, `usable_packages()` reading staging, the
`published` receipt, the same-filesystem startup check (§3), and the guard
reordering (§5). Packages arrive over ssh. **This is the trigger.** Checks: a
staged fixture is absent from `library.packages()` and present in
`pipeline.usable_packages()`; `api_render` refuses it *without spending an
allowance*; after publish `library.find` sees it; `/admin` is 404 with no token
configured and 401 with a wrong one; publish refuses a name in use by a live
job.

**3. The proof render.** `POST /admin/packages/<name>/proof` picks the oldest
upload whose latest recognition names this piece — there is always at least
one, since the demand mail came from it — and creates a **new** job row against
the same upload with `email=ALERT_EMAIL`, `client=''`, `meta={"proof": true}`
and a default `rnd.Style()`. The visitor's row is untouched, so their address
stays in its one place. Ready mail through a new `mail_owner` bucket (40/day,
like `mail_compute`), never `mail_email` (§1). `ledger._done` skips
`stats.record_video()` when `meta.proof` is set, or the landing page counts the
owner's tests as videos made.

**4. Waiting and inviting.** The `wait` endpoint, the `waiting` and `invited`
states, `jobs.wanted`, the `wait_ip` and `mail_invite_email` buckets,
`demand.invite_waiting()` in the sweep, the two page branches, the privacy
sentence. Checks: waiting spends no render allowance; `compute_tick` with a
waiting row still wants no machine; one invite per address-and-piece; no
visitor words in the invite.

**5. The upload box** (§4), if ssh proves annoying.

**6. Packages reach a compute node.** Publish also uploads the package to the
bucket under `scores/<name>/`, and `worker.handle_task`, on `find_package`
returning `None`, fetches it by name into its local `scores/` before failing.
Only needed when `COMPUTE_ENABLED` is about to become true — but needed
*before*, not after, because without it a node renders only what its image knew
and "automatic forever after" is false.

Order: 1, 2, 3, 4, then 5 and 6 as their triggers arrive. Increment 3 comes
before 4 because a proof needs a recording, and every refused visitor's upload
is already on disk and already linked to the piece through
`recognitions.job_id` — parking is not a prerequisite for validating.

---

## 9. Waiting on the owner

- **The proof uses a stranger's recording** — the one that triggered the
  demand. The privacy note covers it ("kept for training and masterclass use"),
  but whether to validate against someone else's upload or to upload his own
  performance for the test is his call, and it changes increment 3.
- Whether a `waiting` row should ever expire, and what the mail says if it
  does.
- Whether the `transit/` question in `status.md` is closed as "keep". Parking
  depends on it.
- Whether re-engraving a public piece with a few minutes of unavailability in
  between is acceptable, or whether `RenderTask` should get a `package_root`
  first (§3).
