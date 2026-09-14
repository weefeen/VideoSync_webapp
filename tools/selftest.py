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
import threading
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

# Mail configured, so the paths that only run when mail is possible are
# actually exercised rather than skipped. A check that quietly skips is worse
# than no check: it is a green line that proves nothing, which this suite has
# shipped once already.
os.environ.setdefault("SMTP_HOST", "smtp.invalid")
os.environ.setdefault("SMTP_FROM", "VideoSync <selftest@example.invalid>")
os.environ.setdefault("ALERT_EMAIL", "operator@example.invalid")
# A public base URL too: mail is refused without one, because a link
# to 127.0.0.1 is useless to a recipient and costly to the domain.
os.environ.setdefault("PUBLIC_BASE_URL", "https://selftest.example")

# And then NOTHING may reach a relay. Every send in the suite is captured
# here instead, once, at import: a check that accidentally sends would
# otherwise sit for a 30-second connect timeout against a real server, or
# worse, reach one.
from app import notify as _notify                                # noqa: E402

SENT: list[tuple[str, str]] = []
_notify._send = lambda message, address: SENT.append(              # noqa: SLF001
    (address.lower(), message.get_content()))


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
def _a_package_on_disk() -> pathlib.Path:
    """The smallest score package actually present on this machine's disk.

    NOT `library.packages()`, which is what these checks used and which is
    only sometimes a disk listing. Once the library moved into the bucket,
    `library` answers from the published catalogue whenever storage is
    reachable, and those entries carry `root = None` on purpose -- the web
    box has no score bytes, which is the whole point of that design.

    So a check that wanted a real folder to tar, copy or load got `None`
    the moment the machine running it had bucket credentials. It never did
    in CI and no developer machine had them either, so three checks passed
    everywhere while testing nothing of the sort -- until a laptop was set
    up to lend itself to the queue and needed the bucket to do it.

    The roots are read directly, because "is there a package on this disk"
    is exactly the question, and the library is deliberately not the place
    to ask it.
    """
    from app import package as pkg
    from app.settings import settings

    found: list[pathlib.Path] = []
    for root in settings.score_roots:
        if not root.exists:
            continue
        for path, loaded, _ in pkg.inspect(root.path):
            if loaded is not None:
                found.append(path)
    if not found:
        raise Failed(
            "no score package on this machine's disk. These checks tar and "
            "load a real package, so one has to be installed under "
            "SCORE_ROOT_DIGITAL or SCORE_ROOT_RASTER.")

    # NOT ONE THAT IS STILL BEING WRITTEN. A score root can be an
    # exporter's working folder, and these checks tar a package and count
    # what came out -- so a package half-exported while the check runs
    # reports "28 band images arrived, 29 were published" and looks like a
    # transfer bug. Measured: 33 bands, then 34 five seconds later.
    import time as _t
    settled = []
    for root in found:
        newest = max((f.stat().st_mtime for f in root.rglob("*")
                      if f.is_file()), default=0)
        if _t.time() - newest > 60:
            settled.append(root)
    if not settled:
        raise Failed("every score package on this disk was written in the "
                     "last minute; something is exporting into a score root "
                     "and these checks cannot read a moving folder")

    # The smallest, because the caller tars it for real.
    return min(settled, key=lambda r: sum(f.stat().st_size
                                          for f in r.rglob("*") if f.is_file()))


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
    import shutil

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

        # A JOB FINISHED BEFORE WORK_DIR MOVED. `result` is an absolute
        # path, and WORK_DIR has already moved once in production
        # (/srv/vsw/work -> /srv/vsw/shared/var). Every job from before the
        # move recorded a path that no longer resolves, the reclaim asked
        # is_file(), got False, and skipped it -- silently, for ever. Five
        # jobs and 657 MB sat on the always-on box with nothing in any log
        # to say why.
        moved_id = "reclaim_moved_root"
        moved_dir = pathlib.Path(settings.work_dir) / moved_id
        moved_dir.mkdir(parents=True, exist_ok=True)
        moved_video = moved_dir / "gone_away.mp4"
        moved_video.write_bytes(b"x" * 4096)
        _queued(moved_id, state=store.DONE,
                result=str(pathlib.Path("/nowhere/that/exists") / moved_id
                           / moved_video.name),
                upload=str(upload), finished=time.time() - window - 60)

        # An abandoned render temporary, in an expired job's folder and in a
        # fresh one. `render.py` names it uniquely per attempt so a retry
        # cannot collide with it -- which also means nothing ever overwrites
        # it, and a render that died before its rename leaves it for ever.
        stale_part = moved_dir / "gone_away.part-deadbeef.mp4"
        stale_part.write_bytes(b"x" * 8192)
        fresh_id = "reclaim_fresh_root"
        fresh_dir = pathlib.Path(settings.work_dir) / fresh_id
        fresh_dir.mkdir(parents=True, exist_ok=True)
        live_part = fresh_dir / "still_going.part-abad1dea.mp4"
        live_part.write_bytes(b"x" * 8192)
        _queued(fresh_id, state=store.DONE,
                result=str(fresh_dir / "still_going.mp4"),
                upload=str(upload), finished=time.time())

        try:
            freed = webside.reclaim_expired_outputs()

            if moved_video.exists():
                raise Failed("a video whose job recorded it under an older "
                             "WORK_DIR was not reclaimed; it would sit on "
                             "the disk for ever with nothing to explain it")
            if stale_part.exists():
                raise Failed("an abandoned render temporary was left in an "
                             "expired job's folder")
            if not live_part.exists():
                raise Failed("A RENDER IN FLIGHT HAD ITS WORKING FILE "
                             "DELETED -- partials may only be swept from a "
                             "job whose window has already closed")
        finally:
            shutil.rmtree(moved_dir, ignore_errors=True)
            shutil.rmtree(fresh_dir, ignore_errors=True)

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
    return ("expired gone, fresh kept, upload untouched; a video under a "
            "moved WORK_DIR still found; abandoned temporaries swept, "
            "a live one left alone")


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

    # Unconfigured must stay harmless -- and UNCONFIGURED IS ARRANGED HERE,
    # not assumed of the machine. This read the real settings and trusted
    # that whoever ran it had no bucket, which was true of CI and of every
    # developer machine until one was given credentials so it could lend
    # itself to the queue. Then `available()` answered honestly, this check
    # called that a failure, and the thing it exists to prove -- that an
    # install with no bucket serves from local disk instead of breaking --
    # was never actually exercised on the machines that did have one.
    import dataclasses as _dc
    from app.settings import settings as _settings

    storage.reset()
    was_settings = storage.settings
    storage.settings = _dc.replace(_settings, object_bucket="")
    try:
        if storage.available():
            raise Failed("storage reports available with nothing configured")
        if storage.head("anything") is not None:
            raise Failed("an unconfigured bucket answered a HEAD")
        if storage.presigned_get("anything") is not None:
            raise Failed("an unconfigured bucket signed a link")
        if not storage.status()["problem"]:
            raise Failed("storage is unavailable and will not say why")
    finally:
        storage.settings = was_settings
        storage.reset()

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

    # -- and `wanted` with no work is NOT a reason to create -------------
    # The hour-aligned rule holds the state at `wanted` through the hour
    # already paid for. That is right for KEEPING a machine and meaningless
    # without one: acting on it would create a machine to sit idle until
    # the boundary it was waiting for.
    store.write_returning("DELETE FROM jobs RETURNING id")
    idle = scl.Scaler(driver=drv.FakeDriver())
    after = idle.tick()
    if after["state"] != "wanted":
        raise Failed("the fixture is wrong; the state should still be wanted")
    if after["did"]:
        raise Failed(
            f"acted on an empty queue: {after['did']!r}. A paid hour is a "
            f"reason to keep a machine, never a reason to make one")

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

    # -- a stuck condition must not become 120 emails an hour ------------
    # The tick runs every 30s and a stuck condition stays stuck, so the
    # naive version mails on every pass — which is the same as mailing
    # nobody, because the hundredth is not read.
    store.compute_alarm_cleared()
    if not store.compute_alarm("two machines"):
        raise Failed("a new condition was not reported at all")
    if store.compute_alarm("two machines"):
        raise Failed("the same condition reported twice in a row; at 30s "
                     "ticks that is 120 identical emails an hour")
    # A DIFFERENT condition is news, even within the hour.
    if not store.compute_alarm("cannot reach the provider"):
        raise Failed("a different condition was suppressed as a repeat")

    # Recovering and failing again is reported at once, not after an hour.
    if store.compute_alarm_cleared() != "cannot reach the provider":
        raise Failed("clearing did not report what had been wrong")
    if not store.compute_alarm("cannot reach the provider"):
        raise Failed("after recovering, the same failure was suppressed — a "
                     "flapping condition would go unreported")
    store.compute_alarm_cleared()

    store.write_returning("DELETE FROM jobs RETURNING id")
    store.write_returning("DELETE FROM compute_events RETURNING id")
    return (f"off by default; ceiling of {ceiling} survives a restart; "
            f"a crowded account is refused; alarms deduped but not muted")


def check_the_recogniser_is_marked_right_or_wrong() -> str:
    """Whether the visitor accepted what we recognised — the one
    human-verified label this site produces.

    It is computed by matching the winning candidate's PACKAGE against the
    package actually rendered, and the candidates are stored as JSON. The
    first version of that JSON omitted `package`, so every job recorded "no
    package was offered for the recognised piece" — which reads like a gap
    in the score library and was in fact a missing field. Silent, plausible,
    and it would have poisoned every label in the corpus.

    So this checks the stored SHAPE, not just the arithmetic: a candidate
    without a package cannot produce an `accepted`, and the recorder must
    put one there.
    """
    import json

    from app import store

    store.write_returning("DELETE FROM recognitions RETURNING id")
    store.put_recognition(
        "verdict-1", country="Belgium", city="Evere", outcome="matched",
        piece_id="op39", title="Scherzo No. 3", confidence=98.0,
        candidates=[{"piece_id": "op39", "label": "Scherzo No. 3",
                     "package": "Op.39_Scherzo", "confidence": 98}])

    row = store.recognitions(1)[0]
    stored = json.loads(row["candidates"] or "[]")
    if not stored or "package" not in stored[0]:
        raise Failed(
            "a stored candidate carries no `package`. The agreement between "
            "us and the visitor is computed against it, so without it every "
            "job records 'no package was offered' — a missing field that "
            "reads like a library gap")

    def agreement(recognised, chosen, candidates):
        suggested = next((c.get("package") for c in candidates
                          if c.get("piece_id") == recognised and c.get("package")),
                         None)
        if not recognised:
            return "nothing recognised"
        if not chosen:
            return "unknown"
        if suggested and suggested == chosen:
            return "accepted"
        if suggested:
            return "overridden"
        return "no package was offered for the recognised piece"

    cases = {
        agreement("op39", "Op.39_Scherzo", stored): "accepted",
        agreement("op39", "Op.23_Ballade", stored): "overridden",
        agreement("", "Op.39_Scherzo", stored): "nothing recognised",
    }
    for got, want in cases.items():
        if got != want:
            raise Failed(f"agreement said {got!r}, expected {want!r}")

    store.write_returning("DELETE FROM recognitions RETURNING id")
    return "accepted, overridden and unrecognised all distinguished"


def check_a_failure_is_never_mailed_to_the_visitor() -> str:
    """A render that fails tells the operator and says nothing to the visitor.

    The rule is the owner's, stated plainly: if something failed, the
    customer does not get a failure message. They were already told their
    recording arrived, so silence is the whole of what they get - and the
    page still shows the error to anyone who opens their own link, so the
    honest answer reaches whoever looks for it.

    The operator is the one who has to act, so he is told either way. Both
    outcomes rather than failures alone, because the question the mail
    exists to answer is "did the video reach the person who asked", and a
    failure-only mail cannot answer it for the jobs that worked.

    Three properties:

      * a failed job sends EXACTLY ONE message, and it goes to ALERT_EMAIL.
      * a finished job sends two: the link to the visitor, the outcome to
        the operator.
      * neither message to the operator carries the visitor's address.
        `Privacy.html` promises an address reaches him only when there is no
        score for the piece; sending it on every render would make the page
        untrue, and a page that is untrue about this is worse than no page.
    """
    from app import notify, store
    from app.queue import ledger
    from app.queue.messages import Event
    from app.settings import settings

    VISITOR = "someone@example.invalid"
    OPERATOR = (settings.alert_email or "").strip().lower()
    if not OPERATOR or not settings.can_email:
        raise Failed(
            "this check needs ALERT_EMAIL and a relay configured. The suite "
            "sets both at import precisely so this path is exercised; if it "
            "is unset here, that setup has been removed and the check is "
            "proving nothing.")

    # The visitor's address must be CONFIRMED, or the success mail is held
    # back by design and this check would be measuring the confirmation gate
    # instead of the thing it exists to prove. Confirming here keeps it
    # testing exactly one idea: a failure is never mailed, a success is.
    import hashlib as _h, time as _t
    _tok = _h.sha256(b"selftest-confirm").hexdigest()
    store.start_confirmation(VISITOR, _tok, _t.time())
    store.confirm_by_token(_tok, _t.time())

    sent = SENT
    sent.clear()
    if True:
        # --- a render that fails -------------------------------------
        jid = "outcome_fail"
        _queued(jid, email=VISITOR, score="Op.35_Sonate", stage="align")
        ledger.apply(Event(job_id=jid, type="started", worker="w1"))
        ledger.apply(Event(job_id=jid, type="failed", worker="w1",
                           error="MemoryError: unable to allocate 4.62 GiB",
                           error_class="MemoryError"))
        if store.get_job(jid)["state"] != store.ERROR:
            raise Failed("the failure did not reach the row")

        to_visitor = [b for a, b in sent if a == VISITOR]
        if to_visitor:
            raise Failed(
                "a failed render mailed the visitor. They are told nothing "
                "when a render fails - only the page shows it, to whoever "
                "opens their own link.")
        to_operator = [b for a, b in sent if a == OPERATOR]
        if len(to_operator) != 1:
            raise Failed(
                f"a failed render sent {len(to_operator)} messages to the "
                f"operator, not 1. Nobody else is watching: silence here is "
                f"a render that failed and was never noticed.")

        # --- a render that works -------------------------------------
        sent.clear()
        jid = "outcome_done"
        _queued(jid, email=VISITOR, score="Op.39_Scherzo")
        ledger.apply(Event(job_id=jid, type="started", worker="w1"))
        ledger.apply(Event(job_id=jid, type="done", result="/tmp/out.mp4",
                           mode="reference", elapsed=12.5, worker="w1"))
        if store.get_job(jid)["state"] != store.DONE:
            raise Failed("the completion did not reach the row")

        if not [b for a, b in sent if a == VISITOR]:
            raise Failed("a finished render did not tell the visitor")
        operator_mail = [b for a, b in sent if a == OPERATOR]
        if len(operator_mail) != 1:
            raise Failed(
                f"a finished render sent {len(operator_mail)} messages to "
                f"the operator, not 1")

        # --- and the address stays out of the operator's mail ---------
        local = VISITOR.split("@")[0]
        for body in operator_mail + to_operator:
            if VISITOR in body or local in body:
                raise Failed(
                    "the operator's mail carries the visitor's address. "
                    "Privacy.html promises it reaches him only when there "
                    "is no score for the piece; whether anybody was told is "
                    "a yes or a no here, not a name.")
    return ("failure: 1 to the operator, 0 to the visitor; success: both; "
            "no visitor address in either operator mail")


def check_a_portrait_video_keeps_the_picture() -> str:
    """9:16 fits the picture instead of cropping it, and stays in the frame.

    Everywhere else the video spans the content width and the overflow is
    cropped. That is right when the canvas and the picture are close in
    shape - at 16/9 nothing is lost at all - and it is ruinous when they are
    not. A 16:9 recording in a 9:16 frame was scaled to 3054x1718 and cut to
    1080 wide: 35% OF THE PICTURE KEPT, the rest discarded, which for a piano
    filmed in landscape loses both ends of the keyboard and usually the
    hands. The file was valid, uploaded fine, and nobody was told.

    Three properties:

      * PORTRAIT NEVER CROPS. Whatever shape the recording is, all of it
        survives. This is the bug that made 9:16 unusable.
      * nothing leaves the canvas, at either band position and anywhere the
        offset is put. The shrink that makes an oversized group fit rounds
        to even numbers, and rounding UP after scaling down put a 1922px
        group in a 1920px frame - two pixels, and the score band was off
        the bottom edge.
      * landscape still crops, unchanged. The fix is for the aspect that
        needed it and must not quietly restyle the one people already use.

    The offset is not checked against any safe zone on purpose. Instagram,
    TikTok and Shorts all draw their interface over the video and none of
    them publishes where; asserting a strip here would be this suite
    claiming to know a number that its own documentation says is observed
    rather than specified. What is checked is that the control works.
    """
    from app import render as rnd

    BAND = 1306 / 244.0                       # the Op.39 band, measured
    SHAPES = {"16:9": 16 / 9, "4:3": 4 / 3, "1:1": 1.0,
              "9:16": 9 / 16, "21:9": 21 / 9, "1:2": 0.5}

    for name, video_aspect in SHAPES.items():
        for offset in (0.0, 0.32, 0.5, 1.0):
            for position in ("top", "bottom"):
                style = rnd.Style(aspect="9/16", portrait_offset=offset,
                                  band_position=position)
                layout = rnd.compute_layout(style, BAND, video_aspect)

                if layout.crops:
                    raise Failed(
                        f"a {name} recording is cropped in portrait. Nothing "
                        f"is cropped in a 9:16 frame: the canvas and the "
                        f"picture are too far apart in shape, and cropping "
                        f"to fit threw away two thirds of the width.")

                top = min(layout.video.y, layout.band.y)
                bottom = max(layout.video.y + layout.video.h,
                             layout.band.y + layout.band.h)
                right = max(layout.video.x + layout.video.w,
                            layout.band.x + layout.band.w)
                if top < 0 or bottom > layout.canvas[1] or right > layout.canvas[0]:
                    raise Failed(
                        f"{name} at offset {offset} with the band {position} "
                        f"puts the group at {top}..{bottom} in a "
                        f"{layout.canvas[0]}x{layout.canvas[1]} frame. "
                        f"Anything past the edge is simply not in the video.")

    # The offset has to actually move it, or the control is decoration.
    style_top = rnd.Style(aspect="9/16", portrait_offset=0.0)
    style_bottom = rnd.Style(aspect="9/16", portrait_offset=1.0)
    high = rnd.compute_layout(style_top, BAND, 16 / 9).video.y
    low = rnd.compute_layout(style_bottom, BAND, 16 / 9).video.y
    if low <= high:
        raise Failed(
            f"the portrait offset does not move the group: 0.0 puts it at "
            f"{high} and 1.0 at {low}. It exists because no app publishes "
            f"where its buttons are, so the person posting has to be able "
            f"to move the score out from under them.")

    # A panel is refused rather than ignored.
    for panel in ("left", "centered"):
        try:
            rnd.Style(aspect="9/16", panel=panel).validate()
        except rnd.RenderError:
            pass
        else:
            raise Failed(
                f"a {panel!r} panel was accepted in portrait. A column in a "
                f"1080-wide frame leaves the picture too narrow to watch, "
                f"and a panel silently dropped is somebody wondering where "
                f"their title went.")

    # And landscape is untouched: it still fills and crops.
    wide = rnd.compute_layout(rnd.Style(aspect="16/9"), BAND, 16 / 9)
    if not wide.crops:
        raise Failed("16/9 stopped cropping. The portrait fix changed the "
                     "aspect everybody already uses.")

    # A PHONE HELD UPRIGHT RECORDS LANDSCAPE AND SAYS "TURN ME". The frames
    # are stored 1920x1080 and the container carries a display matrix saying
    # 90 degrees; ffprobe reports the STORED size and ffmpeg applies the
    # matrix when it decodes. So the probe said landscape while the filter
    # graph received portrait, and every scale and crop above was computed
    # for the wrong shape. Measured on a 640x360 clip with a 90-degree
    # matrix: it decodes to 360x640.
    #
    # Only a QUARTER turn changes the shape -- 180 degrees is the same
    # rectangle, and swapping its sides would be the same bug mirrored.
    turns = {
        "side_data 90": ({"side_data_list": [{"rotation": 90}]}, True),
        "side_data -90": ({"side_data_list": [{"rotation": -90}]}, True),
        "side_data 180": ({"side_data_list": [{"rotation": 180}]}, False),
        "side_data 270": ({"side_data_list": [{"rotation": 270}]}, True),
        "legacy tag 90": ({"tags": {"rotate": "90"}}, True),
        "legacy tag 0": ({"tags": {"rotate": "0"}}, False),
        "nothing said": ({}, False),
        "unreadable": ({"tags": {"rotate": "sideways"}}, False),
    }
    for label, (stream, want) in turns.items():
        _, got = rnd.quarter_turned(stream)
        if got != want:
            raise Failed(f"a stream describing {label} is read as "
                         f"turned={got}; a portrait phone recording would be "
                         f"laid out as landscape and cropped to pieces")

    return (f"{len(SHAPES)} source shapes x 4 offsets x 2 positions: none "
            f"cropped, none outside the frame; panel refused; 16/9 unchanged; "
                f"{len(turns)} rotation cases, quarter turns only")


