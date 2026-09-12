"""The half that owns the table: applying what the worker says, and sweeping.

Two loops, both started by `create_app()`.

**The applier** takes events off the transport and hands them to
`ledger.apply`. It is the only thing that does, so the ordering rules live in
one place.

**The janitor** is the answer to "what if the message never arrived". A queue
can lose a handover — a publish that failed, a process that stopped with
work still in memory, a broker restored from an older disk — and the symptom
is always the same and always silent: a row that says `queued` while nothing
anywhere intends to render it. Nobody notices until someone asks why their
video never came. So the row is the promise, `published_at` records that the
queue took it, and the sweep re-offers anything the queue has evidently
forgotten.

Re-offering is safe and that is what makes the rule simple: a duplicate is
serialised behind the first copy by the single worker, and by the attempt
record once redelivery is real. So the janitor never has to work out *which*
message was lost, only that one was.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import threading
import time

from .. import storage
from .. import store
from ..settings import settings
from . import ledger, worker
from .messages import Event, RenderTask
from .transport import Transport, quiet_pika, transport

logger = logging.getLogger(__name__)

# How long a queued row may go unconfirmed before it is offered again.
UNPUBLISHED_GRACE = 30.0
SWEEP_SECONDS = 30.0

# How often expired videos are swept up. Hourly: it walks the finished
# jobs and nothing about it is urgent to the minute.
RECLAIM_SECONDS = 3600.0

# One encode already uses what the machine has, so one is the honest number.
# It counts consumers now rather than threads, and more than one is refused
# rather than quietly allowed: two consumers could be handed the same task by
# two different routes — a re-offer from the sweep and the original — and
# nothing yet stops them both rendering it. The guard against that is the
# per-attempt record, and it arrives with the broker.
WORKERS = max(1, int(os.getenv("MAX_CONCURRENT_RENDERS", "1") or 1))

_started = threading.Lock()
_running = False


def start_threads() -> int:
    """Bring the queue to life. Returns how many jobs are waiting.

    Called once, from `create_app()`. Idempotent because the development
    server imports the application twice under its reloader, and starting
    two workers would let the same job be rendered twice.
    """
    global _running
    with _started:
        if _running:
            return len(store.waiting())
        _running = True

    if WORKERS != 1:
        raise RuntimeError(
            f"MAX_CONCURRENT_RENDERS is {WORKERS}. Only one is supported "
            f"until a render can prove it has not already been done by "
            f"another consumer; see docs/broker-slice.md §5.")

    quiet_pika()
    bus = transport()
    _recover(bus)

    threading.Thread(target=bus.consume_events, args=(_apply,),
                     name="applier", daemon=True).start()
    threading.Thread(target=_sweep_forever, args=(bus,),
                     name="janitor", daemon=True).start()

    # Who renders depends on the transport, and this is the whole difference
    # between the two arrangements:
    #
    #   in-process queue — nobody else can reach it, so this process must be
    #     the worker too. One command, no broker, which is what a developer
    #     machine and CI need.
    #   a broker — the worker is `python -m app.queue.worker`, its own
    #     process and later its own host. Consuming here as well would put
    #     two consumers on one queue, and nothing yet stops both of them
    #     rendering the same task.
    if not bus.survives_restart:
        threading.Thread(target=bus.consume_tasks, args=(_handle,),
                         name="render", daemon=True).start()
    else:
        logger.info("not rendering here: start a worker with "
                    "`python -m app.queue.worker`")

    waiting = len(store.waiting())
    if waiting:
        logger.info("%d job(s) waiting", waiting)
    return waiting


def publish(job_id: str) -> None:
    """Offer a job to the queue and record that it was taken.

    The row is written before this is called and is not conditional on it:
    if the handover fails, the job is still owed and the sweep will offer it
    again. That is why `published_at` is a separate fact from `queued`.
    """
    row = store.get_job(job_id)
    if row is None:
        return
    transport().publish_task(_task(row))
    store.mark_published(job_id)


# --------------------------------------------------------------------------
def _task(row) -> RenderTask:
    """One row as the message that describes it.

    Deliberately not a job id: a worker on another host cannot read this
    table, and a task that carries only an id would need a second protocol
    to fetch the rest. `upload` is the one field that names a place on a
    particular machine.
    """
    return RenderTask(
        job_id=row["id"], upload=row["upload"], package=row["score"] or "",
        attempt=row["attempt"] or 1, mode=row["mode"],
        duration=row["duration"],
        style=json.loads(row["style"]) if row["style"] else {},
        meta=json.loads(row["meta"] or "{}"),
        queued_at=row["queued_at"] or 0.0,
        input_key=_ensure_input_in_bucket(row))


def _ensure_input_in_bucket(row) -> str:
    """Put the recording in the bucket so a compute node can fetch it, and
    return the key. Empty when there is nothing to do.

    Only when compute could actually take the job: with `COMPUTE_ENABLED`
    off, the only consumer is the local worker on this box, which reads the
    upload straight off the shared disk, so uploading it to the bucket would
    be bandwidth for nobody. With compute on, the input MUST be in the bucket
    before a node picks the task up — a node cannot see this disk — so this
    is the wire that makes the selected workflow (render on a throwaway host)
    actually run rather than fail looking for a file it cannot reach.

    Idempotent: a re-offer by the sweep finds the object already there by its
    size and does not upload it again. A storage failure is swallowed and the
    key comes back empty — the job still renders on the local worker, which
    is the safe degradation while the reason is fixed.
    """
    if not (settings.compute_enabled and storage.available()):
        return ""
    local = pathlib.Path(row["upload"])
    if not local.is_file():
        return ""
    key = storage.work_key(row["id"], "input" + local.suffix.lower())
    try:
        if storage.head(key) != local.stat().st_size:
            storage.put(local, key)
        return key
    except Exception:                                  # noqa: BLE001
        logger.warning("job %s: could not stage the input in the bucket; the "
                       "local worker can still render it", row["id"],
                       exc_info=True)
        return ""


def _apply(event) -> None:
    ledger.apply(event)


# How often the worker says it is still alive while nothing else is happening.
HEARTBEAT_SECONDS = 60.0


def _handle(task: RenderTask, ack) -> None:
    """Run one task, saying so periodically for as long as it takes.

    The encode is silent for up to about twenty-eight minutes — ffmpeg
    reports nothing a stage boundary would notice — so without this the lease
    would expire mid-render and the janitor would queue the job again while
    it was still being rendered. The lease has to measure "is a worker there",
    not "has a stage finished".
    """
    bus = transport()
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                bus.publish_event(Event(job_id=task.job_id, type="heartbeat",
                                        attempt=task.attempt,
                                        worker=worker.name()))
            except Exception:                         # noqa: BLE001
                logger.warning("could not send a heartbeat for %s", task.job_id)

    threading.Thread(target=beat, name=f"beat-{task.job_id}",
                     daemon=True).start()
    try:
        worker.handle_task(task, bus.publish_event)
    finally:
        stop.set()
        ack()


def _recover(bus: Transport) -> None:
    """Put the table and the queue back into agreement at startup.

    Only needed when the transport forgets on restart, which the in-process
    one does. A render cannot resume from the middle, so anything that was
    running goes back to the queue rather than being reported finished or
    left to sit as `running` forever with nothing running it.

    When the transport *does* survive — a broker — this does nothing, and
    that is important: the message is still held there, and re-queuing a row
    whose render is still going would hand out a second copy of live work.
    """
    if bus.survives_restart:
        return
    stranded = store.write_returning(
        "UPDATE jobs SET state = ?, worker = NULL, lease_until = NULL,"
        " started = NULL, stage = NULL, published_at = NULL"
        " WHERE state = ? RETURNING id", (store.QUEUED, store.RUNNING))
    for row in stranded:
        logger.info("job %s was interrupted; queued again", row["id"])
    # Everything queued must be offered again: whatever was holding it went
    # with the process.
    forgotten = store.forget_publications()
    if forgotten:
        logger.info("%d queued job(s) must be offered again",
                    forgotten)


def reclaim_expired_outputs() -> int:
    """Delete rendered videos nobody can download any more. Returns bytes freed.

    `retention.is_live()` stops serving a video after RETENTION_HOT_HOURS
    and the page says so — but until now nothing deleted anything, so every
    render stayed on disk for ever. At 126 MB each that is about eight
    finished jobs per gigabyte, accumulating with no upper bound. A disk
    that fills stops the renders, and the first symptom is unrelated to the
    cause.

    **Only the rendered video.** It is derived: the upload is kept, and the
    score and the style are in the row, so it can be made again — this
    discards a cache that costs one encode to rebuild, not a recording.

    What is deliberately NOT deleted: the upload. Home recordings are the
    one thing a studio library cannot supply and what the recogniser most
    needs; keeping them is a commitment the privacy note makes explicitly.
    They are also the only irreplaceable thing here.

    This is the hot tier expiring with nowhere colder to go yet. When
    archival exists, this becomes a move rather than a delete.
    """
    hours = settings.retention_hot_hours
    if hours <= 0:
        return 0
    cutoff = time.time() - hours * 3600.0
    freed = 0
    rows = store.query(
        "SELECT id, result FROM jobs WHERE state = ? AND result IS NOT NULL"
        " AND finished IS NOT NULL AND finished < ?", (store.DONE, cutoff))
    for row in rows:
        path = pathlib.Path(row["result"])
        try:
            if path.is_file():
                size = path.stat().st_size
                path.unlink()
                freed += size
                logger.info("reclaimed %.0f MB from %s, past its %g-hour "
                            "window", size / 1e6, row["id"], hours)
        except OSError as exc:
            logger.warning("could not reclaim %s: %s", path, exc)
    return freed


def _sweep_forever(bus: Transport) -> None:
    last_reclaim = 0.0
    while True:
        time.sleep(SWEEP_SECONDS)
        try:
            sweep(bus)
        except Exception:                         # noqa: BLE001
            logger.exception("the janitor tripped; it will try again")
        # Hourly, not every sweep: it walks the finished jobs, and nothing
        # about it is urgent to the minute.
        if time.time() - last_reclaim > RECLAIM_SECONDS:
            last_reclaim = time.time()
            try:
                freed = reclaim_expired_outputs()
                if freed:
                    logger.info("reclaimed %.1f GB of expired video",
                                freed / 1e9)
            except Exception:                     # noqa: BLE001
                logger.exception("the reclaim pass tripped; it will try again")


def sweep(bus: Transport) -> int:
    """One pass. Returns how many jobs it offered to the queue again."""
    for job_id in store.reclaim_expired():
        logger.info("job %s lost its lease; it is queued again", job_id)

    offered = 0
    for row in store.unpublished(older_than=UNPUBLISHED_GRACE):
        logger.warning("job %s was never taken by the queue; offering it again",
                       row["id"])
        publish(row["id"])
        offered += 1

    # A queue holding fewer messages than there are jobs waiting has lost
    # some. Which ones cannot be known, and does not need to be: offering a
    # duplicate is harmless, so everything waiting is offered again.
    depth = bus.render_depth()
    waiting = store.waiting()
    if depth is not None and waiting and depth[0] < len(waiting):
        logger.warning("queue holds %d but %d are waiting; re-offering",
                       depth[0], len(waiting))
        for row in waiting:
            publish(row["id"])
            offered += 1
    return offered
