# VideoSync_webapp — queued pipeline over RabbitMQ, one web Linode, one compute singleton

Design only. Nothing is implemented. Written against `VideoSync_webapp`
at `97e253a` (`feature/recognition`); VideoScoreSync and
music_finrgerprint were read and are never modified.

**Settled by the owner, not re-argued here:** RabbitMQ; the web app's
own queues and consumers, in its own repo, on the same broker; a cheap
always-on Linode for the page; **at most one** CPU-only compute Linode,
**created** when work exists, draining tickets **one at a time**, and
**destroyed** after a grace period with nothing to do; **libx264**;
**no GPU anywhere** (CPU recognition measured at 130 s cold against 78 s
on the RTX, same verdict, same confidence); object storage between the
hosts; VideoScoreSync's consumer skeleton reused by copying; limits on
**two axes**, file size and duration.

**Still open — the owner's call:**

| # | Decision | Section |
|---|---|---|
| D | Whether recognition runs on the compute singleton (recommended default) or on the always-on web box (better for every visitor **if** it measures under ~90 s on the small plan) | §12 |
| E | Whether recognition may become non-blocking ("we will confirm the piece by email") — a product change, laid out, not made | §12 |

Reading order if short of time: §2 (topology and diagram), §6 (state
machine), §9 (the singleton's life), §14 (migration path).

---

## 1. The cost model: two axes, measured where possible

The owner sizes the compute box on two parameters — **file size**
(attributed to the embed, ~7 min/GB on CPU) and **duration** (chroma and
alignment). Both axes bound what a visitor may submit and both are
budgeted (§11.4); nothing structural depends on which dominates. The job
table records elapsed, bytes and duration per stage so the constants
are measured, not believed. Where the evidence stands today:

| Stage | Depends on | Measured | Source |
|---|---|---|---|
| identify | neither (`MAX_WINDOWS=8`) | **78 s cold on the RTX, 130 s cold on CPU** — process start + 165 MB checkpoint + 8 windows; 27–44 s warm on the RTX | coordinator, n=1, workstation CPU |
| chroma + sync | duration | ~1.7 s per minute of music (13 s + 0.5 s for 7.5 min) | coordinator |
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

```ini
MAX_UPLOAD_GB=2                 # per job — transfer, storage, decode. 0.5 today (routes.py:28); 10 at the large end
MAX_DURATION_MINUTES=90         # per job — encode, chroma, sync. Nothing equivalent exists today
```

### 1.1 The two ends of the range

**Small end — `MAX_UPLOAD_GB=0.5`, `MAX_DURATION_MINUTES=15`.** Uploads
keep going through Flask; the web box puts them in the bucket; no
direct-to-bucket upload, no resume. Budgets on both axes still replace
the job count (the count bounds neither). **Is the on-demand singleton
justified here?** Not by job length. It is justified by price: the
alternative is *one always-on box big enough to encode* — an 8-core
dedicated Linode, ~$144/month — against a $12 web Linode plus compute
billed only while it exists, $5–35/month at "a handful a day" (§11.3).
What the split costs is engineering (steps 4–6) and a ~2-minute creation
at the start of each idle gap. **At the small end: ship steps 1–3 on one
box, measure, and build the split when the bill says so.** Nothing in
steps 1–3 is discarded by it.

**Large end — `MAX_UPLOAD_GB=10`, `MAX_DURATION_MINUTES=90`.** Direct
browser → bucket upload with resume is **mandatory** (§10.2): 10 GB
through a $12 Linode is 30 GB of transfer and a disk it does not have.
Budgets on both axes, weekly. Fetch is a real stage. Lifecycle rules
matter. Everything else is unchanged.

Because Linode bills an instance whether busy or idle, create/destroy
is cheaper than always-on at every utilisation below ~90 % (§11.3);
job length never decides it.

---

## 2. Topology

```
 BROWSER                 WEB LINODE — always on, 2 GB shared, ffmpeg                                LINODE OBJECT STORAGE (S3)
 ───────                 ───────────────────────────────────────────────                            ──────────────────────────
 POST /api/uploads ────▶ Flask: limits.guard(upload_ip) · rights · declared size ≤ MAX_UPLOAD_GB      bucket vsw/
   (metadata only)       INSERT jobs(state=uploading) · presigned PUT urls (large end)                 uploads/<job>/<name>
 PUT parts ─────────────────────────────────────────────────────────────────────────────────────────▶ audio/<job>/audio.wav
 POST …/complete ──────▶ ffprobe (range reads) → duration ≤ MAX_DURATION_MINUTES, has_audio           work/<job>/verdict.json
                         state=uploaded · publish vsw.extract                                          work/<job>/chroma.npy
                                │                                                                      work/<job>/measures.data
                         ┌──────▼──── RabbitMQ — Docker on the web Linode, TLS 5671 on the VLAN address ONLY ────┐  work/<job>/<stage>_attempt<N>
                         │  work:   vsw.extract  vsw.identify  vsw.chroma  vsw.sync  vsw.fetch  vsw.embed        │  results/<job>/<stem>_synced.mp4
                         │  return: vsw.events ◀── every stage, both hosts       web-local: vsw.notify            │
                         │  dead:   vsw.dead ◀── DLX from every work queue                                        │
                         └──┬──────────┬───────────────┬──────────────────────────────────────────────────────────┘
                            │          │               │
   ┌── web-box processes ───┘          │               └── compute-box processes
   │  extract   app env   ffmpeg -vn -ac 1 -ar 22050 → audio/<job>/audio.wav                    → vsw.identify
   │  events    app env   → jobs.sqlite (state, stage, attempt, position, ETA, per-stage timings) → vsw.notify on done / failed
   │  notify    app env   → SMTP: "queued, ready by ~HH:MM" · "ready" · "failed: why", table-gated
   │  scaler    app env   ONE process. Every 10 s: management API → messages_ready / unacknowledged per vsw.* queue.
   │                      work and no instance → CREATE (lease row + Linode label uniqueness = the singleton lock, §9.4)
   │                      nothing ready, nothing unacked, for COMPUTE_GRACE_SECONDS → DESTROY, revoke its credentials
   │  GET /api/jobs/<id>/status   ◀── browser polls (2 s while identifying, 30 s after submit); no SSE
   │  GET /api/jobs/<id>/download → 302 to a 15-minute presigned GET; the web box never proxies bytes
   │
   │  ═══ Linode VLAN 10.0.0.0/24 (account-isolated L2) ═══  web 10.0.0.2 ◀──▶ compute 10.0.0.3 (fixed at create) ═══
   │      Cloud Firewall on both: public inbound = 22 (admin) + 80/443 (web) only. NEVER Linode's shared "private IP".

 COMPUTE LINODE — at most one; CPU only (8 dedicated cores); custom image (~1–2 min to exist); no secrets baked in
   identify  process    engine env, torch CPU + indexes resident   audio.wav → verdict.json → S           → event identified
   batch     process    ONE consumer on vsw.chroma + vsw.sync + vsw.fetch + vsw.embed, prefetch 1 = one heavy ticket at a time
       chroma  engine env   audio.wav → chroma.npy → S                                                   → vsw.sync
       sync    engine env   chroma.npy + package reference → measures.data → S   (partial-recording check) → vsw.fetch
       fetch   app env      uploads/<job>/<name> → local disk, retry, size check                          → vsw.embed
       embed   app env      render.py: bands · strip · libx264 → results/<job>/<stem>_synced.mp4 → S      → event done
   a stage is done when its output is in S (HEAD); the local <stage>_done.flag only caches that fact (§6.1)
```

| Host | Runs | Because |
|---|---|---|
| Web Linode | Flask, RabbitMQ, the job table, extract, events, notify, scaler | Always on; owns every promise to a visitor. Extract is seconds per minute of video, makes the recogniser and the aligner consume a small file, and the box needs ffmpeg anyway (`routes.py:405` probes there today). |
| Compute singleton | identify, chroma, sync, fetch, embed | The minutes-to-hours of CPU. Exists only while there is work, plus a grace period. |
| Object storage | uploads, canonical audio, intermediates, attempt markers, results | The only durable bytes; the singleton's disk dies with it. |

Two processes on the singleton, not one, is a deliberate reading of
"one ticket at a time": the heavy chain is strictly sequential (one
consumer, four queues, prefetch 1 on the channel), while recognition —
two minutes, and the one step a visitor is watching — must not queue
behind an hour of someone else's encode. If contention between the two
is measured to hurt, the identify process sends `SIGSTOP` to the
running ffmpeg for its two minutes and `SIGCONT` after (Linux, ten
lines). If recognition moves to the web box (§12 option b), the
singleton becomes purely sequential and this paragraph disappears.

The broker lives on the web box and never on the singleton: a broker
destroyed with the machine takes the queue and every in-flight job with
it.

---

## 3. Queues and workers

vhost `vsw`; default direct exchange; classic durable queues (as
VideoScoreSync declares them, `consumer_base_queue.py:44`);
`prefetch_count=1`.

| Queue | Host / interpreter | Payload (§4) needs | Does | Publishes | Completion check |
|---|---|---|---|---|---|
| `vsw.extract` | web / app env | `upload_key`, `upload_bytes` | One ffmpeg pass over the upload (local at the small end, presigned GET stream at the large end): `-vn -ac 1 -ar 22050 -c:a pcm_s16le` → `audio.wav`; PUT `audio/<job>/audio.wav`; event `extracted`. | `vsw.identify` | HEAD `audio/<job>/audio.wav` |
| `vsw.identify` | singleton / engine env (§12 for the alternative) | `audio_key`, `duration_s` | Torch and both indexes (`data/amt_*_index.pkl`, 9 MB) loaded **once at process start** — the resident "serve mode" `identify.py:17-20` wished for is free with a long-lived consumer, and it is what turns 130 s cold into whatever the warm figure is. GET `audio.wav`; `identify_aggregated`; `verdict.json` → PUT `work/<job>/verdict.json`; event `identified` with the verdict. | nothing — the visitor must choose | HEAD `work/<job>/verdict.json` |
| `vsw.chroma` | singleton / engine env | `audio_key`, `package` | GET `audio.wav`; `audio2chroma` → `chroma.npy`; PUT. | `vsw.sync` | HEAD `work/<job>/chroma.npy` |
| `vsw.sync` | singleton / engine env | `package` | `wfn_combination_selector` against the package reference; the column translation and the crowding / span checks exactly as `tools/sync_runner.py:95-210` and `app/sync.py:163-180`; `measures.data` → PUT. **A partial recording fails here, permanently, before gigabytes are fetched.** | `vsw.fetch` | HEAD `work/<job>/measures.data` |
| `vsw.fetch` | singleton / app env | `upload_key`, `upload_bytes` | GET `uploads/<job>/<name>` → `input/video.<ext>`, the `download_with_retry` shape of `consumer_download_video_queue.py:39-57`; verify size; skip if already present at that size. | `vsw.embed` | local file at the right size — this output *is* local |
| `vsw.embed` | singleton / app env (cairo) | `package`, `style`, `meta`, `mode` | `render.render()` unchanged in substance; ffmpeg writes `<stem>.attempt<N>.part.mp4`, Python renames on exit 0; PUT `results/<job>/…`; event `done` with key, bytes, elapsed. | nothing — `done` is an event | HEAD `results/<job>/…mp4` |
| `vsw.events` | web / app env | — | Applies `{job_id, stage, event, attempt, host, started, finished, upload_bytes, duration_s, detail, error, kind}` to `jobs` and `stage_runs`; on `embed.done` / `failed(permanent)` publishes `vsw.notify`; recomputes positions and ETAs (§11.2). | `vsw.notify` | upsert on `(job_id, stage, attempt)` |
| `vsw.notify` | web / app env | `job_id`, `kind ∈ {queued, ready, failed}` | `notify.send_*` gated by `mail_<kind>_at IS NULL` (§13.2). | — | the table |
| `vsw.dead` | web; `tools/queue.py` | — | DLX target of every work queue; read by a person. | — | — |

`identify.py:43`'s semaphore is retired: one identify process with
prefetch 1 is the same guarantee, across machines. There is no
`MAX_CONCURRENT_*` anything — one instance, one heavy ticket, by
construction.

### 3.1 What is copied from VideoScoreSync — verified

| File | Verdict |
|---|---|
| `workers/consumer_base_queue.py` (92 lines), `workers/publisher_base_queue.py` (73) | **Copy, then change** (§8). Importing is impossible in practice: both `import config`, and `config.py:128-136` does `int(os.getenv("PORT_PREPROCESSOR"))` with no default, so the import needs VideoScoreSync's whole `.env` in our process. They also connect as guest (`consumer_base_queue.py:35-42`) and publish without confirms (`publisher_base_queue.py:48-49`). |
| `consumer_extract_audio_queue.py`, `_resample_audio_queue.py`, `_generate_chroma_queue.py`, `_sync_video_queue.py` | **Skeleton yes; the bodies already exist here.** Resample is `ffmpeg -y -i IN -ar 22050 OUT` (`:50-55`); chroma is one `audio2chroma` call (`:49`); sync is one `wfn_combination_selector` call plus a write (`:72-83`); extract re-encodes to AAC m4a (`services/audio_extraction_service.py:173-186`). `tools/sync_runner.py` already calls both services with the column translation and the partial-recording checks. Porting is wrapping. |
| `consumer_download_video_queue.py` | **Shape yes, body no.** Keep `download_with_retry` (`:39-57`) and the done-flag idea (`:78-82`); the body becomes a bucket GET. `:84-88` **acks** a message with a missing `score_id` and counts a failure — the job vanishes and nobody is told; §7 replaces that. |
| `consumer_embed_score_queue.py` | **Not reused**, as the owner said: `ScoreVideoMaker(task)` is shaped for the Dropbox flow and knows nothing of ink colour, crop offset, panel or background; `app/render.py` is ours. |
| `services/worker_metrics.py` | **The idea, not the file.** `observe_task_duration_per_gb` (`:107-124`) is one axis; we record elapsed, bytes **and** duration per stage (§5). It needs `psutil` and `PROMETHEUS_MULTIPROC_DIR`; Prometheus exposure comes later. |
| `models/task_data.py`, `helpers/task_utils.py::get_job_paths` | **Replaced** (§4): 19 competition fields; ~15 `config.*` constants for a layout that is not ours. |
| identify consumer | **New.** ~80 lines around `tools/identify_runner.py`'s logic, resident. |

Not inherited: `basic_nack(requeue=False)` on every exception (a
network blip and a missing audio track treated alike — §7), and the
done-flag honoured only `if method.redelivered` (§6.2).

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
    upload_key: str | None = None    # uploads/<job>/<name>
    upload_bytes: int | None = None
    upload_ext: str | None = None
    duration_s: float | None = None  # ffprobe at upload; one ETA axis
    audio_key: str | None = None     # audio/<job>/audio.wav
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
as arguments to `Registry.start()` (`jobs.py:143-144`).

### 4.2 `app/queue/paths.py`, `app/queue/keys.py`

```python
@dataclasses.dataclass(frozen=True)
class JobPaths:                       # WORK_DIR/<job>/ on either host
    dir: Path
    video: Path                       # input/video.<ext>          (singleton; fetched)
    audio: Path                       # audio.wav                   (web makes it; singleton caches it)
    chroma: Path; measures: Path; verdict: Path
    result: Path                      # <pipeline._safe(package)>_synced.mp4
    log: Path
    def part(self, attempt: int) -> Path        # <stem>.attempt<N>.part.mp4
    def flag(self, stage: str) -> Path          # <stage>_done.flag — a cache of "the output is in S"

def keys(job_id: str) -> Keys:
    uploads/<job>/<name>    audio/<job>/audio.wav
    work/<job>/verdict.json  work/<job>/chroma.npy  work/<job>/measures.data
    work/<job>/<stage>_attempt<N>                    # zero-byte attempt markers (§6.3)
    results/<job>/<stem>_synced.mp4
```

The score library must exist on the singleton (sync reads
`performance/chroma.npy` + `measures.data`; embed reads `lines/*.svg`,
`export.json`): baked into the image, refreshed from `scores/` in the
bucket at start.

---

## 5. The job table — companion to the broker

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
CREATE TABLE stage_runs (                                       -- the calibration record
  job_id TEXT, stage TEXT, attempt INTEGER, host TEXT,
  started REAL, finished REAL, ok INTEGER, error TEXT,
  upload_bytes INTEGER, duration_s REAL,                        -- both axes, every row
  PRIMARY KEY (job_id, stage, attempt)
);
CREATE TABLE calibration (stage TEXT PRIMARY KEY, k0 REAL, k_d REAL, k_g REAL, samples INTEGER, updated REAL);
CREATE TABLE compute (                                          -- the scaler's lease (§9.4); at most one live row
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  state TEXT,                                                   -- creating | running | destroying
  instance_id TEXT, vlan_ip TEXT, created REAL, ready REAL, idle_since REAL,
  broker_user TEXT, store_key_id TEXT, lease_until REAL
);
```

`Job` / `Registry` (`app/jobs.py`) become a thin layer over this;
`Job.public()` keeps its shape (both front-ends read it) and gains
`position`, `eta_at`, `attempt`, `compute` (`none | creating | running`).
`rehydrate()` becomes a `SELECT`.

**Who owns what:**

| Question | Owner | Never asked of |
|---|---|---|
| What should happen next for job J? | **the broker** — a message in `vsw.X` is an obligation to make stage X happen | the table |
| Has stage X already happened? | **the bucket** — HEAD on the stage's output; the local flag is a cache | the table (singleton processes never read it) |
| Where are the bytes? | **the bucket** | local disks |
| What state is J in, where in line, when ready, was it mailed, how long do stages take? | **the table**, derived from `vsw.events` | the broker |
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
    running --> done: results/… in S; event done
    done --> notified: mail_ready_at set
    failed --> notified: mail_failed_at set
    uploaded --> abandoned: no submit within UPLOAD_TTL_DAYS
```

One protocol for every stage:

```
on message (task):
    if output_in_bucket(stage) or flag(stage).exists():     # every message, not only redelivered ones — §6.2
        publish next(task); ack; return                     # safe: the next stage has the same guard
    n = count_markers_in_bucket(stage) + 1                  # crash-attempt counting that survives destroy — §6.3
    PUT work/<job>/<stage>_attempt<n>
    if n > cap(stage): event failed(exhausted); nack(requeue=False); return
    event started
    do the work locally
    PUT outputs to the bucket                               # the point of no return
    touch flag(stage)
    publish next(task) | event done/identified
    event finished(elapsed, upload_bytes, duration_s)
    ack
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
Order: outputs → bucket → flag → publish → ack. With one batch process
and prefetch 1, a duplicate that arrives during the work simply waits
and then finds the output.

### 6.3 Crash matrix — including the singleton being destroyed under the job

"Crash" = the process dies without raising (kill, OOM, the instance is
destroyed). A raised exception is a *failure*, §7.

| State | Crash of | Durable | Recovery |
|---|---|---|---|
| uploading | web box | partial multipart in the bucket | Multipart survives the web box; the browser resumes (§10.2); `abort-incomplete` rule after 1 day. |
| uploaded / extracting | web box | upload; maybe `audio.wav` | Durable message; on restart the consumer HEADs `audio.wav` and skips or redoes. |
| identifying | singleton destroyed | audio | Heartbeat requeues the unacked message within ~2 min (§8.2); the scaler's tick sees `ready > 0`, creates a new instance; re-run. The page keeps polling; copy says "still listening". |
| queued | anything | messages in `vsw.chroma` | Nothing ran. Position recomputed. |
| running: chroma / sync / fetch | singleton destroyed | outputs of finished stages | Requeue; new instance; finished stages skipped by HEAD; fetch re-downloads (the disk is gone). |
| **running: embed, ffmpeg dies with the box** | singleton destroyed | `chroma.npy`, `measures.data`, markers | New instance: chroma/sync skipped by HEAD, fetch redone (k_fetch·G), **embed re-run from the encode only**; marker `embed_attempt2` in the bucket; a partial `.part` never became a result because only Python renames, only on exit 0. |
| running: embed, ffmpeg dies but the consumer lives | — | as above | A failure → §7. |
| done, mail not sent | web box | result; `mail_ready_at NULL` | `vsw.notify` durable; sent once on restart (§13.2). |
| done, mail `sending` | web box | `mail_state='sending'` | **Not resent**; `uncertain`, WARNING. A duplicate to a stranger's typed address is worse than a missing one when the page shows the link. |

Attempt markers live in the **bucket** because create/destroy erases the
disk: a redelivered message carries the same `attempt` as before, and
the consumer counts markers rather than trusting the payload.

---

## 7. Failure policy

| Kind | Examples (all already produce user-safe messages) | Action |
|---|---|---|
| **permanent** | no audio (`identify.py:163`), too short (`:175`), partial recording (`sync.py:168-180`), no measures (`sync_runner.py:159`), "the alignment doesn't cover this video" (`render.py:426`), unknown package, ffmpeg rejecting the input | event `failed(permanent, message)`; `basic_nack(requeue=False)` → `vsw.dead`; the visitor is **mailed why** (§13.2) |
| **config** | runner `kind: "config"` (`identify_runner.py:31`, `sync_runner.py:38`), missing interpreter or index, LLVM/SVML abort (`sync.py:210-214`) | as permanent, plus ERROR log; the mail says "our side" |
| **transient** | bucket GET/PUT errors, broker publish errors, ffmpeg killed by signal / exit 137 / "Cannot allocate memory", `RENDER_TIMEOUT` | `sleep(RETRY_DELAY_SECONDS)` in the consumer, republish with `attempt+1`, ack the original; at the cap → `exhausted` |

```ini
RETRY_DELAY_SECONDS=60
MAX_ATTEMPTS_CHEAP=3        # extract, identify, chroma, sync, fetch
MAX_ATTEMPTS_EMBED=2        # one retry — and with §6.3 that retry is the encode only, never the alignment
```

A cap of three on an hour-long render is indefensible only if a retry
repeats the hour of *everything*. It does not: chroma and sync are kept
in the bucket, so an embed retry is the encode (plus a re-fetch if the
instance was destroyed). Two attempts is right for that — a second
identical failure is a signal. ffmpeg can resume nothing; segmented
encoding is not proposed (§15).

| Situation | Visitor | Operator |
|---|---|---|
| transient, retry scheduled | `/status`: `queued`, "Something went wrong on our side; trying again in a minute (attempt 2 of 2)"; no mail | WARNING; `tools/queue.py` shows the attempt |
| permanent | `failed` + the stage's message; **a mail** saying the same, because they were told to walk away | INFO; message in `vsw.dead` with headers `x-job`, `x-stage`, `x-kind` |
| config | as permanent; the mail says it is our fault | ERROR; `vsw.dead` |
| exhausted | "The render failed twice and has been stopped. Your upload is kept for N days." | ERROR; `vsw.dead` |

`tools/queue.py`: read-only listing of the table, the queue depths and
`vsw.dead`, in the style of `tools/doctor.py`; one explicit write
action, `--republish <job> <stage>`.

---

## 8. Consumer mechanics — what changes in the copied base classes

### 8.1 Copy, do not import

§3.1: `import config` at `config.py:128` fails without VideoScoreSync's
`.env`; guest credentials; no confirms. Copy both into
`app/queue/amqp.py` (~150 lines after the edits below).

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
(pika's `BlockingConnection` is not thread-safe). About 40 lines.

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

(3.12+ also takes a per-queue `x-consumer-timeout`.) Verify the
installed version before step 4.

### 8.4 Other changes to the copies

- Credentials and vhost from `.env` (`RABBITMQ_URL=amqps://user:pass@10.0.0.2:5671/vsw`), never guest.
- **Publisher confirms** (`channel.confirm_delivery()`) and a raised
  error on failure, replacing "publish without confirmation, trust the
  broker" (`publisher_base_queue.py:48-49, 63`). A consumer that cannot
  publish its successor must not ack.
- One long-lived publishing channel per process instead of a connection
  per message with a `sleep(0.1)` (`:35-59`).
- DLX on every work queue: `x-dead-letter-exchange: vsw.dlx` → `vsw.dead`.
- The unconditional completion check and re-publish-on-complete (§6.2).
- The batch process consumes four queues on one channel with a
  channel-level `prefetch_count=1` (`global=True`), which is what makes
  "one heavy ticket at a time" structural rather than conventional.
- Keep `handle_shutdown_signal` (`consumer_base_queue.py:90-93`): a
  Linode shutdown becomes SIGTERM via supervisord, the connection closes
  cleanly, the unacked message is requeued at once.

### 8.5 The local-development escape hatch

VideoScoreSync gates a non-broker path on `USE_RABBITMQ`
(`publisher_task_manager.py:72-77`). It rotted the way such paths do:
the inline path calls `handle_sync_video` in `task_manager.py:179-218`
while the broker path calls `process_sync_video` in
`consumer_sync_video_queue.py:45-97` — two copies of one body, the
inline one without flags or acks.

Keep **one** escape hatch, structured so it cannot diverge:

```ini
QUEUE_TRANSPORT=amqp | inline
```

Every stage is a pure function `handle_<stage>(task, paths, store) -> Task | Event`.
The AMQP consumer is a 30-line shell around it; the inline transport is
a different 30-line shell running the same handler on a thread pool in
one process, same flags, same events into the same table. The bodies
exist once. Inline is also **the single-machine deployment of steps
1–3**, so it is not dead code; Docker Desktop runs the real broker on
Windows for the integration test before each release (the owner already
has `windows.docker` in `config.py:17`).

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
| CPU-only, everything | Debian; ffmpeg; an `app` env (Pillow, cairosvg + cairo, boto3, pika, this repo); an `engine` env (torch **CPU** wheel ~200 MB, the 165 MB transcription checkpoint, librosa, soundfile, numba **with SVML** — `icc_rt` from conda or pip numba; the abort at `sync.py:210` is exactly this condition, test `audio2chroma` before baking); checkouts of VideoScoreSync (`api_audio` + two services) and music_finrgerprint (`src/weefeen_id`, 9 MB indexes); the score library; supervisord | ~2.5–3.5 GB | yes |
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
scaler refuse a stale one.

### 9.2 Isolation — Linode's "private IP" is not private to the account

Linode private addresses are reachable by **every Linode in the same
data centre**. Binding RabbitMQ there would put every job on a network
shared with strangers. The boundary is a **Linode VLAN** (account-
isolated Layer 2; region-dependent — **verify the region**) plus a
**Cloud Firewall** on each box.

- Web box: VLAN interface `10.0.0.2/24` (adding one to an existing
  Linode needs a reboot); RabbitMQ bound to `10.0.0.2` only — never
  `0.0.0.0`, never the shared private IP; management API on loopback.
  Cloud Firewall: public inbound 22 from the admin address, 80/443 from
  anywhere, drop the rest.
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
   on `^vsw\.(sync|fetch|embed|events)$`. Object storage:
   `POST /v4/object-storage/keys` with `bucket_access` limited to the
   `vsw` bucket. Both deleted on destroy; `tools/queue.py --gc` sweeps
   any left by a crashed destroy.
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
| Destroy fires just as a ticket is published | Immediately before `DELETE`, one last management-API read; if anything is ready or unacked, abort the destroy. The remaining window is that read's latency. If a message still lands inside it: not yet consumed → stays `ready`, the next tick creates again (~2–3 min of latency, nothing lost); consumed and unacked → SIGTERM on shutdown requeues it at once, or the heartbeat does within ~2 min. |
| Destroy call fails | Row stays `destroying`; retried each tick; `tools/queue.py --gc` for the rest. |
| Web box down for an hour | The singleton keeps draining (it talks to the broker, which is down too — no: the broker is on the web box, so consumers reconnect with backoff as `consumer_base_queue.py:64-70` already does, and finish what they hold when it returns). Unacked work is requeued by the reconnect and skipped by HEAD. |

The loop, every `COMPUTE_POLL_SECONDS`:

```
depths = management API: messages_ready, messages_unacknowledged per vsw.{identify,chroma,sync,fetch,embed}
row    = compute (or none)
if any ready > 0 and row is none:                        → claim lease; CREATE (image, type, region, VLAN .3,
                                                           firewall, user_data with fresh credentials, label)
if row.running and all ready == 0 and all unacked == 0:  → idle_since = idle_since or now
                                                           if now − idle_since ≥ COMPUTE_GRACE_SECONDS: re-read; DESTROY; revoke
else:                                                    → idle_since = null
write compute.state into the table for /status ("compute": none | creating | running)
```

```ini
COMPUTE_MODE=auto                  # auto | always | never   (never = single-machine deployments)
COMPUTE_POLL_SECONDS=10
COMPUTE_GRACE_SECONDS=1800         # §9.5
COMPUTE_CREATE_SECONDS=120         # what the UI and the ETA quote while creating; calibrated from the compute table
COMPUTE_TYPE=g6-dedicated-8  COMPUTE_REGION=  COMPUTE_IMAGE=private/…  COMPUTE_VLAN=vsw  COMPUTE_FIREWALL_ID=
LINODE_TOKEN=                      # scoped: linodes read/write, images read, firewalls read, object-storage keys read/write
```

`LINODE_TOKEN` is the most powerful secret in the system; it lives only
on the web box.

### 9.5 The grace period — the primary tunable

The cold start is paid **once per idle gap**, not once per job:
arrivals cluster (someone shares the link, several people try it the
same evening), and a visitor who arrives while the instance is up gets
a warm recognition (no create, no 20 s import) and an embed that starts
at once. The grace period buys that warmth with idle minutes. At an
8-core dedicated Linode's ~$0.216/h ($0.0036/min), an idle gap that ends
in a destroy costs `G × $0.0036`:

| grace → / sessions per day ↓ | 5 min | 10 min | 30 min | 60 min |
|---|---|---|---|---|
| **1** (one cluster a day) | $0.54/mo | $1.08 | $3.24 | $6.48 |
| **3** | $1.62 | $3.24 | $9.72 | $19.44 |
| **10** | $5.40 | $10.80 | $32.40 | $64.80 |

What each avoided cold start is worth: ~2 min of instance creation plus
~20 s of model load for the visitor (a warm recognition instead of a
cold one), and the visitor who identified a piece and is choosing a
style — typically a few minutes — never sees the instance vanish under
them. **Recommend `COMPUTE_GRACE_SECONDS=1800` (30 min):** under $10/month
at three sessions a day, and it covers both the "same evening" cluster
and the identify-then-submit pause. If Linode bills any started hour in
full (**verify**), replace the fixed grace with "destroy at the end of
the hour already paid for, but not sooner than 30 minutes idle"
(`COMPUTE_ALIGN_TO_BILLING_HOUR=true`).

---

## 10. Artifacts: how bytes cross

### 10.1 Linode Object Storage, not Dropbox

VideoScoreSync's Dropbox flow (`api_dropbox/`, watch → processing → done
folders at `config.py:251-256`) is a **human** handover for the Cliburn
team. Here the counterpart is a browser and two machines. S3-compatible
storage gives presigned browser PUTs with CORS, multipart uploads that
resume, presigned GETs for the link, lifecycle rules, and near-free
transfer within the region. Linode Object Storage (Ceph RGW, S3 API,
~$5/month for 250 GB and 1 TB transfer) fits; `boto3` is the one new
dependency.

