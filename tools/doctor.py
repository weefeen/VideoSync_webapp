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
from app import svg               # noqa: E402
from app.settings import settings       # noqa: E402

OK, BAD, WARN = "  OK  ", " FAIL ", " WARN "


def case_trouble(root: pathlib.Path) -> list[tuple[str, str]]:
    """Parts of a package that only match if case is ignored.

    `app/package.py` looks for fixed names — `score/lines`, `measures.data`,
    `export.json`. Windows finds those whatever the case on disk; Linux does
    not. So a package built or renamed on a Windows machine can load
    perfectly there, be copied to the server, and simply not be found: the
    piece is reported as having no bands, or no alignment, with nothing
    saying why.

    That is the same silent shape as the recognition bug — right on one
    platform, quietly wrong on the other — so it is worth naming here rather
    than waiting for a render that produces nothing.

    Returns (what was looked for, what is actually on disk).
    """
    wanted: set[str] = set()
    for group in (pkg._LINES_AT, pkg._MEASURES_AT, pkg._MANIFEST_AT,
                  pkg._CHROMA_AT, pkg._SOURCE_AT):
        wanted.update(group)

    found: list[tuple[str, str]] = []
    for relative in sorted(wanted):
        here = root
        parts = relative.split("/")
        for i, part in enumerate(parts):
            # Compare against the real directory entries, NOT with
            # `exists()`. On a case-insensitive filesystem `exists()` says
            # yes for `score/lines` when the folder is called `Lines`, so
            # the mismatch this exists to find would never be reached — the
            # first version of this check made exactly that mistake and
            # reported every package clean.
            try:
                names = {entry.name: entry for entry in here.iterdir()}
            except OSError:
                break
            if part in names:
                here = names[part]
                continue
            match = next((entry for name, entry in names.items()
                          if name.lower() == part.lower()), None)
            if match is not None:
                found.append(("/".join(parts[:i + 1]),
                              match.relative_to(root).as_posix()))
            break
    return found


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

    # Asked through app.svg rather than by importing cairosvg here: on
    # Windows the native library only loads after the conda environment's
    # Libraryin is put on the path, which app.svg does — and it fails
    # with OSError rather than ImportError, so a narrower except let the
    # doctor die on the exact machine it exists to diagnose.
    if svg.available():
        print(f"[{OK}] cairosvg  present — .svg bands render at target resolution")
    else:
        print(f"[{WARN}] cairosvg  unavailable — .svg bands cannot be rasterised")
        print(f"         {svg.why_unavailable()}")

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

    raster_only: list[str] = []
    carrying_png: list[tuple[str, int, int]] = []
    for name, (p, root_kind) in sorted(usable.items()):
        vec = sum(1 for b in p.bands if b.is_vector)
        kind = "digital" if (p.options.get("is_digital") or vec) else root_kind
        w, h = p.band_size
        print(f"[{OK}] {name[:44]:44} {kind:7} "
              f"{len(p.bands):>3} bands ({vec} svg) {w}x{h} "
              f"m1..{p.last_measure} {p.duration / 60:.1f}min")
        if not vec:
            raster_only.append(name)
        # Bitmaps beside the vectors are dead weight: nothing reads them
        # once cairo works, and they are four fifths of what has to be
        # copied to a machine that renders.
        bitmaps = [f for f in p.root.rglob("lines/*")
                   if f.suffix.lower() in (".png", ".jpg", ".jpeg")]
        if bitmaps and vec:
            carrying_png.append(
                (name, len(bitmaps), sum(f.stat().st_size for f in bitmaps)))

    # Only meaningful on a case-insensitive filesystem, which is where the
    # mistake gets made and never noticed.
    miscased: list[tuple[str, str, str]] = []
    for name, (p, _kind) in sorted(usable.items()):
        for wanted, actual in case_trouble(p.root):
            miscased.append((name, wanted, actual))

    for name, reason in sorted(skipped.items()):
        print(f"[{WARN}] {name[:44]:44} {reason}")

    found = len(usable)
    print(f"\n{found} usable, {len(skipped)} skipped")

    # The library is meant to be SVG throughout: vectors recolour, scale to
    # any frame, and compress about tenfold, so a package is a couple of
    # megabytes rather than a hundred. These two checks say when a folder
    # has drifted from that, which is otherwise invisible until a render
    # looks wrong or a machine spends a minute copying pictures nothing reads.
    if raster_only:
        print(f"\n[{WARN}] {len(raster_only)} package(s) have NO svg bands.")
        print("         They cannot be recoloured or scaled cleanly, and the")
        print("         result will be softer than the rest of the library:")
        for name in raster_only[:8]:
            print(f"           {name}")

    if carrying_png:
        total = sum(size for _, _, size in carrying_png)
        print(f"\n[{WARN}] {len(carrying_png)} package(s) carry bitmap bands "
              f"beside their svg — {total / 1e6:.0f} MB that nothing reads.")
        for name, n, size in sorted(carrying_png, key=lambda r: -r[2])[:8]:
            print(f"           {name[:44]:44} {n:>3} files  {size / 1e6:6.1f} MB")
        print("         Deleting them costs nothing and makes each package "
              "roughly forty times smaller.")
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
