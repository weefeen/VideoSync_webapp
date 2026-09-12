"""Check a score package before it goes anywhere near the server.

    python tools/check_score.py "<project folder>"
    python tools/check_score.py "<project folder>" --install

Checking is the default and installing is the flag, because the expensive
mistake is a package that LOOKS installed and cannot be used: a missing
reference chroma means every alignment against it fails, and a folder name
the recogniser does not know means nobody ever reaches the score at all.
Both are silent at install time and only show up when a visitor uploads.

WHAT IT KNOWS, learned from installing the first two by hand:

  * `chroma.npy` sits in `performance/` in a project and the loader only
    looks under `score/`. Nothing complains; alignment simply has no
    reference.
  * a project keeps its alignment in `performance/`, an installed package in
    `reference/`. Both are accepted by the loader, but only one is what the
    installed packages look like, so the checker says which it found.
  * `reference/audio.wav` is 94 MB and NOTHING IN THE APP READS IT. It is
    excluded from the install rather than shipped and left to rot.
  * the folder name must be a key in `pair_list.json` EXACTLY. That is how a
    recognised piece resolves to a score, and a near-miss resolves to
    nothing.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.package import IMAGE_SUFFIXES  # noqa: E402
from app.settings import settings  # noqa: E402

SERVER = "root@172.104.237.127"
REMOTE_SCORES = "/srv/vsw/scores"

# Never shipped: measured at 94 MB on the first package, and nothing in the
# app opens it. Excluding it took one install from 119 MB to 29 MB.
NEVER_SHIP = ("reference/audio.wav", "performance/audio.wav")


class Report:
    """Findings, in the order they were made, with a verdict at the end."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []
        self.blocking = 0

    def ok(self, text: str) -> None:
        self.lines.append(("ok", text))

    def fix(self, text: str) -> None:
        """Wrong in the project, and the installer will correct it."""
        self.lines.append(("fix", text))

    def note(self, text: str) -> None:
        self.lines.append(("note", text))

    def bad(self, text: str) -> None:
        """Wrong, and nothing here can put it right."""
        self.lines.append(("BAD", text))
        self.blocking += 1

    def show(self) -> None:
        for kind, text in self.lines:
            mark = {"ok": "  ok  ", "fix": "  fix ", "note": "  note",
                    "BAD": "  BAD "}[kind]
            print(f"{mark} {text}")


def _humdrum_records(path: pathlib.Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8",
                                   errors="ignore").splitlines():
            if line.startswith("!!!") and ":" in line:
                key, value = line[3:].split(":", 1)
                out.setdefault(key.strip(), value.strip())
    except OSError:
        pass
    return out


