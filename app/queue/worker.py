"""The half that does the work.

Everything it needs arrives in a `RenderTask` and everything it learns leaves
as an `Event`. It never opens `jobs.sqlite` — not because the file is far
away today, but because on the compute host it will be, and a worker that
has quietly grown a habit of reading the table is a worker that cannot move.

Read the imports as the specification: `pipeline`, `render`, `settings` and
the messages. No `store`, no `notify`, no `limits`.
"""
from __future__ import annotations

import itertools
import logging
import os
import pathlib
import shlex
import socket
import time
import traceback
from typing import Callable

from .. import pipeline
from .. import render as rnd
from .. import storage
from . import attempt
from .messages import Event, RenderTask

logger = logging.getLogger(__name__)

Publish = Callable[[Event], None]


def name() -> str:
    """Who did the work. Useless with one worker, necessary with two."""
    return f"{socket.gethostname()}:{os.getpid()}"


def usage() -> tuple[float | None, int | None]:
    """CPU seconds burned and the high-water mark of memory, so far.

    Self AND children, because the children are the point: ffmpeg is what
    actually consumes this machine, and a measurement of the Python process
    alone would report a render as nearly free.

    `resource` is POSIX-only and absent on Windows, where the local
    transport runs everything in one process anyway — so this returns
    (None, None) there rather than pretending. Nothing downstream requires
    a number.

    `ru_maxrss` is a high-water mark the kernel never resets, so it is
    cumulative for the process, not per stage. That is what makes it exact
    without a sampling thread: the rise between two stage boundaries is
    what the stage in between cost.
    """
    try:
        import resource
    except ImportError:                          # Windows
        return None, None
    me = resource.getrusage(resource.RUSAGE_SELF)
    kids = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = me.ru_utime + me.ru_stime + kids.ru_utime + kids.ru_stime
    # Linux reports kilobytes; macOS reports bytes. Only Linux runs this.
    peak = max(me.ru_maxrss, kids.ru_maxrss) * 1024
    return round(cpu, 3), peak


