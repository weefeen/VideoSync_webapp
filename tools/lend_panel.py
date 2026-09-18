"""The page that says what this computer is doing, and one switch.

WHY A PAGE. Lending this machine to the queue was two controls on two
machines: whether `tools/volunteer.py` was running here, and what
`COMPUTE_MODE` said in a .env file on the web box. Neither was visible from
the other, one was a keystroke in a scrolling terminal, and the only way to
answer "what is happening right now" was to read a log.

AND WHY THERE IS ONE SWITCH. Four versions of this page were built and each
was rejected for the same underlying reason: they offered the settings the
PROGRAM has rather than the decision a PERSON makes. The settings are two
-- does this computer render, and does the server rent one when it does
not. Exposed faithfully they must be combined by the reader. Folded into
four outcomes they read as a maze. Split into two questions they still
dragged in a second machine, its price, and the gap between what the site
accepts and what this computer can manage -- a gap in which a video is
refused here, never rented there, and waits for ever.

The decision is one thing: IS THIS COMPUTER WORKING, OR IS EVERYTHING ON
HOLD. Renting is no part of it and is not offered, so the server is held at
`manual`, where it creates nothing, and the switch here is the only thing
that moves.

    ON      every upload is rendered on this computer
    OFF     nothing is rendered anywhere; uploads wait in the queue

Two states, one control, nothing to combine. What is left above it is the
answer to "what is happening right now", which is what the page is opened
to find out.

LOOPBACK ONLY, and that is not a detail. This page can pause a renderer
mid-job and write to the production server's configuration, and it has no
login because it is not reachable from anywhere that would need one. Bound
to 127.0.0.1, refused otherwise.

No dependency: `http.server` from the standard library, and a page with no
build step. The machine this runs on is somebody's desktop, and a control
panel that needs an install is a control panel nobody opens.
"""
from __future__ import annotations

import json
import logging
import shlex
import pathlib
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Its own, rather than relying on whoever imported it having done this.
# `volunteer.py` does it before importing this; a person opening this
# module from `tools/` does not.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

logger = logging.getLogger("volunteer.panel")

# The web box. ssh, not an API: this machine already holds a key to that
# box, and the alternative is an authenticated admin endpoint on a public
# site -- a login to build and protect so a loopback page can read a word.
SERVER = "root@172.104.237.127"
ENV_PATH = "/srv/vsw/shared/.env"

# The only mode this page ever sets, and the only one it writes. Renting is
# not on offer here, so the server is held where it creates nothing and
# work waits for this computer. A literal, never anything typed, because it
# is interpolated into a command that runs as root on the production box.
HELD_AT = "manual"


def _local_project(edition: str) -> "pathlib.Path | None":
    """The folder engraved on THIS computer under exactly this name, if any.

    Searched in the score roots this computer is configured with -- the
    project directory the engraving tool writes to is one of them. Exact
    name, because the name is the contract that releases the waiting links
    and a near-miss releases nothing. Open (a `score/` folder) or closed
    (only its `.spj`) -- both count; the package loader reads either.
    """
    if not edition or "/" in edition or "\\" in edition or edition in (".", ".."):
        return None
    try:
        from app.settings import settings                 # noqa: PLC0415
    except Exception:                                      # noqa: BLE001
        return None
    try:
        from app import package as pkg                    # noqa: PLC0415
    except Exception:                                      # noqa: BLE001
        return None
    for root in settings.score_roots:
        folder = pathlib.Path(root.path) / edition
        if folder.is_dir() and pkg.is_package(folder):
            return folder
    return None


def _local_projects() -> list:
    """Every project folder engraved on this computer, open or closed.

    The engraving tool's project directory is one of the score roots; a
    folder counts when the package loader would accept it -- loose
    `score/lines`, or a `.spj` under its own name. Sorted by name so the
    list reads like the library.
    """
    try:
        from app import package as pkg                    # noqa: PLC0415
        from app.settings import settings                 # noqa: PLC0415
    except Exception:                                      # noqa: BLE001
        return []
    seen: dict = {}
    for root in settings.score_roots:
        base = pathlib.Path(root.path)
        if not base.is_dir():
            continue
        for folder in sorted(base.iterdir()):
            if folder.name.startswith(".") or folder.name in seen:
                continue
            try:
                if folder.is_dir() and pkg.is_package(folder):
                    seen[folder.name] = {"edition": folder.name,
                                         "open": pkg.is_open(folder),
                                         "fingerprint": pkg.fingerprint(folder),
                                         "version": pkg.version_of(folder)}
            except Exception as exc:                       # noqa: BLE001
                logger.debug("%s: not indexed: %s", folder.name, exc)
                continue
    return sorted(seen.values(), key=lambda s: s["edition"].lower())


def _compare(local: dict, there) -> str:
    """How this computer's copy stands to the site's: one word.

    BY THE EXTRACTOR'S KEY WHEN BOTH SIDES HAVE ONE (PROJECT_VERSION_ID_
    SPEC.md): same content_id -> same; our parent is their content ->
    "changed", a fast-forward; their parent is our content -> "behind",
    the site was saved from since; neither -> "diverged", two edits from
    one ancestor, which a person must look at before either wins. Until
    the extractor writes keys, the content fingerprint of the consumed
    files decides, and it can only say same or "changed".
    """
    if there is None:
        return "new"
    if isinstance(there, str):                      # an older server
        there = {"content": there, "version": {}}
    mine, theirs = local.get("version") or {}, there.get("version") or {}
    if mine.get("content_id") and theirs.get("content_id"):
        if mine["content_id"] == theirs["content_id"]:
            return "same"
        if mine.get("parent_id") == theirs["content_id"]:
            return "changed"
        if theirs.get("parent_id") == mine["content_id"]:
            return "behind"
        return "diverged"
    content = str(there.get("content") or "")
    if content == "":
        return "unverified"
    return "same" if content == local.get("fingerprint") else "changed"


