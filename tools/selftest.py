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
import shutil
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

    # `problems()` is what the release check runs before a deploy swaps the
    # symlink, and until now nothing called it any earlier. A line added to
    # it referring to an attribute that does not exist raised AttributeError
    # on the server, mid-deploy, with every local check green — which is the
    # long way round to find a typo.
    try:
        found = settings.problems()
    except Exception as exc:                          # noqa: BLE001
        raise Failed(f"settings.problems() raised {type(exc).__name__}: {exc}"
                     ) from exc
    if not isinstance(found, list):
        raise Failed(f"problems() returned {type(found).__name__}, not a list")
    return (f"{len(limits.RULES)} limit rules, and problems() runs "
            f"({len(found)} reported here)")


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
        # `jobs` as it was before any of this; `stage_runs` absent
        # entirely, which is the other half of what _migrate has to cope
        # with — an empty table_info means "no such table", not "no
        # columns", and it must not try to ALTER one that is not there.
        conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, state TEXT)")
        conn.commit()
        try:
            store._migrate(conn)
            have = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
            wanted = {c for t, c, _ in store._ADDED if t == "jobs"}
            missing = sorted(wanted - have)
            # Running it again must be silent, because it runs on every connect.
            store._migrate(conn)
        finally:
            # Windows will not delete a file SQLite still holds open, and
            # the temporary directory is removed on the way out.
            conn.close()

    if missing:
        raise Failed(f"_migrate did not add: {missing}")
    return (f"{len(store._ADDED)} columns across "
            f"{len({t for t, _, _ in store._ADDED})} tables, "
            f"and a missing table skipped")


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


def check_a_redelivery_does_not_render_twice() -> str:
    """A broker redelivers on any doubt. The work has to be safe under it.

    Without the attempt record a redelivered task is rendered again: a
    finished job overwrites a video somebody may already hold a link to and
    sends a second email about it, and a job that failed on its own inputs
    spends another half hour failing the same way.

    The record is checked on EVERY delivery, not only on ones the broker
    flags as redelivered — a duplicate the janitor published is a first
    delivery as far as RabbitMQ is concerned, which is the case that flag
    misses.
    """
    from app import pipeline
    from app.queue import attempt, worker
    from app.queue.messages import RenderTask

    with tempfile.TemporaryDirectory(prefix="svs_attempt_") as tmp:
        job_dir = pathlib.Path(tmp) / "job"
        job_dir.mkdir()
        task = RenderTask(job_id="job", upload="/tmp/x.mp4",
                          package="Some Score", attempt=2)

        rendered: list[str] = []

        def ran_the_render(*_a, **_k):
            # Raises the failure the worker EXPECTS, so the cases that are
            # supposed to render report cleanly instead of dumping a
            # traceback; the cases that are not supposed to render assert
            # that `rendered` stayed empty.
            rendered.append("ran")
            raise pipeline.PipelineError("stubbed render")

        # `find_package` is stubbed too: without it the task fails on "no
        # such score" before `pipeline.run` is ever reached, and the probe
        # for "did it render?" would never fire — which is exactly what the
        # first version of this check got wrong.
        saved = (pipeline.run, pipeline.job_folder, pipeline.find_package)
        pipeline.run = ran_the_render
        pipeline.job_folder = lambda _job_id: job_dir
        pipeline.find_package = lambda _name: object()
        try:
            # A finished attempt: report the same outcome, do not re-render.
            attempt.write(job_dir, 2, attempt.DONE, result="/tmp/out.mp4",
                          output_bytes=1234, elapsed=9.0)
            seen: list = []
            worker.handle_task(task, seen.append)
            if rendered:
                raise Failed("a finished attempt was rendered again")
            if [e.type for e in seen] != ["done"]:
                raise Failed(f"expected one 'done', got {[e.type for e in seen]}")
            if seen[0].result != "/tmp/out.mp4" or seen[0].output_bytes != 1234:
                raise Failed("the repeated outcome lost the original's detail")

            # A failed attempt: report the failure again, do not re-run it.
            attempt.write(job_dir, 2, attempt.FAILED, error="ffmpeg said no",
                          error_class="ToolFailed", returncode=1)
            seen = []
            worker.handle_task(task, seen.append)
            if rendered:
                raise Failed("a failed attempt was run again")
            if [e.type for e in seen] != ["failed"] or seen[0].error != "ffmpeg said no":
                raise Failed(f"the failure was not repeated: {seen}")

            # A *different* attempt of the same job must render: submitting
            # again with new colours is exactly that, and must not be
            # mistaken for a duplicate.
            other = RenderTask(job_id="job", upload="/tmp/x.mp4",
                               package="Some Score", attempt=3)
            seen = []
            worker.handle_task(other, seen.append)
            if not rendered:
                raise Failed("a new attempt was skipped as though it were a "
                             "redelivery of the old one")
            if [e.type for e in seen] != ["started", "failed"]:
                raise Failed(f"a new attempt did not run and report: "
                             f"{[e.type for e in seen]}")

            # An attempt that only got as far as `started` died mid-render,
            # and there is no result to report — it has to run again.
            rendered.clear()
            attempt.write(job_dir, 4, attempt.STARTED, worker="dead")
            fourth = RenderTask(job_id="job", upload="/tmp/x.mp4",
                                package="Some Score", attempt=4)
            worker.handle_task(fourth, lambda _e: None)
            if not rendered:
                raise Failed("an interrupted attempt was treated as finished")
        finally:
            pipeline.run, pipeline.job_folder, pipeline.find_package = saved
    return "done and failed repeated, new attempt runs, interrupted re-runs"


