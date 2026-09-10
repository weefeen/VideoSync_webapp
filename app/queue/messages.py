"""What the two sides say to each other.

Two shapes, both flat and both versioned. Flat because a nested body is
harder to read in a broker's management UI at three in the morning, which is
when anybody looks; versioned because these will change and a message
already sitting in a durable queue when the code is deployed must be
recognisably from before rather than silently mis-read.

Nothing here imports `store`, `settings` or anything that touches a disk.
That is what lets the worker carry this module to another machine.
"""
from __future__ import annotations

import dataclasses
import json
import time
from typing import Any

# Bumped when a field changes meaning or disappears. A reader that meets a
# version it does not know refuses the message rather than guessing at it:
# a message from the future is a deploy in progress, and the right response
# is to leave it for the process that understands it.
VERSION = 1


class UnknownVersion(ValueError):
    """A message from a different version of this protocol."""


@dataclasses.dataclass(frozen=True)
class RenderTask:
    """One render, described completely enough to run it elsewhere.

    Everything the worker needs is here. It does not read the job table,
    and on the next host it could not: `upload` is the only field that names
    a place on a particular machine, and it is the one that becomes an
    object key when the storage slice lands.

    What is deliberately NOT here: the email address. It stays on the web
    box with the SMTP credentials, and the mail is sent there when `done`
    arrives, so a visitor's address never travels to a compute instance that
    is created and destroyed.
    """
    job_id: str
    upload: str
    package: str
    attempt: int = 1
    kind: str = "render"          # or "ping", which the worker answers idle
    mode: str | None = None
    duration: float | None = None
    style: dict[str, Any] = dataclasses.field(default_factory=dict)
    meta: dict[str, Any] = dataclasses.field(default_factory=dict)
    queued_at: float = 0.0

    def to_json(self) -> str:
        return _dump(self, "kind")

    @classmethod
    def from_json(cls, raw: str | bytes) -> "RenderTask":
        return cls(**_load(raw, cls))


@dataclasses.dataclass(frozen=True)
class Event:
    """One thing the worker has learned, on its way back to the table.

    `seq` orders a job's events within an attempt. One queue with one
    consumer already delivers them in order; the number costs nothing and
    means a duplicate can be recognised without reasoning about the
    transport that carried it.
    """
    job_id: str
    type: str                     # started progress heartbeat done failed pong
    attempt: int = 1
    seq: int = 0
    at: float = dataclasses.field(default_factory=time.time)
    worker: str = ""
    stage: str | None = None
    detail: str = ""
    # What the work has cost so far, measured where it happens and carried
    # back rather than sampled from outside. On the compute host nothing
    # else can see these: the box is created for one job and destroyed, so
    # a number left behind on it is a number nobody ever reads.
    #
    # Both are CUMULATIVE for the attempt, self and children together — the
    # children are the point, since ffmpeg is what actually burns the
    # machine. The ledger takes the difference between one stage boundary
    # and the next.
    cpu_seconds: float | None = None
    peak_rss: int | None = None
    # done
    result: str | None = None
    mode: str | None = None       # which alignment actually ran
    output_bytes: int | None = None
    elapsed: float | None = None
    # failed — the same fields the stage record has always kept, so a
    # failure reads the same whether it crossed a process boundary or not.
    error: str = ""
    error_class: str = ""
    command: str = ""
    returncode: int | None = None
    stderr_tail: str = ""

    def to_json(self) -> str:
        return _dump(self, "type")

    @classmethod
    def from_json(cls, raw: str | bytes) -> "Event":
        return cls(**_load(raw, cls))


# --------------------------------------------------------------------------
def _dump(message: Any, discriminator: str) -> str:
    """Serialise, with the version and the kind first so a tail is readable."""
    body = dataclasses.asdict(message)
    ordered = {"v": VERSION, discriminator: body.pop(discriminator), **body}
    return json.dumps(ordered, ensure_ascii=False)


def _load(raw: str | bytes, cls: type) -> dict:
    """Parse and check the version, dropping fields this build does not know.

    Unknown fields are ignored rather than refused: adding one must not
    require both sides to be deployed at the same instant, which on two
    hosts they never are. Removing or repurposing one is what VERSION is
    for.
    """
    body = json.loads(raw)
    version = body.pop("v", None)
    if version != VERSION:
        raise UnknownVersion(
            f"{cls.__name__} is version {version!r}, this build speaks "
            f"{VERSION}. A message from another deployment.")
    known = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in body.items() if k in known}
