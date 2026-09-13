"""Lend this machine to the queue, so a paid one is not created.

    python tools/volunteer.py            offer this machine
    python tools/volunteer.py --check    just say whether it could
    python tools/volunteer.py --no-panel don't open the page

A PAGE OPENS. Everything below used to be a keystroke in a scrolling
terminal and a setting in a file on another machine, neither visible from
the other. The page at 127.0.0.1:5055 says in words whether this machine
is taking jobs, what it is rendering and how far in, what length it would
accept right now, and what happens to a job when this machine is not
listening -- which is the one control that lives on the web box. Every
change is explicit; nothing on it decides anything by itself.

WHY. A render costs about 29 cents on a rented machine, because Linode
rounds a partial hour up and a video takes ten minutes. This desktop is
already paid for and, at 64 GB and twenty cores, is bigger than the plan it
would replace. While it is consuming, the scaler creates nothing.

HOW THE SCALER KNOWS. This publishes an `alive` event on the queue the
worker already reports on. The web box records it, and `store.compute_tick`
declines to want a machine while that word is fresh -- three missed beats,
so a closed laptop is noticed in about three minutes and the queue moves on
to a paid node rather than waiting for someone to come back.

AND IT WARMS THE LIBRARY WHILE IT WAITS. A score this machine has never
rendered is a 30-110 MB download from the bucket, and on a home connection
that is minutes a visitor spends watching a progress bar. So an idle
volunteer quietly fetches the packages it does not have, one at a time,
pausing the moment a real job arrives. Left running, it ends up holding the
whole library and is instantly ready; stopped early, it has still saved
whatever it managed.

CONTROLS. `p` stops it taking anything new, `r` resumes, `q` stops after
the job in hand. Ctrl-C during a render hands that job straight back to
the queue, so a rented machine picks it up in seconds rather than after
its lease expires. While it is paused, and for a few minutes after it
declines a job, it stops saying `alive`, so a paid machine is rented for
the work it will not take instead of the visitor waiting on a laptop that
has already said no.

AND IT REFUSES WHAT IT CANNOT FINISH. Total RAM is the wrong number: this
desktop has 64 GB and perhaps 15 free while it is being used, and the
alignment's matrix grows with the SQUARE of the recording. A job needing
more than is free goes back to the queue for a rented machine instead of
dying two thirds of the way through somebody's video.
"""
from __future__ import annotations

import argparse
import logging
import os
import pathlib
import socket
import sys
import threading
import time
import webbrowser

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import render as rnd                   # noqa: E402
from app import scorestore, storage, store      # noqa: E402
from app import svg as appsvg                   # noqa: E402
from app.queue import webside, worker           # noqa: E402
from app.queue.messages import Event            # noqa: E402
from app.queue.transport import quiet_pika, transport  # noqa: E402
from app.settings import settings               # noqa: E402

import lend_panel                                  # noqa: E402

logger = logging.getLogger("volunteer")

# Faster than the worker's own job heartbeat: this one decides whether a
# machine gets created, so being slow to speak costs real money, and being
# slow to go quiet costs a visitor their wait.
BEAT_SECONDS = 45.0
# How often a silent volunteer looks again at whether it may speak.
QUIET_POLL = 5.0

# Set while a render is in flight, so the library warmer gets out of the way
# -- the visitor's own upload is coming down the same connection.
_rendering = threading.Event()

# What this machine is doing, for the page to show. Held here rather than
# asked of the web box: everything a render reports goes to the BROKER, so
# without this the only way for this machine to learn what it was itself
# doing was to ask the server about itself.
_job_lock = threading.Lock()
_job: dict | None = None

# How many of the published scores are already on this disk. Recomputed by
# the warmer rather than per request: it is a directory walk per package.
_scores = {"here": 0, "total": 0}


def _job_started(task) -> None:
    with _job_lock:
        global _job
        _job = {"id": task.job_id, "piece": task.package or "a score",
                "minutes": round((task.duration or 0) / 60.0, 1) or None,
                "stage": "prepare", "detail": "", "began": time.time()}


def _job_event(event) -> None:
    """Every event this machine publishes, on its way to the broker."""
    with _job_lock:
        if _job is None or getattr(event, "job_id", "") != _job["id"]:
            return
        kind = getattr(event, "type", "")
        if kind in ("done", "failed"):
            _job["stage"] = "done" if kind == "done" else _job["stage"]
            _job["detail"] = ("" if kind == "done"
                              else getattr(event, "error", "") or "It stopped.")
            return
        if getattr(event, "stage", None):
            _job["stage"] = event.stage
        if getattr(event, "detail", ""):
            _job["detail"] = event.detail