def check_expired_videos_are_reclaimed() -> str:
    """After the download window the video goes and the upload stays.

    `retention.is_live()` stopped serving expired videos from the start, but
    nothing deleted them, so every render stayed on disk for ever — 126 MB
    each, with no upper bound. A disk that fills stops the renders, and the
    first symptom has nothing to do with the cause.

    Both halves matter. The rendered video is derived — the upload is kept
    and the score and style are in the row — so reclaiming it discards a
    cache that one encode rebuilds. The upload is derived from nothing, is a
    recording of an identifiable person, and the privacy note commits to
    holding it. Deleting the wrong one is the difference between
    housekeeping and losing a visitor's recording.
    """
    from app import store
    from app.queue import webside
    from app.settings import settings

    with tempfile.TemporaryDirectory(prefix="svs_reclaim_") as tmp:
        home = pathlib.Path(tmp)
        old_video, new_video, upload = (home / "old.mp4", home / "new.mp4",
                                        home / "upload.mp4")
        for f in (old_video, new_video, upload):
            f.write_bytes(b"x" * 2048)

        window = settings.retention_hot_hours * 3600.0
        _queued("expired", state=store.DONE, result=str(old_video),
                upload=str(upload), finished=time.time() - window - 60)
        _queued("fresh", state=store.DONE, result=str(new_video),
                upload=str(upload), finished=time.time())

        freed = webside.reclaim_expired_outputs()

        if old_video.exists():
            raise Failed("an expired video was not reclaimed")
        if not new_video.exists():
            raise Failed("a video still inside its window was deleted")
        if not upload.exists():
            raise Failed("THE UPLOAD WAS DELETED — it is the one thing here "
                         "that cannot be rebuilt")
        if freed < 2048:
            raise Failed(f"reported freeing {freed} bytes, expected >= 2048")
        # The row survives, so the page still says the link expired rather
        # than the job becoming unknown.
        if store.get_job("expired") is None:
            raise Failed("reclaiming removed the job row as well")
    return "expired gone, fresh kept, upload untouched"


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


