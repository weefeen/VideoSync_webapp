# Slice 1 — the worker out of the Flask process, RabbitMQ between them, one node

The design, and a record of what was built from it. Kept as written
rather than rewritten to match the result: where the build departed
from the plan, the plan still says what it said and the step says why.

**Status: all four steps of §14 are done and proved on the dev node
(`deployment-log.md` §12 to §15). §12's contradictions are settled and §13's
unknown is answered for the dev node — RabbitMQ 3.12.1 accepts a per-queue
`x-consumer-timeout`, so no shared broker's configuration has to change. The
production broker's version is still unchecked.**

**What this slice does NOT give you:** a second host, scale-to-zero, Docker,
or gunicorn. It gives one machine running the two halves as separate
processes with a broker between them, which is the arrangement the second
host becomes a configuration change from.

Scope of this slice: split the worker out of the Flask process **on a single
node**, with RabbitMQ between them. One host, same storage, same code paths.
The point is to prove the message contract — handoff, progress, results,
failures, redelivery — so that moving the worker to a second host later is
configuration rather than a rewrite.

Out of scope: Docker, the second host, scale-to-zero, gunicorn, the bot
check, Apache/TLS/DNS.

---

## 1. The shape

```
 WEB PROCESS  (python run.py)                             WORKER PROCESS  (python -m app.queue.worker)
 ┌────────────────────────────────────────────┐          ┌────────────────────────────────────────────┐
 │ routes.py  POST /render → row (queued)     │          │ connection thread: pika BlockingConnection │
 │            → publish RenderTask ───────────┼─ vsw.render ─▶ hands task to the work thread          │
 │            /status, /events read SQLite    │ (durable, │   drains the event outbox, heartbeats, acks│
 │                                            │ prefetch 1)│ work thread: worker.handle_task()         │
 │ queue/webside.py                           │          │   attempt record → pipeline.run(on_progress)│
 │   applier: consumes vsw.events → apply() ◀─┼─ vsw.events ── started/progress/heartbeat/done/failed │
 │   janitor: every 30 s reclaim, republish   │ (durable) │                                            │
 │ jobs.sqlite — written ONLY by this process │          │ never opens jobs.sqlite                    │
 └────────────────────────────────────────────┘          └────────────────────────────────────────────┘
              same disk: WORK_DIR/uploads, WORK_DIR/<job_id>/, score packages
```

`QUEUE_TRANSPORT=local` collapses the right box into a thread of the left one,
with two in-memory queues instead of the broker. Nothing else differs (§7).

The contract in one sentence: **the broker carries obligations downward
(`vsw.render`) and facts upward (`vsw.events`); SQLite is the web side's view
of those facts; the worker has no view of SQLite at all.** That last clause is
what makes host two a configuration change.

---

## 2. Topology

**One queue for the whole render, not a chain per stage.** VideoScoreSync
chains queues because each stage is a different program with a different
library and a different restart domain. Here `pipeline.run` runs every stage
in one process on one working directory (alignment is a subprocess it spawns
itself). A chain now would mean five queues, four inter-stage messages, five
completion guards and a decision about which interpreter owns each stage —
which is the *next* slice. Splitting `vsw.render` into
`vsw.chroma → vsw.sync → vsw.embed` later is additive: a new consumer
publishes downstream and the queue names are configuration.

Declared by **both** sides at connect, through one function
`app/queue/transport.py::declare(channel)`, so the arguments cannot drift —
the VideoScoreSync habit, kept. All durable, default direct exchange, routing
key = queue name. No fanout, no topic.

| Name | Kind | Arguments |
|---|---|---|
| `vsw.render` | queue | `x-dead-letter-exchange: vsw.dlx`, `x-consumer-timeout: 10800000` (3 h), `x-max-priority: 10` |
| `vsw.events` | queue | `x-dead-letter-exchange: vsw.dlx` |
| `vsw.dlx` | exchange, direct | — |
| `vsw.render.dead`, `vsw.events.dead` | queues | — |

Why all three arguments on day one: RabbitMQ refuses to redeclare an existing
queue with different arguments (`PRECONDITION_FAILED`), so changing any of
them later means deleting the queue. The consumer timeout is mandatory (§5).
`x-max-priority` is what keeps the existing `priority` column and
`store.position()`'s ordering truthful if priority is ever non-zero, and costs
nothing while it is zero.

