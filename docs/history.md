# Decisions, and what they cost to learn

Kept because the reasoning is worth more than the outcome. Several of these
reverse something the design says, and a reader who finds only the new
answer will reasonably re-open the question.

Each entry: what was decided, and what the evidence was. Where a measurement
settled it, the measurement is here — they were expensive to take.

---

## Recognition never creates a machine — reverses decision D

`queue-design.md` put `vsw.identify` on the compute node, on a latency
argument: the web box wins "if it measures under ~90 s". **It measures
126–156 s**, which by D's own criterion argues for the node.

Reversed anyway, for two reasons D did not weigh:

- **Denial of wallet.** Recognition is unauthenticated and always will be —
  that is the product. If "what piece is this?" creates a Linode, a stranger
  with a shell loop runs up the bill from a laptop.
- **It would be slower.** Create plus boot plus torch load is ~94 s
  measured. On a node the visitor waits ~4½ minutes instead of ~2½ — for the
  *first* interaction, the one that decides whether they stay.

It also gets easier, not harder: once rendering moves off the web box it
frees exactly the memory recognition wants.

**A render request creates a machine. Nothing else does.**

---

## Email is required, and it is not a commitment signal

A render is minutes to the better part of an hour. Requiring an address is
about **delivery** — so nobody has to hold a page open — not about proving
intent. Anyone can type `a@b.com`; it defends against nothing. The defences
are `render_ip`, `render_email`, and the bot check still to come.

The interface always demanded an address; the API did not, and the API is
what a script uses.

---

## Hour-aligned teardown, then arrival-rate aware

Verified against the provider's documentation, and it contradicted the
design twice:

> *"Usage is always rounded up to the nearest hour."*

So `COMPUTE_GRACE_SECONDS=600` was not merely arbitrary, it was wasteful:
releasing a machine after ten idle minutes throws away fifty minutes already
paid for, makes the next visitor wait ~94 s for a fresh boot, and bills a
**second** hour if work arrives in the same clock hour.

Then a second correction, from the owner: at the boundary the question is
not "has it been idle long enough" but **"is more work coming"** — and that
is answerable from history rather than guessed. Keeping never saves money
(the hour a job runs in is paid either way); it only buys latency. So
**destroying is the default**, and a machine is held only where the arrival
rate for that hour of the day says it would be working anyway.

Also verified: powered-off instances are still billed, and G8 dedicated
plans lost their monthly cap on 1 July 2026. Break-even against a permanent
machine is roughly **24 renders a day**.

---

## The 25-minute cap cannot be met — the aligner is full DTW

`librosa.sequence.dtw(..., subseq=False)`: no band, no radius, so it
allocates the entire N×M cost matrix in float64. At 22050 Hz with a 512 hop
that is ~43 frames a second.

| recording | matrix | measured |
|---|---|---|
| 7.1 min | 2.51 GB predicted | **2.43 GB** |
| 14.1 min audio, same score | 4.96 GB predicted | **4.62 GB** |
| 25 min | 31 GB | — |

The model is confirmed by measurement, so the rest can be trusted. **The
8 GB node in §9.1 reaches ~11 minutes, not 25.** Reaching 25 needs a 32 GB
plan, or a banded (Sakoe-Chiba) DTW in VideoScoreSync, which would make
memory linear instead of quadratic.

One distinction that caught me out: doubling the *audio* of the same piece
grows memory **linearly**, because the score axis is fixed. A genuinely
longer *piece* grows both axes, hence quadratic. The library's own
distribution — median 3.5 minutes, 78% under 7 — means the common case is
cheap and there is one 31-minute outlier.

---

## Object storage has no cold tier, so Glacier was deferred

Linode Object Storage lifecycle rules do expiry and abandoned-upload cleanup
only. **$5/month flat for 250 GB**, which at ~148 MB kept per job is about
1,700 videos before storage costs anything extra. Archiving to AWS saves
nothing until the low terabytes and costs an account, a second set of
credentials, and a 3–5 hour retrieval.

---

## The result is confirmed stored before the job is called done

*"The render succeeded"* and *"the result is safe"* are different events,
and only the second may be announced. `put` uploads **and HEADs** before
returning; the worker announces `done` only after.

A failure to store fails the **job**, deliberately. Reporting done and
keeping the file locally produces a job that looks finished and a video that
dies with the machine — the outcome object storage exists to prevent.

`upload_file` returning without raising means the parts were accepted, not
that an object of the right size is readable. The check builds a client that
accepts a 4096-byte file and reports a 1-byte object, and fails if that
passes.

---

## Everything a job produces is kept, and it explains itself

