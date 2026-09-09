# VideoSync_webapp — queued pipeline over RabbitMQ, one web Linode, one compute singleton

Design only. Nothing is implemented. Written against `VideoSync_webapp`
at `97e253a` (`feature/recognition`); VideoScoreSync and
music_finrgerprint were read and are never modified.

**Settled by the owner, not re-argued here:** RabbitMQ; the web app's
own queues and consumers, in its own repo, on the same broker; **one
consumer process per stage**, as VideoScoreSync runs them — one
supervisord program, one log, one restart domain each; a cheap
always-on Linode for the page; **at most one** CPU-only compute Linode,
**created** when work exists and **destroyed** after a grace period;
**libx264**; **no GPU anywhere** (CPU recognition measured at 130 s cold
against 78 s on the RTX, same verdict); object storage between the
hosts; limits on **three axes** — file size, duration, and memory,
which grows with the square of duration (§1.2) — with **25 minutes and
an 8 GB instance** decided by the owner on seeing the memory table;
**debuggability as a requirement**: for any failure, which stage, what
it read, what it wrote, what it said — from a machine that may no
longer exist; **observability designed fresh** — Prometheus and Grafana
for time, memory and disk per worker, per-file time and throughput,
with the existing VideoScoreSync setup treated as a list of what not to
repeat ("I have it, but it was not working"); and **everything
deployment-related lives in this repository** — VideoScoreSync's
compose, prometheus, grafana and supervisord files stay untouched and
unreferenced; only its audio services are consumed (§15).

**Still open — the owner's call:**

| # | Decision | Section |
|---|---|---|
| D | Whether recognition runs on the compute singleton (recommended default) or on the always-on web box (better for every visitor **if** it measures under ~90 s on the small plan) | §12 |
| E | Whether recognition may become non-blocking ("we will confirm the piece by email") — a product change, laid out, not made | §12 |

Reading order if short of time: §2 (topology and diagram), §6 (state
machine), §7 (failure and diagnostics), §9 (the singleton's life), §14
(observability), §15 (file layout), §16 (migration path).

---

## 1. The cost model: three axes, measured where possible

The owner sizes the compute box on two parameters — **file size**
(attributed to the embed, ~7 min/GB on CPU) and **duration** (chroma and
alignment). A third, found by measurement after those two were written,
is **memory**: the alignment's DTW is quadratic in duration (§1.2), and
it — not cores — is what the duration cap actually buys. All three bound
what a visitor may submit; the first two are budgeted (§11.4); nothing
structural depends on which dominates the *time*. The job
table records elapsed, bytes and duration per stage so the constants
are measured, not believed. Where the evidence stands today:

| Stage | Depends on | Measured | Source |
|---|---|---|---|
| identify | neither (`MAX_WINDOWS=8`) | **78 s cold on the RTX, 130 s cold on CPU** — process start + 165 MB checkpoint + 8 windows; 27–44 s warm on the RTX | coordinator, n=1, workstation CPU |
| chroma + sync | duration — and **memory, quadratic in duration** | ~1.7 s per minute of music (13 s + 0.5 s for 7.5 min); **24 bytes per DTW cell** (§1.2) | coordinator |
| fetch | bytes | intra-region transfer, ~0.15 min/GB assumed | unmeasured |
| embed | duration **and** bytes | one job on disk: 16 MB, 640×360, 7.25 min in → 105 MB 1080p out, **356 s** wall from `sync/` creation to the mp4 (10:35:41 → 10:41:37). 7 min/GB would predict 7 s. The encoder makes a 1080p frame per performance frame whatever the input, so the duration term (~0.8 min/min here) dominates the decode/size term; the owner's 7 min/GB is that same rule seen through competition footage at ~1.4 GB per 10 min | on disk today |

```
est(ticket) = k_id·[not yet identified] + k_sync·D + k_fetch·G + k_embed_D·D + k_embed_G·G
   D = minutes of video, G = gigabytes — both known at upload (routes.py:405 already probes duration)
   defaults until ten jobs exist:  k_id = 2.2 min   k_sync = 0.03   k_fetch = 0.15   k_embed_D = 0.8   k_embed_G = 7
```

Every constant is a **default**: the CPU figures come from a
workstation, not a Linode plan; `stage_runs` (§5) replaces each with a
median once ten samples exist, and **no user-facing ETA should quote a
constant that has not been re-measured on the actual instance type**.

### 1.1 The caps — settled

```ini
MAX_DURATION_MINUTES=25         # the owner, on seeing §1.2: "let's start with 25 minutes and 8 GB". Nothing equivalent exists today.
MAX_UPLOAD_GB=4                 # 0.5 today (routes.py:28). See below.
```

**Duration = 25 min** puts a ceiling on every job — and, through §1.2,
on the instance's memory. From the one measured render (0.82 s of
processing per second of music — **n = 1, a workstation, a 640×360
source; x264 scales with cores, so the Linode plan sets this ratio and
the whole ETA rests on it; it must be re-measured on the actual instance
before any minute figure is shown to a visitor, and `stage_runs`
replaces it from the first ten jobs**):

| Stage | 25-minute job (worst case) | Typical Chopin piece, 8–15 min |
|---|---|---|
| extract audio | < 1 min | seconds |
| identify | ~2 min (duration-independent, `MAX_WINDOWS=8`) | ~2 min |
| chroma + sync | ~0.7 min (1.7 s per minute of music) | ~20 s |
| fetch | ~0.5 min at 4 GB | seconds |
| encode | ~20.5 min | 7–12 min |
| **whole job** | **~24 min** | **~10–16 min** |

**Size = 4 GB, unchanged.** The pipeline renders to a fixed 1080p canvas
whatever comes in (`render.py:52`, `ASPECTS`; the source is scaled and
cropped into it at `:509-514`), so a 4K upload spends the visitor's
upload time and our transfer on pixels that are thrown away.
Twenty-five minutes of 1080p at a generous 13 Mbit/s is 2.4 GB; 4 GB
therefore accepts anything sensibly encoded — even a 21 Mbit/s camera —
and refuses 4K (25 min at 50 Mbit/s is 9 GB), and the refusal message
can say "export at 1080p". I agree with the figure. Two consequences:
the decode/size term of the encode is bounded to a couple of minutes,
and the design sits at the **small end** — uploads can keep going
through Apache and Flask (§10.2 says what Apache needs for a 4 GB body),
and direct-to-bucket upload with resume (step 7) becomes an
optimisation for when the web host's bandwidth or staging disk is the
constraint, not a requirement.

**Is the on-demand singleton still justified with 24-minute jobs?** Not
by job length; by price. The alternative is *one always-on box with the
memory of §1.2 and the cores to encode* — an 8 GB dedicated Linode,
~$72/month — against compute billed only while it exists: $2–12/month
at "a handful a day" (§11.3). What the split costs is engineering
(steps 4–6) and a ~2-minute creation at the start of each idle gap; a
session of three typical jobs then lives ~45 min plus the grace.
Because Linode bills an instance whether busy or idle, create/destroy
is cheaper at every utilisation below ~90 %; job length never decides
it. **Ship steps 1–3 on one box, measure, then build the split.**
Nothing in steps 1–3 is discarded by it.

### 1.2 The third axis: memory, quadratic in duration — what sizes the box

