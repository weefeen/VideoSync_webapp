"""Check the app against a real broker. Needs RABBITMQ_URL set.

    python tools/brokercheck.py ping        round trip, seconds, no ffmpeg
    python tools/brokercheck.py topology    the queues, and what they hold
    python tools/brokercheck.py submit FILE upload and queue a real render
    python tools/brokercheck.py watch ID    follow one job to the end

These are deliberately NOT in `tools/selftest.py`: that must run anywhere,
including a CI runner with no broker, no ffmpeg and no score packages. This
needs all three, so it is run by hand on a machine that has them, and what it
prints goes into `docs/deployment-log.md` like every other proof.

`ping` is the one to reach for first. It asks the worker to answer without
touching ffmpeg, so a broken URL, a missing vhost, a wrong password or a
queue whose arguments disagree all show up in about a second instead of at
the end of a half-hour render.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from app import store                            # noqa: E402
from app.queue import transport as tr            # noqa: E402
from app.queue.messages import RenderTask        # noqa: E402
from app.settings import settings                # noqa: E402


def need_broker() -> tr.AmqpTransport:
    if not settings.rabbitmq_url:
        sys.exit("RABBITMQ_URL is not set. This checks a real broker; with "
                 "the in-process queue there is nothing to check.")
    return tr.AmqpTransport(settings.rabbitmq_url)


def cmd_topology(_args) -> int:
    """What the broker actually holds, and whether it agrees with us."""
    import pika
    bus = need_broker()
    connection = bus._open()
    try:
        channel = connection.channel()
        tr.declare(channel)                      # would raise on a mismatch
        print(f"  {'queue':22} {'ready':>8} {'consumers':>10}")
        for name in tr.TOPOLOGY:
            result = channel.queue_declare(queue=name, durable=True,
                                           passive=True)
            print(f"  {name:22} {result.method.message_count:>8} "
                  f"{result.method.consumer_count:>10}")
    except pika.exceptions.ChannelClosedByBroker as exc:
        print(f"\n  The broker refused our declaration: {exc}")
        print("  A queue exists with different arguments. It has to be "
              "deleted before it can be declared this way — RabbitMQ will "
              "not change them in place.")
        return 1
    finally:
        connection.close()
    return 0


def cmd_ping(args) -> int:
    """Publish a ping and wait for the worker's pong."""
    bus = need_broker()
    seen: list = []

    import threading
    stop = threading.Event()

    def listen() -> None:
        import pika
        connection = bus._open()
        channel = connection.channel()
        tr.declare(channel)
        for method, _props, body in channel.consume(tr.EVENTS_QUEUE,
                                                    inactivity_timeout=1.0):
            if stop.is_set():
                break
            if method is None:
                continue
            from app.queue.messages import Event
            event = Event.from_json(body)
            channel.basic_ack(method.delivery_tag)
            if event.type == "pong":
                seen.append(event)
                break
        with __import__("contextlib").suppress(Exception):
            connection.close()

    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    time.sleep(0.5)

    began = time.time()
    bus.publish_task(RenderTask(job_id="ping", upload="", package="",
                                kind="ping"))
    print(f"  published a ping to {tr.RENDER_QUEUE}")
    thread.join(timeout=args.timeout)
    stop.set()

    if not seen:
        print(f"  NO ANSWER in {args.timeout:.0f}s.")
        print("  Is a worker running?  python -m app.queue.worker")
        return 1
    print(f"  pong from {seen[0].worker} in {time.time() - began:.2f}s")
    return 0


def cmd_submit(args) -> int:
    """Upload a file and queue it, through the app's own routes."""
    from app.routes import create_app
    app = create_app()
    client = app.test_client()

    works = client.get("/api/library").get_json()["works"]
    if not works:
        sys.exit("no score packages installed here")
    score = args.score or works[0]["id"]

    path = pathlib.Path(args.file)
    with path.open("rb") as fh:
        response = client.post("/api/upload", content_type="multipart/form-data",
                               data={"video": (fh, path.name), "rights": "1"})
    if response.status_code != 200:
        sys.exit(f"upload refused: {response.get_json()}")
    job_id = response.get_json()["job"]["id"]

    response = client.post(f"/api/jobs/{job_id}/render",
                           json={"score": score, "mode": "reference"})
    if response.status_code != 200:
        sys.exit(f"render refused: {response.get_json()}")

    row = store.get_job(job_id)
    print(f"  job         {job_id}")
    print(f"  score       {score}")
    print(f"  published   {'yes' if row['published_at'] else 'NO — the sweep will retry'}")
    print(f"\n  follow it:  python tools/brokercheck.py watch {job_id}")
    return 0


def cmd_watch(args) -> int:
    """Follow one job to the end, printing every change."""
    began = time.time()
    last = None
    while True:
        row = store.get_job(args.job_id)
        if row is None:
            sys.exit(f"no such job: {args.job_id}")
        now = (row["state"], row["stage"], row["detail"])
        if now != last:
            print(f"  {time.time() - began:6.0f}s  {row['state']:8} "
                  f"{(row['stage'] or '-'):8} {(row['detail'] or '')[:44]}")
            last = now
        if row["state"] in (store.DONE, store.ERROR):
            print(f"\n  error   {row['error']}")
            print(f"  result  {row['result']}")
            for run in store.stage_runs(args.job_id):
                print(f"  run     {run['stage']} attempt {run['attempt']} "
                      f"{run['state']} elapsed {run['elapsed']} "
                      f"{run['error_class'] or ''}")
            return 0 if row["state"] == store.DONE else 1
        time.sleep(5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)

    subs.add_parser("topology", help="what the broker holds")

    ping = subs.add_parser("ping", help="round trip with no ffmpeg")
    ping.add_argument("--timeout", type=float, default=30.0)

    submit = subs.add_parser("submit", help="upload and queue a real render")
    submit.add_argument("file")
    submit.add_argument("--score", default="")

    watch = subs.add_parser("watch", help="follow one job")
    watch.add_argument("job_id")

    args = parser.parse_args()
    try:
        return {"topology": cmd_topology, "ping": cmd_ping,
                "submit": cmd_submit, "watch": cmd_watch}[args.command](args)
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
