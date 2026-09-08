"""Drop a video in, get a scored video out. The whole slice, no browser.

    recognise -> resolve to a package -> align -> render

Nothing is typed in: the score is chosen by listening to the recording.
Alignment is against the package's own reference, so the result belongs to
this performance rather than to whoever recorded the reference.

    python tools/try_render.py recording.mp4 [--seconds 60] [--keep]
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from app import identify as ident              # noqa: E402
from app import library                        # noqa: E402
from app import package as pkg                 # noqa: E402
from app import render as rnd                  # noqa: E402
from app import sync                           # noqa: E402
from app.settings import settings              # noqa: E402


def step(n: int, title: str) -> None:
    print(f"\n{'-' * 74}\n  {n}. {title}", flush=True)


def trim(source: pathlib.Path, seconds: float, into: pathlib.Path) -> pathlib.Path:
    """A short excerpt, so a first run costs a minute rather than ten."""
    out = into / f"excerpt{source.suffix}"
    subprocess.run(
        [settings.ffmpeg, "-y", "-loglevel", "error", "-i", str(source),
         "-t", str(seconds), "-c", "copy", str(out)], check=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("media", type=pathlib.Path)
    ap.add_argument("--seconds", type=float, default=0,
                    help="render only the first N seconds")
    ap.add_argument("--keep", action="store_true",
                    help="leave the job folder in place afterwards")
    args = ap.parse_args()

    for name, ready, why in (("recognition", settings.can_identify,
                              settings.why_cannot_identify()),
                             ("alignment", settings.can_sync,
                              settings.why_cannot_sync())):
        if not ready:
            print(f"  {name} unavailable: {why}")
            return 2

    job_dir = settings.work_dir / "try" / time.strftime("%H%M%S")
    job_dir.mkdir(parents=True, exist_ok=True)
    media = args.media
    started = time.perf_counter()

    print("=" * 74)
    print(f"  {media.name}")
    print(f"  job {job_dir}")

    step(1, "listen, and name the piece")
    result = ident.identify(media)
    print(f"     {result.mode}, consensus {result.consensus:.2f} over "
          f"{result.n_windows} windows -> {result.outcome}")
    if result.outcome != ident.MATCHED:
        print("     not recognised — nothing to render against")
        return 1

    best = library.resolve(result.candidates[0])
    print(f"     {best['label']}")
    if not best["renderable"]:
        print("     recognised, but no installed score to render it")
        return 1
    package_root = library.find(best["package"]).root
    print(f"     using {best['package']}")
    if len(best["editions"]) > 1:
        print(f"     (of {len(best['editions'])} editions this recording backs)")

    if args.seconds:
        media = trim(media, args.seconds, job_dir)
        print(f"     trimmed to {args.seconds:.0f}s for this run")

    step(2, "align this performance to that score")
    try:
        alignment = sync.align(package_root, media, job_dir)
    except sync.PartialRecording as exc:
        print(f"     REFUSED: {exc}")
        return 1
    t = alignment.timing
    print(f"     {alignment.measures} measures, "
          f"{alignment.first_measure}..{alignment.last_measure}, "
          f"{alignment.starts_at:.1f}..{alignment.ends_at:.1f}s")
    print(f"     crowding {alignment.crowding:.3f}, "
          f"span {alignment.span_ratio:.3f} of the reference")
    print(f"     chroma {t.get('chroma_s', 0):.1f}s, warp {t.get('align_s', 0):.1f}s")

    step(3, "render the band into the video")
    timed = pkg.load(package_root).with_alignment(alignment.measures_path)
    output = job_dir / "synced.mp4"
    rnd.render(timed, media, output, rnd.Style(), {},
               lambda stage, detail="": print(f"     {stage:<8} {detail}", flush=True))

    size = output.stat().st_size / 1e6
    print(f"\n{'=' * 74}")
    print(f"  {output}")
    print(f"  {size:.1f} MB in {time.perf_counter() - started:.0f}s total")
    if not args.keep:
        shutil.rmtree(job_dir / "sync", ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