### 10.2 Upload — parametric in the caps

**Small end (≤ ~2 GB):** as today — browser → Flask (`MAX_CONTENT_LENGTH`)
→ the web box PUTs to `uploads/<job>/` and deletes its copy after
extract. Scratch disk equal to the cap.

**Large end:** the browser talks to the bucket directly; the web box
sees only metadata:

```
POST /api/uploads        {name, bytes, rights: true}
    limits.guard("upload_ip") · rights asserted here (moves from routes.py:381)
    bytes ≤ MAX_UPLOAD_GB · provisional weekly-GB budget check
    INSERT jobs(state=uploading) · CreateMultipartUpload → {job_id, upload_id, part_size: 64 MiB, parts: N}
GET  /api/uploads/<id>/part/<n>   → one presigned PUT (presigned lazily; 10 GB = 160 parts)
GET  /api/uploads/<id>/parts      → ListParts, so a reloaded page resumes
POST /api/uploads/<id>/complete   {etags}
    CompleteMultipartUpload · HEAD confirms size
    ffprobe over a presigned GET (range reads: header + moov, a few MB even for a non-faststart file)
    has_audio · duration ≤ MAX_DURATION_MINUTES · weekly-minutes budget check
    state=uploaded · publish vsw.extract · return {job, eta}
```

