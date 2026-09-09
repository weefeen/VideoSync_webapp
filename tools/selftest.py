"""Checks that must pass identically on Windows and on Linux.

    python tools/selftest.py

Nothing here needs ffmpeg, cairo, torch, a score package or a network. It is
the part of the app that can be checked anywhere, so it runs on both
platforms on every push and proves they still agree. The expensive proof —
that a real recording comes out as a real video — stays in
`tools/try_render.py` and `tools/try_identify.py`, on a machine with the
tools installed.

Why both platforms, when the app only ever runs on one at a time: the
development machine is Windows and the servers are Linux, and the
differences between them do not announce themselves. They surface as an
empty result rather than as an error.

  - `pathlib.Path` treats a backslash as an ordinary character on Linux, so
    a Windows path parsed there yields the whole path as its "stem".
  - `os.pathsep` is ';' on Windows and ':' on Linux, so one Windows
    directory read on Linux splits into two nonexistent ones.
  - The filesystem is case-sensitive on Linux and not on Windows.

Each of those has already cost real time here. The first one shipped: every
recognition on the Linux node reported consensus 1.00 and then "no
installed score to render it", because the lookup after the recognition
matched nothing and said so quietly. Every check below exists because
something worked on one platform and silently did nothing on the other.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import pathlib
import pkgutil
import re
import sys
import tempfile
import time
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Before importing anything from `app`: the settings module reads WORK_DIR at
# import time, and a check that writes jobs must not touch the developer's
# real queue. python-dotenv does not override variables already set, so this
# wins over a local .env.
_SANDBOX = tempfile.TemporaryDirectory(prefix="svs_selftest_")
os.environ["WORK_DIR"] = str(pathlib.Path(_SANDBOX.name) / "work")


class Failed(AssertionError):
    """A check did not hold. The message is meant to be read on a CI page."""


# --------------------------------------------------------------------------
# the app loads at all
# --------------------------------------------------------------------------
def check_every_module_imports() -> str:
    """An import error otherwise appears as a worker that will not start."""
    names = [f"app.{m.name}" for m in pkgutil.iter_modules([str(ROOT / "app")])]
    bad, missing = [], set()
    for name in names:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:
            missing.add(exc.name or "?")
            bad.append(f"  {name}: {type(exc).__name__}: {exc}")
        except Exception as exc:                      # noqa: BLE001
            bad.append(f"  {name}: {type(exc).__name__}: {exc}")
    if bad:
        note = ""
        # On Windows the workflow is split across conda environments and it
        # is easy to run this under the recognition one, which has torch but
        # no Flask. That is not a broken repository, only the wrong python.
        if missing & {"flask", "cairosvg", "PIL", "dotenv"}:
            note = (f"\n\n  {sorted(missing)} missing — this is probably the "
                    f"wrong interpreter.\n  The app runs under the "
                    f"VideoScoreSync environment on Windows, not the "
                    f"recognition one:\n    {sys.executable}\n  is what ran "
                    f"these checks.")
        raise Failed("modules that would not import:\n" + "\n".join(bad) + note)
    return f"{len(names)} modules"


def check_limits_are_sane() -> str:
    """A typo in .env.example would otherwise be a wrong limit in production."""
    from app import limits
    from app.settings import settings

    for name, rule in limits.RULES.items():
        if rule.limit <= 0:
            raise Failed(f"{name} allows {rule.limit} — nobody could ever act")
        if rule.window <= 0:
            raise Failed(f"{name} has a window of {rule.window} seconds")
    if settings.smtp_port <= 0:
        raise Failed(f"SMTP_PORT is {settings.smtp_port}")
    return f"{len(limits.RULES)} limit rules, all positive"


def check_job_store_round_trips() -> str:
    """The queue is SQLite, and SQLite is where the platforms differ least —
    but the claiming statement uses UPDATE...RETURNING, which is only in
    newer SQLite, and the two platforms ship different builds of it."""
    from app import store

    store.put_job({"id": "selftest", "created": time.time(), "name": "x.mp4",
                   "upload": "x.mp4", "state": store.QUEUED,
                   "queued_at": time.time()})
    if store.get_job("selftest")["state"] != store.QUEUED:
        raise Failed("a job did not come back queued")
    if store.position("selftest") != 0:
        raise Failed("the only job in the queue is not first in it")

    claimed = store.claim_next("selftest-worker")
    if claimed is None or claimed["id"] != "selftest":
        raise Failed("UPDATE...RETURNING claimed nothing — check the SQLite "
                     f"build: {__import__('sqlite3').sqlite_version}")

    run = store.stage_begin("selftest", "embed", media_seconds=10.0)
    store.stage_end(run, state=store.DONE, command="ffmpeg -i a b")
    rows = store.stage_runs("selftest")
    if not rows or rows[0]["command"] != "ffmpeg -i a b":
        raise Failed(f"stage rows did not round-trip: {rows}")
    return f"queued, claimed and staged (sqlite {__import__('sqlite3').sqlite_version})"


# --------------------------------------------------------------------------
# recognition resolves to a score, whatever wrote the pair list
# --------------------------------------------------------------------------
# Two editions of one piece share a recording, which is the normal case and
# the reason a piece id maps to a *list*. The Windows path is what the file
# really contains today; the POSIX one is what it would contain if it were
# ever rewritten portably, and both must work on both platforms.
_BREITKOPF = "Op.39_3ème Scherzo pour le Piano_(Breitkopf)__039-1-BH"
_TROUPENAS = "Op.39_Scherzo_(Troupenas)__039-1-TR"
_WINDOWS_VIDEO = (r"C:\ZZ_perso\weefeen\Chopin_Companion"
                  r"\Videos_downloaded_youtube\work_op_39__scherzo_dyZmzXMHItI.mp4")
_POSIX_VIDEO = "/srv/vsw/videos/work_op_25_no_11_aeolian_harp_QK1yPPjLnGQ.mp4"

_FIXTURE = {
    _BREITKOPF: {"video": _WINDOWS_VIDEO, "level1": "ok"},
    _TROUPENAS: {"video": _WINDOWS_VIDEO, "level1": "ok"},
    "Op.25_Etude No. 11_(Breitkopf)__025-11-BH": {"video": _POSIX_VIDEO,
                                                  "level1": "ok"},
    "Op.42_Valse_(Pacini)__042-1-P": {
        "video": r"C:\videos\work_op_42__valse_D92xATclLHs.mp4",
        "level1": "wrong"},
}


def _runner():
    """Load tools/identify_runner.py without running it."""
    spec = importlib.util.spec_from_file_location(
        "identify_runner", ROOT / "tools" / "identify_runner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_piece_ids_resolve() -> str:
    """The bug that shipped: a Windows path in the pair list, read on Linux.

    `pathlib.Path(video).stem` is a PosixPath there, and a backslash is an
    ordinary character to it, so the "stem" was the entire absolute path. It
    matched no piece id, `score_names` came back empty for everything, and
    the app said "recognised, but no installed score to render it" — with
    full confidence — for all 186 pieces in the corpus.
    """
    runner = _runner()
    with tempfile.TemporaryDirectory(prefix="svs_pairs_") as tmp:
        path = pathlib.Path(tmp) / "pair_list.json"
        path.write_text(json.dumps({"pairs": _FIXTURE}, ensure_ascii=False),
                        encoding="utf-8")
        names = runner._labels(str(path))

        got = names("work_op_39__scherzo_dyZmzXMHItI")
        if got != sorted([_BREITKOPF, _TROUPENAS]):
            raise Failed(
                "a Windows path in the pair list did not resolve to its "
                f"editions on {sys.platform}.\n  expected both editions\n"
                f"  got      {got}")

        if names("work_op_25_no_11_aeolian_harp_QK1yPPjLnGQ") != [
                "Op.25_Etude No. 11_(Breitkopf)__025-11-BH"]:
            raise Failed("a POSIX path in the pair list did not resolve")

        # level1 "wrong" means the recording was never in the indexes, so
        # offering its score would name a piece nothing can be matched to.
        if names("work_op_42__valse_D92xATclLHs"):
            raise Failed("an unvalidated row was offered as a score")

        if names("no_such_recording") or names(""):
            raise Failed("an unknown piece id produced a score name")
    return "3 validated rows, 2 path flavours, 1 rejected"


def check_pair_list_is_read_without_the_dependency() -> str:
    """`weefeen_id.labels` parses the same file and is not portable.

    Kept as a check rather than a comment because the tempting cleanup — "we
    already have a class for this, import it" — reintroduces the bug, and on
    Windows nothing would show it.

    Narrow on purpose. Whether the parsing is *correct* is settled by
    `check_piece_ids_resolve` actually resolving one; this only guards the
    one edit that would quietly hand the job back to the broken parser.
    """
    text = (ROOT / "tools" / "identify_runner.py").read_text(encoding="utf-8")
    # An import, not a mention: the docstrings there name the class on
    # purpose, to say why it is not used.
    imported = re.search(r"^\s*(from\s+weefeen_id\S*\s+import\s+.*ScoreLabels"
                         r"|import\s+.*ScoreLabels)", text, re.MULTILINE)
    if imported:
        raise Failed(f"identify_runner imports ScoreLabels again "
                     f"({imported.group(0).strip()}); it builds its lookup "
                     f"with pathlib.Path, which is wrong on Linux")
    return "no import of the unportable parser"


# --------------------------------------------------------------------------
# configuration says the same thing on both platforms
# --------------------------------------------------------------------------
_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def _declared(path: pathlib.Path) -> dict[str, str]:
    """Variable -> value for one env file, ignoring comments and blanks."""
    found = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _ASSIGNMENT.match(line.strip())
        if match:
            found[match.group(1)] = line.split("=", 1)[1].strip()
    return found


def check_linux_configuration_leaves_no_gaps() -> str:
    """Every variable .env.example sets must also be set in .env.prod.

    `app/settings.py` reads `.env` and then falls back to `.env.example`,
    which carries Windows paths. A gap is not a quiet default on Linux: it
    is `C:\\VideoScoreSync\\scores` arriving as two score roots, one named
    `C` and one named `\\VideoScoreSync\\scores`, because os.pathsep is ':'
    there. That happened on the first deployment.
    """
    example = _declared(ROOT / ".env.example")
    prod = _declared(ROOT / ".env.prod")
    missing = sorted(set(example) - set(prod))
    if missing:
        raise Failed(
            ".env.prod does not set, so Linux falls back to the Windows "
            "value in .env.example:\n  " + "\n  ".join(missing))
    return f"{len(prod)} variables, covering all {len(example)} in .env.example"


def check_linux_configuration_has_no_windows_paths() -> str:
    """A drive letter or a backslash in .env.prod is a copied-over mistake."""
    bad = []
    for name, value in _declared(ROOT / ".env.prod").items():
        if _DRIVE.match(value) or "\\" in value:
            bad.append(f"  {name}={value}")
    if bad:
        raise Failed("Windows paths in the Linux configuration:\n" + "\n".join(bad))
    return "no drive letters, no backslashes"


# --------------------------------------------------------------------------
def main() -> int:
    checks = [
        check_every_module_imports,
        check_limits_are_sane,
        check_job_store_round_trips,
        check_piece_ids_resolve,
        check_pair_list_is_read_without_the_dependency,
        check_linux_configuration_leaves_no_gaps,
        check_linux_configuration_has_no_windows_paths,
    ]
    print(f"  {sys.platform}  python {sys.version.split()[0]}  "
          f"os.pathsep {os.pathsep!r}\n")

    failures = 0
    for check in checks:
        label = check.__name__.removeprefix("check_").replace("_", " ")
        try:
            detail = check()
        except Failed as exc:
            failures += 1
            print(f"  FAIL  {label}\n        {exc}\n")
        except Exception as exc:                      # noqa: BLE001
            failures += 1
            print(f"  ERROR {label}: {type(exc).__name__}: {exc}")
            print(traceback.format_exc())
        else:
            print(f"  ok    {label:52} {detail}")

    # Windows will not delete the open SQLite file, and TemporaryDirectory
    # cleans up at exit. Linux would not have minded either way.
    try:
        from app import store
        store.close()
    except Exception:                                 # noqa: BLE001
        pass

    print()
    if failures:
        print(f"  {failures} of {len(checks)} checks failed")
        return 1
    print(f"  all {len(checks)} checks passed on {sys.platform}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