def handle_task(task: RenderTask, publish: Publish) -> bool:
    """Run one task and report it. True if it produced a video.

    Never raises for a failure of the work: a render that goes wrong is a
    `failed` event, which is a fact like any other. It raises only if the
    reporting itself is broken, because then nothing downstream can be
    trusted to have heard anything.
    """
    seq = itertools.count(1)
    me = name()

    def say(kind: str, **fields) -> None:
        # Every event carries the running cost, so a stage boundary is
        # also a measurement point and nothing extra has to be scheduled.
        #
        # PUBLISHING MUST NOT BE ABLE TO FAIL THE RENDER. `publish` opens a
        # fresh connection per message and raises TransportError on any broker
        # hiccup. Left to propagate it did two wrong things: a `progress`
        # publish is called from inside the render via on_progress, so a
        # transient failure unwound a render twenty minutes in as "could not
        # publish"; and a `done` publish that failed fell into the caller's
        # `except Exception`, which overwrote the just-written DONE record
        # with FAILED — reporting a finished, stored, delivered video as
        # failed. The render's real outcome is already on disk in the attempt
        # record before any terminal event is published, and the webside
        # sweep re-offers a job whose outcome never arrived, so a dropped
        # event is recovered rather than lost. So this swallows and logs.
        cpu, rss = usage()
        try:
            publish(Event(job_id=task.job_id, type=kind, attempt=task.attempt,
                          seq=next(seq), worker=me,
                          cpu_seconds=cpu, peak_rss=rss, **fields))
        except Exception:                              # noqa: BLE001
            logger.warning("job %s: could not publish %s event (the outcome "
                           "is recorded; the sweep will carry it)",
                           task.job_id, kind, exc_info=True)

    if task.kind == "ping":
        # Answered without touching ffmpeg, so the round trip through a real
        # broker can be checked in seconds rather than in half an hour.
        say("pong")
        return False

    # What did this worker already do with this exact attempt? Asked on
    # EVERY delivery, not only ones the broker flags as redelivered: a
    # duplicate the janitor published is a first delivery as far as RabbitMQ
    # is concerned, and that is the case the flag misses.
    job_dir = pipeline.job_folder(task.job_id)
    already = attempt.read(job_dir, task.attempt)
    if already is not None and already.get("status") in (attempt.DONE,
                                                         attempt.FAILED):
        return _repeat(already, say, task)

    attempt.write(job_dir, task.attempt, attempt.STARTED, worker=me)
    say("started")
    began = time.time()
    try:
        package = pipeline.find_package(task.package)
        if package is None:
            raise pipeline.PipelineError(
                f"No score package named {task.package!r}.")

        # Where the recording actually is on THIS host. On the web box it is
        # `task.upload`, sitting on the shared disk, and this is a no-op. On a
        # compute node that path does not exist — the node cannot see the web
        # box's disk — so when the file is absent and the web side staged a
        # copy in the bucket (`input_key`), it is fetched. This is the wire
        # that lets the render run on a throwaway machine at all.
        #
        # When the file is absent and there is nothing to fetch, the original
        # path is passed through unchanged and `pipeline.run` reports the
        # missing input as it always did — this only ADDS the fetch, it does
        # not change what happens when no fetch is possible.
        video = pathlib.Path(task.upload)
        if not video.is_file() and task.input_key and storage.available():
            say("progress", stage="probe", detail="fetching the recording")
            video = pipeline.job_folder(task.job_id) / (
                "input" + pathlib.Path(task.upload).suffix.lower())
            storage.get(task.input_key, video)

        style = rnd.Style(**task.style) if task.style else None
        result = pipeline.run(
            package, video, task.job_id, style,
            task.mode, task.meta,
            lambda stage, detail="": say("progress", stage=stage, detail=detail))
        size = (result.output.stat().st_size
                if result.output.is_file() else None)

        # THE HARD ORDERING RULE. "The render succeeded" and "the result is
        # safe" are different events, and only the second may be announced.
        # The upload is confirmed with a HEAD before this returns; until it
        # does, the message stays unacknowledged, the job is not finished,
        # and a host destroyed here costs a re-render rather than somebody's
        # video.
        #
        # A failure to store fails the JOB, deliberately. The alternative —
        # report done and keep the file locally — produces a job that looks
        # finished and a video that dies with the machine, which is the exact
        # outcome this exists to prevent.
        object_key = None
        if storage.available() and result.output.is_file():
            object_key = storage.output_key(task.job_id,
                                            result.output.suffix or ".mp4")
            say("progress", stage="store",
                detail="putting the result somewhere it survives")
            size = storage.put(result.output, object_key)

            # And everything else the job made: the alignment, the measures,
            # the per-attempt record. 0.2 MB against a 131 MB video, and the
            # half that is expensive to recreate — for a corpus it is the
            # valuable half. Once the renderer is a machine that is
            # destroyed afterwards, not keeping these means losing them.
            #
            # AFTER the video and never allowed to fail the job: the video
            # is what was promised, it is already confirmed stored above,
            # and a working file that will not upload is worth a log line
            # rather than a render thrown away.
            try:
                # Written first, so it is stored along with what it
                # describes rather than needing a second pass.
                storage.write_manifest(
                    job_dir, task.job_id,
                    package=task.package, mode=result.mode,
                    style=task.style, duration=task.duration,
                    output=result.output, output_bytes=size,
                    elapsed=round(time.time() - began, 1))
                kept = storage.put_tree(job_dir, task.job_id,
                                        output=result.output)
                if kept["stored"]:
                    say("progress", stage="store",
                        detail=f"kept {len(kept['stored'])} working file(s)")
            except Exception:                          # noqa: BLE001
                logger.warning("job %s: working files could not be "
                               "stored", task.job_id, exc_info=True)

        elapsed = round(time.time() - began, 1)
        # Recorded BEFORE the event is published. If this worker dies in the
        # gap, the redelivery finds the record and republishes the outcome
        # instead of rendering the same thing again.
        attempt.write(job_dir, task.attempt, attempt.DONE,
                      result=str(result.output), mode=result.mode,
                      object_key=object_key,
                      output_bytes=size, elapsed=elapsed)
        say("done", result=str(result.output), mode=result.mode,
            object_key=object_key, output_bytes=size, elapsed=elapsed)
        return True

    except pipeline.PipelineError as exc:
        # A tool failure carries the command that produced it; anything else
        # has only its message. Both are reported, so "which stage, what
        # inputs, what error" has an answer without a log dig — and the
        # command matters more than it looks, because a render's ffmpeg
        # invocation is assembled from the visitor's own crop, colours and
        # panel choices and cannot be reconstructed by hand.
        detail = {"error": str(exc), "error_class": type(exc).__name__,
                  "command": shlex.join(getattr(exc, "command", []) or []),
                  "returncode": getattr(exc, "returncode", None),
                  "stderr_tail": (getattr(exc, "stderr", "") or "")[-4000:]}
        # A render that failed on its own inputs fails the same way next
        # time. Recorded so a redelivery reports it again rather than
        # spending another half hour proving it.
        attempt.write(job_dir, task.attempt, attempt.FAILED, **detail)
        say("failed", **detail)
        return False

    except Exception as exc:                  # noqa: BLE001 - never die silently
        traceback.print_exc()
        detail = {"error": f"Unexpected failure: {exc}",
                  "error_class": type(exc).__name__}
        attempt.write(job_dir, task.attempt, attempt.FAILED, **detail)
        say("failed", **detail)
        return False