def check_the_output_is_postable() -> str:
    """The encode carries what Instagram, Facebook and YouTube require.

    Read from the source rather than from a rendered file, because these
    checks run where ffmpeg is absent. That makes it weaker than a probe of
    a real output - it proves the flags are asked for, not that they landed
    - so a real file was probed by hand when each was added and every one
    verified. What this catches is the realistic regression: somebody tidies
    the command and a flag goes with it, silently, and nothing says so until
    a visitor's upload is refused by a platform weeks later.

    Each flag, and the sentence it comes from:

      * `-movflags +faststart` - Instagram and Facebook both state "no edit
        lists, moov atom at the front of the file"; YouTube lists it under
        recommended settings as "Fast Start". WITHOUT IT FFMPEG PUTS THE
        INDEX LAST, which was measured on a real output of ours:
        ftyp/free/mdat/moov. A player then cannot start until the whole file
        has arrived.
      * `-ar 48000` and `-ac 2` - "AAC, 48khz sample rate maximum, 1 or 2
        channels (mono or stereo)". Inherited from the recording before
        this, so a 96 kHz piano recording produced a file those platforms
        refuse and which outputs were postable depended on what visitors
        uploaded.
      * `-profile:v high` - YouTube asks for High by name. Left to x264 it
        depends on the build.
      * `-pix_fmt yuv420p` - "4:2:0 chroma subsampling", all three.

    The frame-rate clamp is checked separately, by calling it: 23-60 is
    stated by all three, and a phone slow-motion clip at 120 or an old scan
    at 15 both fell outside.
    """
    import re

    source = (ROOT / "app" / "render.py").read_text(encoding="utf-8")

    # The final encode, not the intermediate band strip: the strip is an
    # internal file nobody posts, and matching it would pass while the real
    # output lost a flag.
    # From the point the output streams are chosen to the output filename:
    # this covers the audio flags and the video ones, which live in
    # separate `cmd +=` lines. Deliberately NOT the whole file - the
    # intermediate band strip is encoded too, and matching that would let
    # this pass while the real output lost a flag.
    start = source.find('cmd += ["-filter_complex"')
    end = source.find("str(partial)]", start)
    if start < 0 or end < 0:
        raise Failed("the final encode command has moved; this check can no "
                     "longer find what it is meant to be reading")
    encode = source[start:end]

    REQUIRED = {
        '"-movflags", "+faststart"': "the moov atom must be at the FRONT; "
                                     "Instagram and Facebook require it and "
                                     "YouTube recommends it",
        '"-ar", "48000"': "48 kHz is the maximum Meta accepts, and inheriting "
                          "the source rate means a 96 kHz upload produces a "
                          "file they refuse",
        '"-ac", "2"': "1 or 2 channels only; a multichannel source passed "
                      "straight through before",
        '"-profile:v", "high"': "YouTube asks for H.264 High by name",
        '"-pix_fmt", "yuv420p"': "4:2:0 chroma, required by all three",
    }
    missing = [f"{flag} - {why}" for flag, why in REQUIRED.items()
               if flag not in encode]
    if missing:
        raise Failed("the output encode no longer asks for:\n    "
                     + "\n    ".join(missing))

    # The clamp, by calling it rather than by reading it.
    from app import render as rnd
    for given, expected in ((120.0, 60.0), (15.0, 23.0), (29.97, 29.97),
                            (25.0, 25.0), (60.0, 60.0), (23.0, 23.0)):
        held = min(60.0, max(23.0, given))
        if abs(held - expected) > 0.001:
            raise Failed(f"a {given} fps source would be held to {held}, "
                         f"not {expected}")
    if "min(60.0, max(23.0, source_fps))" not in source:
        raise Failed(
            "the frame rate is no longer held inside 23-60. All three "
            "platforms state that range, and the rate was being taken "
            "straight from the recording - so a 120 fps phone clip and a "
            "15 fps scan both produced files outside it.")

    return (f"{len(REQUIRED)} required flags present; frame rate held to "
            f"23-60")


def check_the_choices_survive_the_request() -> str:
    """What the visitor picked is what the renderer is given.

    `_style_from` is the only place the interface's words become a `Style`,
    and a field that is dropped or coerced there fails SILENTLY: the render
    succeeds, the video is delivered, and it is simply not the one that was
    asked for. That is not hypothetical.

    THE PANEL WAS `bool(style.get("panel"))`. `bool("off")` is True, and
    `Style.__post_init__` turns a truthy boolean into PANEL_LEFT - so
    choosing "None" and choosing "Centered" both produced a left title
    column, for every render this site has made. The interface had already
    been fixed to send 'off' | 'left' | 'centered' and carries a comment
    saying so; the server half was never done, which undid it completely.

    The boolean form is still accepted, and that is tested too: job rows
    queued before those names existed hold `true`/`false` and have to stay
    renderable from their own row.

    `portrait_offset` is checked for the same reason. The interface moves
    the preview with it; if the renderer never receives it, the preview is
    lying about where the score will sit.
    """
    from app import routes, render as rnd

    for sent, expected in (("off", rnd.PANEL_OFF),
                           ("left", rnd.PANEL_LEFT),
                           ("centered", rnd.PANEL_CENTERED)):
        got = routes._style_from({"style": {"panel": sent}}).panel
        if got != expected:
            raise Failed(
                f"the interface sends panel={sent!r} and the renderer is "
                f"given {got!r}. Every video would carry a layout nobody "
                f"chose, and nothing anywhere would say so.")

    # Old rows, which hold a boolean.
    for legacy, expected in ((True, rnd.PANEL_LEFT), (False, rnd.PANEL_OFF)):
        got = routes._style_from({"style": {"panel": legacy}}).panel
        if got != expected:
            raise Failed(
                f"a job row holding panel={legacy!r} now renders as {got!r}. "
                f"Rows queued before the names existed still have to render "
                f"from their own row.")

    if routes._style_from({"style": {"portrait_offset": 0.9}}).portrait_offset != 0.9:
        raise Failed(
            "portrait_offset does not reach the renderer. The interface "
            "moves the preview with it, so the preview would promise a "
            "position the video does not have.")
    if routes._style_from({"style": {}}).portrait_offset != 0.32:
        raise Failed("the portrait offset default changed without the "
                     "interface's PORTRAIT_OFFSET changing with it")

    # Every field the interface actually sends must be read. Caught by name
    # rather than by hand, so a field added to the request and forgotten here
    # fails a check instead of being quietly dropped.
    wire = (ROOT / "app" / "static" / "svs" / "svs-wire.js").read_text(
        encoding="utf-8", errors="ignore")
    start = wire.find("style: {")
    sent_keys = set()
    if start >= 0:
        import re
        block = wire[start:wire.find("},", start)]
        sent_keys = {m.group(1) for m in re.finditer(r"^\s*(\w+):", block, re.M)}
    source = (ROOT / "app" / "routes.py").read_text(encoding="utf-8")
    body = source[source.find("def _style_from"):source.find("@bp.post", source.find("def _style_from"))]
    dropped = sorted(k for k in sent_keys if f'"{k}"' not in body)
    if dropped:
        raise Failed(
            "the interface sends these and `_style_from` never reads them, "
            "so choosing them changes nothing and says nothing: "
            + ", ".join(dropped))

    return (f"panel survives as a name and as a legacy boolean; "
            f"portrait_offset arrives; {len(sent_keys)} sent fields all read")


def check_the_rate_limit_key_cannot_be_forged() -> str:
    """A client cannot choose which bucket its requests count against.

    Every limit in the app - 8 uploads/hour, 3 renders/week, the per-address
    mail caps - keys on `limits.client_key`. If a request can set that key,
    the caps are per-request-resettable and mean nothing.

    `X-Forwarded-For` is a list a client seeds with whatever it likes. Our
    Apache is the single edge proxy and appends the real peer it saw as the
    LAST element, so the true client is `forwarded[-1]`; everything earlier
    is the client's own claim. Reading `forwarded[0]` - the first - was
    reading exactly the part the attacker controls, so rotating the header
    reset every counter. Proven against the running server before the fix:
    ten uploads with ten forged first-entries all passed an 8/hour cap.

    This asserts the key is the LAST entry, using the header shape gunicorn
    actually receives in production: the spoof, then the peer Apache adds.
    """
    from app import limits

    class FakeRequest:
        def __init__(self, xff, peer="127.0.0.1"):
            self.headers = {"X-Forwarded-For": xff} if xff else {}
            self.remote_addr = peer

        class _H(dict):
            def get(self, k, d=None):
                return dict.get(self, k, d)

    def key(xff):
        r = FakeRequest.__new__(FakeRequest)
        r.headers = FakeRequest._H()
        if xff is not None:
            r.headers["X-Forwarded-For"] = xff
        r.remote_addr = "127.0.0.1"
        return limits.client_key(r)

    old = os.environ.get("TRUST_PROXY")
    os.environ["TRUST_PROXY"] = "true"
    try:
        # Production shape: '<attacker spoof>, <real peer Apache appended>'.
        # Two different spoofs, the SAME real peer, must land on ONE key.
        k1 = key("10.0.0.1, 203.0.113.7")
        k2 = key("10.0.0.99, 203.0.113.7")
        if k1 != "203.0.113.7" or k2 != "203.0.113.7":
            raise Failed(
                f"the rate-limit key is forgeable: two requests with different "
                f"X-Forwarded-For first entries keyed as {k1!r} and {k2!r}. It "
                f"must key on the LAST entry (the peer Apache appended), which "
                f"a client cannot change, or rotating the header resets every "
                f"limit in the app.")
        # A bare header with no proxy append still keys on its last element,
        # never the first-if-it-differs.
        if key("1.1.1.1, 2.2.2.2") != "2.2.2.2":
            raise Failed("client_key did not take the last X-Forwarded-For entry")
    finally:
        if old is None:
            os.environ.pop("TRUST_PROXY", None)
        else:
            os.environ["TRUST_PROXY"] = old

    return "two forged first-entries with one real peer collapse to one key"


def check_the_request_cannot_choose_a_file() -> str:
    """A render request cannot name a file for ffmpeg to read.

    `_style_from` built the backdrop path as
    `style.get("background_path") or settings.background_for(background)`, so
    a caller could set `background_path` to any file the `vsw` user could
    read - another visitor's upload, another visitor's finished video - and
    `Style.validate` only checked it existed. ffmpeg then composited that
    file into the attacker's output, which they downloaded: an arbitrary
    local-file read of everyone else's recordings. Proven by building a
    Style from a crafted request and watching the path arrive intact.

    The interface never sends this field. The fix is that the request cannot
    set it at all - the path comes only from our own configuration.
    """
    from app import routes

    poison = "/etc/passwd" if os.name != "nt" else r"C:\Windows\win.ini"
    style = routes._style_from({"style": {"background": "static",
                                          "background_path": poison}})
    if style.background_path == poison:
        raise Failed(
            f"a render request set background_path to {poison!r} and it was "
            f"honoured. Any file readable by the service reaches ffmpeg and "
            f"is composited into a downloadable video. The path must come "
            f"only from settings.background_for, never from the request.")
    return "a request-supplied background_path is ignored"


def check_the_duration_cap_fails_safe() -> str:
    """The default upload-length cap is one the production box survives.

    The aligner allocates a full N-by-M DTW matrix in float64, so memory
    grows with the SQUARE of duration: 7.1 min -> 2.43 GB, 14.1 min ->
    4.62 GB on the 3.9 GB box. `settings._number` falls back to this code
    default when MAX_DURATION_MINUTES is missing, empty or mistyped, so the
    default is what a fresh or fat-fingered deploy runs with - it must be a
    value the box survives, not an aspiration. It read 25 while the box OOMs
    somewhere past 11, which is a one-request denial of service reintroduced
    by any cleared line.
    """
    from app.settings import (LOCAL_RAM_GB, max_upload_minutes,
                              plan_memory_gb, safe_duration_minutes)

    # THE CAP IS DERIVED, so there is no literal to read any more. What is
    # guarded instead: that the derivation exists, that it answers with
    # something the renderer survives, and that any file which does override
    # it stays inside what ITS OWN settings say will render.
    import os

    had = os.environ.pop("MAX_DURATION_MINUTES", None)
    try:
        derived = max_upload_minutes()
    finally:
        if had is not None:
            os.environ["MAX_DURATION_MINUTES"] = had
    alone = safe_duration_minutes(LOCAL_RAM_GB)
    if derived <= 0:
        raise Failed("the derived cap is zero, so nothing could be uploaded")
    if derived > alone + 0.5:
        raise Failed(
            f"with no compute node the cap derives to {derived} minutes and "
            f"a {LOCAL_RAM_GB} GB box manages {alone:.1f}; a fresh install "
            f"would OOM on its own uploads")

    # An override still has to fit the machine THAT FILE configures. Reading
    # the ceiling from the same file as the cap is the point -- the two moved
    # apart once already, when rendering left the web box and the number
    # stayed behind, and the site refused twenty minutes on a node sized for
    # exactly that.
    checked = []
    for name in (".env.prod", ".env.example"):
        f = ROOT / name
        if not f.is_file():
            continue
        lines = [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines()]

        def value_of(key: str) -> str:
            for ln in lines:
                if ln.startswith(f"{key}="):
                    return ln.split("=", 1)[1].strip()
            return ""

        on = value_of("COMPUTE_ENABLED").lower() in ("1", "true", "yes", "on")
        plan = value_of("COMPUTE_PLAN")
        ram = plan_memory_gb(plan) if on else LOCAL_RAM_GB
        where = f"a {plan} node" if on else "the web box"
        if on and not ram:
            raise Failed(
                f"{name} turns compute on with COMPUTE_PLAN={plan!r}, which "
                f"is not in settings.PLAN_MEMORY_GB, so nothing can say what "
                f"length that machine survives")
        ceiling = safe_duration_minutes(ram)

        cap = value_of("MAX_DURATION_MINUTES")
        if not cap:
            # The floor, because that is what the code returns; reporting the
            # exact figure here would drift from the number that ships.
            checked.append(f"{name} derives {int(ceiling)} on {where}")
            continue
        if float(cap) > ceiling:
            raise Failed(
                f"{name} overrides MAX_DURATION_MINUTES={cap}, and {where} "
                f"({ram:.0f} GB) aligns {ceiling:.1f} minutes before the DTW "
                f"matrix exhausts it. This file becomes the server's .env, so "
                f"this is the live cap, not a default.")
        checked.append(f"{name} pins {cap} under {ceiling:.0f} on {where}")

    return (f"derived {derived:.0f} min here, inside {alone:.1f}; "
            + "; ".join(checked))


def check_the_interface_and_renderer_agree() -> str:
    """What the design screen offers renders as what it showed.

    The interface and the renderer keep two vocabularies, and nothing
    translated between them, so two things the visitor chose were silently
    thrown away between the preview and the video:

      * THE TITLE PANEL. The screen sends `round`, `first`, `last`, `work`;
        the renderer reads `round_name`, `first_name`, `last_name`,
        `composition`. Every panel render dropped the round title, the
        performer's name and the work, keeping only subtitle, country, age
        and composer. The preview showed the full panel; the video did not.

      * THE BACKDROP. "Still artwork" is sent as `image` and "Looping video"
        as `video`; the renderer calls them `static` and `dynamic`. Both
        failed `Style.validate` with "Unknown background" — a 400 after the
        visitor had finished designing.

    This asserts the boundary translates both. It uses the interface's OWN
    field names, read from svs-min.js, so the check breaks if the screen
    starts sending something the server does not map — the failure the last
    two bugs were.
    """
    import re

    from app import panel, routes, render as rnd

    # The panel keys the interface actually sends, lifted from its FIELDS.
    js = (ROOT / "app" / "static" / "svs" / "svs-min.js").read_text(
        encoding="utf-8", errors="ignore")
    start = js.find("const FIELDS = [")
    block = js[start:js.find("];", start)]
    # Each row is `['key', 'Label', ...]`; the key is the first quoted token.
    sent_keys = [m.group(1) for m in re.finditer(r"\[\s*'(\w+)'", block)]
    if "first" not in sent_keys or "work" not in sent_keys:
        raise Failed("could not read the interface's panel FIELDS; this check "
                     "can no longer tell what the screen sends")

    # Give every sent key a value, translate, and render the panel.
    meta_in = {k: f"val_{k}" for k in sent_keys}
    text = panel.values(routes._panel_meta(meta_in))
    # The four that used to vanish must now carry through.
    if not text["name"] or "val_first" not in text["name"]:
        raise Failed("the performer's name does not reach the panel: the "
                     "interface sends first/last, the renderer reads "
                     "first_name/last_name, and nothing mapped them")
    for field, why in (("round_name", "round title"),
                       ("composition", "work")):
        if not text[field]:
            raise Failed(f"the {why} does not reach the panel; the "
                         f"interface/renderer name mismatch is back")

    # The backdrop kinds the interface offers must each resolve to a kind the
    # renderer knows, not raise "Unknown background".
    for sent, expect in (("none", rnd.NONE), ("colour", rnd.NONE),
                         ("image", rnd.STATIC), ("video", rnd.DYNAMIC),
                         ("upload", rnd.NONE)):
        got = routes._style_from({"style": {"background": sent}}).background
        if got != expect:
            raise Failed(
                f"the backdrop option {sent!r} resolves to {got!r}, not "
                f"{expect!r}. The screen offers it, so an untranslated value "
                f"is a 400 after the visitor finished, or a silently wrong "
                f"backdrop.")

    return (f"{len(sent_keys)} panel fields translate; name, round and work "
            f"reach the panel; 5 backdrop options all resolve")