220 KB of derived data against a 131 MB video. It costs nothing and cannot
be recreated once the machine is gone — and for a corpus it is the valuable
half.

The manifest records which score, which **engraving** (a hash — packages get
re-engraved and old bar numbers stop meaning the same bars), reference or
direct, and the torch/librosa/numpy/ffmpeg that produced the numbers.

And `recognition.json`: **whether the visitor accepted what was
recognised.** Human-verified ground truth, and a labelled error is worth
more than ten unlabelled successes.

**None of it carries personal data.** These objects outlive the job row on
purpose; a manifest holding an address would defeat deleting a visitor's
recording on request.

---

## A visitor's address lives in exactly one place

The `jobs` row. `recognitions` keeps the country and city resolved at the
time — enough for "where should the servers be", survives deletion, and is
not personal data.

Geolocation is a **local file read**. Sending addresses to a lookup service
would hand a third party a list of who used the site and when, in exchange
for two columns.

The privacy page says the address is kept for counting uploads instead of a
login, and for deciding where to put servers — the owner's own words.

---

## A private VLAN, not a public broker

The compute node must reach RabbitMQ. A public listener would put the queue
on the internet behind a password, and the firewall could not help: the
node's public address changes on every create, so any rule naming it is
wrong for a window on each one.

The VLAN is account-isolated Layer 2, free, and off the transfer quota. It
cost one reboot of the web box — done while the queue was empty and nobody
knew the URL — and compute nodes never reboot for it.

`vsw-compute` has `configure ^$`: it cannot declare, delete or reshape
anything.

---

## A restricted user, not just a scoped token

Token scopes are half the story; the other half is **which user owns the
token**. A limited user holding only `account_linode_creator` produces a
token that physically cannot see your other machines.

Verified: it lists **0** instances where the unrestricted token listed 3.

The label check in `driver.destroy` is the second guard, because grants get
widened later by people who have forgotten this code assumed otherwise.

Also settled by experiment, since the documentation does not say: a
creator-only user **does** get admin over machines it creates itself.

---

## Mistakes worth keeping

**Three defects in one day were visible on the page and green in every
check** — missing captions, a dead title animation, and a download button
that was not in the DOM. The checks verify behaviour and never look at what
is rendered. `check_the_page_is_actually_styled` closes part of that, and
its own first version was useless: it read `index.html` whole, so a class
named in a comment counted as styled, and deleting every `.delivery` rule
still passed. **Verified by doing exactly that.**

**I diagnosed the download button wrong.** I grepped the stylesheets, found
no `.delivery` rule, and added one. It had been styled all along, in a
`<style>` block the script injects. The real fault was that `watchRender`
held the box reference from the first call, so any later re-render threw it
away for good.

**Yahoo filed the first real mail as spam, and three causes were ours:** no
`Date` header, no `Message-ID` (`smtplib.send_message` adds neither), and a
body pushed to base64 by one em-dash. Then a fourth: the body was being
re-wrapped in transit, breaking the DKIM body hash. 7-bit and hard-wrapped
fixed it. Also `HELO [127.0.0.1]`, because the box's hostname is literally
`localhost`.

**The first alert could not catch what it existed for.** `align` allocates
2433 MB in 18.6 seconds; Prometheus scrapes every 30 s and the rule needed
five minutes above the line. A second rule uses `vsw_peak_rss_bytes` from
`getrusage` — exact, and independent of catching a moment.

**Adding the queued email broke the ready email.** Both drew on one bucket
of 3/week against a render allowance of 3/week, so the second render's "your
video is ready" was refused — silently. Separate buckets now, and the check
asserts the *property*: exhaust the courtesy allowance and at least as many
promise-mails must remain as the address may render.

**`rsync -L` shipped 1.8 GB of visitor videos into the compute image**,
because `var` inside a release is a symlink to the working directory.

**Pinning a `uid` on an existing Grafana datasource put it in a 50-restart
crash loop.** Grafana matches by name and refuses to change a uid;
provisioning fails and takes every dependent module with it.

**Appending to `rabbitmq.conf` took the live broker down** in two ways at
once: two listeners on the same address, and a duplicate key — which is a
parse error, not last-one-wins.

**The `epmd` fix did nothing the first time.** `ERL_EPMD_ADDRESS` in
RabbitMQ's config is ignored because epmd is socket-activated: systemd binds
the port and hands over the descriptor. The binding belongs in a drop-in on
`epmd.socket`, and the empty `ListenStream=` first is required or the new
address is *added* to the packaged one.

**The agreement field read "no package was offered"** on a job where the
visitor had rendered exactly the recognised piece — I had stored the
candidates without their `package`. Silent, plausible, and it would have
poisoned the one human-verified label this site produces.
