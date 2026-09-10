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

from . import store

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

    family("vsw_videos_delivered_total", "counter",
           "Videos finished. Counted at delivery, never at submission.")
    out.append(_line("vsw_videos_delivered_total", whole["n"]))

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