def _job_finished() -> None:
    with _job_lock:
        global _job
        _job = None


def _state() -> dict:
    """Everything the page shows, measured now."""
    quiet_for = max(0.0, _quiet_until - time.time())
    with _job_lock:
        job = dict(_job) if _job else None
    if job:
        job["running_for"] = round(time.time() - job.pop("began"), 1)
    free = free_memory_bytes()
    return {
        "taking": not _paused.is_set() and quiet_for <= 0,
        "paused": _paused.is_set(),
        "quiet_for": round(quiet_for, 1),
        "job": job,
        "free_gb": (free / 1024 ** 3) if free else None,
        "longest_min": _longest_now() if free else None,
        "scores_here": _scores["here"],
        "scores_total": _scores["total"],
        # WHAT RENTING ONE COSTS, from the plan itself. The page says
        # "~$0.29 a video" beside the choice that spends it, and a figure
        # typed into a page is a figure that goes stale the day the plan
        # changes -- which is the whole reason PLAN_HOURLY_USD exists.
        # A partial hour is rounded up by the provider, so one video on an
        # idle machine costs one hour whatever the render took.
        "hourly_cost": settings.compute_hourly_cost,
    }


def _stop_after() -> None:
    """Finish the job in hand, then take nothing more."""
    _paused.set()


def _preflight() -> list[str]:
    """Everything that must be true before this machine is any use."""
    problems: list[str] = []

    if not settings.rabbitmq_url:
        problems.append(
            "RABBITMQ_URL is not set. Open the tunnel and point at it:\n"
            "    ssh -N -L 5672:127.0.0.1:5672 root@chopin.weefeen.com\n"
            "    RABBITMQ_URL=amqp://vsw-compute:<password>@127.0.0.1:5672/vsw")
    else:
        host, port = "127.0.0.1", 5672
        try:
            rest = settings.rabbitmq_url.split("@", 1)[-1]
            host = rest.split("/", 1)[0].split(":")[0] or host
            port = int(rest.split("/", 1)[0].split(":")[1]) if ":" in rest.split("/", 1)[0] else port
        except (ValueError, IndexError):
            pass
        try:
            with socket.create_connection((host, port), timeout=5):
                pass
        except OSError as exc:
            problems.append(
                f"nothing is listening on {host}:{port} ({exc}). The broker "
                f"is not on the public internet by design; open the tunnel:\n"
                f"    ssh -N -L 5672:127.0.0.1:5672 root@chopin.weefeen.com")

    if not storage.available():
        problems.append(f"the bucket is unreachable: {storage.status()}")
    if not appsvg.available():
        problems.append(f"cairosvg will not load: {appsvg.why_unavailable()}")
    if not pathlib.Path(settings.ffmpeg).is_file():
        problems.append(f"no ffmpeg at {settings.ffmpeg}")
    if not settings.can_sync:
        problems.append(f"alignment is not configured: {settings.why_cannot_sync()}")
    return problems


def _beat(bus, stop: threading.Event) -> None:
    """Say we are here, until told to stop -- and only while it is true.

    SILENT WHILE UNWILLING. Paused, or having just handed a job back, this
    machine is not going to take the next thing the broker offers; saying
    `alive` then would keep the scaler from renting and leave a visitor
    waiting on a laptop that has already said no. So the word stops, the
    scaler notices within `VOLUNTEER_WINDOW`, and a paid machine takes the
    work. It resumes the moment this machine is willing again.
    """
    while not stop.is_set():
        if _paused.is_set() or time.time() < _quiet_until:
            stop.wait(QUIET_POLL)
            continue
        try:
            bus.publish_event(Event(job_id="", type="alive",
                                    worker=worker.name()))
        except Exception:                              # noqa: BLE001
            logger.warning("could not announce this machine; the scaler may "
                           "create a paid one", exc_info=True)
        stop.wait(BEAT_SECONDS)


def _warm(stop: threading.Event) -> None:
    """Fetch score packages this machine does not have, while it is idle."""
    root = scorestore.local_root()
    if root is None:
        logger.warning("no score root on this machine; nothing to warm into")
        return
    while not stop.is_set():
        # Never compete with a render for the connection the visitor's own
        # upload is arriving on.
        if _rendering.is_set():
            stop.wait(20)
            continue
        try:
            missing = [n for n in scorestore.catalogue()
                       if not (root / n / "score").is_dir()]
        except Exception:                              # noqa: BLE001
            stop.wait(300)
            continue
        if not missing:
            stop.wait(600)        # the whole library is here; check rarely
            continue
        name = missing[0]
        logger.info("warming the library: %s (%d still missing)",
                    name, len(missing))
        try:
            scorestore.fetch(name, root)
        except Exception:                              # noqa: BLE001
            logger.warning("could not warm %s", name, exc_info=True)
            stop.wait(60)
        stop.wait(5)