def check_the_visitor_list_counts_honestly() -> str:
    """Uploads and recognitions are not multiplied together, and no second
    copy of an address is kept.

    Two mistakes this guards, both of which produce a plausible-looking table
    rather than an error:

      * counting listens and uploads in one join. A visitor who asks about
        one recording three times becomes three uploads, and every per-visitor
        figure is silently inflated.
      * an address in the recognitions table. Deleting somebody's recording
        on request would then leave a copy behind, which the privacy page
        says does not happen — so the column is asserted absent rather than
        trusted to stay that way.
    """
    from app import store, visitors

    now = time.time()
    for jid, client, dur, state in [
            ("vis-a", "81.240.10.5", 600.0, store.DONE),
            ("vis-b", "81.240.10.5", 120.0, store.ERROR),
            ("vis-c", "8.8.8.8", 900.0, store.DONE)]:
        store.put_job({"id": jid, "created": now, "name": f"{jid}.mp4",
                       "upload": f"{jid}.mp4", "state": state,
                       "client": client, "duration": dur,
                       "size_bytes": 1_000_000})

    # Three listens against ONE of that visitor's two uploads. This is the
    # shape that breaks a join.
    for _ in range(3):
        store.put_recognition("vis-a", country="Belgium", city="Evere",
                              outcome="matched", piece_id="op39-3",
                              title="Scherzo No. 3", confidence=70.0,
                              duration=600.0)
    store.put_recognition("vis-c", country="United States",
                          city="Mountain View", outcome="matched",
                          piece_id="op39-3", title="Scherzo No. 3",
                          confidence=90.0, duration=900.0)

    columns = {r["name"] for r in store.query(
        "SELECT name FROM pragma_table_info('recognitions')")}
    if "client" in columns:
        raise Failed("the recognitions table carries an address; the privacy "
                     "page promises the only copy is on the job row")

    rows = {r["address"]: r for r in visitors.visitors()}
    belgian = rows.get("81.240.10.5")
    if belgian is None:
        raise Failed("an address that uploaded is not in the visitor list")
    if belgian["uploads"] != 2:
        raise Failed(f"2 uploads counted as {belgian['uploads']} — listens "
                     f"and uploads have been multiplied together")
    if belgian["identifications"] != 3:
        raise Failed(f"3 listens counted as {belgian['identifications']}")
    if belgian["delivered"] != 1 or belgian["failed"] != 1:
        raise Failed(f"outcomes miscounted: {dict(belgian)}")

    piece = next((p for p in visitors.pieces()
                  if p["piece_id"] == "op39-3"), None)
    if piece is None:
        raise Failed("a recognised piece is missing from the piece list")
    if piece["times"] != 4 or piece["uploads"] != 2:
        raise Failed(f"expected 4 answers over 2 recordings, got "
                     f"{piece['times']} over {piece['uploads']}")
    if piece["median_minutes"] != 10.0:
        raise Failed(f"median length {piece['median_minutes']}, expected 10.0")

    where = {r["country"]: r for r in visitors.countries()}
    if set(where) != {"Belgium", "United States"}:
        raise Failed(f"countries came out as {sorted(where)}")
    if where["Belgium"]["cities"] != "Evere":
        raise Failed(f"city lost: {where['Belgium']}")

    # Deleting the recording takes the address with it, and leaves the
    # statistic standing. This is the promise on the privacy page.
    store.write_returning("DELETE FROM jobs WHERE id = ? RETURNING id",
                          ("vis-a",))
    if any(r["address"] == "81.240.10.5" and r["uploads"] > 1
           for r in visitors.visitors()):
        raise Failed("a deleted recording still counts against its address")
    if not any(p["piece_id"] == "op39-3" for p in visitors.pieces()):
        raise Failed("deleting one recording erased the piece statistics")

    status = visitors.geolocation_status()
    return (f"2 uploads / 3 listens kept apart; geolocation "
            f"{'on' if status['available'] else 'off (optional)'}")


