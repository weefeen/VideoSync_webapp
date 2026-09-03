"""In-memory job registry with a background worker thread per render.

Deliberately simple: one process, no broker, no database. A render is a
long ffmpeg run, so it goes on a thread and the page follows along over
Server-Sent Events. State is lost on restart, which is fine for local use
and is the thing to replace first if this ever runs for real users.
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

# How long a finished job's files stick around before cleanup can reap them.
RETENTION_SECONDS = 60 * 60 * 6


@dataclasses.dataclass
class Job:
    id: str
    original_name: str
    upload_path: pathlib.Path
    score_id: str | None = None
    state: str = "uploaded"          # uploaded | running | done | error
    error: str | None = None
    result: pathlib.Path | None = None
    duration: float | None = None
    size_bytes: int | None = None
    created: float = dataclasses.field(default_factory=time.time)
    stages: dict[str, str] = dataclasses.field(
        default_factory=lambda: {s: "pending" for s in pipeline.STAGES})

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.original_name,
            "state": self.state,
            "error": self.error,
            "score_id": self.score_id,
            "duration": self.duration,
            "size_bytes": self.size_bytes,
            "stages": dict(self.stages),
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
    def start(self, job: Job, score_id: str, meta: dict) -> None:
        """Kick off the render on a background thread."""
        job.score_id = str(score_id)
        job.state = "running"
        threading.Thread(target=self._run, args=(job, meta),
                         name=f"render-{job.id}", daemon=True).start()

    def _run(self, job: Job, meta: dict) -> None:
        def on_progress(stage: str, status: str, detail: str = "") -> None:
            job.stages[stage] = status
            self._emit(job.id, {"type": "progress", "stage": stage,
                                "status": status, "detail": detail,
                                "job": job.public()})

        try:
            task = pipeline.build_task(
                job_id=f"job_r1_c{job.id[:6]}_s{job.score_id}",
                score_id=job.score_id,
                meta=meta,
                ext=job.upload_path.suffix or ".mp4",
            )
            paths = pipeline.prepare_job(task, job.upload_path)
            job.result = pipeline.run(task, paths, on_progress)
            job.state = "done"
            self._emit(job.id, {"type": "done", "job": job.public()})
        except pipeline.PipelineError as exc:
            job.state, job.error = "error", str(exc)
            self._emit(job.id, {"type": "error", "job": job.public()})
        except Exception as exc:  # noqa: BLE001 - never kill the thread silently
            job.state = "error"
            job.error = f"Unexpected failure: {exc}"
            traceback.print_exc()
            self._emit(job.id, {"type": "error", "job": job.public()})

    def stream(self, job_id: str) -> Iterator[dict]:
        """Yield events for a job until it finishes. Replays current state first."""
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
                    yield {"type": "ping"}          # keep the connection warm
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
