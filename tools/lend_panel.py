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
        self._lock = threading.Lock()
        # A LOOK THAT FAILS IS WEATHER. This reaches another machine over
        # the internet every few minutes, so it will fail sometimes -- and
        # the first version put the raw subprocess exception at the top of
        # the page, where it read as "this computer is broken" when nothing
        # about rendering had changed. Nothing is said until several looks
        # in a row have failed, and then it is said quietly.
        self._misses = 0

    def full(self) -> dict:
        """Everything the page draws."""
        with self._lock:
            wanted = list(self._wanted)
        return {**self.state(), "server": self.server(), "wanted": wanted}

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
        try:
            done = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
                 SERVER, "curl -fsS -m 20 http://127.0.0.1:5000/api/wanted"],
                capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.debug("could not read what is wanted: %s", exc)
            return
        if done.returncode != 0:
            return
        try:
            rows = json.loads(done.stdout)
        except (ValueError, TypeError):
            return
        if not isinstance(rows, list):
            return
        with self._lock:
            self._wanted = rows

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
            elif path == "/render-now":
                problem = panel.render_now(str(body.get("job") or ""),
                                           str(body.get("score") or ""))
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
<p class="hint" id="askhint">Pieces somebody uploaded that we recognised and
  have no score for. Once you publish the score, the render starts from
  here — the person who asked is still on the job.</p>
<div id="asks"></div>

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
    box.className = 'status busy'; dot.className = 'dot live';
    text.textContent = 'Making a video';
    sub.textContent = job.piece + ' · running ' + clock(job.running_for) +
      (job.minutes ? ' · ' + job.minutes.toFixed(1) + ' min of music' : '');
    const at = STAGES.findIndex(([k]) => k === job.stage);
    extra.innerHTML = '<div class="stages">' + STAGES.map(([k,l],i) =>
      '<div class="stg ' + (i < at ? 'done' : i === at ? 'now' : '') +
      '"><i></i><span>' + l + '</span></div>').join('') + '</div>' +
      (job.detail ? '<p class="detail">' + job.detail + '</p>' : '') +
      '<div class="after"><button class="plain" id="stop">Finish this one, ' +
      'then stop</button></div>';
    document.getElementById('stop').onclick = () => send('/stop');
  }else if(on){
    box.className = 'status'; dot.className = 'dot on';
    text.textContent = 'Ready';
    sub.textContent = s.quiet_for > 0
      ? 'Standing aside for ' + clock(s.quiet_for) + ' after handing one back.'
      : 'Waiting for someone to upload a performance.';
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

  drawAsks(s.wanted || []);

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

function drawAsks(rows){
  const box = document.getElementById('asks');
  if(!box) return;

  // NEW ONES ONLY, and never on the first draw -- otherwise opening the
  // page plays a sound for a backlog somebody already knows about.
  const ids = rows.map(r => r.job);
  if(knownAsks !== null){
    if(ids.some(id => !knownAsks.includes(id))) bip();
  }
  knownAsks = ids;

  if(!rows.length){
    box.innerHTML = '<p class="none">Nothing waiting. Every piece anybody '
      + 'uploaded, we had the score for.</p>';
    return;
  }
  box.innerHTML = rows.map(r => {
    const ready = (r.ready || []).length ? r.ready[0] : '';
    const folders = (r.editions || []).map(e =>
      '<span class="fold">' + e + '</span>').join(' ');
    const act = ready
      ? '<button class="plain" data-job="' + r.job + '" data-score="' + ready
        + '">Make it now</button>'
      : '<span class="mt">Engrave one of these and publish it, then this '
        + 'turns into a button.</span>';
    return '<div class="ask' + (ready ? ' ready' : '') + '">'
      + '<span class="pc">' + (r.piece || 'an unnamed piece') + '</span>'
      + '<span class="mt">' + ago(r.at) + (r.country ? ' · ' + r.country : '')
      + (r.minutes ? ' · ' + r.minutes.toFixed(1) + ' min' : '')
      + (r.address ? ' · they left an address' : ' · no address')
      + '</span>'
      + '<div class="row">' + folders + '</div>'
      + '<div class="row">' + act + '</div></div>';
  }).join('');

  box.querySelectorAll('button[data-job]').forEach(b => {
    b.onclick = () => send('/render-now',
                           {job: b.dataset.job, score: b.dataset.score});
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