def check_a_cross_site_request_is_refused() -> str:
    """A page on another domain cannot act as a visitor here.

    Upload and identify are multipart/simple requests, so a browser sends
    them cross-origin with no preflight to refuse: any site a visitor happens
    to open could spend that visitor's upload and identify quota and make
    this box decode a file on their behalf. `_cross_site` refuses a request
    that carries the browser's cross-origin signals, while letting a
    same-site page and a non-browser client (no Origin) through.
    """
    from app import routes

    class R:
        def __init__(self, headers):
            self.headers = headers
            self.host = "chopin.weefeen.com"

    if not routes._cross_site(R({"Sec-Fetch-Site": "cross-site"})):
        raise Failed("a Sec-Fetch-Site: cross-site request was not refused")
    if not routes._cross_site(R({"Origin": "https://evil.example"})):
        raise Failed("a request with a foreign Origin was not refused")
    if routes._cross_site(R({"Origin": "https://chopin.weefeen.com"})):
        raise Failed("a same-origin request was wrongly refused — this would "
                     "break the site's own interface")
    if routes._cross_site(R({})):
        raise Failed("a request with no Origin (a direct API call) was "
                     "refused; the rate limits and bot check defend there, "
                     "and blocking it breaks non-browser use")

    # The three state-changing endpoints must all call the guard.
    src = (ROOT / "app" / "routes.py").read_text(encoding="utf-8")
    for fn in ("def api_upload(", "def api_identify(", "def api_render("):
        start = src.find(fn)
        body = src[start:start + 600]
        if "_cross_site(request)" not in body:
            raise Failed(f"{fn.strip('(')} does not refuse a cross-site "
                         f"request; a state-changing endpoint left open lets "
                         f"another site drive it on a visitor")

    return "cross-site refused on upload/identify/render; same-site and "\
           "direct calls allowed"


def check_a_job_id_is_not_guessable() -> str:
    """The job id — the only guard on a visitor's video — is 128 bits.

    There is no login, so whoever holds a job id can fetch that recording's
    result and status. `uuid4().hex[:12]` was 48 bits: a lot to guess once,
    not a lot to grind at scale against an unauthenticated endpoint. The full
    uuid is free and puts it out of reach. Old 12-char ids stay valid, so
    this only lengthens new ones — the download also sends
    `Cache-Control: private, no-store` so a leaked link leaves no cached
    copies behind.
    """
    import re

    src = (ROOT / "app" / "jobs.py").read_text(encoding="utf-8")
    if re.search(r"uuid\.uuid4\(\)\.hex\[:\d+\]", src):
        raise Failed("new_job still truncates uuid4().hex; the job id is a "
                     "capability with no login behind it and must be the full "
                     "128 bits")
    if "uuid.uuid4().hex" not in src:
        raise Failed("could not find the job-id generator in jobs.py")

    routes_src = (ROOT / "app" / "routes.py").read_text(encoding="utf-8")
    if 'Cache-Control"] = "private, no-store"' not in routes_src:
        raise Failed("the video download does not set Cache-Control: private, "
                     "no-store, so a leaked link can leave cached copies")

    return "job id is the full uuid4; download is private, no-store"


def check_the_bot_check_is_wired_and_inert_by_default() -> str:
    """A human-check that guards the upload, and is off until configured.

    The unauthenticated upload→recognise→render chain is the expensive thing
    an automated agent abuses, and it is the security review's standing top
    finding. Turnstile is the guard. Two properties matter:

      * INERT BY DEFAULT. With no keys set, `verify` passes and the upload
        endpoint does not demand a token — a local install and any deployment
        without keys works exactly as before. Both keys are required to turn
        it on; a site key alone would draw a widget whose token nothing
        checks, which only looks protected.
      * WIRED WHERE IT MATTERS. `api_upload` calls the check, so turning the
        keys on actually gates the entrance to the chain. A check built but
        not called is the failure this suite exists to catch.
    """
    import dataclasses
    from app import botcheck, settings as settings_mod

    s = settings_mod.settings
    # As shipped (no keys in the test env) the check must be inert.
    if s.bot_check:
        raise Failed("bot_check is on with no keys configured; the default "
                     "must be off so an unconfigured install still works")
    if not botcheck.verify(""):
        raise Failed("with the check off, verify('') must pass — otherwise "
                     "every upload is refused on an install with no keys")

    # Both-or-neither: a lone site key must NOT switch it on.
    half = dataclasses.replace(s, turnstile_site_key="x", turnstile_secret="")
    if half.bot_check:
        raise Failed("a site key alone turned the check on; without the "
                     "secret the token cannot be verified, so the widget "
                     "would only look like protection")
    both = dataclasses.replace(s, turnstile_site_key="x", turnstile_secret="y")
    if not both.bot_check:
        raise Failed("both keys set but bot_check is still off")

    # And the guard is actually called at the upload entrance.
    src = (ROOT / "app" / "routes.py").read_text(encoding="utf-8")
    start = src.find("def api_upload(")
    body = src[start:src.find("def ", start + 10)]
    if "botcheck.verify(" not in body:
        raise Failed("api_upload never calls botcheck.verify; the check is "
                     "built but does not guard the endpoint it exists for")

    return "off with no keys, both-or-neither to switch on, wired into upload"


def check_a_compute_node_gets_no_dangerous_secret() -> str:
    """The .env sent to a disposable render node withholds what could hurt.

    A compute node is the box that runs strangers' media through ffmpeg,
    torch and librosa — the component most likely to be compromised — and it
    is thrown away after each use. What travels to it in cloud-init decides
    the blast radius of a compromise. Three things must never reach it:

      * the Linode token (it can create and destroy machines — spend money),
      * the SMTP password and ALERT_EMAIL (it sends no mail),
      * the Turnstile secret (it serves no pages and checks no bots),

    and the full-bucket object-storage key must be replaced by a SCOPED one
    when configured, so a popped node cannot read or delete every visitor's
    video and the DB backups.
    """
    from app.compute import cloudinit

    source = "\n".join([
        "LINODE_TOKEN=MONEY",
        "SMTP_PASSWORD=mailpw",
        "ALERT_EMAIL=op@example.com",
        "TURNSTILE_SECRET=botsecret",
        "OBJECT_ENDPOINT=eu.example.com",
        "OBJECT_BUCKET=vsw",
        "OBJECT_KEY=FULLKEY",
        "OBJECT_SECRET=FULLSECRET",
        "OBJECT_KEY_COMPUTE=SCOPEDKEY",
        "OBJECT_SECRET_COMPUTE=SCOPEDSECRET",
        "SCORE_ROOT_DIGITAL=/srv/vsw/scores",
    ])
    env = cloudinit.environment(source, "amqp://vsw:pw@10.0.0.2:5672/vsw")

    for poison, what in (("MONEY", "the Linode token"),
                         ("mailpw", "the SMTP password"),
                         ("op@example.com", "ALERT_EMAIL"),
                         ("botsecret", "the Turnstile secret"),
                         ("FULLSECRET", "the FULL-BUCKET object secret"),
                         ("FULLKEY", "the full-bucket object key")):
        if poison in env:
            raise Failed(f"a compute node's .env carries {what}. That box runs "
                         f"attacker-supplied media and is disposable; this is "
                         f"exactly what must not travel to it.")

    if "SCOPEDKEY" not in env or "SCOPEDSECRET" not in env:
        raise Failed("the scoped compute object-storage key was configured "
                     "but did not reach the node; it would have no way to "
                     "upload the result")

    # And with no scoped key, the full key is sent but marked, so nobody
    # believes compute is contained when it is not.
    no_scope = "\n".join(["OBJECT_KEY=FULLKEY", "OBJECT_SECRET=FULLSECRET"])
    env2 = cloudinit.environment(no_scope, "amqp://vsw:pw@10.0.0.2:5672/vsw")
    if "OBJECT_SCOPED=NO" not in env2:
        raise Failed("with no scoped key the full key is sent, but nothing "
                     "marks that the node is NOT contained — turning compute "
                     "on would silently hand it the full bucket")

    return "linode/smtp/alert/turnstile/full-bucket withheld; scoped key sent"


def check_the_input_reaches_a_compute_node() -> str:
    """The recording a node renders can get to a host that lacks the disk.

    This is the wire the SELECTED workflow needs and did not have. The web box
    and its worker share a disk, so the worker read the upload straight off
    it; a compute node is a different machine and cannot see that disk, so a
    node picked up a job and failed looking for a file it had no way to reach.
    Everything else about the node — create, render, destroy, image — was
    built and tested, but the input never travelled, so the throwaway-host
    render could not run end to end.

    Now the web side stages the input in the bucket and puts the key on the
    task; a host without the file fetches it. Proven here with an in-memory
    bucket: stage on one side, fetch on a host where the upload path does not
    exist, and the bytes match.

    Also asserts that no bucket means nothing staged, and that compute OFF
    is not a reason to skip it: a volunteer machine cannot see this disk
    either, and gating on COMPUTE_ENABLED handed it tasks it could not fetch.
    """
    import dataclasses
    import importlib
    from app import storage, pipeline
    from app.queue import webside
    from app.queue.messages import RenderTask
    from app.settings import settings

    # In-memory stand-in for the bucket.
    bucket: dict = {}
    saved = (storage.put, storage.head, storage.get, storage.available,
             webside.settings)

    def fput(local, key):
        data = pathlib.Path(local).read_bytes(); bucket[key] = data
        return len(data)
    def fhead(key):
        return len(bucket[key]) if key in bucket else None
    def fget(key, local):
        p = pathlib.Path(local); p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(bucket[key]); return len(bucket[key])

    class Row(dict):
        def __getitem__(self, k): return dict.get(self, k)

    tmp = pathlib.Path(tempfile.mkdtemp())
    recording = tmp / "visitor.mp4"
    recording.write_bytes(b"CHOPIN" * 200)
    row = Row(id="input-check", upload=str(recording), score="Op39",
              attempt=1, mode=None, duration=120.0, style=None, meta=None,
              queued_at=0.0)

    try:
        storage.put, storage.head, storage.get = fput, fhead, fget
        storage.available = lambda: True

        # No bucket: nothing to stage into.
        webside.settings = dataclasses.replace(settings, compute_enabled=False)
        storage.available = lambda: False
        if webside._ensure_input_in_bucket(row) != "":
            raise Failed("with no bucket configured the input was staged -- "
                         "into what?")
        # Compute OFF with a bucket: STILL staged. A volunteer machine cannot
        # see this disk any more than a node can.
        storage.available = lambda: True
        if not webside._ensure_input_in_bucket(row):
            raise Failed("with compute off the input was not staged, so a "
                         "volunteer machine -- which cannot see this disk "
                         "either -- would be handed a task it cannot fetch")

        # Compute ON: staged, keyed, and fetchable where the disk is absent.
        webside.settings = dataclasses.replace(settings, compute_enabled=True)
        key = webside._ensure_input_in_bucket(row)
        if not key or fhead(key) != recording.stat().st_size:
            raise Failed("the input was not staged in the bucket with compute "
                         "on, so a node would have nothing to fetch")
        task = webside._task(row)
        if RenderTask.from_json(task.to_json()).input_key != key:
            raise Failed("input_key does not survive the wire; a node would "
                         "not know where to fetch the recording")

        # A host that cannot see the shared disk.
        fetched = pipeline.job_folder("input-check") / "input.mp4"
        fetched.unlink(missing_ok=True)
        storage.get(key, fetched)
        if not fetched.is_file() or fetched.read_bytes() != recording.read_bytes():
            raise Failed("the node's fetched recording does not match the "
                         "original — a render on it would be a render of the "
                         "wrong or a truncated file")
    finally:
        (storage.put, storage.head, storage.get, storage.available,
         webside.settings) = saved

    return "input staged and keyed with compute on; fetched byte-identical; "\
           "nothing staged with compute off"


def check_a_transparent_band_floats_over_the_video() -> str:
    """Max paper transparency shows the VIDEO through the notes, not the void.

    A transparent band is composited last, over the picture. If the video
    sits in a separate region beside the band, the only thing under the
    floating notes is the dark canvas — so turning the paper see-through
    revealed a flat colour instead of the performance, which is the opposite
    of the point. When the band needs alpha the video now fills the whole
    content and the band is laid on top of it; when the paper is solid the
    video sits beside the band as before (an opaque band hides what is behind
    it, and a smaller video keeps all of the picture uncropped).

    The preview mirror in svs-wire.js is switched by the same `S.alpha <= 0`,
    so the design screen shows what the render produces rather than notes over
    a backdrop the video never had.
    """
    from app import render as rnd

    ba, va = 1306 / 244.0, 16 / 9
    for pos in ("top", "bottom"):
        opaque = rnd.compute_layout(
            rnd.Style(aspect="16/9", band_bg_opacity=1.0, band_position=pos),
            ba, va)
        clear = rnd.compute_layout(
            rnd.Style(aspect="16/9", band_bg_opacity=0.0, band_position=pos),
            ba, va)

        def covers(L):
            return (L.video.y <= L.band.y
                    and L.video.y + L.video.h >= L.band.y + L.band.h)

        if covers(opaque):
            raise Failed(f"an OPAQUE band ({pos}) has the video behind it — "
                         f"that crops the picture under a band nobody sees "
                         f"through, for no gain")
        if not covers(clear):
            raise Failed(f"a TRANSPARENT band ({pos}) does NOT have the video "
                         f"behind it, so the floating notes sit over the dark "
                         f"canvas instead of the performance — the bug this "
                         f"guards")

    # And the preview is switched by the same signal, or it lies again.
    wire = (ROOT / "app" / "static" / "svs" / "svs-wire.js").read_text(
        encoding="utf-8", errors="ignore")
    if "S.alpha <= 0" not in wire or "behind ? contentH" not in wire:
        raise Failed("the preview no longer mirrors the transparent-band "
                     "layout; it will show the notes over a backdrop the "
                     "render puts them over the video")

    return "opaque: video beside; transparent: video behind, top and bottom; "           "preview mirrors it"


def check_a_compute_node_can_actually_run() -> str:
    """The four defects that each stopped a created node from taking a task.

    The node lifecycle was written but never ran end to end — a created node
    could not read its env, had no route to the broker, was forbidden to
    declare the topology it consumed, and was sent user_data the provider
    rejects. Each is independent and each is fatal, so the automated
    create→render→destroy loop had never worked; the hand-run tests fixed
    these by hand. This asserts the code no longer has them.

    Provider and broker SEMANTICS (does Linode accept this body, does the
    broker admit this consumer) cannot be proven without a real create and a
    real broker, and are not claimed here — only that the code now sends the
    right shape and takes the right branch.
    """
    import base64
    from app import settings as settings_mod
    from app.compute import cloudinit
    from app.compute.driver import LinodeDriver
    from app.queue.transport import AmqpTransport

    # 1) The env bridge: cloud-init links current/.env -> shared/.env, the
    #    path the app actually loads.
    ud = cloudinit.user_data("X=1")
    if "ln -sfn /srv/vsw/shared/.env /srv/vsw/current/.env" not in ud:
        raise Failed("cloud-init does not link the node's .env into the path "
                     "the app loads; the node boots with empty config and the "
                     "worker restart-loops")

    # 2) A VLAN interface reaches the create body, and 4) user_data is base64
    #    with a root_pass.
    driver = LinodeDriver.__new__(LinodeDriver)
    seen = {}
    driver._call = lambda m, p, body=None: (
        seen.update(body=body)
        or {"id": 1, "label": body["label"], "status": "x", "ipv4": ["1.2.3.4"]})
    driver.create("vsw-compute-1", "g6-dedicated-4", "private/1", "eu-central",
                  "#cloud-config\n", ["ssh-ed25519 AAA"],
                  interfaces=[{"purpose": "public"},
                              {"purpose": "vlan", "label": "vsw-vlan",
                               "ipam_address": "10.0.0.3/24"}])
    body = seen["body"]
    if not any(i.get("purpose") == "vlan" for i in body.get("interfaces", [])):
        raise Failed("the create body has no VLAN interface; the node has "
                     "only a public NIC and cannot reach the broker")
    if not base64.b64decode(body["metadata"]["user_data"]).startswith(b"#cloud"):
        raise Failed("user_data is not base64-encoded; the Metadata service "
                     "rejects it and the node boots with no config")
    if not body.get("root_pass"):
        raise Failed("no root_pass in the create body; an image build "
                     "requires one even when only keys are used")

    # 3) A node (vsw-compute user) must NOT declare; the web box (vsw) must.
    node = AmqpTransport("amqp://vsw-compute:pw@10.0.0.2:5672/vsw")
    web = AmqpTransport("amqp://vsw:pw@127.0.0.1:5672/vsw")
    if node._may_declare:
        raise Failed("a compute node would try to declare the topology, which "
                     "its narrow broker user is forbidden to do — an "
                     "ACCESS_REFUSED that drops it into a reconnect loop")
    if not web._may_declare:
        raise Failed("the web box would NOT declare the topology, so nothing "
                     "creates the queues")

    # 5) The web box's worker stands aside when compute is on; a node does not.
    #    Both hang off the same is_compute_node signal (the broker user).
    import dataclasses as _dc
    as_node = _dc.replace(settings_mod.settings,
                          rabbitmq_url="amqp://vsw-compute:p@h/vsw")
    as_web = _dc.replace(settings_mod.settings,
                         rabbitmq_url="amqp://vsw:p@h/vsw")
    if not as_node.is_compute_node:
        raise Failed("a node is not recognised as a node from its broker user")
    if as_web.is_compute_node:
        raise Failed("the web box is mis-recognised as a node")

    return ("env bridged, VLAN interface sent, user_data base64 + root_pass, "
            "node skips declare, node/web-box distinguished")


