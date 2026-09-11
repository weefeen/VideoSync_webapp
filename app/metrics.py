"""What the pipeline is doing, in the format Prometheus reads.

Everything here comes out of `jobs` and `stage_runs` — the tables the ledger
already writes. Nothing is counted twice, nothing is kept in memory, and
nothing has to be scraped from the machine that did the work.

**That last part is the design.** The renderer runs on a host that is
created for a job and destroyed afterwards. A counter living in that
process dies with it, and Prometheus cannot scrape a machine that no longer
exists — so the worker reports its costs in the events it already sends,
the ledger writes them here, and this reads them back on the always-on box.
A destroyed instance's history survives because it was never on the
instance.

The text format is written by hand rather than through a client library.
It is a dozen lines of string formatting against a specification that has
not changed in a decade, and the alternative is a dependency in the web
process for that.

NOT FOR THE PUBLIC. Prometheus reaches it over the loopback; whatever
fronts the site must not expose /metrics.
"""
from __future__ import annotations

import logging
import threading

from . import limits
from . import notify
from . import store
from .settings import settings

logger = logging.getLogger(__name__)

# The stage a job is in tells you where the queue is stuck, so every stage
# is listed even at zero — a series that only appears under load is a
# series that is missing exactly when somebody is looking.
from .pipeline import STAGES

STATES = (store.QUEUED, store.RUNNING, store.DONE, store.ERROR)
WHOLE = "render"


def _line(name: str, value, labels: dict[str, str] | None = None) -> str:
    if labels:
        inside = ",".join(f'{k}="{v}"' for k, v in labels.items())
        name = f"{name}{{{inside}}}"
    return f"{name} {value}"


