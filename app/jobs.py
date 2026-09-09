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
import shlex
import threading
import time
import traceback
import uuid
from typing import Any

from . import limits
from . import notify
from . import paths as jobpaths
from . import pipeline
from . import render as rnd
from . import stats
from . import store
from .settings import settings

logger = logging.getLogger(__name__)

# One encode uses what the machine has, so one worker is the honest default.
# It is a setting because the day the work spreads over more machines, that
# should be a number to change rather than a file to rewrite.
WORKERS = max(1, int(os.getenv("MAX_CONCURRENT_RENDERS", "1") or 1))

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
    email: str = ""
    detail: str = ""
    duration: float | None = None
    size_bytes: int | None = None
    priority: int = 0
    created: float = dataclasses.field(default_factory=time.time)
    queued_at: float | None = None
    started: float | None = None
    finished: float | None = None
    stages: dict[str, str] = dataclasses.field(
        default_factory=lambda: {s: "pending" for s in pipeline.STAGES})

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
            "email": self.email, "duration": self.duration,
            "size_bytes": self.size_bytes, "priority": self.priority,
            "queued_at": self.queued_at, "started": self.started,
            "finished": self.finished,
            "style": json.dumps(dataclasses.asdict(style)) if style else None,
            "meta": json.dumps(meta or {}),
        }

    @classmethod
    def from_row(cls, row) -> "Job":
        job = cls(id=row["id"],
                  original_name=row["name"] or "",
                  upload_path=pathlib.Path(row["upload"] or ""))
        job.score, job.mode = row["score"], row["mode"]
        job.state, job.error = row["state"], row["error"]
        job.result = pathlib.Path(row["result"]) if row["result"] else None
        job.email = row["email"] or ""
        job.duration, job.size_bytes = row["duration"], row["size_bytes"]
        job.priority = row["priority"] or 0
        job.created = row["created"]
        job.queued_at, job.started = row["queued_at"], row["started"]
        job.finished = row["finished"]
        done = job.state == store.DONE
        job.stages = {s: ("done" if done else "pending") for s in pipeline.STAGES}
        return job

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
        self._wake = threading.Event()
        self._live: dict[str, Job] = {}     # uploaded, not yet submitted
        self._workers: list[threading.Thread] = []

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
        job.save(style, meta)
        with self._lock:
            self._live.pop(job.id, None)
        self.ensure_workers()
        self._wake.set()

    # -- the workers -----------------------------------------------------
    def ensure_workers(self) -> None:
        with self._lock:
            self._workers = [w for w in self._workers if w.is_alive()]
            for n in range(WORKERS - len(self._workers)):
                worker = threading.Thread(
                    target=self._serve, daemon=True,
                    name=f"render-{len(self._workers) + n}")
                self._workers.append(worker)
                worker.start()

    def _serve(self) -> None:
        """Take the next job, run it, repeat. Idle when there is nothing."""
        me = threading.current_thread().name
        lease = max(600.0, settings.sync_timeout * 4)
        while True:
            row = store.claim_next(me, lease_seconds=lease)
            if row is None:
                self._wake.wait(timeout=20)
                self._wake.clear()
                continue
            try:
                self._run(Job.from_row(row), row)
            except Exception:                    # noqa: BLE001
                traceback.print_exc()

    def _run(self, job: Job, row) -> None:
        try:
            style = rnd.Style(**json.loads(row["style"])) if row["style"] else None
            meta = json.loads(row["meta"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self._fail(job, f"The stored settings could not be read: {exc}")
            return

        lease = max(600.0, settings.sync_timeout * 4)

        def on_progress(stage: str, detail: str = "") -> None:
            if stage in job.stages:
                for name in pipeline.STAGES:
                    if name == stage:
                        break
                    if job.stages[name] == "pending":
                        job.stages[name] = "done"
                job.stages[stage] = "active"
            job.detail = detail
            # Say we are still here, so nothing reclaims a job that is only
            # slow rather than abandoned.
            store.renew(job.id, lease)

        run = store.stage_begin(job.id, "render",
                                inputs=[str(job.upload_path)],
                                bytes_in=job.size_bytes,
                                media_seconds=job.duration)
        try:
            package = pipeline.find_package(job.score or "")
            if package is None:
                raise pipeline.PipelineError(
                    f"No score package named {job.score!r}.")

            result = pipeline.run(package, job.upload_path, job.id,
                                  style, job.mode, meta, on_progress)
            job.result, job.mode = result.output, result.mode
            job.state, job.finished = store.DONE, time.time()
            for name in job.stages:
                job.stages[name] = "done"
            store.update_job(job.id, state=store.DONE, finished=job.finished,
                             result=str(job.result), mode=job.mode,
                             worker=None, lease_until=None)
            store.stage_end(run, state=store.DONE, outputs=[str(job.result)])
            # Counted here rather than at submit, so the tally means
            # delivered and not attempted.
            stats.record_video()
            self._tell_them(job)

        except pipeline.PipelineError as exc:
            # A tool failure carries the command that produced it; anything
            # else has only its message. Both are recorded, so "which stage,
            # what inputs, what error" has an answer without a log dig.
            store.stage_end(
                run, state=store.ERROR, error=exc,
                command=shlex.join(getattr(exc, "command", []) or []) or None,
                returncode=getattr(exc, "returncode", None),
                stderr_tail=(getattr(exc, "stderr", "") or "")[-4000:] or None)
            self._fail(job, str(exc))
        except Exception as exc:                 # noqa: BLE001 - never die silently
            traceback.print_exc()
            store.stage_end(run, state=store.ERROR, error=exc)
            self._fail(job, f"Unexpected failure: {exc}")
        finally:
            self._wake.set()                     # someone may be next

    @staticmethod
    def _tell_them(job: Job) -> None:
        """Send the "it is ready" message, if we can and were asked to.

        Never fatal: the video exists, the page shows the link, and a mail
        server having a bad day is not a failed render.
        """
        if not job.email or not settings.can_email:
            return
        address = job.email.strip().lower()
        if not limits.allowed("mail_email", address):
            logger.info("not mailing %s: over its allowance", address)
            return
        if not limits.allowed("mail_total", "all"):
            logger.warning("daily mail cap reached; not mailing %s", address)
            return
        try:
            notify.send_ready(job.id, job.email, piece=job.score or "",
                              finished=job.finished)
        except notify.MailError as exc:
            logger.warning("could not tell %s about job %s: %s",
                           job.email, job.id, exc)

    def _fail(self, job: Job, message: str) -> None:
        job.state, job.error = store.ERROR, message
        job.finished = time.time()
        for name, value in job.stages.items():
            if value == "active":
                job.stages[name] = "failed"
        store.update_job(job.id, state=store.ERROR, error=message,
                         finished=job.finished, worker=None, lease_until=None)

    def resume(self) -> int:
        """Pick the queue up again after a restart.

        A render cannot continue from the middle, so anything caught in
        flight goes back to the queue rather than being reported finished or
        quietly forgotten. Returns how many are now waiting.
        """
        reclaimed = store.reclaim_expired()
        stranded = store.write_returning(
            "UPDATE jobs SET state = ?, worker = NULL, lease_until = NULL,"
            " started = NULL WHERE state = ? RETURNING id",
            (store.QUEUED, store.RUNNING))
        for row in stranded:
            logger.info("job %s was interrupted; queued again", row["id"])
        waiting = len(store.waiting())
        if waiting:
            logger.info("%d job(s) waiting (%d recovered from a restart)",
                        waiting, len(reclaimed) + len(stranded))
            self.ensure_workers()
            self._wake.set()
        return waiting


registry = Registry()


def new_job(original_name: str, upload_path: pathlib.Path) -> Job:
    return registry.add(Job(id=uuid.uuid4().hex[:12],
                            original_name=original_name,
                            upload_path=upload_path))


def job_paths(job: Job) -> jobpaths.JobPaths:
    """This job's folder, in the layout the engine uses."""
    return jobpaths.for_job(job.id, pathlib.Path(job.upload_path).suffix)