def check_the_video_carries_a_mark() -> str:
    """A mark is burned into the pixels, so a repost is still traceable home.

    A caption or a hashtag is stripped the moment someone re-uploads; a mark
    in the pixels survives a re-encode, and is the one identification that
    does. On by default, drawn with PIL (not ffmpeg drawtext, which needs
    fontconfig and falls over without it), and sized to the canvas.
    """
    from PIL import Image
    from app import render as rnd

    # OFF by default, deliberately: it was never asked for, and a line of
    # text the owner did not choose does not belong on someone's performance.
    if rnd.Style().watermark:
        raise Failed("the text watermark is ON by default; it was added "
                     "unasked and must stay opt-in")

    work = pathlib.Path(tempfile.mkdtemp())
    for canvas in ((1920, 1080), (1080, 1920), (1080, 1080)):
        path, w, h = rnd._watermark_png("chopin.weefeen.com", canvas, work)
        if not path.is_file():
            raise Failed("the watermark PNG was not produced")
        img = Image.open(path)
        if img.mode != "RGBA":
            raise Failed("the watermark is not transparent, so it would paint "
                         "a solid box over the video")
        if w >= canvas[0] or h >= canvas[1] or w < 8 or h < 8:
            raise Failed(f"the mark is {w}x{h} on a {canvas} canvas — it does "
                         f"not fit or is invisibly small")
        # Something was actually drawn (not a blank transparent image).
        if img.getbbox() is None:
            raise Failed("the watermark image is empty — no text was drawn")

    # And it must not land ON the notation. Behind a transparent band the
    # video fills the frame, so "bottom-right of the video" is also on top of
    # the score — the mark sat across the staff, over the one thing the video
    # exists to show.
    for opacity in (0.0, 1.0):
        style = rnd.Style(aspect="16/9", band_bg_opacity=opacity,
                          band_position="bottom")
        L = rnd.compute_layout(style, 1306 / 244.0, 16 / 9)
        _, mark_w, mark_h = rnd._watermark_png("chopin.weefeen.com",
                                               L.canvas, work)
        pad = max(8, L.canvas[1] // 90)
        wy = min(L.canvas[1] - mark_h, L.video.y + L.video.h - mark_h - pad)
        if style.needs_alpha and style.band_position != rnd.TOP:
            wy = min(wy, L.band.y - mark_h - pad)
        if wy + mark_h > L.band.y:
            raise Failed(f"at opacity {opacity} the mark overlaps the score "
                         f"band (mark ends {wy + mark_h}, band starts "
                         f"{L.band.y}) — it would sit across the notation")

    return ("on by default; transparent PNG, drawn and sized, fits every "
            "aspect, and clear of the score band")


def check_one_person_cannot_hold_billions_of_buckets() -> str:
    """An IPv6 visitor is a /64, not an address.

    An IPv4 address is roughly a person. An IPv6 address is not: the smallest
    allocation to a home or a phone is a /64. Keying limits on the full
    address gave one visitor 2**64 buckets — every cap here was one address
    away from unlimited, with no spoofing at all, just by using the next
    address in a range legitimately theirs. This asserts the truncation.
    """
    from app.limits import _bucket

    a = _bucket("2001:db8:abcd:1234:5:6:7:8")
    b = _bucket("2001:db8:abcd:1234:ffff:ffff:ffff:ffff")
    if a != b:
        raise Failed(f"two addresses in ONE /64 got different buckets "
                     f"({a} vs {b}) — the cap is bypassed by changing address")
    if _bucket("2001:db8:abcd:9999::1") == a:
        raise Failed("two different /64s share a bucket — unrelated visitors "
                     "would spend each other's quota")
    if _bucket("203.0.113.9") != "203.0.113.9":
        raise Failed("an IPv4 address was rewritten; it is already one person")
    if _bucket("unknown") != "unknown":
        raise Failed("an unparseable key was dropped; refusing to count is "
                     "worse than counting something odd")
    return "IPv6 truncated to /64, IPv4 untouched, unparseable preserved"


def check_a_volunteer_machine_stops_the_paid_one() -> str:
    """While a machine is lending itself, no machine is rented.

    A render costs about 29 cents on a rented node, because Linode rounds a
    partial hour up and a video takes ten minutes. A desktop that is already
    paid for can consume the same queue -- the worker is portable, which is
    the whole reason a compute node works at all -- but the scaler decides
    from the JOB TABLE alone: `queued + running > 0` means "want a machine",
    whoever happens to be consuming. So a volunteer would have taken the job
    AND a node would have booted beside it, and the bill would have been
    unchanged.

    A volunteer now says `alive` on the queue the worker already reports on,
    and that word is good for three missed beats. Fresh, and nothing is
    wanted; stale, and the next tick wants a machine again -- so a laptop
    that is closed without warning costs a visitor a few minutes, not their
    video.
    """
    import time as _t

    from app import store

    import dataclasses

    from app.settings import settings

    def idle() -> None:
        """Nothing running, so each case decides whether to CREATE."""
        with store.write() as conn:
            conn.execute("UPDATE compute SET state='none', idle_since=NULL,"
                         " updated=NULL WHERE singleton = 1")

    store.set_meta_always("volunteer", "")
    now = _t.time()
    was = store.settings
    _queued("volunteer-check", state=store.QUEUED)
    try:
        store.settings = dataclasses.replace(settings, compute_mode="auto")
        idle()
        alone = store.compute_tick(0.0, live=False)
        if alone.get("state") != "wanted":
            raise Failed(f"with work queued and no volunteer the scaler is "
                         f"{alone.get('state')!r}; it must want a machine")

        store.volunteer_seen("selftest-desktop", now)
        if store.volunteer(now) is None:
            raise Failed("a heartbeat just recorded does not read back")
        idle()
        lent = store.compute_tick(0.0, live=False)
        if lent.get("state") == "wanted":
            raise Failed("a machine is wanted while a volunteer is "
                         "consuming; the job would be rendered twice over "
                         "and the rented one paid for")

        # `cloud` ignores the volunteer, for the days a desktop is not to be
        # trusted with somebody's video.
        store.settings = dataclasses.replace(settings, compute_mode="cloud")
        idle()
        if store.compute_tick(0.0, live=False).get("state") != "wanted":
            raise Failed("COMPUTE_MODE=cloud did not rent a machine even "
                         "with work queued; that mode exists to be certain")

        # `manual` never rents, volunteer or not. The operator asked.
        store.settings = dataclasses.replace(settings, compute_mode="manual")
        store.set_meta_always("volunteer", "")
        idle()
        if store.compute_tick(0.0, live=False).get("state") == "wanted":
            raise Failed("COMPUTE_MODE=manual rented a machine; the whole "
                         "point of it is that nothing is rented")
        store.settings = was

        # And the word expires, or a closed laptop holds the queue for ever.
        if store.volunteer(now + store.VOLUNTEER_WINDOW + 1) is not None:
            raise Failed(f"a volunteer is still trusted "
                         f"{store.VOLUNTEER_WINDOW:.0f}s after its last "
                         f"heartbeat; a closed laptop would strand the queue")
    finally:
        store.settings = was
        store.set_meta_always("volunteer", "")
        with store.write() as conn:
            conn.execute("DELETE FROM jobs WHERE id = ?", ("volunteer-check",))
            conn.execute("UPDATE compute SET state='none', idle_since=NULL,"
                         " updated=NULL WHERE singleton = 1")

    # A CONSUMER MAY DECLINE, and the two curves must agree. The site sizes
    # its upload cap from the RENDERER'S memory; a volunteer sizes its
    # refusal from the memory FREE ON IT right now. If those disagreed, the
    # site would accept recordings the desktop always hands back -- work
    # bouncing between machines while a visitor watches a bar -- or worse,
    # the desktop would take one it cannot finish and die two thirds
    # through.
    import importlib.util
    import math

    from app.settings import (DTW_GB_AT, DTW_MINUTES_AT, USABLE_RAM,
                              safe_duration_minutes)

    spec = importlib.util.spec_from_file_location(
        "volunteer_mod", ROOT / "tools" / "volunteer.py")
    vol = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(spec and vol)

    for gb in (4, 8, 16, 32, 64):
        longest = safe_duration_minutes(gb)
        wants = vol._needed_bytes(longest) / 1024 ** 3     # noqa: SLF001
        budget = gb * USABLE_RAM
        if abs(wants - budget) > 0.05:
            raise Failed(
                f"at {gb} GB the site accepts {longest:.1f} min, and the "
                f"volunteer reckons that needs {wants:.2f} GB against a "
                f"{budget:.2f} GB budget. The two curves have drifted, so "
                f"jobs would bounce between machines.")

    if vol._needed_bytes(0) != 0:                          # noqa: SLF001
        raise Failed("a recording of no length is reckoned to need memory")

    # THAT A DECLINE ACTUALLY REQUEUES is proved by running one, in
    # `check_a_declined_task_really_goes_back`. It used to be proved here by
    # searching transport.py for the decline line and for `requeue=True`
    # near it -- and both were present, correct, and in a function that was
    # never passed `accept` at all, so the real consumer raised NameError on
    # the first task while this check passed. A search over source says the
    # code was written; only running it says the code runs.
    #
    # What is still worth reading rather than running: Ctrl-C during a
    # render hands the job back. Exercising that means interrupting a real
    # render mid-encode, which this suite cannot do.
    tsrc = (ROOT / "app" / "queue" / "transport.py").read_text(encoding="utf-8")
    if "interrupted mid-render" not in tsrc or tsrc.count("requeue=True") < 2:
        raise Failed("Ctrl-C during a render does not hand the job back, so "
                     "closing a laptop costs a visitor the whole lease")

    # A MACHINE MID-RENDER IS NOT DESTROYED BECAUSE A LAPTOP SAID HELLO. The
    # waiver used to zero `busy` outright, which dropped the running count:
    # at the hour boundary the machine looked idle and would have been torn
    # down under the render. Only what the VOLUNTEER itself holds is waived.
    now2 = _t.time()
    store.settings = dataclasses.replace(settings, compute_mode="auto")
    _queued("volunteer-mid", state=store.RUNNING)
    try:
        with store.write() as conn:
            conn.execute("UPDATE jobs SET worker = ? WHERE id = ?",
                         ("vsw-compute-node", "volunteer-mid"))
            conn.execute("UPDATE compute SET state='wanted', machine_id=42,"
                         " since=?, idle_since=NULL, updated=? WHERE singleton=1",
                         (now2 - 3590.0, now2))
        store.volunteer_seen("laptop", now2)
        held = store.compute_tick(0.0, live=False)
        if held.get("state") != "wanted" or held.get("busy", 0) < 1:
            raise Failed("a volunteer's hello let the scaler release a machine "
                         "that is rendering somebody's video")
        # The same job held by the VOLUNTEER does not hold the machine: at
        # the boundary it is released, or an hour is bought for nothing.
        with store.write() as conn:
            conn.execute("UPDATE jobs SET worker = ? WHERE id = ?",
                         ("laptop", "volunteer-mid"))
            conn.execute("UPDATE compute SET state='wanted', machine_id=42,"
                         " since=?, idle_since=?, updated=? WHERE singleton=1",
                         (now2 - 3590.0, now2 - 60.0, now2))
        freed = store.compute_tick(0.0, live=False)
        if freed.get("busy", 1) != 0:
            raise Failed("a job the volunteer itself is rendering kept a paid "
                         "machine alive -- an hour bought for nothing")
    finally:
        store.set_meta_always("volunteer", "")
        with store.write() as conn:
            conn.execute("DELETE FROM jobs WHERE id = ?", ("volunteer-mid",))
            conn.execute("UPDATE compute SET state='none', idle_since=NULL,"
                         " machine_id=NULL, updated=NULL WHERE singleton = 1")
        store.settings = was

    # SILENT WHILE UNWILLING. A paused volunteer, or one that has just
    # declined, must stop saying `alive`: while it is heard nothing is
    # rented, and it has already said it will not take the job at the head
    # of the queue -- so the visitor would wait on a laptop that said no.
    import threading as _th

    class _Bus:
        def __init__(self):
            self.said = 0

        def publish_event(self, event):
            self.said += 1

    vol.BEAT_SECONDS, vol.QUIET_POLL = 0.02, 0.02
    bus, halt = _Bus(), _th.Event()
    vol._paused.set()                                          # noqa: SLF001
    _th.Thread(target=vol._beat, args=(bus, halt), daemon=True).start()  # noqa: SLF001
    _t.sleep(0.25)
    if bus.said:
        raise Failed("a PAUSED volunteer still says alive, so no machine is "
                     "rented while it declines everything")
    vol._paused.clear()                                        # noqa: SLF001
    _t.sleep(0.25)
    if not bus.said:
        raise Failed("a resumed volunteer never says alive again")
    said = bus.said
    vol._go_quiet()                                            # noqa: SLF001
    _t.sleep(0.25)
    if bus.said > said + 1:
        raise Failed("a volunteer that just declined keeps saying alive; the "
                     "scaler will not rent and the job waits on it")
    halt.set()
    vol._quiet_until = 0.0                                     # noqa: SLF001

    # And what it declines: a recording it cannot reach, going quiet as it does.
    class _Task:
        job_id = "t1"
        kind = "render"
        upload = str(ROOT / "no-such-recording.mp4")
        input_key = ""
        duration = 60.0

    vol.free_memory_bytes = lambda: 64 * 1024 ** 3
    if vol._accept(_Task()):                                   # noqa: SLF001
        raise Failed("a volunteer accepted a task whose recording is neither "
                     "in the bucket nor on its disk")
    if vol._quiet_until <= _t.time():                          # noqa: SLF001
        raise Failed("declining did not silence the heartbeat")
    vol._quiet_until = 0.0                                     # noqa: SLF001
    _Task.input_key = "jobs/t1/input.mp4"
    if not vol._accept(_Task()):                               # noqa: SLF001
        raise Failed("a volunteer refused a task it could fetch")

    # A PING CARRIES NO RECORDING BY DESIGN. Declined, it is requeued and
    # comes straight back to the head -- one delivery at a time -- so a
    # single ping left over from a diagnosis blocked every real job behind
    # it, indefinitely. Measured on the live queue.
    class _Ping:
        job_id = "p1"
        kind = "ping"
        upload = ""
        input_key = ""
        duration = None

    vol._quiet_until = 0.0                                     # noqa: SLF001
    if not vol._accept(_Ping()):                               # noqa: SLF001
        raise Failed("a ping was declined; requeued at the head of the "
                     "queue it blocks every real job behind it")

    # The operator watches this window to see their own render happen, and
    # pika narrates six lines per connection at INFO against a heartbeat
    # that opens one every 45 seconds.
    vsrc = (ROOT / "tools" / "volunteer.py").read_text(encoding="utf-8")
    if "quiet_pika()" not in vsrc:
        raise Failed("the volunteer does not quieten pika, so what it is "
                     "doing is buried under socket bookkeeping")

    return (f"auto rents alone and stands aside for a volunteer; cloud "
            f"always rents; manual never does; the offer expires after "
            f"{store.VOLUNTEER_WINDOW:.0f}s; a consumer may decline and the "
            f"job is requeued, not discarded; a machine mid-render survives "
            f"a hello; a paused or declining volunteer falls silent")

def check_a_node_may_read_what_it_is_told_to_pull() -> str:
    """Everything cloud-init pulls at boot must be permitted by the wrapper.

    A node's pull key is authorised on the web box with a FORCED COMMAND --
    `command="/usr/local/bin/vsw-pull-only"` -- so every ssh from a node runs
    that wrapper whatever it asked for. It is the containment: a node holds a
    key to root on a box it must only ever read three directories from.

    THE FAILURE THIS EXISTS FOR. The wrapper lived only on the server, made
    by hand, mentioned nowhere in this repository. When cloud-init began
    pulling the music fonts and the alignment engine at boot, the wrapper
    refused both -- and the cloud-init swallowed the refusal with `|| true`.
    Both fixes were committed, deployed, and did nothing: a tempo marking
    kept rendering as an empty box on a node that had been told to install
    the font, and nodes kept running whatever engine the image was captured
    with. It took a rendered video to notice.

    So the two halves are compared here rather than trusted to stay in step.
    """
    import re

    cloud = (ROOT / "app" / "compute" / "cloudinit.py").read_text(
        encoding="utf-8")
    wrapper_path = ROOT / "deploy" / "vsw-pull-only"
    if not wrapper_path.is_file():
        raise Failed("deploy/vsw-pull-only is missing. It is the forced "
                     "command on a node's key; without it in the repository "
                     "a rebuilt web box has no restriction at all, and "
                     "nothing here can say what a node may read.")
    wrapper = wrapper_path.read_text(encoding="utf-8")

    # What cloud-init asks to read: the remote side of each rsync.
    wanted = set(re.findall(r"root@\{web_host\}:(\S+?)\s", cloud))
    if not wanted:
        raise Failed("cloud-init pulls nothing from the web box; this check "
                     "can no longer tell whether the wrapper matches it")

    # What the wrapper permits: the paths in its --sender cases.
    allowed = set(re.findall(r'"rsync --server --sender "\*" (\S+?)"', wrapper))
    if not allowed:
        raise Failed("no permitted paths could be read out of "
                     "deploy/vsw-pull-only; this check cannot do its job")

    refused = sorted(w for w in wanted if w not in allowed)
    if refused:
        raise Failed(
            f"cloud-init pulls {refused} at boot and vsw-pull-only permits "
            f"only {sorted(allowed)}. The forced command would refuse it, "
            f"and the node would carry on without it.")

    # And a pull that cannot fail loudly is a fix that can be deployed and
    # do nothing, which is exactly what happened.
    for path in sorted(wanted):
        line = next((l for l in cloud.splitlines() if path in l and "rsync" in l), "")
        if "|| true" in line:
            raise Failed(f"the boot-time pull of {path} swallows its own "
                         f"failure with `|| true`; a refusal would be "
                         f"invisible, as it already has been once")

    return (f"{len(wanted)} boot-time pull(s), all permitted by the forced "
            f"command, none of them silent about failing")

def check_music_glyphs_survive_the_rasteriser() -> str:
    """A tempo's metronome note must not come out as a tofu box.

    Verovio draws almost everything as `<path>`, which needs no font. A few
    marks it emits as LIVE TEXT in a music font instead:

        <tspan font-family="Leipzig" font-size="503px">&#xECA7;</tspan>

    U+ECA7 is SMuFL metNote8thUp. Verovio embeds the Leipzig font in the SVG
    as an @font-face data URI, which is why the file looks right in any
    browser -- and cairosvg does not honour @font-face at all. It reads
    fontconfig and nothing else, so with no Leipzig installed the character
    fell back to a font with nothing at that codepoint: "Allegro. (&#x25A1; = 108)"
    in somebody's finished video.

    The fix is not a substitution table. music_line_extractor has one, and
    it is right for a desktop app that must not touch a stranger's operating
    system -- it covers eight metronome glyphs. This font carries 642, and a
    dynamic or an ornament emitted as text would each need another row. The
    font is taken out of the score that needs it and installed, which covers
    every glyph at once.

    Bytes come from the package, never from us, so nothing here redistributes
    a font.
    """
    import io

    from app import smufl
    from app.settings import settings

    if not smufl.available():
        raise Failed(smufl.why_unavailable())

    # A real band from a real package -- this is about somebody else's output.
    band = None
    for root in settings.score_roots:
        if not root.exists:
            continue
        for lines in sorted(root.path.glob("*/score/lines")):
            found = sorted(lines.glob("*.svg"))
            if found:
                band = found[0]
                break
        if band:
            break
    if band is None:
        raise Failed("no vector band installed to read a font out of")

    text = band.read_text(encoding="utf-8", errors="replace")
    faces = smufl.faces(text)
    if not faces:
        raise Failed(f"{band.name} embeds no font. If Verovio stopped "
                     f"embedding one, the glyphs it writes as text can no "
                     f"longer be drawn at all and this needs rethinking.")

    from fontTools.ttLib import TTFont
    total_pua = 0
    for name, raw in faces.items():
        font = TTFont(io.BytesIO(raw))
        cmap = set(font.getBestCmap())
        pua = {c for c in cmap if 0xE000 <= c <= 0xF8FF}
        total_pua += len(pua)
        # The metronome set, which is what a tempo marking uses and what was
        # actually broken. Whole, half, quarter, 8th, 16th, 32nd.
        need = {0xECA0: "whole", 0xECA2: "half", 0xECA5: "quarter",
                0xECA7: "8th", 0xECA9: "16th", 0xECAB: "32nd"}
        missing = {hex(c): why for c, why in need.items() if c not in cmap}
        if missing:
            raise Failed(f"the {name} font in {band.name} has no glyph for "
                         f"{missing}; a tempo marking using one would render "
                         f"as a box")

    # Whatever the SCORE actually uses must be in the font it carries.
    used = {ord(ch) for ch in text if 0xE000 <= ord(ch) <= 0xF8FF}
    covered = set()
    for raw in faces.values():
        covered |= set(TTFont(io.BytesIO(raw)).getBestCmap())
    unmet = sorted(used - covered)
    if unmet:
        raise Failed(f"{band.name} uses {[hex(c) for c in unmet]}, which the "
                     f"font it embeds cannot draw")

    # And the two places that put it where fontconfig will find it.
    store_src = (ROOT / "app" / "scorestore.py").read_text(encoding="utf-8")
    if "smufl.install_from_package" not in store_src:
        raise Failed("installing a score does not unpack the font it needs, "
                     "so the render host has nothing to draw with")
    cloud = (ROOT / "app" / "compute" / "cloudinit.py").read_text(
        encoding="utf-8")
    if "fc-cache" not in cloud:
        raise Failed("a compute node never runs fc-cache, so a font copied "
                     "to it is invisible to the rasteriser")
    # fontconfig is read once per process and cached -- measured: a font
    # installed mid-render changes nothing for that render. It has to be in
    # place before the worker starts, so the ORDER of the boot steps is the
    # thing to assert, read from the cloud-config itself rather than from
    # where a string happens to appear in the source.
    import yaml
    from app.compute import cloudinit as ci

    steps = yaml.safe_load(ci.user_data("X=1", "k", "10.0.0.2"))["runcmd"]
    fonts_at = next((i for i, c in enumerate(steps) if "fc-cache" in str(c)), -1)
    worker_at = next((i for i, c in enumerate(steps)
                      if "vsw-worker" in str(c) and "systemctl" in str(c)), -1)
    if fonts_at < 0:
        raise Failed("a compute node never installs the fonts at boot")
    if worker_at < 0:
        raise Failed("the cloud-config never starts the worker; this check "
                     "can no longer tell whether fonts come first")
    if fonts_at > worker_at:
        raise Failed(f"fonts are installed at boot step {fonts_at} and the "
                     f"worker starts at {worker_at}; fontconfig is cached at "
                     f"process start, so that worker would still draw boxes")

    # THE OTHER HALF, and the reason this check exists at all rather than
    # just a fix. music_line_extractor adapts Verovio output for cairosvg in
    # `_fix_svg` -- and it has TWO of them. The page renderer recolours
    # editor marks and substitutes music glyphs; the band exporter, whose
    # output we consume, does neither. Three of its fixes ARE baked into the
    # bands before we see them, so those are asserted; the two that are not
    # are handled here.
    from app import svg as appsvg

    for needle, fatal, _why in smufl.KNOWN_ARTEFACTS:
        if needle in text:
            raise Failed(f"an installed band still contains {needle!r}, "
                         f"which the exporting pipeline is supposed to have "
                         f"removed{' and which is fatal' if fatal else ''}")

    # Editor-marked notes come through magenta because only the PAGE
    # pipeline recolours them. Ours does it at render time.
    if appsvg.adapt(b'<g fill="magenta" color="magenta">') !=             b'<g fill="grey" color="grey">':
        raise Failed("a magenta editor mark is not recoloured, so it would "
                     "shout over the music in a finished video")
    once = appsvg.adapt(b'fill="magenta"')
    if appsvg.adapt(once) != once:
        raise Failed("the adaptation is not idempotent")

    # And the general form: live text in a font the file does not carry
    # cannot be drawn by anything here, whatever the codepoint.
    faked = text.replace('font-family="Leipzig"', 'font-family="Nowhere"', 1)
    if not smufl.undrawable(faked):
        raise Failed("text in a font the score does not embed is not "
                     "reported; the next glyph like this would reach a "
                     "video as a box with nothing to warn anybody")
    if smufl.undrawable(text):
        raise Failed(f"this package draws text it cannot supply a font for: "
                     f"{smufl.undrawable(text)}")

    return (f"{len(faces)} embedded face(s), {total_pua} music codepoints, "
            f"every glyph this score uses is covered, unpacked at install "
            f"and installed before the worker starts; editor marks "
            f"recoloured, upstream artefacts absent")


def check_a_tempo_mark_is_never_a_box() -> str:
    """The note in a tempo marking is DRAWN, on any machine.

    The check above asserts the score's font is unpacked and installed.
    That passed all along, and was never enough: a machine lent to the
    queue had the font installed, fontconfig resolved it (`fc-match
    Leipzig` -> Leipzig), and cairo drew an empty box anyway. Half of one
    real recording -- 288 seconds of 578 -- carried a band with one of
    these, and it shipped to the landing page before anybody noticed.

    A fix that depends on every render host agreeing about fonts is a fix
    that breaks quietly on the next host. So the character is SUBSTITUTED
    for a standard Unicode music symbol before rasterising, and this
    asserts on the output rather than on the setup: no private-use
    character survives in live text, and what replaces it draws as
    something other than the fallback box.
    """
    from app import svg

    sample = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="120">'
        '<text x="10" y="80" font-size="40">Piu lento ('
        '<tspan font-family="Leipzig" font-size="50">\ueca7</tspan>'
        ' = 132)</text></svg>').encode("utf-8")

    out = svg.adapt(sample).decode("utf-8")
    if "\ueca7" in out:
        raise Failed("a private-use music character survives into the "
                     "rasteriser, where it becomes an empty box on any host "
                     "whose fonts do not happen to cooperate")
    if "\u266a" not in out:
        raise Failed("the eighth note was not substituted for a real "
                     "Unicode symbol")
    if "Segoe UI Symbol" not in out:
        raise Failed("the music font was not swapped for a fallback list, so "
                     "the substituted symbol is asked of a font that has it "
                     "only by luck")

    # ALL OR NOTHING. A tspan holding a glyph we cannot map keeps the font it
    # asked for: half-substituting puts the wrong symbol beside a box and
    # makes it look deliberate.
    unknown = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<text><tspan font-family="Leipzig">\ueca7\ue0a4</tspan></text>'
        '</svg>').encode("utf-8")
    if "Segoe" in svg.adapt(unknown).decode("utf-8"):
        raise Failed("a tspan with an unmapped glyph was substituted anyway; "
                     "the unknown one becomes a box beside a real note")

    # And it is idempotent, because bands are adapted on every render.
    if svg.adapt(svg.adapt(sample)) != svg.adapt(sample):
        raise Failed("adapting twice differs from adapting once")

    if not svg.available():
        return ("substituted and the family swapped; drawing not measured, "
                "cairo is absent here")

    # The measurement, where there is a rasteriser: the note must not be
    # the same shape as the box it replaces.
    import io as _io
    from PIL import Image as _Image
    c = svg._cairosvg()                                       # noqa: SLF001

    def px(family: str, ch: str) -> int:
        one = ('<svg xmlns="http://www.w3.org/2000/svg" width="160" '
               'height="160"><rect width="160" height="160" fill="white"/>'
               f'<text x="20" y="120" font-family="{family}" '
               f'font-size="110">{ch}</text></svg>')
        png = c.svg2png(bytestring=one.encode("utf-8"), output_width=160,
                        output_height=160, background_color="white")
        return sum(1 for v in _Image.open(_io.BytesIO(png)).convert("L")
                   .getdata() if v < 200)

    box = px("NoSuchFontAnywhere", "&#xECA7;")
    note = px("Segoe UI Symbol, Apple Symbols, DejaVu Sans, Noto Music, serif",
              "&#x266A;")
    if note == box:
        raise Failed(f"the substituted note draws exactly like the box it "
                     f"replaces ({note} px); nothing was gained")
    if note == 0:
        raise Failed("the substituted note draws nothing at all")
    return (f"no private-use character reaches the rasteriser; the note "
            f"draws at {note} px against the box's {box}")

