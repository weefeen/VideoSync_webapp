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

import contextlib
import logging
import queue
import threading
import time
from typing import Callable, Protocol

from ..settings import settings
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


# --------------------------------------------------------------------------
# the broker
# --------------------------------------------------------------------------
RENDER_QUEUE = "vsw.render"
EVENTS_QUEUE = "vsw.events"
DEAD_EXCHANGE = "vsw.dlx"

# Declared by BOTH sides on every connect, from this one table, so the two
# can start in either order and cannot disagree about the arguments.
#
# All three arguments are set on day one on purpose. RabbitMQ refuses to
# redeclare an existing queue with different arguments (PRECONDITION_FAILED),
# so adding one later means deleting the queue on every broker it exists on.
#
# `x-consumer-timeout` is the one that is not optional. Since 3.8.15 the
# broker closes the channel of a consumer whose delivery stays unacked longer
# than this, and the default is 30 MINUTES — under our worst case of about 32
# for a 25-minute upload. A render would have its delivery pulled back
# mid-encode, every time, only for the longest jobs. Three hours is the cap
# with room to spare. Needs RabbitMQ >= 3.12.
TOPOLOGY = {
    RENDER_QUEUE: {"x-dead-letter-exchange": DEAD_EXCHANGE,
                   "x-consumer-timeout": 10_800_000,
                   "x-max-priority": 10},
    EVENTS_QUEUE: {"x-dead-letter-exchange": DEAD_EXCHANGE},
    f"{RENDER_QUEUE}.dead": {},
    f"{EVENTS_QUEUE}.dead": {},
}


def declare(channel) -> None:
    """Make the queues exist. Safe to call on every connect."""
    channel.exchange_declare(DEAD_EXCHANGE, exchange_type="direct",
                             durable=True)
    for name, arguments in TOPOLOGY.items():
        channel.queue_declare(queue=name, durable=True, arguments=arguments)
    # Dead letters keep their original routing key, so one exchange serves
    # both queues and each lands somewhere named after where it came from.
    for name in (RENDER_QUEUE, EVENTS_QUEUE):
        channel.queue_bind(f"{name}.dead", DEAD_EXCHANGE, routing_key=name)


