"""The page that says what this machine is doing, and lets you change it.

WHY A PAGE. Lending a machine to the queue was two controls on two
machines: whether `tools/volunteer.py` was running here, and what
`COMPUTE_MODE` said in a .env file on the web box. Neither is visible from
the other, one of them is a keystroke in a scrolling terminal, and the only
way to answer "what is happening right now" was to read a log.

AND WHY IT IS SHAPED LIKE THIS. The first version exposed those two
controls faithfully -- a switch for this machine, a mode for that one --
and it was not usable. You cannot answer "what happens to the next upload"
by looking at either; you have to hold both in your head and combine them,
and combining them is the entire job. A control surface that models the
implementation makes its reader do the work the program should have done.

So the page asks ONE question -- where should videos be made -- and offers
the four answers that exist. Each answer sets both machines. What is left
is a status line that says what is happening right now, and the facts
about this computer that decide what it can accept.

    where should videos be made?   four outcomes, one choice, both machines
    what is happening now?         the render, its stage, how long it has run
    what can this computer take?   memory, and the length that follows from it

LOOPBACK ONLY, and that is not a detail. This page can pause a renderer and
change what the production server does with its money, and it has no login
because it is not reachable from anywhere that would need one. Bound to
127.0.0.1, refused otherwise.

No dependency: `http.server` from the standard library, and a page with no
build step. The machine this runs on is somebody's desktop, and a control
panel that needs an install is a control panel nobody opens.
"""
from __future__ import annotations

import html
import json
import logging
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger("volunteer.panel")

# The web box, for the one question this page asks about the other machine.
# ssh, not an API: this laptop already holds a key to that box, and the
# alternative is an authenticated admin endpoint on a public site -- a
# login to build and protect so that a page on loopback can read one word.
SERVER = "root@172.104.237.127"
ENV_PATH = "/srv/vsw/shared/.env"

# The mode may only ever be one of these. A WHITELIST, because the value is
# interpolated into a command that runs as root on the production server.
MODES = ("manual", "auto", "cloud")

# THE FOUR ANSWERS, and what each one means on the two machines. This is the
# whole of the page's logic: a reader chooses an outcome, and the two
# settings that produce it are ours to work out, not theirs.
#
#   taking  whether THIS machine consumes the queue
#   mode    what the WEB BOX does when this machine is not heard
#
# `rent` and `wait` both stop this machine consuming, because a choice that
# says "rent one" while this one quietly keeps taking work is a lie.
CHOICES = {
    "here": {
        "taking": True, "mode": "manual",
        "title": "On this computer",
        "cost": "free",
        "detail": "Only while this window is open. If it is closed, videos "
                  "wait in the queue until you open it again.",
    },
    "here_or_rent": {
        "taking": True, "mode": "auto",
        "title": "On this computer, or rent one when it is off",
        "cost": "free while this is open",
        "detail": "This computer takes everything it can. When it is closed "
                  "or busy, a machine is rented so nobody waits.",
    },
    "rent": {
        "taking": False, "mode": "cloud",
        "title": "Always rent a machine",
        "cost": "per video",
        "detail": "This computer takes nothing, even while it is running. "
                  "Every video is made on a rented machine.",
    },
    "wait": {
        "taking": False, "mode": "manual",
        "title": "Nowhere yet — let them wait",
        "cost": "free",
        "detail": "Nothing is rendered and nothing is rented. Uploads queue "
                  "up until you choose one of the above.",
    },
}


def choice_now(taking: bool, mode: str) -> str:
    """Which of the four the two machines are currently set to, or ''."""
    for name, spec in CHOICES.items():
        if spec["taking"] == bool(taking) and spec["mode"] == mode:
            return name
    return ""