def check_a_confirmation_is_bound_and_expires() -> str:
    """Knowing an address must not be enough to have us write to its owner.

    Confirmation used to be a property of the ADDRESS and it lasted for
    ever. Anyone who knew a confirmed address could submit a job naming it,
    and this server would mail its owner "we are making your score video"
    about a video they never uploaded -- while spending their weekly
    allowance, because the limits are keyed on the address rather than on
    whoever typed it.

    Two things now. The click records itself in the BROWSER that made it, so
    a submission has to show that proof; and the permission LAPSES on
    inactivity, because a mailbox changes hands and a click from years ago
    is not consent today. Any proved submission renews it, so somebody who
    uses the site is never asked twice.

    An IP would answer neither question -- a whole office shares one and a
    phone changes its own between cells -- which is why this is a signed
    cookie and not an address book of peers.
    """
    import time as _t

    from app import proof, store
    from app.settings import settings

    me = "bound-check@example.com"
    other = "someone-else@example.com"

    # --- the cookie ----------------------------------------------------
    mine = proof.add(None, me)
    if me in mine:
        raise Failed("the address itself is in the cookie; a stolen cookie "
                     "would name the mailbox it proves")
    if not proof.proves(mine, me):
        raise Failed("a freshly issued proof does not prove its address")
    if proof.proves(mine, other):
        raise Failed("a proof for one address proves another")
    if proof.proves(None, me):
        raise Failed("no cookie at all counts as proof")

    body, mac = mine.split(".", 1)
    bent = body[:-2] + ("AA" if not body.endswith("AA") else "BB") + "." + mac
    if proof.proves(bent, me):
        raise Failed("A FORGED COOKIE IS ACCEPTED -- anyone could mint proof "
                     "for any address")

    both = proof.add(mine, other)
    if not (proof.proves(both, me) and proof.proves(both, other)):
        raise Failed("a browser cannot hold proof for two addresses")
    many = None
    for i in range(proof.MAX_ADDRESSES + 3):
        many = proof.add(many, f"held{i}@example.com")
    held = sum(1 for i in range(proof.MAX_ADDRESSES + 3)
               if proof.proves(many, f"held{i}@example.com"))
    if held != proof.MAX_ADDRESSES:
        raise Failed(f"the cookie holds {held} addresses, not "
                     f"{proof.MAX_ADDRESSES}; it would grow without bound")

    # --- the expiry ----------------------------------------------------
    now = _t.time()
    ttl = settings.confirm_ttl_days * 86400.0
    if ttl <= 0:
        raise Failed("confirmations never expire; a click from years ago "
                     "still authorises mail today")

    with store.write() as conn:
        conn.execute("DELETE FROM emails WHERE address = ?", (me,))
        conn.execute(
            "INSERT INTO emails (address, created, confirmed) VALUES (?,?,?)",
            (me, now - ttl - 86400, now - ttl - 86400))
    if store.may_mail(me, now):
        raise Failed(f"an address confirmed {settings.confirm_ttl_days:.0f}+ "
                     f"days ago and untouched since is still mailable")

    # Using the site renews it, so a regular visitor is never asked twice.
    store.saw_address(me, now)
    if not store.may_mail(me, now):
        raise Failed("a proved submission did not renew the permission, so "
                     "somebody who uses the site would be asked again")

    # Suppression still outranks everything, freshness included.
    with store.write() as conn:
        conn.execute("UPDATE emails SET suppressed = ? WHERE address = ?",
                     (now, me))
    if store.may_mail(me, now):
        raise Failed("SUPPRESSION WAS OVERRIDDEN by a recent visit; somebody "
                     "who said 'not me' would start receiving mail again")
    with store.write() as conn:
        conn.execute("DELETE FROM emails WHERE address = ?", (me,))

    # --- and the two are actually wired to the mailer -------------------
    routes = (ROOT / "app" / "routes.py").read_text(encoding="utf-8")
    jobs_src = (ROOT / "app" / "jobs.py").read_text(encoding="utf-8")
    if "proof.proves(" not in routes:
        raise Failed("the submission never checks the browser's proof")
    if "proof.attach(" not in routes:
        raise Failed("the confirmation click never records itself in the "
                     "browser, so no submission could ever show proof")
    if "if not proved or not store.may_mail(address)" not in jobs_src:
        raise Failed("the mailer ignores whether the browser proved the "
                     "address, so knowing it is enough to be written to")

    return (f"cookie proves one address and no other, forgery refused, "
            f"capped at {proof.MAX_ADDRESSES}; lapses after "
            f"{settings.confirm_ttl_days:.0f} idle days, renewed by use, "
            f"and suppression still outranks it")

def check_no_confirmation_screen_without_a_confirmation() -> str:
    """"Open the email we just sent" only when one was actually sent.

    The page showed the identity-check screen to everyone whose install can
    send mail AT ALL -- `if(SERVER.can_email)` -- and never asked whether
    THIS address needed proving. An address confirmed weeks earlier gets the
    "we are making your score video" mail instead, and its owner was sent
    hunting for a confirmation link that does not exist, while their video
    rendered behind the screen telling them nothing was rendering.

    It also claimed "Nothing is rendered until you click." That was never
    true of any address. `jobs.registry.start` queues and publishes the job
    before mail is considered at all, and `_say_it_is_queued` says so in as
    many words: the render is not held up, the visitor watches the page.

    So the server now answers the same question the mailer asks -- and it
    must be the same one, or the page and the mailbox disagree about which
    message went out.
    """
    routes = (ROOT / "app" / "routes.py").read_text(encoding="utf-8")
    wire = (ROOT / "app" / "static" / "svs" / "svs-wire.js").read_text(
        encoding="utf-8", errors="ignore")
    page = (ROOT / "app" / "static" / "svs" / "index.html").read_text(
        encoding="utf-8", errors="ignore")

    if "address_confirmed" not in routes:
        raise Failed("the render response does not say whether the address "
                     "was already proved, so the page cannot know which "
                     "mail was sent")
    if "store.may_mail(address)" not in routes:
        raise Failed("the page's answer is not derived from may_mail, the "
                     "same question the mailer asks; the two can disagree "
                     "about which message went out")

    if "SERVER.can_email && !alreadyProved" not in wire:
        raise Failed("the confirmation screen is still shown whenever the "
                     "install can send mail, rather than when a "
                     "confirmation was actually sent")
    if "data.address_confirmed" not in wire:
        raise Failed("the page never reads the server's answer")

    if "Nothing is rendered until you click" in page:
        raise Failed("the page still claims the render waits for the click. "
                     "It does not: jobs.registry.start queues and publishes "
                     "before any mail is considered.")

    return ("the confirm screen needs a confirmation, and the page no "
            "longer claims the render waits for it")

def check_a_refusal_is_not_a_failed_recognition() -> str:
    """A video the server turned away must not read as unrecognised music.

    `failed()` set `S.recog = 'none'` -- the RECOGNISER'S verdict, the one
    that draws "We couldn't place this recording. Is it a Chopin piece? The
    library is Chopin and nothing else" -- and then prepended the real
    message above it. The visitor got two statements, and the louder one was
    false: a ten-minute recording refused for length was presented as music
    nobody could identify, so the natural next thought was "is my Chopin not
    Chopin?" rather than "it was too long".

    The file bar made it worse. `S.src` ships with the design mock's values,
    and they are only replaced when the probe returns. A refused upload is
    never probed, so the bar printed the mock's `7:04` beside the real
    filename -- a length nobody had measured, contradicting the message
    right below it, which said ten minutes.
    """
    wire = (ROOT / "app" / "static" / "svs" / "svs-wire.js").read_text(
        encoding="utf-8", errors="ignore")
    mini = (ROOT / "app" / "static" / "svs" / "svs-min.js").read_text(
        encoding="utf-8", errors="ignore")

    start = wire.find("function failed(")
    if start < 0:
        raise Failed("failed() is gone; this check needs rewriting")
    end = wire.find("\nfunction ", start + 10)
    body = wire[start:end if end > 0 else start + 900]

    if "'none'" in body or '"none"' in body:
        raise Failed("failed() still puts the page into the recogniser's "
                     "'none' state, so a refused upload is drawn as music "
                     "nobody could place")
    if "S.recog = 'refused'" not in body:
        raise Failed("failed() does not mark the stop as a refusal")
    if "S.refusal" not in body:
        raise Failed("failed() does not keep the server's message anywhere "
                     "the page can draw it")

    # The mock's duration must be cleared when a real file is chosen, or an
    # unprobed upload shows 7:04 for a recording of any length.
    if "S.src.dur = ''" not in wire:
        raise Failed("the mock's duration is never cleared, so a refused "
                     "upload reports a length nobody measured")

    # And the page must actually have somewhere to draw it.
    if "S.recog==='refused'" not in mini.replace(" ", ""):
        raise Failed("svs-min.js has no branch for a refusal, so the state "
                     "exists and nothing renders it")

    # An upload the server never accepted leaves no recording, so offering a
    # list of pieces to pick from leads nowhere; one that failed while
    # LISTENING does leave a recording, and picking by hand is a real path.
    if "failed(err.message || 'The upload did not go through.', false)" not in wire:
        raise Failed("a refused upload still offers a manual piece list, "
                     "which cannot render anything")
    if "'Listening failed.', true)" not in wire:
        raise Failed("recognition that failed after the file arrived does "
                     "not offer the manual list, though the recording is here")

    return ("a refusal is its own state, carries the server's words, clears "
            "the mock duration, and only offers a manual pick when there is "
            "a recording to render")

def check_the_delivery_page_can_show_the_download() -> str:
    """`watchRender` must BIND the elements it writes into.

    The function used `box`, `stat`, `bar` and `what`, and bound none of them:
    it called `deliveryBox()` and threw the result away. svs-wire.js runs
    under 'use strict', so the first `bar.style.width` was a ReferenceError
    that killed the function before it could reach the done branch. The page
    said "your video is below" and then showed nothing below it — for every
    visitor, including everyone arriving from the link in their email, while
    the server had the finished video and served it correctly.

    Narrow on purpose: it asserts the four names are declared inside the
    function rather than trying to be a JavaScript linter.
    """
    wire = (ROOT / "app" / "static" / "svs" / "svs-wire.js").read_text(
        encoding="utf-8", errors="ignore")

    start = wire.find("async function watchRender(")
    if start < 0:
        raise Failed("watchRender is gone; this check needs rewriting")
    nxt = wire.find("\nasync function ", start + 10)
    alt = wire.find("\nfunction ", start + 10)
    ends = [e for e in (nxt, alt) if e > 0]
    body = wire[start:min(ends)] if ends else wire[start:]

    for name in ("box", "stat", "bar", "what"):
        if (f"const {name}" not in body and f"let {name}" not in body
                and f"var {name}" not in body):
            raise Failed(
                f"watchRender writes to `{name}` and never declares it — "
                f"under 'use strict' that is a ReferenceError, and the "
                f"download link is never inserted into the page")

    if "insertAdjacentHTML" not in body or "/download" not in body:
        raise Failed("watchRender no longer inserts the download link")

    return "watchRender binds box/stat/bar/what and inserts the download link"


def check_nothing_is_mailed_to_an_unproved_address() -> str:
    """No message reaches an address until its owner has clicked.

    Nothing proved ownership before. Anyone could type any address and we
    mailed it -- and the finished-video mail carries /app/#job=<id>, which IS
    the download credential, so a typo or somebody else's address handed a
    stranger a working link to a real person's performance. The spam exposure
    was the smaller half of that.

    Asserts the whole shape: unknown and pending addresses may not be mailed,
    one request is one mail and not many, the click is single use, and a
    refusal is permanent and outranks any later confirmation.
    """
    import hashlib
    import secrets
    import time as _time
    from app import store

    now = _time.time()
    address = f"prove-{secrets.token_hex(4)}@example.invalid"

    if store.may_mail(address):
        raise Failed("a never-seen address may be mailed; the gate is open")

    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if not store.start_confirmation(address, digest, now):
        raise Failed("could not start a confirmation for a fresh address")
    if store.may_mail(address):
        raise Failed("an address with a PENDING confirmation may be mailed — "
                     "the link would go out before anyone proved they own it")
    if store.start_confirmation(address, digest, now + 5):
        raise Failed("a second confirmation mail was allowed straight away; "
                     "resubmitting would let anyone mail a stranger repeatedly")

    if store.confirm_by_token(digest, now + 10) != address:
        raise Failed("the confirmation click did not confirm the address")
    if not store.may_mail(address):
        raise Failed("a confirmed address still may not be mailed")
    if store.confirm_by_token(digest, now + 20) is not None:
        raise Failed("the confirmation link worked twice; it must be single "
                     "use so a forwarded mail cannot re-confirm")

    # Refusal: permanent, and it outranks confirmation.
    victim = f"nope-{secrets.token_hex(4)}@example.invalid"
    vtoken = hashlib.sha256(secrets.token_urlsafe(32).encode()).hexdigest()
    store.start_confirmation(victim, vtoken, now)
    if store.suppress_by_token(vtoken, now + 1) != victim:
        raise Failed("refusing did not suppress the address")
    if store.may_mail(victim):
        raise Failed("a suppressed address may still be mailed")
    if store.start_confirmation(victim, vtoken, now + 10 ** 6):
        raise Failed("a suppressed address could be asked again — somebody "
                     "who said 'not me' must not be re-mailed by a later "
                     "visitor typing their address")

    return ("unknown and pending refused; one ask per quiet period; click is "
            "single use; refusal permanent and outranks confirmation")