`api_audio/sync.py`'s `dtw_between_chromas` calls
`librosa.sequence.dtw(x1, x2, subseq=False)` — the full cost matrix, no
band, no global constraint. `services/audio_to_chroma.py` sets
`HOP_SIZE = 2205` at 22050 Hz, i.e. **10 frames per second**, and
`sync_dtw` pads 5 s of silence at each end (+100 frames). Measured by
the coordinator with `tracemalloc`, three consistent runs at 60, 120
and 240 s (a first 30 s run read 512 B/cell and was discarded — it had
caught librosa's import allocations): **24.0 bytes per DTW cell**, i.e.
three 8-byte arrays over the matrix.

```
cells = (minutes × 600 + 100)²        peak ≈ cells × 24 bytes
```

| cap | DTW peak | smallest plan that holds it |
|---|---|---|
| 10 min | 0.89 GB | 2 GB |
| 15 min | 1.99 GB | 4 GB |
| 20 min | 3.51 GB | 8 GB |
| **25 min** | **5.47 GB** | **8 GB** |
| 31 min | 8.39 GB | 16 GB — the longest recording in the corpus |
| 40 min | 13.94 GB | 16 GB — what this document specified before the measurement |
| 60 min | 31.28 GB | 32 GB |

The corpus (249 recordings, measured): median 3.5 min = 0.12 GB, p95
12.8 min = 1.45 GB. **The owner's decision: 25 minutes and 8 GB.** It
covers every identified piece in the corpus including the longest
concerto movement (`mov_op_11_mvt1`, 23.4 min) and excludes one
unlabelled 31-minute outlier out of 249 — at half the memory of a
40-minute cap. This section previously said the worst overlap "fits an
8-core/16 GB plan" on the strength of ffmpeg and torch alone; that
figure was wrong because it did not count the DTW.

**What the plan buys, kept separate.** Linode names dedicated plans by
RAM: `g6-dedicated-8` is 8 GB with **4** dedicated cores (~$0.108/h,
$72/month). Memory decides the plan; cores decide the encode ratio.
The 0.82 s/s figure came from a workstation with more cores than four,
so the encode on this plan may run nearer 1.2–1.6 s/s — a 25-minute
piece then takes 30–40 min rather than 20. That is a throughput
question answered by measurement in step 6 (`COMPUTE_TYPE` is a knob;
the 16 GB plan has 8 cores at double the rate), not a memory one.

**Fitting 5.5 GB into 8 GB on a box that runs other things.** The
resident identify program holds torch and the checkpoint (~1.5–2 GB),
the OS and the other programs ~0.5 GB, an encode ~1 GB; with the DTW at
its peak the sum is ~9 GB. So on this plan:

- a **`heavy` lock** on the singleton (`fcntl.flock` on
  `/run/vsw/heavy.lock`), held by `embed` for the duration of the
  encode and by `sync` for the DTW. They never coincide. It costs no
  throughput: B's sync only has to finish before B's embed, which waits
  for A's embed anyway. Recognition does not take the lock — its 2 GB is
  in the baseline;
- `sync` **pre-checks before allocating**: `cells × 24 > MEMORY_BUDGET_BYTES`
  (from `duration_s` in the task) fails permanently with the honest
  message, without touching memory (§7.1);
- a **4 GB swap file** on the image as the last net — a DTW that spills a
  little into swap is slow, not dead;
- `oom_score_adj` per program in the supervisord confs: `sync` highest,
  `embed` lowest, so if the kernel must kill something it kills the
  cheap stage, not the encode;
- step 6 measures the real baseline RSS on the plan; if baseline + 5.47 GB
  leaves less than ~0.5 GB, the identify program becomes non-resident on
  this plan (20 s more per recognition) or the plan goes to 16 GB.

**The lever, for the owner — a note, not a proposal.** A banded DTW
(Sakoe-Chiba) that stores only the band makes memory *linear* in
duration and removes the cap as a constraint. It lives in the read-only
VideoScoreSync repository, so it is his change, not ours. Note that
librosa's `global_constraints=True, band_rad=…` masks the full matrix
and saves **no** memory; the implementation has to allocate the band
only (~`N × 2w` cells). Roughly: with a ±5-minute band (generous for
tempo differences between a visitor and the reference) at the same
24 B/cell, a 25-minute piece needs ~2.2 GB and an 8 GB box could take
about 80 minutes; with float32 and a single array, several times that.
Until then, 25 minutes.

---

## 2. Topology

```
 BROWSER                 WEB — the existing www.weefeen.com Linode (EU): Apache vhost chopin.weefeen.com → gunicorn   AWS S3 — Standard 48 h, then Glacier
 ───────                 ───────────────────────────────────────────────                            ──────────────────────────
 POST /api/uploads ────▶ Flask: limits.guard(upload_ip) · rights · declared size ≤ MAX_UPLOAD_GB      bucket vsw/
   (metadata only)       INSERT jobs(state=uploading) · presigned PUT urls (step 7, optional)          transit/<job>/input/<job>.<ext>
 PUT parts ─────────────────────────────────────────────────────────────────────────────────────────▶ transit/<job>/sync_data/audio.wav
 POST …/complete ──────▶ ffprobe (range reads) → duration ≤ MAX_DURATION_MINUTES, has_audio           jobs/<job>/sync_data/verdict.json
                         state=uploaded · publish vsw.extract                                          jobs/<job>/sync_data/chroma.npy
                                │                                                                      jobs/<job>/sync_data/measures.data
                         ┌──────▼──── RabbitMQ — the EXISTING broker on www, our own vhost "vsw", TLS 5671 on the VLAN address ──┐  jobs/<job>/markers/<stage>_attempt<N>
                         │  work:   vsw.extract  vsw.identify  vsw.chroma  vsw.sync  vsw.fetch  vsw.embed        │  jobs/<job>/output/<job>_PROCESSED.mp4
                         │  return: vsw.events ◀── every stage, both hosts: started / log / finished / failed     │  jobs/<job>/logs/<stage>.attempt<N>.log
                         │  dead:   vsw.dead ◀── {task, failure record} published by the failing stage,           │  logs/instances/<instance_id>/*.log
                         │                       plus DLX for anything rejected before our code ran               │
                         └──┬──────────┬───────────────┬──────────────────────────────────────────────────────────┘
                            │          │               │
   ┌── web-box programs ────┘          │               └── compute-box programs
   │  (supervisord, one log each)      │
   │  extract   app env   ffmpeg -vn -ac 1 -ar 22050 → transit/<job>/sync_data/audio.wav  (exact command kept) → vsw.identify
   │  events    app env   → jobs.sqlite: jobs · stage_runs (inputs, outputs, command, error, stderr tail) · calibration
   │                      → WORK_DIR/<job>/job.log — one narrative per job, both hosts, all stages       → vsw.notify on done / failed
   │  notify    app env   → SMTP: "queued, ready by ~HH:MM" · "ready" · "failed: why", table-gated
   │  scaler    app env   ONE process. Every 10 s: management API → messages_ready / unacknowledged per vsw.* queue.
   │                      work and no instance → CREATE (lease row + Linode label uniqueness = the singleton lock, §9.4)
   │                      nothing ready, nothing unacked, for COMPUTE_GRACE_SECONDS → SHUTDOWN (logs flush) → DESTROY → revoke
   │  GET /api/jobs/<id>/status   ◀── browser polls (2 s while identifying, 30 s after submit); no SSE
   │  GET /api/jobs/<id>/download → 302 to a 15-minute presigned GET; the web box never proxies bytes
   │  tools/queue.py show <job>  ── which stage failed, what it read, what it wrote, the command, the error (§7.6)
   │  prometheus + grafana  Docker, bound to loopback; scrape targets GENERATED from the stage registry (§14.2);
   │                        the singleton appears through a file_sd targets file the scaler writes on create, empties on destroy
   │
   │  ═══ Linode VLAN 10.0.0.0/24 (account-isolated L2) ═══  web 10.0.0.2 ◀──▶ compute 10.0.0.3 (fixed at create) ═══
   │      Cloud Firewall on both: public inbound = 22 (admin) + 80/443 (web) only. NEVER Linode's shared "private IP".

 COMPUTE LINODE — at most one; SAME REGION as www (VLANs do not cross regions); CPU only, 8 GB / 4 dedicated cores (§1.2); custom image; no secrets baked in
   supervisord: ONE PROGRAM PER STAGE — one log, one restart domain, one queue each; the stage's library imported directly
   identify   engine env  torch CPU + indexes resident   vsw.identify   audio.wav → verdict.json → S            → event identified
   chroma     engine env  audio2chroma imported          vsw.chroma     audio.wav → chroma.npy → S               → vsw.sync
   sync       engine env  wfn_combination_selector       vsw.sync       chroma + package reference → measures → S → vsw.fetch
   fetch      app env     boto3                          vsw.fetch      transit/<job>/input/<job>.<ext> → local disk         → vsw.embed
   embed      app env     cairo, ffmpeg                  vsw.embed      THE ONLY CONSUMER, prefetch 1 = one encode at a time → event done
   logship    app env     every 60 s and at shutdown: /var/log/supervisor/*.log → logs/instances/<instance_id>/
   each program serves /metrics on its own port (§14.3), scraped over the VLAN; node_exporter on 9100 for the host curves
   every attempt: PUT jobs/<job>/logs/<stage>.attempt<N>.log (full stderr, traceback, exact commands) BEFORE its finished/failed event
   a stage is done when its output is in S (HEAD); the local <stage>_done.flag only caches that fact (§6.1)
```

| Host | Runs | Because |
|---|---|---|
| www.weefeen.com (existing, EU) | Apache vhost → Flask, our vhost on the existing RabbitMQ, the job table and per-job logs, extract, events, notify, scaler, Prometheus + Grafana | Always on and already paid for; owns every promise to a visitor and every diagnostic. Extract is seconds per minute of video, makes the recogniser and the aligner consume a small file, and the box has ffmpeg duty anyway (`routes.py:405` probes there today). §15.1 says what sits beside the live Symfony site and what is capped. |
| Compute singleton | identify, chroma, sync, fetch, embed, logship | The minutes-to-hours of CPU. Exists only while there is work, plus a grace period. |
| AWS S3 | `transit/` — the recording and its audio, deleted after 48 h; `jobs/` — result, chroma, measures, verdict, params without contact details, logs; markers; shipped instance logs | The only durable bytes; the singleton's disk dies with it. Three tiers (§10.4): the singleton's disk, S3 Standard for 48 h, Glacier by lifecycle rule. |

**One process per stage, and what that buys.** The owner's two
requirements — "one at a time" and "debug quickly: which stage, inputs,
outputs, error" — are both served by the VideoScoreSync arrangement,
not by a single batch consumer:

- *The one-at-a-time guarantee survives, because it only ever mattered
  for the encode.* `vsw.embed` has exactly one consumer at prefetch 1,
  so there is never more than one ffmpeg encode on the box. Chroma
  (~13 s), sync (~0.5 s) and fetch (network) overlapping someone else's
  encode cost that encode seconds and let the next job be *ready* the
  moment the encoder is free; recognition (~1–2 min of multi-threaded
  torch) overlapping an encode slows both for that minute, which is
  nothing against an hour — and if it is measured to hurt the
  interactive step, the identify program sends `SIGSTOP` to the running
  ffmpeg for its minute and `SIGCONT` after (Linux, ten lines).
  **Memory is the one overlap that is forbidden:** a 25-minute
  alignment peaks at 5.5 GB (§1.2) and cannot share an 8 GB box with an
  encode, so `sync` and `embed` take the `heavy` lock and never run
  together — at no cost to throughput, since B's sync only has to
  precede B's embed, which waits for A's anyway. Everything else
  overlaps freely. Strict global serialisation would only add latency
  for the second visitor of an evening; I do not think it is needed and
  do not propose it.
- *Each stage runs under the interpreter that owns its library*, so
  `tools/identify_runner.py` and `tools/sync_runner.py` — which exist
  only because Flask's interpreter cannot import torch or numba — go
  away, and with them the stderr string-matching that classifies their
  deaths (§8.1). A failure becomes a Python exception with a real
  traceback in a real log.
- *Failure isolation:* an ffmpeg that takes the embed program down does
  not take chroma with it; supervisord restarts the one that died.
- *Per-stage logs, per-stage restart, per-stage metrics port* — the
  pattern the owner already operates.

The broker lives on the web box and never on the singleton: a broker
destroyed with the machine takes the queue and every in-flight job with
it.

---

## 3. Queues and workers

vhost `vsw`; default direct exchange; classic durable queues (as
VideoScoreSync declares them, `consumer_base_queue.py:44`);
`prefetch_count=1` per consumer; one supervisord program per queue.

| Queue | Program / host / interpreter | Reads (recorded as `inputs`) | Does | Writes (recorded as `outputs`) → publishes | Completion check |
|---|---|---|---|---|---|
| `vsw.extract` | `extract` / web / app env | `transit/<job>/input/<job>.<ext>` (the staged file on www; a presigned GET stream once step 7 exists) | One ffmpeg pass: `-vn -ac 1 -ar 22050 -c:a pcm_s16le`; **exact command and full stderr kept** (§7.4) | `transit/<job>/sync_data/audio.wav` → `vsw.identify` | HEAD `transit/<job>/sync_data/audio.wav` |
| `vsw.identify` | `identify` / singleton (§12 for the alternative) / **engine env**, imports `weefeen_id` directly | `transit/<job>/sync_data/audio.wav`, `duration_s`, the two indexes | Torch and indexes loaded **once at process start** — the resident "serve mode" `identify.py:17-20` wished for; `identify_aggregated`; the too-short / no-audio gates of `identify.py:160-178` as native checks | `jobs/<job>/sync_data/verdict.json` → event `identified` with the verdict | HEAD `jobs/<job>/sync_data/verdict.json` |
| `vsw.chroma` | `chroma` / singleton / **engine env**, imports `services.audio_to_chroma` from the read-only VideoScoreSync checkout | `transit/<job>/sync_data/audio.wav` | `audio2chroma`; a **startup self-test** on a bundled 1-second WAV so a mis-built env fails at program start, not on a visitor's job (§8.1) | `jobs/<job>/sync_data/chroma.npy` → `vsw.sync` | HEAD `jobs/<job>/sync_data/chroma.npy` |
| `vsw.sync` | `sync` / singleton / **engine env**, imports `services.audio_synchronization_service` | `jobs/<job>/sync_data/chroma.npy`, the package's `performance/chroma.npy` + `measures.data` | `wfn_combination_selector`; the column translation and the crowding / span checks exactly as `tools/sync_runner.py:95-210` and `app/sync.py:163-180`, now raising `PartialRecording` natively. **A partial recording fails here, permanently, before gigabytes are fetched.** | `jobs/<job>/sync_data/measures.data` → `vsw.fetch` | HEAD `jobs/<job>/sync_data/measures.data` |
| `vsw.fetch` | `fetch` / singleton / app env | `transit/<job>/input/<job>.<ext>`, `upload_bytes` | `download_with_retry` shape of `consumer_download_video_queue.py:39-57`; verify size; skip if present at that size | `input/<job_id>.<ext>` (local; size recorded) → `vsw.embed` | local file at the right size |
| `vsw.embed` | `embed` / singleton / app env (cairo) — **the only consumer of this queue** | `input/<job_id>.<ext>`, `jobs/<job>/sync_data/measures.data`, the package's `lines/*`, `export.json`, `style`, `meta` | `render.render()` unchanged in substance; **three commands** (ffprobe, band strip, main encode) each kept verbatim with their stderr (§7.4); ffmpeg writes `output/<job_id>.attempt<N>.part.mp4`, Python renames on exit 0 | `jobs/<job>/output/<job>_PROCESSED.mp4` → event `done` with key, bytes, elapsed | HEAD `jobs/<job>/output/<job>_PROCESSED.mp4` |
| `vsw.events` | `events` / web / app env | — | Applies `started / log / finished / failed` events to `jobs`, `stage_runs`, `calibration`; appends narrative lines to `WORK_DIR/<job>/job.log`; on `embed.done` / `failed(permanent)` publishes `vsw.notify`; recomputes positions and ETAs (§11.2) | `vsw.notify` | upsert on `(job_id, stage, attempt)` |
| `vsw.notify` | `notify` / web / app env | `job_id`, `kind ∈ {queued, ready, failed}` | `notify.send_*` gated by `mail_<kind>_at IS NULL` (§13.2) | — | the table |
| `vsw.dead` | read by `tools/queue.py` | — | `{task, failure}` records published by the failing stage before it acks (§7.5), plus raw DLX rejects with `x-death` headers | — | — |

`identify.py:43`'s semaphore is retired: one identify program with
prefetch 1 is the same guarantee, across machines.

### 3.1 What is copied from VideoScoreSync — verified

| File | Verdict |
|---|---|
| `workers/consumer_base_queue.py` (92 lines), `workers/publisher_base_queue.py` (73) | **Copy, then change** (§8). Importing is impossible in practice: both `import config`, and `config.py:128-136` does `int(os.getenv("PORT_PREPROCESSOR"))` with no default, so the import needs VideoScoreSync's whole `.env` in our process. They also connect as guest (`consumer_base_queue.py:35-42`) and publish without confirms (`publisher_base_queue.py:48-49`). |
| `consumer_extract_audio_queue.py`, `_resample_audio_queue.py`, `_generate_chroma_queue.py`, `_sync_video_queue.py` | **Skeleton yes — one program, one queue, one library call — and the bodies already exist here.** Resample is `ffmpeg -y -i IN -ar 22050 OUT` (`:50-55`); chroma is one `audio2chroma` call (`:49`); sync is one `wfn_combination_selector` call plus a write (`:72-83`); extract re-encodes to AAC m4a (`services/audio_extraction_service.py:173-186`). `tools/sync_runner.py` already calls both services with the column translation and the partial-recording checks; that code moves into the two engine-env programs and the runner is deleted. |
| `consumer_download_video_queue.py` | **Shape yes, body no.** Keep `download_with_retry` (`:39-57`) and the done-flag idea (`:78-82`); the body becomes a bucket GET. `:84-88` **acks** a message with a missing `score_id` and counts a failure — the job vanishes and nobody is told; §7 replaces that. |
| `consumer_embed_score_queue.py` | **Not reused**, as the owner said: `ScoreVideoMaker(task)` is shaped for the Dropbox flow and knows nothing of ink colour, crop offset, panel or background; `app/render.py` is ours. |
| `helpers/task_utils.py::get_logger_for_task` (`:25-48`) | **The idea, not the mechanism.** A logger keyed by job id is right; a `FileHandler` on the local disk is wrong here because the disk is on the wrong host and is destroyed (§7.3). |
| `services/worker_metrics.py` | **The idea, not the file.** `observe_task_duration_per_gb` (`:107-124`) is one axis; we record elapsed, bytes **and** duration per stage (§5). `start_metrics_server(port)` per program is the pattern to re-add later, with `DISABLE_METRICS=1` until then; the file itself needs `psutil` and `PROMETHEUS_MULTIPROC_DIR`. |
| `models/task_data.py`, `helpers/task_utils.py::get_job_paths` | **Replaced** (§4): 19 competition fields; ~15 `config.*` constants for a layout that is not ours. |
| identify program | **New.** ~80 lines around the logic of `tools/identify_runner.py`, resident, under the engine interpreter. |

Not inherited: `basic_nack(requeue=False)` on every exception (a
network blip and a missing audio track treated alike — §7), the
done-flag honoured only `if method.redelivered` (§6.2), and failure
records that consist of one log line.

### 3.2 The canonical audio — produced once, consumed twice

```
ffmpeg -i <upload> -vn -ac 1 -ar 22050 -c:a pcm_s16le audio.wav
```

**22050 Hz, matching VideoScoreSync, not optional:** `audio_to_chroma.py:30-31`
defines the 0.1 s hop as `HOP_SIZE = 2205` at `AUDIO_REF_FREQ = 22050`;
the alignment is calibrated on it; music_finrgerprint's docstring says
the same (`pipeline.py:8`). The recogniser's transcription model
resamples to `AMT_SAMPLE_RATE = 16000` (`features/amt_pitch.py:28`, via
`librosa.load(sr=…)` at `aggregate.py:138`), so 22050 loses it nothing.
Both loaders take mono (`aggregate.py:138`, `api_audio/chroma.py:372`);
the downmix happens once. VideoScoreSync's AAC intermediate is not
matched: its m4a becomes the final audio track there; here that track
is mapped from the original video (`render.py:547`).

Today `identify.py:210` and `sync_runner.py:148` each hand the **video**
to their library, which decodes it independently. 2.6 MB per minute;
a retry of recognition or chroma never touches the video again.

---

## 4. Task payload and path map

### 4.1 `app/queue/task.py`

One flat, JSON-serialisable record. VideoScoreSync's `TaskData` nests a
`JobParams` that is a dataclass in one consumer and a dict in another
(`consumer_download_video_queue.py:67` vs `consumer_sync_video_queue.py:66`).

```python
@dataclasses.dataclass(frozen=True)
class Task:
    job_id: str
    stage: str                       # extract | identify | chroma | sync | fetch | embed
    attempt: int = 1                 # bumped by the consumer that republishes a transient failure
    upload_key: str | None = None    # transit/<job>/input/<job>.<ext>
    upload_bytes: int | None = None
    upload_ext: str | None = None
    duration_s: float | None = None  # ffprobe at upload; one ETA axis
    audio_key: str | None = None     # transit/<job>/sync_data/audio.wav
    package: str | None = None       # score package folder name, as library.py keys it
    mode: str | None = None          # "reference" (pipeline.MODES)
    style: dict | None = None        # dataclasses.asdict(render.Style)
    meta: dict | None = None         # title-panel fields; visitor-typed, stays inside the job
    submitted_at: float | None = None

    def next(self, stage: str, **more) -> "Task": ...   # same job, next stage, attempt=1
    def to_json(self) -> bytes: ...
    @classmethod
    def from_json(cls, body: bytes) -> "Task": ...
```

`style` and `meta` travel in the message because today they exist only
as arguments to `Registry.start()` (`jobs.py:143-144`). Everything a
stage needs to be **re-run** is either in the `Task` or in the bucket,
which is what makes replay (§7.5) a one-line publish.

### 4.2 The job tree — VideoScoreSync's, exactly, plus one file

The owner: *"I want to keep the same job project structure that we have
in VideoScoreSync."* From `config.py:311-360` and
`helpers/folders_utils.py:57-72`, the canonical tree, and what this app
puts in each slot:

```
<WORK_DIR>/<job_id>/
    input/          <job_id>.<ext>              the recording — named by job id, never by the visitor's file name
                    job_params.json             the Task: package, mode, style, meta, duration, bytes, sha256 of the
                                                recording — NO email address, NO visitor address (those stay in jobs.sqlite)
                    video_params.json           ffprobe's answer (VideoScoreSync's VIDEO_PARAMS)
    sync_data/      audio.wav                   the canonical 22050 Hz mono audio (§3.2)   AUDIO_FOR_SYNC_FILE_PATH
                    chroma.npy                                                              CHROMA_FILE_PATH
                    measures.data                                                           MEASURES_DATA_FILE_PATH
                    verdict.json                THE ONE ADDITION: the recogniser's decision; VideoScoreSync has no equivalent
    output/         <job_id>_PROCESSED.<ext>    the result (PROCESSED_EXTENSION)
                    <job_id>.attempt<N>.part.<ext>   ffmpeg's target until Python renames it on exit 0
    logs/           <stage>.attempt<N>.log      our addition: the per-attempt logs of §7.3
    static_pages/   present for parity, unused: VideoScoreSync's Cliburn title pages (static_info.json)
                    output/hq_audio.m4a and silent_<job_id>.<ext> are likewise not produced here (§3.2)
```

`app/queue/paths.py` copies the constants by name — `INPUT_FOLDER`,
`OUTPUT_FOLDER`, `SYNC_DATA_FOLDER`, `STATIC_PAGES_FOLDER`,
`PROCESSED_EXTENSION`, `JOB_PARAMS_FILE_PATH`, `AUDIO_FOR_SYNC_FILE_PATH`,
`CHROMA_FILE_PATH`, `MEASURES_DATA_FILE_PATH` — so a person who knows one
tree knows the other. **One directory is the whole job**, on either
host; `shutil.rmtree(job_dir)` is the whole cleanup.

**The defect this fixes.** Today an upload lands in a *shared*
`work_dir/uploads/<job_id>.mp4` (`settings.py:216`, `routes.py:399-402`)
while the job's own folder is `work_dir/<job_id>` (`pipeline.py:84-85`)
holding `sync/<package>/` and the output loose at the top. A job's
files live in two places, which is why `pipeline.cleanup()`
(`pipeline.py:171-173`) is dead code — nothing calls it — and why
980 MB has accumulated from testing alone. Step 1 adopts the tree and
calls the cleanup.

```python
@dataclasses.dataclass(frozen=True)
class JobPaths:                                   # WORK_DIR/<job_id>/ on either host, the tree above
    dir: Path
    video: Path                                   # input/<job_id>.<ext>
    params: Path                                  # input/job_params.json
    audio: Path; chroma: Path; measures: Path; verdict: Path      # sync_data/…
    result: Path                                  # output/<job_id>_PROCESSED.<ext>
    def part(self, attempt: int) -> Path          # output/<job_id>.attempt<N>.part.<ext>
    def flag(self, stage: str) -> Path            # <stage>_done.flag — a cache of "the output is in S3"
    def log(self, stage: str, attempt: int) -> Path   # logs/<stage>.attempt<N>.log

def keys(job_id: str) -> Keys:                    # the bucket mirrors the tree, under two prefixes (§10.4)
    transit/<job_id>/input/<job_id>.<ext>         # DISCARDED after the job: the visitor's recording
    transit/<job_id>/sync_data/audio.wav          # DISCARDED: their raw performance audio
    jobs/<job_id>/input/job_params.json           # KEPT (no contact details in it)
    jobs/<job_id>/sync_data/{chroma.npy, measures.data, verdict.json}     # KEPT: a re-render needs no re-alignment
    jobs/<job_id>/output/<job_id>_PROCESSED.<ext> # KEPT: the video, live 48 h, then Glacier
    jobs/<job_id>/logs/<stage>.attempt<N>.log     # KEPT: diagnostics
    jobs/<job_id>/markers/<stage>_attempt<N>      # zero-byte attempt markers (§6.3)
    logs/instances/<instance_id>/<program>.log    # shipped supervisord logs (§7.7)
```

The score library must exist on the singleton (sync reads
`performance/chroma.npy` + `measures.data`; embed reads `lines/*.svg`,
`export.json`): baked into the image, refreshed from `scores/` in the
bucket at start.

---

## 5. The job table — companion to the broker, and the diagnostic record

`WORK_DIR/jobs.sqlite` on the web box, WAL, `busy_timeout=5000`,
stdlib `sqlite3`. Several processes on the web box write it (Flask,
events, notify, scaler) — the case where per-job JSON files stop being
enough.

```sql
CREATE TABLE jobs (
  id TEXT PRIMARY KEY, visitor TEXT, email TEXT, name TEXT,
  upload_key TEXT, upload_bytes INTEGER, upload_ext TEXT, duration_s REAL, has_audio INTEGER,
  package TEXT, mode TEXT, style TEXT, meta TEXT,               -- JSON
  state TEXT, stage TEXT, attempt INTEGER, error TEXT, error_kind TEXT,
  verdict TEXT, result_key TEXT, result_bytes INTEGER,
  rank INTEGER, position INTEGER, est_minutes REAL, eta_at REAL, -- §11.2, recomputed by the events consumer
  created REAL, uploaded REAL, submitted REAL, finished REAL,
  mail_state TEXT, mail_queued_at REAL, mail_ready_at REAL, mail_failed_at REAL
);

CREATE TABLE stage_runs (                       -- one row per attempt of a stage: the answer to "what happened"
  job_id TEXT, stage TEXT, attempt INTEGER,
  host TEXT,                                    -- web | compute
  instance_id TEXT,                             -- Linode id; where logs/instances/<id>/ came from
  pid INTEGER,
  state TEXT,                                   -- started | done | failed | interrupted
  queued_at REAL,                               -- when the message was published: queue wait = started − queued_at
  started REAL, ended REAL, elapsed_s REAL,
  inputs TEXT,                                  -- JSON: what it read — bucket keys / local paths with byte sizes and mtimes
  outputs TEXT,                                 -- JSON: what it wrote — keys / paths with byte sizes
  bytes_in INTEGER, bytes_out INTEGER,          -- the size axis: GB/s = bytes_in / elapsed for byte-bound stages (§14.7)
  media_s REAL,                                 -- the duration axis: realtime factor = media_s / elapsed for encode-like stages
  peak_rss_mb REAL, cpu_s REAL,                 -- this PROCESS and its children (psutil.Process), never the whole host (§14.5)
  disk_read_mb REAL, disk_write_mb REAL,        -- psutil.Process().io_counters() deltas over the attempt
  commands TEXT,                                -- JSON list of {argv (shlex-joined), returncode, elapsed_s, stderr_tail}
                                                --   for every subprocess the attempt ran; NULL for pure-Python stages
  error_kind TEXT,                              -- transient | permanent | config | exhausted | interrupted
  error_class TEXT,                             -- e.g. app.sync.PartialRecording, subprocess.CalledProcessError
  error_message TEXT,                           -- the user-safe message, or the exception's str()
  stderr_tail TEXT,                             -- last 200 lines of the failing child's stderr, or the full traceback
  log_key TEXT,                                 -- jobs/<job>/logs/<stage>.attempt<N>.log in the bucket (the whole story)
  PRIMARY KEY (job_id, stage, attempt)
);

CREATE TABLE calibration (stage TEXT PRIMARY KEY, k0 REAL, k_d REAL, k_g REAL, samples INTEGER, updated REAL);

CREATE TABLE compute (                          -- the scaler's lease (§9.4); at most one live row
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  state TEXT,                                   -- creating | running | draining | destroying
  instance_id TEXT, vlan_ip TEXT, created REAL, ready REAL, idle_since REAL,
  broker_user TEXT, store_key_id TEXT, lease_until REAL
);
```

Beside the table, `WORK_DIR/<job>/job.log` on the web box is the
**narrative**: one file per job, both hosts, all stages, in arrival
order, each line stamped with its origin (`2026-09-09 10:35:41 compute
li-4821 sync a1 INFO 412 measures placed`). It is written only by the
events consumer (§7.3).

`Job` / `Registry` (`app/jobs.py`) become a thin layer over this;
`Job.public()` keeps its shape (both front-ends read it) and gains
`position`, `eta_at`, `attempt`, `compute` (`none | creating | running`).
`rehydrate()` becomes a `SELECT`.

**Who owns what:**

| Question | Owner | Never asked of |
|---|---|---|
| What should happen next for job J? | **the broker** — a message in `vsw.X` is an obligation to make stage X happen | the table |
| Has stage X already happened? | **the bucket** — HEAD on the stage's output; the local flag is a cache | the table (singleton programs never read it) |
| Where are the bytes? | **the bucket** | local disks |
| What state is J in, where in line, when ready, was it mailed? | **the table**, derived from `vsw.events` | the broker |
| What did stage X of J read, write, run and say, on attempt N? | **`stage_runs`** for the record, **`jobs/<job>/logs/…` in the bucket** for the whole story, **`job.log`** for the narrative | the singleton's disk |
| Does a compute instance exist? | **the Linode API** (label `vsw-compute`); the `compute` row is the lease for *intent* | — |

The table may lag by the latency of `vsw.events`; it never decides
whether to work. If an event is lost (§8.4 turns on confirms, so it
should not be), a job shows stuck in a stage; `tools/queue.py`
reconciles against the bucket and the queues, and the operator
republishes.

---

## 6. State machine

### 6.1 Job states

```
uploading ─▶ uploaded ─▶ extracting ─▶ identifying ─▶ identified | unrecognised | unavailable
                                                            │  visitor picks score, style, email; POST /render
                                                            ▼
                         queued ─▶ running:chroma ─▶ running:sync ─▶ running:fetch ─▶ running:embed ─▶ done ─▶ notified
                                                                                                   │
        failed(kind = permanent | config | exhausted | interrupted) ◀────────────── any running stage
        abandoned ◀── uploaded/identified with no submit within UPLOAD_TTL_DAYS
```

```mermaid
stateDiagram-v2
    [*] --> uploading: POST /api/uploads
    uploading --> uploaded: POST …/complete (ffprobe ok, both caps ok)
    uploaded --> extracting: vsw.extract consumed (web box)
    extracting --> identifying: audio.wav in S; vsw.identify published
    identifying --> identified: verdict matched
    identifying --> unrecognised: verdict not confident
    identified --> queued: POST /render (both budgets ok)
    unrecognised --> queued: POST /render with a hand-picked score
    queued --> running: vsw.chroma consumed (singleton)
    running --> running: chroma → sync → fetch → embed
    running --> queued: transient failure, attempt < cap (republished after RETRY_DELAY_SECONDS)
    running --> failed: permanent / config / cap reached
    running --> done: jobs/<job>/output/… in S3, confirmed; event done
    done --> notified: mail_ready_at set
    failed --> notified: mail_failed_at set
    uploaded --> abandoned: no submit within UPLOAD_TTL_DAYS
```

One protocol for every stage — the diagnostic record is part of it,
not an afterthought:

```
on message (task):
    if output_in_bucket(stage) or flag(stage).exists():     # every message, not only redelivered ones — §6.2
        publish next(task); ack; return                     # safe: the next stage has the same guard
    n = count_markers_in_bucket(stage) + 1                  # crash-attempt counting that survives destroy — §6.3
    if n > 1 and no stage_run(stage, n−1) ended:            # the previous attempt died without reporting
        event interrupted(stage, n−1, "process died; see logs/instances/<id>/")
    PUT jobs/<job>/markers/<stage>_attempt<n>
    if n > cap(stage): publish dead{task, failure(exhausted)}; event failed(exhausted); ack; return
    open per-attempt log  logs/<stage>.attempt<n>.log       # everything below is written to it
    event started(inputs = what will be read, with sizes)
    do the work locally — every subprocess through shell.run(), which records argv, rc, elapsed, stderr (§7.4)
    PUT outputs to the bucket                               # the point of no return
    PUT the per-attempt log
    touch flag(stage)
    publish next(task) | event done/identified
    event finished(outputs with sizes, commands, elapsed, bytes_in, bytes_out, media_s, rss/cpu/io of this process, log_key)
    ack
on exception:
    classify (§7.1); PUT the per-attempt log (with traceback)
    transient and n < cap → sleep; republish task(attempt=n+1); ack
    else → publish dead{task, failure}; event failed(kind, class, message, stderr_tail, commands, log_key); ack
```

### 6.2 The ordering fix

`consumer_embed_score_queue.py:68-80` publishes the next stage **then**
touches `embed_done.flag`: a crash between the two re-runs the hour and
publishes downstream twice. Reversing the order alone turns the same
crash into a *lost* downstream message, because `:45` honours the flag
only `if method.redelivered` and then acks without re-publishing.

Both windows close with two changes: **check for completion on every
message**, and **when already complete, re-publish the next stage before
acking**. A duplicate downstream message is absorbed by the downstream's
own unconditional check; a lost one is re-created by the redelivery.
Order: outputs → bucket → flag → publish → ack. With one consumer per
queue at prefetch 1, a duplicate that arrives during the work simply
waits and then finds the output.

### 6.3 Crash matrix — including the singleton being destroyed under the job

"Crash" = the process dies without raising (kill, OOM, the instance is
destroyed). A raised exception is a *failure*, §7.

| State | Crash of | Durable | Recovery |
|---|---|---|---|
| uploading | web box | partial multipart in the bucket | Multipart survives the web box; the browser resumes (§10.2); `abort-incomplete` rule after 1 day. |
| uploaded / extracting | web box | upload; maybe `audio.wav` | Durable message; on restart the consumer HEADs `audio.wav` and skips or redoes. |
| identifying | singleton destroyed | audio | Heartbeat requeues the unacked message within ~2 min (§8.2); the scaler's tick sees `ready > 0`, creates a new instance; re-run. The page keeps polling; copy says "still listening". |
| queued | anything | messages in `vsw.chroma` | Nothing ran. Position recomputed. |
| running: chroma / sync / fetch | one program dies, or the singleton is destroyed | outputs of finished stages | Program death: supervisord restarts it, the broker requeues on the dropped connection, the next attempt records `interrupted` for the last one. Destroy: new instance; finished stages skipped by HEAD; fetch re-downloads (the disk is gone). |
| **running: embed, ffmpeg dies with the box** | singleton destroyed | `chroma.npy`, `measures.data`, markers | New instance: chroma/sync skipped by HEAD, fetch redone (k_fetch·G), **embed re-run from the encode only**; marker `embed_attempt2` in the bucket; a partial `.part` never became a result because only Python renames, only on exit 0. The half-written per-attempt log of attempt 1 is on the dead disk; the shipped supervisord log (§7.7) has its last 60 s. |
| running: embed, ffmpeg dies but the program lives | — | as above | A failure → §7, with the exact command and stderr. |
| done, mail not sent | web box | result; `mail_ready_at NULL` | `vsw.notify` durable; sent once on restart (§13.2). |
| done, mail `sending` | web box | `mail_state='sending'` | **Not resent**; `uncertain`, WARNING. A duplicate to a stranger's typed address is worse than a missing one when the page shows the link. |

Attempt markers live in the **bucket** because create/destroy erases the
disk: a redelivered message carries the same `attempt` as before, and
the consumer counts markers rather than trusting the payload.

---

## 7. Failure policy and diagnostics

The owner's requirement, verbatim: *"sometimes some issues happens at
one stage. We need to be able to debug very quickly. Which stage fails,
what is the inputs and outputs and error message."* This section is
that feature. Everything in it survives the destruction of the machine
that produced it (§7.7).

### 7.1 Classification

| Kind | Examples (all already produce user-safe messages) | Action |
|---|---|---|
| **permanent** | no audio (`identify.py:163`), too short (`:175`), partial recording (`sync.py:168-180`), no measures (`sync_runner.py:159`), "the alignment doesn't cover this video" (`render.py:426`), unknown package, ffmpeg rejecting the input | dead record + event `failed(permanent)`; ack; the visitor is **mailed why** (§13.2) |
| **config** | an import that fails at program start, a missing index, the SVML abort (`sync.py:210-214`) — now caught by the startup self-test (§8.1), so it never reaches a job | program exits non-zero at start; supervisord shows `FATAL` after its retries; `tools/queue.py compute` shows it; nothing is consumed, nothing is lost |
| **permanent — memory, deterministic** | the alignment's DTW needs `(D×600+100)² × 24` bytes (§1.2). Caught **before allocation** by `sync`'s pre-check against `MEMORY_BUDGET_BYTES`; if the estimate was wrong and it still fails, `MemoryError` in the program, or the OOM-killer taking it (seen by the next attempt as `interrupted`, on `sync`, with `dmesg`'s line in the shipped log) | permanent on the first `MemoryError`; on an OOM-kill of `sync` the next attempt re-runs the pre-check with a 20 % tighter budget and fails permanently rather than dying again. Retrying identically cannot succeed. Message to the visitor: "This recording is N minutes long, and aligning it needs more memory than our machine has; the limit is 25 minutes." Also mailed. |
| **transient** | bucket GET/PUT errors, broker publish errors, `RENDER_TIMEOUT`, **ffmpeg killed by signal / exit 137 / "Cannot allocate memory"** — ffmpeg's own need is bounded (~1 GB at 1080p) and constant per resolution, so an OOM there is memory pressure from something *else* on the box, which a retry after the lock is released will not see | `sleep(RETRY_DELAY_SECONDS)`, republish with `attempt+1`, ack the original; at the cap → `exhausted` |
| **interrupted** | the previous attempt's program died without reporting (kill, OOM, native abort, instance destroyed) — detected by the next attempt from the attempt markers | recorded against the dead attempt with a pointer to the shipped instance log; counted toward the cap like any attempt |

```ini
RETRY_DELAY_SECONDS=60
MAX_ATTEMPTS_CHEAP=3        # extract, identify, chroma, sync, fetch
MAX_ATTEMPTS_EMBED=2        # one retry — and with §6.3 that retry is the encode only, never the alignment
```

A cap of three on an hour-long render is indefensible only if a retry
repeats the hour of *everything*. It does not: chroma and sync are kept
in the bucket, so an embed retry is the encode (plus a re-fetch if the
instance was destroyed). Two attempts is right for that — a second
identical failure is a signal, and the signal now comes with the
command that failed. ffmpeg can resume nothing; segmented encoding is
not proposed (§17).

| Situation | Visitor | Operator |
|---|---|---|
| transient, retry scheduled | `/status`: `queued`, "Something went wrong on our side; trying again in a minute (attempt 2 of 2)"; no mail | WARNING in the program log; `tools/queue.py show <job>` lists the attempt with its error |
| permanent | `failed` + the stage's message; **a mail** saying the same, because they were told to walk away | INFO; the dead record in `vsw.dead`; `show <job>` |
| config | never reaches a visitor | ERROR at program start; `compute` view |
| exhausted | "The render failed twice and has been stopped. Your upload is kept for N days." | ERROR; the dead record; `show <job>` prints both attempts side by side |

### 7.2 The failure record — what every attempt writes

Every attempt, successful or not, produces one `stage_runs` row (§5)
through `started` / `finished` / `failed` events. What goes into
`inputs` and `outputs` is fixed per stage so the operator always knows
what to expect:

| Stage | `inputs` | `outputs` | `commands` |
|---|---|---|---|
| extract | `{"upload": key or path, "bytes": …, "duration_s": …}` | `{"audio": "transit/<job>/sync_data/audio.wav", "bytes": …, "seconds": …}` | 1 × ffmpeg |
| identify | `{"audio": key, "bytes": …, "duration_s": …, "pitch_index": path+mtime, "chord_index": path+mtime}` | `{"verdict": key, "mode": …, "winner": …, "consensus": …, "n_windows": …}` | none |
| chroma | `{"audio": key, "bytes": …}` | `{"chroma": key, "frames": …, "bytes": …}` | none |
| sync | `{"chroma": key, "package": name, "ref_chroma": path+frames, "ref_measures": path+count}` | `{"measures": key, "measures": …, "first": …, "last": …, "crowding": …, "span_ratio": …}` | none |
| fetch | `{"upload": key, "bytes": …}` | `{"video": local path, "bytes": …}` | none |
| embed | `{"video": path+bytes, "measures": key, "package": name, "bands": count, "style": {...}, "meta_keys": [...]}` | `{"result": key, "bytes": …, "part": path}` | 3 × (ffprobe, ffmpeg strip, ffmpeg encode) |

`error_class` is the Python class (`app.sync.PartialRecording`,
`subprocess.CalledProcessError`, `botocore.exceptions.ClientError`);
`error_message` is what the visitor may be shown; `stderr_tail` is the
last 200 lines of the failing child's stderr, or the full traceback for
a native exception. `log_key` points at the complete per-attempt log.

### 7.3 One story per job, across stages and both hosts

VideoScoreSync's `get_logger_for_task` (`helpers/task_utils.py:25-48`)
gives each job a logger with a `FileHandler` at
`<job>/<job>.log`. The idea is right — a logger keyed by job — and the
mechanism is wrong for two hosts and a disposable disk: the file would
be on the singleton, and gone with it. Two tiers instead:

1. **The narrative** — INFO and above, a few dozen lines per job
   ("sync a1 started: chroma 4132 frames, reference 288 measures";
   "embed a1: ffmpeg encode `…` (rc 0, 5m52s)"; "embed a1 failed:
   RenderError …"). Each program's `JobLogger(job_id, stage, attempt)`
   emits these as `log` events on `vsw.events`; the events consumer
   appends them to `WORK_DIR/<job>/job.log` on the web box. One file,
   both hosts, all stages, arrival order, origin-stamped.
2. **The whole story** — the same logger's DEBUG stream, the complete
   stderr of every subprocess, and tracebacks, written to the local
   per-attempt file `logs/<stage>.attempt<N>.log` and **PUT to the bucket
   at `jobs/<job>/logs/<stage>.attempt<N>.log` before the attempt's
   `finished` or `failed` event** (§6.1). Sizes are small — an hour of
   ffmpeg stderr with `-nostats -loglevel warning` is kilobytes; the
   progress stream goes to `-progress` on a separate local file and is
   not shipped.

The narrative answers "what happened to this job" in one `cat`; the
per-attempt log answers "show me everything" for one stage; both exist
after the singleton is gone.

### 7.4 Exact commands — the one artefact worth the most

For every stage that shells out, the record must let the owner paste
the command into a terminal on the same image and watch it fail. Today
`render._run` (`render.py:412-417`) keeps **six lines of stderr and
discards `cmd` entirely**. The filter graph it ran (`render.py:503-536`)
is hundreds of characters assembled from the visitor's aspect, crop
offset, colours, opacity, band position and panel choices; without the
command a failed render is not reproducible, and with it the owner can
re-run it on the same image in a minute. **Preserving that command line
is the single highest-value debugging change in the codebase**, and it
lands in step 1. `probe` (`:271-282`) raises on a failed ffprobe with no
stderr at all; `extract` does not exist yet. One helper replaces them:

```python
# app/workers/shell.py
def run(argv: list[str], *, log: JobLogger, timeout: float | None,
        cwd: Path | None = None, stderr_file: Path | None = None) -> Completed:
    """Run one command, keep everything a person needs to run it again."""
    log.info("$ %s", shlex.join(argv))                 # the reproducible line, in the narrative AND the attempt log
    ... Popen; stderr tee'd to stderr_file and to a 200-line ring buffer; wall time measured ...
    completed = Completed(argv=argv, returncode=rc, elapsed_s=…, stderr_tail=ring.text(), stderr_path=stderr_file)
    log.attempt.commands.append(completed.record())   # lands in stage_runs.commands
    if rc != 0: raise CommandFailed(completed)         # carries all of the above; classify() reads rc and the tail
    return completed
```

`RenderError` and the new `ExtractError` wrap `CommandFailed` and
expose `command`, `returncode`, `stderr_tail`, `stderr_path`. Three
commands in embed (ffprobe, the band strip, the encode) and one in
extract are recorded whether they succeed or fail, so a *slow* encode
is as inspectable as a failed one. `shlex.join` produces a POSIX-quoted
line; on the Windows dev box the same record is printed with
`subprocess.list2cmdline` by `tools/queue.py --windows`.

### 7.5 Dead tickets — inspectable and replayable

Two things arrive in `vsw.dead`:

- **Our records.** A failing stage does not merely nack; it publishes
  `{"task": <Task as sent>, "failure": <the stage_runs row>, "job": {upload_key, audio_key, package, style, meta}}`
  to `vsw.dead` and then acks the original. The dead message is
  self-contained: the exact payload that ran, what it read, what it
  wrote before failing, the command, the error, and the key of the full
  log.
- **Raw rejects** via DLX (`x-dead-letter-exchange: vsw.dlx`) for
  anything rejected before our code ran — an unparseable body, a
  program crashing in `from_json`. They carry RabbitMQ's `x-death`
  headers and the original body.

Replay, after the bug is fixed:

```
tools/queue.py replay <job> <stage> [--invalidate-downstream] [--set key=value ...]
```

rebuilds the `Task` from the `jobs` row (everything a stage needs is
there or in the bucket, §4.1), deletes that stage's attempt markers in
the bucket so the count starts at 1, publishes the task to `vsw.<stage>`
with `attempt=1`, and sets the job back to `running:<stage>`. Because
completion is checked by HEAD on each stage's output, **replaying
`embed` re-runs only the encode; replaying `sync` re-runs sync and the
chain continues (fetch → embed) by itself**. `--invalidate-downstream`
deletes the outputs of later stages first, for the case where the fix
changes an earlier stage's result (a sync fix must produce a new
render). `--set style.band_fg=#000000` overrides a payload field for
"was it the style?" experiments. The scaler sees `ready > 0` and
creates the singleton if needed. The dead message stays until
`tools/queue.py dead --ack <id>` or its 30-day TTL, so a replayed job
and its dead record can be compared.

For a debugger rather than a re-run: `tools/queue.py run <job> <stage>`
executes the same handler inline on the operator's machine against the
bucket, under the right interpreter (`ID_PYTHON` / `SYNC_PYTHON` from
`.env`), with the same inputs. Same code, no broker.

### 7.6 The operator view — exactly what was asked

`tools/queue.py`, read-only except `replay`, `dead --ack` and `--gc`, in
the style of `tools/doctor.py`:

```
tools/queue.py list [--state failed|running|queued] [--since 24h]
tools/queue.py show <job> [--full] [--windows]
tools/queue.py logs <job> [<stage> [<attempt>]]        # fetch from the bucket and print
tools/queue.py dead [--show <id>] [--ack <id>]
tools/queue.py replay <job> <stage> [...]
tools/queue.py queues                                  # depths, ready / unacked, from the management API
tools/queue.py compute [--gc]                          # the singleton row, program states, orphaned credentials
```

`show` prints the job's stage runs in order, then the narrative:

```
job 3263f4b50583   Op.39 Scherzo (Breitkopf)   visitor 203.0.113.7   state failed (exhausted at embed)
upload  transit/3263f4b50583/input/3263f4b50583.mp4   16.4 MB   7.25 min   640x360

stage     att  host             started   elapsed  state        error
extract   1    web              10:31:02  0m09s    done
identify  1    compute li-4821  10:33:40  1m38s    done         → matched, consensus 1.00, 8 windows
chroma    1    compute li-4821  10:35:41  0m13s    done
sync      1    compute li-4821  10:35:54  0m01s    done         → 412 measures, crowding 0.000, span 1.00
fetch     1    compute li-4821  10:35:55  0m02s    done
embed     1    compute li-4821  10:35:57  4m10s    failed       transient  CommandFailed: Rendering the video failed
embed     2    compute li-4821  10:41:12  4m08s    failed       exhausted  CommandFailed: Rendering the video failed

embed attempt 2
  inputs   video input/3263f4b50583.mp4 (16.4 MB)  measures jobs/3263f4b50583/sync_data/measures.data (412 rows)
           package Op.39_…  bands 71  style {aspect 16/9, band bottom, bg none, ink #1c1622}
  outputs  none
  commands
    $ ffprobe -v error -print_format json -show_format -show_streams input/3263f4b50583.mp4          rc 0   0.2s
    $ ffmpeg -y -f concat -safe 0 -i /tmp/videosync-x/bands.txt -vf scale=1306:244,… band.mp4  rc 0   31.4s
    $ ffmpeg -y -f lavfi -i color=c=0x141019:s=1920x1080:r=25 -i input/3263f4b50583.mp4 -i band.mp4 \
        -filter_complex "[0:v]scale=…" -map "[out]" -map 1:a:0 -c:a aac -b:a 192k \
        -c:v libx264 -crf 20 -preset medium -pix_fmt yuv420p -t 435.096 Op.39_….attempt2.part.mp4   rc 137  3m36s
  error    CommandFailed (transient→exhausted): Rendering the video failed
  stderr   … [libx264 @ 0x55d] frame= 5391 fps=24 q=28.0 size= 61440kB time=00:03:35.64 bitrate=2333.6kbits/s
           Killed
  log      jobs/3263f4b50583/logs/embed.attempt2.log   (tools/queue.py logs 3263f4b50583 embed 2)
  dead     vsw.dead #17  — tools/queue.py replay 3263f4b50583 embed

narrative (job.log, 41 lines) — tools/queue.py show --full
```

An HTTP twin, `GET /internal/jobs/<id>/diagnostics` returning the same
as JSON, is bound to loopback and the VLAN only, for `curl` from the web
box; no public admin surface (§17).

### 7.7 What survives the singleton

| Artefact | Where it lives | Written when |
|---|---|---|
| `stage_runs` rows, `jobs` state | web box, SQLite | every `started` / `finished` / `failed` / `interrupted` event, at once |
| the narrative `job.log` | web box | every `log` event, at once |
| per-attempt logs with full stderr and tracebacks | bucket `jobs/<job>/logs/…` | PUT at the end of every attempt, before its final event |
| the dead record | broker (durable queue, on the web box) | published before the failing stage acks |
| supervisord per-program logs — the only trace of a program that **died without raising** (OOM, native abort, `kill -9`, destroy) | bucket `logs/instances/<instance_id>/` | `logship` program: every 60 s, and once more at shutdown |
| ffmpeg's partial `.part` output | the singleton's disk | not preserved; it is an incomplete encode and its stderr already says why |

To make the last shipment happen, the scaler does not `DELETE` a running
instance: it issues Linode's `shutdown`, waits for `offline` (SIGTERM
reaches supervisord → each program's `handle_shutdown_signal` → the
broker requeues at once; `logship` runs its final sync), then `DELETE`
(§9.4). The window in which a program dies *and* its last minute of log
is lost is therefore a hard kill of the instance by the provider, not a
normal destroy.

---

## 8. Consumer mechanics — what changes in the copied base classes

### 8.1 One program per stage, each under the interpreter that owns its library

Today three interpreters exist because no single environment can hold
cairo, torch and numba together; Flask's process therefore reaches
recognition and alignment through **runner scripts** it spawns with the
right interpreter and talks to through JSON files and exit codes:

| Today | What it costs the owner when something breaks |
|---|---|
| `tools/identify_runner.py`, driven by `app/identify.py:206-262` | `_run` waits on a child, `_failure` (`:254-262`) reconstructs a reason from the **last line of stderr** and a substring match on `"No module named"` |
| `tools/sync_runner.py`, driven by `app/sync.py:118-158` | `_status` (`:197-218`) substring-matches `"LLVM ERROR"` / `"Symbol not found"` in the child's output to guess a config problem, otherwise reports "exited with code N: <last line>" |

With one program per stage, **each program runs under the interpreter
that can import its library**, so both runners are deleted and the
string-matching with them:

| Program | Interpreter | Imports directly | Replaces |
|---|---|---|---|
| `identify` | engine env (`ID_PYTHON`) | `weefeen_id.aggregate.identify_aggregated`, `load_v6_indexes` — once, at start | `tools/identify_runner.py`; `identify.py:206-262`. `app/identify.py` keeps `Identification`, `Candidate`, the `outcome` rule and the too-short / no-audio gates as plain functions used by the program. |
| `chroma`, `sync` | engine env (`SYNC_PYTHON`) | `services.audio_to_chroma.audio2chroma`, `services.audio_synchronization_service.wfn_combination_selector` from `VSS_ROOT` on `sys.path`, read-only, `PYTHONDONTWRITEBYTECODE` as today | `tools/sync_runner.py`; `sync.py:118-158` and `_status` at `:197-218`. `app/sync.py` keeps `Alignment`, `PartialRecording`, `MAX_CROWDING`, `SPAN_RANGE` and the checks as a pure function over the rows. |
| `extract`, `fetch`, `embed`, `events`, `notify`, `scaler` | app env | ffmpeg via `shell.run`, boto3, pika, `render.render()` | nothing to delete; `render._run` and `probe` gain the command record (§7.4) |

What replaces `_status()`'s guesses, case by case:

- **A wrong environment** (the `"No module named"` and `"Symbol not
  found"` cases) fails at **program start**: the import raises with a
  full traceback in the program's own stderr log, or — for the SVML
  abort, which cannot be caught — a **startup self-test** runs
  `audio2chroma` on a bundled one-second WAV before the program
  connects to the broker. An abort there is a crash loop that
  supervisord reports as `FATAL` after its retries, visible in
  `tools/queue.py compute`, with the LLVM line in `chroma_err.log`. No
  message is ever consumed by a broken program. The same self-test runs
  in `deploy/compute-image.sh` before the image is baked.
- **A failure on a particular file** is a Python exception in the
  program that ran the library: class, message and traceback go into
  `stage_runs` and the per-attempt log. `PartialRecording` is raised by
  the sync program itself, not reconstructed from a JSON field.
- **A native abort on a particular file** (the rare segfault in a
  codec) kills the program. supervisord restarts it; the broker
  requeues on the dropped connection; the next attempt records the
  previous one as `interrupted` with a pointer to the shipped program
  log, which holds the abort's last words; the cap turns a repeat into
  `exhausted`. If such aborts ever become common, the escalation is to
  run the library call in a child *process of the same interpreter*
  (`multiprocessing`, `spawn`) so the program survives and captures the
  signal — no runner script, no second interpreter, just a fork
  boundary — but that is not proposed now.

**A fact to act on before step 2, outside this repo:** the engine
interpreter (`2026liszt`: torch 2.11, librosa) has **no `pika`** and no
`boto3`; the app interpreter (VideoScoreSync env: pika 1.3.2, librosa)
has no torch. The engine-env programs need both installed there — one
line the owner runs himself (`<ID_PYTHON> -m pip install pika boto3`);
the webapp never modifies that environment. On the Linux image it is
moot: `deploy/compute-image.sh` builds the `engine` env from a lock file
that lists them.

### 8.2 The hour-long callback vs the heartbeat

`BaseQueueConsumer` uses `pika.BlockingConnection(heartbeat=60)`
(`consumer_base_queue.py:35-42`) and runs the work inline in the
callback. pika services heartbeats only when control returns to the
connection; during an hour of `ScoreVideoMaker` it does not, the broker
drops the connection after three missed beats, requeues the message, the
eventual `basic_ack` lands on a dead channel, and the done-flag saves the
day on redelivery. It works by accident.

**Keep the heartbeat at 60 s; move the work off the connection thread.**
The connection thread loops `connection.process_data_events(time_limit=1)`
while a worker thread runs the handler; the worker hands `ack` / `nack` /
`publish` back through `connection.add_callback_threadsafe(...)`
(pika's `BlockingConnection` is not thread-safe). About 40 lines, in the
one copied base class every program shares.

**Why not `heartbeat=0`:** it disables dead-peer detection. When the
scaler destroys the singleton mid-job, the unacked message stays bound
to a connection the broker still believes is alive until TCP keepalive
gives up — hours by default. The next instance's consumer connects and
finds nothing while the job sits invisible. With a 60 s heartbeat the
requeue takes about two minutes. The heartbeat is what makes destroying
the instance safe.

### 8.3 RabbitMQ's acknowledgement timeout

Independently of heartbeats, RabbitMQ (since 3.8.15) closes a channel
whose delivery stays unacknowledged longer than `consumer_timeout`,
**default 30 minutes**, with `PRECONDITION_FAILED`; the message is
requeued while the encode still runs and the second copy starts a second
encode. In `rabbitmq.conf`:

```
consumer_timeout = 10800000     # 3 h, ms; longer than the longest embed at MAX_DURATION_MINUTES
```

On www this is the **shared** broker's setting (§15.1): it applies to
the PHP consumers too, and only loosens — they ack in seconds and never
notice. If the broker is ≥ 3.12, prefer the per-queue
`x-consumer-timeout` on `vsw.embed` and leave the global alone. Verify
the installed version before step 4.

### 8.4 Other changes to the copies

- Credentials and vhost from `.env` (`RABBITMQ_URL=amqps://user:pass@10.0.0.2:5671/vsw`), never guest.
- **Publisher confirms** (`channel.confirm_delivery()`) and a raised
  error on failure, replacing "publish without confirmation, trust the
  broker" (`publisher_base_queue.py:48-49, 63`). A consumer that cannot
  publish its successor must not ack.
- One long-lived publishing channel per program instead of a connection
  per message with a `sleep(0.1)` (`:35-59`).
- DLX on every work queue: `x-dead-letter-exchange: vsw.dlx` → `vsw.dead`;
  plus the explicit dead record of §7.5.
- The unconditional completion check and re-publish-on-complete (§6.2).
- The stage shell is **one function** shared by every program —
  `serve(queue, handler, interpreter_selftest)` — so the protocol of
  §6.1 exists once and a program file is ~30 lines: import the library,
  define `handle(task, paths, store, log)`, call `serve`.
- Keep `handle_shutdown_signal` (`consumer_base_queue.py:90-93`): a
  Linode shutdown becomes SIGTERM via supervisord, the connection closes
  cleanly, the unacked message is requeued at once.
- One supervisord `[program:<stage>]` per queue, `autorestart=true`,
  `stdout_logfile` / `stderr_logfile` per program as
  `supervisord_embed_score.conf:5-12` does; the `.conf` files are
  generated from `.env` by `deploy/`.

### 8.5 The local-development escape hatch

VideoScoreSync gates a non-broker path on `USE_RABBITMQ`
(`publisher_task_manager.py:72-77`). It rotted the way such paths do:
the inline path calls `handle_sync_video` in `task_manager.py:179-218`
while the broker path calls `process_sync_video` in
`consumer_sync_video_queue.py:45-97` — two copies of one body, the
inline one without flags or acks.

Keep **one** escape hatch, structured so it cannot diverge, and keep
the per-interpreter split even without a broker — a thread pool in one
process cannot import cairo and torch together, so "inline" here means
**the same per-stage programs reading a directory instead of a queue**:

```ini
QUEUE_TRANSPORT=amqp | files
```

`files`: `WORK_DIR/queue/<stage>/<ts>-<job>.json`, claimed by
`os.replace` into `claimed/` (atomic on both OSes), acked by delete,
dead-lettered by move into `dead/`; events written straight into
`jobs.sqlite` and `job.log` by the program (same machine). ~60 lines.
`tools/workers.py` starts the programs with their interpreters on
Windows the way supervisord does on Linux. The handler bodies, the
protocol of §6.1, the logs and the records are identical in both modes;
only the transport and the events sink are adapters. `files` is also
**the single-machine deployment of steps 1–3**, so it is not dead code;
Docker Desktop runs the real broker on Windows for the integration test
before each release (the owner already has `windows.docker` in
`config.py:17`).

Testing the CPU path on a machine with a GPU: set
`CUDA_VISIBLE_DEVICES=-1`. The empty string is **not** equivalent — it
leaves `torch.cuda.is_available()` true with `device_count() == 0`, and
`torch.load` then fails with "Attempting to deserialize object on CUDA
device 0". This cost one wrong measurement already.

---

## 9. The singleton's life on Linode

### 9.1 Create/destroy, with numbers

Linode bills a powered-off Linode at the full rate — plan and disk stay
reserved — so stopping saves nothing and destroying is the only thing
that stops the meter. Design for create/destroy. What makes it fast is a
**custom image**, and what makes the image fit is that nothing needs
CUDA:

| Image | Contents | Size | Fits the custom-image limit (~6 GB compressed — **verify**) |
|---|---|---|---|
| CPU-only, everything | Debian; ffmpeg; an `app` env (Pillow, cairosvg + cairo, boto3, pika, this repo); an `engine` env (torch **CPU** wheel ~200 MB, the 165 MB transcription checkpoint, librosa, soundfile, numba **with SVML** — `icc_rt` from conda or pip numba; the abort at `sync.py:210` is exactly this condition, and the chroma program's self-test runs in the image build); checkouts of VideoScoreSync (`api_audio` + two services) and music_finrgerprint (`src/weefeen_id`, 9 MB indexes); the score library; supervisord with one program per stage plus `logship` | ~2.5–3.5 GB | yes |
| with CUDA (not chosen) | + torch+cu12x ~2.5 GB + CUDA libraries + driver | 5–10 GB | no, or barely |

Two interpreters on the singleton rather than three: `2026liszt`
already hosts torch, librosa and numba together, so one CPU `engine`
env does too; `ID_PYTHON` and `SYNC_PYTHON` may point at it, as they do
today. The `app` env stays separate for cairo.

| Path | Time until the first stage runs | Idle cost |
|---|---|---|
| **Create from the custom image** | create ~30–60 s + boot ~30 s + supervisord + torch import and checkpoint load ~20–25 s → **~2 min** | none — the instance does not exist |
| Create from a stock image + provisioning | 5–15 min | none |
| Powered-off instance | ~1 min | **full price** — not an option on Linode |

The image is built by `deploy/compute-image.sh` in this repo — the
owner's "deployment in the webapp" — from the two lock files, and
rebuilt when they change; an image-version tag on the instance lets the
scaler refuse a stale one. **The build fails unless the image proves
itself**, in this order, before it is captured: every module each stage
imports, imported under that stage's interpreter (`weefeen_id`,
`services.audio_to_chroma`, `services.audio_synchronization_service`,
`app.render`, `pika`, `boto3`); `audio2chroma` on a bundled 1-second WAV
(the SVML abort); `identify_aggregated` on a bundled 30-second clip;
`render.render()` on a 2-second fixture through the real ffmpeg; and
every program's `/metrics` answering on its port. A broken image then
fails at build, in front of a person, rather than at three in the
morning under a visitor's job. VideoScoreSync's `embed_score_err.log`
ending in `ImportError: libGL.so.1` is the kind of thing this catches —
that file is dated 2025-04-22, sixteen months old, evidence that it
broke once, not of the current state; the check stands on its own.

### 9.2 Isolation — Linode's "private IP" is not private to the account

Linode private addresses are reachable by **every Linode in the same
data centre**. Binding RabbitMQ there would put every job on a network
shared with strangers. The boundary is a **Linode VLAN** (account-
isolated Layer 2; region-dependent — **verify the region**) plus a
**Cloud Firewall** on each box.

- Web box (www): VLAN interface `10.0.0.2/24` (adding one to the
  existing Linode needs a reboot — schedule it with the site's owner).
  The existing broker gets **one additional TLS listener on
  `10.0.0.2:5671`** for us; its current listener for the PHP consumers
  is untouched, and nothing of ours listens on `0.0.0.0` or the shared
  private IP; management API on loopback. Cloud Firewall: public inbound
  22 from the admin address, 80/443 from anywhere, drop the rest —
  checked against what the Symfony site already needs before applying.
- Singleton: created **with** a VLAN interface at the fixed
  `ipam_address 10.0.0.3/24`. Cloud Firewall: public inbound 22 from the
  admin address only. Its public IP changes on every create and
  **nothing references it**: the broker's bind address, the firewall
  rules and `RABBITMQ_URL` all name VLAN addresses.
- Cloud Firewalls filter the public and private interfaces, not VLAN
  traffic (**verify**); the VLAN is trusted by construction, which is
  why the broker must listen nowhere else.
- **TLS on 5671 regardless**: a CA created once, its certificate (public
  material only) in the image, the server certificate on the web box.
  Belt-and-braces on an isolated VLAN, a one-time setup, and it means a
  misconfigured VLAN is not a leaked password.
- Object storage is reached over Linode's public HTTPS endpoint; it has
  no VLAN, so credentials are the boundary there.

### 9.3 Credentials for an instance that is new every time

Baking long-lived secrets into the image is the obvious approach and
the obvious risk: whoever has the image has the broker and the bucket.
Better, and not much more work:

1. **Per-instance, short-lived credentials created by the scaler at
   create time and revoked at destroy.** RabbitMQ: `PUT /api/users/vsw_c_<id>`
   with a random password; permissions read on the work queues, write
   on `^vsw\.(sync|fetch|embed|events|dead)$`; deleted on destroy,
   `tools/queue.py compute --gc` sweeping any left by a crashed destroy.
   S3: **STS temporary credentials** — the scaler's own IAM user may
   only `sts:AssumeRole` a role scoped to the `vsw` bucket
   (`s3:GetObject/PutObject/DeleteObject/ListBucket` on `transit/*` and
   `jobs/*`), and assumes it with `DurationSeconds` at the role's
   12-hour maximum. Nothing to revoke: they expire. A session that
   outlives them (a four-hour queue cap makes that rare) refreshes
   through the VLAN bootstrap endpoint of item 3.
2. **Delivered through cloud-init `user_data`** on the create call
   (Linode Metadata; region-dependent — **verify**). The image holds no
   secrets; first boot writes `/etc/vsw/env` and starts supervisord.
3. Fallback where Metadata is unavailable: the image boots a bootstrap
   service that calls `https://10.0.0.2/internal/bootstrap` over the
   VLAN with its Linode ID; the scaler, which knows the ID it just
   created, answers once with that instance's credentials.
4. Last resort only: bake bucket-scoped and queue-scoped credentials,
   rotate them with every image build, and accept the risk knowingly.

### 9.4 The scaler — one process on the web box, and the singleton lock

`python -m app.scaler` under supervisord/systemd beside gunicorn — **the
only thing that ever calls the Linode API**. Flask never does; it
publishes messages and nothing more. Being one process makes the
ordinary case trivially exclusive; two further guards cover operator
error and restarts:

- **The lease row.** `compute` has `CHECK (singleton = 1)` on its primary
  key, so at most one row can exist. The scaler claims with
  `BEGIN IMMEDIATE; INSERT … state='creating', lease_until=now+600`
  — SQLite serialises writers across processes, so a second scaler
  started by mistake gets a constraint error and stands down.
- **Linode label uniqueness.** Labels are unique per account; the
  instance is always created with label `vsw-compute`. Two creates
  racing past every local guard produce one instance and one `400`
  "label must be unique", and the loser adopts the winner by listing
  `?label=vsw-compute`. The provider is the final referee; the row is
  the fast path.

Behaviour on the awkward paths:

| Situation | What happens |
|---|---|
| Scaler restarts while the row says `creating` | On start it reconciles against the API: instance labelled `vsw-compute` exists → adopt (row → `running`, wait for the VLAN address to answer); none and `lease_until` passed → delete the row, next tick creates afresh. |
| Create fails halfway (API accepted, then provisioning error) | The API call returns an instance id first; the row records it immediately. On error the scaler destroys that id if it exists and clears the row. Reconciliation on the next start does the same for anything it does not remember. |
| Destroy fires just as a ticket is published | Immediately before the shutdown, one last management-API read; if anything is ready or unacked, abort. The remaining window is that read's latency. If a message still lands inside it: not yet consumed → stays `ready`, the next tick creates again (~2–3 min of latency, nothing lost); consumed and unacked → SIGTERM on shutdown requeues it at once. |
| Destroy call fails | Row stays `destroying`; retried each tick; `tools/queue.py compute --gc` for the rest. |
| Web box down for an hour | The broker is on the web box, so the singleton's programs reconnect with backoff as `consumer_base_queue.py:64-70` already does, and finish what they hold when it returns; unacked work is requeued by the reconnect and skipped by HEAD. `logship` keeps shipping to the bucket meanwhile, so nothing diagnostic is lost either. |

The loop, every `COMPUTE_POLL_SECONDS`:

```
depths = management API: messages_ready, messages_unacknowledged per vsw.{identify,chroma,sync,fetch,embed}
row    = compute (or none)
if any ready > 0 and row is none:                        → claim lease; CREATE (image, type, REGION = www's region — a VLAN
                                                           does not cross regions, so this is a hard requirement — VLAN .3,
                                                           firewall, user_data with fresh credentials, label);
                                                           write deploy/targets/compute.json so Prometheus scrapes it (§14.4)
if row.running and all ready == 0 and all unacked == 0:  → idle_since = idle_since or now
                                                           (unacked == 0 is what proves every result is in S3 — see below)
                                                           if now − idle_since ≥ COMPUTE_GRACE_SECONDS:
                                                               re-read; row → draining; POST …/shutdown;
                                                               wait for offline (≤ 90 s; logship's final sync happens here);
                                                               DELETE; revoke credentials; empty targets/compute.json; delete row
else:                                                    → idle_since = null
write compute.state into the table for /status ("compute": none | creating | running)
```

**The hard ordering rule: "render succeeded" and "the result is safe"
are different events, and the instance may only die after the second.**
It holds by construction, and is stated so nobody weakens it by
accident: the embed program's `ack` is the *last* step of §6.1, after
the PUT of `output/<job_id>_PROCESSED.<ext>` to `jobs/<job_id>/…` has
returned and a HEAD has confirmed the size (multipart for anything over
100 MB, so a dropped connection fails the PUT rather than truncating
the object). Until that `ack`, the delivery is `messages_unacknowledged`
and the destroy condition above is false — `unacked == 0` *means* every
result is in S3. A SIGTERM during the PUT (the provider stopping the
box, not us) requeues the message; the next instance finds no result
and re-runs the encode. The scaler adds one belt to those braces: it
also refuses to destroy while any `jobs` row is `running:embed` with
`result_key IS NULL` — a check that lags by the events latency, which
is why the unacknowledged count, not the table, is the authority.

```ini
COMPUTE_MODE=auto                  # auto | always | never   (never = single-machine deployments)
COMPUTE_POLL_SECONDS=10
COMPUTE_GRACE_SECONDS=600          # §9.5 — the largest single cost driver at this volume
COMPUTE_CREATE_SECONDS=120         # what the UI and the ETA quote while creating; calibrated from the compute table
COMPUTE_TYPE=g6-dedicated-8        # Linode names plans by RAM: 8 GB, 4 dedicated cores (§1.2). g6-dedicated-16 doubles both and the rate.
COMPUTE_REGION=                    # MUST equal www's region
COMPUTE_IMAGE=private/…  COMPUTE_VLAN=vsw  COMPUTE_FIREWALL_ID=
MEMORY_BUDGET_BYTES=5900000000     # what sync may allocate for its DTW on this plan: 8 GB minus the measured baseline (§1.2)
LINODE_TOKEN=                      # scoped: linodes read/write, images read, firewalls read, object-storage keys read/write
```

`LINODE_TOKEN` is the most powerful secret in the system; it lives only
on the web box.

### 9.5 The grace period — the primary tunable

The cold start is paid **once per idle gap**, not once per job:
arrivals cluster (someone shares the link, several people try it the
same evening), and a visitor who arrives while the instance is up gets
a warm recognition (no create, no 20 s import) and an embed that starts
at once. The grace period buys that warmth with idle minutes. At the
8 GB dedicated plan's ~$0.108/h ($0.0018/min), an idle gap that ends in
a destroy costs `G × $0.0018`:

| grace → / sessions per day ↓ | 5 min | 10 min | 30 min | 60 min |
|---|---|---|---|---|
| **1** (one cluster a day) | $0.27/mo | $0.54 | $1.62 | $3.24 |
| **3** | $0.81 | $1.62 | $4.86 | $9.72 |
| **10** | $2.70 | $5.40 | $16.20 | $32.40 |

What each avoided cold start is worth: ~2 min of instance creation plus
~20 s of model load for the visitor (a warm recognition instead of a
cold one), and the visitor who identified a piece and is choosing a
style — typically a few minutes — never sees the instance vanish under
them. But measured per video (§11.6), **the grace is the largest single
cost at this volume**: on the 8 GB plan a median 3.5-minute video costs
$0.0036 to identify, $0.0014 per minute to align and encode and $0.0033
to boot the instance for — about $0.012 of work — while a 30-minute
grace adds $0.0493, six times the work. Per isolated video: 5-min grace
$0.020, 10-min $0.028, 30-min $0.061. And at ~10 videos a month a
second job almost never arrives inside any window, so the grace is
paid and rarely redeemed.

**Recommend `COMPUTE_GRACE_SECONDS=600` (10 min).** What it must cover is
the one dependency inside a single visit: recognition happens on the
singleton, the visitor then picks a score and a style and submits; if
the box dies in that pause, the submit pays a two-minute create. Ten
minutes covers that pause for anyone actually choosing; five is tight
for it. What it gives up against 30: the second visitor of an evening
arriving 10–30 minutes after the first finished pays the create — a
latency cost, ~$0.003, not a money one. In one line: 10 min costs
$0.028 per isolated video against $0.061, and loses warm starts only
for arrivals 10–30 minutes apart, which at ten videos a month are a
handful a year. Raise it when the traffic says so — the
`vsw_compute_creates_total` panel shows how often a create follows a
destroy within the hour. A session then lives for its jobs (≤ 24 min
each, typically 10–16) plus 10 minutes. If Linode bills any started hour in
full (**verify**), replace the fixed grace with "destroy at the end of
the hour already paid for, but not sooner than 30 minutes idle"
(`COMPUTE_ALIGN_TO_BILLING_HOUR=true`).

---

## 10. Artifacts: three tiers, and how bytes cross

### 10.1 AWS S3 with Glacier — not Dropbox, not Linode Object Storage, not R2

VideoScoreSync's Dropbox flow (`api_dropbox/`, watch → processing → done
folders at `config.py:251-256`) is a **human** handover for the Cliburn
team; the counterpart here is a browser and two machines.

**A correction to this document's own earlier choice.** Previous
versions specified Linode Object Storage, for intra-provider transfer.
Two facts make that wrong now. It has a single storage class (lifecycle
*expiry* only, no archive tier), so the 48-hour → archive move would be
a mover we write and operate; S3 does it with one lifecycle rule. And
transfer is not a cost at this volume: AWS gives 100 GB a month of
egress free, and this app moves about 3 GB a month to visitors (ten
50 MB downloads, plus the singleton's fetches of recordings — median
50 MB, 4 GB at the cap — from S3 to Linode). Cloudflare R2's zero-egress
advantage therefore does not apply here either, and R2 has one storage
class too. The bucket lives in the AWS region nearest www's Linode
region; one private bucket, no public ACLs, presigned URLs only.

**Glacier Flexible Retrieval, not Deep Archive.** Restores in 3–5 hours
(1–5 minutes expedited) against 12–48 hours, for under a dollar a month
of difference until the archive passes ~300 GB. With a 48-hour link,
missed windows will be routine, so restore requests will be routine.
Small print to record: a 90-day minimum storage duration per object;
upload requests $0.03–0.05 per 1 000.

### 10.2 Upload — parametric in the caps

**At 4 GB (§1.1) — the path we ship:** browser → Apache → gunicorn →
Flask (`MAX_CONTENT_LENGTH` raised to 4 GB) → a staging file on the block
volume (§15.1) → PUT to `transit/<job_id>/input/<job_id>.<ext>`, local
copy deleted after extract. What Apache needs for that body:
`ProxyTimeout 3600` (the default is 60 s; 4 GB at 20 Mbit/s takes
~27 min) and a matching gunicorn `--timeout`; nothing for the size —
`LimitRequestBody` defaults to unlimited. Werkzeug streams a multipart
body to a temp file, so RAM is not the limit; staging disk is: 4 GB ×
concurrent uploads, bounded by `LIMIT_UPLOADS_PER_HOUR` and a new
`MAX_CONCURRENT_UPLOADS=2` that answers 503 beyond.

**Direct-to-bucket (step 7, optional at 4 GB):** the browser PUTs
multipart parts to `transit/<job_id>/input/…` through presigned URLs
(64 MiB parts; `ListParts` on reload so an interrupted upload resumes;
`abort-incomplete-multipart-upload` after 1 day), and the web box sees
only metadata: `POST /api/uploads` (limits, rights, declared size),
`GET …/part/<n>`, `POST …/complete` (HEAD for the size, `ffprobe` over a
presigned GET for duration and audio, both caps, both budgets, then
`vsw.extract`). Admission happens twice either way: before any byte and
after the probe; a file failing the second check is deleted and the
visitor told why, as at `routes.py:404-416` today.

### 10.3 The link, and the 48 hours enforced by our code

`/api/jobs/<id>/download` stays the address in the mail. Before
`finished + RETENTION_HOT_HOURS` it answers `302` to a 15-minute presigned
GET, generated per click; the web box never proxies a result. After it,
**`410 Gone`**, with a JSON body for the API and a plain page for a
browser:

> **This video has been moved to long-term storage.** It was online
> until Thursday 11 September, 14:20 — links stay live for 48 hours
> after a render. It has not been lost: reply to your confirmation
> email, or write to <address>, and we will bring it back online within
> a few hours, for another 48 hours.

It must read as "archived, restorable", never as "we lost it".

**Why our clock and not the lifecycle rule.** S3 lifecycle works in
whole days and runs asynchronously: a rule set at day 2 fires "sometime
after", and a live link could then point at an object already in
Glacier — a GET that fails with `InvalidObjectState`, which to a
visitor is a broken link, or a naive client waiting hours for it. So:

```
our /download endpoint     410 Gone at exactly finished + RETENTION_HOT_HOURS (48 h)
S3 lifecycle rule          transition jobs/ to GLACIER at Days: 3
the deliberate gap         one day, so a valid link always points at a retrievable object
```

**Restore.** `tools/queue.py restore <job> [--expedited]` calls
`RestoreObject(Days=2)`; the events program polls `HEAD` for
`x-amz-restore: ongoing-request="false"`, sets `jobs.restored_until`,
and the link is live again for 48 hours with a second `ready` mail.
Expedited ~$0.03/GB and $10 per 1 000 requests; standard $0.01/GB.
Restoring the small `sync_data/` files is seconds and cents.

`notify.py:54`'s "The link works for as long as the file is kept on the
server" becomes untrue and is replaced by the copy in §13.2, which
names the actual expiry date and time.

### 10.4 Three tiers — what crosses, what is discarded, and why

| Tier | Where | Holds | Lives |
|---|---|---|---|
| **1 — performance** | the singleton's local disk | the whole `<job_id>/` tree of §4.2, **one job at a time**, ~4.5 GB peak (4 GB recording + 0.36 GB result + audio + chroma) | dies with the instance |
| **2 — hot, 48 h** | S3 Standard | what crosses from tier 1 (below); the emailed link is live | `RETENTION_HOT_HOURS`, enforced by us |
| **3 — archive** | S3 Glacier Flexible Retrieval | the same objects, moved by lifecycle rule | indefinitely; restored on request |

What crosses from tier 1 to tier 2 — the exclusions are deliberate:

| | Object | Why |
|---|---|---|
| **KEEP** | `output/<job_id>_PROCESSED.<ext>` | the deliverable |
| **KEEP** | `sync_data/chroma.npy`, `measures.data`, `verdict.json` | a job can be **re-rendered with different styling** without redoing recognition or alignment: the encode alone (~0.8 min per minute of music) instead of boot + recognition + alignment + encode. This is why project files are kept, not only the video. |
| **KEEP** | `input/job_params.json` **minus the email address** and minus the visitor's address | reproducibility: package, mode, style, meta, duration, bytes, the recording's sha256 |
| **KEEP** | `logs/<stage>.attempt<N>.log` | diagnostics (§7.7); they contain job-id paths and commands, no personal data |
| **DISCARD** | `input/<job_id>.<ext>` — the visitor's own recording | |
| **DISCARD** | `sync_data/audio.wav` — their raw performance audio | |
| **DISCARD** | `output/hq_audio.m4a`, `silent_*` — intermediates (not produced here anyway) | |
| **DISCARD** | the rasterised bands | regenerable from the package |

**The privacy argument, explicitly:** the two largest files are also the
two that are personal data — someone's recording of themselves, and its
audio — and both are discarded. What is archived is a video the visitor
asked us to make and the abstract artefacts of aligning it. **No
contact details in archived storage:** the email address exists only in
`jobs.sqlite` on the always-on host; `job_params.json` in the bucket
carries neither it nor the visitor's network address nor the original
file name (the tree is named by job id; the original name lives in
`jobs.name`). The panel text (`meta`) is what the visitor chose to burn
into the video and is kept with it.

**Mechanics.** `transit/<job_id>/…` is deleted by the events program at
`finished + RETENTION_HOT_HOURS` — the same moment the link goes `410` —
and on `abandoned`; the lifecycle rule expiring `transit/` at day 3 is
the backstop. Within those 48 hours a restyle needs no re-upload; after
them the visitor re-uploads, `sha256` and duration in `job_params.json`
match the file to its archived `sync_data/` (restored expedited in
minutes), and recognition and alignment are skipped.

```
deploy/s3/lifecycle.json
  transit/          Expiration Days: 3                       backstop; our code deletes at 48 h
  jobs/             Transition GLACIER Days: 3               our code returns 410 at 48 h; the day between is deliberate
  jobs/*/markers/   Expiration Days: 30
  logs/instances/   Expiration Days: 90
  AbortIncompleteMultipartUpload  Days: 1
```

Sizes: tier 2 at ten videos a month holds well under a gigabyte; tier 3
grows by ~50 MB per video — 0.018 cents per video per month, $0.11 a
month after five years at ten a month. Egress is the visitors'
downloads and the singleton's fetches, inside the free 100 GB.

```ini
S3_BUCKET=vsw
AWS_REGION=eu-central-1            # nearest www's Linode region; verify
AWS_ACCESS_KEY_ID= AWS_SECRET_ACCESS_KEY=      # www's user: the bucket, and sts:AssumeRole on the compute role (§9.3)
S3_COMPUTE_ROLE_ARN=
RETENTION_HOT_HOURS=48
ARCHIVE_TRANSITION_DAYS=3          # must exceed RETENTION_HOT_HOURS / 24 by at least a whole day
S3_ARCHIVE_CLASS=GLACIER           # Flexible Retrieval. DEEP_ARCHIVE deliberately not (§10.1)
```

---

## 11. ETA, capacity, budgets, admission

### 11.1 Calibration

Per stage, from the last 20 successful `stage_runs` once 10 exist:
`k0`, `k_d`, `k_g` by robust fit (medians of `elapsed/D` and `elapsed/G`
are enough at this volume) into `calibration`; §1's defaults before
that. The ETA is quoted as a range (×0.7–×1.4) while the sample is thin,
as a clock time once it is not. **Re-measure on the actual instance type
before the first real visitor**: the CPU figures in §1 are from a
workstation.

### 11.2 Position and ETA — exactly

One embed consumer drains sequentially, so "ahead" is every unfinished
job, in queue order:

```
est(J)      = Σ_stage (k0 + k_d·D_J + k_g·G_J)  over the stages J has not completed
                                                (identify included while J is not yet identified)
ahead(J)    = jobs with state ∈ {queued, running} and (rank, submitted) < (rank_J, submitted_J)
remaining(R)= est(R) − elapsed of R's current stage, floored at 1 min          (R = the running job, if any)
cold        = compute.state == none     ? COMPUTE_CREATE_SECONDS
            : compute.state == creating ? COMPUTE_CREATE_SECONDS − (now − compute.created)
            : 0
eta_at(J)   = now + cold + remaining(R) + Σ est(A) for A in ahead(J) \ {R} + est(J)
position(J) = 1 + |ahead(J)|
```

`rank` is the fairness rank: a visitor's k-th unfinished job sorts after
every other visitor's (k−1)-th. Stored in `jobs.rank`, `jobs.position`,
`jobs.est_minutes`, `jobs.eta_at`; recomputed for every unfinished job
by the events consumer on each transition and by the scaler when
`compute.state` changes; served by `/status`; quoted at submit ("3rd in
line; ready by about 16:40"), on the page, and in the `queued` mail.

The broker is FIFO; the web box can hold a job's `vsw.chroma` publish
until its rank says so (`queued_unpublished`, released by the events
consumer). ~20 lines; skip in step 1, add with the rank in step 3.

### 11.3 Capacity, and where create/destroy pays

With the 25-minute ceiling every job costs `≈ 3 + 0.82·D` minutes of
compute (identify 2, chroma/sync/extract ~1, encode 0.82 per minute of
music — **on four cores this may be 1.2–1.6; measure**, §1.2 — fetch
seconds at ≤ 4 GB) plus the cold start and grace **once per session** —
assumed here at one session per five jobs (2 min create + 10 min grace
≈ 2.4 min per job; at "a handful a day" sessions are mostly single
jobs, and §11.6 prices that case). Rows are real Chopin lengths; the
last is the ceiling.

| min/job ↓ · jobs/week → | 5 | 20 | 50 | 100 | 200 |
|---|---|---|---|---|---|
| **8** (a nocturne) | 1.0 h | 4.0 h | 10 h | 20 h | 40 h |
| **15** (a ballade) | 1.5 h | 5.9 h | 15 h | 30 h | 59 h |
| **20** (a scherzo pair) | 1.8 h | 7.3 h | 18 h | 36 h | 73 h |
| **25** (the ceiling; the longest concerto movement) | 2.2 h | 8.6 h | 22 h | 43 h | 86 h |

Dollars: ~$0.108/h → **$/month ≈ h/week × 0.47**; always-on the same
plan is $72/month ≈ 150 h/week, which no cell reaches. The owner's
"handful a day" (≤ 50/week, typically 8–15 min) is **$2–9/month** of
compute; the page itself now costs nothing extra (§15.1). If the
four-core encode ratio measures at 1.5 rather than 0.82, multiply the
hours by up to 1.6. Prices are placeholders for the current list.

Saturation: at the worst case, 3 renders × 24 min = 72 compute-minutes
per visitor per week, so **140 visitors** all at the ceiling fill the
singleton 24/7; at typical 12-minute pieces (~13 compute-minutes each),
~260. Under create/destroy "saturation" means a growing queue and bill,
which §11.5 caps. A session of three typical jobs keeps the singleton
alive ~45 min plus the 10-min grace; one full-length job, ~24 + 10.

### 11.4 Budgets on both axes — replacing the job count

`limits.py:67-72` counts **jobs**. Three jobs is 15 minutes of machine
or 4½ hours, 50 MB or 30 GB; the count bounds neither axis. Replace it:

```ini
# Per visitor (by address, and by email), sliding week, as limits.py already counts.
LIMIT_DURATION_MINUTES_PER_WEEK=75    # three full-length pieces, or nine nocturnes
LIMIT_UPLOAD_GB_PER_WEEK=12           # 3 × MAX_UPLOAD_GB
LIMIT_RENDERS_PER_WEEK=10             # kept only as a floor: every job pays a fixed ~3 min that neither axis
                                      # captures; 10 never binds for a person
# Per job (§1.1, §1.2)
MAX_UPLOAD_GB=4
MAX_DURATION_MINUTES=25
```

Arithmetic, worst case: 75 video-minutes ≈ 3 × (3 + 0.82 × 25) ≈ 71
compute-minutes ≈ **$0.13 per visitor per week**, plus ≤ 12 GB × 7 days
in the bucket. Typical case, three 12-minute pieces: ~39 compute-minutes
≈ $0.07. Ten visitors a week at the worst case: under $6/month. The
duration budget is checked at `POST /render` against `duration_s`
(exact) and both budgets provisionally at `POST /api/uploads`. A
permanent failure still spends the budget — refunding it would make
"upload a partial recording" a free way to burn compute; the visitor is
told why and the upload is kept.

### 11.5 Admission control in minutes

```ini
MAX_QUEUE_MINUTES=240     # accepted-but-unfinished processing minutes, all visitors
```

At submit, if `cold + remaining(R) + Σ est(ahead) + est(new) > MAX_QUEUE_MINUTES`,
refuse with `503` + `Retry-After`: "The queue is about N hours long
right now. Your upload is kept for 7 days — try again after HH:MM." Four
hours is ten full-length jobs or about eighteen typical ones ahead — an
evening's burst — and with the 24-minute ceiling no honest ETA inside
it is "tomorrow". Delivery is by mail (§13.2), so the cap bounds the
bill and the bucket, not the visitor's patience; it is easy to raise.

### 11.6 The bill, per video — measured, and what drives it

Median video in the corpus: 3.5 minutes, ~50 MB of output, 5.2 minutes
of work. On the 8 GB dedicated plan ($72/month ÷ 730 h):

| item | cost | scales with |
|---|---|---|
| identify | $0.0036 | nothing — fixed per video (`MAX_WINDOWS=8`) |
| chroma + sync + encode | $0.0014 per minute of music | **minutes of music** |
| boot the instance | $0.0033 | **sessions** |
| grace before destroy, 30 min | $0.0493 — **6.2× the work** | sessions × the grace |
| the same at 10 min (§9.5's default) | $0.0164 | |
| **an isolated video, 10-min grace** | **$0.028** ($0.061 at 30 min) | |
| a video sharing a warm instance | $0.008 | |
| Glacier, per video per month | $0.00018 — five years at ten a month: $0.11/month | videos × months |
| **all in, ten videos a month** | **~$0.36/month** at 30-min grace; **~$0.18** at 10 | |
| all in, thirty a month | ~$1.08 / ~$0.55 | |

Stated plainly, because it was asked twice and it is the crux: **the
bill is driven by minutes of music and by sessions — not by gigabytes,
and not per video.** Upload size affects storage (about 3 % of the
bill) and transfer, not the encode: the canvas is a fixed 1080p, so a
4 GB 4K source and a 200 MB phone clip of the same piece cost the same
to process. The two levers are the duration budget (§11.4) and the
grace period (§9.5); nothing else moves the number.

---

## 12. Where recognition runs — decision D, and what the page must say

The measurement resolved the GPU question: **130 s cold on CPU, 78 s
on the RTX, same piece, consensus 1.00 in both**. n=1, one file, a
workstation CPU — the constant must be re-measured on the Linode plans
before it is quoted to anyone. It also opened a choice that the
singleton makes sharper: recognition is the one step a visitor watches,
and `svs-wire.js` promises "a few seconds". Neither option keeps that
promise; the copy changes regardless.

| Option | First visitor of an idle gap | Visitor while the singleton is up | Money | Side effects |
|---|---|---|---|---|
| **(a) on the singleton** (default) | ~2 min create + warm recognition on 4 dedicated cores (est. 90–150 s; the 130 s cold figure minus ~20 s of import/load, on a CPU with fewer cores than the workstation — **measure**) ≈ **3.5–4.5 min** | warm recognition only ≈ **1.5–2.5 min** | none extra | Recognition and an in-flight encode share the cores (§2's `SIGSTOP` if it matters). The singleton is created at `…/complete`, so an abandoned upload costs one create + grace ≈ $0.12. |
| **(b) on the web box** | recognition on the small plan's CPU, no create: est. **2–5 min** on 2 shared vCPUs, less on a dedicated plan — **measure** | same | web plan 2 GB → 4 GB, +$12/month shared or +$24 dedicated (torch CPU + checkpoint need ~2 GB resident) | **www is the live Symfony host (§15.1)**: torch's 2 GB resident and minutes of multi-threaded CPU per recognition would land beside their site. If tried anyway: serialise with the existing `identify.py:43` semaphore (kept, on the web box), a queue depth of one or two, refusing beyond with "busy, try in a minute", `nice -n 10`. The singleton becomes purely batch, created at submit rather than at upload. The identify program is the same file either way; only its supervisor host changes. |
| (d) non-blocking: accept, recognise with everything else, mail "we think it is X — confirm or choose" | never waits | never waits | cheapest | A product change: the visitor returns once. **Decision E, the owner's.** |
| (e) a torch-free first guess on the web box | seconds | seconds | nothing | music_finrgerprint's librosa-only pipelines (`pipeline.py`, `pipeline_v4.py`; indexes `data/index.pkl`, `chord_index.pkl`, `pitch_index.pkl` present). **I do not know why AMT superseded them; the owner does.** If their accuracy was acceptable, this alone keeps "a few seconds" literally true, with AMT confirming later on the singleton. A day's experiment. |

**Recommendation: (a) by default, with a 30-minute grace (§9.5), and a
measurement gate for (b).** Reasoning: with the grace, every visitor
but the first of an evening finds the instance up and waits about two
minutes; the first waits about four. (b) makes *every* visitor wait
whatever the small plan's CPU takes, which on shared vCPUs is likely
longer than (a)'s warm case, for $12–24/month more and with the site's
responsiveness at stake. (b) wins **only if** a resident recognition on
the chosen web plan measures under ~90 s — then it beats (a) for the
first visitor and ties for the rest, and its cleaner batch/interactive
split is worth the plan upgrade. Measure both on the actual plans in
step 6 before choosing; the queue topology is identical either way
(`vsw.identify` is consumed on whichever host runs it). Try (e) for a
day if the owner remembers the old pipeline being usable; and decision
E remains the only way to make the wait disappear.

The copy (`svs-wire.js:399-402` area), driven by `/status`'s `compute`
state and the calibrated identify estimate, must be:

- singleton creating: **"Preparing to listen — about two minutes. Or pick
  the piece yourself below."** — the manual picker (`api_library`,
  already the fallback when `can_identify` is false) shown from the
  first second, so nobody is *waiting* for the answer, only *offered* it;
- recognising: **"Listening — about two minutes."** with the number
  from calibration;
- overrun of twice the estimate: **"Still listening — this is taking
  longer than usual."**, picker still there;
- never "a few seconds", never a spinner without a number.

---

## 13. Progress to the browser, and the mail

### 13.1 Polling, not SSE

The designed interface already polls `/api/jobs/<id>/status` every 2 s
(`svs-wire.js:1123-1127`); only the older `app.js:347-370` uses the SSE
route. With the state in a table written by another process, an SSE
stream would be a polling loop in a trench coat, and an hour-long
`EventSource` is cut by every proxy, sleeping laptop and backgrounded
phone in between; its one merit — reconnect with a state replay — is
what polling does by nature. **Drop `/api/jobs/<id>/events` and
`Registry.stream()`**; both UIs poll `/status`: 2 s while
`uploading…identifying`, 30 s after submit while the tab stays open,
with the page saying "you can close this; we will email `<address>`".

### 13.2 Email is the delivery; the page is for the impatient

`notify.py:12-13` calls the mail "a convenience on top of" the page.
With hour-long jobs and a visitor told to leave, that framing is
inverted and the docstring must say so. Three mails per job through
`vsw.notify`:

| kind | when | says |
|---|---|---|
| `queued` | at submit | position, ETA as a clock time (§11.2), "you can close this page", the link that will work later |
| `ready` | on `embed.done` | the link, and **the exact date and time it stops working** (`finished + RETENTION_HOT_HOURS`, §10.3); that it can be restored on request afterwards. `notify.py:54`'s "The link works for as long as the file is kept on the server" becomes untrue and is replaced: *"Your video is ready: <link>. The link works until Thursday 11 September, 14:20 (your video is kept online for 48 hours). After that it is moved to long-term storage; reply to this message and we will bring it back within a few hours."* |
| `failed` | on `failed(permanent | exhausted)` | the stage's own message; for anything that is our fault, say so |

Guarantees:

- **At-least-once attempt.** SMTP transient errors (`notify.py:70-71`)
  republish with backoff 1, 5, 30, 120 min, then hourly to 24 h; then
  `mail_state='failed'`, ERROR log. The result stays downloadable.
- **At-most-once delivery** per kind per job: `mail_state='sending'`
  before `send_message`, `mail_<kind>_at` after; a `sending` older than
  10 minutes on restart is `uncertain`, not resent (§6.3).
- **The caps in `limits.py:74-75` must not silence a result.** Today
  `_tell_them` (`jobs.py:206-211`) skips the mail when a cap is hit; a
  skipped result mail is now a visitor who never learns. The
  per-address cap counts **jobs**, not mails; `mail_total` (200/day)
  **defers** `ready`/`failed` to the next window and drops only `queued`.
- No address verification (§17); the render limits already gate the
  address, and a typed address remains the abuse vector `limits.py:73`
  describes.

---

## 14. Observability — designed fresh, and verified at every step

The owner: *"visualize the performance of our tool chain — time spent by
each worker, memory used, access disk"*, with Prometheus and Grafana;
and of his existing setup: *"I have it, but it was not working."* So
this is not "copy VideoScoreSync's"; it is a small set of metrics each
of which comes with the query that proves it is arriving.

**Division of responsibility, stated once.** The **job table**
(`stage_runs`, fed by `vsw.events`) is the system of record for "how
long did stage X take on job Y, on what input, with what result" —
durable, per job, queryable after the machine is gone; it answers the
debugging question of §7. **Prometheus** holds resource curves and
aggregates — quantiles, rates, queue depths, host CPU/memory/disk — and
Grafana draws them. Neither replaces the other, and Grafana never
answers "which stage failed with which inputs".

### 14.1 Why the existing one never worked — findings, not changes

VideoScoreSync is read-only; these are noted for the owner and fixed in
the webapp's own files.

1. **No worker was ever scraped.** `prometheus.yml:4-39` lists
   `prometheus`, `dropbox_webhook`, `node_exporter_linode`,
   `node_exporter`, `rabbitmq`, `cadvisor`, `node_exporter_local` — and
   no job for preprocessor, extract_audio, resample_audio,
   generate_chroma, sync_video, embed_score, upload_video or
   postprocessor. Every `@track_task` observation and every
   `start_metrics_server(port=PORT_*)` published to a port nothing read.
   This alone explains "time spent by each worker" never appearing.
2. **`worker_metrics.py` cannot be imported without `PROMETHEUS_MULTIPROC_DIR`.**
   Lines 18-19 run `multiprocess.MultiProcessCollector(CollectorRegistry())`
   at module import, which raises when the variable is unset. It is not
   set in `Dockerfile`, `Dockerfile.cpu`, `Dockerfile.gpu`,
   `docker-compose.yml` or any supervisord conf; `.env`/`.env.base`
   were unreadable to me, so **near-certain, not verified** —
   `prometheus_client` is not installed in the app env to reproduce it.
   If unset, every consumer importing the module died at start. The
   related defect: in multiprocess mode a `Gauge` needs
   `multiprocess_mode=`; `CPU_USAGE`/`MEM_USAGE_MB`/`CURRENT_JOBS`
   (`:28-34`) do not set it.
3. **Host-wide numbers labelled per worker.** `CPU_USAGE` uses
   `psutil.cpu_percent()` and `MEM_USAGE_MB` uses
   `psutil.virtual_memory().used` (`:65-66`) — both system-wide — under
   `worker=<pid>`. Every worker reports the same curve, which defeats
   exactly the question being asked.
4. **RabbitMQ scraped on the wrong port.** `prometheus.yml:27` targets
   `15672`, the management UI; Prometheus-format metrics come from the
   `rabbitmq_prometheus` plugin on `15692`. That scrape has been failing
   silently.
5. **The dashboard answers a different question.** The 699 KB
   `dashboard_template.json` is the stock node_exporter host dashboard
   (Network Traffic, Disk IOps, Sockstat, Systemd, TCP Stat, …): "how is
   the machine", not "how is the tool chain".

### 14.2 One stage registry, from which every artefact is rendered

Root cause 1 is structural: a hand-maintained scrape file drifted from
the worker list. The fix is that the list exists **once**, in code:

```python
# app/stages.py — the only place a stage is declared
STAGES = (
    Stage("extract",  queue="vsw.extract",  host="web",     env="app",    port=9301, next="identify"),
    Stage("identify", queue="vsw.identify", host="compute", env="engine", port=9302, next=None),
    Stage("chroma",   queue="vsw.chroma",   host="compute", env="engine", port=9303, next="sync"),
    Stage("sync",     queue="vsw.sync",     host="compute", env="engine", port=9304, next="fetch"),
    Stage("fetch",    queue="vsw.fetch",    host="compute", env="app",    port=9305, next="embed"),
    Stage("embed",    queue="vsw.embed",    host="compute", env="app",    port=9306, next=None),
    Stage("events",   queue="vsw.events",   host="web",     env="app",    port=9307),
    Stage("notify",   queue="vsw.notify",   host="web",     env="app",    port=9308),
    Stage("scaler",   queue=None,           host="web",     env="app",    port=9309),
)
```

Rendered from it by `deploy/render.py` (run by the deploy script and by
the image build): the supervisor programs for the web host and for the
compute image; the queue declarations (`amqp.declare_all()`); the
Prometheus static targets for the web-side programs and the shape of
the file_sd entry for the compute side; the `.env.example` port block;
and the expectation list of `tools/doctor.py --monitoring`. A stage
cannot exist without a scrape target, a supervisor program and a doctor
check, because they are the same line.

### 14.3 The minimum that answers the questions — each with its proof

`app/metrics.py`: **the default `CollectorRegistry`, no multiprocess
collector, no `PROMETHEUS_MULTIPROC_DIR`** — one process per stage makes
multiprocess mode unnecessary and removes that class of failure.
`prometheus_client.start_http_server(stage.port)` in every program.

| Metric | Type, labels | Emitted by | Proof it arrives — query, and what you should see |
|---|---|---|---|
| `vsw_stage_seconds` | Histogram `{stage}`, buckets 5 · 15 · 30 · 60 · 120 · 300 · 600 · 1200 · 1800 · 2700 s | each program, on `finished`/`failed` | `histogram_quantile(0.5, sum by (le, stage) (rate(vsw_stage_seconds_bucket[6h])))` → after one job, one value per stage; `embed` in the hundreds |
| `vsw_stage_runs_total` | Counter `{stage, outcome=done|failed|retried|interrupted}` | each program | `sum by (stage, outcome) (increase(vsw_stage_runs_total[24h]))` → `done` counts equal the jobs run today |
| `vsw_stage_media_seconds_total`, `vsw_stage_bytes_in_total`, `vsw_stage_bytes_out_total` | Counter `{stage}` | each program, on `finished` | realtime factor: `rate(vsw_stage_media_seconds_total{stage="embed"}[6h]) / rate(vsw_stage_seconds_sum{stage="embed"}[6h])` → ≈ 1.2 on the dev laptop (§14.7); GB/s: `rate(vsw_stage_bytes_in_total{stage="fetch"}[6h]) / rate(vsw_stage_seconds_sum{stage="fetch"}[6h]) / 1e9` |
| `vsw_process_rss_bytes`, `vsw_process_cpu_seconds_total`, `vsw_process_io_read_bytes_total`, `vsw_process_io_write_bytes_total` | Gauge / Counter `{stage}` | a 5-second sampler thread in each program — **this process plus its children** (§14.5) | `max_over_time(vsw_process_rss_bytes{stage="embed"}[1h])` → > 500 MB while an encode runs (ffmpeg is a child); `rate(vsw_process_io_write_bytes_total{stage="embed"}[5m])` → non-zero during an encode |
| `vsw_jobs_waiting`, `vsw_eta_seconds_max` | Gauge | `events` (owns the table) | `vsw_jobs_waiting` → equals `tools/queue.py list --state queued` |
| `vsw_calibration_k` | Gauge `{stage, axis=k0|k_d|k_g}` | `events` | `vsw_calibration_k{stage="embed", axis="k_d"}` → 0.8 until ten jobs, then the measured value — the drift the owner asked to see |
| `vsw_compute_up`, `vsw_compute_seconds_total`, `vsw_compute_creates_total` | Gauge / Counter | `scaler` | `increase(vsw_compute_seconds_total[30d]) / 3600 * 0.108` → the month's compute bill at the 8 GB plan's rate; `vsw_compute_up` → 1 while the singleton exists |
| queue depth per queue | — | **RabbitMQ's own plugin** (§14.6), nothing to emit | `rabbitmq_detailed_queue_messages_ready{vhost="vsw"}` and `…_unacked{vhost="vsw"}` → one series per `vsw.*` queue |
| host CPU, memory, disk I/O, filesystem | — | `node_exporter` on both hosts | `node_cpu_seconds_total`, `node_memory_MemAvailable_bytes`, `node_disk_io_time_seconds_total`, `node_filesystem_avail_bytes` |

Nine application series per stage; nothing speculative. Anything the
owner asks for later is added to `app/metrics.py` **with its proof
query** or not at all.

### 14.4 Metrics from a machine that exists only sometimes

`prometheus.yml` in VideoScoreSync is all `static_configs`; the
singleton has no fixed existence and gets its address at create.

| Option | Verdict |
|---|---|
| **file-based service discovery** — the scaler writes `deploy/targets/compute.json` (`[{"targets": ["10.0.0.3:9302", …, "10.0.0.3:9100"], "labels": {"host": "compute", "instance_id": "li-4821"}}]`) once the VLAN address answers, and writes `[]` on destroy; Prometheus re-reads it (`refresh_interval: 30s`), no restart | **Adopt.** Keeps the pull model, and the scaler already owns the lifecycle and knows the address. |
| Pushgateway | Rejected: metrics persist after the process dies and must be deleted explicitly; histograms lose meaning; it inverts the model for a box that is reachable over the VLAN anyway. |
| `remote_write` from the compute box | Rejected: an agent on the machine that dies, pushing to the machine that lives — more parts on the ephemeral side for no gain. |

The last seconds before a destroy: the scrape interval is 15 s and the
shutdown path of §9.4 takes ≥ 30 s (`shutdown`, wait for `offline`), so
the final samples are normally scraped; at worst one interval is lost.
The per-job truth is in the table regardless. After destroy the
compute targets vanish from `up`; the singleton's lifetime panel reads
`vsw_compute_up` from the scaler, which does not vanish.

### 14.5 Memory and CPU per worker — per process, plus children

`psutil.Process(os.getpid())` for RSS and CPU time, `io_counters()` for
disk, and **`children(recursive=True)` summed in** — ffmpeg is a child of
the embed program and holds the memory and the I/O; without the
children the embed program would report 50 MB while the encode used
two gigabytes. Sampled every 5 s into the gauges of §14.3. This replaces
`worker_metrics.py:61-72`'s host-wide `cpu_percent()` and
`virtual_memory().used`.

### 14.6 RabbitMQ's numbers

Enable the `rabbitmq_prometheus` plugin on the existing broker (a
broker-wide, harmless change, §15.1), bound to loopback
(`prometheus.tcp.ip = 127.0.0.1`), and scrape
`127.0.0.1:15692/metrics/detailed?vhost=vsw&family=queue_coarse_metrics`
— the plain `/metrics` endpoint **aggregates across queues** unless
`prometheus.return_per_object_metrics` is set, and per-queue depth is
the whole point. The scaler keeps reading the management API's JSON for
its create/destroy decision (§9.4); both come from the same broker
state, so the dashboard and the scaler cannot disagree.

### 14.7 Per-file time and throughput — the rates that do not lie

The owner: *"how much time spent for each file and Go/s speed."* Per
stage per job, `stage_runs` records `elapsed_s`, `bytes_in`,
`bytes_out`, `media_s` and `queued_at` (§5). Two derived rates, each
attributed only to the stages it characterises:

| Stage | Bound by | The stable figure | A GB/s figure here would… |
|---|---|---|---|
| embed (encode) | media duration | **realtime factor** = `media_s / elapsed_s` — the job on disk: 435 s of music in 356 s = **1.22× realtime**; the standard way to state encoder throughput, and what a regression shows up in | measure the *input's bitrate*: a 4K source and a phone clip of the same length take the same time and show GB/s ten times apart |
| chroma, sync, identify | media duration (identify flat) | realtime factor (identify: seconds per job) | mislead the same way |
| fetch, extract's demux, the result PUT | bytes | **GB/s** = `bytes_in / elapsed_s` (fetch, extract) or `bytes_out / elapsed_s` (PUT) | — this is where it belongs |

No single global "GB/s" is shown anywhere. The per-**file** roll-up,
because "it was slow" and "it waited" are different complaints:

```
tools/queue.py show <job>            (the time section)
                          queued   work     realtime   GB/s
  extract    0m01s        0m09s    —        0.18
  identify   2m10s ← waited for the singleton to be created
             0m00s        1m38s
  chroma     0m00s        0m13s    33×
  sync       0m00s        0m01s
  fetch      0m00s        0m02s    —        0.9
  embed      0m00s        5m56s    1.22×
  ─────────────────────────────────────────
  uploaded → done   10m10s   =  2m11s waiting  +  7m59s working
```

The same roll-up is a Grafana panel ("last 20 jobs: waiting vs working,
stacked") and the same rows feed `calibration` (§11.1). **One mechanism —
`stage_runs` — serves debugging (§7), monitoring (this section) and
estimation (§11); there is no second timing system.**

### 14.8 The dashboard — `deploy/grafana/dashboards/vsw-pipeline.json`

Hand-authored, small, about the pipeline; provisioned by the compose
file. Twelve panels:

| # | Panel | Query |
|---|---|---|
| 1 | Time per stage, p50 / p90 / max (bar gauge) | `histogram_quantile(0.5|0.9, sum by (le, stage) (rate(vsw_stage_seconds_bucket[24h])))` |
| 2 | Time per stage over time (p50 lines) | same, `[1h]`, time series |
| 3 | Realtime factor, embed and chroma | §14.3's ratio, per stage |
| 4 | GB/s, fetch and extract | §14.3's ratio, per stage |
| 5 | Last 20 jobs: waiting vs working (stacked bars) | from the table via Grafana's SQLite datasource plugin, or `vsw_job_*` gauges the events program exposes for the last N jobs — choose the plugin; it reads `stage_runs` directly |
| 6 | Queue depth per `vsw.*` queue, ready and unacked | `rabbitmq_detailed_queue_messages_ready{vhost="vsw"}`, `…_unacked` |
| 7 | Jobs waiting and current max ETA | `vsw_jobs_waiting`, `vsw_eta_seconds_max` |
| 8 | Failures and retries per stage (24 h) | `increase(vsw_stage_runs_total{outcome!="done"}[24h])` by stage, outcome |
| 9 | Singleton: up / creating timeline, creates this month, hours and $ | `vsw_compute_up`, `vsw_compute_creates_total`, `increase(vsw_compute_seconds_total[30d])/3600 * $price` |
| 10 | Memory per program (RSS incl. children) | `vsw_process_rss_bytes` by stage |
| 11 | Disk I/O per program | `rate(vsw_process_io_{read,write}_bytes_total[5m])` by stage |
| 12 | Calibration constants vs defaults | `vsw_calibration_k` by stage, axis, with the §1 defaults as thresholds |

A host dashboard sits beside it: import "Node Exporter Full"
(grafana.com ID 1860) by ID for both hosts rather than vendoring 699 KB.

### 14.9 Containers and cAdvisor — no

cAdvisor reports per-container use, so it helps only if the workers run
in containers. The compute image runs **bare supervisord, no Docker**:
one fewer layer on a machine that must be useful ninety seconds after
it exists, and §14.5's per-process metrics answer the per-worker
question more precisely than per-container ones would. On www,
Prometheus, Grafana and node_exporter run under Docker because Docker
is already there; cAdvisor is not needed for our question and is not
run.

### 14.10 Where it runs, and what it costs a production box

On www (§15.1), in `deploy/docker-compose.yml`: Prometheus bound to
`127.0.0.1:9090`, Grafana to `127.0.0.1:3000` and reached through the
Apache vhost at `https://chopin.weefeen.com/grafana/` (`GF_SERVER_ROOT_URL`,
`serve_from_sub_path`, Grafana's own login, admin password from `.env`),
node_exporter on `127.0.0.1:9100`. Retention
`--storage.tsdb.retention.time=30d`. Volume: ~4 000 series (nine
programs × ~60, node_exporter ~1 500 × 2 hosts, RabbitMQ ~300) at 15 s
≈ 35 MB/day ≈ **1–1.5 GB for 30 days**, on the block volume. RAM:
Prometheus 200–400 MB, Grafana ~100 MB. Check `free -m` on www before
step 4; if the plan is 2 GB, this is the item that argues for the next
size up, not the app itself.

### 14.11 "Is monitoring actually working" — `tools/doctor.py --monitoring`

One command, run after every deploy, that would have caught every item
of §14.1:

```
target               expected   up   last sample
web/extract   :9301  yes        1    12 s ago     vsw_stage_seconds_count{stage="extract"} = 41
web/events    :9307  yes        1    12 s ago
web/notify    :9308  yes        1    12 s ago
web/scaler    :9309  yes        1    12 s ago     vsw_compute_up = 0
compute/*     file_sd  not expected (vsw_compute_up = 0) — skipped
rabbitmq      :15692 yes        1    12 s ago     8 queues in vhost vsw
node_exporter :9100  yes        1    12 s ago
grafana       :3000  yes        ok   dashboard vsw-pipeline provisioned
```

It reads the registry for what *should* be up, queries Prometheus's
`up` and one application series per program, expects compute targets
only while `vsw_compute_up == 1`, and exits non-zero on any gap.
"It is instrumented" is not a state this tool can report.

### 14.12 What exists at each migration step

| Step | Timing per stage | Resource use | Curves and dashboard |
|---|---|---|---|
| 1 | `stage_runs` with elapsed, bytes, media; `show` with realtime factor and GB/s | `peak_rss_mb`, `cpu_s`, disk deltas per attempt in the table | none — one machine, no Prometheus |
| 2 | + one `/metrics` per program | + per-process gauges | Prometheus + Grafana under Docker Desktop, scrape file rendered from the registry, dashboard v1 (panels 1–4, 8, 10, 11); `doctor --monitoring` |
| 4 | same, on www | same | on www; panel 6 from the broker; §14.10 |
| 6 | + `compute` labels | + node_exporter on the image | file_sd for the singleton; panels 9, 12 |

---

## 15. Deployment — the host, and where every file lives

Everything deployment-related is in `VideoSync_webapp/deploy/`. The
weefeen (Symfony) repository is read-only to us like the other two;
nothing in it is modified, and nothing in VideoScoreSync's compose,
prometheus, grafana or supervisord files is used or referenced.

### 15.1 The host: the existing www.weefeen.com Linode

| Fact | Consequence |
|---|---|
| `172.104.249.66`, an EU region; `vss.weefeen.com` is `45.33.126.124`, US | **The singleton is created in www's region** — VLANs do not cross regions (§9.4). |
| Apache 2.4.41, not nginx | Our vhost `chopin.weefeen.com`, mounted at `/` — no path prefix, no `SCRIPT_NAME`, no URL rewriting in the browser code: `ProxyPass / http://127.0.0.1:5057/`, `ProxyPassReverse`, `ProxyTimeout 3600`, `ProxyPreserveHost On`, `ProxyAddHeaders Off` + `RequestHeader set X-Forwarded-For "%{REMOTE_ADDR}s"` — **overwrite, never append**, or a client-supplied header becomes free quota; `mod_remoteip` for Apache's own logs. `LimitRequestBody` stays at its unlimited default. `/grafana/` proxied to `127.0.0.1:3000`. |
| Certificate: GoDaddy, CN `weefeen.com`, SANs `weefeen.com`, `www.weefeen.com`, no wildcard, expires 2026-10-08 | `certbot --apache -d chopin.weefeen.com` — free, auto-renewing, coexists with the GoDaddy cert on its own vhost. |
| RabbitMQ already running, with supervisor-managed PHP consumers (`etc/supervisor/weefeen_prod.ini`: `weefeen_create_chroma`, `weefeen_sync_performance`, `weefeen_detect_measures`, `messenger-consume`) | **We add a vhost, not a broker.** `vsw` vhost; users `vsw_web` and the per-instance `vsw_c_<id>` (§9.3) with permissions scoped to `vsw`; three additive broker-wide changes: a TLS listener on the VLAN address (`listeners.ssl.default = 10.0.0.2:5671`; their existing listener untouched), `consumer_timeout = 10800000` (§8.3 — a **global** setting that only loosens; their consumers ack in seconds and never notice; use the per-queue argument instead if the broker is ≥ 3.12), and the `rabbitmq_prometheus` plugin on loopback (§14.6). |
| What a vhost isolates, and what it does not | Separate queue namespace, users, permissions and policies — **not** a separate process, memory or disk. If our queues grew without bound, the broker's memory alarm would block **every** publisher, theirs included. Our messages are < 4 KB of JSON (the bytes live in the bucket) and `MAX_QUEUE_MINUTES` bounds the count to a few dozen, so the realistic risk is low — and it is made structural: a policy `max-length = 1000` on `^vsw\.`, and `rabbitmqctl set_vhost_limits -p vsw '{"max-connections": 20, "max-queues": 20}'`. |
| Supervisor installed, established pattern (`directory` / `command` / `autostart` / `autorestart` / `stdout_logfile` / `stderr_logfile`), running as root | Ours match the pattern — `deploy/supervisor/vsw.conf`, symlinked into `/etc/supervisor/conf.d/`, program names `vsw_extract`, `vsw_events`, `vsw_notify`, `vsw_scaler`, `vsw_web` (gunicorn) — and run as a dedicated unprivileged user **`vsw`**: these programs handle visitor-uploaded files and pass visitor-derived arguments to ffmpeg; a bug in that path should be bounded by a user that owns nothing but `/mnt/volume_1/vsw`. |
| Docker available | Prometheus, Grafana, node_exporter in `deploy/docker-compose.yml` (§14.10). Not the broker, not the workers. |
| Block storage at `/mnt/volume_1`; Symfony deploys to `/mnt/volume_1/weefeen/current/` | Ours under **`/mnt/volume_1/vsw/`**: `releases/<sha>/` and `current` (the app), `work/` (`WORK_DIR`: job dirs, `jobs.sqlite`, `job.log`s), `staging/` (uploads in flight, ≤ 2 × 4 GB), `prometheus/`, `grafana/`. Space: staging 8 GB, work ~20 GB (audio, logs, transient measures — results live in the bucket), Prometheus 1.5 GB, headroom to 40 GB. |
| Deployment is GitHub Actions → Capistrano over rsync | Ours: **GitHub Actions → `deploy/deploy.sh` over ssh** — clone the tag into `releases/<sha>`, build the venv from the lock file, run `deploy/render.py`, migrate `jobs.sqlite`, flip `current`, `supervisorctl restart vsw:*`, `apachectl graceful`, then `tools/doctor.py --monitoring`. The same releases/current shape as Capistrano, without a Ruby toolchain for a Python app. |
| It is their **production** box | What we run beside Symfony: gunicorn (idle unless serving), extract (ffmpeg demux + resample, one core for 10–40 s per upload), mail, the scaler (an HTTP call every 10 s), Prometheus + Grafana (~0.5 GB RAM). Caps: extract is one program at prefetch 1 (structural) and runs under `nice -n 10`; `MAX_CONCURRENT_UPLOADS=2`; `LIMIT_UPLOADS_PER_HOUR` stays. **Recognition does not run here** — option (b) of §12 would put torch's 2 GB and minutes of CPU on a live site, which tilts decision D firmly toward (a) unless www is a large plan. |

`.env` on www, beyond what `.env.example` already documents:

```ini
PUBLIC_BASE_URL=https://chopin.weefeen.com
TRUST_PROXY=true                 # MANDATORY behind Apache: without it limits.py:181-185 sees 127.0.0.1 for every
                                 # request and the whole internet shares one bucket. Apache must OVERWRITE
                                 # X-Forwarded-For (above), never append a client-supplied one.
WORK_DIR=/mnt/volume_1/vsw/work
UPLOAD_STAGING_DIR=/mnt/volume_1/vsw/staging
RABBITMQ_URL=amqps://vsw_web:…@10.0.0.2:5671/vsw
RABBITMQ_MANAGEMENT_URL=http://127.0.0.1:15672
PROMETHEUS_URL=http://127.0.0.1:9090
METRICS_PORT_EXTRACT=9301 … METRICS_PORT_SCALER=9309     # rendered from app/stages.py; listed for the operator
```

### 15.2 File layout — every artefact, one place to look

`VideoSync_webapp` today tracks `app/`, `tools/`, `run.py`,
`requirements.txt`, `.env.example`, `README.md` and this document; it
has no deployment files. Everything below is new.

| Path | Contains | Step |
|---|---|---|
| `app/stages.py` | the single stage registry (§14.2) | 2 |
| `app/queue/task.py`, `paths.py`, `keys.py` | `Task`, `JobPaths`, bucket keys (§4) | 1 |
| `app/queue/transport.py` | `files` and `amqp` transports behind one interface (§8.5) | 1 (`files`), 4 (`amqp`) |
| `app/queue/amqp.py` | the copied and corrected consumer/publisher base (§8) | 4 |
| `app/queue/store.py` | `jobs.sqlite` access: `jobs`, `stage_runs`, `calibration`, `compute` (§5) | 1 |
| `app/queue/sink.py` | events sink: direct-to-table (`files`) or publish-to-`vsw.events` (`amqp`) | 1 / 4 |
| `app/workers/__init__.py` (`serve()`), `shell.py`, `joblog.py` | the one stage shell (§6.1), `shell.run` (§7.4), `JobLogger` (§7.3) | 1 |
| `app/workers/render.py` | step 1's single stage (today's `pipeline.run`) — folded into `embed` in step 2 | 1 |
| `app/workers/extract.py`, `identify.py`, `chroma.py`, `sync.py`, `fetch.py`, `embed.py` | one program per stage (§3) | 2 |
| `app/workers/events.py`, `notify.py` | the web-side programs (§3, §13.2) | 3 (inline), 4 |
| `app/workers/logship.py` | supervisord logs → `logs/instances/<id>/` (§7.7) | 6 |
| `app/metrics.py` | plain registry, per-process psutil with children, `start_http_server` (§14.3, §14.5) | 2 |
| `app/scaler.py` | Linode create/destroy, lease row, credentials, `targets/compute.json` (§9.4, §14.4) | 6 |
| `app/store/` (`object_store.py`) | boto3 against AWS S3: PUT/GET/HEAD/DELETE, presign, multipart, `RestoreObject`, the `transit/` / `jobs/` key scheme (§4.2, §10) | 5 |
| `app/retention.py` | the 48-hour clock: `410` after `finished + RETENTION_HOT_HOURS`, deletion of `transit/` objects, the restore request (§10.3, §10.4) | 5 |
| `deploy/s3/bucket.sh`, `deploy/s3/lifecycle.json`, `deploy/s3/iam/` | bucket creation, the lifecycle rules of §10.4 (`transit/` expire day 3, `jobs/` → GLACIER day 3), the compute role and the scaler's user policy (§9.3) | 5 |
| `tools/queue.py` | `list`, `show`, `logs`, `dead`, `replay`, `run`, `queues`, `compute` (§7.6) | 1 (`list`, `show`, `logs`), 3 (`dead`, `replay`, `run`), 4 (`queues`), 6 (`compute`) |
| `tools/workers.py` | starts the stage programs with their interpreters on Windows (§8.5) | 2 |
| `tools/doctor.py --monitoring` | the check of §14.11 | 2 |
| `deploy/render.py` | renders supervisor confs, `prometheus.yml`, the port block, from `app/stages.py` | 2 |
| `deploy/docker-compose.yml` | Prometheus, Grafana, node_exporter — **web host only**; loopback bindings | 2 (dev), 4 (www) |
| `deploy/prometheus.yml` | **generated**, never hand-maintained; static web targets + `file_sd_configs` for `targets/compute.json` | 2 |
| `deploy/targets/compute.json` | written by the scaler; `[]` when no singleton | 6 |
| `deploy/grafana/dashboards/vsw-pipeline.json`, `deploy/grafana/provisioning/` | the twelve-panel dashboard (§14.8), datasource and dashboard provisioning | 2 |
| `deploy/supervisor/vsw.conf` | web-host programs in the weefeen_prod.ini style, user `vsw` (§15.1) — generated | 4 |
| `deploy/supervisord/compute.conf` | compute-image programs: one per stage + `logship` — generated | 6 |
| `deploy/apache/chopin.weefeen.com.conf` | the vhost of §15.1 | 4 |
| `deploy/rabbitmq/vhost.sh` | vhost, users, permissions, policies, vhost limits, TLS listener snippet, plugin (§15.1) | 4 |
| `deploy/deploy.sh`, `.github/workflows/deploy.yml` | releases/current deploy over ssh, then `doctor --monitoring` (§15.1) | 4 |
| `deploy/compute-image.sh`, `deploy/compute/` (lock files, fixtures, self-test) | bakes the Linode custom image with the build-time checks of §9.1 | 6 |
| `deploy/tls/` | CA generation and the server cert request for 5671 (public material only in git) | 6 |
| `.env.example` | every knob in this document, with its default and one line of why — as today | each step adds its own |

---

## 16. Migration path — each step shippable and verified; step 1 is one machine; diagnostics from step 1

**Step 1 — job table, one embed worker, the diagnostic record
(~350 lines).** `jobs.sqlite` with `jobs`, the full `stage_runs` schema
of §5, `calibration`; `Job`/`Registry` over it; `Task`, `JobPaths`; the
`files` transport (§8.5); **one worker program** (app env) consuming
`queue/embed/` and running today's `pipeline.run` as a single stage
called `render`; `shell.run` (§7.4) replacing `render._run` and `probe`,
so every ffmpeg/ffprobe command and its stderr tail are recorded;
per-attempt logs under `WORK_DIR/<job>/logs/`; `tools/queue.py list`,
`show`, `logs`; `position` and `eta_at` exactly as §11.2 with `cold = 0`;
`routes.py:459` refuses `queued`; both UIs show "N in line, ready by
about HH:MM"; SSE removed; `MAX_DURATION_MINUTES` enforced at upload;
**the job tree of §4.2 adopted** — `input/<job_id>.<ext>` replaces the
shared `uploads/` folder, `pipeline.cleanup()` is finally called, and
the 980 MB of test residue is deleted by hand once.
*Verify:* two submits → one ffmpeg; restart with two queued → both
resume in order; `stage_runs` has a row with elapsed, bytes in and
out, media seconds and the process's peak RSS, and `show` prints the
realtime factor and the waiting/working split (§14.7); **kill ffmpeg
mid-encode and `tools/queue.py show` prints the exact command, `rc -9`,
and the stderr tail**; paste the command into a terminal and it runs.

**Step 2 — stages, per-interpreter programs, extract-audio (~400
lines).** `handle_extract/identify/chroma/sync/fetch/embed` as pure
functions; `serve()` as the one stage shell; **`identify`, `chroma`,
`sync` programs under the engine interpreter importing their libraries
directly; `tools/identify_runner.py` and `tools/sync_runner.py`
deleted; `identify.py:206-262` and `sync.py:118-158, 197-218` deleted**;
the chroma self-test; completion checks, attempt markers, `interrupted`
detection, `.attemptN.part` outputs; transient/permanent classification
and caps; `tools/workers.py` starting the programs on Windows;
`job.log` narrative; `CUDA_VISIBLE_DEVICES=-1` in the CPU test. The
owner installs `pika` and `boto3` into the engine env first (§8.1).
**Observability arrives here, with the programs:** `app/stages.py` and
`deploy/render.py`; `app/metrics.py` with one `/metrics` per program
(plain registry, per-process psutil with children); Prometheus +
Grafana under Docker Desktop from `deploy/docker-compose.yml`, the
scrape file **generated** from the registry, dashboard v1 (panels 1–4,
8, 10, 11 of §14.8); `tools/doctor.py --monitoring`.
*Verify:* verdict and `measures.data` identical to before
(`tools/compare_alignments.py`, byte for byte, per `README.md`); kill
ffmpeg mid-encode → retried once, **no re-align**; a no-audio upload
fails permanently with no retry; **a partial recording fails in `sync`
with `PartialRecording` as `error_class` and a real traceback in the
attempt log**; `kill -9` the chroma program mid-job → the next attempt
records `interrupted` for the first; point `SYNC_PYTHON` at the wrong
env → the chroma program refuses to start and says why; **`doctor
--monitoring` shows every program `up`, `vsw_stage_seconds_count`
grows after a job, panel 1 shows one bar per stage and panel 10 shows
ffmpeg's memory under `embed`** — the proof that VideoScoreSync's setup
never had.

**Step 3 — mail as delivery, budgets, admission, replay (~250
lines).** Three mail kinds with the table gate and backoff; `notify.py`
docstring; `LIMIT_DURATION_MINUTES_PER_WEEK`, `LIMIT_UPLOAD_GB_PER_WEEK`,
`MAX_QUEUE_MINUTES`; fairness rank; per-job mail counting; the `dead/`
directory with `{task, failure}` records; **`tools/queue.py dead` and
`replay`**, including `--invalidate-downstream`; `tools/queue.py run`
for the debugger.
*Verify:* kill between done and mail → exactly one `ready`; kill during
`sending` → none, `uncertain`; over-budget on either axis refused with
the right copy; A with three uploads and B with one → B second in line;
**break the embed on purpose, fix it, `replay <job> embed` → only the
encode re-runs and the visitor gets a `ready` mail**.

**At the small end (§1.1) one always-on box stops here. Below is
contingent on wanting the compute box to exist only while working.**

**Step 4 — RabbitMQ, and the app live on www.weefeen.com (~450 lines +
the deploy files of §15.2).** Copy and fix the base classes (§8);
`QUEUE_TRANSPORT=amqp`; one supervisor program per stage; `vsw.events`
carrying `started / log / finished / failed / interrupted` into the
table and `job.log`; DLX plus the explicit dead record on `vsw.dead`.
First against Docker Desktop's broker on Windows, then on www:
`deploy/rabbitmq/vhost.sh` (vhost `vsw`, users, policies, vhost limits,
`consumer_timeout` or the per-queue argument, the `rabbitmq_prometheus`
plugin); `deploy/apache/chopin.weefeen.com.conf` and `certbot --apache`;
`deploy/supervisor/vsw.conf` as user `vsw`; `deploy/deploy.sh` with the
Actions workflow; Prometheus + Grafana from `deploy/docker-compose.yml`
on www behind `/grafana/`; `TRUST_PROXY=true`, `MAX_CONCURRENT_UPLOADS`,
extract under `nice`. The compute-side programs still run on the same
machine at this step (or on the dev box against www's broker over a
temporary TLS listener); the singleton is step 6.
*Verify:* `kill -9` the embed program mid-encode → requeued after the
heartbeat, finished stages skipped, `embed_attempt2` marker present, the
first attempt shows `interrupted` in `show`; a permanent failure lands
in `vsw.dead` with its full record **and** the visitor gets the `failed`
mail; `dead --show` prints the payload that ran; a 40-minute sleep in
embed does not trip the acknowledgement timeout; two jobs submitted
together: their chroma/sync overlap, their encodes do not. On www:
`curl -I https://chopin.weefeen.com/` from outside → 200 with the
certbot certificate; two visitors on different networks get two rate
buckets (`TRUST_PROXY` and the overwritten header work); a 4 GB upload
completes through Apache; `rabbitmqctl list_queues -p vsw` shows only
ours and `supervisorctl status` shows their consumers untouched;
`doctor --monitoring` on www is green, including `rabbitmq :15692` and
the per-queue depth series.

**Step 5 — AWS S3 and the three tiers, still one machine (~400
lines).** `deploy/s3/` (bucket, lifecycle rules, IAM role and user);
the `transit/` / `jobs/` key scheme mirroring the job tree; PUT/GET/HEAD
in every handler, markers in the bucket, **per-attempt logs PUT to
`jobs/<job>/logs/` before the final event**, `log_key` in `stage_runs`,
`tools/queue.py logs` reading from the bucket; `app/retention.py`:
download `302` until `finished + RETENTION_HOT_HOURS`, then `410` with
the copy of §10.3, `transit/` deleted at the same moment, the `ready`
mail naming the expiry; `tools/queue.py restore`.
*Verify:* delete the local job directory after `done` → download and
`logs` still work; delete `chroma.npy` locally and replay sync → fetched
from the bucket, not recomputed; set `RETENTION_HOT_HOURS=0.05` → the
link answers `410` three minutes after `done` with the archived-not-lost
text, and `transit/<job>/` is empty; `aws s3api get-bucket-lifecycle-configuration`
shows the two rules with the one-day gap; `restore` on an object forced
to GLACIER brings the link back and sends a second `ready`; `jobs/<job>/`
contains no email address and no original file name.

**Step 6 — the singleton and the scaler (~450 lines + the image
script).** VLAN in www's region, Cloud Firewalls, the TLS listener on
the VLAN address of the existing broker; per-instance credentials via
user_data; `deploy/compute-image.sh` with the full build-time checks of
§9.1, node_exporter, the 4 GB swap file and per-program `oom_score_adj`
on the image; the `heavy` lock in `sync` and `embed` and the DTW
pre-check against `MEMORY_BUDGET_BYTES` (§1.2); `app/scaler.py` with
the lease row, the label guard, **shutdown-before-delete**, and
`targets/compute.json` for file_sd (§14.4); `logship`; `instance_id` on
every event; `vsw_compute_*` metrics and panels 9 and 12;
`COMPUTE_MODE=auto`; **measure on the plan: the encode ratio on four
cores, recognition warm and cold, and the resident baseline RSS against
the 5.47 GB DTW peak** — settle decision D (option (b) is now a
production-box question, §15.1) and confirm 8 GB holds, or move to 16;
the copy of §12.
*Verify:* destroy the singleton by hand mid-embed → a new one within
~3 minutes and the job resumes at the encode, and **`show` names the
dead instance and `logs/instances/<id>/embed_err.log` holds its last
minute**; grace-period destroy observed with a final log shipment; an
upload completed with no instance has one running before extract
finishes; start a second scaler by mistake → it stands down; the
destroyed instance's broker user and storage key are gone; two uploads
completing in the same second → exactly one instance; `doctor
--monitoring` lists the compute targets `up` while `vsw_compute_up == 1`
and skips them after the destroy; panel 9 shows the session's hours and
dollars; a 25-minute recording aligns while an encode is queued behind
the `heavy` lock and `show` prints its peak RSS near 5.5 GB; a
26-minute recording is refused at upload, and one smuggled past the
cap fails in `sync` with the memory message and no retry.

**Step 7 — optional at 4 GB: direct-to-bucket upload with resume (~300
lines, mostly browser JS).** §10.2 endpoints; `UPLOAD_DIRECT=true`;
ffprobe over a presigned GET.
*Verify:* a 5 GB upload interrupted at 60 % resumes after a reload
without re-sending parts; a declared 20 GB is refused before any byte.

**Contingent:** `start_metrics_server` per program with a Prometheus
port each, as VideoScoreSync does; `SIGSTOP` priority for recognition
if contention is measured; the librosa-only first guess (§12 e); the
in-interpreter child-process boundary for native aborts if they ever
recur (§8.1).

---

## 17. Explicitly not proposed

- Publishing into VideoScoreSync's queues, or importing its consumers.
- One batch consumer over several queues; a broker on the singleton.
- Powering off instead of destroying; a GPU anywhere; more than one
  compute instance.
- Binding anything to Linode's shared private IP.
- Long-lived secrets in the image, except as §9.3's last resort.
- SSE of any kind.
- Segmented / resumable encoding.
- Refunding budget on a permanent failure.
- Address verification before mailing.
- An operator web UI or auth; a log-aggregation service (Loki, ELK);
  `tools/queue.py` on the box and the bucket are the operator surface.
- A cancel endpoint; the grace period and lifecycle rules bound the cost
  of an abandoned job.
- Merging `vsw.chroma` and `vsw.sync` into one program: they mirror the
  two consumers being reused and one log per stage is the point.
- A per-visitor "one at a time" refusal; the fairness rank does the same
  job without refusing anyone.
- Pushgateway, `remote_write`, cAdvisor, Docker on the compute image
  (§14.4, §14.9).
- Linode Object Storage or Cloudflare R2 for the tiers (one storage
  class each; the mover would be ours to write and run), Glacier Deep
  Archive (12–48 h restores against routine restore requests), and
  enforcing the 48 hours with a lifecycle rule instead of our own clock
  (§10.1, §10.3).
- Archiving the visitor's recording or their audio; keeping an email
  address anywhere but `jobs.sqlite` (§10.4).
- Vendoring the 699 KB host dashboard; it is imported by ID (§14.8).
- Recognition on www (§12 b) unless it measures under ~90 s on a plan
  that can spare 2 GB beside the Symfony site.
- A second broker on www; Capistrano for our deploy; any change to the
  weefeen repository.
- Quorum queues.

---

## 18. What I am not sure about

- **Every CPU constant** — 130 s recognition, 0.8 min/min encode — is
  n=1 on a workstation. The calibration loop and the "re-measure on the
  plan before quoting" rule exist for that; nothing structural depends
  on the values.
- **The decode/size term of the embed** may matter more on high-bitrate
  inputs than today's 640×360 job shows; the owner's 7 min/GB stays as
  its default until `stage_runs` says otherwise.
- **Linode specifics not verified against current documentation:** the
  custom-image size limit (~6 GB compressed), which regions offer VLANs
  and the Metadata service, whether Cloud Firewalls leave VLAN traffic
  unfiltered, whether partial hours are billed in full, whether label
  uniqueness is enforced at create time as I expect, and whether
  `shutdown` reliably delivers SIGTERM to supervisord before `offline`.
  Each has a fallback in §9 or §7.7; none changes the shape.
- **`ffprobe` over a presigned GET on a non-faststart 4 GB MP4** —
  unmeasured; fallback: probe on the singleton in `fetch`.
- **`consumer_timeout`** depends on the broker version installed.
- **Whether every ffmpeg OOM presents recognisably**; the embed cap of 2
  bounds a wrong guess, and the record now shows the command and `rc`
  either way.
- **The SVML condition** for the `engine` env (`sync.py:210-214`); the
  chroma self-test exists so it is found at build or start, never on a
  job.
- **Whether `weefeen_id` and the two VideoScoreSync services import
  cleanly as long-lived residents** — the runners were one-shot; a
  resident process may hold file handles or global state across jobs.
  Step 2's verification (ten jobs through one program) is where that
  shows.
- **`PROMETHEUS_MULTIPROC_DIR`** is absent from every VideoScoreSync
  file I could read; `.env` / `.env.base` were unreadable and
  `prometheus_client` is not installed in the app env, so root cause 2
  of §14.1 is near-certain, not reproduced.
- **www's plan and free memory** — unknown; §14.10 and §15.1 assume
  ≥ 4 GB. `free -m` before step 4.
- **Whether www's region offers VLANs and the Metadata service**, and
  whether `mod_proxy`, `mod_headers` and `mod_remoteip` are enabled
  (`a2enmod` is a minute; the VLAN is not negotiable).
- **RabbitMQ's version on www** — decides `/metrics/detailed` (3.9+)
  and the per-queue `x-consumer-timeout` (3.12+).
- **The 8 GB fit** (§1.2) assumes the resident programs' baseline stays
  near 2.5 GB beside a 5.47 GB DTW; the 24 B/cell figure is three runs
  with `tracemalloc` on a workstation. Step 6 measures both on the plan
  before the cap is shown to anyone; the fallbacks (non-resident
  identify, the 16 GB plan) are ready.
- **The encode ratio on four cores** — 0.82 was a workstation; the
  capacity and ETA tables scale with whatever step 6 measures.
- **AWS small print I have not re-verified this month:** the 100 GB/month
  free egress, Glacier Flexible's 90-day minimum and $0.03–0.05 per
  1 000 requests, the 12-hour STS role maximum, and expedited-restore
  availability in the chosen region. None changes the shape; the
  per-video figures of §11.6 move by cents at most.
- **`Job.save()` / `rehydrate()`** (`jobs.py:66-79, 250-284`) landed in
  `c718193` at 10:43 today and the only job on disk finished at 10:41
  without a manifest; step 1 replaces both with the table.
