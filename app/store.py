"""Where jobs live between the moment someone asks and the moment they get it.

Until now the registry was a dictionary. That was honest for a local tool —
its own docstring said so — but it makes three promises the app cannot keep
once more than one person uses it: a queued job survives a restart, a visitor
can be told where they are in the line, and an operator can find out what a
failed stage was given and what it produced.

SQLite rather than a broker, at this step. The work still happens in one
process on one machine, so a table is the whole of what is needed; when the
stages move to their own processes the broker carries *what to do next* and
this keeps carrying *what state is this job in*. The split is deliberate and
survives that change.

Two tables:

    jobs        one row per submission, including everything needed to run it
                after a restart — the style and the metadata are stored, not
                just the identifiers, because a queued job that cannot be
                resumed is a job that was silently dropped.

    stage_runs  one row per attempt at a stage: what it read, what it wrote,
                how long it took, and — when it failed — the exact command,
                the return code and the tail of stderr. This is the table
                that answers "which stage failed, with what inputs, and why",
                and the same rows calibrate the time estimates, so the
                numbers shown to visitors come from what actually happened
                rather than from a constant somebody measured once.

WAL so a reader never blocks the worker, and one connection behind one lock
because at a handful of jobs a day contention is not a problem worth solving.
"""

from __future__ import annotations

import contextlib
import json
import logging
import pathlib
import sqlite3
import threading
import time
from typing import Any, Iterator

from .settings import settings

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    created     REAL NOT NULL,
    name        TEXT NOT NULL,
    upload      TEXT NOT NULL,
    score       TEXT,
    mode        TEXT,
    state       TEXT NOT NULL,
    error       TEXT,
    result      TEXT,
    email       TEXT NOT NULL DEFAULT '',
    client      TEXT NOT NULL DEFAULT '',
    duration    REAL,
    size_bytes  INTEGER,
    style       TEXT,
    meta        TEXT,
    queued_at   REAL,
    started     REAL,
    finished    REAL
);
CREATE INDEX IF NOT EXISTS jobs_state_queued ON jobs(state, queued_at);