Admission happens twice: before any byte (address limit, rights,
declared size) and after the probe (duration, both budgets). A file
failing the second check is deleted and the visitor told why — one
upload's cost, as at `routes.py:404-416` today. Resumability is S3
multipart itself: independent parts, retried individually, listable
after a reload; no `tus` server; an `abort-incomplete-multipart-upload`
rule of 1 day cleans abandoned ones.

### 10.3 Download link

`/api/jobs/<id>/download` stays the address in the mail; it answers
`302` to a presigned GET valid 15 minutes, generated per click. Stable,
revocable, expiring with the lifecycle rule — as `notify.py:54` already
promises.

### 10.4 Lifecycle, and what dies with the singleton

```
uploads/ 7 days      audio/ work/ 30 days      results/ 30 days
```

Every stage's output reaches the bucket before its flag, so a destroyed
instance loses exactly the stage that was running. Its disk is a cache.
Storage at the large end: a 60-minute result at today's output bitrate
(1.9 Mbit/s) is ~0.85 GB, ten a day for 30 days ≈ 250 GB; uploads at
10 GB × 7 days dominate, ten a day ≈ 700 GB — a few dollars a month
either way; egress is the visitors' downloads, cents.

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

One instance drains sequentially, so "ahead" is everything unfinished,
in queue order:

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