class AmqpTransport:
    """RabbitMQ. Messages outlive the process, which is the whole point.

    Three separate connection owners, because `pika.BlockingConnection` is
    NOT thread-safe and one connection per thread is the only safe rule:

      * publishing opens a short-lived connection per message and closes it.
        Wasteful in principle, thread-safe by construction, and at a few jobs
        a day the waste is not measurable.
      * `consume_tasks` holds one long-lived connection on its own thread.
      * `consume_events` holds another on its own.

    Nothing in a request handler ever touches pika.
    """

    survives_restart = True

    def __init__(self, url: str) -> None:
        self._url = url

    # -- connections -----------------------------------------------------
    def _open(self):
        import pika
        parameters = pika.URLParameters(self._url)
        # Not 0. Disabling heartbeats disables dead-peer detection, and a
        # compute instance that is destroyed mid-render would then keep its
        # delivery bound until the broker noticed some other way.
        parameters.heartbeat = 60
        parameters.blocked_connection_timeout = 90
        return pika.BlockingConnection(parameters)

    def _publish(self, queue_name: str, body: str, priority: int = 0) -> None:
        import pika
        try:
            connection = self._open()
            try:
                channel = connection.channel()
                declare(channel)
                # Confirms turn a silent loss into an exception. Without them
                # a publish can vanish while the caller logs success, and the
                # job sits in `queued` for ever with nobody wondering why.
                channel.confirm_delivery()
                channel.basic_publish(
                    exchange="", routing_key=queue_name, body=body,
                    properties=pika.BasicProperties(
                        delivery_mode=2, content_type="application/json",
                        priority=priority))
            finally:
                connection.close()
        except Exception as exc:                  # noqa: BLE001
            raise TransportError(f"could not publish to {queue_name}: {exc}") from exc

    # -- down ------------------------------------------------------------
    def publish_task(self, task: RenderTask) -> None:
        self._publish(RENDER_QUEUE, task.to_json())

    def consume_tasks(self, handle: Callable[[RenderTask, Ack], None]) -> None:
        """Take one task at a time and run it, reconnecting for ever."""
        backoff = 5
        while True:
            try:
                self._consume_tasks_once(handle)
                backoff = 5
            except Exception as exc:              # noqa: BLE001
                logger.warning("task consumer lost its connection: %s", exc)
                logger.info("retrying in %ds", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)

    def _consume_tasks_once(self, handle: Callable[[RenderTask, Ack], None]) -> None:
        connection = self._open()
        try:
            channel = connection.channel()
            declare(channel)
            # One delivery at a time. With one consumer that IS the "one
            # render at a time" cap, enforced by the broker rather than by us
            # remembering to.
            channel.basic_qos(prefetch_count=1)
            logger.info("consuming %s", RENDER_QUEUE)

            for method, _props, body in channel.consume(
                    RENDER_QUEUE, inactivity_timeout=5.0):
                if method is None:
                    continue                      # idle; heartbeats serviced
                try:
                    task = RenderTask.from_json(body)
                except Exception:                 # noqa: BLE001
                    # It will not parse next time either. Dead-lettered with
                    # the reason rather than redelivered for ever.
                    logger.exception("unreadable task, dead-lettering: %.400s",
                                     body)
                    channel.basic_reject(method.delivery_tag, requeue=False)
                    continue
                self._run_off_thread(connection, handle, task)
                channel.basic_ack(method.delivery_tag)
        finally:
            with contextlib.suppress(Exception):
                connection.close()

    @staticmethod
    def _run_off_thread(connection, handle, task: RenderTask) -> None:
        """Do the work on another thread, and keep this one talking.

        `BlockingConnection` only services heartbeats when control returns to
        it. Running a 30-minute render inside the delivery callback — which
        is what the pattern this is adapted from does — means the heartbeat
        stops for the length of the job, the broker declares the consumer
        dead after about three minutes and requeues the message mid-encode,
        and the eventual ack lands on a channel that is gone. That pattern
        only survives because its completion flag catches the redelivery.
        """
        done = threading.Event()

        def run() -> None:
            try:
                handle(task, lambda: None)
            finally:
                done.set()

        threading.Thread(target=run, name=f"render-{task.job_id}",
                         daemon=True).start()
        while not done.wait(timeout=0):
            connection.process_data_events(time_limit=1.0)

    # -- up --------------------------------------------------------------
    def publish_event(self, event: Event) -> None:
        self._publish(EVENTS_QUEUE, event.to_json())

    def consume_events(self, apply: Callable[[Event], None]) -> None:
        backoff = 5
        while True:
            try:
                connection = self._open()
                try:
                    channel = connection.channel()
                    declare(channel)
                    channel.basic_qos(prefetch_count=100)
                    logger.info("consuming %s", EVENTS_QUEUE)
                    for method, _props, body in channel.consume(
                            EVENTS_QUEUE, inactivity_timeout=5.0):
                        if method is None:
                            continue
                        try:
                            apply(Event.from_json(body))
                        except Exception:         # noqa: BLE001
                            logger.exception("could not apply: %.400s", body)
                        # Acked either way: an event that cannot be applied
                        # will not apply on redelivery either, and the row it
                        # was about is recoverable from the lease.
                        channel.basic_ack(method.delivery_tag)
                finally:
                    with contextlib.suppress(Exception):
                        connection.close()
                backoff = 5
            except Exception as exc:              # noqa: BLE001
                logger.warning("event consumer lost its connection: %s", exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, 300)

    # -- what the janitor asks --------------------------------------------
    def render_depth(self) -> tuple[int, int] | None:
        """(ready, consumers), or None if the broker could not be asked."""
        try:
            connection = self._open()
            try:
                channel = connection.channel()
                result = channel.queue_declare(
                    queue=RENDER_QUEUE, durable=True, passive=True)
                return result.method.message_count, result.method.consumer_count
            finally:
                with contextlib.suppress(Exception):
                    connection.close()
        except Exception as exc:                  # noqa: BLE001
            logger.warning("could not measure the queue: %s", exc)
            return None


_transport: Transport | None = None
_transport_lock = threading.Lock()


def quiet_pika() -> None:
    """Stop pika narrating every connection and channel at INFO.

    It logs six lines per connection, and this app opens a short-lived one
    per publish, so the four lines a render actually produces are buried.
    Its warnings are the part worth reading — a dropped connection, a
    refused declaration — and those still come through.
    """
    logging.getLogger("pika").setLevel(logging.WARNING)


def transport() -> Transport:
    """The one transport this process uses.

    `local` unless RABBITMQ_URL is set, so a fresh checkout and every
    developer machine behave as they always have, with no broker to install.
    """
    global _transport
    with _transport_lock:
        if _transport is None:
            url = settings.rabbitmq_url
            if url:
                _transport = AmqpTransport(url)
                logger.info("queue transport: amqp")
            else:
                _transport = LocalTransport()
                logger.info("queue transport: local (in this process)")
        return _transport


def reset() -> None:
    """Forget the transport. For tests, which need a clean queue each time."""
    global _transport
    with _transport_lock:
        _transport = None
