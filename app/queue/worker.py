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
from .messages import Event, RenderTask

logger = logging.getLogger(__name__)

Publish = Callable[[Event], None]


def name() -> str:
    """Who did the work. Useless with one worker, necessary with two."""
    return f"{socket.gethostname()}:{os.getpid()}"


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
        publish(Event(job_id=task.job_id, type=kind, attempt=task.attempt,
                      seq=next(seq), worker=me, **fields))

    if task.kind == "ping":
        # Answered without touching ffmpeg, so the round trip through a real
        # broker can be checked in seconds rather than in half an hour.
        say("pong")
        return False

    say("started")
    began = time.time()
    try:
        package = pipeline.find_package(task.package)
        if package is None:
            raise pipeline.PipelineError(
                f"No score package named {task.package!r}.")
        style = rnd.Style(**task.style) if task.style else None
        result = pipeline.run(
            package, pathlib.Path(task.upload), task.job_id, style,
            task.mode, task.meta,
            lambda stage, detail="": say("progress", stage=stage, detail=detail))
        say("done", result=str(result.output), mode=result.mode,
            output_bytes=(result.output.stat().st_size
                          if result.output.is_file() else None),
            elapsed=round(time.time() - began, 1))
        return True

    except pipeline.PipelineError as exc:
        # A tool failure carries the command that produced it; anything else
        # has only its message. Both are reported, so "which stage, what
        # inputs, what error" has an answer without a log dig — and the
        # command matters more than it looks, because a render's ffmpeg
        # invocation is assembled from the visitor's own crop, colours and
        # panel choices and cannot be reconstructed by hand.
        say("failed", error=str(exc), error_class=type(exc).__name__,
            command=shlex.join(getattr(exc, "command", []) or []),
            returncode=getattr(exc, "returncode", None),
            stderr_tail=(getattr(exc, "stderr", "") or "")[-4000:])
        return False

    except Exception as exc:                  # noqa: BLE001 - never die silently
        traceback.print_exc()
        say("failed", error=f"Unexpected failure: {exc}",
            error_class=type(exc).__name__)
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