CREATE TABLE IF NOT EXISTS stage_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT NOT NULL,
    stage         TEXT NOT NULL,
    attempt       INTEGER NOT NULL DEFAULT 1,
    state         TEXT NOT NULL,
    started       REAL NOT NULL,
    ended         REAL,
    elapsed       REAL,
    inputs        TEXT,
    outputs       TEXT,
    command       TEXT,
    returncode    INTEGER,
    error_class   TEXT,
    error_message TEXT,
    stderr_tail   TEXT,
    bytes_in      INTEGER,
    media_seconds REAL
);
CREATE INDEX IF NOT EXISTS stage_runs_job ON stage_runs(job_id, started);
CREATE INDEX IF NOT EXISTS stage_runs_calib ON stage_runs(stage, state, ended);
"""

# States a job can be in. `queued` is the new one and the point of this
# module: work that has been accepted but has not started.
QUEUED, RUNNING, DONE, ERROR = "queued", "running", "done", "error"
LIVE = (QUEUED, RUNNING)

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def _path() -> pathlib.Path:
    return settings.work_dir / "jobs.sqlite"


def connect() -> sqlite3.Connection:
    """The one connection, opened on first use."""
    global _conn
    with _lock:
        if _conn is None:
            path = _path()
            path.parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False because the worker thread and the
            # request threads share this; every use holds `_lock`.
            _conn = sqlite3.connect(str(path), check_same_thread=False,
                                    timeout=30.0)
            _conn.row_factory = sqlite3.Row
            # WAL lets the page read a job's state while a render is being
            # recorded. Without it a status poll can block on the writer.
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")
            _conn.executescript(SCHEMA)
            _conn.commit()
        return _conn


@contextlib.contextmanager
def write() -> Iterator[sqlite3.Connection]:
    """A transaction. Rolls back if the body raises."""
    with _lock:
        conn = connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def query(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return connect().execute(sql, args).fetchall()


def one(sql: str, args: tuple = ()) -> sqlite3.Row | None:
    rows = query(sql, args)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------

def put_job(row: dict[str, Any]) -> None:
    """Insert or replace one job row. Keys must be columns."""
    keys = sorted(row)
    columns = ", ".join(keys)
    holes = ", ".join("?" for _ in keys)
    with write() as conn:
        conn.execute(f"INSERT OR REPLACE INTO jobs ({columns}) VALUES ({holes})",
                     tuple(row[k] for k in keys))


def update_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in sorted(fields))
    with write() as conn:
        conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?",
                     tuple(fields[k] for k in sorted(fields)) + (job_id,))


def get_job(job_id: str) -> sqlite3.Row | None:
    return one("SELECT * FROM jobs WHERE id = ?", (job_id,))


def waiting() -> list[sqlite3.Row]:
    """Queued jobs, oldest first — the line, in the order it will be served."""
    return query("SELECT * FROM jobs WHERE state = ? ORDER BY queued_at",
                 (QUEUED,))


def running() -> sqlite3.Row | None:
    return one("SELECT * FROM jobs WHERE state = ? ORDER BY started", (RUNNING,))


def unfinished() -> list[sqlite3.Row]:
    """Everything that was accepted and never reached a conclusion.

    Read at start-up. A job left `running` by a crash did not survive, but a
    job left `queued` never began, so it can simply be served now.
    """
    return query("SELECT * FROM jobs WHERE state IN (?, ?) ORDER BY queued_at",
                 LIVE)


def position(job_id: str) -> int | None:
    """How many jobs are ahead of this one. 0 means it is next; None if it
    is not waiting (already running, finished, or unknown)."""
    row = get_job(job_id)
    if row is None or row["state"] != QUEUED:
        return None
    ahead = one("SELECT COUNT(*) AS n FROM jobs WHERE state = ? AND queued_at < ?",
                (QUEUED, row["queued_at"] or 0))
    return int(ahead["n"]) if ahead else 0


# ---------------------------------------------------------------------------
# stage_runs — the debugging record, and the calibration data
# ---------------------------------------------------------------------------

def stage_begin(job_id: str, stage: str, *, attempt: int = 1,
                inputs: list[str] | None = None,
                bytes_in: int | None = None,
                media_seconds: float | None = None) -> int:
    """Record that a stage started. Returns the row id to finish with."""
    with write() as conn:
        cur = conn.execute(
            "INSERT INTO stage_runs (job_id, stage, attempt, state, started,"
            " inputs, bytes_in, media_seconds) VALUES (?,?,?,?,?,?,?,?)",
            (job_id, stage, attempt, RUNNING, time.time(),
             json.dumps(inputs or []), bytes_in, media_seconds))
        return int(cur.lastrowid)


def stage_end(run_id: int, *, state: str = DONE,
              outputs: list[str] | None = None,
              command: str | None = None,
              returncode: int | None = None,
              error: BaseException | None = None,
              stderr_tail: str | None = None) -> None:
    """Close a stage row.

    `command` and `stderr_tail` matter more than they look: a render's ffmpeg
    invocation is assembled from the visitor's own crop, colours and panel
    choices, so without the exact command a failure cannot be reproduced.
    """
    now = time.time()
    with write() as conn:
        started = conn.execute("SELECT started FROM stage_runs WHERE id = ?",
                               (run_id,)).fetchone()
        elapsed = (now - started["started"]) if started else None
        conn.execute(
            "UPDATE stage_runs SET state=?, ended=?, elapsed=?, outputs=?,"
            " command=?, returncode=?, error_class=?, error_message=?,"
            " stderr_tail=? WHERE id = ?",
            (state, now, elapsed, json.dumps(outputs or []), command,
             returncode,
             type(error).__name__ if error else None,
             str(error) if error else None,
             stderr_tail, run_id))


def stage_runs(job_id: str) -> list[sqlite3.Row]:
    return query("SELECT * FROM stage_runs WHERE job_id = ? ORDER BY started",
                 (job_id,))


def rate(stage: str, minimum: int = 5) -> float | None:
    """Measured seconds of work per second of media, for one stage.

    None until there are enough finished samples to mean anything — the
    caller then falls back to its documented default. This is what stops an
    estimate quoted to a visitor from resting forever on a constant measured
    once on a developer's machine.
    """
    rows = query(
        "SELECT elapsed, media_seconds FROM stage_runs"
        " WHERE stage = ? AND state = ? AND elapsed > 0 AND media_seconds > 0"
        " ORDER BY ended DESC LIMIT 50", (stage, DONE))
    ratios = sorted(r["elapsed"] / r["media_seconds"] for r in rows)
    if len(ratios) < minimum:
        return None
    return ratios[len(ratios) // 2]          # median, not mean: one stall
                                             # should not move the estimate