class Panel:
    """What the page may ask of the volunteer, and nothing else."""

    def __init__(self, state, pause, resume, stop_after) -> None:
        self.state = state
        self.pause = pause
        self.resume = resume
        self.stop_after = stop_after
        # `asked` False means the first look at the server has not come
        # back yet, which is not a problem and must not be drawn as one.
        self._server = {"mode": "", "problem": "", "asked": False}
        self._wanted: list = []
        # THE SERVER'S INDEX: folder name -> fingerprint of the copy it
        # holds. Compared with the same fingerprint of each project here,
        # so the page lists only what is new or changed -- and says which.
        self._index: dict = {}
        self._index_read = False
        self._lock = threading.Lock()
        # A LOOK THAT FAILS IS WEATHER. This reaches another machine over
        # the internet every few minutes, so it will fail sometimes -- and
        # the first version put the raw subprocess exception at the top of
        # the page, where it read as "this computer is broken" when nothing
        # about rendering had changed. Nothing is said until several looks
        # in a row have failed, and then it is said quietly.
        self._misses = 0
        # Publishing a score from this page: which edition, whether it is
        # still running, and the last lines the installer printed.
        self._publish: dict = {}

    def full(self) -> dict:
        """Everything the page draws."""
        with self._lock:
            wanted = [dict(r) for r in self._wanted]
            publish = dict(self._publish)
        # WHETHER THE SCORE IS ALREADY ON THIS COMPUTER, asked here and not
        # on the server: the server knows what is published, only this
        # machine knows what has just been engraved on it. A row whose score
        # is not published yet but whose folder is sitting in a project
        # directory here is one button away from being released.
        for row in wanted:
            editions = row.get("editions") or []
            row["local"] = bool(editions and not row.get("ready")
                                and _local_project(editions[0]) is not None)
        with self._lock:
            index = dict(self._index)
            index_read = self._index_read
        # ONLY THE DIFFERENCES. Same name and same fingerprint is the same
        # score, and a score the site already has, as it is here, is not
        # something to publish. Until the server's index has been read once
        # nothing is offered: without it every score would look new.
        scores = []
        if index_read:
            for s in _local_projects():
                status = _compare(s, index.get(s["edition"]))
                if status != "same":
                    scores.append({**s, "status": status})
        return {**self.state(), "server": self.server(), "wanted": wanted,
                "publish": publish, "scores": scores,
                "index_read": index_read}

    def server(self) -> dict:
        with self._lock:
            return dict(self._server)

    def refresh(self) -> None:
        """Look at the server, and hold it where it rents nothing.

        Read AND corrected. This page offers no way to rent, so it must not
        leave a server quietly renting -- and it can be set elsewhere,
        because it is a file on another machine. If it has drifted, the
        next look puts it back and says so in the log.
        """
        mode = self._read_mode()
        if mode is None:
            return
        if mode != HELD_AT:
            logger.info("the server was set to %r, which rents machines; "
                        "holding it at %r", mode, HELD_AT)
            problem = self._write_mode(HELD_AT)
            if problem:
                self._set(mode, problem)
                return
            mode = self._read_mode() or HELD_AT
        self._set(mode, "")

    def read_wanted(self) -> None:
        """Pieces somebody asked for and we cannot make yet. Never raises.

        THE DEMAND LOOP, which existed only as mail: a request for a piece
        with no score is refused, the operator is told, and the message
        waits in an inbox with everything else. This is the same fact where
        the work happens -- and the page can make a sound, which an inbox
        cannot.
        """
        rows = self._server_json("/api/wanted")
        if isinstance(rows, list):
            with self._lock:
                self._wanted = rows
        index = self._server_json("/api/library/index")
        if isinstance(index, dict):
            with self._lock:
                self._index = {str(k): str(v) for k, v in index.items()}
                self._index_read = True

    def _server_json(self, path: str):
        """One JSON answer from the server's own API, or None. Never raises."""
        try:
            done = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
                 SERVER, f"curl -fsS -m 20 http://127.0.0.1:5000{path}"],
                capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.debug("could not read %s: %s", path, exc)
            return None
        if done.returncode != 0:
            return None
        try:
            return json.loads(done.stdout)
        except (ValueError, TypeError):
            return None

    def render_now(self, job: str, score: str) -> str:
        """Finish a request whose score now exists. '' or a reason.

        Runs `tools/retrigger.py`, which is where this has always lived --
        it writes the job row and the janitor publishes it. The address is
        on the row now, so nothing has to be retyped.

        The job id and score come back from our own `/api/wanted`, never
        from anything typed, and both are checked against it again here:
        they are about to be arguments to a command on the server.
        """
        with self._lock:
            rows = list(self._wanted)
        match = next((r for r in rows if r.get("job") == job), None)
        if match is None:
            return "that request is not in the list any more"
        if score not in (match.get("ready") or []):
            return f"{score!r} is not a published score for that request"

        command = (
            "cd /srv/vsw/current && sudo -u vsw /srv/vsw/venv/bin/python "
            f"tools/retrigger.py {shlex.quote(job)} "
            f"--score {shlex.quote(score)}")
        try:
            done = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
                 SERVER, command],
                capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"could not reach the server: {exc}"
        if done.returncode != 0:
            return ((done.stderr or done.stdout or "").strip()[-240:]
                    or "the server refused it")
        logger.info("started the held render for job %s as %r", job, score)
        self.read_wanted()
        return ""

    def _read_mode(self) -> str | None:
        try:
            done = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
                 SERVER, f"grep -E '^COMPUTE_MODE=' {ENV_PATH} || true"],
                capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._set("", f"could not reach the server: {exc}")
            return None
        if done.returncode != 0:
            self._set("", (done.stderr or "").strip()[-200:]
                      or "the server refused the connection")
            return None
        for line in done.stdout.splitlines():
            if line.startswith("COMPUTE_MODE="):
                return line.split("=", 1)[1].strip().lower()
        # Unset means `auto`, which is what app/store.py falls back to.
        return "auto"

    def _write_mode(self, name: str) -> str:
        if name != HELD_AT:                    # nothing else is ever written
            return f"{name!r} is not a mode this page sets"
        command = (
            f"grep -q '^COMPUTE_MODE=' {ENV_PATH}"
            f" && sed -i 's/^COMPUTE_MODE=.*/COMPUTE_MODE={name}/' {ENV_PATH}"
            f" || echo 'COMPUTE_MODE={name}' >> {ENV_PATH};"
            f" systemctl restart vsw-scaler vsw-web")
        try:
            done = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                 SERVER, command], capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"could not reach the server: {exc}"
        if done.returncode != 0:
            return (done.stderr or "").strip()[-200:] or "the change was refused"
        return ""

    def publish_score(self, edition: str) -> str:
        """Check and install a score engraved on this computer. '' or a reason.

        The same command a person would type -- `tools/check_score.py <folder>
        --install` -- run for them, so nothing new decides whether a score is
        fit to publish: the checker refuses what it has always refused, and
        says why, and that is what the page shows.

        THE EDITION NAMES A FOLDER ON THIS COMPUTER, found by exact name
        under the configured score roots and nowhere else -- what was sent
        is never a path. Publishing a new score and UPDATING one already on
        the server are the same command: the installer replaces the copy,
        republishes it, and the server forgets what it had measured of the
        old one. Only one runs at a time.
        """
        with self._lock:
            running = bool(self._publish.get("running"))
        if running:
            return "a score is already being published"
        folder = _local_project(edition)
        if folder is None:
            return (f"there is no folder named {edition!r} on this computer; "
                    f"engrave it under exactly that name")

        with self._lock:
            self._publish = {"edition": edition, "running": True, "ok": None,
                             "tail": ["checking the score"],
                             "started": __import__("time").time()}

        def run() -> None:
            checker = pathlib.Path(__file__).resolve().parent / "check_score.py"
            lines: list = []
            code = 1
            try:
                proc = subprocess.Popen(
                    [sys.executable, str(checker), str(folder), "--install"],
                    cwd=str(checker.parent.parent), stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                    errors="replace")
                for line in proc.stdout:
                    line = line.rstrip()
                    # The app's own logging is not the installer talking.
                    if not line.strip() or " INFO " in line or " WARNING " in line:
                        continue
                    lines.append(line.strip())
                    with self._lock:
                        self._publish["tail"] = lines[-6:]
                code = proc.wait(timeout=1800)
            except Exception as exc:                       # noqa: BLE001
                lines.append(f"could not run the installer: {exc}")
            with self._lock:
                self._publish.update(running=False, ok=(code == 0),
                                     tail=lines[-8:])
            logger.info("published %s: %s", edition,
                        "ok" if code == 0 else f"failed ({code})")
            self.read_wanted()

        threading.Thread(target=run, name="publish", daemon=True).start()
        return ""

    def dismiss(self, job: str) -> str:
        """Close a request that will never be engraved. '' or a reason.

        The job id is checked against our own list before it becomes an
        argument to anything, the same rule `render_now` follows.
        """
        with self._lock:
            rows = list(self._wanted)
        if not any(r.get("job") == job for r in rows):
            return "that request is not in the list any more"
        try:
            done = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
                 SERVER,
                 "curl -fsS -m 20 -X POST -H 'Content-Type: application/json' "
                 f"-d '{json.dumps({'job': job})}' "
                 "http://127.0.0.1:5000/api/wanted/dismiss"],
                capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"could not reach the server: {exc}"
        if done.returncode != 0:
            return ((done.stderr or done.stdout or "").strip()[-200:]
                    or "the server refused it")
        self.read_wanted()
        return ""

    def forget_limits(self) -> tuple[str, int]:
        """Clear the site's rate limits. Returns (problem, keys cleared).

        Reached over ssh and then over LOOPBACK on the web box, never from
        the internet: `curl` to 127.0.0.1:5000 carries no X-Forwarded-For,
        which is how the endpoint tells "somebody already on this machine"
        from "the whole internet arriving through Apache". Apache denies
        the path too, so two independent mistakes are needed to expose it.
        """
        try:
            done = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                 SERVER,
                 "curl -fsS -m 20 -X POST http://127.0.0.1:5000"
                 "/api/limits/forget"],
                capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"could not reach the server: {exc}", 0
        if done.returncode != 0:
            return ((done.stderr or "").strip()[-200:]
                    or "the server refused it"), 0
        try:
            return "", int(json.loads(done.stdout).get("cleared", 0))
        except (ValueError, TypeError):
            return "the server's answer could not be read", 0

    def set_working(self, on: bool) -> str:
        """The one control. Returns '' or a reason."""
        (self.resume if on else self.pause)()
        return ""

    # How many consecutive failed looks before the page mentions it. Three,
    # at the interval below, is about a quarter of an hour of not reaching
    # the server -- long enough that it is not a passing hiccup.
    QUIET_MISSES = 3

    def _set(self, mode: str, problem: str) -> None:
        with self._lock:
            if problem:
                self._misses += 1
                if self._misses < self.QUIET_MISSES:
                    # Keep whatever was last known and say nothing.
                    self._server = {**self._server, "asked": True}
                    return
                problem = ("not reachable for a while — this computer is "
                           "still rendering normally")
            else:
                self._misses = 0
            self._server = {"mode": mode, "problem": problem, "asked": True}