def check_a_result_is_safe_before_it_is_announced() -> str:
    """A render that cannot be stored is a FAILED job, not a finished one.

    This is the rule the whole compute design rests on: "the render
    succeeded" and "the result is safe" are different events, and only the
    second may be announced. If the worker reported `done` and kept the file
    locally, the job would look finished and the video would die with the
    machine — which is precisely the outcome object storage exists to
    prevent, reintroduced by the code meant to prevent it.

    Also checks the confirmation. `upload_file` returning without raising
    means the parts were accepted, not that an object of the right size is
    readable — so `put` HEADs afterwards, and a short object must raise
    rather than pass.
    """
    import types

    from app import storage

    # A bucket that accepts everything and returns a truncated object. The
    # nastiest realistic failure: the upload "works" and the result is
    # unusable, which is invisible without the HEAD.
    class Truncating:
        def upload_file(self, filename, bucket, key, **kw):
            self.key = key

        def head_object(self, Bucket, Key):
            return {"ContentLength": 1}

    storage.reset()
    storage._client = Truncating()          # noqa: SLF001
    storage._tried = True                   # noqa: SLF001
    try:
        if not storage.available():
            raise Failed("a stubbed client did not report as available")

        big = pathlib.Path(tempfile.gettempdir()) / "vsw-selftest-output.mp4"
        big.write_bytes(b"x" * 4096)
        try:
            storage.put(big, "jobs/x/output/x_PROCESSED.mp4")
        except storage.StorageError as exc:
            if "4096" not in str(exc) and "expected" not in str(exc):
                raise Failed(f"the wrong error for a short object: {exc}")
        else:
            raise Failed("a 1-byte object passed as a 4096-byte upload — the "
                         "HEAD confirmation is not being made")

        # Missing entirely is the other half: uploaded, and not there.
        class Absent(Truncating):
            def head_object(self, Bucket, Key):
                raise RuntimeError("NoSuchKey")

        storage._client = Absent()          # noqa: SLF001
        try:
            storage.put(big, "jobs/x/output/x_PROCESSED.mp4")
        except storage.StorageError:
            pass
        else:
            raise Failed("an object that is not there passed as stored")
        big.unlink(missing_ok=True)

        # The key never contains anything a visitor chose. A file name is
        # untrusted text and a slash in an S3 key is a directory separator.
        key = storage.output_key("abc123", ".mp4")
        if not key.startswith("jobs/abc123/") or "PROCESSED" not in key:
            raise Failed(f"unexpected key shape: {key}")
    finally:
        storage.reset()

    # Unconfigured must stay harmless: this is what a developer machine and
    # every test above it run with, and it has to behave exactly as before.
    if storage.available():
        raise Failed("storage reports available with nothing configured")
    if storage.head("anything") is not None:
        raise Failed("an unconfigured bucket answered a HEAD")
    if storage.presigned_get("anything") is not None:
        raise Failed("an unconfigured bucket signed a link")
    if not storage.status()["problem"]:
        raise Failed("storage is unavailable and will not say why")

    # And the event carries the key, or a stored result cannot be found again.
    from app.queue.messages import Event
    ev = Event(job_id="j", type="done", object_key="jobs/j/output/j.mp4")
    if Event.from_json(ev.to_json()).object_key != "jobs/j/output/j.mp4":
        raise Failed("object_key does not survive a round trip through the "
                     "event, so the ledger would never learn it")

    return "short and missing objects both rejected; unconfigured is inert"


def check_the_courtesy_mail_cannot_starve_the_promise() -> str:
    """The "queued" notice must never spend the "ready" mail's allowance.

    This exists because adding the queued notice DID exactly that. Both mails
    drew on one `mail_email` bucket of 3 a week against a render allowance of
    3 a week, so the second render's "your video is ready" was refused — and
    `_maybe_mail` skips silently when refused, so the person who waited an
    hour simply got nothing. The courtesy mail turned into a way of losing
    the mail that mattered.

    Two buckets make that impossible rather than unlikely, and this checks the
    property rather than the number: exhausting the queued allowance must
    leave the ready allowance untouched.
    """
    from app import limits

    limits._counters._hits.clear()                  # noqa: SLF001
    who = "someone@example.com"

    # Burn the queued allowance right down.
    burnt = 0
    while limits.allowed("mail_queued_email", who):
        burnt += 1
        if burnt > 50:
            raise Failed("the queued-notice allowance appears to be unlimited")
    if burnt == 0:
        raise Failed("the queued-notice allowance was already empty")

    # The promise must still be sendable, at least as many times as that
    # address may render — otherwise somebody's finished video goes unannounced.
    ready = 0
    while limits.allowed("mail_email", who):
        ready += 1
        if ready > 50:
            break
    renders = limits.RULES["render_email"].limit
    if ready < renders:
        raise Failed(
            f"after the queued notices ran out, only {ready} ready-mails "
            f"remain for an address allowed {renders} renders — a finished "
            f"video would go unannounced")

    limits._counters._hits.clear()                  # noqa: SLF001
    return f"{burnt} queued notices burnt, {ready} ready-mails still available"


