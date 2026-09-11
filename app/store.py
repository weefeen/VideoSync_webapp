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

Three tables:

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

    recognitions one row per answer from the recogniser: the piece it named,
                how far ahead of the runners-up it was, how long the recording
                ran, and the country it came from. It carries no address —
                that lives on the job row and nowhere else, so deleting
                somebody's recording deletes it. What is left here is what
                people play and where from, which outlives any one upload.

WAL so a reader never blocks the worker, and one connection behind one lock
because at a handful of jobs a day contention is not a problem worth solving.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
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
    finished    REAL,
    -- Higher goes first. Everything is 0 today; an institution running an
    -- event to a schedule cannot sit behind forty public uploads, and
    -- adding this column later means migrating a table with history in it.
    priority    INTEGER NOT NULL DEFAULT 0,
    -- Which worker took it. Useless with one worker and necessary with two,
    -- which is the point: the second one should need no schema change.
    worker      TEXT,
    lease_until REAL,
    -- Where the render has got to, and the line under it. These were held
    -- on the in-memory Job and so were visible only inside the process
    -- doing the work: every status poll rebuilt the Job from this table and
    -- got "nothing has started yet" for the whole render. Once the worker
    -- is a separate process there is nowhere else for them to live.
    stage       TEXT,
    detail      TEXT NOT NULL DEFAULT '',
    -- Bumped when the same job is submitted again, so a late message from
    -- the previous run cannot be mistaken for this one's.
    attempt     INTEGER NOT NULL DEFAULT 1,
    -- When the work was handed to the queue. NULL on a queued row means the
    -- handover has not been confirmed, which is what lets a lost message be
    -- noticed rather than waited on forever.
    published_at REAL,
    -- Where the finished video lives in the bucket. NULL means local disk
    -- only. `result` stays as it was: the path on the machine that made it,
    -- which is still what a re-run checks and what a local install serves.
    object_key  TEXT
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(state, priority DESC, queued_at);

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
    media_seconds REAL,
    -- CPU seconds this stage actually burned, the worker AND the ffmpeg it
    -- spawned. Wall time says how long you waited; this says whether the
    -- machine was working or blocked, and the ratio between them is how
    -- many cores a stage really uses.
    -- CPU seconds THIS stage burned: a delta, not a running total. The
    -- worker can only report a cumulative figure, so the value it reported
    -- when the stage opened is kept below and subtracted at the close.
    cpu_seconds   REAL,
    cpu_at_open   REAL,
    -- The high-water mark of resident memory, in bytes, as of the end of
    -- this stage — process and children together. CUMULATIVE, not per
    -- stage: the kernel does not reset it. So the number itself is the peak
    -- so far, and the RISE from the previous stage is what that stage cost.
    -- Recorded this way because it needs no sampling thread to be exact.
    peak_rss      INTEGER
);
CREATE INDEX IF NOT EXISTS stage_runs_job ON stage_runs(job_id, started);
CREATE INDEX IF NOT EXISTS stage_runs_calib ON stage_runs(stage, state, ended);

-- What the recogniser was asked, and what it answered. One row per
-- identification, not per job: the same upload can be listened to twice, and
-- an answer that changed between two attempts is the interesting one.
--
-- Separate from `jobs` because the two hold different facts. `jobs.score` is
-- the score package a render was finally run against — which the visitor can
-- override, and which stays empty for anyone who listened and then left.
-- This is what the machine said, before anybody agreed with it.
CREATE TABLE IF NOT EXISTS recognitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL,
    at          REAL NOT NULL,
    -- Where it came from, and DELIBERATELY NOT the address. The address
    -- lives in exactly one place — the job row — so that deleting somebody's
    -- recording on request deletes it, with no second copy to remember.
    -- What is kept here is the coarse location, resolved once at the moment
    -- of the upload: it is what the question "where should the servers be"
    -- actually needs, it survives the recording being deleted, and it is not
    -- personal data. Which visitor did what is still answerable while the
    -- job row exists, by joining on job_id.
    country     TEXT NOT NULL DEFAULT '',
    city        TEXT NOT NULL DEFAULT '',
    -- matched | unavailable | unrecognised | error. `unavailable` means it
    -- was named and no score is installed for it — the answer that says what
    -- to engrave next.
    outcome     TEXT NOT NULL,
    -- The piece the recogniser named, in its own vocabulary. Never the
    -- edition: two visitors playing the same Ballade must land on one row
    -- however their scores were published.
    piece_id    TEXT,
    title       TEXT,
    -- Its share of the whole ranked list, as a percentage — the "compared to
    -- the others" figure. 100 means nothing else came close.
    confidence  REAL,
    -- How much of the recording agreed with itself, and over how many
    -- windows. Confidence says the winner beat the field; these say whether
    -- the field was worth beating.
    consensus   REAL,
    windows     INTEGER,
    -- Seconds of music. Held here as well as on the job so that "how long is
    -- this piece, across everyone who has played it" is one GROUP BY.
    duration    REAL,
    -- The runners-up, as JSON, for the times the winner was wrong.
    candidates  TEXT
);
CREATE INDEX IF NOT EXISTS recognitions_piece ON recognitions(piece_id, at);
CREATE INDEX IF NOT EXISTS recognitions_where ON recognitions(country, at);