Prefetch: `vsw.render` consumer `prefetch_count=1` — with one worker process
that *is* the "1 concurrent render" cap. `MAX_CONCURRENT_RENDERS` stops
meaning threads and starts meaning worker processes; it stays 1 and the worker
refuses to start with anything else in this slice (§5 says why).
`vsw.events` consumer `prefetch_count=100`, acked after each SQLite commit.

Messages: `delivery_mode=2`, `content_type=application/json`,
`message_id=<job_id>:<attempt>`, `priority=<row.priority>`.

vhost `vsw`, user `vsw`, from `RABBITMQ_URL=amqp://vsw:<pw>@127.0.0.1:5672/vsw`.
Never guest, never the default vhost — production is a vhost on a broker shared
with the Symfony app next door.

---

## 3. Source of truth

| Fact | Lives in | Written by |
|---|---|---|
| A render must happen (once per attempt) | `vsw.render` | web, at submit and by the janitor |
| Everything needed to run it | the `RenderTask` message | web, from the row at publish time |
| What the worker did, in order | `vsw.events` until applied | worker |
| State, stage, detail, position, ETA, attempt, worker, lease, retention, email, error, result | `jobs.sqlite` | **the web process only** |
| Whether *this attempt* already finished | `<job_dir>/render.attempt<N>.json` | worker |
| The bytes | `WORK_DIR/uploads/`, `WORK_DIR/<job_id>/` | worker (outputs), routes (upload) |

**The message carries the whole job, not an id.** A worker on host two cannot
read a SQLite file on host one, so a bare id would force an HTTP side-channel
to fetch the spec — a second protocol to keep alive. The email address does
**not** travel: it stays in `jobs.sqlite` on the web box and the mail is sent
by the web side on `done`, because the SMTP credentials are there and contact
details never leave that box.

### 3.1 `RenderTask` → `vsw.render`

```json
{
  "v": 1,
  "kind": "render",
  "job_id": "3f9a1c2b7d4e",
  "attempt": 1,
  "queued_at": 1789300000.0,
  "upload": "/srv/vsw/work/uploads/3f9a1c2b7d4e.mp4",
  "package": "Op.39_3ème Scherzo pour le Piano_(Breitkopf)__039-1-BH",
  "mode": "reference",
  "duration": 424.3,
  "style": { "...": "dataclasses.asdict(render.Style), verbatim" },
  "meta":  { "...": "the title-panel fields the visitor typed" }
}
```

`kind` also admits `"ping"`: the worker answers with a `pong` event and acks,
without ffmpeg. That is what makes the live-broker check runnable in seconds.
`upload` is **the one host-bound field**; in the storage slice it becomes a
bucket key and nothing else in the message changes.

### 3.2 `Event` → `vsw.events`

```json
{ "v": 1, "job_id": "3f9a1c2b7d4e", "attempt": 1, "seq": 7, "at": 1789300123.4,
  "worker": "dev-node:41213", "type": "progress", "stage": "bands", "detail": "21/83" }
```

