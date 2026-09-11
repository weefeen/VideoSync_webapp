"""The only place a worker's report becomes a fact in the job table.

Everything the worker learns arrives here as an `Event` and leaves as a row.
Nothing else writes `state`, `stage`, `detail`, `result` or `finished` for a
running job — which is the whole point. One writer means the rules about
what may follow what live in one function that can be read end to end, and
it means the second host needs no access to the database at all.

**Every rule here exists because a message can arrive twice, late, or out of
turn.** A broker redelivers on any doubt; a reclaim can fire a moment before
a slow worker's next word arrives. So `apply` is idempotent and knows which
attempt it is looking at, and the awkward orderings are decided here rather
than left to whichever of them happens first.
"""
from __future__ import annotations

import logging
import time

from .. import limits, notify, pipeline, stats, storage, store
from ..settings import settings
from .messages import Event

logger = logging.getLogger(__name__)

# How long a row may go unheard from before something may reclaim it. The
# worker sends a heartbeat every minute whether or not ffmpeg has said
# anything, so this is a statement about a worker being gone, not about a
# stage being slow — which is what the old hour-long lease actually measured.
LEASE_SECONDS = 600.0


def apply(event: Event) -> bool:
    """Write one report into the job table. True if the row changed.

    Returns False for anything correctly ignored — a duplicate, a message
    from a superseded attempt, a job that no longer exists — so a caller can
    count what it dropped without treating it as an error.
    """
    if event.type == "pong":
        logger.info("worker %s answered a ping", event.worker or "?")
        return False

    row = store.get_job(event.job_id)
    if row is None:
        logger.warning("event %s for unknown job %s", event.type, event.job_id)
        return False

    # A late word from a previous run of the same job. The row has moved on
    # and must not be dragged back to it.
    current = row["attempt"] if "attempt" in row.keys() else 1
    if event.attempt < (current or 1):
        logger.info("dropping %s from attempt %d of %s; the row is on %d",
                    event.type, event.attempt, event.job_id, current)
        return False

    handler = _HANDLERS.get(event.type)
    if handler is None:
        logger.warning("unknown event type %r for %s", event.type, event.job_id)
        return False
    return handler(event, row)


# --------------------------------------------------------------------------
def _started(event: Event, row) -> bool:
    """The worker has picked the job up."""
    if row["state"] == store.DONE:
        # It finished, and this is a redelivery of an older message. The
        # result stands; re-running would overwrite a video someone may
        # already have been sent a link to.
        logger.info("ignoring 'started' for %s, which is already done",
                    event.job_id)
        return False

    # A stage run still open for this attempt means the last worker died
    # holding it. Close it as interrupted so the record says so, rather than
    # leaving a row that looks like it is still running forever.
    _close_open_run(event, state=store.ERROR, error_class="Interrupted",
                    error_message="The work was picked up again.")

    store.update_job(event.job_id, state=store.RUNNING, worker=event.worker,
                     started=event.at, stage=None, detail="", error=None,
                     lease_until=time.time() + LEASE_SECONDS)
    store.stage_begin(event.job_id, WHOLE, attempt=event.attempt,
                      inputs=[row["upload"]], bytes_in=row["size_bytes"],
                      media_seconds=row["duration"],
                      cpu_at_open=event.cpu_seconds)
    return True