Compute-hours per week = jobs × est / 60, with est at 1 GB per job
(add 7 min per extra GB while the owner's per-GB term stands) plus the
cold start and grace **once per session** — here assumed at one session
per five jobs (2 min create + 30 min grace ≈ 6.4 min per job).

| min/job ↓ · jobs/week → | 5 | 20 | 50 | 100 | 200 |
|---|---|---|---|---|---|
| **5** | 1.6 h | 6.3 h | 16 h | 32 h | 63 h |
| **15** | 2.3 h | 9.0 h | 23 h | 45 h | 90 h |
| **30** | 3.3 h | 13 h | 33 h | 65 h | 130 h |
| **60** | 5.3 h | 21 h | 53 h | 107 h | 213 h ✗ |
| **90** | 7.4 h | 29 h | 74 h | 148 h | 295 h ✗ |

✗ = more than a week holds; a bigger plan (the singleton stays one).
Dollars: ~$0.216/h → **$/month ≈ h/week × 0.94**; always-on the same
plan is $144/month ≈ 150 h/week. Every cell but ✗ is cheaper created on
demand; the owner's "handful a day" (≤ 50/week, mostly ≤ 30 min) is
**$3–35/month** of compute plus $12 for the page. Prices are
placeholders for the current list.

Saturation of the current limits: 3 jobs/week/IP × 90 min = 270 min per
visitor per week; **37 visitors** at full allowance fill the singleton
24/7; at typical 10-minute pieces, 336. Under create/destroy
"saturation" means a growing queue and bill, which §11.5 caps.

### 11.4 Budgets on both axes — replacing the job count

`limits.py:67-72` counts **jobs**. Three jobs is 15 minutes of machine
or 4½ hours, 50 MB or 30 GB; the count bounds neither axis. Replace it:

```ini
# Per visitor (by address, and by email), sliding week, as limits.py already counts.
LIMIT_DURATION_MINUTES_PER_WEEK=60    # three 20-minute pieces, or one recital
LIMIT_UPLOAD_GB_PER_WEEK=6            # 3 × MAX_UPLOAD_GB at the small end; bites at the large end
LIMIT_RENDERS_PER_WEEK=10             # kept only as a floor: every job pays a fixed ~2 min that neither axis
                                      # captures; 10 never binds for a person
# Per job (§1)
MAX_UPLOAD_GB=2
MAX_DURATION_MINUTES=90
```

Arithmetic: 60 video-minutes ≈ 2 + 0.8 × 60 ≈ 50 processing-minutes ≈
$0.18 of compute per visitor per week; 6 GB ≈ 42 minutes more if the
per-GB term holds, ≈ $0.15; plus ≤ 6 GB × 7 days in the bucket. Ten
such visitors a week: **under $20/month**. The duration budget is
checked at `POST /render` against `duration_s` (exact) and both budgets
provisionally at `POST /api/uploads`. A permanent failure still spends
the budget — refunding it would make "upload a partial recording" a
free way to burn compute; the visitor is told why and the upload is
kept.

### 11.5 Admission control in minutes

```ini
MAX_QUEUE_MINUTES=600     # accepted-but-unfinished processing minutes, all visitors
```

At submit, if `cold + remaining(R) + Σ est(ahead) + est(new) > MAX_QUEUE_MINUTES`,
refuse with `503` + `Retry-After`: "The queue is about N hours long
right now. Your upload is kept for 7 days — try again after HH:MM." Ten
hours rather than four because delivery is by mail (§13.2): "by
tomorrow morning", said honestly, is served; the cap bounds the bill and
the bucket, not the visitor's patience.

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
| **(a) on the singleton** (default) | ~2 min create + warm recognition on 8 dedicated cores (est. 60–110 s; the 130 s cold figure minus ~20 s of import/load, on a stronger CPU — **measure**) ≈ **3–4 min** | warm recognition only ≈ **1–2 min** | none extra | Recognition and an in-flight encode share the cores (§2's `SIGSTOP` if it matters). The singleton is created at `…/complete`, so an abandoned upload costs one create + grace ≈ $0.12. |
| **(b) on the web box** | recognition on the small plan's CPU, no create: est. **2–5 min** on 2 shared vCPUs, less on a dedicated plan — **measure** | same | web plan 2 GB → 4 GB, +$12/month shared or +$24 dedicated (torch CPU + checkpoint need ~2 GB resident) | A recognition can make the site sluggish; serialise with the existing `identify.py:43` semaphore (kept, on the web box) and a queue depth of one or two, refusing beyond with "busy, try in a minute". The singleton becomes purely batch and strictly sequential, created at submit rather than at upload. |
| (d) non-blocking: accept, recognise with everything else, mail "we think it is X — confirm or choose" | never waits | never waits | cheapest | A product change: the visitor returns once. **Decision E, the owner's.** |
| (e) a torch-free first guess on the web box | seconds | seconds | nothing | music_finrgerprint's librosa-only pipelines (`pipeline.py`, `pipeline_v4.py`; indexes `data/index.pkl`, `chord_index.pkl`, `pitch_index.pkl` present). **I do not know why AMT superseded them; the owner does.** If their accuracy was acceptable, this alone keeps "a few seconds" literally true, with AMT confirming later on the singleton. A day's experiment. |

**Recommendation: (a) by default, with a 30-minute grace (§9.5), and a
measurement gate for (b).** Reasoning: with the grace, every visitor
but the first of an evening finds the instance up and waits one to two
minutes; the first waits three to four. (b) makes *every* visitor wait
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
- recognising: **"Listening — about a minute and a half."** with the
  number from calibration;
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
| `ready` | on `embed.done` | the link (`notify.py:37-73`, unchanged in substance), how long it is kept |
| `failed` | on `failed(permanent | config | exhausted)` | the stage's own message; for config, "our side" |

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
- No address verification (§15); the render limits already gate the
  address, and a typed address remains the abuse vector `limits.py:73`
  describes.

---

## 14. Migration path — each step shippable and verified; step 1 is one machine

**Step 1 — job table and one-at-a-time rendering, one machine, no broker
(~250 lines).** `jobs.sqlite` with `jobs`, `stage_runs`, `calibration`;
`Job`/`Registry` over it; state `queued`; one render worker thread with
a FIFO; `position` and `eta_at` exactly as §11.2 with `cold = 0` and
§1's defaults; `routes.py:459` refuses `queued` too; both UIs show "N in
line, ready by about HH:MM"; SSE removed, `/status` polled;
`MAX_DURATION_MINUTES` enforced at upload beside the existing byte cap.
*Verify:* two submits → one ffmpeg; restart with two queued → both
resume in order; `stage_runs` has a row with elapsed, bytes **and**
duration; the ETA shown at submit is within the quoted range of the
actual.

**Step 2 — extract-audio and the stage handlers (~300 lines).** `Task`,
`JobPaths`, `handle_*` as pure functions; completion checks, attempt
markers, `.attemptN.part` outputs; transient/permanent classification
and caps; the runners take `audio.wav`; `QUEUE_TRANSPORT=inline` runs the
chain in-process (interpreters still reached through the existing
runner subprocesses); recognition on CPU with `CUDA_VISIBLE_DEVICES=-1`
in the test.
*Verify:* verdict and `measures.data` identical to before
(`tools/compare_alignments.py`, byte for byte, per `README.md`); kill
ffmpeg mid-encode → retried once, **no re-align**; a no-audio upload
fails permanently with no retry.

**Step 3 — mail as delivery, budgets on both axes, admission (~200
lines).** Three mail kinds with the table gate and backoff; `notify.py`
docstring; `LIMIT_DURATION_MINUTES_PER_WEEK`, `LIMIT_UPLOAD_GB_PER_WEEK`,
`MAX_QUEUE_MINUTES`; fairness rank; per-job mail counting.
*Verify:* kill between done and mail → exactly one `ready`; kill during
`sending` → none, `uncertain`; over-budget on either axis refused with
the right copy; A with three uploads and B with one → B second in line.

**At the small end (§1.1) one always-on box stops here. Below is
contingent on wanting the compute box to exist only while working.**

**Step 4 — RabbitMQ on the same machine (~350 lines).** Copy and fix
the base classes (§8); the identify process and the four-queue batch
process under supervisord; DLX and `vsw.dead`; `vsw.events` → the
table; `consumer_timeout` raised; `QUEUE_TRANSPORT=amqp`. Docker Desktop
on Windows for development.
*Verify:* `kill -9` the batch process mid-encode → requeued after the
heartbeat, finished stages skipped, `embed_attempt2` marker present; a
permanent failure lands in `vsw.dead` **and** the visitor gets the
`failed` mail; a 40-minute sleep in embed does not trip the
acknowledgement timeout; two jobs submitted together run their heavy
stages strictly one after the other.

**Step 5 — object storage, still one machine (~250 lines).** Keys,
PUT/GET/HEAD in every handler, markers in the bucket, download `302`,
lifecycle rules.
*Verify:* delete the local job directory after `done` → download still
works; delete `chroma.npy` locally and republish sync → fetched from the
bucket, not recomputed.

**Step 6 — the second Linode and the scaler (~400 lines + the image
script).** VLAN, Cloud Firewalls, broker rebound to the VLAN address
with TLS; per-instance credentials via user_data; `deploy/compute-image.sh`;
`app/scaler.py` with the lease row and label guard; `COMPUTE_MODE=auto`;
**measure recognition on both plans and settle decision D**; the copy
of §12.
*Verify:* destroy the singleton by hand mid-embed → a new one within
~3 minutes and the job resumes at the encode; grace-period destroy
observed; an upload completed with no instance has one running before
extract finishes; start a second scaler by mistake → it stands down; the
destroyed instance's broker user and storage key are gone; two uploads
completing in the same second → exactly one instance.

**Step 7 — large end only: direct-to-bucket upload with resume (~300
lines, mostly browser JS).** §10.2 endpoints; `UPLOAD_DIRECT=true`;
ffprobe over a presigned GET.
*Verify:* a 5 GB upload interrupted at 60 % resumes after a reload
without re-sending parts; a declared 20 GB is refused before any byte.

**Contingent:** Prometheus exposure on the consumers; `SIGSTOP` priority
for recognition if contention is measured; the librosa-only first guess
(§12 e).

---

## 15. Explicitly not proposed

- Publishing into VideoScoreSync's queues, or importing its consumers.
- A broker on the singleton.
- Powering off instead of destroying; a GPU anywhere; more than one
  compute instance.
- Binding anything to Linode's shared private IP.
- Long-lived secrets in the image, except as §9.3's last resort.
- SSE of any kind.
- Segmented / resumable encoding.
- Refunding budget on a permanent failure.
- Address verification before mailing.
- An operator web UI or auth; `tools/queue.py` on the box.
- A cancel endpoint; the grace period and lifecycle rules bound the cost
  of an abandoned job.
- Merging or further splitting `vsw.chroma` / `vsw.sync`: they mirror
  the two consumers being reused and already share one process.
- A per-visitor "one at a time" refusal; the fairness rank does the same
  job without refusing anyone.
- Quorum queues, Prometheus/Grafana in the first seven steps.

---

## 16. What I am not sure about

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
  unfiltered, whether partial hours are billed in full, and whether
  label uniqueness is enforced at create time as I expect. Each has a
  fallback in §9; none changes the shape.
- **`ffprobe` over a presigned GET on a non-faststart 10 GB MP4** —
  unmeasured; fallback: probe on the singleton in `fetch`.
- **`consumer_timeout`** depends on the broker version installed.
- **Whether every ffmpeg OOM presents recognisably**; the embed cap of 2
  bounds a wrong guess.
- **The SVML condition** for the `engine` env (`sync.py:210-214`); test
  `audio2chroma` in the image before baking.
- **`Job.save()` / `rehydrate()`** (`jobs.py:66-79, 250-284`) landed in
  `c718193` at 10:43 today and the only job on disk finished at 10:41
  without a manifest; step 1 replaces both with the table.
