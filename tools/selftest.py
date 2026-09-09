"""Checks that must pass identically on Windows and on Linux.

    python tools/selftest.py

Nothing here needs ffmpeg, cairo, torch, a score package or a network. It is
the part of the app that can be checked anywhere, so it runs on both
platforms on every push and proves they still agree. The expensive proof —
that a real recording comes out as a real video — stays in
`tools/try_render.py` and `tools/try_identify.py`, on a machine with the
tools installed.

Why both platforms, when the app only ever runs on one at a time: the
development machine is Windows and the servers are Linux, and the
differences between them do not announce themselves. They surface as an
empty result rather than as an error.

  - `pathlib.Path` treats a backslash as an ordinary character on Linux, so
    a Windows path parsed there yields the whole path as its "stem".
  - `os.pathsep` is ';' on Windows and ':' on Linux, so one Windows
    directory read on Linux splits into two nonexistent ones.
  - The filesystem is case-sensitive on Linux and not on Windows.

Each of those has already cost real time here. The first one shipped: every
recognition on the Linux node reported consensus 1.00 and then "no
installed score to render it", because the lookup after the recognition
matched nothing and said so quietly. Every check below exists because
something worked on one platform and silently did nothing on the other.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import pathlib
import pkgutil
import re
import sys
import tempfile
import time
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Before importing anything from `app`: the settings module reads WORK_DIR at
# import time, and a check that writes jobs must not touch the developer's
# real queue. python-dotenv does not override variables already set, so this
# wins over a local .env.
_SANDBOX = tempfile.TemporaryDirectory(prefix="svs_selftest_")
os.environ["WORK_DIR"] = str(pathlib.Path(_SANDBOX.name) / "work")


class Failed(AssertionError):
    """A check did not hold. The message is meant to be read on a CI page."""


# --------------------------------------------------------------------------
# the app loads at all
# --------------------------------------------------------------------------
def check_every_module_imports() -> str:
    """An import error otherwise appears as a worker that will not start."""
    names = [f"app.{m.name}" for m in pkgutil.iter_modules([str(ROOT / "app")])]
    bad, missing = [], set()
    for name in names:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:
            missing.add(exc.name or "?")
            bad.append(f"  {name}: {type(exc).__name__}: {exc}")
        except Exception as exc:                      # noqa: BLE001
            bad.append(f"  {name}: {type(exc).__name__}: {exc}")
    if bad:
        note = ""
        # On Windows the workflow is split across conda environments and it
        # is easy to run this under the recognition one, which has torch but
        # no Flask. That is not a broken repository, only the wrong python.
        if missing & {"flask", "cairosvg", "PIL", "dotenv"}:
            note = (f"\n\n  {sorted(missing)} missing — this is probably the "
                    f"wrong interpreter.\n  The app runs under the "
                    f"VideoScoreSync environment on Windows, not the "
                    f"recognition one:\n    {sys.executable}\n  is what ran "
                    f"these checks.")
        raise Failed("modules that would not import:\n" + "\n".join(bad) + note)
    return f"{len(names)} modules"


def check_limits_are_sane() -> str:
    """A typo in .env.example would otherwise be a wrong limit in production."""
    from app import limits
    from app.settings import settings

    for name, rule in limits.RULES.items():
        if rule.limit <= 0:
            raise Failed(f"{name} allows {rule.limit} — nobody could ever act")
        if rule.window <= 0:
            raise Failed(f"{name} has a window of {rule.window} seconds")
    if settings.smtp_port <= 0:
        raise Failed(f"SMTP_PORT is {settings.smtp_port}")
    return f"{len(limits.RULES)} limit rules, all positive"


def check_job_store_round_trips() -> str:
    """A job goes in, is found by the sweep, and its stage record survives.

    SQLite is where the two platforms differ least, but not where they are
    identical: the sweep uses `UPDATE ... RETURNING`, which older builds do
    not have, and Windows and Linux ship different ones.
    """
    from app import store

    store.put_job({"id": "selftest", "created": time.time(), "name": "x.mp4",
                   "upload": "x.mp4", "state": store.QUEUED,
                   "queued_at": time.time()})
    if store.get_job("selftest")["state"] != store.QUEUED:
        raise Failed("a job did not come back queued")
    if store.position("selftest") != 0:
        raise Failed("the only job in the queue is not first in it")

    # A job nothing has confirmed the handover of. The sweep finds these,
    # and it uses UPDATE...RETURNING, which is not in older SQLite — the two
    # platforms ship different builds, so it is exercised rather than assumed.
    store.update_job("selftest", queued_at=time.time() - 3600)
    if "selftest" not in [r["id"] for r in store.unpublished(older_than=30)]:
        raise Failed("an unconfirmed job was not found by unpublished(); "
                     f"sqlite {__import__('sqlite3').sqlite_version}")
    store.mark_published("selftest")
    if [r["id"] for r in store.unpublished(older_than=30)] == ["selftest"]:
        raise Failed("a published job is still reported as unhandled")
    if store.forget_publications() < 1:
        raise Failed("forget_publications did not clear a queued job")

    run = store.stage_begin("selftest", "embed", media_seconds=10.0)
    store.stage_end(run, state=store.DONE, command="ffmpeg -i a b")
    rows = store.stage_runs("selftest")
    if not rows or rows[0]["command"] != "ffmpeg -i a b":
        raise Failed(f"stage rows did not round-trip: {rows}")
    return (f"queued, swept and staged "
            f"(sqlite {__import__('sqlite3').sqlite_version})")


def check_a_task_reaches_a_worker_and_comes_back() -> str:
    """The whole seam, with the work stubbed: publish, consume, report, apply.

    This is the path a broker will carry. Running it in-process on every push
    means the shapes, the ordering and the ledger's rules are exercised on
    both platforms without anybody installing a broker — and when the broker
    arrives, only `transport.py` is new.
    """
    from app import store
    from app.queue import ledger, transport, worker
    from app.queue.messages import Event, RenderTask

    bus = transport.LocalTransport()
    jid = "seam"
    _queued(jid, score="Some Score", mode="reference")

    # The work itself is not the subject here, so it is replaced by
    # something that reports the same way a real render does.
    def fake_render(task: RenderTask, publish) -> None:
        publish(Event(job_id=task.job_id, type="started", attempt=task.attempt,
                      seq=1, worker="stub"))
        publish(Event(job_id=task.job_id, type="progress", attempt=task.attempt,
                      seq=2, stage="encode", detail="1920x1080"))
        publish(Event(job_id=task.job_id, type="done", attempt=task.attempt,
                      seq=3, result="/tmp/seam.mp4", elapsed=1.0))

    bus.publish_task(RenderTask(job_id=jid, upload=f"/tmp/{jid}.mp4",
                                package="Some Score", mode="reference"))
    # Drain by hand rather than by thread, so the check cannot be flaky.
    body = bus._tasks.get_nowait()
    fake_render(RenderTask.from_json(body), bus.publish_event)

    applied = 0
    while not bus._events.empty():
        ledger.apply(Event.from_json(bus._events.get_nowait()))
        applied += 1

    row = store.get_job(jid)
    if row["state"] != store.DONE or row["result"] != "/tmp/seam.mp4":
        raise Failed(f"the seam did not finish the job: {row['state']!r}")
    if applied != 3:
        raise Failed(f"{applied} events applied, expected 3")

    # A ping is answered without touching ffmpeg, which is what makes a live
    # broker checkable in seconds rather than in half an hour.
    answers = []
    worker.handle_task(RenderTask(job_id="p", upload="", package="",
                                  kind="ping"), answers.append)
    if [e.type for e in answers] != ["pong"]:
        raise Failed(f"a ping was not answered with a pong: {answers}")

    if transport.LocalTransport.survives_restart:
        raise Failed("the in-process transport claims to survive a restart")
    return "published, consumed, reported, applied; ping answered"


def check_old_databases_gain_the_new_columns() -> str:
    """`CREATE TABLE IF NOT EXISTS` does nothing to a table that exists.

    So a database written before a column was added never gets it, and the
    first write naming that column fails with "no such column" — on the
    server, against the only copy of the queue that matters.
    """
    import sqlite3
    from app import store

    with tempfile.TemporaryDirectory(prefix="svs_migrate_") as tmp:
        path = pathlib.Path(tmp) / "old.sqlite"
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        # A jobs table from before any of this existed.
        conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, state TEXT)")
        conn.commit()

        store._migrate(conn)
        have = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
        missing = sorted({c for _, c, _ in store._ADDED} - have)

        # Running it again must be silent, because it runs on every connect.
        store._migrate(conn)
        conn.close()

    if missing:
        raise Failed(f"_migrate did not add: {missing}")
    return f"{len(store._ADDED)} columns added to an old table, twice safely"


# --------------------------------------------------------------------------
# the worker's report becomes the row, and only once
# --------------------------------------------------------------------------
def _queued(job_id: str, **over) -> None:
    """Put one job in the table, queued, ready to be reported on."""
    from app import store
    row = {"id": job_id, "created": time.time(), "name": f"{job_id}.mp4",
           "upload": f"/tmp/{job_id}.mp4", "state": store.QUEUED,
           "queued_at": time.time(), "email": "", "attempt": 1,
           "duration": 120.0, "size_bytes": 1024}
    row.update(over)
    store.put_job(row)


def check_ledger_applies_a_run_in_order() -> str:
    """started -> progress -> done leaves the row and the record right."""
    from app import store
    from app.queue import ledger
    from app.queue.messages import Event

    jid = "ledger_order"
    _queued(jid)
    ledger.apply(Event(job_id=jid, type="started", worker="w1"))
    row = store.get_job(jid)
    if row["state"] != store.RUNNING or row["worker"] != "w1":
        raise Failed(f"'started' left the row {row['state']!r}/{row['worker']!r}")

    ledger.apply(Event(job_id=jid, type="progress", stage="bands",
                       detail="10/83", worker="w1"))
    row = store.get_job(jid)
    if (row["stage"], row["detail"]) != ("bands", "10/83"):
        raise Failed(f"progress did not land: {row['stage']!r} {row['detail']!r}")

    # `probe` is real work but not a milestone: it moves the detail line
    # without advancing the stage.
    ledger.apply(Event(job_id=jid, type="progress", stage="probe",
                       detail="looking at the file", worker="w1"))
    if store.get_job(jid)["stage"] != "bands":
        raise Failed("a non-milestone stage moved the stage")

    ledger.apply(Event(job_id=jid, type="done", result="/tmp/out.mp4",
                       mode="reference", elapsed=12.5, worker="w1"))
    row = store.get_job(jid)
    if row["state"] != store.DONE or row["result"] != "/tmp/out.mp4":
        raise Failed(f"'done' left the row {row['state']!r}")
    if row["worker"] is not None or row["lease_until"] is not None:
        raise Failed("a finished job still holds a worker or a lease")

    runs = [r for r in store.stage_runs(jid) if r["stage"] == "render"]
    if len(runs) != 1 or runs[0]["ended"] is None or runs[0]["elapsed"] is None:
        raise Failed(f"the stage record is not one closed row: {runs}")
    return "running, staged, finished, one closed record"


def check_ledger_is_idempotent() -> str:
    """Every rule here is a message arriving twice, late, or out of turn.

    A broker redelivers on any doubt, and a lease can be reclaimed a moment
    before a slow worker's next word lands. The expensive one is `done`
    twice: the second would send a second email about the same video.
    """
    from app import stats, store
    from app.queue import ledger
    from app.queue.messages import Event

    jid = "ledger_twice"
    _queued(jid)
    ledger.apply(Event(job_id=jid, type="started", worker="w1"))

    before = stats.videos()
    ledger.apply(Event(job_id=jid, type="done", result="/tmp/a.mp4"))
    if ledger.apply(Event(job_id=jid, type="done", result="/tmp/a.mp4")):
        raise Failed("a redelivered 'done' changed the row a second time")
    if stats.videos() - before != 1:
        raise Failed(f"one render counted {stats.videos() - before} times")

    # A late word from a superseded attempt must not drag the row back.
    if ledger.apply(Event(job_id=jid, type="progress", attempt=0,
                          stage="bands", detail="stale")):
        raise Failed("an older attempt's progress was applied")

    # A reclaim that fired while the worker was merely slow.
    jid2 = "ledger_reclaim"
    _queued(jid2)
    ledger.apply(Event(job_id=jid2, type="started", worker="w1"))
    store.update_job(jid2, state=store.QUEUED, worker=None)
    ledger.apply(Event(job_id=jid2, type="heartbeat", worker="w1"))
    if store.get_job(jid2)["state"] != store.RUNNING:
        raise Failed("a heartbeat did not undo a premature reclaim")

    # A crash and a redelivery: the abandoned record must be closed, not
    # left looking like it is still running.
    ledger.apply(Event(job_id=jid2, type="started", worker="w2"))
    runs = [r for r in store.stage_runs(jid2) if r["stage"] == "render"]
    closed = [r for r in runs if r["error_class"] == "Interrupted"]
    if len(runs) != 2 or len(closed) != 1:
        raise Failed(f"the interrupted run was not closed: "
                     f"{[(r['state'], r['error_class']) for r in runs]}")
    return "one mail, one count, stale dropped, reclaim undone, run closed"


def check_stages_are_derived_from_the_row() -> str:
    """The picture both front-ends draw must survive a process boundary.

    It was a dict on the in-memory Job, so a status poll — which rebuilds
    the Job from the table — saw all-pending for the whole render. The
    progress indicator sat at zero and then jumped to complete.
    """
    from app import jobs, pipeline, store

    jid = "stages"
    _queued(jid)
    store.update_job(jid, state=store.RUNNING, stage="strip")
    stages = jobs.Job.from_row(store.get_job(jid)).stages
    order = list(pipeline.STAGES)
    at = order.index("strip")
    wrong = [s for s in order[:at] if stages[s] != "done"]
    if wrong or stages["strip"] != "active":
        raise Failed(f"mid-render picture is wrong: {stages}")
    if any(stages[s] != "pending" for s in order[at + 1:]):
        raise Failed(f"stages after the current one are not pending: {stages}")

    store.update_job(jid, state=store.ERROR)
    if jobs.Job.from_row(store.get_job(jid)).stages["strip"] != "failed":
        raise Failed("a failed job does not mark the stage it failed in")

    store.update_job(jid, state=store.DONE)
    if set(jobs.Job.from_row(store.get_job(jid)).stages.values()) != {"done"}:
        raise Failed("a finished job does not show every stage done")
    return f"{len(order)} stages, before/at/after, plus failed and finished"


def check_messages_round_trip() -> str:
    """Both shapes survive JSON, and a message from another build is refused."""
    from app.queue import messages

    task = messages.RenderTask(
        job_id="abc", upload=r"C:\work\abc.mp4", package="Op.39_Scherzo",
        attempt=2, mode="reference", duration=424.3,
        style={"aspect": "16/9", "crf": 20}, meta={"performer": "Zoé"})
    if messages.RenderTask.from_json(task.to_json()) != task:
        raise Failed("a RenderTask did not survive the round trip")

    event = messages.Event(job_id="abc", type="failed", attempt=2, seq=9,
                           error="ffmpeg said no", error_class="ToolFailed",
                           returncode=-9, stderr_tail="…")
    if messages.Event.from_json(event.to_json()) != event:
        raise Failed("an Event did not survive the round trip")

    # A field this build has never heard of is ignored, so the two sides
    # need not be deployed in the same instant.
    grown = task.to_json().replace('"kind": "render"',
                                   '"kind": "render", "invented": 1')
    if messages.RenderTask.from_json(grown) != task:
        raise Failed("an unknown field was not ignored")

    # A different version is refused rather than guessed at.
    future = task.to_json().replace(f'"v": {messages.VERSION}', '"v": 99')
    try:
        messages.RenderTask.from_json(future)
    except messages.UnknownVersion:
        pass
    else:
        raise Failed("a message from version 99 was accepted")
    return f"both shapes, v{messages.VERSION}, unknown fields ignored"


# --------------------------------------------------------------------------
# recognition resolves to a score, whatever wrote the pair list
# --------------------------------------------------------------------------
# Two editions of one piece share a recording, which is the normal case and
# the reason a piece id maps to a *list*. The Windows path is what the file
# really contains today; the POSIX one is what it would contain if it were
# ever rewritten portably, and both must work on both platforms.
_BREITKOPF = "Op.39_3ème Scherzo pour le Piano_(Breitkopf)__039-1-BH"
_TROUPENAS = "Op.39_Scherzo_(Troupenas)__039-1-TR"
_WINDOWS_VIDEO = (r"C:\ZZ_perso\weefeen\Chopin_Companion"
                  r"\Videos_downloaded_youtube\work_op_39__scherzo_dyZmzXMHItI.mp4")
_POSIX_VIDEO = "/srv/vsw/videos/work_op_25_no_11_aeolian_harp_QK1yPPjLnGQ.mp4"

_FIXTURE = {
    _BREITKOPF: {"video": _WINDOWS_VIDEO, "level1": "ok"},
    _TROUPENAS: {"video": _WINDOWS_VIDEO, "level1": "ok"},
    "Op.25_Etude No. 11_(Breitkopf)__025-11-BH": {"video": _POSIX_VIDEO,
                                                  "level1": "ok"},
    "Op.42_Valse_(Pacini)__042-1-P": {
        "video": r"C:\videos\work_op_42__valse_D92xATclLHs.mp4",
        "level1": "wrong"},
}


def _runner():
    """Load tools/identify_runner.py without running it."""
    spec = importlib.util.spec_from_file_location(
        "identify_runner", ROOT / "tools" / "identify_runner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_piece_ids_resolve() -> str:
    """The bug that shipped: a Windows path in the pair list, read on Linux.

    `pathlib.Path(video).stem` is a PosixPath there, and a backslash is an
    ordinary character to it, so the "stem" was the entire absolute path. It
    matched no piece id, `score_names` came back empty for everything, and
    the app said "recognised, but no installed score to render it" — with
    full confidence — for all 186 pieces in the corpus.
    """
    runner = _runner()
    with tempfile.TemporaryDirectory(prefix="svs_pairs_") as tmp:
        path = pathlib.Path(tmp) / "pair_list.json"
        path.write_text(json.dumps({"pairs": _FIXTURE}, ensure_ascii=False),
                        encoding="utf-8")
        names = runner._labels(str(path))

        got = names("work_op_39__scherzo_dyZmzXMHItI")
        if got != sorted([_BREITKOPF, _TROUPENAS]):
            raise Failed(
                "a Windows path in the pair list did not resolve to its "
                f"editions on {sys.platform}.\n  expected both editions\n"
                f"  got      {got}")

        if names("work_op_25_no_11_aeolian_harp_QK1yPPjLnGQ") != [
                "Op.25_Etude No. 11_(Breitkopf)__025-11-BH"]:
            raise Failed("a POSIX path in the pair list did not resolve")

        # level1 "wrong" means the recording was never in the indexes, so
        # offering its score would name a piece nothing can be matched to.
        if names("work_op_42__valse_D92xATclLHs"):
            raise Failed("an unvalidated row was offered as a score")

        if names("no_such_recording") or names(""):
            raise Failed("an unknown piece id produced a score name")
    return "3 validated rows, 2 path flavours, 1 rejected"


def check_pair_list_is_read_without_the_dependency() -> str:
    """`weefeen_id.labels` parses the same file and is not portable.

    Kept as a check rather than a comment because the tempting cleanup — "we
    already have a class for this, import it" — reintroduces the bug, and on
    Windows nothing would show it.

    Narrow on purpose. Whether the parsing is *correct* is settled by
    `check_piece_ids_resolve` actually resolving one; this only guards the
    one edit that would quietly hand the job back to the broken parser.
    """
    text = (ROOT / "tools" / "identify_runner.py").read_text(encoding="utf-8")
    # An import, not a mention: the docstrings there name the class on
    # purpose, to say why it is not used.
    imported = re.search(r"^\s*(from\s+weefeen_id\S*\s+import\s+.*ScoreLabels"
                         r"|import\s+.*ScoreLabels)", text, re.MULTILINE)
    if imported:
        raise Failed(f"identify_runner imports ScoreLabels again "
                     f"({imported.group(0).strip()}); it builds its lookup "
                     f"with pathlib.Path, which is wrong on Linux")
    return "no import of the unportable parser"


# --------------------------------------------------------------------------
# configuration says the same thing on both platforms
# --------------------------------------------------------------------------
_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def _declared(path: pathlib.Path) -> dict[str, str]:
    """Variable -> value for one env file, ignoring comments and blanks."""
    found = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _ASSIGNMENT.match(line.strip())
        if match:
            found[match.group(1)] = line.split("=", 1)[1].strip()
    return found


def check_linux_configuration_leaves_no_gaps() -> str:
    """Every variable .env.example sets must also be set in .env.prod.

    `app/settings.py` reads `.env` and then falls back to `.env.example`,
    which carries Windows paths. A gap is not a quiet default on Linux: it
    is `C:\\VideoScoreSync\\scores` arriving as two score roots, one named
    `C` and one named `\\VideoScoreSync\\scores`, because os.pathsep is ':'
    there. That happened on the first deployment.
    """
    example = _declared(ROOT / ".env.example")
    prod = _declared(ROOT / ".env.prod")
    missing = sorted(set(example) - set(prod))
    if missing:
        raise Failed(
            ".env.prod does not set, so Linux falls back to the Windows "
            "value in .env.example:\n  " + "\n  ".join(missing))
    return f"{len(prod)} variables, covering all {len(example)} in .env.example"


def check_the_queue_topology_is_stable() -> str:
    """The queue arguments must not change by accident.

    RabbitMQ refuses to redeclare an existing queue with different arguments
    (`PRECONDITION_FAILED`) and will not change them in place. So altering
    any value below means deleting that queue on every broker it exists on,
    losing whatever it holds — which is a deployment step somebody has to
    plan, not a line to edit and push. This check makes that visible at the
    moment of the edit rather than on the next deploy.

    `x-consumer-timeout` is the load-bearing one. RabbitMQ's default is 30
    minutes and our worst case is about 32, so without it the longest
    uploads — and only those — would have their delivery pulled back
    mid-encode, every single time.
    """
    from app.queue import transport

    expected = {
        "vsw.render": {"x-dead-letter-exchange": "vsw.dlx",
                       "x-consumer-timeout": 10_800_000,
                       "x-max-priority": 10},
        "vsw.events": {"x-dead-letter-exchange": "vsw.dlx"},
        "vsw.render.dead": {},
        "vsw.events.dead": {},
    }
    if transport.TOPOLOGY != expected:
        raise Failed(
            "the queue topology changed. Every broker that already has these "
            "queues must have them DELETED before this can be deployed.\n"
            f"  now      {transport.TOPOLOGY}\n"
            f"  expected {expected}\n"
            "  If the change is intended, update this check in the same "
            "commit and say so in the message.")
    return f"{len(expected)} queues, arguments unchanged"


def check_the_readme_layout_is_real() -> str:
    """Every path in the README's layout block must exist.

    A layout that lists a file somebody deleted is worse than no layout: it
    is read once, believed, and then quietly wastes the next person's
    afternoon. This is the cheapest way to keep the map and the ground the
    same shape, and it costs nothing to run.
    """
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    try:
        block = text.split("```", 2)[1]
    except IndexError:
        raise Failed("the README has no layout block any more") from None

    found, stack = [], []
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        token = line.strip().split()[0]
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = (stack[-1][1] if stack else "") + token
        if token.endswith("/"):
            stack.append((indent, path))
        found.append(path)

    missing = [p for p in found if not (ROOT / p).exists()]
    if missing:
        raise Failed("the README's layout names things that do not exist:\n  "
                     + "\n  ".join(missing))
    if len(found) < 10:
        raise Failed(f"only {len(found)} paths found in the layout block; "
                     f"the parser or the block has changed shape")
    return f"{len(found)} paths, all present"


def check_linux_configuration_has_no_windows_paths() -> str:
    """A drive letter or a backslash in .env.prod is a copied-over mistake."""
    bad = []
    for name, value in _declared(ROOT / ".env.prod").items():
        if _DRIVE.match(value) or "\\" in value:
            bad.append(f"  {name}={value}")
    if bad:
        raise Failed("Windows paths in the Linux configuration:\n" + "\n".join(bad))
    return "no drive letters, no backslashes"


# --------------------------------------------------------------------------
def main() -> int:
    checks = [
        check_every_module_imports,
        check_limits_are_sane,
        check_job_store_round_trips,
        check_a_task_reaches_a_worker_and_comes_back,
        check_the_queue_topology_is_stable,
        check_old_databases_gain_the_new_columns,
        check_messages_round_trip,
        check_ledger_applies_a_run_in_order,
        check_ledger_is_idempotent,
        check_stages_are_derived_from_the_row,
        check_piece_ids_resolve,
        check_pair_list_is_read_without_the_dependency,
        check_linux_configuration_leaves_no_gaps,
        check_linux_configuration_has_no_windows_paths,
        check_the_readme_layout_is_real,
    ]
    print(f"  {sys.platform}  python {sys.version.split()[0]}  "
          f"os.pathsep {os.pathsep!r}\n")

    failures = 0
    for check in checks:
        label = check.__name__.removeprefix("check_").replace("_", " ")
        try:
            detail = check()
        except Failed as exc:
            failures += 1
            print(f"  FAIL  {label}\n        {exc}\n")
        except Exception as exc:                      # noqa: BLE001
            failures += 1
            print(f"  ERROR {label}: {type(exc).__name__}: {exc}")
            print(traceback.format_exc())
        else:
            print(f"  ok    {label:52} {detail}")

    # Windows will not delete the open SQLite file, and TemporaryDirectory
    # cleans up at exit. Linux would not have minded either way.
    try:
        from app import store
        store.close()
    except Exception:                                 # noqa: BLE001
        pass

    print()
    if failures:
        print(f"  {failures} of {len(checks)} checks failed")
        return 1
    print(f"  all {len(checks)} checks passed on {sys.platform}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