-- The compute node's life: at most one row, ever.
--
-- `CHECK (singleton = 1)` on the primary key is what enforces that. SQLite
-- serialises writers across processes, so a second scaler started by mistake
-- gets a constraint error and stands down rather than creating a second
-- machine — the local half of the guard, with the provider's label
-- uniqueness as the final referee.
--
-- Today nothing creates a machine. The row is kept in SHADOW: every scrape
-- records what a scaler WOULD have decided, so the trigger can be watched
-- against real traffic before it is given the power to spend money, and
-- COMPUTE_GRACE_SECONDS can be settled from evidence rather than guessed.
-- The same row and the same arithmetic become the real thing; only the part
-- that calls the provider is missing.
CREATE TABLE IF NOT EXISTS compute (
    singleton   INTEGER PRIMARY KEY CHECK (singleton = 1),
    -- none | wanted | creating | running | draining. In shadow only the
    -- first two are ever written.
    state       TEXT NOT NULL DEFAULT 'none',
    since       REAL,
    -- When work last ran out. NULL while there is work. The grace period is
    -- measured from here, and it is the single largest cost lever there is.
    idle_since  REAL,
    -- Accumulated seconds a node would have existed. Against the plan's
    -- hourly rate this is the monthly bill, and it is the number the whole
    -- scale-to-zero design exists to make small.
    would_run   REAL NOT NULL DEFAULT 0,
    -- What actually exists at the provider, as last seen.
    machine_id    INTEGER,
    machine_label TEXT,
    -- The last thing the scaler refused to act on, and when it
    -- last said so. Without these the tick would mail on every
    -- pass: at 30-second ticks that is 120 identical messages
    -- an hour, which is the same as sending none.
    alarm         TEXT,
    alarm_at      REAL,
    creates     INTEGER NOT NULL DEFAULT 0,
    destroys    INTEGER NOT NULL DEFAULT 0,
    updated     REAL
);