def check_a_machine_is_only_wanted_while_there_is_work() -> str:
    """The shadow scaler's arithmetic, including the two ways to burn money.

    Nothing creates a machine yet, which is exactly why this is worth
    pinning: the same arithmetic becomes the real thing, and by then a
    mistake costs money rather than a wrong number on a graph.

    The two failures it guards:

      * tearing down before the grace period. Creating costs ~2 minutes of a
        visitor's wait, so a node that dies the instant a job ends makes the
        next one pay that again — which at a steady trickle of work is more
        expensive than never tearing down at all.
      * counting time while no machine would exist. `would_run` against the
        plan's hourly rate IS the bill, so an accumulator that keeps running
        in the `none` state reports a cost that would never be charged, and
        the decision it informs is made on a lie.
    """
    from app import store

    grace = 2.0
    store.write_returning("DELETE FROM jobs RETURNING id")
    store.write_returning("DELETE FROM compute_events RETURNING id")
    with store.write() as conn:
        conn.execute("UPDATE compute SET state='none', idle_since=NULL,"
                     " would_run=0, creates=0, destroys=0, updated=NULL"
                     " WHERE singleton = 1")

    first = store.compute_tick(grace)
    if first["state"] != "none":
        raise Failed(f"a machine is wanted with no work at all: {first}")

    store.put_job({"id": "shadow-1", "created": time.time(), "name": "a.mp4",
                   "upload": "a.mp4", "state": store.QUEUED,
                   "queued_at": time.time()})
    wanted = store.compute_tick(grace)
    if wanted["state"] != "wanted" or wanted["creates"] != 1:
        raise Failed(f"a queued job did not ask for a machine: {wanted}")

    # Running, not queued: still work, and the idle clock must not start.
    store.update_job("shadow-1", state=store.RUNNING, started=time.time())
    busy = store.compute_tick(grace)
    if busy["state"] != "wanted":
        raise Failed("a running job stopped wanting a machine")
    if busy["idle_seconds"] > 0:
        raise Failed(f"the idle clock is running while a job is: {busy}")

    store.update_job("shadow-1", state=store.DONE, finished=time.time())
    store.compute_tick(grace)                       # idle starts here
    time.sleep(grace + 0.3)

    # Idle well past the grace, but the paid hour has barely begun. It must
    # STAY UP: the provider rounds partial hours up, so that hour is already
    # bought, and releasing it early buys nothing while costing the next
    # visitor a ~2 minute boot.
    early = store.compute_tick(grace)
    if early["state"] != "wanted":
        raise Failed(
            f"released after {early['idle_seconds']:.1f}s idle with most of "
            f"the paid hour left. Linode rounds partial hours up, so that "
            f"time is already paid for; giving it back early wastes it and "
            f"makes the next job wait for a fresh boot")

    # Now wind the clock so the paid hour is nearly over. Same idle time,
    # opposite answer — which is the whole point of the rule.
    with store.write() as conn:
        conn.execute("UPDATE compute SET since = ? WHERE singleton = 1",
                     (time.time() - 3600 + 60,))
    gone = store.compute_tick(grace)
    if gone["state"] != "none" or gone["destroys"] != 1:
        raise Failed(
            f"not released even though the paid hour is ending and it has "
            f"been idle throughout: {gone}")

    # The bill must stop when the machine would. One more tick in `none`
    # must not add to it.
    held = gone["would_run"]
    time.sleep(0.3)
    after = store.compute_tick(grace)
    if after["would_run"] > held + 0.01:
        raise Failed(
            f"would_run grew from {held:.2f} to {after['would_run']:.2f} "
            f"while no machine would exist — that figure is the bill")

    actions = [r["action"] for r in store.compute_events()]
    if actions != ["would-destroy", "would-create"]:
        raise Failed(f"decisions not recorded in order: {actions}")
    if not all((r["reason"] or "").strip() for r in store.compute_events()):
        raise Failed("a decision was recorded with no reason")

    store.write_returning("DELETE FROM jobs RETURNING id")
    return (f"wanted while busy, HELD through the paid hour despite being "
            f"idle, released at the boundary; {held:.1f}s billed")