class Panel:
    """What the page may ask of the volunteer, and nothing else."""

    def __init__(self, state, pause, resume, stop_after) -> None:
        self.state = state
        self.pause = pause
        self.resume = resume
        self.stop_after = stop_after
        self._mode = {"name": "", "problem": "reading…"}
        self._mode_lock = threading.Lock()

    def full(self) -> dict:
        """Everything the page draws, with the chosen outcome worked out."""
        state = self.state()
        mode = self.mode()
        return {**state, "mode": mode,
                "choice": choice_now(state.get("taking", False),
                                     mode.get("name", ""))}

    # -- the other machine ----------------------------------------------
    def mode(self) -> dict:
        with self._mode_lock:
            return dict(self._mode)

    def refresh_mode(self) -> None:
        """Read COMPUTE_MODE off the web box. Never raises."""
        try:
            done = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                 SERVER, f"grep -E '^COMPUTE_MODE=' {ENV_PATH} || true"],
                capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._set_mode("", f"could not reach the web box: {exc}")
            return
        if done.returncode != 0:
            self._set_mode("", (done.stderr or "").strip()[-200:]
                           or "the web box refused the connection")
            return
        name = ""
        for line in done.stdout.splitlines():
            if line.startswith("COMPUTE_MODE="):
                name = line.split("=", 1)[1].strip().lower()
        # Unset means `auto`, which is what app/store.py falls back to.
        self._set_mode(name or "auto", "")

    def choose(self, name: str) -> str:
        """Apply one of the four. Returns '' or a reason.

        THE WEB BOX FIRST. If it cannot be reached, nothing has changed
        anywhere -- where doing this machine first would leave the two
        halves disagreeing, which is the state the page exists to make
        impossible.
        """
        spec = CHOICES.get(name)
        if spec is None:
            return f"{name!r} is not one of the choices"
        problem = self.set_mode(spec["mode"])
        if problem:
            return problem
        (self.resume if spec["taking"] else self.pause)()
        return ""

    def set_mode(self, name: str) -> str:
        """Change it on the web box. Returns '' or a reason."""
        if name not in MODES:
            return f"{name!r} is not a mode"
        # The scaler reads this at start-up, so it is restarted; vsw-web
        # holds the same settings object and answers the page that tells a
        # visitor what to expect.
        command = (
            f"grep -q '^COMPUTE_MODE=' {ENV_PATH}"
            f" && sed -i 's/^COMPUTE_MODE=.*/COMPUTE_MODE={name}/' {ENV_PATH}"
            f" || echo 'COMPUTE_MODE={name}' >> {ENV_PATH};"
            f" systemctl restart vsw-scaler vsw-web")
        try:
            done = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                 SERVER, command],
                capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"could not reach the web box: {exc}"
        if done.returncode != 0:
            return (done.stderr or "").strip()[-200:] or "the change was refused"
        self.refresh_mode()
        logger.info("compute mode set to %s on the web box", name)
        return ""

    def _set_mode(self, name: str, problem: str) -> None:
        with self._mode_lock:
            self._mode = {"name": name, "problem": problem}