-- One row per decision, with the reason. Prometheus cannot hold a reason —
-- a distinct message per decision is a new series each time — so the counts
-- live there and the WHY lives here, keyed by time so the two read together.
CREATE TABLE IF NOT EXISTS compute_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      REAL NOT NULL,
    action  TEXT NOT NULL,          -- would-create | would-destroy
    reason  TEXT NOT NULL,
    ready   INTEGER NOT NULL DEFAULT 0,
    unacked INTEGER NOT NULL DEFAULT 0,
    idle    REAL
);
CREATE INDEX IF NOT EXISTS compute_events_at ON compute_events(at);
"""

# How close to the end of a paid hour a machine may be released. Linode
# rounds partial hours up, so the last minutes of an hour are already bought
# and there is nothing to save by giving them back early; this only has to be
# long enough that the destroy completes before the next hour starts.
RELEASE_WINDOW = 180.0

# Below this much history, the arrival rate is arithmetic rather than
# evidence and the machine is released regardless. A fortnight is enough to
# have seen each hour of the day twice over on a weekday and a weekend.
MIN_DAYS_OF_EVIDENCE = 7

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
            # BEFORE the schema, not after. SCHEMA ends with
            # `CREATE INDEX ... ON recognitions(country, at)`, and an index
            # on a column that does not exist yet is an error, not a no-op —
            # so a database written before that column was added could not be
            # opened at all, and the migration that would have fixed it ran
            # one line too late to be reached. `_migrate` already skips a
            # table that does not exist, so a new database is unaffected.
            _migrate(_conn)
            _conn.executescript(SCHEMA)
            _conn.commit()
        return _conn


# Columns added after the table already existed somewhere. They are in
# SCHEMA as well, so a new database gets them from the CREATE and this does
# nothing; an existing one gets them here.
_ADDED = (
    ("jobs", "stage", "TEXT"),
    ("jobs", "detail", "TEXT NOT NULL DEFAULT ''"),
    ("jobs", "attempt", "INTEGER NOT NULL DEFAULT 1"),
    ("jobs", "published_at", "REAL"),
    ("jobs", "object_key", "TEXT"),
    # The recognitions table once kept `client` — the visitor's address —
    # and now keeps the place it resolved to instead, so that deleting a
    # recording deletes the address with it. An older database still has the
    # old column, holding addresses this design says it must not hold.
    ("recognitions", "country", "TEXT NOT NULL DEFAULT ''"),
    ("recognitions", "city", "TEXT NOT NULL DEFAULT ''"),
    ("compute", "machine_id", "INTEGER"),
    ("compute", "machine_label", "TEXT"),
    ("compute", "alarm", "TEXT"),
    ("compute", "alarm_at", "REAL"),
    ("stage_runs", "cpu_seconds", "REAL"),
    ("stage_runs", "cpu_at_open", "REAL"),
    ("stage_runs", "peak_rss", "INTEGER"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an older database up to the current columns.

    Every statement in SCHEMA is `CREATE TABLE IF NOT EXISTS`, which does
    nothing at all to a table that already exists — so a database written
    before a column was added never gains it. `ALTER TABLE ADD COLUMN` is
    the only way in and SQLite has no `IF NOT EXISTS` for it, hence reading
    `table_info` first. Cheap: it runs once per process, on first connect.
    """
    for table in dict.fromkeys(t for t, _, _ in _ADDED):
        have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        # An empty result means the table does not exist, not that it has no
        # columns — and SCHEMA is about to create it with everything already
        # in place. Without this the loop reads "no columns present" and
        # tries to ALTER a table that is not there.
        if not have:
            continue
        for owner, column, definition in _ADDED:
            if owner == table and column not in have:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                logger.info("added %s.%s to an existing database", table, column)


def close() -> None:
    """Let go of the database file. The next call reopens it.

    Only tests and shutdown need this, and it exists because of Windows:
    there an open file cannot be deleted, so a temporary WORK_DIR cannot be
    cleaned up while this connection is held. On Linux the unlink would
    simply succeed and nothing would ever have asked for it.
    """
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


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