def _progress(event: Event, row) -> bool:
    """Where the work has got to.

    `probe` and `panel` are real work but not milestones: they move the line
    of detail without advancing the stage, which is the rule the in-memory
    version used and the one both front-ends were written against.
    """
    # A terminal job takes no more progress. Checked FIRST, before
    # `_turn_stage` — a late or redelivered progress event for a job already
    # DONE or ERROR used to open a fresh stage_runs row that nothing then
    # closed, because the close only happens at the next milestone or at the
    # terminal event, both already past. That left an orphan open stage row
    # on the record for every out-of-order progress event.
    if row["state"] in (store.DONE, store.ERROR):
        return False

    stage = event.stage if event.stage in pipeline.STAGES else row["stage"]
    # A milestone stage begins: close the one before it and open this one.
    # This is what makes "where does the time actually go" answerable —
    # without it there is one row per job saying only how long the whole
    # thing took, which is the question nobody is asking.
    if stage and stage != row["stage"]:
        _turn_stage(event, row, stage)
    if row["state"] == store.QUEUED:
        # Something reclaimed this row while the worker was mid-stage. The
        # worker is plainly alive; put it back rather than let a second copy
        # be handed out.
        logger.info("%s reported progress while queued; it is running",
                    event.job_id)
        store.update_job(event.job_id, state=store.RUNNING,
                         worker=event.worker or row["worker"])
    store.set_progress(event.job_id, stage, event.detail, LEASE_SECONDS)
    return True


def _heartbeat(event: Event, row) -> bool:
    """Still here. The encode is silent for half an hour; this is not."""
    if row["state"] in (store.DONE, store.ERROR):
        return False
    if row["state"] == store.QUEUED:
        store.update_job(event.job_id, state=store.RUNNING,
                         worker=event.worker or row["worker"])
    store.renew(event.job_id, LEASE_SECONDS)
    return True


def _done(event: Event, row) -> bool:
    """It worked. Record it, then tell them — in that order, and once."""
    if row["state"] == store.DONE:
        # A redelivered completion. The mail has already gone; sending it
        # again would be the visible half of this bug.
        return False

    finished = event.at or time.time()
    fields = {"state": store.DONE, "finished": finished,
              "result": event.result, "object_key": event.object_key,
              "stage": "done",
              "detail": event.detail, "error": None,
              "worker": None, "lease_until": None}
    # Which alignment actually ran is decided by the work, not by the
    # request, so the row learns it here rather than at submit.
    if event.mode:
        fields["mode"] = event.mode
    store.update_job(event.job_id, **fields)
    _close_open_run(event, state=store.DONE,
                    outputs=[event.result] if event.result else None)

    # Whether the recogniser was right, kept beside the alignment it
    # produced. Only here is both halves known: the verdict is in
    # `recognitions` and the visitor's choice is on the job row, and the
    # worker has never seen either. Never allowed to fail the job — the
    # video is delivered either way.
    try:
        storage.store_verdict(event.job_id, row["score"] or "")
    except Exception:                                    # noqa: BLE001
        logger.warning("job %s: the verdict could not be stored",
                       event.job_id, exc_info=True)
    # Counted here rather than at submit, so the tally means delivered and
    # not attempted.
    stats.record_video()
    final = store.get_job(event.job_id)
    told = _tell_them(final)
    _tell_the_operator(final, ok=True, told=told)
    return True


def _failed(event: Event, row) -> bool:
    """It did not work, and why. Not retried: a render that failed on its
    inputs fails again, and the queue must not spend an hour proving it."""
    if row["state"] in (store.DONE, store.ERROR):
        return False
    store.update_job(event.job_id, state=store.ERROR, error=event.error,
                     finished=event.at or time.time(),
                     detail="", worker=None, lease_until=None)
    _close_open_run(event, state=store.ERROR,
                    command=event.command or None,
                    returncode=event.returncode,
                    stderr_tail=(event.stderr_tail or "")[-4000:] or None,
                    error_class=event.error_class or None,
                    error_message=event.error or None)
    # `row` is the state BEFORE the update above, so it still carries the
    # stage the render was in when it died. The updated row does not.
    _tell_the_operator(row, ok=False, told=False,
                       stage=row["stage"] or "",
                       error=event.error or "",
                       error_class=event.error_class or "")
    return True


_HANDLERS = {"started": _started, "progress": _progress,
             "heartbeat": _heartbeat, "done": _done, "failed": _failed}


# --------------------------------------------------------------------------
# The one row that spans the whole job. Kept alongside the per-stage rows
# because `store.rate()` calibrates the queue's time estimates from it, and
# because "how long did this job take" should not require adding six rows up.
WHOLE = "render"