def check(root: pathlib.Path) -> Report:
    r = Report()

    if not root.is_dir():
        r.bad(f"{root} is not a folder")
        return r
    r.ok(f"folder: {root.name}")

    # -- the bands, without which nothing renders --------------------------
    #
    # The contract is `lines/<first_measure>.{svg,png,jpg}`: the FILENAME is
    # the measure the band starts at, and that is what the renderer sorts and
    # times by. A file named anything else is not a band, it is litter, and a
    # band named wrongly puts the wrong music on screen at the wrong moment.
    lines = root / "score" / "lines"
    if not lines.is_dir():
        r.bad("score/lines is missing -- not exported yet; nothing can render")
    else:
        files = [p for p in lines.iterdir() if p.is_file()]
        if not files:
            r.bad("score/lines is empty -- the score has not been exported")
        else:
            good, odd, bad_name = [], [], []
            for f in files:
                if f.suffix.lower() not in IMAGE_SUFFIXES:
                    odd.append(f.name)
                    continue
                try:
                    good.append((int(f.stem), f))
                except ValueError:
                    bad_name.append(f.name)

            if good:
                good.sort()
                first, last = good[0][0], good[-1][0]
                kinds = {}
                for _, f in good:
                    kinds[f.suffix.lower()] = kinds.get(f.suffix.lower(), 0) + 1
                shape = ", ".join(f"{n}{ext}" for ext, n in sorted(kinds.items()))
                r.ok(f"score/lines: {len(good)} bands ({shape}), "
                     f"measures {first}-{last}")

                if first != 1:
                    r.note(f"the first band starts at measure {first}, not 1 "
                           f"-- fine for an excerpt, wrong for a whole piece")

                seen = [m for m, _ in good]
                if len(set(seen)) != len(seen):
                    dupes = sorted({m for m in seen if seen.count(m) > 1})
                    r.bad(f"two bands claim the same starting measure "
                          f"{dupes[:5]} -- one will silently win")

                # SVG is the good case: it rasterises to whatever size the
                # layout asks for, which is why the band stays sharp at
                # 1916px when the package ships 1306. But it needs cairosvg
                # ON THE MACHINE THAT RENDERS, and a package of pure .svg on
                # a host without it does not degrade, it refuses.
                svgs = kinds.get(".svg", 0)
                if svgs and svgs == len(good):
                    from app import package as _pkg
                    if _pkg.can_rasterize_svg():
                        r.ok("bands are vector, and cairosvg is here to "
                             "rasterise them")
                    else:
                        r.note("bands are ALL vector and cairosvg is not "
                               "installed here -- this machine cannot render "
                               "them, though the server can. Checked only.")
                elif svgs:
                    r.note(f"mixed bands: {svgs} vector, "
                           f"{len(good) - svgs} raster")
                else:
                    r.note("bands are raster only -- they will be upscaled to "
                           "the band rectangle and soften")
            else:
                r.bad("no band is named after a measure number")

            if bad_name:
                r.bad(f"band files not named <measure>.<ext>: "
                      f"{bad_name[:4]} -- the renderer cannot time these")
            if odd:
                r.note(f"non-image files in lines/: {odd[:4]}")

    # -- the manifest -------------------------------------------------------
    manifest = root / "score" / "export.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            w, h = data.get("band_w"), data.get("band_h")
            r.ok(f"score/export.json: bands cut at {w}x{h}")
        except json.JSONDecodeError as exc:
            r.bad(f"score/export.json is not valid JSON: {exc}")
    else:
        r.bad("score/export.json is missing -- no band geometry")

    # -- the reference chroma, the quiet one --------------------------------
    if (root / "score" / "chroma.npy").is_file():
        r.ok("score/chroma.npy: where the loader looks")
    elif (root / "performance" / "chroma.npy").is_file():
        r.fix("chroma.npy is in performance/ -- the loader only looks under "
              "score/. It will be copied across; without it every alignment "
              "against this score fails.")
    elif (root / "reference" / "chroma.npy").is_file():
        r.fix("chroma.npy is in reference/ -- will be copied to score/")
    else:
        r.bad("no chroma.npy anywhere -- alignment has no reference to warp "
              "against")

    # -- the alignment ------------------------------------------------------
    if (root / "reference" / "measures.data").is_file():
        r.ok("reference/measures.data: the alignment")
    elif (root / "performance" / "measures.data").is_file():
        r.fix("the alignment is in performance/ -- installed packages keep it "
              "in reference/. The folder will be renamed on the way up.")
    else:
        r.bad("no measures.data -- this score has never been aligned to a "
              "reference recording")

    # -- metadata, which the licence depends on -----------------------------
    source = root / "score" / "source.krn"
    if source.is_file():
        rec = _humdrum_records(source)
        title = rec.get("OTL", "")
        opus = rec.get("OPS", "")
        publisher = rec.get("PPR", "")
        r.ok(f"score/source.krn: {title or '(no title)'} "
             f"{opus or '(no opus)'}")
        if publisher:
            place = rec.get("PPP", "")
            r.ok(f"edition: {publisher}{', ' + place if place else ''}")
        else:
            # The Chopin Institute scores are CC BY 4.0 and the licence
            # requires the attribution; the video prints it from here.
            r.bad("no PPR record -- the edition cannot be credited, and the "
                  "CC BY licence on these scores requires it")
    else:
        r.bad("score/source.krn is missing -- no composer, title or edition")

    # -- what will not be shipped -------------------------------------------
    for rel in NEVER_SHIP:
        f = root / rel
        if f.is_file():
            r.note(f"{rel} is {f.stat().st_size / 1e6:.0f} MB and is read by "
                   f"nothing -- it will not be uploaded")

    if (root / "performance").is_dir() and (root / "reference").is_dir():
        r.note("both performance/ and reference/ exist; reference/ wins")

    # -- the name the recogniser will look for ------------------------------
    pair_list = pathlib.Path(settings.pair_list) if settings.pair_list else None
    if pair_list and pair_list.is_file():
        try:
            data = json.loads(pair_list.read_text(encoding="utf-8"))
            pairs = data.get("pairs", data)
            if root.name in pairs:
                r.ok("the recogniser knows this folder name")
            else:
                near = [k for k in pairs
                        if k.split("_")[0] == root.name.split("_")[0]]
                r.bad(f"'{root.name}' is not a key in pair_list.json, so a "
                      f"recognised upload can never resolve to this score."
                      + (f" Close names: {near[:3]}" if near else ""))
        except json.JSONDecodeError:
            r.note("pair_list.json could not be read; name not checked")
    else:
        r.note("no PAIR_LIST configured here, so the folder name was not "
               "checked against the recogniser -- do it on the server")

    # -- and finally, the app's own loader -----------------------------------
    try:
        from app import package
        pkg = package.load(root)
        r.ok(f"the app loads it: {pkg.display_name}")
    except Exception as exc:                          # noqa: BLE001
        r.bad(f"the app cannot load it: {type(exc).__name__}: {exc}")
        return r

    # -- DO THE BANDS AND THE ALIGNMENT AGREE? -------------------------------
    #
    # The quietest failure in the whole contract. `band_schedule` places each
    # band at the timestamp of its first measure; when that measure has no
    # alignment point it falls to the next aligned one, and when there is no
    # next one it does `continue` -- the band is DROPPED. Silently. A package
    # whose bands run past the end of its alignment loads perfectly, renders
    # without an error, and simply stops showing music partway through.
    #
    # So the two are compared here rather than discovered by watching a video
    # to the end.
    timeline = list(pkg.timeline or [])
    if not timeline:
        r.bad("the alignment is empty -- every band would be dropped and the "
              "video would show no score at all")
    else:
        aligned = sorted(m for m, _ in timeline)
        first_aligned, last_aligned = aligned[0], aligned[-1]
        r.ok(f"alignment: {len(aligned)} points, measures "
             f"{first_aligned}-{last_aligned}")

        bands = list(pkg.bands or [])
        orphans = [b.first_measure for b in bands
                   if not any(m >= b.first_measure for m in aligned)]
        if orphans:
            r.bad(f"{len(orphans)} band(s) start after the last aligned "
                  f"measure ({last_aligned}) and would be silently dropped: "
                  f"measures {sorted(orphans)[:5]} -- the video would stop "
                  f"showing score partway through")
        else:
            r.ok(f"every one of the {len(bands)} bands can be placed on the "
                 f"timeline")

        exact = sum(1 for b in bands
                    if any(m == b.first_measure for m in aligned))
        if exact < len(bands):
            r.note(f"{len(bands) - exact} band(s) have no alignment point at "
                   f"their exact first measure and will use the next one "
                   f"-- normal for a pickup bar, worth a look if it is many")

        # Scheduled against the PIECE'S OWN length, not an arbitrary minute:
        # a 60-second schedule legitimately places only the first few bands,
        # and reporting that as a shortfall is noise that trains people to
        # ignore the checker.
        duration = max((t for _, t in timeline), default=0.0)
        try:
            schedule = pkg.band_schedule(duration + 1.0)
            if len(schedule) != len(bands):
                r.bad(f"over the full {duration / 60:.1f} minutes only "
                      f"{len(schedule)} of {len(bands)} bands are placed; "
                      f"{len(bands) - len(schedule)} would never appear")
            else:
                r.ok(f"all {len(bands)} bands schedule over the full "
                     f"{duration / 60:.1f} minutes")
        except Exception as exc:                      # noqa: BLE001
            r.bad(f"the bands cannot be scheduled: {type(exc).__name__}: {exc}")

    return r