def check_mail_looks_like_mail() -> str:
    """The headers a receiver expects, and a body it can read.

    Every one of these was missing when Yahoo filed the first real message as
    spam, and all three were ours rather than the relay's:

      * `Date` — mandatory under RFC 5322, and `smtplib.send_message` does
        NOT add it. Scored on its own as MISSING_DATE.
      * `Message-ID` — same omission, scored as MISSING_MID, and without it
        no client can thread or de-duplicate.
      * a base64 body. One em-dash in "— Weefeen" was enough for
        `set_content` to choose base64 for the whole message, and a short
        plain-text mail that arrives entirely base64 is what obfuscators
        send to keep wording away from filters.

    The Message-ID domain is checked too: taken from the machine's hostname
    it would disagree with From and SPF, which is itself a signal.
    """
    from app import notify
    from app.settings import settings

    m = notify._compose("Subject here", "someone@example.com",   # noqa: SLF001
                        "A line.\n\n— Weefeen\n")

    for header in ("Date", "Message-ID", "From", "To", "Subject"):
        if not m.get(header):
            raise Failed(f"outgoing mail has no {header} header")

    cte = (m.get("Content-Transfer-Encoding") or "").lower()
    if cte != "7bit":
        raise Failed(
            f"the body is {cte}, not 7bit. Anything else means a relay may "
            f"re-encode or re-wrap it, and A2 relays our signed mail through "
            f"MailChannels — a changed body breaks the DKIM body hash, which "
            f"is what Yahoo reported")

    longest = max((len(ln) for ln in m.get_content().splitlines()), default=0)
    if longest > 72:
        raise Failed(
            f"a body line is {longest} characters. Long lines force soft "
            f"wrapping, and a relay that re-wraps differently changes the "
            f"body a signature was computed over")

    # Readable on the wire is the property, not the encoding's name.
    if "A line." not in m.as_string():
        raise Failed("the body is not legible in the encoded message")

    sender = settings.smtp_from or ""
    if "@" in sender:
        domain = sender.rsplit("@", 1)[-1].strip("> ").strip()
        if domain and domain not in m["Message-ID"]:
            raise Failed(
                f"Message-ID {m['Message-ID']} does not carry {domain}; a "
                f"message id that disagrees with From is itself a signal")

    return f"Date, Message-ID, {cte} body, id aligned with the sender"