def render() -> str:
    """The whole exposition. One database read per family, no caching.

    Cheap enough to leave uncached: these are aggregates over a table with
    one row per stage per job, which after a year of this app's traffic is
    still a few thousand rows.
    """
    out: list[str] = []

    def family(name: str, kind: str, help_text: str) -> None:
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} {kind}")

    # ── the queue, right now ────────────────────────────────────────────
    counts = {row["state"]: row["n"] for row in store.query(
        "SELECT state, COUNT(*) AS n FROM jobs GROUP BY state")}
    family("vsw_jobs", "gauge", "Jobs by state.")
    for state in STATES:
        out.append(_line("vsw_jobs", counts.get(state, 0), {"state": state}))

    family("vsw_jobs_waiting", "gauge",
           "Jobs accepted and not yet started. The number a visitor is behind.")
    out.append(_line("vsw_jobs_waiting", counts.get(store.QUEUED, 0)))

    # Where the running ones have got to. This is the answer to "what is it
    # doing" without reading a log.
    running = {row["stage"]: row["n"] for row in store.query(
        "SELECT stage, COUNT(*) AS n FROM jobs WHERE state = ?"
        " GROUP BY stage", (store.RUNNING,))}
    family("vsw_jobs_in_stage", "gauge", "Running jobs, by the stage they are in.")
    for stage in STAGES:
        out.append(_line("vsw_jobs_in_stage", running.get(stage, 0),
                         {"stage": stage}))

    # ── what each stage costs ───────────────────────────────────────────
    # Sums and counts rather than an average, so Prometheus can average over
    # whatever window is being looked at instead of over all history.
    rows = store.query(
        "SELECT stage, state, COUNT(*) AS n,"
        "       COALESCE(SUM(elapsed), 0) AS wall,"
        "       COALESCE(SUM(cpu_seconds), 0) AS cpu,"
        "       COALESCE(MAX(peak_rss), 0) AS rss,"
        "       COALESCE(SUM(media_seconds), 0) AS media"
        " FROM stage_runs WHERE ended IS NOT NULL GROUP BY stage, state")

    family("vsw_stage_runs_total", "counter",
           "Finished stage runs, by stage and how they ended.")
    family("vsw_stage_seconds_total", "counter",
           "Wall-clock seconds spent in a stage. Divide by the run count for "
           "an average, or by the job total for a share.")
    family("vsw_stage_cpu_seconds_total", "counter",
           "CPU seconds a stage burned, worker and ffmpeg together. Against "
           "wall time this is how many cores the stage actually uses.")
    family("vsw_stage_peak_rss_bytes", "gauge",
           "Highest resident memory reached by the end of a stage, ever. "
           "Cumulative for the process, so the rise between stages is what "
           "that stage cost.")
    for row in rows:
        labels = {"stage": row["stage"], "state": row["state"]}
        out.append(_line("vsw_stage_runs_total", row["n"], labels))
        out.append(_line("vsw_stage_seconds_total", round(row["wall"], 3), labels))
        out.append(_line("vsw_stage_cpu_seconds_total", round(row["cpu"], 3), labels))
        out.append(_line("vsw_stage_peak_rss_bytes", int(row["rss"]),
                         {"stage": row["stage"]}))

    # ── the pipeline as a whole ─────────────────────────────────────────
    whole = store.query(
        "SELECT COUNT(*) AS n, COALESCE(SUM(elapsed), 0) AS wall,"
        "       COALESCE(SUM(media_seconds), 0) AS media,"
        "       COALESCE(SUM(bytes_in), 0) AS bytes_in,"
        "       COALESCE(MAX(peak_rss), 0) AS rss"
        " FROM stage_runs WHERE stage = ? AND state = ? AND ended IS NOT NULL",
        (WHOLE, store.DONE))[0]

    family("vsw_pipeline_seconds_total", "counter",
           "Wall-clock seconds of finished jobs.")
    out.append(_line("vsw_pipeline_seconds_total", round(whole["wall"], 3)))
    family("vsw_pipeline_media_seconds_total", "counter",
           "Seconds of music rendered. Against the line above this is the "
           "realtime factor — the number every estimate rests on.")
    out.append(_line("vsw_pipeline_media_seconds_total",
                     round(whole["media"], 3)))
    family("vsw_pipeline_bytes_in_total", "counter",
           "Bytes of uploaded video rendered, for seconds-per-gigabyte.")
    out.append(_line("vsw_pipeline_bytes_in_total", int(whole["bytes_in"])))
    family("vsw_peak_rss_bytes", "gauge",
           "The largest memory any render has ever reached on this host.")
    out.append(_line("vsw_peak_rss_bytes", int(whole["rss"])))

    # How full the bucket is. 250 GB comes with the flat monthly fee, so
    # this is the number that says when storage stops being free-at-the-
    # margin and a colder tier starts being worth its complexity — a
    # decision that should arrive as a graph, not as a surprise on a bill.
    stored = store.one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS bytes"
        " FROM jobs WHERE object_key IS NOT NULL AND object_key != ''")
    family("vsw_stored_objects", "gauge",
           "Finished videos confirmed in the bucket.")
    out.append(_line("vsw_stored_objects", stored["n"] if stored else 0))
    family("vsw_stored_bytes", "gauge",
           "Bytes of finished video in the bucket, from the recorded sizes. "
           "Against the 250 GB included in the flat fee, this is how much "
           "runway is left before storage costs anything extra.")
    out.append(_line("vsw_stored_bytes", int(stored["bytes"]) if stored else 0))
    family("vsw_storage_included_bytes", "gauge",
           "Storage included in the monthly fee, so the graph carries the "
           "line it is being judged against.")
    out.append(_line("vsw_storage_included_bytes", 250 * 1000 ** 3))

    family("vsw_videos_delivered_total", "counter",
           "Videos finished. Counted at delivery, never at submission.")
    out.append(_line("vsw_videos_delivered_total", whole["n"]))


    # ── the compute node, in shadow ─────────────────────────────────────
    # Nothing creates a machine yet. Every scrape records what a scaler
    # WOULD have decided, so the trigger can be watched against real traffic
    # before it is given the power to spend money — and so the grace period
    # is chosen from evidence rather than guessed. THE SCRAPE IS THE TICK.
    shadow = store.compute_tick(settings.compute_grace_seconds)
    if shadow.get("event"):
        _tell_the_operator(shadow)

    family("vsw_queue_ready", "gauge",
           "Jobs accepted and not yet started — what a scaler reads as "
           "messages_ready. Above zero means a machine is wanted.")
    out.append(_line("vsw_queue_ready", shadow["ready"]))
    family("vsw_queue_unacked", "gauge",
           "Jobs a worker is holding — messages_unacknowledged. Zero means "
           "every finished result is safely stored, which is the condition "
           "that makes destroying a host safe.")
    out.append(_line("vsw_queue_unacked", shadow["unacked"]))

    family("vsw_compute_wanted", "gauge",
           "1 when a compute node would exist right now, 0 when it would "
           "not. Nothing acts on this yet.")
    out.append(_line("vsw_compute_wanted",
                     1 if shadow["state"] == "wanted" else 0))

    family("vsw_compute_idle_seconds", "gauge",
           "How long there has been no work at all. A node is torn down "
           "once this passes the grace period, so watching it against real "
           "traffic is what says whether the grace period is right.")
    out.append(_line("vsw_compute_idle_seconds",
                     round(shadow["idle_seconds"], 1)))

    family("vsw_compute_grace_seconds", "gauge",
           "The configured grace period, so the graph carries the line it "
           "is being judged against.")
    out.append(_line("vsw_compute_grace_seconds",
                     settings.compute_grace_seconds))

    family("vsw_compute_would_create_total", "counter",
           "Times a machine would have been created.")
    out.append(_line("vsw_compute_would_create_total", shadow["creates"]))
    family("vsw_compute_would_destroy_total", "counter",
           "Times a machine would have been destroyed.")
    out.append(_line("vsw_compute_would_destroy_total", shadow["destroys"]))

    family("vsw_compute_would_run_seconds_total", "counter",
           "Seconds a machine would have existed. Against the plan's hourly "
           "rate this is the bill, and it is the number the whole "
           "scale-to-zero design exists to keep small.")
    out.append(_line("vsw_compute_would_run_seconds_total",
                     round(shadow["would_run"], 1)))

    family("vsw_compute_paid_seconds_left", "gauge",
           "Seconds left in the hour already paid for. The provider rounds "
           "partial hours up, so a machine is released near zero rather than "
           "when it happens to go idle.")
    out.append(_line("vsw_compute_paid_seconds_left",
                     round(shadow.get("paid_left", 0.0), 1)))

    family("vsw_compute_hourly_cost", "gauge",
           "What an hour of the chosen plan costs, so the dashboard can turn "
           "the seconds above into money without the figure being hidden in "
           "a query.")
    out.append(_line("vsw_compute_hourly_cost", settings.compute_hourly_cost))

    return "\n".join(out) + "\n"