def serve(panel: Panel, port: int = 5055) -> str:
    """Start the panel on a background thread. Returns its URL, or ''."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass                      # the console belongs to the render

        def _send(self, code, body: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, code=200) -> None:
            self._send(code, json.dumps(payload).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self) -> None:                     # noqa: N802
            path = self.path.split("?")[0]
            if path == "/":
                self._send(200, PAGE.replace("__PORT__", str(port))
                           .encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/state":
                self._json(panel.full())
            else:
                self._send(404, b"no", "text/plain")

        def do_POST(self) -> None:                    # noqa: N802
            path = self.path.split("?")[0]
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, TypeError):
                body = {}

            if path == "/working":
                if "on" not in body:
                    self._json({"problem": "no state given"}, code=400)
                    return
                problem = panel.set_working(bool(body.get("on")))
                if problem:
                    self._json({"problem": problem}, code=400)
                    return
            elif path == "/dismiss":
                problem = panel.dismiss(str(body.get("job") or ""))
                if problem:
                    self._json({"problem": problem}, code=400)
                    return
            elif path == "/render-now":
                problem = panel.render_now(str(body.get("job") or ""),
                                           str(body.get("score") or ""))
                if problem:
                    self._json({"problem": problem}, code=400)
                    return
            elif path == "/publish":
                problem = panel.publish_score(str(body.get("edition") or ""))
                if problem:
                    self._json({"problem": problem}, code=400)
                    return
            elif path == "/stop":
                panel.stop_after()
            elif path == "/forget-limits":
                problem, cleared = panel.forget_limits()
                if problem:
                    self._json({"problem": problem}, code=400)
                    return
                self._json({**panel.full(), "cleared": cleared})
                return
            else:
                self._send(404, b"no", "text/plain")
                return
            self._json(panel.full())

    try:
        # 127.0.0.1, never 0.0.0.0: this pauses a renderer and writes to
        # the production server's configuration.
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        logger.warning("could not open the panel on port %d: %s", port, exc)
        return ""

    threading.Thread(target=server.serve_forever, name="panel",
                     daemon=True).start()
    return f"http://127.0.0.1:{port}/"


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------
PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>This computer</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600&family=Inter:wght@400;500;600&display=swap">
<style>
/* Contrast is a requirement, not a taste. An earlier version set 9.5px
   uppercase labels in #9a92a6 on #f6f1e8 -- 3.1:1, and unreadable. Nothing
   below is lighter than 5.4:1 against the surface it sits on. */
:root{
  --paper:#f4efe6; --surface:#fffdfa; --raise:#fbf7f0;
  --ink:#191320; --soft:#4a4356; --quiet:#655d73;
  --line:rgba(25,19,32,.16); --line-2:rgba(25,19,32,.09);
  --b1:#3d1e5c; --b2:#5c2f86; --mag:#b81e6e; --good:#1d6b4a;
  --serif:Fraunces,Georgia,serif;
  --sans:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  --mono:ui-monospace,"Cascadia Mono",Consolas,monospace;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --paper:#14111a; --surface:#1e1926; --raise:#251f2f;
    --ink:#f4eefa; --soft:#c3bad0; --quiet:#a79db6;
    --line:rgba(244,238,250,.20); --line-2:rgba(244,238,250,.10);
    --b1:#c9aef0; --b2:#d8c4f5; --mag:#ff8ec4; --good:#6fd6a6;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);
  font-size:16px;line-height:1.6;-webkit-font-smoothing:antialiased}
.wrap{max-width:560px;margin:0 auto;padding:38px 20px 60px}
header{display:flex;align-items:baseline;gap:10px;margin-bottom:30px;
  color:var(--quiet);font-size:13px}
.wordmark{font-family:var(--serif);font-weight:600;font-size:16px;
  color:var(--ink)}

/* Reported, not chosen -- so it must not look like something to pick. */
.status{border-left:3px solid var(--good);padding:2px 0 2px 16px}
.status.busy{border-left-color:var(--mag)}
.status.off{border-left-color:var(--quiet)}
.now{font-family:var(--serif);font-size:24px;font-weight:600;
  letter-spacing:-.015em;line-height:1.25;margin:0;text-wrap:balance}
.sub{color:var(--soft);margin:5px 0 0;font-size:14.5px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;
  margin-right:10px;vertical-align:middle;background:var(--quiet)}
.dot.live{background:var(--mag);animation:pulse 1.6s ease-in-out infinite}
.dot.on{background:var(--good)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
@media (prefers-reduced-motion:reduce){.dot.live{animation:none}}

.stages{display:flex;gap:4px;margin-top:16px}
.stg{flex:1}
.stg i{display:block;height:4px;border-radius:2px;background:var(--line-2)}
.stg.done i{background:var(--b2)}
.stg.now i{background:var(--mag)}
.stg span{font-size:11px;color:var(--quiet);margin-top:6px;display:block;
  text-align:center}
.stg.now span{color:var(--mag);font-weight:600}
.detail{margin:13px 0 0;color:var(--soft);font-size:14px}

/* THE ONE CONTROL. */
.control{display:flex;align-items:center;gap:18px;margin:30px 0 0;
  background:var(--surface);border:1.5px solid var(--line);border-radius:8px;
  padding:20px 22px;transition:border-color .18s}
.control.on{border-color:var(--b1)}
.knob{flex:0 0 auto;width:62px;height:34px;border-radius:99px;padding:0;
  border:0;background:var(--quiet);position:relative;cursor:pointer;
  transition:background .18s}
.knob::after{content:"";position:absolute;top:4px;left:4px;width:26px;
  height:26px;border-radius:50%;background:#fff;
  box-shadow:0 1px 3px rgba(0,0,0,.35);transition:transform .18s}
.knob[aria-checked="true"]{background:var(--b1)}
.knob[aria-checked="true"]::after{transform:translateX(28px)}
.knob:focus-visible{outline:3px solid var(--mag);outline-offset:3px}
.knob[disabled]{opacity:.45;cursor:default}
@media (prefers-reduced-motion:reduce){.knob,.knob::after{transition:none}}
.label{flex:1}
.label b{display:block;font-size:17px;font-weight:600}
.label span{display:block;color:var(--soft);font-size:14px;margin-top:2px}

.after{margin-top:16px}
button.plain{font:inherit;font-size:13.5px;font-weight:600;border-radius:5px;
  cursor:pointer;padding:10px 18px;border:1.5px solid var(--line);
  background:var(--raise);color:var(--ink)}
button.plain:hover{border-color:var(--ink)}
button.plain:focus-visible{outline:3px solid var(--mag);outline-offset:2px}

.hint{color:var(--soft);font-size:14px;margin:0 0 12px}
h2{font-family:var(--serif);font-size:17px;font-weight:600;margin:34px 0 12px}
.ask{background:var(--surface);border:1.5px solid var(--line);
  border-radius:8px;padding:14px 16px;margin-bottom:9px}
.ask.ready{border-color:var(--good)}
.ask .pc{font-weight:600;font-size:15.5px}
.ask .mt{display:block;color:var(--soft);font-size:13.5px;margin-top:2px}
.ask .row{display:flex;align-items:center;gap:10px;margin-top:11px;
  flex-wrap:wrap}
.ask .fold{font-family:var(--mono);font-size:11.5px;color:var(--soft);
  background:var(--line-2);padding:3px 7px;border-radius:3px;
  word-break:break-all}
.none{color:var(--soft);margin:0}
.facts{display:flex;flex-wrap:wrap;gap:24px;padding:16px 18px;
  background:var(--surface);border:1px solid var(--line);border-radius:8px}
.fact .n{font-family:var(--serif);font-size:20px;font-weight:600;
  font-variant-numeric:tabular-nums;line-height:1.2}
.fact .k{font-size:12.5px;color:var(--soft)}
.problem{color:var(--mag);font-size:14px;margin:14px 0 0;min-height:1.3em;
  font-weight:500}
footer{margin-top:34px;color:var(--soft);font-size:13.5px;line-height:1.6}
footer code{font-family:var(--mono);font-size:12.5px;color:var(--ink)}
</style></head><body>
<div class="wrap">
<header><span class="wordmark">weefeen</span><span>this computer</span></header>

<div class="status" id="status">
  <p class="now"><span class="dot" id="dot"></span><span id="nowtext">…</span></p>
  <p class="sub" id="sub"></p>
  <div id="extra"></div>
</div>

<div class="control" id="control">
  <button class="knob" id="knob" role="switch" aria-checked="false"
          aria-labelledby="knoblabel"></button>
  <span class="label" id="knoblabel"><b id="knobtitle">…</b>
    <span id="knobsub"></span></span>
</div>
<p class="problem" id="problem"></p>

<h2>Asked for, not engraved</h2>
<p class="hint" id="askhint">Pieces somebody wants that have no score yet.
  An <b>upload</b> is a person who chose a piece and left an address: publish
  the score and start their render from here. A <b>YouTube link</b> needs
  nothing from you but the score: publish it under the folder name shown and
  every link waiting on it aligns by itself.</p>
<div id="asks"></div>

<h2>Scores to send</h2>
<p class="hint">Projects on this computer that the site does not have, or
  has in an older form: the score files here and there are compared, not
  their names. <b>Publish</b> sends a new one; <b>Update</b> replaces the
  site&rsquo;s copy with what is in the folder now. Both check the score
  first and say what they find.</p>
<div id="scores"></div>

<h2>What this computer can take</h2>
<div class="facts" id="facts"></div>

<h2>Testing</h2>
<p class="hint">The site allows one visitor three videos a week, which is
  right for a visitor and impossible for whoever is proving it works.</p>
<div class="control">
  <span class="label"><b>Clear the weekly limits</b>
    <span>Everyone&rsquo;s, on the whole site — uploads, renders and mail.
      There is no way to clear one person&rsquo;s.</span></span>
  <button class="plain" id="forget">Clear</button>
</div>
<p class="problem" id="cleared"></p>

<footer>This page is on this computer only — <code>127.0.0.1:__PORT__</code>.
  Closing it changes nothing. Closing the black <code>lend.bat</code> window
  stops this computer taking videos, exactly like the switch above.
  No machine is ever rented.</footer>
</div>

<script>
const STAGES = [['prepare','ready'],['align','align'],['bands','bands'],
                ['strip','time'],['encode','encode'],['done','done']];
/* A YOUTUBE LINK IS NOT A RENDER, and must not be called "Making a video":
   no video is made, and on a piece nobody has engraved nothing is aligned
   either. Its steps are its own. `identify` has to happen before anything
   else can be decided -- it is how we know which score the link needs, and
   so which folder name will release it from the waiting list. */
const LINK_STAGES = [['VALIDATING','fetch'],['IDENTIFYING','identify'],
                     ['SYNCHRONISING','align'],['done','done']];
const LINK_NOW = {
  VALIDATING: 'Fetching the sound of the video. No picture is downloaded.',
  IDENTIFYING: 'Listening to find which piece it is, so we know which score it needs. '
             + 'If nobody has engraved that score, it goes on the waiting list below and stops there.',
  SYNCHRONISING: 'The score exists: lining it up with the playing, bar by bar.',
};
let busy = false;

async function send(path, body){
  busy = true;
  document.getElementById('problem').textContent = '';
  try{
    const r = await fetch(path, {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body || {})});
    const s = await r.json();
    if(!r.ok) document.getElementById('problem').textContent =
      s.problem || 'That did not go through.';
    else paint(s);
  }catch(e){
    document.getElementById('problem').textContent = e.message;
  }finally{ busy = false; }
}

function clock(s){
  if(s == null) return '—';
  const m = Math.floor(s/60), r = Math.floor(s%60);
  return m ? m + 'm ' + String(r).padStart(2,'0') + 's' : r + 's';
}

function paint(s){
  const job = s.job, on = s.taking;
  const box = document.getElementById('status');
  const dot = document.getElementById('dot');
  const text = document.getElementById('nowtext');
  const sub = document.getElementById('sub');
  const extra = document.getElementById('extra');

  if(job){
    const link = String(job.id || '').indexOf('perf:') === 0;
    const steps = link ? LINK_STAGES : STAGES;
    box.className = 'status busy'; dot.className = 'dot live';
    text.textContent = link ? 'Preparing a YouTube link' : 'Making a video';
    sub.textContent = job.piece + ' · running ' + clock(job.running_for) +
      (job.minutes ? ' · ' + job.minutes.toFixed(1) + ' min of music' : '');
    const at = steps.findIndex(([k]) => k === job.stage);
    const said = job.detail || (link ? (LINK_NOW[job.stage] || '') : '');
    extra.innerHTML = '<div class="stages">' + steps.map(([k,l],i) =>
      '<div class="stg ' + (i < at ? 'done' : i === at ? 'now' : '') +
      '"><i></i><span>' + l + '</span></div>').join('') + '</div>' +
      (said ? '<p class="detail">' + said + '</p>' : '') +
      '<div class="after"><button class="plain" id="stop">Finish this one, ' +
      'then stop</button></div>';
    document.getElementById('stop').onclick = () => send('/stop');
  }else if(on){
    box.className = 'status'; dot.className = 'dot on';
    text.textContent = 'Ready';
    sub.textContent = s.quiet_for > 0
      ? 'Standing aside for ' + clock(s.quiet_for) + ' after handing one back.'
      : 'Waiting for an upload or a YouTube link.';
    extra.innerHTML = '';
  }else if(s.quiet_for > 0){
    // NOT THE SAME AS "on hold", and saying so mattered: a machine standing
    // aside after handing a job back read as switched off, and the switch
    // looked broken because it could not clear a timer it did not know about.
    box.className = 'status off'; dot.className = 'dot';
    text.textContent = 'Standing aside for ' + clock(s.quiet_for);
    sub.textContent = 'It handed a job back, so it is letting another '
      + 'machine take it. Switch off and on again to take work now.';
    extra.innerHTML = '';
  }else{
    box.className = 'status off'; dot.className = 'dot';
    text.textContent = 'Everything is on hold';
    sub.textContent = 'Uploads wait in the queue until you switch this on.';
    extra.innerHTML = '';
  }

  const knob = document.getElementById('knob');
  knob.setAttribute('aria-checked', on ? 'true' : 'false');
  knob.disabled = busy;
  document.getElementById('control').className = 'control' + (on ? ' on' : '');
  document.getElementById('knobtitle').textContent =
    on ? 'Taking videos' : 'On hold';
  document.getElementById('knobsub').textContent = on
    ? 'Every upload is made on this computer.'
    : 'Nothing is made anywhere. Nothing is rented.';

  // "Not asked yet" is not "broken": the placeholder used to be drawn as a
  // failure, so the page opened accusing the server of being unreachable
  // before the first look had returned.
  document.getElementById('problem').textContent =
    (s.server && s.server.asked && s.server.problem)
      ? 'The server could not be reached: ' + s.server.problem : '';

  PUBLISH = s.publish || {};
  drawAsks(s.wanted || []);
  drawScores(s.scores || [], !!s.index_read);

  document.getElementById('facts').innerHTML = [
    [s.free_gb == null ? '—' : s.free_gb.toFixed(1) + ' GB', 'memory free now'],
    [s.longest_min == null ? '—' : Math.floor(s.longest_min) + ' min',
     'longest video it can take'],
    [s.scores_here + ' of ' + s.scores_total, 'scores downloaded'],
  ].map(([n,k]) => '<div class="fact"><div class="n">' + n +
                   '</div><div class="k">' + k + '</div></div>').join('');
}

document.getElementById('forget').onclick = async function(){
  const note = document.getElementById('cleared');
  this.disabled = true; note.textContent = 'Clearing…';
  try{
    const r = await fetch('/forget-limits', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:'{}'});
    const s = await r.json();
    note.textContent = r.ok
      ? 'Cleared ' + s.cleared + ' counter(s). Upload again straight away.'
      : (s.problem || 'That did not go through.');
  }catch(e){ note.textContent = e.message; }
  this.disabled = false;
};

/* A SOUND, because this is the one thing on the page worth interrupting
 * somebody for: a person uploaded a piece, was told we cannot make it, and
 * went away. Everything else here can wait until the page is looked at.
 *
 * Synthesised rather than a file: the panel has no build step and no
 * assets, and a two-note figure is six lines of Web Audio. Browsers refuse
 * to make noise before the page has been interacted with, which is correct
 * and also means the first one may be silent -- the list is still right. */
let knownAsks = null;
function bip(){
  try{
    const ac = new (window.AudioContext || window.webkitAudioContext)();
    [0, 0.16].forEach((at, i) => {
      const o = ac.createOscillator(), g = ac.createGain();
      o.type = 'sine';
      o.frequency.value = i ? 1174.66 : 880;      // A5 then D6
      g.gain.setValueAtTime(0.0001, ac.currentTime + at);
      g.gain.exponentialRampToValueAtTime(0.22, ac.currentTime + at + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, ac.currentTime + at + 0.14);
      o.connect(g); g.connect(ac.destination);
      o.start(ac.currentTime + at); o.stop(ac.currentTime + at + 0.16);
    });
  }catch(e){}
}

function ago(ms){
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if(s < 90) return 'just now';
  if(s < 5400) return Math.round(s / 60) + ' min ago';
  if(s < 172800) return Math.round(s / 3600) + ' h ago';
  return Math.round(s / 86400) + ' days ago';
}

let PUBLISH = {};

/* WHAT A ROW OFFERS WHEN ITS SCORE IS NOT PUBLISHED. The folder is on this
   computer: a button. It is being published: what the installer is saying.
   It just was: whether it worked, and why not if it did not. Otherwise: the
   instruction, as before. */
function publishBit(edition, r, afterwards){
  const p = PUBLISH || {};
  if(p.edition === edition && p.running){
    const last = (p.tail || []).slice(-1)[0] || '';
    return '<span class="mt"><b>Publishing…</b> ' + last + '</span>';
  }
  if(p.edition === edition && p.ok === true){
    return '<span class="mt"><b>Published.</b> ' + afterwards + '</span>';
  }
  const failed = (p.edition === edition && p.ok === false)
    ? '<span class="mt"><b>Not published.</b> ' + (p.tail || []).slice(-3).join(' · ')
      + '</span> '
    : '';
  if(r.local){
    return failed + '<button class="plain" data-publish="' + edition + '">'
      + 'Publish this score</button> <span class="mt">The folder is on this computer. '
      + 'It is checked first, then sent to the server.</span>';
  }
  return failed;
}

function drawAsks(rows){
  const box = document.getElementById('asks');
  if(!box) return;

  // NEW ONES ONLY, and never on the first draw -- otherwise opening the
  // page plays a sound for a backlog somebody already knows about.
  // `job` is null on every link row, so keying on it alone made them all
  // the same item and the chime stopped noticing new ones.
  const ids = rows.map(r => r.job || ('p:' + r.perf));
  if(knownAsks !== null){
    if(ids.some(id => !knownAsks.includes(id))) bip();
  }
  knownAsks = ids;

  if(!rows.length){
    box.innerHTML = '<p class="none">Nothing waiting. Every piece anybody '
      + 'uploaded or linked, we had the score for.</p>';
    return;
  }
  box.innerHTML = rows.map(r => {
    const ready = (r.ready || []).length ? r.ready[0] : '';
    const folders = (r.editions || []).map(e =>
      '<span class="fold">' + e + '</span>').join(' ');

    /* A LINK IS NOT AN UPLOAD and must not borrow its words. Nobody is
       waiting on an email, there is no file of theirs to render, and when
       the score is published the alignment starts itself -- so "Make it
       now" would be a button lying about who does the work. What a link row
       has to say instead is how many videos one engraving would release,
       and the exact folder name to publish under, because that name is what
       finds them again. */
    if(r.kind === 'link'){
      const n = r.waiting || 1;
      const vids = (r.videos || []).map(v =>
        '<div class="mt">· <a href="' + v.url + '" target="_blank" rel="noopener">'
        + (v.title || v.id) + '</a></div>').join('');
      return '<div class="ask' + (ready ? ' ready' : '') + '">'
        + '<span class="pc">' + (r.piece || 'an unnamed piece') + '</span>'
        + '<span class="mt">' + ago(r.at) + ' · ' + n + ' video'
        + (n === 1 ? '' : 's') + ' waiting on this score · audio already here'
        + '</span>'
        + '<div class="row">' + folders + '</div>'
        + vids
        + '<div class="row">'
        + (ready
           ? '<span class="mt">The score is published — these finish on their own, nothing to press.</span>'
           : (publishBit(r.editions && r.editions[0], r,
                         'The waiting videos align by themselves; nothing else to press.')
              || '<span class="mt">Engrave it under exactly that folder name, '
                 + 'and a button to publish it appears here.</span>'))
        + '</div></div>';
    }

    const act = (ready
      ? '<button class="plain" data-job="' + r.job + '" data-score="' + ready
        + '">Make it now</button>'
      : (publishBit(r.editions && r.editions[0], r,
                    'Their render can be started once the list refreshes.')
         || '<span class="mt">Engrave one of these and publish it, then this '
            + 'turns into a button.</span>'))
      // NOT A DELETE. Somebody asked for this and is entitled to the
      // answer, so closing it tells them rather than making the request
      // disappear silently.
      + ' <button class="plain" data-drop="' + r.job
      + '" title="Tell them we will not be making this one">Not this one'
      + '</button>';
    return '<div class="ask' + (ready ? ' ready' : '') + '">'
      + '<span class="pc">' + (r.piece || 'an unnamed piece') + '</span>'
      + '<span class="mt">' + ago(r.at) + (r.country ? ' · ' + r.country : '')
      + (r.minutes ? ' · ' + r.minutes.toFixed(1) + ' min' : '')
      + (r.confirmed ? ' · confirmed address'
         : r.address ? ' · address not confirmed yet'
         : ' · no address')
      + '</span>'
      + '<div class="row">' + folders + '</div>'
      + '<div class="row">' + act + '</div></div>';
  }).join('');

  box.querySelectorAll('button[data-job]').forEach(b => {
    b.onclick = () => send('/render-now',
                           {job: b.dataset.job, score: b.dataset.score});
  });
  box.querySelectorAll('button[data-drop]').forEach(b => {
    b.onclick = () => send('/dismiss', {job: b.dataset.drop});
  });
  box.querySelectorAll('button[data-publish]').forEach(b => {
    b.onclick = () => send('/publish', {edition: b.dataset.publish});
  });
}

/* EVERY SCORE ON THIS COMPUTER, each with the one button it needs. The
   same installer runs either way; what differs is the word, because
   "publish" on a score the site already has would read as a mistake, and
   the person pressing it is about to replace what visitors see. */
function drawScores(rows, indexed){
  const box = document.getElementById('scores');
  if(!box) return;
  if(!indexed){
    box.innerHTML = '<p class="none">Reading the site\u2019s index of scores\u2026</p>';
    return;
  }
  if(!rows.length){
    box.innerHTML = '<p class="none">Nothing to send. Every score engraved on this '
      + 'computer is on the site, as it is here.</p>';
    return;
  }
  box.innerHTML = rows.map(r => {
    const p = PUBLISH || {};
    let act;
    if(p.edition === r.edition && p.running){
      act = '<span class="mt"><b>' + (r.status === 'new' ? 'Publishing…' : 'Updating…')
        + '</b> ' + ((p.tail || []).slice(-1)[0] || '') + '</span>';
    }else{
      const said = (p.edition === r.edition && p.ok === true)
        ? '<span class="mt"><b>Done.</b> The site has this version now.</span> '
        : (p.edition === r.edition && p.ok === false)
        ? '<span class="mt"><b>Not sent.</b> ' + (p.tail || []).slice(-3).join(' · ') + '</span> '
        : '';
      act = said + (r.status === 'behind' ? '' :
          '<button class="plain" data-publish="' + r.edition + '">'
        + (r.status === 'new' ? 'Publish this score'
         : r.status === 'diverged' ? 'Replace the site\u2019s copy with this one'
         : 'Update it on the site') + '</button>');
    }
    const WORDS = {
      changed: 'changed since the site received it',
      unverified: 'on the site from before the index existed \u2014 send it once to settle it',
      behind: 'the site has a NEWER save of this project than this computer \u2014 nothing to send; bring that one here first',
      diverged: 'edited here AND on the site since they were last the same \u2014 look at both before replacing either',
      'new': 'new \u2014 not on the site',
    };
    return '<div class="ask' + (r.status === 'new' ? '' : ' ready') + '">'
      + '<span class="pc">' + r.edition + '</span>'
      + '<span class="mt">' + (WORDS[r.status] || r.status)
      + ' · ' + (r.open ? 'open in the engraving tool' : 'closed (.spj)') + '</span>'
      + '<div class="row">' + act + '</div></div>';
  }).join('');
  box.querySelectorAll('button[data-publish]').forEach(b => {
    b.onclick = () => send('/publish', {edition: b.dataset.publish});
  });
}

document.getElementById('knob').onclick = function(){
  send('/working', {on: this.getAttribute('aria-checked') !== 'true'});
};

async function tick(){
  if(!busy){ try{ paint(await (await fetch('/state')).json()); }catch(e){} }
  setTimeout(tick, 1500);
}
tick();
</script></body></html>
"""
