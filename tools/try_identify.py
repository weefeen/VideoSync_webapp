"""Identify a recording and say what we could render it against.

The whole recognition slice without a browser: configuration check, the
subprocess call into music_finrgerprint, then resolution to an installed
score package.

    python tools/try_identify.py path/to/recording.mp4
    python tools/try_identify.py a.mp4 b.mp4 --expect-refused c.wav
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# Score names carry accents and the Windows console defaults to cp1252,
# which cannot encode them — without this the run dies on a print.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from app import identify as ident              # noqa: E402
from app import library                        # noqa: E402
from app.settings import settings              # noqa: E402

OUTCOME_NOTE = {
    ident.MATCHED: "say it and move on",
    ident.AMBIGUOUS: "ask 'did you mean'",
    ident.UNRECOGNISED: "let them pick from the library",
}


def report(media: pathlib.Path, expect_refused: bool) -> bool:
    print("\n" + "-" * 74)
    print(f"  {media.name}")

    duration = ident.duration_of(media)
    print(f"  {duration:.0f} seconds" if duration else "  length unknown")

    try:
        result = ident.identify(media, duration)
    except ident.TooShort as exc:
        print(f"  REFUSED AT THE DOOR: {exc}")
        return expect_refused
    except ident.IdentifyError as exc:
        print(f"  FAILED: {exc}")
        return False

    t = result.timing
    print(f"  indexes {t.get('load_indexes_s', 0):.1f} s   "
          f"identify {t.get('identify_s', 0):.1f} s")
    print(f"  {result.mode}   consensus {result.consensus:.2f} "
          f"over {result.n_windows} windows   -> {result.outcome} "
          f"({OUTCOME_NOTE[result.outcome]})")

    for candidate in result.candidates[:3]:
        resolved = library.resolve(candidate)
        mark = "->" if resolved["renderable"] else "  "
        print(f"   {mark} {candidate.score:7.3f}  {resolved['label'][:52]}")
        for edition in resolved["editions"]:
            state = ("renderable" if edition["renderable"]
                     else f"unusable: {edition['problem'][:30]}" if edition["present"]
                     else "not installed")
            print(f"           [{state:>16}]  {edition['name'][:54]}")

    if result.outcome == ident.UNRECOGNISED:
        print("  RESULT: not in the collection — refused")
        return expect_refused

    best = library.resolve(result.candidates[0])
    if best["renderable"]:
        print(f"  RESULT: renders against {best['package'][:56]}")
    else:
        print("  RESULT: recognised, but no installed score to render it")
    return not expect_refused


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("media", nargs="*", type=pathlib.Path)
    ap.add_argument("--expect-refused", nargs="*", default=[], type=pathlib.Path,
                    help="recordings that SHOULD be turned away")
    args = ap.parse_args()

    print("=" * 74)
    if not settings.can_identify:
        print(f"  recognition unavailable: {settings.why_cannot_identify()}")
        return 2
    print(f"  index      {settings.id_index_dir}")
    print(f"  runner     {settings.id_python}")
    print(f"  renderable {len(library.packages())} package(s): "
          f"{', '.join(p.name[:40] for p in library.packages()) or 'none'}")

    outcomes = [report(m, False) for m in args.media]
    outcomes += [report(m, True) for m in args.expect_refused]

    passed = sum(outcomes)
    print("\n" + "=" * 74)
    print(f"  {passed}/{len(outcomes)} as expected")
    return 0 if passed == len(outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
