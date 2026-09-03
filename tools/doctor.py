"""Check that this machine is configured to create videos.

Validates .env, then reports every score package it can find and whether
each is digital (vector bands) or raster. Read-only.

Usage:
    python tools/doctor.py
"""

import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import package as pkg          # noqa: E402
from app.settings import settings       # noqa: E402

OK, BAD, WARN = "  OK  ", " FAIL ", " WARN "


def main() -> int:
    print("=== configuration (.env) ===")
    problems = settings.problems()
    for root in settings.score_roots:
        print(f"[{OK if root.exists else BAD}] {root.kind:8} score root  {root.path}")
    if not settings.score_roots:
        print(f"[{BAD}] no score roots configured")
    for path in settings.video_roots:
        n = len(list(path.glob("*.mp4"))) if path.is_dir() else 0
        print(f"[{OK if path.is_dir() else WARN}] video root          {path}"
              f"{f'  ({n} mp4)' if path.is_dir() else '  (missing)'}")
    print(f"[{OK}] work dir            {settings.work_dir}")

    print("\n=== tools ===")
    for name, exe in (("ffmpeg", settings.ffmpeg), ("ffprobe", settings.ffprobe)):
        found = pathlib.Path(exe).is_file()
        print(f"[{OK if found else BAD}] {name:8} {exe}")
        if found and name == "ffmpeg":
            ver = subprocess.run([exe, "-version"], capture_output=True, text=True)
            print(f"         {ver.stdout.splitlines()[0][:76]}")

    try:
        import cairosvg  # noqa: F401
        print(f"[{OK}] cairosvg  present — .svg bands render at target resolution")
    except ImportError:
        print(f"[{WARN}] cairosvg  missing — .svg bands unsupported, "
              f"raster only (pip install cairosvg)")

    print("\n=== score packages ===")
    usable: dict[str, tuple] = {}
    skipped: dict[str, str] = {}
    for root in settings.score_roots:
        if not root.exists:
            continue
        # The same piece can appear under more than one root; keep the
        # first usable copy, and only report it as skipped if none worked.
        for path, p, reason in pkg.inspect(root.path):
            if p is not None:
                usable.setdefault(p.name, (p, root.kind))
            else:
                skipped.setdefault(path.name, reason)
    skipped = {k: v for k, v in skipped.items() if k not in usable}

    for name, (p, root_kind) in sorted(usable.items()):
        vec = sum(1 for b in p.bands if b.is_vector)
        kind = "digital" if (p.options.get("is_digital") or vec) else root_kind
        w, h = p.band_size
        print(f"[{OK}] {name[:44]:44} {kind:7} "
              f"{len(p.bands):>3} bands ({vec} svg) {w}x{h} "
              f"m1..{p.last_measure} {p.duration / 60:.1f}min")

    for name, reason in sorted(skipped.items()):
        print(f"[{WARN}] {name[:44]:44} {reason}")

    found = len(usable)
    print(f"\n{found} usable, {len(skipped)} skipped")
    if not found:
        print(f"[{WARN}] no packages found under the configured roots")
        print("         A package is a folder with lines/, measures.data and export.json.")
        print("         Produce one from music_line_extractor:")
        print("           python -m services.workflows.cli export_package \\")
        print("             --spj-path=... --project-folder=... --fmt-svg")

    for issue in problems:
        print(f"\n[{BAD}] {issue}")
    print(f"\n{'READY' if not problems and found else 'NOT READY'}")
    return 0 if (not problems and found) else 1


if __name__ == "__main__":
    raise SystemExit(main())
