"""The queue: what is waiting, what is running, and when it will be done.

This used to start a thread the moment a job was submitted. That is fine
for one person on a laptop and wrong for a public address: ten visitors
meant ten simultaneous encodes on one machine, each making the others
slower, with nothing to tell anybody why.

Now a submission is a row. Workers take the next one atomically — `store`
does that in a single statement — and how many workers there are is a
setting rather than a shape baked into this file. The default is one,
because one encode already uses what the machine has; the reason it is a
number is that the day a second worker is wanted, it should be a matter of
starting one.

Three things follow from the queue living on disk rather than in memory:

    a restart resumes it    queued work is picked up again, and a job that
                            was mid-render when the process died goes back
                            to the queue instead of being lost in silence
    people can be told      "third in line, ready by about 14:20", from
                            measured rates rather than from a guess
    the estimate improves   every finished render records how long it took
                            against the length of the music, and the median
                            of those replaces the constant below
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import pathlib
import threading
import time
import uuid
from typing import Any

from . import limits
from . import notify
from . import paths as jobpaths
from . import pipeline
from . import render as rnd
from . import store
from .settings import settings
from .queue import webside

logger = logging.getLogger(__name__)

# Until enough jobs have run here to know better. Measured on a workstation:
# about 0.82 s of encode per second of music, plus a fixed cost that does
# not depend on length. `store.rate` replaces the first once there are
# samples, and no figure shown to a visitor should rest on this for long.
SECONDS_PER_SECOND = 0.82
FIXED_SECONDS = 140.0
CALIBRATE_AFTER = 5


@dataclasses.dataclass
class Job:
    id: str
    original_name: str
    upload_path: pathlib.Path
    score: str | None = None
    mode: str | None = None
    state: str = "uploaded"      # uploaded | queued | running | done | error
    error: str | None = None
    result: pathlib.Path | None = None
    # Where the finished video lives in the bucket, once it does.
    object_key: str = ""
    email: str = ""
    # The address the upload came from. Kept because it is the only thing
    # standing in for a login here: the limits that stop one visitor using
    # the whole machine are counted against it, and where uploads come from
    # is what decides where the servers should be. Never shown to anyone but
    # the operator, and never resolved to a person.
    client: str = ""
    detail: str = ""
    duration: float | None = None
    size_bytes: int | None = None
    priority: int = 0
    created: float = dataclasses.field(default_factory=time.time)
    queued_at: float | None = None
    started: float | None = None
    finished: float | None = None
    # Where the render has got to. `stages` is derived from it rather than
    # stored: two of them held the same truth, and the copy that lived on
    # this object was the one nothing outside the working process could see.
    stage: str | None = None
    attempt: int = 1

    # -- the store -------------------------------------------------------
    def row(self, style: rnd.Style | None = None,
            meta: dict | None = None) -> dict[str, Any]:
        """This job as columns.

        The style and the metadata are stored, not just the identifiers: a
        queued job that cannot be resumed after a restart is a job that was
        quietly dropped.
        """
        return {
            "id": self.id, "created": self.created, "name": self.original_name,
            "upload": str(self.upload_path), "score": self.score,
            "mode": self.mode, "state": self.state, "error": self.error,
            "result": str(self.result) if self.result else None,
            "object_key": self.object_key or None,
            "email": self.email, "client": self.client,
            "duration": self.duration,
            "size_bytes": self.size_bytes, "priority": self.priority,
            "queued_at": self.queued_at, "started": self.started,
            "finished": self.finished,
            "style": json.dumps(dataclasses.asdict(style)) if style else None,
            "meta": json.dumps(meta or {}),
            # Written explicitly because put_job is INSERT OR REPLACE: a
            # column left out of this dict is not left alone, it is reset.
            "stage": self.stage, "detail": self.detail,
            "attempt": self.attempt,
        }

    @classmethod
    def from_row(cls, row) -> "Job":
        job = cls(id=row["id"],
                  original_name=row["name"] or "",
                  upload_path=pathlib.Path(row["upload"] or ""))
        job.score, job.mode = row["score"], row["mode"]
        job.state, job.error = row["state"], row["error"]
        job.result = pathlib.Path(row["result"]) if row["result"] else None
        job.object_key = ((row["object_key"] if "object_key" in row.keys()
                           else "") or "")
        job.email = row["email"] or ""
        job.client = (row["client"] if "client" in row.keys() else "") or ""
        job.duration, job.size_bytes = row["duration"], row["size_bytes"]
        job.priority = row["priority"] or 0
        job.created = row["created"]
        job.queued_at, job.started = row["queued_at"], row["started"]
        job.finished = row["finished"]
        keys = row.keys()
        job.stage = row["stage"] if "stage" in keys else None
        job.detail = (row["detail"] if "detail" in keys else "") or ""
        job.attempt = (row["attempt"] if "attempt" in keys else 1) or 1
        return job

    @property
    def stages(self) -> dict[str, str]:
        """The per-stage picture both front-ends draw, derived from the row.

        Everything before the current stage is finished, the current one is
        active, the rest have not started. Held as state until now, which
        meant it only existed inside the process doing the work: a status
        poll rebuilt the Job from the table and got all-pending for the
        whole render, so the progress indicator sat at zero and then jumped.
        """
        if self.state == store.DONE:
            return {s: "done" for s in pipeline.STAGES}
        if self.stage not in pipeline.STAGES:
            return {s: "pending" for s in pipeline.STAGES}
        out, reached = {}, False
        for name in pipeline.STAGES:
            if name == self.stage:
                reached = True
                out[name] = "failed" if self.state == store.ERROR else "active"
            else:
                out[name] = "pending" if reached else "done"
        return out

    def save(self, style: rnd.Style | None = None,
             meta: dict | None = None) -> None:
        store.put_job(self.row(style, meta))

    # -- what the page is told -------------------------------------------
    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.original_name,
            "state": self.state,
            "error": self.error,
            "detail": self.detail,
            "score": self.score,
            "mode": self.mode,
            "mode_label": pipeline.MODE_LABELS.get(self.mode or "", ""),
            "duration": self.duration,
            "size_bytes": self.size_bytes,
            "elapsed": round((self.finished or time.time()) - self.started, 1)
                       if self.started else None,
            "stages": dict(self.stages),
            # How many are in front. None unless waiting; 0 means next.
            "ahead": store.position(self.id),
            "eta_seconds": _eta_for(self),
            "output_bytes": self.result.stat().st_size
                            if self.result and self.result.is_file() else None,
            "download": f"/api/jobs/{self.id}/download"
                        if self.state == store.DONE else None,
        }


# ---------------------------------------------------------------------------
# estimating
# ---------------------------------------------------------------------------

def _work_seconds(duration: float | None) -> float:
    """How long a job of this length should take.

    Measured where there are measurements and assumed where there are not,
    and the two are deliberately kept apart: the constant came from one
    machine, and every deployment will differ from it.
    """
    rate = store.rate("render", minimum=CALIBRATE_AFTER) or SECONDS_PER_SECOND
    return FIXED_SECONDS + rate * (duration or 0.0)


def _eta_for(job: Job) -> int | None:
    """Seconds until this job should be done, counting everything ahead.

    None when there is nothing sensible to say — not submitted, or already
    finished. Never less than half a minute while running, because "any
    moment now" that persists reads as a stuck page.
    """
    if job.state == store.RUNNING and job.started:
        left = _work_seconds(job.duration) - (time.time() - job.started)
        return max(30, int(left))
    if job.state != store.QUEUED:
        return None
    total = 0.0
    current = store.running()
    if current:
        spent = time.time() - (current["started"] or time.time())
        total += max(0.0, _work_seconds(current["duration"]) - spent)
    for row in store.waiting():
        total += _work_seconds(row["duration"])
        if row["id"] == job.id:
            break
    return int(total)


class Registry:
    """The queue, and the workers that drain it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._live: dict[str, Job] = {}     # uploaded, not yet submitted

    # -- lookup ----------------------------------------------------------
    def add(self, job: Job) -> Job:
        with self._lock:
            self._live[job.id] = job
        job.save()
        return job

    def get(self, job_id: str) -> Job | None:
        row = store.get_job(job_id)
        with self._lock:
            held = self._live.get(job_id)
        if row is None:
            return held
        # An upload that has not been submitted keeps its in-memory object,
        # so what the probe learned during upload is not thrown away.
        if held and row["state"] == "uploaded":
            return held
        return Job.from_row(row)

    def all(self) -> list[Job]:
        return [Job.from_row(r) for r in store.query(
            "SELECT * FROM jobs ORDER BY created DESC LIMIT 200")]

    # -- submitting ------------------------------------------------------
    def start(self, job: Job, score: str, mode: str | None,
              style: rnd.Style, meta: dict) -> None:
        """Put the job in the queue. It runs when a worker reaches it."""
        job.score = score
        job.mode = mode
        job.state = store.QUEUED
        job.queued_at = time.time()
        # Submitting a finished or failed job again starts a new attempt, so
        # a message still in flight from the previous one cannot be mistaken
        # for this one's and overwrite what this run produces.
        previous = store.get_job(job.id)
        if previous is not None and previous["state"] in (store.DONE, store.ERROR):
            job.attempt = (previous["attempt"] or 1) + 1
        job.stage, job.detail, job.error = None, "", None
        job.result, job.finished, job.started = None, None, None
        job.save(style, meta)
        with self._lock:
            self._live.pop(job.id, None)
        # The row is the promise and it is written above. If the handover
        # fails the job is still owed, and the janitor offers it again —
        # which is why `published_at` is a fact of its own.
        try:
            webside.publish(job.id)
        except Exception:                        # noqa: BLE001
            logger.exception("job %s could not be handed to the queue; "
                             "the sweep will offer it again", job.id)
        _say_it_is_queued(job)

    # -- the workers -----------------------------------------------------

