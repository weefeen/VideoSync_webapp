"""Render one clip across many style combinations.

Alignment dominates the runtime of a job, so this reuses an alignment that
already exists and varies only the styling — which is what a layout matrix
is actually testing. Every combination is rendered and a frame extracted,
so the results can be eyeballed together.

Usage:
    python tools/matrix.py --score "Op.39" --clip <mp4> --measures <measures.data>
"""

import argparse
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import pipeline, render as rnd          # noqa: E402
from app.settings import settings                # noqa: E402

META = {
    "round_name": "Preliminary", "subtitle": "ROUND RECITAL",
    "first_name": "Rafal", "last_name": "Blechacz",
    "country": "Poland", "age": "20",
    "composer": "CHOPIN",
    "composition": "Scherzo No. 3 in C-sharp minor, Op. 39",
}

# (label, aspect, background, band position, panel, extra style kwargs)
CASES = [
    ("none-16x9-bottom",      "16/9", rnd.NONE,    rnd.BOTTOM, False, {}),
    ("none-16x9-top",         "16/9", rnd.NONE,    rnd.TOP,    False, {}),
    ("static-16x9-top-panel", "16/9", rnd.STATIC,  rnd.TOP,    True,  {}),
    ("dynamic-16x9-top-panel","16/9", rnd.DYNAMIC, rnd.TOP,    True,  {}),
    ("none-1x1-bottom",       "1/1",  rnd.NONE,    rnd.BOTTOM, False, {}),
    ("none-9x16-bottom",      "9/16", rnd.NONE,    rnd.BOTTOM, False, {}),
    ("static-1x1-top-panel",  "1/1",  rnd.STATIC,  rnd.TOP,    True,  {}),
    ("dynamic-9x16-panel",    "9/16", rnd.DYNAMIC, rnd.BOTTOM, True,
     {"band_fg": "#ffffff", "band_bg_opacity": 0.0}),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--score", required=True)
    ap.add_argument("--clip", required=True, help="short video to render")
    ap.add_argument("--measures", required=True,
                    help="an existing measures.data for that clip")
    ap.add_argument("--static", help="image for --background static")
    ap.add_argument("--dynamic", help="video for --background dynamic")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    settings.ensure_dirs()
    p = pipeline.find_package(args.score)
    if p is None:
        print(f"No package matching {args.score!r}")
        return 1
    p = p.with_alignment(pathlib.Path(args.measures))
    clip = pathlib.Path(args.clip)
    out_dir = pathlib.Path(args.out) if args.out else settings.work_dir / "matrix"
    (out_dir / "frames").mkdir(parents=True, exist_ok=True)

    print(f"score {p.name}\nclip  {clip.name}\n"
          f"bands {len(p.bands)} ({sum(1 for b in p.bands if b.is_vector)} svg)\n")

    failures = 0
    for label, aspect, background, position, use_panel, extra in CASES:
        path = {rnd.STATIC: args.static, rnd.DYNAMIC: args.dynamic}.get(background)
        if background != rnd.NONE and not path:
            print(f"  {label:26} SKIP  (no --{background} supplied)")
            continue

        style = rnd.Style(aspect=aspect, background=background,
                          background_path=path, band_position=position,
                          panel=use_panel, **extra)
        target = out_dir / f"{label}.mp4"
        t0 = time.time()
        try:
            rnd.render(p, clip, target, style, META)
        except rnd.RenderError as exc:
            failures += 1
            print(f"  {label:26} FAIL  {str(exc).splitlines()[0][:70]}")
            continue

        layout = rnd.compute_layout(style, p.band_size[0] / p.band_size[1],
                                    rnd.probe(clip)["aspect"])
        subprocess.run([settings.ffmpeg, "-y", "-i", str(target), "-ss", "6",
                        "-frames:v", "1", "-q:v", "3",
                        str(out_dir / "frames" / f"{label}.jpg")],
                       capture_output=True)
        print(f"  {label:26} OK    {layout.canvas[0]}x{layout.canvas[1]}  "
              f"band {layout.band.w}x{layout.band.h}  "
              f"video {layout.video.w}x{layout.video.h}  "
              f"{target.stat().st_size / 1e6:4.1f} MB  {time.time() - t0:4.1f}s")

    print(f"\n{len(CASES) - failures}/{len(CASES)} rendered -> {out_dir}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
