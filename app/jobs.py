"""In-memory job registry with a worker thread per render.

Deliberately simple: one process, no broker, no database. A job aligns a
recording and then encodes it, which takes minutes, so it runs on a thread
and the page follows along over Server-Sent Events.

State is lost on restart. That is fine for a local tool and is the first
thing to replace if this ever serves real users.
"""

from __future__ import annotations

import dataclasses
import pathlib
import json
import logging
import queue
import threading
import time
import traceback
import uuid
from typing import Any, Iterator

from . import limits
from . import notify
from . import pipeline
from .settings import settings

logger = logging.getLogger(__name__)
from . import render as rnd
from . import stats


@dataclasses.dataclass
class Job:
    id: str
    original_name: str
    upload_path: pathlib.Path
    score: str | None = None
    mode: str | None = None
    state: str = "uploaded"          # uploaded | running | done | error
    error: str | None = None
    result: pathlib.Path | None = None
    email: str = ""
    detail: str = ""
    duration: float | None = None
    size_bytes: int | None = None
    created: float = dataclasses.field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    stages: dict[str, str] = dataclasses.field(
        default_factory=lambda: {s: "pending" for s in pipeline.STAGES})

    def manifest(self) -> dict[str, Any]:
        """Everything needed to answer for this job after a restart."""
        return {
            "id": self.id, "name": self.original_name,
            "upload": str(self.upload_path), "score": self.score,
            "mode": self.mode, "state": self.state, "error": self.error,
            "result": str(self.result) if self.result else None,
            "duration": self.duration, "size_bytes": self.size_bytes,
            "created": self.created, "finished": self.finished,
            "email": self.email,
        }

    def save(self) -> None:
        """Write the manifest beside the job. Never fatal.

        The registry lives in memory, so without this a finished video
        becomes unreachable the moment the server restarts — and the link
        someone was given stops working for reasons they cannot see.
        """
        try:
            folder = pathlib.Path(self.result).parent if self.result                 else pipeline.job_folder(self.id)
            folder.mkdir(parents=True, exist_ok=True)
            temp = folder / "job.json.part"
            temp.write_text(json.dumps(self.manifest(), indent=2), encoding="utf-8")
            temp.replace(folder / "job.json")
        except OSError as exc:
            logger.warning("could not write the job manifest: %s", exc)

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
            "output_bytes": self.result.stat().st_size
                            if self.result and self.result.is_file() else None,
            "download": f"/api/jobs/{self.id}/download" if self.state == "done" else None,
        }