# `ensure_workers`, `_serve`, `_run` and `resume` were here. The worker loop
# is `app/queue/webside.py` and the work itself is `app/queue/worker.py`;
# what is left of this class is lookup and submission, which is all the web
# side ever needed from it.


registry = Registry()


def new_job(original_name: str, upload_path: pathlib.Path,
            client: str = "") -> Job:
    """A job, saved. `client` is passed in rather than set afterwards
    because `add` writes the row immediately: an address assigned after this
    returns would not reach the table until the next save, and a visitor who
    uploads and never renders never causes one."""
    return registry.add(Job(id=uuid.uuid4().hex[:12],
                            original_name=original_name,
                            upload_path=upload_path,
                            client=client))


def job_paths(job: Job) -> jobpaths.JobPaths:
    """This job's folder, in the layout the engine uses."""
    return jobpaths.for_job(job.id, pathlib.Path(job.upload_path).suffix)


def say_a_score_is_wanted(job: Job, score: str, address: str) -> None:
    """Tell the operator a request just died for want of a score.

    This is the entire demand loop. There is no queue of parked requests and
    no backlog page: the request is refused, the visitor is told the score is
    not in the library, and this message is what survives it.

    It carries the visitor's address, which is a DELIBERATE EXCEPTION to the
    rule that an address lives only on the job row. The refused request never
    reached the point where the address is stored, so without this the person
    who prompted an hour of engraving could not be sent the result. The
    operator asked for it explicitly, knowing the cost: there is now a second
    copy of the address, in a mailbox, that deleting a recording does not
    reach. The privacy page says so.

    It is the address and nothing else. Not the filename, not the address
    they connected from, not any free text - a visitor must never be able to
    put words of their own into mail sent to the operator.

    Goes to ALERT_EMAIL, capped only by the global daily allowance. On a
    thread and never allowed to fail the request: the visitor is waiting on
    the response, and an SMTP round-trip must not sit in front of it.
    """
    if not settings.can_email:
        return
    if not limits.allowed("mail_total", "all"):
        logger.warning("daily mail cap reached; not reporting that %r is "
                       "wanted for job %s", score, job.id)
        return

    minutes = (job.duration / 60.0) if job.duration else None

    country, title = "", ""
    try:
        row = store.recognition_for(job.id)
        if row is not None:
            country = row["country"] or ""
            title = row["title"] or ""
    except Exception:                                # noqa: BLE001
        logger.debug("no recognition row for job %s", job.id)

    def work() -> None:
        try:
            notify.send_wanted(job_id=job.id, score=score, title=title,
                               minutes=minutes, country=country,
                               address=address)
        except Exception:                            # noqa: BLE001
            logger.warning("could not report that %r is wanted for job %s",
                           score, job.id, exc_info=True)

    threading.Thread(target=work, name=f"wanted-{job.id}", daemon=True).start()


