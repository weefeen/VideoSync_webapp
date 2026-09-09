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

from app import package as pkg, pipeline, render as rnd            # noqa: E402
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
    layout = ap.add_argument_group("layout")
    layout.add_argument("--aspect", default="16/9", choices=sorted(rnd.ASPECTS))
    layout.add_argument("--position", default="bottom", choices=[rnd.TOP, rnd.BOTTOM],
                        help="where the score band sits")
    layout.add_argument("--background", default=rnd.NONE, choices=rnd.BACKGROUNDS,
                        help="none = plain colour; static = an image; "
                             "dynamic = a looping video")
    layout.add_argument("--background-path", help="image or video for the backdrop")
    layout.add_argument("--panel", action="store_true",
                        help="reserve a left column for the title text")
    layout.add_argument("--panel-width", type=float, default=0.301,
                        help="panel width as a fraction of the canvas (default 0.301)")
    layout.add_argument("--canvas-bg", default="#141019")
    layout.add_argument("--band-bg", default="#ffffff", help="the band's paper colour")
    layout.add_argument("--band-fg", default="#1c1622", help="the note colour")
    layout.add_argument("--band-bg-opacity", type=float, default=1.0,
                        help="paper transparency: 0 = notes float over the "
                             "backdrop, 1 = solid (default 1.0)")
    layout.add_argument("--band-opacity", type=float, default=1.0,
                        help="fades the whole band, notes included (default 1.0)")

    info = ap.add_argument_group("panel text (only drawn with --panel)")
    for field in ("round-name", "subtitle", "first-name", "last-name",
                  "country", "age", "composer", "composition"):
        info.add_argument(f"--{field}", default="")
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
            ref = "reference" if (p.root / "reference").is_dir() else "no reference"
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
                      background=args.background,
                      background_path=args.background_path,
                      panel=args.panel, panel_width=args.panel_width,
                      canvas_bg=args.canvas_bg,
                      band_bg=args.band_bg, band_fg=args.band_fg,
                      band_bg_opacity=args.band_bg_opacity,
                      band_opacity=args.band_opacity)
    meta = {k: getattr(args, k) for k in
            ("round_name", "subtitle", "first_name", "last_name",
             "country", "age", "composer", "composition")}

    print(f"score : {p.name}")
    print(f"bands : {len(p.bands)} "
          f"({sum(1 for b in p.bands if b.is_vector)} svg)")
    print(f"video : {video.name}")
    print(f"style : {args.aspect} · band {args.position} · bg {args.background}"
          f"{' · panel' if args.panel else ''}")
    print(f"job   : {job_dir}")

    t0 = time.time()
    try:
        result = pipeline.run(p, video, job_id, style, args.mode, meta,
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