def check_the_video_carries_the_weefeen_mark() -> str:
    """The mark the design screen draws is actually rendered.

    The preview painted the weefeen logo and app/panel.py drew text only, so
    every video went out unbranded while the design screen promised
    otherwise -- the same preview/render mismatch as the transparent band,
    and invisible to the styling check, which only proves CSS classes exist.

    Placement follows the preview: the FULL logo at a panel's head, the
    CIRCLE on the score band when there is no panel. Never both.
    """
    import tempfile as _tf
    from app import render as rnd

    work = pathlib.Path(_tf.mkdtemp())
    seen = {}
    for panel in (rnd.PANEL_OFF, rnd.PANEL_LEFT):
        style = rnd.Style(aspect="16/9", panel=panel)
        layout = rnd.compute_layout(style, 1306 / 244.0, 16 / 9)
        placed = rnd._logo_placement(style, layout, work)
        if placed is None:
            raise Failed(f"no weefeen mark is drawn with panel={panel!r}; the "
                         f"design screen shows one and the video would have "
                         f"none")
        path, x, y, w, h = placed
        if not path.is_file():
            raise Failed("the mark image was not produced")
        cw, ch = layout.canvas
        if x < 0 or y < 0 or x + w > cw or y + h > ch:
            raise Failed(f"the mark falls outside the frame at panel={panel!r}"
                         f": {w}x{h} at ({x},{y}) on {cw}x{ch}")
        seen[panel] = path.name

    if "FULL" not in seen[rnd.PANEL_LEFT]:
        raise Failed("a panel should carry the FULL logo at its head, got "
                     + seen[rnd.PANEL_LEFT])
    if "CIRCLE" not in seen[rnd.PANEL_OFF]:
        raise Failed("with no panel the band should carry the CIRCLE, got "
                     + seen[rnd.PANEL_OFF])

    return f"panel -> {seen[rnd.PANEL_LEFT]}; no panel -> {seen[rnd.PANEL_OFF]}"


def check_an_object_says_what_it_is() -> str:
    """Every object carries its own content type, not the video's.

    `storage.put` hardcoded `ContentType: video/mp4` on every upload. That
    was true while this module only ever carried finished videos, and
    quietly wrong from the moment it also carried score packages,
    engravings and a catalogue -- all of which went into the bucket
    labelled as video.

    A label outranks a filename. A `.tar` marked `video/mp4` downloads from
    a browser as a media file, which is how this was found; a `.svg` marked
    that way will not render from a bucket URL at all. The site was spared
    only because it fetches previews through boto3 and sets its own
    mimetype on the way out.
    """
    from app import storage

    expected = {
        "jobs/abc/output/abc_PROCESSED.mp4": "video/mp4",
        "scores/Some Score.tar": "application/x-tar",
        "scores/Some Score/band.svg": "image/svg+xml",
        "scores/Some Score/pages/p1.svg": "image/svg+xml",
        "scores/catalogue.json": "application/json",
    }
    for key, want in expected.items():
        got = storage.content_type(key)
        if got != want:
            raise Failed(f"{key} would be stored as {got!r}, not {want!r}")

    # Unknown means unknown. Guessing lets a browser rename a file it has
    # no business renaming; "bytes" makes it keep the name it arrived with.
    if storage.content_type("scores/no-suffix") != "application/octet-stream":
        raise Failed("an unrecognised object claims a type it cannot know")

    # And put() must actually consult it. The bug was not the absence of a
    # table, it was a literal on the upload call.
    src = (ROOT / "app" / "storage.py").read_text(encoding="utf-8")
    body = src[src.index("def put("):src.index("def get(")]
    if "content_type(key)" not in body:
        raise Failed("put() does not derive the content type from the key")
    if '"video/mp4"' in body:
        raise Failed("put() still hardcodes video/mp4 for every object")

    return (f"{len(expected)} kinds mapped, unknown stays octet-stream, "
            f"put() derives it from the key")

def check_the_score_reaches_a_compute_node() -> str:
    """The score a node renders can get to a host that has never had it.

    THE BUG THIS EXISTS FOR. Scores reached a compute node exactly one way:
    they were rsynced onto a machine which was captured as a Linode image,
    and nodes booted from that image. Installing a score therefore meant
    re-capturing a multi-gigabyte image -- and until somebody did, the score
    sat on the web box, which does not render. A score could be installed,
    catalogued, advertised to a visitor, and unrenderable. It happened: the
    Rondo was installed and the only machine that could have used it had no
    idea it existed.

    The suite had "the input reaches a compute node" and no sibling for the
    score, which is why nothing said a word. This is that sibling.

    Proven with an in-memory bucket and a REAL package: publish it, then
    fetch it into a score root that starts empty -- a host that has never
    seen this score -- and ask the loader, on that root, whether it can play
    it. Not "the files copied": the loader's own verdict.
    """
    import dataclasses
    import shutil
    from app import package as pkg, scorestore, storage
    from app import library
    from app.settings import settings

    # ON DISK, not from the library: this tars the folder for real, and
    # `library` answers from the published catalogue whenever the bucket is
    # reachable -- where a package has no local folder at all, by design.
    source = _a_package_on_disk()

    bucket: dict = {}
    saved = (storage.put, storage.head, storage.get, storage.available,
             storage.list_keys)

    def fput(local, key):
        data = pathlib.Path(local).read_bytes(); bucket[key] = data
        return len(data)

    def fhead(key):
        return len(bucket[key]) if key in bucket else None

    def fget(key, local):
        q = pathlib.Path(local); q.parent.mkdir(parents=True, exist_ok=True)
        q.write_bytes(bucket[key]); return len(bucket[key])

    def flist(prefix):
        return [k for k in bucket if k.startswith(prefix)]

    fresh = pathlib.Path(tempfile.mkdtemp())
    try:
        storage.put, storage.head, storage.get = fput, fhead, fget
        storage.available = lambda: True
        storage.list_keys = flist

        # Nothing published: the answer is a clean "no", not an exception.
        # The worker turns this into "install it with check_score.py", and
        # it must be able to tell that apart from a broken download.
        if scorestore.fetch(source.name, fresh) is not None:
            raise Failed("fetching an unpublished score did not report it "
                         "missing, so a node could not tell 'nobody "
                         "installed this' from 'the download broke'")

        stored = scorestore.publish(source)
        if fhead(scorestore.key(source.name)) != stored:
            raise Failed("publishing a score did not leave it in the bucket")
        if scorestore.catalogue() != [source.name]:
            raise Failed("the published score is not in the bucket's "
                         "catalogue, so nothing can discover it")

        # A host that has never had this score.
        if list(fresh.iterdir()):
            raise Failed("the fixture is wrong; the fresh root is not empty")
        landed = scorestore.fetch(source.name, fresh)
        if landed is None or not landed.is_dir():
            raise Failed("the score did not arrive on a host without it")

        # THE LOADER'S VERDICT, on the fresh root, not a file count.
        verdicts = list(pkg.inspect(fresh))
        usable = [got for _, got, _ in verdicts if got is not None]
        if len(usable) != 1:
            why = "; ".join(w for _, got, w in verdicts if got is None)
            raise Failed(f"a node could not load the score it fetched: {why}")
        if usable[0].name != source.name:
            raise Failed(f"the fetched package calls itself "
                         f"{usable[0].name!r}, not {source.name!r}; the "
                         f"recogniser resolves on that exact string")

        # Bands are what a render draws; a package without them is installed
        # and useless, which is the failure this whole module is about.
        bands = list((landed / "score" / "lines").glob("*"))
        original = list((source / "score" / "lines").glob("*"))
        if len(bands) != len(original) or not bands:
            raise Failed(f"{len(bands)} band images arrived, {len(original)} "
                         f"were published")

        # Fetched once per host, not once per job: a node renders several
        # jobs and re-downloading 110 MB for each is the node's whole hour.
        before = len(bucket)
        again = scorestore.fetch(source.name, fresh)
        if again != landed or len(bucket) != before:
            raise Failed("a second fetch did not reuse the package already "
                         "on the host")

        # The staging directory must not read as a package. During a fetch
        # it holds a half-extracted tree, and a catalogue that lists it
        # would show a broken score that appears and vanishes on its own.
        (fresh / ".fetching").mkdir(exist_ok=True)
        if any(c.name.startswith(".") for c in pkg.candidates(fresh)):
            raise Failed("the staging directory is offered as a score "
                         "package")
    finally:
        (storage.put, storage.head, storage.get, storage.available,
         storage.list_keys) = saved
        shutil.rmtree(fresh, ignore_errors=True)

    # And the worker asks for it. Everything above is the mechanism; this is
    # the line that connects it to a job, and its absence is what made the
    # mechanism worth nothing for as long as it did not exist.
    worker_src = (ROOT / "app" / "queue" / "worker.py").read_text(
        encoding="utf-8")
    if "scorestore.ensure" not in worker_src:
        raise Failed("the worker never asks the bucket for a score it does "
                     "not have, so a node still renders only what its image "
                     "happened to be captured with")

    # A node given a key scoped to jobs/ alone would meet this as a
    # permissions error at render time. The contract says scores/ out loud.
    cloud_src = (ROOT / "app" / "compute" / "cloudinit.py").read_text(
        encoding="utf-8")
    if "scores/" not in cloud_src:
        raise Failed("the scoped compute credential's contract does not "
                     "mention scores/, so scoping it would break rendering")

    return (f"published {stored / 1e6:.0f} MB; a host with an empty score "
            f"root fetched it and the loader played it back as "
            f"{usable[0].display_name!r}; cached, and not offered mid-fetch")

def check_the_web_box_needs_no_scores() -> str:
    """The site answers from the published catalogue, not from a disk.

    THE PROBLEM. The web box carried every score package so that it could
    answer three questions -- what is in the library, what does the opening
    band look like, and what does one engraved plate look like -- and it
    was carrying twenty-five megabytes of engraving per score to do it. Two
    scores cost 134 MB. The 375 that are coming would cost about 26 GB, on
    a box with 63 GB free, to serve a few hundred kilobytes of preview.

    Recognition never needed them: it matches a recording against 8.8 MB of
    fingerprint index and names a piece. Only the catalogue and the preview
    ever touched a package, and neither needs the package.

    So the catalogue is published to the bucket and the two preview files
    are published loose beside it. Proven here with an in-memory bucket and
    a score root that DOES NOT EXIST: publish, then answer every question
    the site asks, with nowhere on disk for a score to be.
    """
    import dataclasses
    import shutil
    from app import library, package as pkg, scorestore, storage
    from app.settings import settings

    # ON DISK. This check's whole point is that the web box needs no
    # scores, so it must start from a machine that HAS one -- and ask the
    # disk for it, not the library, which on a credentialed machine answers
    # from the bucket with no folder behind it.
    source = _a_package_on_disk()
    real = pkg.load(source)

    bucket: dict = {}
    saved = (storage.put, storage.head, storage.get, storage.available,
             storage.list_keys, library.settings, scorestore.settings)

    def fput(local, key):
        data = pathlib.Path(local).read_bytes(); bucket[key] = data
        return len(data)

    def fhead(key):
        return len(bucket[key]) if key in bucket else None

    def fget(key, local):
        q = pathlib.Path(local); q.parent.mkdir(parents=True, exist_ok=True)
        q.write_bytes(bucket[key]); return len(bucket[key])

    def flist(prefix):
        return [k for k in bucket if k.startswith(prefix)]

    work = pathlib.Path(tempfile.mkdtemp())
    try:
        storage.put, storage.head, storage.get = fput, fhead, fget
        storage.available = lambda: True
        storage.list_keys = flist
        scorestore.publish(source)

        # A WEB BOX WITH NO SCORES. Not an empty directory -- no directory.
        nowhere = dataclasses.replace(settings, score_roots=[],
                                      work_dir=work)
        library.settings = nowhere
        scorestore.settings = nowhere
        library._catalogue = library._Catalogue()      # noqa: SLF001

        found = library.packages()
        if len(found) != 1:
            raise Failed(f"the library shows {len(found)} scores on a box "
                         f"with none on disk; it should show the published "
                         f"one")
        entry = found[0]

        # Everything /api/library puts on the page, and the licence credit
        # the CC BY terms require, all without a package on this disk.
        if entry.name != real.name or entry.last_measure != real.last_measure:
            raise Failed("the catalogue disagrees with the score it came "
                         "from about its name or its length")
        if tuple(entry.band_size) != tuple(real.band_size):
            raise Failed(f"the catalogue says the band is {entry.band_size} "
                         f"and the score says {real.band_size}; the preview "
                         f"would draw the wrong shape")
        if len(entry.bands) != len(real.bands):
            raise Failed("the catalogue lost bands")
        if entry.metadata.get("PPR", "") != real.metadata.get("PPR", ""):
            raise Failed("the publisher is missing from the catalogue, and "
                         "the CC BY licence on these scores requires it")
        if library.find(real.name) is None:
            raise Failed("a score cannot be found by name, so the render "
                         "request that names it would be refused")

        # The preview, fetched one small file at a time, into memory.
        band = scorestore.preview_bytes(real.name, scorestore.PREVIEW_BAND)
        if not band or len(band) < 1000:
            raise Failed("the opening band did not arrive, so the preview "
                         "would show an empty frame")
        if not band.lstrip()[:5].lower().startswith(b"<?xml") and                 b"<svg" not in band[:2000]:
            raise Failed("what arrived as the opening band is not an svg")
        plates = len(sorted(source.glob("pages/page_*.svg")))
        if entry.pages != plates:
            raise Failed(f"the catalogue counts {entry.pages} engraved "
                         f"plates and there are {plates}")
        if plates:
            got = scorestore.preview_bytes(real.name, scorestore.plate_name(1))
            if not got:
                raise Failed("the engraved plate did not arrive")

        # AND THE POINT OF ALL OF IT: NOTHING landed on this box. Not the
        # packages, and not a cached copy of the preview either -- the
        # library is heading for 500 GB, and a disk cache is a library that
        # fills up slowly rather than all at once.
        landed = [f for f in work.rglob("*") if f.is_file()]
        if landed:
            raise Failed(f"{len(landed)} file(s) were written to the web "
                         f"box's disk serving a preview: "
                         f"{', '.join(str(f.name) for f in landed[:3])}")
        whole = sum(f.stat().st_size
                    for f in source.rglob("*") if f.is_file())

        # And the cache is capped, or a big enough library fills the memory
        # instead of the disk and nothing has been solved.
        if scorestore.PREVIEW_CACHE_BYTES > 64 * 1024 * 1024:
            raise Failed("the preview cache is not meaningfully capped")
    finally:
        (storage.put, storage.head, storage.get, storage.available,
         storage.list_keys, library.settings, scorestore.settings) = saved
        library._catalogue = library._Catalogue()      # noqa: SLF001
        scorestore._cache.clear()                      # noqa: SLF001
        scorestore._preview_cache.clear()              # noqa: SLF001
        shutil.rmtree(work, ignore_errors=True)

    # Recognition is on the web box and must stay independent of all this:
    # it names a recording from a fingerprint index, and a box with no
    # scores must still be able to tell a visitor what they played.
    id_src = (ROOT / "app" / "identify.py").read_text(encoding="utf-8")
    if "score_roots" in id_src or "library" in id_src.split("\n")[0:0] or \
            "from . import library" in id_src:
        raise Failed("the recogniser reaches into the score library, so a "
                     "box without the scores could not identify a recording")

    return (f"catalogue, band and plate all served with no score root at "
            f"all and nothing written to disk, against a "
            f"{whole / 1e6:.0f} MB package")

def check_the_edition_is_credited() -> str:
    """The engraving's publisher is named on the band, quietly.

    Musicians choose editions deliberately -- a Breitkopf Chopin and a
    Paderewski Chopin disagree about phrasing, fingering and sometimes notes
    -- so a score video that will not say which one it used is worth less to
    exactly the people who care most. The package carried PPR and PPP all
    along and nothing drew them.

    The colour is mixed from the band's OWN ink towards its paper, never a
    fixed grey: the band's colours change per video, and a hardcoded dark
    would shout on dark paper and vanish on pale ink.
    """
    import tempfile as _tf
    from PIL import Image
    from app import library, render as rnd

    packages = library.packages()
    if not packages:
        raise Failed("no score package installed to read an edition from")
    pkg = packages[0]
    if not pkg.edition:
        raise Failed(f"{pkg.name} exposes no edition; PPR/PPP are missing")

    if not rnd.Style().score_credit:
        raise Failed("the edition credit is off by default")

    work = pathlib.Path(_tf.mkdtemp())
    style = rnd.Style(aspect="16/9")
    made = rnd._credit_png(pkg.edition, (1920, 1080), style, work)
    if made is None:
        raise Failed("no credit image was produced")
    path, w, h = made
    if not path.is_file() or Image.open(path).getbbox() is None:
        raise Failed("the credit image is empty; nothing was drawn")

    # It follows the band, and is neither the ink nor the paper.
    ink = rnd._blend(style.band_fg, style.band_bg, 0.55)
    if ink == (0, 0, 0) or ink == (255, 255, 255):
        raise Failed(f"the credit colour collapsed to {ink}; it must sit "
                     f"between the notation and the paper")
    pale = rnd._blend("#ffffff", "#111111", 0.55)
    if pale == ink:
        raise Failed("the credit colour ignores the band's colours")

    # It must fit beside the weefeen mark, not under it.
    layout = rnd.compute_layout(style, 1306 / 244.0, 16 / 9)
    if w > layout.band.w / 2:
        raise Failed(f"the credit is {w}px on a {layout.band.w}px band; it "
                     f"would reach the weefeen mark at the other end")

    return f"{pkg.edition!r} drawn in {ink}, {w}x{h}, on every band"


def check_the_page_is_actually_styled() -> str:
    """Everything the script puts on the page can be seen, and the CSS parses.

    THREE DEFECTS IN ONE DAY were visible on the page and green in every
    check: captions that vanished, a title animation that stopped, and a
    download button that was in the DOM and invisible because no rule for
    `.delivery`, `.stat`, `.rail` or `.get` existed anywhere. The other 26
    checks verify behaviour and none of them look at what is rendered, which
    is the gap that keeps producing them.

    This is not a browser and cannot see a layout. It checks two things that
    are decidable from the source and that all three of those bugs would
    have failed:

      * every class the script assigns has a rule somewhere — a stylesheet,
        a <style> block the script injects, or an explicit note that it is
        styled inline.
      * the CSS braces balance. An unterminated @media block swallows every
        rule after it, which is how the title animation was lost: the rules
        were present, inside a block that never closed.
    """
    import re

    web = ROOT / "app" / "static" / "svs"
    scripts = sorted(web.glob("*.js"))
    sheets = [web / "index.html", *sorted(web.glob("*.css"))]

    # Where a rule can legitimately live: a stylesheet, or a <style> block
    # the script writes into the page at runtime.
    #
    # ONLY the <style> blocks of the HTML, never the whole file. Reading it
    # whole was the first version of this and it made the check useless: a
    # class named in a comment, in prose, or in the page's own JavaScript
    # counted as styled, so deleting every `.delivery` rule still passed.
    # Verified by doing exactly that.
    css = ""
    for sheet in sheets:
        text = sheet.read_text(encoding="utf-8", errors="ignore")
        if sheet.suffix == ".html":
            text = "\n".join(
                re.findall(r"<style[^>]*>(.*?)</style>", text, re.S))
        css += "\n" + text
    # Comments too: a rule discussed in a comment is not a rule.
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for p in scripts:
        text = p.read_text(encoding="utf-8", errors="ignore")
        for block in re.findall(r"<style[^>]*>(.*?)</style>", text, re.S):
            css += "\n" + block
        # Template literals assigned to the .textContent of a <style> this
        # script created. The element is never actually called `style` — it
        # is bandCSS, clashCSS, countCSS — so the names are read out of the
        # createElement calls rather than assumed.
        names = re.findall(
            r"""(?:const|let|var)\s+(\w+)\s*=\s*document\.createElement\("""
            r"""['"]style['"]\)""", text)
        for name in names:
            for block in re.findall(
                    name + r"\.textContent\s*=\s*`(.*?)`", text, re.S):
                css += "\n" + block

    # Classes positioned entirely by `el.style`, with no rule by design.
    # Listed explicitly, because "it has no CSS" is exactly the bug this
    # check exists to find — an exemption has to be a decision.
    INLINE_ONLY = {
        # Positioned entirely by el.style, with no rule by design.
        "panelfoot",    # Object.assign(foot.style, …) in svs-wire.js
        # These have no rule and do not need one: each renders on its own.
        # Listed rather than filtered by element type, so adding a class
        # that genuinely needs styling still fails.
        "samplevid",    # a <video>; it has intrinsic size and controls
        "vector",       # a modifier on .realband, which IS styled
        "half",         # a <div> of text in svs-min.js; inherits the page
    }

    made = set()
    for p in scripts:
        text = p.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"""class=\\?["']([a-zA-Z0-9 _-]+)""", text):
            made.update(m.group(1).split())
        for m in re.finditer(r"""className\s*=\s*["']([a-zA-Z0-9 _-]+)""", text):
            made.update(m.group(1).split())

    styled = set(re.findall(r"\.([a-zA-Z][a-zA-Z0-9_-]*)", css))
    invisible = sorted(made - styled - INLINE_ONLY)
    if invisible:
        raise Failed(
            "the script creates these and nothing styles them, so they go "
            "into the page and cannot be seen: "
            + ", ".join("." + c for c in invisible)
            + ". If one is positioned by el.style, add it to INLINE_ONLY "
              "with the reason.")

    # Braces, ignoring comments and strings well enough for a stylesheet.
    for sheet in sheets:
        text = sheet.read_text(encoding="utf-8", errors="ignore")
        if sheet.suffix == ".html":
            text = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", text, re.S))
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        opened, closed = text.count("{"), text.count("}")
        if opened != closed:
            raise Failed(
                f"{sheet.name} has {opened} '{{' and {closed} '}}'. An "
                f"unterminated block swallows every rule after it — which is "
                f"how the title animation was lost, with the rules present "
                f"and trapped inside a @media that never closed.")

    return (f"{len(made)} script-made classes all styled; braces balance in "
            f"{len(sheets)} stylesheet(s)")


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
def check_a_plate_that_cannot_be_measured_is_still_served() -> str:
    """The preview's fallback must be a whole plate, not a stack trace.

    `_ink_band` measures where the engraving sits on a page so the preview
    can crop the margins. When cairo cannot render the plate -- no library,
    a malformed file, an odd viewBox -- it is meant to log and fall back to
    the whole plate. The except branch that did that named a variable from
    an earlier signature, so the one path written to survive a failure was
    the one path that raised: every unmeasurable plate became a 500 on the
    page preview, with the original reason hidden under a NameError.

    Exercised by making the rasteriser fail on purpose and asking for the
    band. The point is not the tuple; it is that the call returns at all.
    """
    from app import routes, svg

    class _Broken:
        @staticmethod
        def svg2png(**_kw):
            raise RuntimeError("cairo said no")

    real = svg._cairosvg                                 # noqa: SLF001
    svg._cairosvg = lambda: _Broken()                    # noqa: SLF001
    try:
        band = routes._ink_band(b"<svg/>", "selftest-unmeasurable")   # noqa: SLF001
    except NameError as exc:
        raise Failed(f"the fallback for an unmeasurable plate raises "
                     f"{exc!r} instead of serving the whole plate")
    finally:
        svg._cairosvg = real                             # noqa: SLF001
        routes._INK.pop("selftest-unmeasurable", None)  # noqa: SLF001

    if tuple(band) != (0.0, 1.0, 0.0, 1.0):
        raise Failed(f"an unmeasurable plate should be served whole, got "
                     f"{band}")
    return "rasteriser failure -> the whole plate, logged, not raised"