class Registry:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._subscribers: dict[str, list[queue.Queue]] = {}
        self._lock = threading.Lock()

    # -- lookup ----------------------------------------------------------
    def add(self, job: Job) -> Job:
        with self._lock:
            self._jobs[job.id] = job
            self._subscribers[job.id] = []
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: -j.created)

    # -- events ----------------------------------------------------------
    def subscribe(self, job_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.setdefault(job_id, []).append(q)
        return q

    def unsubscribe(self, job_id: str, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers.get(job_id, []):
                self._subscribers[job_id].remove(q)

    def _emit(self, job_id: str, payload: dict) -> None:
        with self._lock:
            subscribers = list(self._subscribers.get(job_id, []))
        for q in subscribers:
            q.put(payload)

    # -- running ---------------------------------------------------------
    def start(self, job: Job, score: str, mode: str | None,
              style: rnd.Style, meta: dict) -> None:
        """Kick off alignment and rendering on a background thread."""
        job.score = score
        job.mode = mode
        job.state = "running"
        job.started = time.time()
        threading.Thread(target=self._run, args=(job, style, meta),
                         name=f"job-{job.id}", daemon=True).start()

    def _run(self, job: Job, style: rnd.Style, meta: dict) -> None:
        def on_progress(stage: str, detail: str = "") -> None:
            # Stages arrive in order; mark everything before this one done.
            if stage in job.stages:
                for name in pipeline.STAGES:
                    if name == stage:
                        break
                    if job.stages[name] == "pending":
                        job.stages[name] = "done"
                job.stages[stage] = "active"
            job.detail = detail
            self._emit(job.id, {"type": "progress", "stage": stage,
                                "detail": detail, "job": job.public()})

        try:
            package = pipeline.find_package(job.score or "")
            if package is None:
                raise pipeline.PipelineError(f"No score package named {job.score!r}.")

            result = pipeline.run(package, job.upload_path, job.id,
                                  style, job.mode, meta, on_progress)
            job.result = result.output
            job.mode = result.mode
            for name in job.stages:
                job.stages[name] = "done"
            job.state = "done"
            job.finished = time.time()
            job.save()
            # One more video that actually exists. Counted here rather than
            # at submit, so the tally means delivered and not attempted.
            stats.record_video()
            job.save()
            self._tell_them(job)
            self._emit(job.id, {"type": "done", "job": job.public()})

        except pipeline.PipelineError as exc:
            self._fail(job, str(exc))
        except Exception as exc:  # noqa: BLE001 - never die silently
            traceback.print_exc()
            self._fail(job, f"Unexpected failure: {exc}")

    @staticmethod
    def _tell_them(job: Job) -> None:
        """Send the "it is ready" message, if we can and were asked to.

        Never fatal: the video exists, the page shows the link, and a mail
        server having a bad day is not a reason to report a failed render.
        """
        if not job.email or not settings.can_email:
            return
        # Mail goes to an address somebody typed, so it is capped even
        # after everything upstream has allowed the render.
        address = job.email.strip().lower()
        if not limits.allowed("mail_email", address):
            logger.info("not mailing %s: over its allowance", address)
            return
        if not limits.allowed("mail_total", "all"):
            logger.warning("daily mail cap reached; not mailing %s", address)
            return
        try:
            notify.send_ready(job.id, job.email, piece=job.score or "")
        except notify.MailError as exc:
            logger.warning("could not tell %s about job %s: %s",
                           job.email, job.id, exc)

    def _fail(self, job: Job, message: str) -> None:
        job.state, job.error = "error", message
        job.finished = time.time()
        job.save()
        for name, value in job.stages.items():
            if value == "active":
                job.stages[name] = "failed"
        self._emit(job.id, {"type": "error", "job": job.public()})

    def stream(self, job_id: str) -> Iterator[dict]:
        """Yield events for a job until it finishes. Replays state first."""
        job = self.get(job_id)
        if job is None:
            return
        q = self.subscribe(job_id)
        try:
            yield {"type": "state", "job": job.public()}
            if job.state in ("done", "error"):
                return
            while True:
                try:
                    event = q.get(timeout=15)
                except queue.Empty:
                    yield {"type": "ping"}        # keep the connection warm
                    continue
                yield event
                if event["type"] in ("done", "error"):
                    return
        finally:
            self.unsubscribe(job_id, q)


    def rehydrate(self) -> int:
        """Read finished jobs back off disk at start-up.

        Only the ones that produced a file: an interrupted render cannot be
        resumed, and offering a link to a video that was never finished is
        worse than admitting the job is gone.
        """
        found = 0
        root = settings.work_dir
        if not root.is_dir():
            return 0
        for manifest in sorted(root.glob("*/job.json")):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            result = data.get("result")
            if data.get("state") != "done" or not result:
                continue
            if not pathlib.Path(result).is_file():
                continue
            job = Job(id=data["id"], original_name=data.get("name", ""),
                      upload_path=pathlib.Path(data.get("upload", "")))
            job.score, job.mode = data.get("score"), data.get("mode")
            job.state, job.result = "done", pathlib.Path(result)
            job.duration, job.size_bytes = data.get("duration"), data.get("size_bytes")
            job.created = data.get("created") or time.time()
            job.finished = data.get("finished")
            job.email = data.get("email") or ""
            job.stages = {s: "done" for s in pipeline.STAGES}
            self.add(job)
            found += 1
        if found:
            logger.info("%d finished job(s) still available", found)
        return found


registry = Registry()


def new_job(original_name: str, upload_path: pathlib.Path) -> Job:
    return registry.add(Job(id=uuid.uuid4().hex[:12],
                            original_name=original_name,
                            upload_path=upload_path))
