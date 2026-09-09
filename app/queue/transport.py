"""How a task reaches a worker, and how its news gets back.

One protocol, so that the arrangement can change without the work changing.
Today there is one implementation and it is two in-memory queues; the point
of writing it as a seam now is that the broker becomes a second
implementation of `Transport` rather than an edit to everything that touches
a job.

**The local transport is not a stub.** It is what runs on the development
machine and it is what runs in CI, so the message shapes, the ordering, the
attempt rules and the recovery sweep are all exercised on every push without
anyone installing a broker. What a broker adds is that messages survive the
process — which is exactly the one behaviour `survives_restart` names, and
the only thing the janitor has to know about which transport it is talking
to.

Every message is serialised even when it is going nowhere. A `pathlib.Path`
smuggled into `style`, or a `set` in `meta`, then fails on the developer's
machine instead of on the node the first time a broker is real.
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, Protocol

from .messages import Event, RenderTask

logger = logging.getLogger(__name__)

# What a consumer calls when a task is finished with — successfully or not.
# On a broker this is the ack that lets the message be forgotten; here it
# only drops it. Taking it as a callback rather than returning a value keeps
# the shape the broker needs, where the ack must happen on another thread.
Ack = Callable[[], None]


class TransportError(RuntimeError):
    """The queue could not be reached. The row is the promise; try again."""


class Transport(Protocol):
    """Obligations go down, facts come up."""

    survives_restart: bool

    def publish_task(self, task: RenderTask) -> None: ...
    def consume_tasks(self, handle: Callable[[RenderTask, Ack], None]) -> None: ...
    def publish_event(self, event: Event) -> None: ...
    def consume_events(self, apply: Callable[[Event], None]) -> None: ...
    def render_depth(self) -> tuple[int, int] | None: ...


class LocalTransport:
    """Both halves in one process, talking through two queues.

    `survives_restart` is False and that is the whole difference: when this
    process stops, anything queued is gone, so the janitor re-feeds the
    queue from the table at startup. A broker holds them instead, and the
    janitor does nothing.
    """

    survives_restart = False

    def __init__(self) -> None:
        self._tasks: queue.Queue[str] = queue.Queue()
        self._events: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._in_flight = 0

    # -- down ------------------------------------------------------------
    def publish_task(self, task: RenderTask) -> None:
        self._tasks.put(task.to_json())

    def consume_tasks(self, handle: Callable[[RenderTask, Ack], None]) -> None:
        """Take tasks and run them, one at a time, forever."""
        while True:
            body = self._tasks.get()
            try:
                task = RenderTask.from_json(body)
            except Exception:                     # noqa: BLE001
                # A body that cannot be parsed is not going to parse on the
                # next attempt either. Dropped with the reason, rather than
                # retried until it fills the log.
                logger.exception("unreadable task, dropped: %.400s", body)
                continue
            with self._lock:
                self._in_flight += 1
            try:
                handle(task, lambda: None)
            except Exception:                     # noqa: BLE001
                # handle() reports its own failures; reaching here means the
                # reporting itself broke, and the loop must survive it.
                logger.exception("worker raised past its own reporting")
            finally:
                with self._lock:
                    self._in_flight -= 1

    # -- up --------------------------------------------------------------
    def publish_event(self, event: Event) -> None:
        self._events.put(event.to_json())

    def consume_events(self, apply: Callable[[Event], None]) -> None:
        """Apply what the worker reports, in the order it reported it."""
        while True:
            body = self._events.get()
            try:
                apply(Event.from_json(body))
            except Exception:                     # noqa: BLE001
                logger.exception("could not apply event: %.400s", body)

    # -- what the janitor asks --------------------------------------------
    def render_depth(self) -> tuple[int, int] | None:
        """(waiting, workers). Approximate, and only used as a lower bound."""
        with self._lock:
            return self._tasks.qsize() + self._in_flight, 1


_transport: Transport | None = None
_transport_lock = threading.Lock()


def transport() -> Transport:
    """The one transport this process uses."""
    global _transport
    with _transport_lock:
        if _transport is None:
            _transport = LocalTransport()
            logger.info("queue transport: local (in this process)")
        return _transport


def reset() -> None:
    """Forget the transport. For tests, which need a clean queue each time."""
    global _transport
    with _transport_lock:
        _transport = None