def serve(panel: Panel, port: int = 5055) -> str:
    """Start the panel on a background thread. Returns its URL, or ''."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass                      # the console belongs to the render

        def _send(self, code, body: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            # Nothing here is for anyone else to frame or cache.
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

            if path == "/choose":
                problem = panel.choose(str(body.get("choice") or ""))
                if problem:
                    self._json({"problem": problem}, code=400)
                    return
            elif path == "/stop":
                panel.stop_after()
            else:
                self._send(404, b"no", "text/plain")
                return
            self._json(panel.full())

    try:
        # 127.0.0.1, never 0.0.0.0: this pauses a renderer and spends money.
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
_CHOICE_ROWS = "".join(
    f'<label class="choice" data-choice="{name}">'
    f'<input type="radio" name="choice" value="{name}">'
    f'<span class="tick" aria-hidden="true"></span>'
    f'<span class="body"><span class="ct">{html.escape(c["title"])}'
    f'<em class="cost" data-cost="{name}">{html.escape(c["cost"])}</em></span>'
    f'<span class="cd">{html.escape(c["detail"])}</span></span></label>'
    for name, c in CHOICES.items())

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Where videos are made</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600&family=Inter:wght@400;500;600&display=swap">
<style>
/* Contrast is a requirement, not a taste. Every colour below was picked to
   clear 4.5:1 against the surface it sits on -- the previous palette used a
   9.5px uppercase label at #9a92a6 on #f6f1e8, which is 3.1:1 and could not
   be read. Nothing here is lighter than --soft, and --soft is 7:1. */
:root{
  --paper:#f4efe6; --surface:#fffdfa; --raise:#fbf7f0;
  --ink:#191320;        /* 16.1:1 on paper */
  --soft:#4a4356;       /*  7.9:1 -- body text that is not the point */
  --quiet:#655d73;      /*  5.4:1 -- the lightest thing allowed */
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
.wrap{max-width:640px;margin:0 auto;padding:34px 20px 64px}
header{display:flex;align-items:baseline;gap:10px;margin-bottom:30px;
  color:var(--quiet);font-size:13px}
.wordmark{font-family:var(--serif);font-weight:600;font-size:16px;
  color:var(--ink)}

/* WHAT IS HAPPENING, first, because it is what you came to find out. */
.status{background:var(--surface);border:1px solid var(--line);
  border-radius:6px;padding:18px 20px}
.status .now{font-family:var(--serif);font-size:23px;font-weight:600;
  letter-spacing:-.015em;line-height:1.25;margin:0;text-wrap:balance}
.status .sub{color:var(--soft);margin:5px 0 0;font-size:14.5px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;
  margin-right:9px;vertical-align:middle;background:var(--quiet)}
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
.after{margin-top:15px}

h2{font-family:var(--serif);font-size:19px;font-weight:600;letter-spacing:-.01em;
  margin:36px 0 4px}
.hint{color:var(--soft);font-size:14px;margin:0 0 14px}

/* ONE QUESTION, FOUR ANSWERS. Each sets both machines; the reader never
   has to combine two settings to know what happens next. */
.choice{display:flex;gap:13px;align-items:flex-start;background:var(--surface);
  border:1.5px solid var(--line);border-radius:6px;padding:14px 16px;
  margin-bottom:9px;cursor:pointer;transition:border-color .15s,background .15s}
.choice:hover{border-color:var(--quiet);background:var(--raise)}
.choice input{position:absolute;opacity:0;width:0;height:0}
.tick{flex:0 0 auto;width:18px;height:18px;border-radius:50%;margin-top:3px;
  border:2px solid var(--quiet);transition:border-color .15s}
.choice:has(input:checked){border-color:var(--b1);background:var(--raise)}
.choice:has(input:checked) .tick{border-color:var(--b1);
  box-shadow:inset 0 0 0 4px var(--b1)}
.choice:has(input:focus-visible){outline:3px solid var(--mag);outline-offset:2px}
.body{flex:1}
.ct{display:flex;flex-wrap:wrap;align-items:baseline;gap:9px;
  font-weight:600;font-size:15.5px}
.choice:has(input:checked) .ct{color:var(--b1)}
.cost{font-style:normal;font-size:12px;font-weight:500;color:var(--soft);
  background:var(--line-2);padding:2px 8px;border-radius:99px;white-space:nowrap}
.cd{display:block;color:var(--soft);font-size:14px;margin-top:3px}

.facts{display:flex;flex-wrap:wrap;gap:22px;padding:15px 17px;
  background:var(--surface);border:1px solid var(--line);border-radius:6px}
.fact .n{font-family:var(--serif);font-size:20px;font-weight:600;
  font-variant-numeric:tabular-nums;line-height:1.2}
.fact .k{font-size:12.5px;color:var(--soft)}

button{font:inherit;font-size:13px;font-weight:600;border-radius:4px;
  cursor:pointer;padding:10px 18px;border:1.5px solid var(--line);
  background:var(--raise);color:var(--ink);transition:border-color .15s}
button:hover:not([disabled]){border-color:var(--ink)}
button:focus-visible{outline:3px solid var(--mag);outline-offset:2px}
button[disabled]{opacity:.4;cursor:default}
.problem{color:var(--mag);font-size:14px;margin:12px 0 0;min-height:1.3em;
  font-weight:500}
footer{margin-top:36px;color:var(--soft);font-size:13.5px;line-height:1.6}
footer code{font-family:var(--mono);font-size:12.5px;color:var(--ink)}
</style></head><body>
<div class="wrap">
<header><span class="wordmark">weefeen</span><span>this computer</span></header>

<div class="status">
  <p class="now" id="now"><span class="dot" id="dot"></span><span id="nowtext">…</span></p>
  <p class="sub" id="sub"></p>
  <div id="extra"></div>
</div>

<h2>Where should videos be made?</h2>
<p class="hint">This sets both this computer and the server. Whatever you
  pick is what happens to the next upload.</p>
<div id="choices">__CHOICES__</div>
<p class="problem" id="problem"></p>

<h2>What this computer can take</h2>
<div class="facts" id="facts"></div>

<footer>This page is on this computer only — <code>127.0.0.1:__PORT__</code>.
  Closing it changes nothing. Closing the black <code>lend.bat</code> window
  is what stops this computer taking work.</footer>
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
  const job = s.job, dot = document.getElementById('dot');
  const text = document.getElementById('nowtext');
  const sub = document.getElementById('sub');
  const extra = document.getElementById('extra');

  // WHAT IS HAPPENING, in one sentence, before any control.
  if(job){
    dot.className = 'dot live';
    text.textContent = 'Making a video on this computer';
    sub.textContent = job.piece + ' · running ' + clock(job.running_for) +
      (job.minutes ? ' · ' + job.minutes.toFixed(1) + ' min of music' : '');
    const at = STAGES.findIndex(([k]) => k === job.stage);
    extra.innerHTML = '<div class="stages">' + STAGES.map(([k,l],i) =>
      '<div class="stg ' + (i < at ? 'done' : i === at ? 'now' : '') +
      '"><i></i><span>' + l + '</span></div>').join('') + '</div>' +
      (job.detail ? '<p class="detail">' + job.detail + '</p>' : '') +
      '<div class="after"><button id="stop">Finish this one, then stop</button></div>';
    document.getElementById('stop').onclick = () => send('/stop');
  }else{
    const taking = s.taking;
    dot.className = 'dot ' + (taking ? 'on' : '');
    text.textContent = taking ? 'Ready — nothing to do yet'
                              : 'This computer is not taking videos';
    sub.textContent = taking
      ? 'The next upload will be made here.'
      : (s.choice === 'rent' ? 'The next upload will be made on a rented machine.'
        : s.quiet_for > 0
          ? 'It just handed a job back; standing aside for ' + clock(s.quiet_for) + '.'
          : 'The next upload will wait in the queue.');
    extra.innerHTML = '';
  }

  // The one question.
  document.querySelectorAll('.choice input').forEach(i => {
    i.checked = (i.value === s.choice);
    i.disabled = busy || !(s.mode && s.mode.name);
  });
  if(s.mode && s.mode.problem)
    document.getElementById('problem').textContent =
      'The server could not be reached: ' + s.mode.problem;
  else if(!s.choice && s.mode && s.mode.name)
    document.getElementById('problem').textContent =
      'The two machines are set to a combination that is not one of these ' +
      '(server: ' + s.mode.name + '). Pick one to line them up.';

  // The price, from the server's own plan rather than typed in here.
  if(s.hourly_cost){
    const each = '~$' + s.hourly_cost.toFixed(2) + ' a video';
    document.querySelectorAll('[data-cost="rent"]').forEach(e =>
      e.textContent = each);
    document.querySelectorAll('[data-cost="here_or_rent"]').forEach(e =>
      e.textContent = each + ' only when off');
  }

  document.getElementById('facts').innerHTML = [
    [s.free_gb == null ? '—' : s.free_gb.toFixed(1) + ' GB', 'memory free now'],
    [s.longest_min == null ? '—' : Math.floor(s.longest_min) + ' min',
     'longest video it will accept'],
    [s.scores_here + ' of ' + s.scores_total, 'scores already downloaded'],
  ].map(([n,k]) => '<div class="fact"><div class="n">' + n +
                   '</div><div class="k">' + k + '</div></div>').join('');
}

document.querySelectorAll('.choice input').forEach(i => {
  i.onchange = () => { if(i.checked) send('/choose', {choice: i.value}); };
});

async function tick(){
  if(!busy){ try{ paint(await (await fetch('/state')).json()); }catch(e){} }
  setTimeout(tick, 1500);
}
tick();
</script></body></html>
""".replace("__CHOICES__", _CHOICE_ROWS)
