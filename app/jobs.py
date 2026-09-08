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
import queue
import threading
import time
import traceback
import uuid
from typing import Any, Iterator

from . import pipeline
from . import render as rnd


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
    detail: str = ""
    duration: float | None = None
    size_bytes: int | None = None
    created: float = dataclasses.field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    stages: dict[str, str] = dataclasses.field(
        default_factory=lambda: {s: "pending" for s in pipeline.STAGES})

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
            self._emit(job.id, {"type": "done", "job": job.public()})

        except pipeline.PipelineError as exc:
            self._fail(job, str(exc))
        except Exception as exc:  # noqa: BLE001 - never die silently
            traceback.print_exc()
            self._fail(job, f"Unexpected failure: {exc}")

    def _fail(self, job: Job, message: str) -> None:
        job.state, job.error = "error", message
        job.finished = time.time()
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


registry = Registry()


def new_job(original_name: str, upload_path: pathlib.Path) -> Job:
    return registry.add(Job(id=uuid.uuid4().hex[:12],
                            original_name=original_name,
                            upload_path=upload_path))
