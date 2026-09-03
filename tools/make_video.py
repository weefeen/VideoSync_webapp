"""Create a synced score video from the command line.

The same code path the web app uses. Aligns the recording (against the
score, or against the package's reference recording) and composites the
score band onto it.

Usage:
    python tools/make_video.py --list
    python tools/make_video.py --score "Op.39" --video "...mp4" [--mode auto|reference]
"""

import argparse
import pathlib
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import autosync, package as pkg, pipeline, render as rnd   # noqa: E402
from app.settings import settings                                   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true",
                    help="list score packages and what each supports")
    ap.add_argument("--score", help="package name, or a unique fragment")
    ap.add_argument("--video", help="the performance video")
    ap.add_argument("--mode", choices=pipeline.MODES,
                    help="auto = align to the score; reference = align to the "
                         "reference recording (default: reference when available)")
    ap.add_argument("--seconds", type=float,
                    help="use only the first N seconds (quick check)")
    ap.add_argument("--job", help="job id (default: random)")
    ap.add_argument("--keep", action="store_true",
                    help="keep the job workspace after a failure")
    ap.add_argument("--aspect", default="16/9", choices=sorted(rnd.ASPECTS))
    ap.add_argument("--position", default="bottom", choices=["top", "bottom"])
    ap.add_argument("--band-width", type=float, default=1.0)
    ap.add_argument("--band-bg", default="#ffffff")
    ap.add_argument("--band-fg", default="#1c1622")
    args = ap.parse_args()

    settings.ensure_dirs()

    if args.list:
        # A piece can sit under more than one root; report it once.
        usable: dict[str, pkg.ScorePackage] = {}
        skipped: dict[str, str] = {}
        for root in settings.score_roots:
            for path, p, reason in pkg.inspect(root.path):
                if p is not None:
                    usable.setdefault(p.name, p)
                else:
                    skipped.setdefault(path.name, reason)
        for name, p in sorted(usable.items()):
            ref = "reference+auto" if autosync.has_reference(p.root) else "auto only"
            vec = sum(1 for b in p.bands if b.is_vector)
            fmt = f"{vec} svg" if vec else f"{len(p.bands)} raster"
            print(f"OK   {name[:46]:46} {len(p.bands):>3} bands ({fmt:>10})  {ref}")
        for name, reason in sorted(skipped.items()):
            if name not in usable:
                print(f"--   {name[:46]:46} {reason}")
        if not settings.can_autosync:
            print("\nNOTE: MLE_ROOT / MLE_PYTHON not set — alignment unavailable.")
        return 0

    if not args.score or not args.video:
        ap.error("--score and --video are required (or use --list)")

    p = pipeline.find_package(args.score)
    if p is None:
        print(f"No usable package matching {args.score!r}. Try --list.")
        return 1

    video = pathlib.Path(args.video)
    if not video.is_file():
        print(f"No such video: {video}")
        return 1

    # Default the workspace to the score's own name, so a job's outputs sit
    # beside the package they came from and are easy to find.
    job_id = args.job or p.name
    job_dir = pipeline.job_folder(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)

    if args.seconds:
        clip = job_dir / f"clip{video.suffix}"
        print(f"trimming first {args.seconds}s -> {clip.name}")
        subprocess.run([settings.ffmpeg, "-y", "-i", str(video), "-t",
                        str(args.seconds), "-c", "copy", str(clip)],
                       capture_output=True, check=True)
        video = clip

    style = rnd.Style(aspect=args.aspect, band_position=args.position,
                      band_width=args.band_width,
                      band_bg=args.band_bg, band_fg=args.band_fg)

    print(f"score : {p.name}")
    print(f"bands : {len(p.bands)} "
          f"({sum(1 for b in p.bands if b.is_vector)} svg)")
    print(f"video : {video.name}")
    print(f"job   : {job_dir}")

    t0 = time.time()
    try:
        result = pipeline.run(p, video, job_id, style, args.mode,
                              on_progress=lambda s, d="": print(
                                  f"  [{s:8}] {d}", flush=True))
    except pipeline.PipelineError as exc:
        print(f"\nFAILED: {exc}")
        if not args.keep:
            print(f"(workspace kept at {job_dir})")
        return 1

    print(f"\nmode   : {pipeline.MODE_LABELS[result.mode]}")
    print(f"aligned: {result.measures}")
    print(f"wrote  : {result.output}")
    print(f"took   : {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