def install(root: pathlib.Path) -> int:
    """Send a checked package up, in the shape installed packages have."""
    name = root.name
    excludes = []
    for rel in NEVER_SHIP:
        if (root / rel).is_file():
            excludes.append(f"--exclude={rel}")

    print(f"\n  installing {name} ...")
    tar = subprocess.Popen(
        ["tar", "-cf", "-", *excludes,
         "--transform", "s|^performance/|reference/|",
         "score"] + (["performance"] if (root / "performance").is_dir() else [])
        + (["reference"] if (root / "reference").is_dir() else []),
        cwd=str(root), stdout=subprocess.PIPE)

    remote = f"""
      set -e
      D='{REMOTE_SCORES}/{name}'
      rm -rf "$D"; mkdir -p "$D"
      tar -xf - -C "$D"
      # The loader only looks under score/; projects keep it beside the
      # performance it was computed from.
      [ -f "$D/score/chroma.npy" ] || cp "$D/reference/chroma.npy" "$D/score/chroma.npy"
      rmdir "$D/performance" 2>/dev/null || true
      chown -R vsw:vsw "$D"
      du -sh "$D"
    """
    done = subprocess.run(["ssh", "-o", "BatchMode=yes", SERVER, remote],
                          stdin=tar.stdout)
    tar.wait()
    if done.returncode != 0:
        print("  install FAILED", file=sys.stderr)
        return 1
    print("  installed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check a score package, and optionally install it.")
    parser.add_argument("folder", help="the music_line_extractor project")
    parser.add_argument("--install", action="store_true",
                        help="install it too, if nothing is blocking")
    args = parser.parse_args()

    root = pathlib.Path(args.folder)
    print()
    report = check(root)
    report.show()
    print()

    if report.blocking:
        print(f"  {report.blocking} problem(s) block this package. "
              f"Nothing was sent.")
        return 1

    fixes = sum(1 for kind, _ in report.lines if kind == "fix")
    print("  this package is usable"
          + (f", with {fixes} thing(s) the installer corrects" if fixes else ""))
    if not args.install:
        print("  add --install to send it to the server")
        return 0
    return install(root)


if __name__ == "__main__":
    sys.exit(main())
