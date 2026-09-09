"""What this worker already did with this task, remembered on disk.

A broker redelivers whenever it is in any doubt: a worker that dropped its
connection, a consumer timeout, a janitor that offered a job again because
the queue looked short. Redelivery is the mechanism that makes a dead worker
recoverable, so it is not something to prevent — it is something the work has
to be safe under.

Without a record, a redelivery means one of two bad outcomes. A job that had
already finished is rendered a second time, overwriting a video somebody may
already hold a link to and sending a second email about it. A job that had
already failed on its own inputs is re-run to fail again, half an hour at a
time, for as long as the message keeps coming back.

So before doing anything, a worker asks what it did last time.

    <job folder>/render.attempt<N>.json
      {"status": "started" | "done" | "failed", ...}

Per attempt, not per job: submitting a finished job again with new colours is
a new attempt and *must* re-render. Checked on **every** delivery, not only
on ones RabbitMQ flags as redelivered — a duplicate the janitor published is
a first delivery as far as the broker is concerned, and is exactly the case
the flag misses.

It lives beside the video rather than in the job table because the worker
does not have the table, and on the compute host will not be able to reach
it. When the storage slice moves job folders to a bucket, this moves with
them as a small object and the logic is unchanged.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import time
from typing import Any

logger = logging.getLogger(__name__)

STARTED, DONE, FAILED = "started", "done", "failed"


def path_for(job_dir: pathlib.Path, attempt: int) -> pathlib.Path:
    return job_dir / f"render.attempt{attempt}.json"


def read(job_dir: pathlib.Path, attempt: int) -> dict[str, Any] | None:
    """What happened last time, or None if this attempt is new here.

    An unreadable record is treated as absent. It is a hint that saves work,
    never the authority on anything, so a corrupt one costs a re-render and
    not a wrong answer.
    """
    path = path_for(job_dir, attempt)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("unreadable attempt record %s (%s); starting over",
                       path, exc)
        return None


def write(job_dir: pathlib.Path, attempt: int, status: str, **fields: Any) -> None:
    """Record where this attempt got to. Never raises.

    Written through a temporary file so a reader never sees half of one, and
    failures are logged rather than raised: not being able to write this
    costs a re-render on redelivery, which is far better than failing a
    render that has otherwise worked.
    """
    path = path_for(job_dir, attempt)
    body = {"status": status, "at": time.time(), **fields}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".part")
        temp.write_text(json.dumps(body, indent=2), encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        logger.warning("could not record attempt %d for %s: %s",
                       attempt, job_dir.name, exc)