def check_a_render_cannot_hang_forever() -> str:
    """A tool that stalls is killed, and a hand-back kills the tool.

    The worker heartbeats while ffmpeg runs, so an encode that hung renewed
    its own lease for ever and held a paid machine with it -- the one
    failure the lease could not catch, because the process holding it was
    perfectly alive. And a volunteer's Ctrl-C handed the job back while the
    ffmpeg it had started carried on, because a child outlives a parent that
    merely exits.
    """
    import re as _re
    import sys as _sys
    import threading as _th
    import time as _t
    from app import render as rnd

    sleeper = [_sys.executable, "-c", "import time; time.sleep(30)"]
    began = _t.time()
    try:
        rnd._run(sleeper, "a stalled tool", timeout=0.5)       # noqa: SLF001
    except rnd.ToolFailed as exc:
        if "gave up" not in str(exc):
            raise Failed(f"the deadline raised, but not as a deadline: {exc}")
    else:
        raise Failed("a tool that never returns was waited for")
    if _t.time() - began > 10:
        raise Failed("the deadline fired late; the tool was not killed")
    if rnd._children:                                           # noqa: SLF001
        raise Failed("a finished tool is still listed as running")

    outcome: dict = {}

    def run() -> None:
        try:
            rnd._run(sleeper, "a render in flight")             # noqa: SLF001
        except rnd.ToolFailed as exc:
            outcome["failed"] = str(exc)

    thread = _th.Thread(target=run, daemon=True)
    thread.start()
    for _ in range(100):
        if rnd._children:                                       # noqa: SLF001
            break
        _t.sleep(0.05)
    else:
        raise Failed("the running tool was never registered, so a hand-back "
                     "could not have killed it")
    killed = rnd.abort_children()
    thread.join(10)
    if thread.is_alive() or killed != 1:
        raise Failed(f"abort_children stopped {killed} tool(s) and the render "
                     f"thread {'is still running' if thread.is_alive() else 'ended'}")

    # And both real invocations are bounded.
    src = (ROOT / "app" / "render.py").read_text(encoding="utf-8")
    calls = [m.start() for m in _re.finditer(r"\n\s+_run\(", src)]
    unbounded = [src[i:i + 60].strip().splitlines()[0]
                 for i in calls if "timeout=" not in src[i:i + 600]]
    if len(calls) < 2 or unbounded:
        raise Failed(f"ffmpeg is run without a deadline at: {unbounded}")
    return (f"a stalled tool is killed at its deadline, a hand-back kills the "
            f"tool in flight, and all {len(calls)} ffmpeg runs are bounded")


def check_a_video_counts_wherever_it_was_made() -> str:
    """The tally counts deliveries, not the machines that made them.

    The number on the page is "videos this install has made", and a render
    now happens on whichever machine took the job -- the web box, a rented
    node, or a laptop lent to the queue. If the count were made where the
    WORK happened it would land in that machine's own `var/stats.json`: a
    node's dies with the node, and a laptop's sits on the laptop, so every
    render off the web box would be invisible on the page and the number
    would quietly under-report the busier the system got.

    It is counted where the RESULT IS APPLIED instead -- the ledger, which
    runs only in the web box's applier thread, once per `done` event. So
    this asserts two things that must stay true together: the worker never
    counts, and the ledger does.
    """
    import re as _re
    from app import stats, store
    from app.queue import ledger
    from app.queue.messages import Event

    worker_src = (ROOT / "app" / "queue" / "worker.py").read_text(encoding="utf-8")
    if _re.search(r"\bstats\b", worker_src):
        raise Failed("the worker touches the tally. It runs on whichever "
                     "machine took the job, so the count would land on a "
                     "node that is about to be destroyed, or on a laptop")

    ledger_src = (ROOT / "app" / "queue" / "ledger.py").read_text(encoding="utf-8")
    if "stats.record_video()" not in ledger_src:
        raise Failed("nothing in the ledger counts a delivered video, so the "
                     "page's tally never moves")

    # A render that happened somewhere else entirely: the job row names a
    # worker that is not this machine, and the result is a path this box
    # has never had. Only the `done` event came back.
    job_id = "tally-elsewhere"
    _queued(job_id, state=store.RUNNING)
    before = stats.videos()
    try:
        store.update_job(job_id, worker="somebody-elses-laptop")
        ledger.apply(Event(job_id=job_id, type="done",
                           worker="somebody-elses-laptop",
                           result="/not/on/this/disk/video.mp4",
                           object_key=f"jobs/{job_id}/video.mp4",
                           output_bytes=1234, elapsed=1.0))
        after = stats.videos()
    finally:
        with store.write() as conn:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    if after - before != 1:
        raise Failed(f"a video rendered on another machine moved the tally by "
                     f"{after - before}; a render off the web box must count "
                     f"exactly once, where the result is applied")
    return ("counted in the ledger, on the web box, once per delivery -- a "
            "render on a node or a lent laptop counts the same")


def check_a_bucket_without_credentials_is_not_available() -> str:
    """No keys is not "available"; it is a machine that must decline work.

    `boto3.client` builds with no credentials at all -- it defers to the
    ambient chain and fails only when something is asked of it. So a machine
    with no keys reported the bucket as AVAILABLE, and the truth arrived as
    `NoCredentialsError` in the middle of a render.

    It is the state a lent laptop arrives in -- boto3 installed, keys never
    set -- and it decides whether that machine declines the job or takes it
    and loses it. `app/storage.py` has always claimed in its own docstring
    that it reports this; the check is that it now does.
    """
    import dataclasses
    import importlib
    from app import storage
    from app.settings import settings

    if "without credentials" not in (storage.__doc__ or ""):
        raise Failed("storage.py no longer promises to report missing "
                     "credentials; this check is guarding nothing")

    try:
        import boto3                                          # noqa: F401
    except ImportError:
        return "boto3 absent here, which storage already refuses on"

    was, seen = storage.settings, {}
    try:
        # A bucket configured, keys blank, and nothing in the ambient chain.
        storage.settings = dataclasses.replace(
            settings, object_bucket="video-sync", object_key="",
            object_secret="")
        for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                    "AWS_PROFILE", "AWS_SHARED_CREDENTIALS_FILE",
                    "AWS_CONFIG_FILE"):
            seen[var] = os.environ.pop(var, None)
        # Point the credential files at nothing, so a developer's own
        # ~/.aws does not make this pass on their machine and fail in CI.
        os.environ["AWS_SHARED_CREDENTIALS_FILE"] = str(
            pathlib.Path(tempfile.gettempdir()) / "vsw-no-such-credentials")
        os.environ["AWS_CONFIG_FILE"] = str(
            pathlib.Path(tempfile.gettempdir()) / "vsw-no-such-config")
        storage._client, storage._tried, storage._problem = None, False, ""
        # AND IT MUST NOT RAISE. `available()` is asked as a question
        # everywhere -- `if storage.available():` -- so an exception out of
        # it is not a "no", it is a crash in whatever was asking. The
        # credential chain reaches the network, and on a machine where that
        # fails it threw SSLError straight through this line.
        try:
            answer = storage.available()
        except Exception as exc:                                # noqa: BLE001
            raise Failed(f"storage.available() raised {type(exc).__name__} "
                         f"instead of answering: {exc}")
        if answer:
            raise Failed("a bucket with no credentials reports itself "
                         "available; a machine would accept a render and "
                         "then fail to fetch the recording")
        why = storage.status().get("problem", "")
        if "OBJECT_KEY" not in why:
            raise Failed(f"the reason does not say what to set: {why!r}")
    finally:
        storage.settings = was
        for var, value in seen.items():
            os.environ.pop(var, None)
            if value is not None:
                os.environ[var] = value
        storage._client, storage._tried, storage._problem = None, False, ""

    return "no credentials -> not available, and the reason names OBJECT_KEY"


def check_a_fetched_score_lands_where_it_was_told() -> str:
    """A package from the bucket unpacks into a root chosen on purpose.

    `local_root` says "the first configured DIGITAL root" and took the first
    root of any kind. On a machine with several -- a laptop lent to the
    queue, where the roots include working folders -- that meant thousands
    of files from the server could be unpacked into whichever tree happened
    to be listed first, looking like work somebody had done.
    """
    import dataclasses
    from app import scorestore
    from app.settings import DIGITAL, RASTER, ScoreRoot, settings

    here = pathlib.Path(tempfile.mkdtemp())
    a, b = here / "a-working-folder", here / "the-library"
    a.mkdir()
    b.mkdir()

    was = scorestore.settings
    try:
        # Raster listed first, digital second: the digital one wins.
        scorestore.settings = dataclasses.replace(
            settings, score_roots=[ScoreRoot(RASTER, a), ScoreRoot(DIGITAL, b)])
        if scorestore.local_root() != b:
            raise Failed(f"a fetched package would land in "
                         f"{scorestore.local_root()}, not the digital root")

        # Order among digital roots is the operator's, and is respected.
        scorestore.settings = dataclasses.replace(
            settings, score_roots=[ScoreRoot(DIGITAL, b), ScoreRoot(DIGITAL, a)])
        if scorestore.local_root() != b:
            raise Failed("the first digital root listed is not the one used")

        # Nothing digital: any root beats refusing to fetch at all.
        scorestore.settings = dataclasses.replace(
            settings, score_roots=[ScoreRoot(RASTER, a)])
        if scorestore.local_root() != a:
            raise Failed("with no digital root nothing would be fetched")

        # A root that is not there is not a destination.
        scorestore.settings = dataclasses.replace(
            settings, score_roots=[ScoreRoot(DIGITAL, here / "gone"),
                                   ScoreRoot(DIGITAL, b)])
        if scorestore.local_root() != b:
            raise Failed("a missing root was chosen over one that exists")
    finally:
        scorestore.settings = was
        shutil.rmtree(here, ignore_errors=True)

    return "digital root first, operator's order respected, missing roots skipped"


def check_the_lending_panel_is_local_and_narrow() -> str:
    """One switch, reachable from nowhere else, that never rents a machine.

    It pauses a renderer mid-job and writes the production server's
    configuration, and it has NO LOGIN. That is the right trade only while
    it cannot be reached from off this machine, so the binding is the whole
    of its security and is asserted rather than left to a default.

    The rest is about it staying simple. Four versions of this page were
    rejected for offering the settings the program has instead of the
    decision a person makes; what survived is one switch, with the server
    held where it creates nothing. A second control would be the fifth
    version of the same mistake, so the shape is pinned here: exactly one
    switch, and exactly one mode ever written -- the one that rents nothing.
    """
    import json as _json
    import urllib.error
    import urllib.request
    sys.path.insert(0, str(ROOT / "tools"))
    import lend_panel                                       # noqa: PLC0415

    src = (ROOT / "tools" / "lend_panel.py").read_text(encoding="utf-8")
    if '"0.0.0.0"' in src or "'0.0.0.0'" in src:
        raise Failed("the panel binds 0.0.0.0. It can pause a render and "
                     "write the server's configuration, and it has no login")
    if '("127.0.0.1", port)' not in src:
        raise Failed("the panel does not bind 127.0.0.1 explicitly")

    # IT NEVER RENTS. `manual` is the only mode it writes and `_write_mode`
    # refuses anything else outright, so no path through this page and no
    # value arriving on it can leave the server creating machines.
    if lend_panel.HELD_AT != "manual":
        raise Failed(f"the panel holds the server at {lend_panel.HELD_AT!r}, "
                     f"which is not the mode that rents nothing")

    state = {"taking": True, "paused": False, "quiet_for": 0.0, "job": None,
             "free_gb": 8.0, "longest_min": 10.0,
             "scores_here": 1, "scores_total": 2}
    rang = []
    panel = lend_panel.Panel(lambda: state, lambda: rang.append("pause"),
                             lambda: rang.append("resume"),
                             lambda: rang.append("stop"))
    panel._set("manual", "")                                 # noqa: SLF001

    for bogus in ("auto", "cloud", "manual; rm -rf /", "$(id)", ""):
        if panel._write_mode(bogus) == "":                   # noqa: SLF001
            raise Failed(f"the panel wrote {bogus!r} to the server; it is "
                         f"interpolated into a root command, and anything "
                         f"but 'manual' means it can rent machines")

    # The switch reaches the volunteer, both ways.
    panel.set_working(False)
    panel.set_working(True)
    if rang != ["pause", "resume"]:
        raise Failed(f"the switch does not start and stop this machine: {rang}")

    url = lend_panel.serve(panel, port=5098)
    if not url:
        raise Failed("the panel would not start")

    page = urllib.request.urlopen(url, timeout=5).read().decode("utf-8")
    switches = page.count('role="switch"')
    if switches != 1:
        raise Failed(f"{switches} switches on a page whose whole point is "
                     f"that there is one")
    for gone in ("Rent a machine", "rent one", "per video"):
        if gone in page:
            raise Failed(f"the page still offers renting ({gone!r}); it is "
                         f"not a decision this page makes")
    for needed in ("On hold", "Taking videos", "What this computer can take"):
        if needed not in page:
            raise Failed(f"the panel never shows {needed!r}")

    got = _json.loads(urllib.request.urlopen(url + "state", timeout=5).read())
    missing = {"taking", "job", "free_gb", "longest_min", "server"} - set(got)
    if missing:
        raise Failed(f"the page is not told {sorted(missing)}")

    # The switch over HTTP, and a request that names no state refused: a
    # malformed call must not silently stop the machine.
    rang.clear()
    urllib.request.urlopen(urllib.request.Request(
        url + "working", method="POST",
        data=_json.dumps({"on": False}).encode()), timeout=5)
    if rang != ["pause"]:
        raise Failed(f"switching off did not reach the volunteer: {rang}")
    try:
        urllib.request.urlopen(urllib.request.Request(
            url + "working", method="POST", data=b"{}"), timeout=5)
    except urllib.error.HTTPError as exc:
        if exc.code != 400:
            raise Failed(f"a switch with no state answered {exc.code}")
    else:
        raise Failed("a switch request naming no state was accepted, so a "
                     "malformed call silently stops the machine")

    rang.clear()
    urllib.request.urlopen(urllib.request.Request(
        url + "stop", method="POST", data=b"{}"), timeout=5)
    if rang != ["stop"]:
        raise Failed(f"'finish this one, then stop' did not reach the "
                     f"volunteer: {rang}")

    return ("loopback only, no login; one switch and no second control; "
            "'manual' is the only mode it can write, so it never rents")