def check_the_driver_will_not_delete_what_is_not_ours() -> str:
    """The only code here that spends money, and the rules that bound it.

    A restricted Linode user is the first guard: the scaler's token cannot
    see the web box at all. This is the second, and it exists because grants
    get widened later by people who have forgotten that this code assumed
    otherwise — at which point the only thing standing between a bug and
    somebody's production machine is a label check.

    Three properties, each of which has a plausible way of being lost:

      * a machine not named `vsw-compute…` is never CREATED, so `destroy`
        can always tell its own work from somebody else's.
      * a machine not named `vsw-compute…` is never DELETED, and the refusal
        raises rather than returning quietly — a teardown that silently
        does nothing is how an instance is left running for a month.
      * the label is read from the PROVIDER at delete time, not taken from
        the caller. Ids are reused; a stale row naming id 12345 would
        otherwise delete whatever now holds that id.
    """
    from app.compute import driver

    fake = driver.FakeDriver()

    mine = fake.create("vsw-compute-test", "plan", "image", "region", "", [])
    if not mine.is_ours:
        raise Failed("a machine we created is not recognised as ours")

    for bad in ("production-web", "vsw-web", "compute-1", ""):
        try:
            fake.create(bad, "plan", "image", "region", "", [])
        except driver.ComputeError:
            pass
        else:
            raise Failed(
                f"created a machine named {bad!r}; every machine must carry "
                f"the {driver.LABEL_PREFIX!r} prefix or the teardown cannot "
                f"tell what is safe to delete")

    # Somebody else's machine, sitting where ours was.
    theirs = fake.plant("weefeen-production")
    try:
        fake.destroy(theirs.id)
    except driver.ComputeError:
        pass
    else:
        raise Failed(
            "DELETED A MACHINE THAT WAS NOT OURS. The label check is the "
            "last thing between a bug and somebody's live server")
    if theirs.id not in fake.machines:
        raise Failed("the foreign machine was removed despite the refusal")

    if not fake.destroy(mine.id):
        raise Failed("could not delete our own machine")
    if fake.destroys != 1:
        raise Failed(f"expected exactly 1 destroy, got {fake.destroys}")

    # Already gone is success, not an error: the sweep runs repeatedly and
    # must not stall on a machine somebody removed by hand.
    if not fake.destroy(mine.id):
        raise Failed("deleting an absent machine reported failure")

    # And the real driver must refuse to exist without a token rather than
    # failing later, mid-decision, with something obscure.
    try:
        driver.LinodeDriver("")
    except driver.ComputeError:
        pass
    else:
        raise Failed("a driver was built with no token")

    return (f"{driver.LABEL_PREFIX!r} enforced on create and delete; "
            f"a foreign machine survived both")


def check_a_stored_project_explains_itself() -> str:
    """Everything a job produced is kept, and the manifest says what it is.

    A folder holding `measures.data`, `performance.npy` and a video is not
    self-describing. Which score was it aligned against? Which engraving of
    it? Reference recording or direct? Which librosa produced those numbers?
    Without that written down beside them the corpus is a pile of arrays,
    and the answer cannot be reconstructed once the machine that made it has
    been destroyed.

    Also checks what must NOT be in it. These objects are kept indefinitely
    for training while the job row is deleted on request, so a manifest
    carrying an address, an email or an uploaded file name would quietly
    undo that promise.
    """
    import json

    from app import storage

    folder = pathlib.Path(tempfile.mkdtemp(prefix="vsw-project-"))
    try:
        (folder / "measures.data").write_text("1 0.0\n2 1.5\n")
        (folder / "sync").mkdir()
        (folder / "sync" / "performance.npy").write_bytes(b"\x93NUMPY fake")
        output = folder / "piece_synced.mp4"
        output.write_bytes(b"video")
        # A render in progress is not a result.
        (folder / "piece.part.mp4").write_bytes(b"half a video")

        written = storage.write_manifest(
            folder, "job123", package="Op.39_Scherzo_(Breitkopf)",
            mode="reference", style={"aspect": "16/9"},
            duration=424.0, output=output, output_bytes=5, elapsed=448.0)
        manifest = json.loads(written.read_text(encoding="utf-8"))

        for key in ("job", "score_package", "alignment_mode", "media_seconds",
                    "produced_by", "files", "schema"):
            if key not in manifest:
                raise Failed(f"the manifest has no {key!r}")
        if manifest["alignment_mode"] != "reference":
            raise Failed("the alignment mode was not recorded, and it changes "
                         "what the numbers in measures.data mean")

        listed = {f["path"] for f in manifest["files"]}
        if "measures.data" not in listed or "sync/performance.npy" not in listed:
            raise Failed(f"the manifest does not list the work: {listed}")

        # No personal data, checked as a property of the whole document
        # rather than field by field — a future field could reintroduce it.
        blob = json.dumps(manifest).lower()
        for forbidden in ("@", "email", "client", "ip_address", "upload_name"):
            if forbidden in blob:
                raise Failed(
                    f"the manifest contains {forbidden!r}. These objects "
                    f"outlive the job row on purpose; personal data in them "
                    f"defeats deleting a visitor's recording on request")

        # The part file must not be offered for storage.
        storage.reset()
        plan = [p.name for p in sorted(folder.rglob("*")) if p.is_file()]
        if "piece.part.mp4" not in plan:
            raise Failed("the fixture is wrong; there is no part file to skip")
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        storage.reset()

    return (f"{len(manifest['files'])} files listed, mode and package "
            f"recorded, no personal data")