def _stage_runs_open(job_id: str, attempt: int, *, whole: bool):
    """Open runs for this attempt — either the whole-job one, or the stages."""
    return [r for r in store.stage_runs(job_id)
            if r["ended"] is None and (r["attempt"] or 1) == attempt
            and ((r["stage"] == WHOLE) == whole)]


def _turn_stage(event: Event, row, stage: str) -> None:
    """End the stage that was running and start the next one."""
    for run in _stage_runs_open(event.job_id, event.attempt, whole=False):
        store.stage_end(run["id"], state=store.DONE,
                        cpu_seconds=event.cpu_seconds,
                        peak_rss=event.peak_rss)
    store.stage_begin(event.job_id, stage, attempt=event.attempt,
                      media_seconds=row["duration"],
                      cpu_at_open=event.cpu_seconds)


def _close_open_run(event: Event, *, state: str = store.ERROR,
                    **fields) -> None:
    """End the stage record this attempt left open, if it left one.

    The run id is not carried in the message — the worker does not know it,
    because the table is not its business — so it is looked up by job and
    attempt.
    """
    # The per-stage row first, with its own cost, then the whole-job row
    # with the same numbers and whatever detail the caller passed — the
    # command and stderr of a failure belong on both, since the stage is
    # where it happened and the job is where anybody looks first.
    cost = {"cpu_seconds": event.cpu_seconds, "peak_rss": event.peak_rss}
    for run in _stage_runs_open(event.job_id, event.attempt, whole=False):
        store.stage_end(run["id"], state=state, **cost)
    for run in _stage_runs_open(event.job_id, event.attempt, whole=True):
        store.stage_end(run["id"], state=state, **cost, **fields)


def _tell_the_operator(row, *, ok: bool, told: bool, stage: str = "",
                       error: str = "", error_class: str = "") -> None:
    """Say how a job ended, to the person who can do something about it.

    Both outcomes. A failure mail alone cannot answer the question this
    exists for - "did the video reach the person who asked" - and a render
    started by hand on somebody's behalf otherwise gives no signal at all
    between running the command and hoping.

    Goes to ALERT_EMAIL and carries no visitor address: whether anybody was
    told is a yes or a no here. `Privacy.html` promises an address reaches
    the operator only when there is no score for the piece, and widening that
    quietly would make the page untrue.

    Never fatal, and never in front of anything. By the time this runs the
    row is written, the video is stored and the visitor has been told; an
    operational courtesy must not be able to undo any of that.
    """
    if row is None or not settings.can_email:
        return

    seconds = None
    try:
        if ok and row["finished"] and row["started"]:
            seconds = float(row["finished"]) - float(row["started"])
    except (TypeError, ValueError):
        seconds = None

    try:
        notify.send_job_ended(job_id=row["id"], ok=ok,
                              piece=row["score"] or "",
                              seconds=seconds, told=told,
                              stage=stage, error=error,
                              error_class=error_class)
    except Exception:                                  # noqa: BLE001
        logger.warning("could not report how job %s ended", row["id"],
                       exc_info=True)


def _tell_them(row) -> bool:
    """Send the "it is ready" message, if we can and were asked to.

    Never fatal: the video exists, the page shows the link, and a mail
    server having a bad day is not a failed render.

    This runs on the web side and nowhere else. The address is in the table,
    the credentials are in this box's `.env`, and neither travels to a
    compute instance that gets created and destroyed.
    """
    if row is None:
        return False
    email = (row["email"] or "").strip()
    if not email or not settings.can_email:
        return False
    address = email.lower()
    if not limits.allowed("mail_email", address):
        logger.info("not mailing %s: over its allowance", address)
        return False
    if not limits.allowed("mail_total", "all"):
        logger.warning("daily mail cap reached; not mailing %s", address)
        return False
    try:
        notify.send_ready(row["id"], email, piece=row["score"] or "",
                          finished=row["finished"])
        return True
    except notify.MailError as exc:
        logger.warning("could not tell %s about job %s: %s",
                       email, row["id"], exc)
        return False