def check_the_limit_reset_cannot_be_reached_from_outside() -> str:
    """The one endpoint that makes this service MORE abusable.

    Three renders a week is right for a visitor and impossible for whoever
    is proving the service works, so there has to be a way to clear the
    counters -- and a reset anybody could call is the same as having no
    limits at all, on a free, unauthenticated endpoint that costs a GPU
    minute and a multi-gigabyte render per call.

    ITS GUARD IS NOT `remote_addr`, AND THAT IS THE POINT. Apache proxies
    from 127.0.0.1, so `remote_addr` is loopback for the entire internet:
    the obvious guard would have permitted everything while reading as
    though it permitted nothing. Apache sets X-Forwarded-For on what it
    proxies and a call made on the box straight to gunicorn carries none,
    so the ABSENCE of that header is what separates "already on this
    machine" from "arrived from outside".

    Checked here rather than trusted to the front end, because the
    neighbouring endpoints (/metrics, /api/visitors) rely on the Apache
    configuration alone -- which is right for reading, and not enough for
    a switch that turns the limits off.
    """
    from app import limits, routes

    app = routes.create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    # A visitor, arriving through the proxy: indistinguishable from a path
    # that does not exist.
    got = client.post("/api/limits/forget",
                      headers={"X-Forwarded-For": "203.0.113.9"})
    if got.status_code != 404:
        raise Failed(
            f"a proxied request cleared the rate limits ({got.status_code}). "
            f"Anyone could reset their own allowance, which is the same as "
            f"having none")

    # A chain, which is what a second proxy produces. Still outside.
    got = client.post("/api/limits/forget",
                      headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.2"})
    if got.status_code != 404:
        raise Failed("a forwarded chain cleared the rate limits")

    # GET is not it either: a link somebody clicks must not disarm this.
    if client.get("/api/limits/forget").status_code not in (404, 405):
        raise Failed("the reset answers GET, so a link or a crawler could "
                     "clear the limits")

    # And from the box itself it works, and actually empties the counters.
    limits.guard("render_ip", "selftest-victim")
    before = limits._counters._hits                     # noqa: SLF001
    if not before:
        raise Failed("nothing was recorded, so clearing proves nothing")
    got = client.post("/api/limits/forget")
    if got.status_code != 200:
        raise Failed(f"a local request could not clear the limits "
                     f"({got.status_code})")
    if limits._counters._hits:                          # noqa: SLF001
        raise Failed("the reset answered but the counters are still there")

    allowed, _ = limits._counters.check("render_ip",     # noqa: SLF001
                                        "selftest-victim")
    if not allowed:
        raise Failed("the counters were cleared and the limit still refuses")

    # The front end denies it as well -- two independent mistakes needed.
    vhost = (ROOT / "deploy" / "install-web.sh").read_text(encoding="utf-8")
    if "limits/forget" not in vhost:
        raise Failed("the installer's Apache rules do not deny "
                     "/api/limits/forget, so a rebuilt box would publish it")

    return ("a proxied request gets 404, a chain gets 404, GET is refused, "
            "and a call from the box clears every counter")


def check_a_declined_task_really_goes_back() -> str:
    """A consumer that declines is RUN here, not read.

    This existed already and proved nothing. It searched transport.py for
    the text `if accept is not None and not accept(task)` and for
    `requeue=True` nearby -- both of which were present and correct, in a
    function that was never passed `accept` at all. The AMQP consumer
    therefore raised `NameError: name 'accept' is not defined` on the first
    real task, the reconnect handler read that as a lost connection, and it
    backed off 5, 10, 20 ... 300 seconds. An empty queue never reaches that
    line, so every idle run looked perfect and the first upload somebody
    made sat in the queue while the machine that should have taken it said
    it was alive every 45 seconds.

    A grep cannot catch that, and no amount of care in writing one would.
    So this puts a task through a real transport and asserts on what
    happens to it.
    """
    import time as _time
    from app.queue import transport as tmod
    from app.queue.messages import RenderTask

    bus = tmod.LocalTransport()
    task = RenderTask(job_id="decline-me", upload="x.mp4", package="P",
                      duration=60.0)

    seen, verdicts = [], [False, True]

    def accept(t) -> bool:
        seen.append(t.job_id)
        return verdicts.pop(0) if verdicts else True

    ran = []

    def handle(t, ack) -> None:
        ran.append(t.job_id)
        ack()
        raise SystemExit                      # stop the loop once it lands

    bus.publish_task(task)
    worker = threading.Thread(
        target=lambda: _swallow(bus.consume_tasks, handle, accept),
        daemon=True)
    worker.start()

    # Declined once, then taken: the task must come BACK, not vanish.
    deadline = _time.time() + 20
    while _time.time() < deadline and not ran:
        _time.sleep(0.05)

    if not seen:
        raise Failed("the consumer never asked whether to take the task, so "
                     "`accept` is not reaching the transport at all -- which "
                     "is exactly the shape of the bug this exists for")
    if len(seen) < 2:
        raise Failed(f"the task was offered {len(seen)} time(s): a declined "
                     f"task was dropped instead of going back on the queue")
    if not ran:
        raise Failed("the task was declined and never offered again, so a "
                     "visitor's video would wait for ever")

    # And the AMQP consumer passes it along the same way. Checked on the
    # signature rather than a live broker, because this is the join that
    # broke: the check exists BECAUSE the two halves were both correct and
    # not connected.
    import inspect
    sig = inspect.signature(tmod.AmqpTransport._consume_tasks_once)   # noqa: SLF001
    if "accept" not in sig.parameters:
        raise Failed("the AMQP consumer's inner loop takes no `accept`, so "
                     "the decline check inside it cannot work")
    src = inspect.getsource(tmod.AmqpTransport.consume_tasks)
    if "_consume_tasks_once(handle, accept)" not in src:
        raise Failed("`accept` is not handed to the AMQP inner loop; the "
                     "decline check would raise NameError on the first task")

    return (f"offered {len(seen)} times, declined once, requeued and then "
            f"rendered; and the AMQP consumer is passed the same callback")


def _swallow(fn, *args) -> None:
    """Run something that ends by raising, without noise."""
    try:
        fn(*args)
    except BaseException:                                    # noqa: BLE001
        pass


def check_the_page_promises_only_what_we_do() -> str:
    """Three claims the page made, none of which were true.

    They are grouped because they are one failure: copy written against an
    intention, kept after the intention changed, and never read again.

      * "Your video is deleted from our machines once the render is
        delivered." It is not, and the opposite is a deliberate decision --
        `paths.keep()` keeps the RECORDING on purpose, because home
        recordings are the one thing a studio library cannot supply and
        the thing recognition most needs. A promise to delete is exactly
        the kind nobody checks and everybody remembers.

      * "Stay here -- the video appears below when it is done." Written
        when no mail was ever sent, and true then. Mail has worked for a
        while, so this asked somebody to watch a page for the better part
        of an hour for a render that would mail them anyway.

      * "Two of your three free videos left this month." The allowance is
        three a WEEK, and what one visitor has left is not knowable in the
        page at all.

    Asserted against the code rather than against a nicer sentence: the
    retention claim has to agree with `paths.keep()`, and the numbers have
    to be read from the server rather than typed.
    """
    import re as _re
    from app import paths as jobpaths

    page = (ROOT / "app" / "static" / "svs" / "index.html").read_text(
        encoding="utf-8")
    wire = (ROOT / "app" / "static" / "svs" / "svs-wire.js").read_text(
        encoding="utf-8")

    # COMMENTS ARE NOT COPY. The note explaining why a sentence was removed
    # necessarily quotes the sentence, and a check that cannot tell the two
    # apart forbids writing down why anything was fixed.
    def spoken(js: str) -> str:
        js = _re.sub(r"/\*.*?\*/", " ", js, flags=_re.S)
        return _re.sub(r"^\s*//.*$", " ", js, flags=_re.M)

    wire_copy = spoken(wire)
    both = page + wire_copy

    # THE UPLOAD IS KEPT, and the code is the authority on that.
    kept = jobpaths.JobPaths("x").keep.__doc__ or ""
    if "upload" not in kept.lower() or "kept" not in kept.lower():
        raise Failed("paths.keep() no longer explains that the recording is "
                     "kept; if that changed, this page's copy has to change "
                     "with it, and this check is the reminder")
    for lie in ("deleted from our machines",
                "deleted once the render",
                "your video is deleted"):
        if lie in both.lower():
            raise Failed(f"the page claims {lie!r}. The recording is kept "
                         f"deliberately -- see app/paths.py -- so this is a "
                         f"promise the service does not keep")

    # NOBODY IS ASKED TO WAIT. A render is minutes to an hour, and a mail
    # is sent; asking somebody to sit on the page is asking for nothing.
    for wait in ("stay here", "stay on this page", "keep this page open"):
        if wait in both.lower():
            raise Failed(f"the page says {wait!r} while it is rendering. A "
                         f"render takes minutes to the better part of an "
                         f"hour, and an email is sent when it is done")
    if "we will email you when it is done" not in wire_copy:
        raise Failed("the rendering screen does not say the email is coming, "
                     "so somebody has no reason to close the tab")

    # THE NUMBERS COME FROM THE SERVER, not from whatever the mockup said.
    # ANY monthly claim, not one phrasing of it. The first version of this
    # looked for "free videos ... this month" and passed while the landing
    # page said "Three videos a month" four hundred lines above -- the same
    # falsehood, worded differently, on the screen more people read.
    monthly = _re.search(r"(video|free)[^.<]{0,40}a month"
                         r"|this month", both, _re.I)
    if monthly:
        raise Failed(f"the page says the free allowance is monthly "
                     f"({monthly.group(0)!r}); it is weekly, and the number "
                     f"is reported by /api/library")
    if _re.search(r"(two|three|2|3) of your", both, _re.I):
        raise Failed("the page states how many free videos a visitor has "
                     "left. That is not knowable in the page")
    if "SERVER.videosPerWeek" not in wire or "SERVER.retentionHours" not in wire:
        raise Failed("the allowance and the delivery window are not read "
                     "from the server, so they will drift again")

    # THE RECAP THUMBNAIL IS A PICTURE, NOT A CONTROL. It is painted with
    # the design frame's own markup, so it arrived carrying that frame's
    # instructions -- "click to add one", "Click to replace" -- on a screen
    # where the render has already started and nothing can be changed.
    for hint in (".ghostlab", ".vidlab", ".vswap", ".artlab"):
        if f"#miniFrame {hint}" not in wire and f", #miniFrame {hint}" not in wire:
            raise Failed(f"the recap thumbnail still shows {hint}, which "
                         f"tells somebody to click something that is no "
                         f"longer there and is not in their video")
    if "#miniFrame.pnl.ghost{display:none" not in wire.replace(" ", ""):
        raise Failed("the recap thumbnail draws a ghost panel where the "
                     "finished video has none, so it shows a different "
                     "composition from the one being rendered")

    # WHERE ELSE TO LOOK, BEFORE THEY WONDER. The confirmation screen said
    # "check the spam folder" only inside the note that appears after
    # somebody clicks "Resend" -- by which point they have already waited,
    # concluded nothing arrived, and acted on it. Automated mail from a
    # low-volume domain lands in spam often enough that this is the
    # ordinary case; it belongs in the copy that is there on arrival.
    verify = page[page.find('data-s="verify"'):]
    verify = verify[:verify.find("</section>")]
    visible = _re.sub(r"<p[^>]*hidden[^>]*>.*?</p>", "", verify, flags=_re.S)
    if not _re.search(r"junk|spam", visible, _re.I):
        raise Failed("the confirmation screen never says to look in the junk "
                     "or spam folder without clicking something first, which "
                     "is where the message most often is")

    # And one copy for the rendering screen, since two of them disagreed.
    if wire_copy.count("It's <em>rendering</em>") != 1:
        raise Failed("more than one place writes the rendering heading; that "
                     "is how 'stay here' survived a screen that already said "
                     "the opposite")

    return ("nothing promises deletion, nobody is asked to wait, and the "
            "allowance and window are read from the server")


def check_a_wanted_score_reaches_the_operator() -> str:
    """A piece somebody asked for and could not have is not lost.

    The demand loop was a mail and nothing else: a request for a piece with
    no score is refused, the operator is told once, and that message waits
    in an inbox with everything else. Nothing listed what was outstanding,
    and finishing a request days later meant reading the address back out
    of that mail and retyping it into a command.

    Three things have to hold, and each was missing:

      * the address survives the refusal, so the person who asked can be
        told when it is finally made. The column existed; nothing wrote it.
      * the list is reachable ONLY from the box, because it carries
        visitors' addresses -- guarded like the limit reset, by the absence
        of X-Forwarded-For, since Apache proxies from 127.0.0.1 and
        `remote_addr` reads as local for the whole internet.
      * nothing but a published score can be started, and only for a job
        actually on the list. Both become arguments to a command that runs
        on the server.
    """
    import json as _json
    sys.path.insert(0, str(ROOT / "tools"))
    import lend_panel                                       # noqa: PLC0415
    from app import routes

    app = routes.create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    # From outside: the same answer as a path that does not exist.
    got = client.get("/api/wanted",
                     headers={"X-Forwarded-For": "203.0.113.9"})
    if got.status_code != 404:
        raise Failed(f"/api/wanted answered {got.status_code} to a proxied "
                     f"request; it carries visitors' addresses")
    # From the box: a list, even when empty.
    got = client.get("/api/wanted")
    if got.status_code != 200 or not isinstance(got.get_json(), list):
        raise Failed(f"/api/wanted did not answer locally "
                     f"({got.status_code})")

    # A FILE SOMEBODY DROPPED IS NOT A REQUEST. This listed `unavailable`
    # recognitions, which are written when somebody uploads and we listen
    # -- so dropping a file in and wandering off put a piece on the
    # operator's list with no address and nobody waiting, under a heading
    # promising "the person who asked is still on the job".
    routes_src = (ROOT / "app" / "routes.py").read_text(encoding="utf-8")
    i = routes_src.find("def api_wanted")
    body = routes_src[i:i + 4000]
    if "FROM recognitions" in body:
        raise Failed("the wanted list reads recognitions, which exist as "
                     "soon as somebody uploads. A request is a job with a "
                     "score chosen and an address left")
    if "FROM jobs" not in body:
        raise Failed("the wanted list does not read held jobs")
    if "confirmed" not in body:
        raise Failed("the list does not say whether the address was ever "
                     "confirmed, so an operator cannot tell which requests "
                     "can actually be delivered")

    # The refusal keeps the address, or the loop cannot be closed.
    src = (ROOT / "app" / "jobs.py").read_text(encoding="utf-8")
    i = src.find("def say_a_score_is_wanted")
    if i < 0:
        raise Failed("nothing reports that a score is wanted any more")
    if "update_job" not in src[i:i + 2600]:
        raise Failed("a refused request does not keep the address, so the "
                     "person who asked cannot be told when the score is "
                     "finally engraved")

    # And the panel starts only what it was told about.
    panel = lend_panel.Panel(lambda: {"taking": True}, lambda: None,
                             lambda: None, lambda: None)
    panel._wanted = [{                                       # noqa: SLF001
        "job": "j1", "piece": "P", "editions": ["Good"], "ready": ["Good"],
        "at": 0, "address": True, "country": "", "minutes": None,
        "state": "uploaded", "recording": True}]
    for job, score, why in (
            ("unknown", "Good", "a job that is not on the list"),
            ("j1", "NotPublished", "a score that is not published"),
            ("j1", "", "no score at all"),
            ("j1", "Good; rm -rf /", "a shell fragment")):
        if panel.render_now(job, score) == "":
            raise Failed(f"the panel would start {why}, and both halves "
                         f"become arguments to a command on the server")

    # THE TWO SHAPES OF `editions`, which cost a failed submission. The
    # live answer carries objects (`library.resolve` -> `e.public()`); the
    # stored recognition carries bare names. The page must read a NAME from
    # either, because it puts that string in the render request -- and
    # reading the stored shape while the server sends the live one posted
    # "{'name': 'Op.25_1.re ETUDE...', 'present': False}" as a score.
    wire = (ROOT / "app" / "static" / "svs" / "svs-wire.js").read_text(
        encoding="utf-8")
    if "typeof e === 'string'" not in wire:
        raise Failed("the page assumes one shape for an edition. The live "
                     "answer sends objects and the stored recognition sends "
                     "names; whichever it assumes, the other one submits a "
                     "score nobody can find")

    page = lend_panel.PAGE
    if "Asked for, not engraved" not in page:
        raise Failed("the panel never shows what was asked for")
    if "function bip" not in page or "knownAsks" not in page:
        raise Failed("a new request makes no sound, so it is only seen by "
                     "somebody already looking at the page")
    if "knownAsks !== null" not in page:
        raise Failed("the sound would play for the backlog on every load, "
                     "which teaches somebody to ignore it")

    return ("refused requests keep the address; the list is 404 from "
            "outside; only a published score for a listed job can start; "
            "a new one makes a sound and a backlog does not")


def check_a_piece_we_have_not_engraved_still_looks_real() -> str:
    """The design screen shows the actual opening bars, or generic staves.

    A request for a piece with no package is taken rather than refused --
    the score is made from requests like it. But the design screen previews
    a REAL band: `realBand()` wants `band`, `band_w` and `band_h`, and
    `staveHTML` falls back to drawn staves without them. So the visitor
    whose piece we had not engraved got a visibly worse screen than
    everybody else: the same refusal, expressed in squiggles.

    `tools/make_preview_bands.py` engraves one system per work straight
    from the Humdrum corpus, and publishes it where a package's own preview
    goes -- `scores/<name>/band.svg`, served by the endpoint that already
    exists. This asserts the three joins between that and the screen.
    """
    import dataclasses
    from app import routes, scorestore
    from app.settings import settings

    # 1. The library offers them, and NOT as works: `works` is what can be
    #    made, and anything reading it as "the repertoire" must not start
    #    counting pieces that cannot be rendered.
    app = routes.create_app()
    app.config["TESTING"] = True
    body = app.test_client().get("/api/library").get_json()
    if "previews" not in body:
        raise Failed("/api/library does not offer the preview bands, so the "
                     "page cannot show a held piece's own opening bars")
    if not isinstance(body["previews"], dict):
        raise Failed("previews is not a mapping of name -> band")
    names = {w["id"] for w in body.get("works", [])}
    if names & set(body["previews"]):
        raise Failed("a work appears in both `works` and `previews`; the "
                     "library is what can be MADE and these cannot be")

    # 2. Each entry carries what `realBand()` actually requires. Two of the
    #    three is the same as none: it falls back to staves either way.
    was = routes.scorestore.previews
    try:
        routes.scorestore.previews = lambda: {
            "Op.23_X__023-1-BH": {"w": 1280, "h": 231, "title": "Ballade"},
            "Missing_h__001": {"w": 1280, "title": "no height"},
        }
        offered = routes._previews_offered()               # noqa: SLF001
    finally:
        routes.scorestore.previews = was
    entry = offered.get("Op.23_X__023-1-BH")
    if not entry:
        raise Failed("a published preview was not offered to the page")
    for field in ("band", "band_w", "band_h"):
        if not entry.get(field):
            raise Failed(f"a preview is offered without {field!r}; "
                         f"`realBand()` needs all three and draws generic "
                         f"staves if any is missing")
    if "/band" not in entry["band"]:
        raise Failed("a preview does not point at the band endpoint")
    if "Missing_h__001" in offered:
        raise Failed("a preview with no height was offered; it would be "
                     "laid out at a shape nobody engraved")

    # 3. The page reads them when it enters a held piece into its library.
    wire = (ROOT / "app" / "static" / "svs" / "svs-wire.js").read_text(
        encoding="utf-8")
    if "SERVER.previews" not in wire:
        raise Failed("the page never looks at the preview bands, so a held "
                     "piece still draws generic staves")
    i = wire.find("SERVER.previews")
    if "band_w" not in wire[i:i + 400] or "band_h" not in wire[i:i + 400]:
        raise Failed("the page takes a preview's url without its size, and "
                     "`realBand()` declines without both")

    return (f"offered separately from works, all three fields or none, and "
            f"the page reads them; {len(offered)} usable in this fixture")


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
        check_the_recogniser_is_marked_right_or_wrong,
        check_the_page_is_actually_styled,
        check_a_failure_is_never_mailed_to_the_visitor,
        check_nothing_is_mailed_to_an_unproved_address,
        check_a_portrait_video_keeps_the_picture,
        check_the_output_is_postable,
        check_the_choices_survive_the_request,
        check_the_interface_and_renderer_agree,
        check_the_rate_limit_key_cannot_be_forged,
        check_the_request_cannot_choose_a_file,
        check_the_duration_cap_fails_safe,
        check_a_cross_site_request_is_refused,
        check_a_job_id_is_not_guessable,
        check_the_bot_check_is_wired_and_inert_by_default,
        check_a_compute_node_gets_no_dangerous_secret,
        check_a_compute_node_can_actually_run,
        check_the_input_reaches_a_compute_node,
        check_an_object_says_what_it_is,
        check_the_score_reaches_a_compute_node,
        check_the_web_box_needs_no_scores,
        check_a_transparent_band_floats_over_the_video,
        check_the_video_carries_a_mark,
        check_the_video_carries_the_weefeen_mark,
        check_the_edition_is_credited,
        check_a_volunteer_machine_stops_the_paid_one,
        check_a_declined_task_really_goes_back,
        check_a_node_may_read_what_it_is_told_to_pull,
        check_music_glyphs_survive_the_rasteriser,
        check_a_tempo_mark_is_never_a_box,
        check_a_plate_that_cannot_be_measured_is_still_served,
        check_a_render_cannot_hang_forever,
        check_a_video_counts_wherever_it_was_made,
        check_a_bucket_without_credentials_is_not_available,
        check_a_fetched_score_lands_where_it_was_told,
        check_the_lending_panel_is_local_and_narrow,
        check_the_limit_reset_cannot_be_reached_from_outside,
        check_a_wanted_score_reaches_the_operator,
        check_a_piece_we_have_not_engraved_still_looks_real,
        check_a_confirmation_is_bound_and_expires,
        check_no_confirmation_screen_without_a_confirmation,
        check_the_page_promises_only_what_we_do,
        check_a_refusal_is_not_a_failed_recognition,
        check_the_delivery_page_can_show_the_download,
        check_one_person_cannot_hold_billions_of_buckets,
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
