"""Publish the loose assets the watch page needs, for scores already installed.

A package published before the watch page existed has its opening band and
its plates in the bucket and nothing else. Following a performance needs
every system and the alignment besides, so this puts those there.

Additive: it writes `bands/<measure>.svg` and `alignment.data` under the
score's own prefix and re-writes the opening band and plates with identical
bytes. Nothing the rendering side reads is touched, and the package tar is
not rebuilt -- this is the cheap half of `scorestore.publish`.

    python tools/publish_previews.py --check       # say what would be sent
    python tools/publish_previews.py               # send it
    python tools/publish_previews.py NAME [NAME]   # only these
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import library, scorestore, storage      # noqa: E402
from app.settings import settings                 # noqa: E402


def folders() -> dict[str, pathlib.Path]:
    """Every package directory on this machine, by its folder name."""
    found: dict[str, pathlib.Path] = {}
    for root in settings.score_roots:
        if not root.exists:
            continue
        for candidate in sorted(pathlib.Path(root.path).iterdir()):
            if (candidate / "score" / "lines").is_dir():
                found.setdefault(candidate.name, candidate)
    return found


def main(argv: list[str]) -> int:
    dry = "--check" in argv
    wanted = [a for a in argv[1:] if not a.startswith("--")]
    if not storage.available():
        print("no bucket configured here; nothing can be published")
        return 1

    on_disk = folders()
    names = [p.name for p in library.packages()]
    if wanted:
        names = [n for n in names if n in wanted]

    total = 0
    for name in names:
        folder = on_disk.get(name)
        if folder is None:
            print(f"  skip   {name}: not on this machine")
            continue
        bands = sorted((folder / "score" / "lines").glob("*.svg"))
        alignment = next((p for p in (folder / "reference" / "measures.data",
                                      folder / "performance" / "measures.data",
                                      folder / "score" / "measures.data")
                          if p.is_file()), None)
        if dry:
            print(f"  would  {name}: {len(bands)} systems"
                  f"{' + alignment' if alignment else ' (NO alignment)'}")
            total += len(bands) + (1 if alignment else 0)
            continue
        sent = scorestore._put_preview(folder)
        print(f"  sent   {name}: {sent} objects")
        total += sent
    print(f"{total} object(s) {'to send' if dry else 'sent'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