def check_the_scaler_cannot_run_away() -> str:
    """Off by default, bounded when on, and honest about what exists.

    This is the first code here that can spend money without a person
    watching, so the guards are checked as properties rather than trusted to
    have been written:

      * nothing is created while COMPUTE_ENABLED is off, however loudly the
        queue asks.
      * the hourly ceiling is counted from machines that WERE created, not
        from what this process remembers intending — a scaler restarting in
        a loop would otherwise reset its own count each time and create
        without limit, which is the exact failure the ceiling exists for.
      * two machines existing is refused rather than reconciled by guessing.
        Deleting the wrong one is worse than doing nothing and saying so.
    """
    from app import store
    from app.compute import driver as drv
    from app.compute import scaler as scl
    from app.settings import settings

    fake = drv.FakeDriver()
    s = scl.Scaler(driver=fake)

    # -- off means off ---------------------------------------------------
    store.write_returning("DELETE FROM jobs RETURNING id")
    store.put_job({"id": "scaler-1", "created": time.time(), "name": "a.mp4",
                   "upload": "a.mp4", "state": store.QUEUED,
                   "queued_at": time.time()})
    with store.write() as conn:
        conn.execute("UPDATE compute SET state='none', idle_since=NULL,"
                     " machine_id=NULL WHERE singleton = 1")

    if settings.compute_enabled:
        raise Failed("COMPUTE_ENABLED is on during the checks; it must "
                     "default to off so nothing is created by a test run")
    did = s.tick()
    if fake.creates:
        raise Failed(f"created a machine with the switch off: {did}")
    if "would create" not in did["did"]:
        raise Failed(f"expected a would-create while off, got {did['did']!r}")

    # -- the ceiling counts what happened, not what we remember ----------
    store.write_returning("DELETE FROM compute_events RETURNING id")
    ceiling = settings.compute_max_creates_per_hour
    for n in range(ceiling):
        store.compute_record_create(9000 + n, f"vsw-compute-{n}")
    if store.compute_creates_this_hour() != ceiling:
        raise Failed(f"the ledger counted {store.compute_creates_this_hour()} "
                     f"of {ceiling} creates")

    # A brand new Scaler — as after a restart — must still see them.
    fresh = scl.Scaler(driver=drv.FakeDriver())
    if store.compute_creates_this_hour() < ceiling:
        raise Failed("a restarted scaler forgot the creates already made; "
                     "the ceiling would reset on every crash")

    # -- two machines is refused, not guessed at -------------------------
    crowded = drv.FakeDriver()
    crowded.create("vsw-compute-a", "p", "i", "r", "", [])
    crowded.create("vsw-compute-b", "p", "i", "r", "", [])
    try:
        scl.Scaler(driver=crowded).tick()
    except drv.ComputeError:
        pass
    else:
        raise Failed("two machines existing was not refused; picking one to "
                     "delete is how the wrong one goes")
    if crowded.destroys:
        raise Failed("it deleted something while confused about how many "
                     "machines exist")

    store.write_returning("DELETE FROM jobs RETURNING id")
    store.write_returning("DELETE FROM compute_events RETURNING id")
    return (f"off by default; ceiling of {ceiling} survives a restart; "
            f"a crowded account is refused, not guessed")


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
        check_a_redelivery_does_not_render_twice,
        check_expired_videos_are_reclaimed,
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
        check_the_visitor_list_counts_honestly,
        check_a_result_is_safe_before_it_is_announced,
        check_the_courtesy_mail_cannot_starve_the_promise,
        check_a_machine_is_only_wanted_while_there_is_work,
        check_mail_looks_like_mail,
        check_the_driver_will_not_delete_what_is_not_ours,
        check_a_stored_project_explains_itself,
        check_the_scaler_cannot_run_away,
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