| `type` | Extra fields | Applied as |
|---|---|---|
| `started` | — | `running`, `worker`, `started`, lease renewed; opens a `stage_runs` row. Any still-open run for the same attempt is first closed as `error`/`Interrupted` — that is how a crash-and-redelivery becomes visible without a new attempt number. |
| `progress` | `stage`, `detail` | `stage` (only when in `pipeline.STAGES`; `probe`/`panel` update `detail` only, exactly today's rule), `detail`, lease renewed |
| `heartbeat` | — | lease renewed. Sent every 60 s while a task is in flight — the encode is silent for up to ~28 minutes and the lease must not depend on ffmpeg talking. |
| `done` | `result`, `mode`, `output_bytes`, `elapsed` | `done`, `finished`, `result`, `stage='done'`; `stage_end(DONE)`; `stats.record_video()`; the mail |
| `failed` | `error`, `error_class`, `command`, `returncode`, `stderr_tail` | `error`, `finished`; `stage_end(ERROR, …)` with the same fields recorded today |
| `pong` | — | logged; consumed by `tools/brokercheck.py` |

`seq` is monotonic per attempt. FIFO on one queue with one consumer already
gives order; `seq` costs nothing and lets `apply` drop a duplicate.

### 3.3 How the two cannot drift

`ledger.apply` is a pure state transition over the row, **idempotent and
attempt-aware**: `done` on a `done` row is a no-op (so the mail is sent once);
a `progress` from an older attempt is dropped; a `progress` or `heartbeat` for
the current attempt on a `queued` row flips it back to `running` (the reclaim
was premature — the worker is evidently alive); `done` for the current attempt
is accepted from any state but `done`. Every rule is a check in `selftest.py`.
Because `apply` is the only writer of worker facts in **both** transports, the
local mode exercises the identical function.

`attempt` is bumped by the web side on re-submission only. A broker redelivery
re-uses the attempt number; the `Interrupted` closing of the previous
`stage_runs` row records it.

Four additive columns on `jobs` (`stage`, `detail`, `attempt`,
`published_at`) via a ten-line `_migrate()` using `PRAGMA table_info` — the
schema is `CREATE TABLE IF NOT EXISTS`, so there is no other way to add a
column to a database with history in it. `Job.stages` stops being in-memory
state and becomes a derivation from `(state, stage)`, which is exactly what
`on_progress` computes incrementally today, so both front-ends see the same
shapes.

---

## 4. Progress back to the browser

What the browsers actually read: the designed interface (`/app/`) **polls
`/status`**; only the older page (`/`, `app.js:348`) opens `/events`. So
progress has to reach one place — the job row — and both pages follow.

| Option | Verdict |
|---|---|
| Worker writes progress into SQLite directly | Right *read* path, wrong *write* path. Two processes writing one SQLite file works on one node and is a dead end for host two. |
| A fanout/topic exchange the web process subscribes to | Progress is lost whenever the web process is down, so `done` needs a second durable channel — and then `progress` and `done` can arrive out of order. |
| Hybrid: durable to SQLite, ephemeral progress over fanout | Two transports, two failure modes, an ordering problem, for latency nobody can see on a 30-minute job. |
| **Recommended: one durable `vsw.events` carries both; the web side applies everything to SQLite; `/status` and SSE both read SQLite** | One channel, ordered, survives either process restarting. The worker never learns where the table is. |

`Registry.stream()` becomes a 1-second poll of the row with change detection,
keeping the 15-second `waiting` event, so `app.js` runs unmodified. The
in-memory `subscribe`/`_emit` machinery goes.

Cost: one durable queue; a consumer thread in the web process with its own
pika connection; 20–50 small messages per job counting heartbeats; and
`/events` becomes, in the prior document's words, polling in a trench coat.
That is a feature here: the endpoint can be retired with the old page without
the worker noticing.

---

## 5. Redelivery, ack timing, leases

**Ack on completion, never before.** A render cannot resume from the middle, so
the broker's redelivery *is* our crash recovery. Per delivery:

```
task ← body                            (unparseable → basic_reject(requeue=False) → dead)
record ← <job_dir>/render.attempt<N>.json
if record.status in (done, failed):    re-publish that outcome; ack; return
write record {status: started}
publish started
pipeline.run(..., on_progress → publish progress)
ffmpeg writes <output>.part.mp4; Python renames on exit 0
write record {status: done, result, bytes}
publish done   (confirmed)
ack
```

Failure branch: any exception → record `failed`, publish `failed` (command,
returncode, stderr tail as today), **ack** — not `nack(requeue=False)` as the
reference does. The failure is recorded in `stage_runs`, which is our
dead-letter record, and a poison job that crashes the worker must not come
back at the next restart to crash it again.

**Holding an unacked delivery for 32 minutes has two consequences:**

1. **Heartbeats.** `BlockingConnection` only services heartbeats when control
   returns to it. The reference runs the work *inside* the callback, so a
   60-second heartbeat dies after three minutes, the broker requeues mid-encode,
   and the eventual ack lands on a dead channel — its done-flag then saves it on
   redelivery. It works by accident. Here the callback hands the task to a
   **work thread** while the **connection thread** loops
   `process_data_events(time_limit=1)`, draining an outbox, publishing events,
   sending the 60-second heartbeat, and finally acking. The only pika object the
   work thread touches is a `queue.Queue`.
2. **`consumer_timeout`.** Since 3.8.15 RabbitMQ closes the channel of a
   consumer whose delivery stays unacked longer than `consumer_timeout`,
   **default 30 minutes — under our 32-minute worst case.** Hence
   `x-consumer-timeout` on the queue, which needs **RabbitMQ ≥ 3.12**. The dev
   node has 3.12.1 (verified). **The production broker's version is unknown and
   it is shared with the Symfony app**; if it is 3.8.15–3.11 the only fix is
   `consumer_timeout` in `rabbitmq.conf`, which is the owner's to change.
   *First thing to do on any broker: `rabbitmqctl version`.*

**Who recovers what:**

| Failure | Recovered by | Time |
|---|---|---|
| Worker dies mid-render | Broker requeues the unacked delivery; next worker re-runs it (same attempt, previous run closed `Interrupted`) | ~2–3 min |
| No worker running at all | Lease expiry → `reclaim_expired` → row to `queued` **without republishing** (the broker still holds the message) | 600 s |
| Reclaim was premature | The next `progress`/`heartbeat` flips the row back to `running` | one event |
| Worker finished, died before ack | Redelivery → attempt record says `done` → outcome re-published, ack, no second render | ~2–3 min |
| Web process restarts mid-render | Nothing to recover: `resume()` no longer touches `running` rows; events buffer in `vsw.events` | 0 |

**The done-flag trick is right in idea, wrong in implementation.** It is
honoured only `if method.redelivered` (a janitor duplicate is not flagged); it
records "done" and nothing else (a permanent failure re-runs on every
duplicate); and it is not scoped to an attempt (a re-submission with new
colours would be skipped as already done). The **per-attempt record** closes
all three holes and moves to the bucket as a marker object later. The output
file is deliberately *not* the guard — a crashed ffmpeg leaves a
plausible-looking file, which is why the `.part` rename is in this slice.

**Why one worker is load-bearing.** Duplicates are harmless because prefetch 1
on a single consumer serialises them. Two worker processes could run the same
attempt concurrently; preventing that needs a claim marker with
PUT-if-absent semantics, worth building only when a second compute box is.

Not fixed here, flagged: a hung ffmpeg (alive, silent, forever) is caught by
nothing today and by nothing here — heartbeats keep the lease alive. The fix
is a worker-side render timeout derived from `duration`.

---

## 6. Lost messages

Both mechanisms, because they cover different holes.

**Publisher confirms** (`channel.confirm_delivery()`) close the hole the
reference leaves open — a publish that vanishes while the caller logs
"✅ Published". The row is written first (`published_at NULL`), then published;
on confirm `published_at` is stamped. The reference's `time.sleep(0.1)` exists
only because nothing waited for the broker; with confirms it goes.

**A reconciliation sweep** in the janitor, every 30 s, catches what confirms
cannot (a broker restored from an older disk, a message dead-lettered by a
bug, a local-mode restart):

1. `reclaim_expired()` → rows to `queued`, no republish, and only while the
   applier is healthy — a lease can only be judged expired by a web side that
   is actually hearing the worker.
2. Rows `queued AND published_at IS NULL` older than 30 s → publish, stamp.
3. If a passive `queue_declare` on `vsw.render` reports
   `message_count + consumer_count` **less** than the number of `queued` rows,
   republish every `queued` row older than 2 minutes. Over-publishing is safe,
   so the rule need not know *which* message was lost.
4. At startup, if the transport does not survive a restart (local mode), every
   `running` row → `queued` and every `queued` row → `published_at NULL`.
   This is the only mode-dependent behaviour and it is one boolean on the
   transport, not a code path.

`store.claim_next` leaves the running path in both modes (the broker is the
claim). Delete it rather than leave a claim statement nobody calls.

---

## 7. Local mode on Windows

`QUEUE_TRANSPORT=local` (the default when `RABBITMQ_URL` is unset, so a fresh
checkout behaves as today) versus `amqp`. One protocol, two implementations:

```python
class Transport(Protocol):
    survives_restart: bool
    def publish_task(self, task: RenderTask) -> None: ...   # raises TransportError
    def consume_tasks(self, handle) -> None: ...            # blocks
    def publish_event(self, event: Event) -> None: ...
    def consume_events(self, apply) -> None: ...            # blocks
    def render_depth(self) -> tuple[int, int] | None: ...   # (ready, consumers)
```

`LocalTransport`: two `queue.Queue`s; `survives_restart=False`; `create_app()`
starts one worker thread running **the same `worker.handle_task`** and one
applier thread running **the same `ledger.apply`**. Every message is
JSON round-tripped *even in memory*, so a `pathlib.Path` smuggled into `style`
fails on the developer's machine and not on the node.

`AmqpTransport`: pika, `URLParameters(RABBITMQ_URL)`, `heartbeat=60`,
`blocked_connection_timeout=90`; `declare()` on every connect; publisher
confirms; the reference's reconnect-and-backoff (5 s → 300 s) and its
SIGINT/SIGTERM close — registered by the **worker's** main only, since the web
process leaves signals to werkzeug and `signal.signal` is main-thread-only.

Deliberately not offered: a third, file-based transport. The in-process
transport *is* "the current worker", and every extra implementation is another
place to diverge.

Two operational notes: under `run.py --debug` the werkzeug reloader imports the
app twice, so background threads must start only when
`WERKZEUG_RUN_MAIN == "true"` or not in debug — today's threads have the same
double-start and nobody noticed because the store claim serialised them. And on
Windows `python run.py` remains the whole thing: one command, no broker.

---

## 8. `pika`

`pika>=1.3`, pure Python, already present in the `VideoScoreSync` conda env the
app runs under, so nothing to install on the workstation. Not `aio-pika` or
`kombu`: no asyncio here, and matching the reference keeps operational
knowledge transferable.

- **`BlockingConnection` is not thread-safe.** One connection per thread; the
  only sanctioned cross-thread call is `add_callback_threadsafe`. Three
  separate owners in the web process — the publisher (short-lived connection
  per publish, kept *because* it is thread-safe by construction), the applier
  thread, the janitor — and the connection/work split in the worker. SSE
  generators and request handlers never touch pika.
- **Heartbeats need `process_data_events`** (§5). This is the one place the
  reference base class must change rather than be copied.
- Catch `AMQPConnectionError` and `AMQPChannelError`; the reference's
  `ConnectionClosed, AMQPError` misses `StreamLostError` on some paths.
- The engine env (`2026liszt`) has no pika and does not need it in this slice.

---

## 9. What proves it

**In `tools/selftest.py` — no broker, both platforms, every push:**

| Check | Proves |
|---|---|
| `messages round trip` | `RenderTask`/`Event` → JSON → back, equal; `v=2` rejected cleanly |
| `worker handles a task once` | `handle_task` with `pipeline.run` stubbed emits `started, progress…, done` with increasing `seq`; a second delivery of the same attempt emits `done` again and **does not call the stub** |
| `worker reports a failure` | stub raises → one `failed` with `error_class`, `command`, `stderr_tail`; redelivery re-emits, no re-run |
| `ledger applies in order` | the sequence lands as `running → done`, `stage_runs` closed, `store.rate("render")` sees it |
| `ledger is idempotent` | `done` twice → one mail; old-attempt `progress` dropped; `heartbeat` on `queued` → `running`; `started` on an open run → previous `Interrupted` |
| `local transport end to end` | `Registry.start()` → worker thread → applier → `stream()` yields `queued, progress, done`; and `survives_restart=False` re-queues a `running` row |
| `topology is stable` | `declare()`'s arguments equal a golden dict — a change fails the build with "this needs the queue deleted on every broker" |
| `job store round trips` | today's check, minus `claim_next`, plus the four new columns after `_migrate()` on a pre-existing database |

**With a live broker — `tools/brokercheck.py`, by hand on the dev node:**

1. `ping` → `pong`, round trip in seconds, no ffmpeg.
2. `render --seconds 60`: real short job, output exists, row `done`.
3. `kill -9` the worker mid-encode → redelivered in ~3 min, re-runs, one
   output, first `stage_runs` row reads `Interrupted`.
4. `kill` the web process mid-encode, restart → no re-queue, no second encode,
   buffered events applied on reconnect.
5. `RENDER_FAKE_SECONDS=2400` → no `consumer_timeout` trip, heartbeats visible
   in `rabbitmqctl list_consumers`.
6. Stop the broker before submit → row `queued`, page says "in line"; start it
   → janitor publishes within 30 s.
7. Submit twice by hand → one encode.

These need ffmpeg, a package and a broker, so they stay out of CI; results go
into `deployment-log.md` like every other proof on that node.

---

## 10. Modules

New package `app/queue/`:

| Module | Responsibility |
|---|---|
| `messages.py` | `RenderTask`, `Event`, `to_json`/`from_json`, schema version |
| `transport.py` | `Transport` protocol, `LocalTransport`, `AmqpTransport`, `declare()`, `TOPOLOGY` |
| `worker.py` | `handle_task()` — attempt record, `pipeline.run`, events; `main()` for `python -m app.queue.worker` |
| `ledger.py` | `apply(event)` — the only writer of worker facts into `store`; mail and stats on `done` |
| `webside.py` | applier loop + janitor loop; `start_threads()` now, `main()` for a separate process later |

Existing files:

| File | Change |
|---|---|
| `app/jobs.py` | keeps `Job`, `public()`, ETA, `start()`, `stream()` (poll-based). Loses `_serve`, `_run`, `_fail`, `_tell_them`, `ensure_workers`, `WORKERS`, `subscribe`/`_emit`, and the `running→queued` statement in `resume()`. ~455 → ~250 lines. |
| `app/store.py` | `_migrate()`; four columns; `unpublished()`, `mark_published()`, `set_progress()`, `open_stage_run()`; `claim_next` deleted |
| `app/render.py` | encode to `.part.mp4`, `os.replace` on success (3 lines) |
| `app/routes.py` | `create_app()` calls `webside.start_threads()` instead of `registry.resume()`; `/events` and `/status` contracts unchanged |
| `app/settings.py` | `queue_transport`, `rabbitmq_url`, `render_lease_seconds`, `render_queue` |
| `run.py` | banner: transport, and whether the broker answered |
| `requirements.txt` | `pika>=1.3` |
| `.env.example`, `.env.prod` | the four variables — the parity check already enforces both |
| `tools/selftest.py` | the checks above |
| `tools/brokercheck.py` | new |
| `deploy/bootstrap.sh` | `rabbitmq-server` |

Unchanged: `paths.py` (the job tree is not adopted in this slice),
`identify.py` and its thread, `sync.py`, the runners, `notify.py`,
`retention.py`.

---

## 11. Where this deviates from the VideoScoreSync pattern

| Reference | Here | Why |
|---|---|---|
| Work runs inside the delivery callback | Work on a thread; connection thread services heartbeats and acks | 60-second heartbeat vs 32-minute callback |
| Publish without confirms, `sleep(0.1)`, log on failure | Confirms, no sleep, `TransportError` raised | A lost publish is a job stuck in `queued` forever |
| `basic_nack(requeue=False)` on any exception | Report `failed`, ack | The failure record is in `stage_runs`; a poison job must not crash-loop |
| `<stage>_done.flag`, only `if redelivered` | Per-attempt record, checked on every delivery | Janitor duplicates are not flagged; failures and re-submissions need it too |
| Nested `TaskData`, 19 competition fields | Flat, versioned `RenderTask` | The fields are ours; a version field is the price of changing them |
| `import config`, guest, `sleep(5)` at start, Prometheus | `RABBITMQ_URL` from `.env`; no sleep; no metrics in this slice | Metrics are their own slice |
| Short-lived publisher connection | Kept, plus confirms | Thread-safe by construction; the volume does not justify pooling |
| Both sides declare the queues | Kept, through one function | Startup order stays irrelevant |
| One queue per stage | One queue for the render | This app's stages share one process and one directory |

---

## 12. OPEN — where this contradicts `queue-design.md`

That document was written first and decided two things the other way. Both
were verified in it, not assumed:

- **§13.1 "Polling, not SSE"** says *"Drop `/api/jobs/<id>/events`"*. This
  design keeps `/events`, because `/` and `app.js:348` still use it.
  *Recommendation:* retire `/` and `app.js` when the designed interface is the
  only public page, and `/events` with them; the poll-backed `stream()` makes
  that a deletion rather than a migration. Until then both pages work.
- **§16 step 1** puts *"the `files` transport (§8.5)"* and one machine before
  any broker, which arrives at step 4. This design puts RabbitMQ in on one
  machine now, with an in-process local mode instead of files.
  *Recommendation:* skip the files transport. A third implementation of the
  same protocol is more surface to diverge, and the in-process worker already
  is the local mode.

Not adopted here, and not in conflict — just later: §3's chain per stage, §5's
schema, §4.2's job tree.

**The two documents must not disagree on the record.** Once the owner decides,
one paragraph at the top of `queue-design.md` naming this slice and these
points is enough.

---

## 13. OPEN — what is not known

- **The production broker's RabbitMQ version.** It decides whether
  `x-consumer-timeout` works per queue, needs a global setting, or is moot.
  Everything else is the same in all three cases. `rabbitmqctl version`.
- **Whether a ≤3.11 broker ignores or refuses an unknown `x-consumer-timeout`
  argument.** Believed stored-and-ignored; `brokercheck.py ping` settles it.
- **Whether the `/` page is wanted at all.** If not, `/events`, `stream()` and
  the SSE shim disappear before they are written.

---

## 14. Order of work — four steps, each with its own proof

1. ~~**The contract without the broker.**~~ **DONE.** `messages.py`,
   `ledger.py`, the four store columns with `_migrate()`, `Job.stages`
   derived, the in-process worker rewired to report through `ledger.apply`
   instead of writing state itself. *Proved:* 12 checks green on both
   platforms, and a real render driven through the API on the dev node
   advanced `align → strip → encode → done` at 1.299 s/s against 1.292
   before the change — see `deployment-log.md` §12.

   Two deviations from this document, both deliberate. The `.part` rename is
   **not** in: it guards against a crashed ffmpeg being mistaken for a
   result, which only matters once a delivery can be redelivered, so it goes
   with the attempt record in step 3. And the SSE shim of §4 was never
   written, because §12's open question was settled the other way — the
   development page is retired, `/` redirects to `/app/`, and `/events`,
   `stream()`, `subscribe`/`_emit` and the in-memory queues are all gone.
2. ~~**The transport seam.**~~ **DONE.** `transport.py` with
   `LocalTransport` only, `worker.handle_task`, `webside.py`, `Registry`
   shrunk from 455 lines to about 250, `claim_next` deleted, `resume()`'s
   statement gone. *Proved:* 14 checks green on both platforms, one of which
   runs the whole seam with the render stubbed; and a real render on the node
   came out **byte for byte identical** to step 1's, at 547.9 s against
   550.8 s — see `deployment-log.md` §13.
3. ~~**AMQP.**~~ **DONE.** `AmqpTransport`, `python -m app.queue.worker`,
   `tools/brokercheck.py`, `pika`, the per-attempt record and the `.part`
   rename. A real render went through RabbitMQ in 546.7 s against 547.9 s
   without it, byte-identical. Redelivery proved by killing a worker
   mid-encode — requeued, re-run, the abandoned stage row closed
   `Interrupted` — and by re-publishing finished work, which reported the
   outcome again without rendering and without double-counting the video.
   `deployment-log.md` §14 and §15.

   One thing the kill test found that is **not** fixed here: SIGKILL on the
   worker orphans its ffmpeg, which holds a core until it finishes and
   halved the retry's speed on two cores. A process killed with SIGKILL
   cannot clean up after itself, so the supervisor must — systemd's default
   `KillMode=control-group` does exactly that, which makes the units a
   measured requirement rather than tidiness.

   Superseded, for the record: **AMQP.** `AmqpTransport`, `python -m app.queue.worker`,
   `tools/brokercheck.py`, `pika` in requirements. *Proof:* the seven live
   checks on the dev node, written into the deployment log.
4. **Paperwork.** The reconciling note in `queue-design.md`, `README.md`'s
   layout table, the two `.env` templates.

Every step leaves the app working on Windows without a broker.