def _say_it_is_queued(job: Job) -> None:
    """Tell the visitor we have it, roughly how long, and where to look.

    A render can take the better part of an hour and silence for that long
    reads as failure — at which point people upload the same recording again,
    which costs another render and lengthens the queue that caused the wait.

    Its own allowance, not the ready mail's. A courtesy must never be able to
    spend the budget of the message that carries the promise: with one shared
    cap, adding this mail refused the "your video is ready" mail for somebody
    who had waited an hour, silently.

    On a thread, because this holds an SMTP conversation with a relay on the
    other side of the internet and the visitor is waiting for the response to
    their render request. Never allowed to fail the submission: the job is
    queued either way, and a missing courtesy is not worth a 500.
    """
    address = (job.email or "").strip()
    if not address or not settings.can_email:
        return
    if not limits.allowed("mail_queued_email", address.lower()):
        logger.info("not sending a queued notice to %s: over its allowance",
                    address)
        return
    if not limits.allowed("mail_total", "all"):
        logger.warning("daily mail cap reached; no queued notice to %s", address)
        return

    eta = _eta_for(job)
    ahead = store.position(job.id) or 0

    def work() -> None:
        try:
            notify.send_queued(job.id, address,
                               piece=job.score or "",
                               ahead=ahead,
                               minutes=(eta / 60.0) if eta else None)
        except Exception:                            # noqa: BLE001
            logger.warning("could not send a queued notice to %s for job %s",
                           address, job.id, exc_info=True)

    threading.Thread(target=work, name=f"queued-mail-{job.id}",
                     daemon=True).start()