def write_returning(sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    """A statement that changes rows and reports which. Committed.

    `query` deliberately does not commit, so using it for an UPDATE loses
    the change at the next connection close — a mistake worth making
    impossible rather than remembering.
    """
    with write() as conn:
        return conn.execute(sql, args).fetchall()


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
    """The line, in the order it will actually be served.

    The ordering is duplicated in `claim_next` and in `position`, and all
    three have to agree: a page that says "you are next" while the worker
    takes somebody else is worse than showing no position at all.
    """
    return query("SELECT * FROM jobs WHERE state = ?"
                 " ORDER BY priority DESC, queued_at, id", (QUEUED,))


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
    is not waiting (already running, finished, or unknown).

    "Ahead" follows the order work is actually taken in, so a higher
    priority counts as ahead even when it arrived later. A queue position
    that does not match the order of service is worse than none.
    """
    row = get_job(job_id)
    if row is None or row["state"] != QUEUED:
        return None
    # `id` breaks the tie because two jobs really can share a queued_at:
    # time.time() moves in ~15 ms steps on Windows, and two submissions in
    # the same step would otherwise each be told they were ahead of the
    # other. Arbitrary is fine; disagreeing is not.
    ahead = one(
        "SELECT COUNT(*) AS n FROM jobs WHERE state = ? AND ("
        "  priority > ?"
        "  OR (priority = ? AND queued_at < ?)"
        "  OR (priority = ? AND queued_at = ? AND id < ?))",
        (QUEUED, row["priority"],
         row["priority"], row["queued_at"] or 0,
         row["priority"], row["queued_at"] or 0, row["id"]))
    return int(ahead["n"]) if ahead else 0


# `claim_next` was here: one atomic UPDATE...RETURNING that handed the next
# queued job to whichever worker asked first. The queue itself is the claim
# now — a task is delivered to one consumer — and keeping a second way to
# take a job would be an invitation to use it and get two workers on one
# render by two different routes.


def reclaim_expired() -> list[str]:
    """Put back jobs whose worker stopped saying it was alive.

    Called at start-up and periodically. A render cannot be resumed from the
    middle, so this returns the job to the queue rather than pretending the
    work survived — a second attempt is cheaper than a job that is silently
    nobody's.
    """
    now = time.time()
    with write() as conn:
        rows = conn.execute(
            "UPDATE jobs SET state = ?, worker = NULL, lease_until = NULL,"
            " started = NULL WHERE state = ? AND lease_until IS NOT NULL"
            " AND lease_until < ? RETURNING id", (QUEUED, RUNNING, now)).fetchall()
    return [r["id"] for r in rows]


def renew(job_id: str, lease_seconds: float = 3600.0) -> None:
    """A worker saying it is still there."""
    update_job(job_id, lease_until=time.time() + lease_seconds)


def set_progress(job_id: str, stage: str | None, detail: str,
                 lease_seconds: float = 3600.0) -> None:
    """Where the work has got to, and that it is still going.

    One statement rather than a `set_stage` and a `renew`, because these two
    facts are the same fact: a stage that moved is a worker that is alive,
    and writing them apart would let a status poll land between them and
    read a job that had advanced but looked abandoned.
    """
    update_job(job_id, stage=stage, detail=detail,
               lease_until=time.time() + lease_seconds)


def mark_published(job_id: str) -> None:
    """The queue has taken the work. Until this, a lost handover is possible."""
    update_job(job_id, published_at=time.time())


def unpublished(older_than: float = 30.0) -> list[sqlite3.Row]:
    """Jobs that are waiting but were never handed over.

    A publish that failed leaves exactly this: a row that says `queued` while
    nothing anywhere intends to render it. Silent, and permanent, until
    somebody asks why their video never came.

    The age is what keeps the sweep off a job that was queued a moment ago
    and is being published right now.
    """
    return query("SELECT * FROM jobs WHERE state = ? AND published_at IS NULL"
                 " AND queued_at < ? ORDER BY priority DESC, queued_at, id",
                 (QUEUED, time.time() - older_than))


def forget_publications() -> int:
    """Mark every queued job as never handed over. Returns how many.

    For a transport that does not survive a restart: whatever was holding
    those tasks went with the process, so the sweep must offer them again.
    """
    rows = write_returning(
        "UPDATE jobs SET published_at = NULL WHERE state = ? RETURNING id",
        (QUEUED,))
    return len(rows)


# ---------------------------------------------------------------------------
# stage_runs — the debugging record, and the calibration data
# ---------------------------------------------------------------------------

def stage_begin(job_id: str, stage: str, *, attempt: int = 1,
                inputs: list[str] | None = None,
                bytes_in: int | None = None,
                media_seconds: float | None = None,
                cpu_at_open: float | None = None) -> int:
    """Record that a stage started. Returns the row id to finish with."""
    with write() as conn:
        cur = conn.execute(
            "INSERT INTO stage_runs (job_id, stage, attempt, state, started,"
            " inputs, bytes_in, media_seconds, cpu_at_open)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (job_id, stage, attempt, RUNNING, time.time(),
             json.dumps(inputs or []), bytes_in, media_seconds, cpu_at_open))
        return int(cur.lastrowid)


def stage_end(run_id: int, *, state: str = DONE,
              outputs: list[str] | None = None,
              command: str | None = None,
              returncode: int | None = None,
              error: BaseException | None = None,
              error_class: str | None = None,
              error_message: str | None = None,
              stderr_tail: str | None = None,
              cpu_seconds: float | None = None,
              peak_rss: int | None = None) -> None:
    """Close a stage row.

    `command` and `stderr_tail` matter more than they look: a render's ffmpeg
    invocation is assembled from the visitor's own crop, colours and panel
    choices, so without the exact command a failure cannot be reproduced.

    The failure arrives either as the exception itself, from a caller in the
    same process, or as the two strings, from one that heard about it from
    somewhere else — an exception does not cross a process boundary, and the
    record must read the same either way.
    """
    now = time.time()
    if error is not None:
        error_class = error_class or type(error).__name__
        error_message = error_message or str(error)
    with write() as conn:
        was = conn.execute("SELECT started, cpu_at_open FROM stage_runs"
                           " WHERE id = ?", (run_id,)).fetchone()
        elapsed = (now - was["started"]) if was else None
        # The caller passes what the worker last reported, which is a
        # running total for the whole attempt. This stage's share is the
        # rise since it opened.
        if cpu_seconds is not None and was and was["cpu_at_open"] is not None:
            cpu_seconds = max(0.0, round(cpu_seconds - was["cpu_at_open"], 3))
        conn.execute(
            "UPDATE stage_runs SET state=?, ended=?, elapsed=?, outputs=?,"
            " command=?, returncode=?, error_class=?, error_message=?,"
            " stderr_tail=?, cpu_seconds=?, peak_rss=? WHERE id = ?",
            (state, now, elapsed, json.dumps(outputs or []), command,
             returncode, error_class, error_message, stderr_tail,
             cpu_seconds, peak_rss, run_id))


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


# ---------------------------------------------------------------------------
# recognitions — what was played, and how sure we were
# ---------------------------------------------------------------------------

def put_recognition(job_id: str, *, country: str = "", city: str = "",
                    outcome: str,
                    piece_id: str | None = None, title: str | None = None,
                    confidence: float | None = None,
                    consensus: float | None = None,
                    windows: int | None = None,
                    duration: float | None = None,
                    candidates: list | None = None) -> int:
    """Record one answer from the recogniser. Returns the row id.

    Appended, never replaced. A second listen to the same upload is a second
    row, because the pair of them is the evidence that the answer is not
    stable — and overwriting the first would destroy exactly that.
    """
    with write() as conn:
        cur = conn.execute(
            "INSERT INTO recognitions (job_id, at, country, city, outcome,"
            " piece_id, title, confidence, consensus, windows, duration,"
            " candidates) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, time.time(), country or "", city or "", outcome,
             piece_id, title,
             confidence, consensus, windows, duration,
             json.dumps(candidates or [])))
        return int(cur.lastrowid)


def recognition_for(job_id: str) -> sqlite3.Row | None:
    """The latest thing the recogniser said about this upload.

    The in-memory copy in `routes._identifications` does not survive a
    restart, so anything that has to be true about a job days after it was
    uploaded reads this instead.
    """
    return one("SELECT * FROM recognitions WHERE job_id = ?"
               " ORDER BY at DESC LIMIT 1", (job_id,))


def recognitions(limit: int = 500) -> list[sqlite3.Row]:
    """The most recent answers, newest first."""
    return query("SELECT * FROM recognitions ORDER BY at DESC LIMIT ?", (limit,))


def pieces() -> list[sqlite3.Row]:
    """Every piece that has ever been recognised here, with its statistics.

    Length is reported as a range and a median rather than a mean: a piece
    played twice, once complete and once as a fragment somebody stopped
    recording, has no meaningful average — and the two numbers say so where
    one number would hide it.

    Rows with no duration are counted in `times` but cannot contribute a
    length, so `lengths` is reported separately. Two different denominators,
    and quietly sharing one is how a statistic becomes wrong.
    """
    return query(
        "SELECT piece_id, MAX(title) AS title,"
        "       COUNT(*) AS times,"
        # Distinct RECORDINGS, not distinct people: this table holds no
        # address, so "how many different players" is not a question it can
        # answer. Two listens to one upload count once here and twice in
        # `times`, and the pair of numbers is the useful thing.
        "       COUNT(DISTINCT job_id) AS uploads,"
        "       COUNT(duration) AS lengths,"
        "       MIN(duration) AS shortest,"
        "       MAX(duration) AS longest,"
        "       AVG(confidence) AS confidence,"
        "       MIN(at) AS first_at, MAX(at) AS last_at"
        " FROM recognitions WHERE piece_id IS NOT NULL AND piece_id != ''"
        " GROUP BY piece_id ORDER BY times DESC, piece_id")


def piece_durations(piece_id: str) -> list[float]:
    """Every recorded length for one piece, in order. For the median."""
    return [r["duration"] for r in query(
        "SELECT duration FROM recognitions WHERE piece_id = ?"
        " AND duration IS NOT NULL ORDER BY duration", (piece_id,))]


def listens_by_client() -> list[sqlite3.Row]:
    """How many times each address had something identified, and what.

    Joined through `jobs`, because the recognition row holds no address —
    only the place. So this answers "which visitor" for exactly as long as
    the job row exists, and stops answering it the moment somebody's
    recording is deleted. That is the intended behaviour, not a limitation.
    """
    return query("""
        SELECT j.client AS client,
               COUNT(*) AS listens,
               SUM(r.outcome = 'matched') AS matched,
               COUNT(DISTINCT r.piece_id) AS pieces
          FROM recognitions r JOIN jobs j ON j.id = r.job_id
         WHERE j.client != ''
         GROUP BY j.client""")


def places() -> list[sqlite3.Row]:
    """Uploads by country and city, from the recognition rows.

    The long-lived answer to "where should the servers be". Survives any
    individual recording being deleted, because it never held an address.
    """
    return query("""
        SELECT country, city, COUNT(*) AS listens,
               COALESCE(SUM(duration), 0) AS seconds,
               MIN(at) AS first_at, MAX(at) AS last_at
          FROM recognitions WHERE country != ''
         GROUP BY country, city ORDER BY listens DESC""")


def visitors() -> list[sqlite3.Row]:
    """One row per address, with what it has done here.

    Uploads and recognitions are counted from their own tables rather than
    from a join: an address that listened five times and never rendered would
    otherwise be multiplied by its own recognitions and read as five uploads.
    """
    return query("""
        SELECT client,
               COUNT(*) AS uploads,
               MIN(created) AS first_seen,
               MAX(created) AS last_seen,
               SUM(state = 'done') AS delivered,
               SUM(state = 'error') AS failed,
               COALESCE(SUM(duration), 0) AS seconds,
               COALESCE(SUM(size_bytes), 0) AS bytes
          FROM jobs WHERE client != ''
         GROUP BY client ORDER BY last_seen DESC""")

# ---------------------------------------------------------------------------
# compute — what a scaler would do, before it is allowed to do it
# ---------------------------------------------------------------------------

def arrival_rate(hour_of_day: int, days: int = 14) -> tuple[float, int]:
    """Jobs per hour historically seen in THIS hour of the day.

    Returns (rate, days_of_evidence). The second number matters as much as
    the first: a rate computed from two days of a new site is not evidence
    of anything, and the caller must be able to tell "quiet" from "we do not
    know yet".

    Hour of day rather than a flat average, because that is how this traffic
    will actually behave. People upload a recital recording in the evening,
    not at four in the morning, and a machine kept alive through the night
    on last night's average is a machine paid for to do nothing.
    """
    now = time.time()
    since = now - days * 86400
    rows = query("SELECT created FROM jobs WHERE created >= ?", (since,))

    # Only count days the install was actually alive, or a site that was
    # switched off for a week looks quiet rather than absent.
    oldest = one("SELECT MIN(created) AS c FROM jobs")
    first = (oldest["c"] if oldest and oldest["c"] else now)
    observed = max(1.0, min(days, (now - first) / 86400))

    seen = 0
    for row in rows:
        if time.gmtime(row["created"]).tm_hour == hour_of_day:
            seen += 1
    return seen / observed, int(observed)


def compute_row() -> sqlite3.Row:
    """The one compute row, created empty on first read."""
    row = one("SELECT * FROM compute WHERE singleton = 1")
    if row is None:
        with write() as conn:
            conn.execute("INSERT OR IGNORE INTO compute (singleton, state)"
                         " VALUES (1, 'none')")
        row = one("SELECT * FROM compute WHERE singleton = 1")
    return row


def compute_tick(grace_seconds: float,
                 keep_if_arrivals: float = 1.0) -> dict[str, Any]:
    """Decide what a scaler would do now, and remember it. Returns the state.

    THE SCRAPE IS THE TICK. Prometheus polls every 30 s, which is a fine
    resolution for a decision whose grace period is ten minutes, and it means
    no extra thread exists to be forgotten about. When the real scaler
    arrives it calls this on its own schedule and acts on the answer; the
    arithmetic does not change.

    `ready` and `unacked` come from the job table rather than the broker's
    management API, which is not enabled on this host and belongs to a shared
    broker. The mapping is exact for this purpose — a queued job is a message
    waiting, a running job is a message a worker holds — and it fails in the
    SAFE direction: a worker that dies leaves its row `running` until the
    lease expires, so the answer is "still busy" and a real scaler would
    decline to destroy rather than destroy something live.
    """
    now = time.time()
    counts = {r["state"]: r["n"] for r in query(
        "SELECT state, COUNT(*) AS n FROM jobs GROUP BY state")}
    ready = int(counts.get(QUEUED, 0))
    unacked = int(counts.get(RUNNING, 0))
    busy = ready + unacked

    row = compute_row()
    state = row["state"]
    idle_since = row["idle_since"]
    would_run = float(row["would_run"] or 0.0)
    creates = int(row["creates"] or 0)
    destroys = int(row["destroys"] or 0)
    updated = row["updated"]

    # A node that would exist has been existing since the last tick. Counted
    # before any transition, so the seconds are attributed to the state the
    # node was actually in.
    if state == "wanted" and updated:
        would_run += max(0.0, min(now - updated, 3600.0))

    event = None
    if busy > 0:
        idle_since = None
        if state != "wanted":
            state, creates = "wanted", creates + 1
            event = ("would-create",
                     f"{ready} waiting and {unacked} running, and no machine")
    else:
        if idle_since is None:
            idle_since = now
        if state == "wanted":
            # HOUR-ALIGNED, because that is how the provider bills. Linode
            # rounds every partial hour UP to a whole one, so a machine that
            # has been up for six minutes has already cost a full hour.
            # Destroying it at ten idle minutes therefore throws away
            # fifty minutes that are paid for, makes the next visitor wait
            # ~2 minutes for a fresh boot, AND bills a second hour if work
            # arrives before the first one is up.
            #
            # So the question is not "has it been idle long enough" but
            # "is the hour we bought nearly over". Idle time still has to
            # clear `grace_seconds` first: a machine released seconds after
            # a job ends, merely because the hour happened to be ending,
            # would be torn down in exactly the moment a second upload is
            # most likely.
            began = row["since"] or now
            paid_until = began + math.ceil(max(now - began, 1) / 3600.0) * 3600
            near_the_boundary = (paid_until - now) <= RELEASE_WINDOW
            if near_the_boundary:
                # THE QUESTION AT THE BOUNDARY is not "has it been idle long
                # enough" but "is more work coming". Those are different, and
                # a fixed idle threshold answers the wrong one: a job that
                # finished five minutes ago says nothing about whether
                # another is due.
                #
                # Keeping never saves money — the hour a job runs in is paid
                # for either way — it only saves the next visitor a ~94s
                # boot. So DESTROYING IS THE DEFAULT, and the machine is kept
                # only where history says this hour of the day is genuinely
                # busy enough that it would be working anyway.
                hour = time.gmtime(now).tm_hour
                rate, observed = arrival_rate(hour)
                # Too new to have an opinion. Destroy: an unproven guess
                # should cost a boot, not an hour.
                enough_history = observed >= MIN_DAYS_OF_EVIDENCE
                busy_hour = enough_history and rate >= keep_if_arrivals
                if busy_hour:
                    event = None
                else:
                    state, destroys = "none", destroys + 1
                    reason = (f"{rate:.1f} jobs/h usually at {hour:02d}:00 "
                              f"over {observed}d")
                    if not enough_history:
                        reason = (f"only {observed}d of history, not enough "
                                  f"to justify holding an hour")
                    event = ("would-destroy",
                             f"paid hour ending, nothing to do, and {reason}")

    with write() as conn:
        conn.execute(
            "UPDATE compute SET state=?, since=?, idle_since=?, would_run=?,"
            " creates=?, destroys=?, updated=? WHERE singleton = 1",
            (state, row["since"] if state == row["state"] else now,
             idle_since, round(would_run, 3), creates, destroys, now))
        if event:
            conn.execute(
                "INSERT INTO compute_events (at, action, reason, ready,"
                " unacked, idle) VALUES (?,?,?,?,?,?)",
                (now, event[0], event[1], ready, unacked,
                 None if idle_since is None else round(now - idle_since, 1)))

    began = row["since"] or now
    paid_until = began + math.ceil(max(now - began, 1) / 3600.0) * 3600
    result = {"state": state, "ready": ready, "unacked": unacked,
              "paid_left": max(0.0, paid_until - now) if state == "wanted" else 0.0,
              "idle_seconds": 0.0 if idle_since is None else now - idle_since,
              "would_run": would_run, "creates": creates,
              "destroys": destroys}
    # Only when the decision actually FLIPPED, never on the ticks in between.
    # The tick runs on every scrape; mailing per tick would be two an hour
    # forever and the messages would stop being read, which is the same as
    # not sending them.
    if event:
        result["event"] = {"action": event[0], "reason": event[1]}
    return result


def compute_events(limit: int = 100) -> list[sqlite3.Row]:
    """The decisions, newest first."""
    return query("SELECT * FROM compute_events ORDER BY at DESC LIMIT ?",
                 (limit,))


def compute_creates_this_hour() -> int:
    """How many machines have actually been created in the last hour.

    The ceiling is counted from what WAS created, not from what the scaler
    remembers intending. A process that restarts in a loop would otherwise
    reset its own count each time and create without limit — which is the
    exact failure the ceiling exists to prevent.
    """
    row = one("SELECT COUNT(*) AS n FROM compute_events"
              " WHERE action = 'created' AND at >= ?", (time.time() - 3600,))
    return int(row["n"]) if row else 0


def compute_record_create(machine_id: int, label: str) -> None:
    """A machine really exists now. Written immediately, before anything
    else can fail: a create that is not recorded is a machine nothing
    remembers and nothing will ever delete."""
    with write() as conn:
        conn.execute(
            "INSERT INTO compute_events (at, action, reason, ready, unacked)"
            " VALUES (?,?,?,?,?)",
            (time.time(), "created", f"{label} ({machine_id})", 0, 0))
        conn.execute("UPDATE compute SET machine_id = ?, machine_label = ?"
                     " WHERE singleton = 1", (machine_id, label))


def compute_record_destroy(machine_id: int) -> None:
    with write() as conn:
        conn.execute(
            "INSERT INTO compute_events (at, action, reason, ready, unacked)"
            " VALUES (?,?,?,?,?)",
            (time.time(), "destroyed", str(machine_id), 0, 0))
        conn.execute("UPDATE compute SET machine_id = NULL,"
                     " machine_label = NULL WHERE singleton = 1")


def compute_alarm(kind: str, repeat_after: float = 3600.0) -> bool:
    """Record a condition the scaler will not act on. True if it is worth
    telling somebody.

    The tick runs every thirty seconds and a stuck condition stays stuck, so
    the naive version sends 120 identical messages an hour — which is the
    same as sending none, because nobody reads the hundredth. This returns
    True the first time a condition appears, and then at most once an hour
    while it persists.

    Clearing is explicit rather than implicit: `compute_alarm_cleared()` is
    called when a tick completes normally, so the NEXT occurrence is treated
    as new and reported at once rather than waiting out the hour.
    """
    now = time.time()
    row = compute_row()
    same = (row["alarm"] or "") == kind
    last = row["alarm_at"] or 0.0
    tell = (not same) or (now - last) >= repeat_after
    if tell:
        with write() as conn:
            conn.execute("UPDATE compute SET alarm = ?, alarm_at = ?"
                         " WHERE singleton = 1", (kind, now))
    return tell


def compute_alarm_cleared() -> str:
    """A tick completed normally. Returns what had been wrong, if anything."""
    row = compute_row()
    was = row["alarm"] or ""
    if was:
        with write() as conn:
            conn.execute("UPDATE compute SET alarm = NULL, alarm_at = NULL"
                         " WHERE singleton = 1")
    return was