def failures(limit: int = 50) -> list[dict]:
    """Recent failures, with enough to act on rather than only a count.

    Deliberately NOT metrics. A Prometheus label holding an error message
    or an ffmpeg command line is unbounded cardinality — one new series per
    distinct failure — which is how a monitoring system is brought down by
    the thing it is monitoring. Counts belong there; the reason belongs
    here, keyed by job so the two can be read together.

    The command matters more than it looks: a render's ffmpeg invocation is
    assembled from the visitor's own crop, colours and panel choices, so
    without it a failure cannot be reproduced by hand.
    """
    failed = store.query(
        "SELECT id, created, score, error FROM jobs WHERE state = ?"
        " ORDER BY created DESC LIMIT ?", (store.ERROR, limit))

    out = []
    for job in failed:
        # Prefer the row for the stage it died in; fall back to the
        # whole-job row. Jobs from before per-stage timing existed have only
        # the latter, and dropping them would quietly hide the oldest
        # failures — which are the ones nobody has looked at yet.
        runs = [r for r in store.stage_runs(job["id"])
                if r["state"] == store.ERROR]
        detail = next((r for r in runs if r["stage"] != WHOLE),
                      next(iter(runs), None))

        tail = ((detail["stderr_tail"] if detail else "") or "").strip().splitlines()
        out.append({
            # Milliseconds: what Grafana reads as a time axis.
            "time": int((job["created"] or 0) * 1000),
            "job": job["id"],
            "score": job["score"] or "",
            "stage": (detail["stage"] if detail and detail["stage"] != WHOLE
                      else "not recorded"),
            "attempt": (detail["attempt"] if detail else 1) or 1,
            "error": (detail["error_class"] if detail else "") or "",
            "message": ((detail["error_message"] if detail else None)
                        or job["error"] or "")[:400],
            "returncode": detail["returncode"] if detail else None,
            "command": ((detail["command"] if detail else "") or "")[:600],
            # The last few lines are where ffmpeg says what it objected to;
            # the rest is banner.
            "stderr": "\n".join(tail[-6:]),
        })
    return out


def compute_decisions(limit: int = 100) -> list[dict]:
    """Why a machine would have been created or destroyed.

    Deliberately NOT metrics, for the same reason as `failures`: a reason is
    text, and text as a Prometheus label is one new series per distinct
    message. The counts belong there; the why belongs here, keyed by time so
    the two are read together.
    """
    return [{
        "time": int((r["at"] or 0) * 1000),
        "action": r["action"],
        "reason": r["reason"],
        "ready": r["ready"],
        "unacked": r["unacked"],
        "idle_seconds": r["idle"],
    } for r in store.compute_events(limit)]


def _tell_the_operator(shadow: dict) -> None:
    """Mail the operator when a machine would come up or go away.

    On a thread and never allowed to raise: this is called from the metrics
    endpoint, and a monitoring scrape that fails because a mail server was
    slow would take the dashboard down with it — the instrument breaking
    because of the thing it is instrumenting.
    """
    event = shadow["event"]
    hours = shadow["would_run"] / 3600.0

    def work() -> None:
        try:
            if not limits.allowed("mail_compute", "all"):
                logger.info("not mailing the compute notice: over the daily cap")
                return
            notify.send_compute(
                event["action"], event["reason"],
                ready=shadow["ready"], unacked=shadow["unacked"],
                # Until a provider call exists, nothing is really created.
                # Saying otherwise would be a lie in the one channel that has
                # to stay trustworthy.
                shadow=True,
                hours=hours, cost=hours * settings.compute_hourly_cost)
        except Exception:                             # noqa: BLE001
            logger.warning("could not send the compute notice", exc_info=True)

    threading.Thread(target=work, name="compute-mail", daemon=True).start()