def _repeat(record: dict, say, task: RenderTask) -> bool:
    """Report an outcome this worker already reached, without repeating it.

    The video exists, or the failure is settled. Either way the work is done
    and only the news is missing — this attempt's `done` or `failed` never
    reached the table, or reached it and the ack did not get back to the
    broker before the worker stopped.
    """
    status = record.get("status")
    logger.info("attempt %d of %s already %s here; reporting it again, "
                "not rendering again", task.attempt, task.job_id, status)
    if status == attempt.DONE:
        say("done", result=record.get("result"), mode=record.get("mode"),
            object_key=record.get("object_key"),
            output_bytes=record.get("output_bytes"),
            elapsed=record.get("elapsed"))
        return True
    say("failed", error=record.get("error", "") or "It failed before.",
        error_class=record.get("error_class", ""),
        command=record.get("command", ""),
        returncode=record.get("returncode"),
        stderr_tail=record.get("stderr_tail", ""))
    return False


def main() -> int:
    """Run as a worker process: `python -m app.queue.worker`.

    This is the compute host's whole program. It consumes tasks and reports;
    it serves nothing, and it never opens the job table. Without
    RABBITMQ_URL there is nothing to consume from another process, so it
    says so rather than sitting silently on an in-memory queue nobody else
    can reach.
    """
    import logging
    import signal
    import sys
    import threading

    from ..settings import settings
    from . import transport as transport_module
    from .transport import transport
    from . import webside

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    transport_module.quiet_pika()
    if not settings.rabbitmq_url:
        print("RABBITMQ_URL is not set. A separate worker process needs a "
              "broker to take work from; with the in-process queue the web "
              "process is already the worker.", file=sys.stderr)
        return 2

    # The web box's own worker STANDS ASIDE when compute is on. Scale-to-zero
    # means a disposable node does the rendering, and a second consumer on the
    # web box — an already-connected one — would take the task first, so the
    # scaler creates a node that idles for a paid hour while this box renders.
    # A node (the `vsw-compute` user) always consumes; the web box (the `vsw`
    # user) consumes only while compute is off, which is also the in-process
    # fallback. Identified by the broker user, the one signal a worker has.
    if settings.compute_enabled and not settings.is_compute_node:
        logger.info("compute is enabled and this is the web box; the render "
                    "worker stands aside so a compute node takes the work")
        # Idle instead of exiting: `Restart=always` would otherwise spin this
        # unit forever. Block until signalled; the handlers raise SystemExit,
        # which interrupts the wait. `threading.Event().wait()` blocks on
        # every platform, unlike signal.pause().
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        try:
            threading.Event().wait()
        except (SystemExit, KeyboardInterrupt):
            pass
        return 0

    bus = transport()
    logger.info("worker %s consuming tasks", name())

    def stop(signum, _frame):
        logger.info("signal %s; finishing the current task then exiting", signum)
        raise SystemExit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        bus.consume_tasks(webside._handle)
    except SystemExit:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
