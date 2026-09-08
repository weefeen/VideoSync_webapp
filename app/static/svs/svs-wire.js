'use strict';
/* Connect the interface to this server.
 *
 * The design package is a working mock: it holds a fixed library, a fixed
 * ranking, and a submit button that only changes screen. Everything else —
 * the frame editor, the poster stills, the rights gate, the address memory
 * — already works client-side and is left alone.
 *
 * This file replaces exactly four things:
 *
 *     WORKS    the library            <- GET  /api/library
 *     CANDS    the recognised piece   <- POST /api/jobs/<id>/identify
 *     upload   read locally only      <- POST /api/upload
 *     #submit  changed screen only    <- POST /api/jobs/<id>/render
 *
 * Two additions the mock had no need for. Recognition takes the better
 * part of a minute, so there is a state for listening; and a piece can be
 * recognised whose score is not installed here, which is neither "we heard
 * it" nor "we could not place it" and must not be told as either.
 */

const API = {
  library:  '/api/library',
  upload:   '/api/upload',
  identify: id => `/api/jobs/${id}/identify`,
  answer:   id => `/api/jobs/${id}/identification`,
  render:   id => `/api/jobs/${id}/render`,
};

let JOB = null;                 // the job this upload belongs to
let SERVER = { can_identify: false, can_sync: false };

/* ── the library ─────────────────────────────────────────────────────── */
async function loadLibrary(){
  try{
    const r = await fetch(API.library);
    if(!r.ok) throw new Error(r.status);
    const data = await r.json();
    SERVER = { can_identify: data.can_identify, can_sync: data.can_sync };
    if(Array.isArray(data.works)) WORKS = data.works;
  }catch(err){
    // Leave the shipped library in place: the editor still works, and the
    // person can still choose by hand.
    console.warn('library unavailable, keeping the built-in list', err);
  }
}

/* ── recognition states the mock did not have ────────────────────────── */
const baseRecognise = recognise;

function fileBar(meta){
  $('#pieceblock').classList.toggle('on', !!S.file);
  $('#advance').classList.toggle('on', !!S.file && !!S.piece);
  $('#uploadstate').classList.toggle('hide', !!S.file);
  $('#filebar').classList.toggle('on', !!S.file);
  $('#filebar').classList.toggle('found', S.recog === 'heard');
  $('#fbName').textContent = S.file || '';
  $('#fbMeta').innerHTML = `${S.src.label} · ${S.src.dur} · ${meta}`;
}

recognise = function(){
  if(S.recog === 'listening'){
    fileBar('<span class="lab">listening</span>');
    $('#piecelab').textContent = 'Listening';
    $('#piecehint').textContent = '';
    $('#recogbody').innerHTML = `
      <p class="heard">Listening to your recording and comparing it with the
      library. <b>This takes about a minute.</b><br>Nothing else is needed
      from you meanwhile.</p>`;
    return;
  }
  if(S.recog === 'unavailable'){
    const named = CANDS.length ? (W(CANDS[0][0])?.t || S.heardLabel) : S.heardLabel;
    fileBar('<span class="ok">recognised</span>');
    $('#piecelab').textContent = 'Recognised';
    $('#piecehint').textContent = '';
    $('#recogbody').innerHTML = `
      <p class="heard">We heard <b>${esc(named || 'this piece')}</b>, but that
      score is not in the library yet.<br>It is being added one at a time —
      until then, pick something else below.</p>
      <div class="manual"><span class="lab">What we can do today</span>
        <div class="pieces" id="pieces"></div></div>`;
    manualList();
    return;
  }
  return baseRecognise();
};

