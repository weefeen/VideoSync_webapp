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
import shlex
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
                if svgs:
                    _check_vector_bands(root, r)
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

    # -- WHY the alignment is missing, which is the useful half -------------
    #
    # Three blockers -- no chroma, no measures.data, an empty alignment --
    # have one cause often enough that saying it is worth more than listing
    # them: the reference was saved with no recording attached. Measured on
    # the Ballade, whose `reference.json` recorded `video_file_path: null`
    # and whose reference/ held nothing but that file. Without this the
    # three symptoms send somebody looking for three problems.
    # EITHER FOLDER, like every other check here: a project keeps this in
    # `performance/` and an installed package in `reference/`, and looking
    # in only one of them is how this check silently did nothing the first
    # time it ran.
    ref = next((root / d / "reference.json" for d in ("reference", "performance")
                if (root / d / "reference.json").is_file()), None)
    if ref is not None:
        try:
            snapshot = json.loads(ref.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            r.note(f"reference/reference.json could not be read: {exc}")
            snapshot = {}
        if snapshot.get("is_reference"):
            recording = snapshot.get("video_file_path")
            audio = [n for n in ("reference/audio.wav", "performance/audio.wav")
                     if (root / n).is_file()]
            if not recording and not audio:
                r.bad("a reference was saved with NO RECORDING attached "
                      "(reference.json says video_file_path: null, and there "
                      "is no audio.wav). That is why there is no chroma and "
                      "no alignment: point the reference step at a recording "
                      "of this piece and run it again. The engraving is "
                      "finished and is not the problem.")
            elif recording and not audio:
                r.note(f"the reference names a recording that is not in the "
                       f"package: {str(recording)[:70]}. It is not shipped, "
                       f"so this only matters if the alignment is missing "
                       f"too.")

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



def _check_vector_bands(root: pathlib.Path, r: "Report") -> None:
    """What a Verovio band still carries that would spoil the video.

    THE PIPELINES DIVERGED and this is the guard against it happening
    again. music_line_extractor adapts a Verovio SVG for cairosvg in a
    function called `_fix_svg` -- and it has TWO of them. The one that
    renders its own pages recolours editor marks and substitutes music
    glyphs; the one that exports the bands we consume does neither. So a
    band can arrive here carrying something that was solved upstream for a
    different output, and the first anybody hears of it is a box in a
    finished video, which is where the metronome note in a tempo marking
    was found.

    Read from the first, middle and last band rather than all of them: they
    come out of one Verovio run, so what afflicts one afflicts all, and a
    package is dozens of files at half a megabyte.
    """
    from app import smufl

    bands = sorted((root / "score" / "lines").glob("*.svg"))
    if not bands:
        return
    sample = {bands[0], bands[len(bands) // 2], bands[-1]}

    artefacts: set[tuple[bool, str]] = set()
    undrawable: set[str] = set()
    embedded: set[str] = set()
    for band in sorted(sample):
        text = band.read_text(encoding="utf-8", errors="replace")
        artefacts.update(smufl.artefacts(text))
        undrawable.update(smufl.undrawable(text))
        embedded.update(smufl.faces(text))

    for fatal, why in sorted(artefacts):
        if fatal:
            r.bad(f"the bands {why}")
        else:
            r.note(f"the bands {why}")
    for why in sorted(undrawable):
        r.bad(f"a band draws {why}")

    if embedded:
        if smufl.available():
            many = len(embedded) > 1
            r.ok(f"music font{'s' if many else ''} "
                 f"{', '.join(sorted(embedded))} "
                 f"{'travel' if many else 'travels'} with the score and "
                 f"will be installed where it renders")
        else:
            r.warn(f"the bands embed {', '.join(sorted(embedded))}, and "
                   f"{smufl.why_unavailable()} -- the server will still "
                   f"unpack it, this machine cannot check it")
    elif not undrawable:
        r.note("the bands embed no font, and draw no text that needs one")


def install(root: pathlib.Path) -> int:
    """Send a checked package up, in the shape installed packages have.

    The renaming of performance/ to reference/ happens ON THE SERVER, not in
    tar. Windows ships bsdtar, which has no --transform, and it does not fail
    loudly: the first hand-install produced a package with BOTH folders and
    looked like it had worked. Anything shape-changing therefore runs where
    the shell is known.
    """
    name = root.name
    which = "reference" if (root / "reference").is_dir() else "performance"
    if not (root / which).is_dir():
        print("  nothing to send: no reference/ or performance/",
              file=sys.stderr)
        return 1

    excludes = []
    for rel in NEVER_SHIP:
        if (root / rel).is_file():
            excludes.append("--exclude")
            excludes.append(rel)

    # THE ENGRAVED PLATES SHIP TOO. `pages/page_NNN.svg` is the whole plate
    # as engraved, and the site uses it as page furniture -- the score that
    # sits behind the text. It was never in this tar, so every installed
    # package arrived with no plates: the catalogue recorded `pages: 0`,
    # `_put_preview` published none, `/api/library/<name>/page` answered
    # 404, and the score behind the page quietly disappeared.
    #
    # Optional, because a package without plates is still renderable -- the
    # video is cut from `score/lines`, not from these.
    sending = ["score", which]
    plates = sorted(root.glob("pages/page_*.svg"))
    if plates:
        sending.append("pages")

    print()
    print(f"  installing {name} ...")
    print(f"    sending score/ and {which}/"
          + (f" and {len(plates)} engraved plate(s)" if plates else
             " -- NO pages/ folder, so this score will not appear behind "
             "the text on the site")
          + (" (without the reference audio)" if excludes else ""))

    tar = subprocess.Popen(["tar", "-cf", "-", *excludes, *sending],
                           cwd=str(root), stdout=subprocess.PIPE)

    remote = f"""
      set -e
      D='{REMOTE_SCORES}/{name}'
      rm -rf "$D"; mkdir -p "$D"
      tar -xf - -C "$D"
      # Installed packages keep the alignment in reference/; a project calls
      # it performance/. Renamed here, where the shell is GNU and known.
      if [ -d "$D/performance" ] && [ ! -d "$D/reference" ]; then
          mv "$D/performance" "$D/reference"
      fi
      rmdir "$D/performance" 2>/dev/null || true
      # The loader only looks under score/ for the reference chroma.
      if [ ! -f "$D/score/chroma.npy" ] && [ -f "$D/reference/chroma.npy" ]; then
          cp "$D/reference/chroma.npy" "$D/score/chroma.npy"
      fi
      chown -R vsw:vsw "$D"
      echo "    installed: $(du -sh "$D" | cut -f1)"
    """
    done = subprocess.run(["ssh", "-o", "BatchMode=yes", SERVER, remote],
                          stdin=tar.stdout)
    tar.wait()
    if done.returncode != 0 or tar.returncode != 0:
        print("  install FAILED", file=sys.stderr)
        return 1

    # AND INTO THE BUCKET, which is the part that makes it renderable.
    # The web box does not render; compute nodes do, and a node is created
    # from an image that knows nothing about a score installed after it was
    # captured. The bucket is the only library both machines can reach, so
    # a package that is on the web box and not in the bucket is installed
    # nowhere that matters.
    #
    # Pushed FROM THE SERVER: that is where the bucket credentials live and
    # where the package now sits in its corrected shape. Uploading from a
    # laptop would mean putting the bucket key on the laptop.
    print("    publishing to the bucket, so compute nodes can fetch it")
    if _remote_python(PUBLISH, name) != 0:
        print("  the package is on the web box but NOT in the bucket, so no "
              "compute node can render it", file=sys.stderr)
        return 1

    # Installed is not the same as usable: ask the server's own loader, on
    # the server, rather than trusting that a copy succeeded.
    if _remote_python(PROBE, name) != 0:
        print()
        print("    the server cannot render it yet, so nothing was started.")
        print("    Re-run this once it can; requests keep waiting until then.")
        return 1

    # THE REQUESTS THAT WERE WAITING FOR THIS SCORE NOW GO. A request for a
    # piece we had not engraved is held, not refused: the row keeps the
    # recording, the address and the folder name. Nothing can tell that row
    # the engraving has arrived, so publishing the score is what starts it.
    #
    # Deliberately after the probe and not before: the check above is the
    # assurance that the score is complete and loadable on the server, and
    # it is the only thing standing between a half-finished upload and a
    # render against it.
    print()
    print("    starting the requests that were waiting for this score")
    _remote_shell("cd /srv/vsw/current && sudo -u vsw /srv/vsw/venv/bin/python"
                  " tools/retrigger.py --waiting")
    return 0


# Run on the server, against the app's own code, with the package name as
# argv[1] -- never interpolated into the source, because these names carry
# spaces, parentheses and accents and one of them will eventually carry a
# quote.
PUBLISH = """
import pathlib, sys
sys.path.insert(0, '.')
from app import scorestore
name = sys.argv[1]
n = scorestore.publish(pathlib.Path('/srv/vsw/scores') / name)
print('    in the bucket: %.0f MB at %s' % (n / 1e6, scorestore.key(name)))
"""

PROBE = """
import sys
sys.path.insert(0, '.')
from app import library, scorestore
name = sys.argv[1]
m = [p for p in library.packages() if p.name == name]
print('    the bucket now holds %d score(s)' % len(scorestore.catalogue()))
if not m:
    # `library.packages()` is the RENDERABLE ones, so this is not "the
    # object arrived" but "the server can make a video out of it". A
    # non-zero exit because what follows -- starting the requests that
    # were waiting for this score -- must not run on a half-arrived
    # package: it would spend a machine's minutes producing nothing.
    print('    THE SERVER CANNOT SEE IT')
    raise SystemExit(1)
print('    the server loads it:', m[0].display_name, '|', m[0].edition)
print('    engraved plates on the server: %d' % m[0].pages)
if not m[0].pages:
    print('    (none, so this score will not appear behind the text)')
"""


def _remote_shell(command: str) -> int:
    """Run a command on the server. The caller composes it; nothing here
    interpolates a package name into a shell string."""
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", SERVER, command]).returncode


def _remote_python(script: str, *args: str) -> int:
    """Run a snippet on the server, as the app user, inside the app."""
    cmd = ("cd /srv/vsw/current && sudo -u vsw /srv/vsw/venv/bin/python -c "
           + shlex.quote(script)
           + "".join(" " + shlex.quote(a) for a in args))
    return subprocess.run(["ssh", "-o", "BatchMode=yes", SERVER, cmd]).returncode


def verdict(root: pathlib.Path, report: "Report") -> dict:
    """The same answer, for a program rather than a person.

    FOR THE BUTTON IN music_line_extractor. That application produces these
    packages, so it is the right place to ship one from -- but it must not
    grow its own copy of what a package has to be. This checker is the one
    copy, and `docs/score-package-contract.md` exists because these three
    programs have already drifted apart twice reading the same folder.

    So the interface is this: run the checker, read `ok`, show `problems`.
    Nothing over there needs to know what a band filename means, that the
    reference audio is excluded, or that `performance/` is renamed on the
    server -- and nothing over there needs bucket credentials, because
    `--install` streams a tar over ssh and publishes on the box.

    `publish_as` is the field worth acting on: a package is findable by the
    EXACT folder name, and `recogniser_knows` says whether that name is one
    the recogniser can offer. A perfect package under a name nobody asks
    for is a score no visitor will ever reach.
    """
    return {
        "ok": report.blocking == 0,
        "blocking": report.blocking,
        "publish_as": root.name,
        "recogniser_knows": any(
            "recogniser knows this folder name" in text
            for kind, text in report.lines if kind == "ok"),
        "corrected_on_install": sum(1 for kind, _ in report.lines
                                    if kind == "fix"),
        "problems": [{"level": kind, "text": text}
                     for kind, text in report.lines
                     if kind in ("BAD", "fix", "note")],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check a score package, and optionally install it.")
    parser.add_argument("folder", help="the music_line_extractor project")
    parser.add_argument("--install", action="store_true",
                        help="install it too, if nothing is blocking")
    parser.add_argument("--json", action="store_true",
                        help="report as JSON, for another program to read")
    args = parser.parse_args()

    root = pathlib.Path(args.folder)
    if not args.json:
        print()
    report = check(root)
    if args.json:
        # Nothing but JSON on stdout, so a caller can parse it without
        # knowing anything about how this prints for a person.
        answer = verdict(root, report)
        if args.install and answer["ok"]:
            code = install(root)
            answer["installed"] = code == 0
            if code != 0:
                answer["ok"] = False
        elif args.install:
            answer["installed"] = False
        print(json.dumps(answer, ensure_ascii=False, indent=1))
        return 0 if answer["ok"] else 1

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