# Set when the operator says "take nothing more". The job in hand finishes;
# anything the broker offers after that goes straight back to the queue, so
# a rented machine picks it up in seconds instead of the visitor waiting out
# a lease on a laptop that is about to be shut.
_paused = threading.Event()

# Until when this machine keeps quiet after handing a job back. A decline
# means the job at the head of the queue is one this machine will not take,
# and with one message delivered at a time nothing behind it can be reached
# either -- so the only way forward is a rented machine, and one is only
# rented while no volunteer is heard. Two windows: one for the scaler to
# notice the silence, one for the node to boot and take the job.
_quiet_until = 0.0


def _go_quiet() -> None:
    global _quiet_until
    _quiet_until = time.time() + 2 * store.VOLUNTEER_WINDOW


def free_memory_bytes() -> int:
    """Memory actually available now, without adding a dependency for it.

    TOTAL RAM IS THE WRONG NUMBER. This desktop has 64 GB and perhaps 15 GB
    free while it is being used for anything else, and the alignment
    allocates a full DTW matrix whose size grows with the SQUARE of the
    recording. Accepting a twenty-minute upload on the strength of the
    sticker figure is how a render dies two thirds of the way through.
    """
    if sys.platform == "win32":
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = Status()
        st.dwLength = ctypes.sizeof(Status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return int(st.ullAvailPhys)
        return 0
    try:
        for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def _needed_bytes(minutes: float) -> float:
    """What aligning a recording of this length will want.

    The same curve the site sizes its upload limit with, read backwards:
    7.1 minutes measured at 2.43 GB, growing with the square.
    """
    from app.settings import DTW_GB_AT, DTW_MINUTES_AT

    if minutes <= 0:
        return 0.0
    return (minutes / DTW_MINUTES_AT) ** 2 * DTW_GB_AT * 1024 ** 3


def _accept(task) -> bool:
    """Whether this machine should take this particular job.

    Every refusal comes with a silence (`_go_quiet`): a job declined once is
    offered again every two seconds until somebody else takes it, and nobody
    else is rented while this machine keeps saying it is here.
    """
    if _paused.is_set():
        logger.info("paused; job %s goes back to the queue", task.job_id)
        return False

    # THE RECORDING HAS TO BE REACHABLE. A task carries the web box's own
    # path and, when it was staged, the bucket key; this machine can see the
    # bucket and nothing else. Without the key the job belongs to a worker
    # on that box, and taking it here would fail at the first byte.
    if not task.input_key and not pathlib.Path(task.upload or "").is_file():
        logger.warning("declining job %s: its recording is not in the bucket "
                       "and %s is not on this disk. A machine that can see "
                       "it will take it.", task.job_id, task.upload)
        _go_quiet()
        return False

    minutes = (task.duration or 0) / 60.0
    need = _needed_bytes(minutes)
    free = free_memory_bytes()
    # A margin, because the encode and the rest of the process want memory
    # too and an OOM here costs the whole render, not a retry.
    if need and free and need * 1.3 > free:
        logger.warning(
            "declining job %s: %.0f min needs about %.1f GB and only %.1f GB "
            "is free. A rented machine will take it.",
            task.job_id, minutes, need / 1e9, free / 1e9)
        _go_quiet()
        return False
    return True


def _keys(stop: threading.Event) -> None:
    """p pause, r resume, q stop after the current job. Windows only.

    Elsewhere the same is done with Ctrl-C, which now hands a job in flight
    back to the queue rather than leaving it to a lease.
    """
    if sys.platform != "win32":
        return
    import msvcrt

    while not stop.is_set():
        if not msvcrt.kbhit():
            stop.wait(0.2)
            continue
        key = msvcrt.getch().decode("ascii", "ignore").lower()
        if key == "p":
            _paused.set()
            print("\n  PAUSED. The current job finishes; nothing new is "
                  "taken. `r` to resume.")
        elif key == "r":
            _paused.clear()
            print("\n  resumed: taking work again.")
        elif key == "q":
            _paused.set()
            print("\n  stopping after the current job. Ctrl-C to hand it "
                  "back now instead.")



def _longest_now() -> float:
    """The longest recording this machine could align with what is free."""
    import math

    from app.settings import DTW_GB_AT, DTW_MINUTES_AT

    free_gb = free_memory_bytes() / 1024 ** 3 / 1.3
    if free_gb <= 0:
        return 0.0
    return DTW_MINUTES_AT * math.sqrt(free_gb / DTW_GB_AT)


def _watch_mode(panel, stop: threading.Event) -> None:
    """Keep the web box's setting fresh, off the request path.

    Read on a timer rather than when the page asks: it is an ssh round trip
    to another machine, and a page that waits on one feels broken.

    THE FIRST READ IS ALSO ADOPTED. Starting up meant "this machine takes
    jobs", whatever the server had been told -- so a server set to rent a
    machine for every video got a laptop competing with it for the same
    queue, two renderers for one job, and a page showing a pair of settings
    none of its own choices described. The server was told what to do last
    and it is the half that spends money, so it wins: if it is renting for
    everything, this machine stands back until somebody says otherwise.
    """
    first = True
    while not stop.is_set():
        panel.refresh_mode()
        if first:
            first = False
            if panel.mode().get("name") == "cloud" and not _paused.is_set():
                _paused.set()
                logger.info("the server rents a machine for every video, so "
                            "this one stands back. Change it on the panel.")
        stop.wait(30.0)


def _handle(task, ack) -> None:
    """The worker's own handler, with the warmer held off around it."""
    _rendering.set()
    _job_started(task)
    try:
        webside._handle(task, ack, observe=_job_event)  # noqa: SLF001
    finally:
        _rendering.clear()
        _job_finished()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="report readiness and exit")
    parser.add_argument("--no-panel", action="store_true",
                        help="do not open the page in a browser")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    # Pika narrates six lines per connection at INFO, and this opens a
    # short-lived one for every heartbeat -- so the handful of lines that
    # say what this machine is actually DOING scrolled past between walls
    # of socket bookkeeping. `webside.start` and `worker.main` both quieten
    # it; this has its own main and was the one place that did not. Its
    # warnings still come through, which is the half worth reading.
    quiet_pika()

    print()
    problems = _preflight()
    if problems:
        print("  this machine cannot take work yet:\n")
        for p in problems:
            print("   * " + p.replace("\n", "\n     "))
        print()
        return 1

    root = scorestore.local_root()
    try:
        published = scorestore.catalogue()
        here = [n for n in published if root and (root / n / "score").is_dir()]
    except Exception:                                  # noqa: BLE001
        published, here = [], []
    _scores["here"], _scores["total"] = len(here), len(published)
    print(f"  ready: {worker.name()}")
    print(f"    broker   {settings.rabbitmq_url.split('@')[-1]}")
    print(f"    scores   {len(here)} of {len(published)} already here"
          f"{'' if len(here) == len(published) else ' (the rest warm in the background)'}")
    print(f"    saying   'alive' every {BEAT_SECONDS:.0f}s, so no paid machine "
          f"is created while this runs")
    print()
    if args.check:
        return 0

    bus = transport()
    stop = threading.Event()
    threading.Thread(target=_beat, args=(bus, stop), name="beat",
                     daemon=True).start()
    threading.Thread(target=_warm, args=(stop,), name="warm",
                     daemon=True).start()
    threading.Thread(target=_keys, args=(stop,), name="keys",
                     daemon=True).start()

    print(f"  free memory now: {free_memory_bytes() / 1e9:.1f} GB "
          f"-- longest recording this could align right now: "
          f"{_longest_now():.0f} min")
    print()
    # THE PAGE. The keystrokes still work -- they cost nothing and a
    # terminal is where this is started -- but nothing requires you to
    # remember them, or to remember that the other half of the decision
    # lives in a file on the web box.
    panel = lend_panel.Panel(_state, _paused.set, _paused.clear, _stop_after)
    threading.Thread(target=_watch_mode, args=(panel, stop), name="mode",
                     daemon=True).start()
    url = lend_panel.serve(panel)
    if url:
        print(f"  the panel:  {url}")
        if not args.no_panel:
            try:
                webbrowser.open(url)
            except Exception:                          # noqa: BLE001
                pass
    print()
    print("  consuming.   p pause    r resume    q stop after this job")
    print("               Ctrl-C hands the current job straight back\n")
    try:
        bus.consume_tasks(_handle, accept=_accept)
    except KeyboardInterrupt:
        # The transport handed the job back before this was raised. What is
        # left is the tool it was running: the render sits on a daemon
        # thread and ffmpeg is a child of this process, and on Windows a
        # child outlives a parent that merely exits. Kill it, and leave
        # without giving the render thread a chance to report a `failed`
        # that would contradict the hand-back.
        killed = rnd.abort_children()
        print(f"\n  stopping: the job went back to the queue"
              f"{f', {killed} tool(s) stopped' if killed else ''}.")
        stop.set()
        os._exit(0)
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