/* ── upload, then listen ─────────────────────────────────────────────── */
async function uploadAndIdentify(name, blob){
  S.file = name;
  S.piece = null;
  S.manual = false;
  S.recog = 'listening';
  draw();
  centerOn('#pieceblock', 240);

  let data;
  try{
    const form = new FormData();
    form.append('video', blob, name);
    const r = await fetch(API.upload, { method:'POST', body: form });
    data = await r.json();
    if(!r.ok) throw new Error(data.error || `upload failed (${r.status})`);
  }catch(err){
    return failed(err.message || 'The upload did not go through.');
  }

  JOB = data.job.id;
  if(data.probe && data.probe.duration) S.src.dur = clock(data.probe.duration);

  if(!SERVER.can_identify){
    // Without a recogniser the library is still usable by hand; say so
    // rather than pretending nothing was heard.
    return unheard();
  }

  try{
    await fetch(API.identify(JOB), { method:'POST' });
  }catch(err){
    return failed('Could not start listening.');
  }
  poll();
}

function clock(seconds){
  const s = Math.round(seconds);
  return `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`;
}

function failed(message){
  S.recog = 'none';
  draw();
  const body = $('#recogbody');
  if(body) body.insertAdjacentHTML('afterbegin',
    `<p class="heard"><b class="alert">${esc(message)}</b></p>`);
}

/* Ask until it answers. The work is on a thread over there; this only
 * decides when to stop waiting. */
async function poll(started){
  started = started || Date.now();
  if(Date.now() - started > 5 * 60 * 1000) return failed('Listening timed out.');
  let a;
  try{
    const r = await fetch(API.answer(JOB));
    a = await r.json();
  }catch(err){
    return failed('Lost contact while listening.');
  }
  if(a.state === 'running' || a.state === 'idle'){
    return setTimeout(()=>poll(started), 2000);
  }
  if(a.state === 'error') return failed(a.error || 'Listening failed.');
  applyAnswer(a);
}

function applyAnswer(a){
  const cands = a.candidates || [];
  // The interface keys everything by the library's own ids, and the server
  // answers with the same ones, so a recognised piece the library knows is
  // selectable immediately.
  CANDS = cands.filter(c => c.renderable).map(c => [c.package, c.confidence]);
  S.heardLabel = cands.length ? cands[0].label : '';

  if(a.outcome === 'matched' && CANDS.length){
    S.recog = 'heard';
    choose(CANDS[0][0]);
    fileRow(S.file, 'recognised');
    draw();
    centerOn('#pieceblock', 240);
    return;
  }
  if(a.outcome === 'unavailable'){
    S.recog = 'unavailable';
    S.piece = null;
    draw();
    centerOn('#pieceblock', 240);
    return;
  }
  unheard();
}

/* ── send it to render ───────────────────────────────────────────────── */
const baseSubmit = $('#submit').onclick;

$('#submit').onclick = async function(){
  const address = $('#email').value.trim();
  if(!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(address)){
    emailNote('That address does not look right yet.', true);
    $('#email').focus();
    return;
  }
  if(!JOB || !S.piece){
    emailNote('Choose a recording and a piece first.', true);
    return;
  }
  emailNote('Sending it to render…');
  try{
    const r = await fetch(API.render(JOB), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        score: S.piece,
        style: {
          aspect: S.aspect,
          band_position: S.bandPos,
          panel: S.panel !== 'off',
          band_bg: S.bandColor,
          band_fg: S.noteColor,
          band_bg_opacity: S.alpha / 100,
          canvas_bg: S.bdColor,
          background: S.bd === 'colour' ? 'none' : S.bd,
        },
        meta: S.text,
        email: address,
      }),
    });
    const data = await r.json();
    if(!r.ok) throw new Error(data.error || `render refused (${r.status})`);
  }catch(err){
    emailNote(err.message || 'That did not go through.', true);
    return;
  }
  emailNote();
  baseSubmit();          // the screens, the address memory, the recap
};

/* ── take over the upload, leaving the rights gate exactly as it was ─── */
$('#rightsGo').onclick = ()=>{
  const name = pendingFile, blob = pendingBlob;
  $('#rights').classList.remove('on');
  pendingFile = null; pendingBlob = null; $('#file').value = '';
  if(blob) grabPosters(blob);              // stills are read locally, as before
  if(name && blob) uploadAndIdentify(name, blob);
};

loadLibrary().then(()=>{ if(!S.file) draw(); });
