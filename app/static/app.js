'use strict';
/* Score Video Sync — client.
 *
 * Four screens: drop a video, choose how it should look, watch it render,
 * download it. Style choices are held in one object and posted with the
 * render request, so the controls have no other state to keep in sync.
 */

const state = {
  job: null,
  options: null,
  scores: [],
  style: {
    aspect: '16/9',
    background: 'none',
    band_position: 'bottom',
    panel: false,
    band_bg: '#ffffff',
    band_fg: '#1c1622',
    band_bg_opacity: 1.0,
  },
  mode: 'auto',
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

/* ---------- screens ---------- */
function show(name) {
  $$('[data-screen]').forEach((el) => el.classList.toggle('active', el.dataset.screen === name));
  banner(null);
}

function banner(message, kind = 'err') {
  const slot = $('#banner-slot');
  if (!message) { slot.innerHTML = ''; return; }
  slot.innerHTML =
    `<div class="banner${kind === 'warn' ? ' warn' : ''}">
       <div class="icon">!</div>
       <div><h4>${kind === 'warn' ? 'Heads up' : "That didn't work"}</h4><p></p></div>
     </div>`;
  slot.querySelector('p').textContent = message;   // never inject as HTML
}

/* ---------- demo hero ---------- */
/* A suggestion of notation drifting behind the headline. Deliberately
 * synthetic — the real thing needs an aligned recording, and this only has
 * to convey what the product does before anyone has uploaded anything. */
function staffSvg(seed) {
  const W = 900, H = 260, top = 70, gap = 13;
  let out = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">`;
  for (const staff of [0, 120]) {
    for (let i = 0; i < 5; i++) {
      const y = top + staff + i * gap;
      out += `<line x1="0" y1="${y}" x2="${W}" y2="${y}" stroke="rgba(255,255,255,.45)" stroke-width="1"/>`;
    }
  }
  // Pseudo-random but stable per copy, so the loop seam isn't obvious.
  let n = seed * 9301 + 49297;
  const rand = () => ((n = (n * 9301 + 49297) % 233280) / 233280);
  for (let x = 40; x < W - 30; x += 26 + rand() * 22) {
    const staff = rand() > 0.45 ? 0 : 120;
    const y = top + staff + Math.floor(rand() * 9) * (gap / 2);
    const stemUp = rand() > 0.5;
    out += `<ellipse cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" rx="5.4" ry="4" `
         + `fill="rgba(255,255,255,.85)" transform="rotate(-18 ${x.toFixed(1)} ${y.toFixed(1)})"/>`
         + `<line x1="${(x + (stemUp ? 5 : -5)).toFixed(1)}" y1="${y.toFixed(1)}" `
         + `x2="${(x + (stemUp ? 5 : -5)).toFixed(1)}" y2="${(y + (stemUp ? -34 : 34)).toFixed(1)}" `
         + `stroke="rgba(255,255,255,.8)" stroke-width="1.6"/>`;
  }
  return out + '</svg>';
}

function buildDemo() {
  const scroller = $('#demo-scroller');
  if (!scroller) return;
  scroller.innerHTML = [1, 2, 3].map(staffSvg).join('');

  const toggle = $('#demo-toggle');
  toggle.addEventListener('click', () => {
    const paused = scroller.classList.toggle('paused');
    toggle.textContent = paused ? '▶ play' : '⏸ pause';
  });

  // A drifting timecode, so the band reads as a moving picture.
  let t = 0;
  setInterval(() => {
    if (scroller.classList.contains('paused')) return;
    t = (t + 1) % (60 * 8);
    $('#demo-tc').textContent =
      `${String(Math.floor(t / 60)).padStart(2, '0')}:${String(t % 60).padStart(2, '0')}`;
  }, 1000);
}

/* ---------- setup ---------- */
async function boot() {
  buildDemo();
  try {
    state.options = await (await fetch('/api/options')).json();
    state.scores = (await (await fetch('/api/scores')).json()).scores;
  } catch (err) {
    banner('Could not reach the server. Is it still running?');
    return;
  }

  const o = state.options;
  $('#formats').textContent = `.mp4 · .mov · .avi · .mkv · up to ${o.max_upload_mb} mb`;
  $('#capabilities').textContent =
    [o.can_align ? 'alignment ready' : 'alignment unavailable',
     o.can_rasterize_svg ? 'vector bands' : 'raster bands'].join(' · ');

  // Backgrounds with no artwork configured can't be offered.
  o.backgrounds.forEach((b) => {
    const btn = document.querySelector(`#background-group [data-v="${b.value}"]`);
    if (btn && !b.available) {
      btn.disabled = true;
      btn.style.opacity = 0.4;
      btn.title = `No ${b.value} artwork configured (set BACKGROUND_${b.value.toUpperCase()} in .env)`;
    }
  });

  const sel = $('#score-select');
  sel.innerHTML = '';
  state.scores.forEach((s) => {
    const opt = document.createElement('option');
    opt.value = s.name;
    // The folder name is an internal identifier; show the work instead.
    opt.textContent = s.label || s.name;
    sel.appendChild(opt);
  });
  if (!state.scores.length) {
    sel.innerHTML = '<option>No scores available</option>';
    banner(o.composer_filter
      ? `No ${o.composer_filter} scores were found. Check SCORE_ROOT_* in .env, then run tools/doctor.py.`
      : 'No score packages were found. Check SCORE_ROOT_* in .env, then run tools/doctor.py.', 'warn');
  }
  sel.addEventListener('change', onScoreChange);
  onScoreChange();

  // The library is scoped to one composer for now; say so rather than
  // leaving a musician to wonder why their piece isn't listed.
  const who = o.composer_filter;
  $('#teaser-count').textContent = state.scores.length;
  $('#teaser-list').textContent =
    state.scores.map((s) => s.label || s.name).slice(0, 6).join(' · ') || '—';
  if (who) {
    $('#teaser-label').textContent =
      `${who} ${state.scores.length === 1 ? 'work' : 'works'} ready to sync`;
    $('#demo-title').textContent = `${who} · score band synced to your playing`;
    const h = document.querySelector('.demo-headline h1');
    if (h) h.innerHTML =
      `Turn your ${who} performance<br>into a <span class="italic">synced score video.</span>`;
  }

  if (!o.can_align) {
    banner('Alignment is unavailable: set MLE_ROOT and MLE_PYTHON in .env.', 'warn');
  }
  wireControls();
  updatePreview();
}

function currentScore() {
  return state.scores.find((s) => s.name === $('#score-select').value);
}

function onScoreChange() {
  const score = currentScore();
  if (!score) return;
  const refBtn = document.querySelector('#mode-group [data-v="reference"]');
  const allowed = score.modes.includes('reference');
  refBtn.disabled = !allowed;
  refBtn.style.opacity = allowed ? 1 : 0.4;
  refBtn.title = allowed ? '' : 'This package has no reference recording';
  if (!allowed && state.mode === 'reference') setToggle('mode', 'auto');
  $('#score-hint').textContent =
    [score.composer, `${score.measures} measures`,
     `${score.bands} bands${score.vector_bands ? ', vector' : ', raster'}`]
      .filter(Boolean).join(' · ');
}

/* ---------- controls ---------- */
function setToggle(group, value) {
  const container = document.querySelector(`[data-group="${group}"]`);
  if (!container) return;
  container.querySelectorAll('button').forEach((b) =>
    b.classList.toggle('on', b.dataset.v === value));
  applyControl(group, value);
}

function applyControl(group, value) {
  if (group === 'mode') { state.mode = value; describeMode(); return; }
  if (group === 'panel') { state.style.panel = value === 'on'; }
  else { state.style[group] = value; }
  updatePreview();
}

function describeMode() {
  const opt = (state.options.modes || []).find((m) => m.value === state.mode);
  $('#mode-hint').textContent = opt ? opt.label : '';
}

function wireControls() {
  $$('.toggle-group').forEach((group) => {
    group.addEventListener('click', (e) => {
      const btn = e.target.closest('button');
      if (!btn || btn.disabled) return;
      group.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b === btn));
      applyControl(group.dataset.group, btn.dataset.v);
    });
  });

  $$('.swatches').forEach((group) => {
    group.addEventListener('click', (e) => {
      const sw = e.target.closest('.swatch');
      if (!sw) return;
      group.querySelectorAll('.swatch').forEach((s) => s.classList.toggle('selected', s === sw));
      applyControl(group.dataset.group, sw.dataset.v);
    });
  });

  const slider = $('#band-bg-opacity');
  slider.addEventListener('input', () => {
    state.style.band_bg_opacity = slider.value / 100;
    $('#band-bg-opacity-val').textContent = `${slider.value}%`;
    updatePreview();
  });

  // The drop bar is a <label for="file-input">, so the button inside it
  // already opens the picker. Binding it again would open two.
  $('#file-input').addEventListener('change', (e) => {
    if (e.target.files.length) upload(e.target.files[0]);
  });

  const zone = $('#drop-zone');
  ['dragenter', 'dragover'].forEach((ev) =>
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add('hover'); }));
  ['dragleave', 'drop'].forEach((ev) =>
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove('hover'); }));
  zone.addEventListener('drop', (e) => {
    if (e.dataTransfer.files.length) upload(e.dataTransfer.files[0]);
  });

  $('#generate-btn').addEventListener('click', render);
  $$('[data-goto]').forEach((b) =>
    b.addEventListener('click', () => { state.job = null; show(b.dataset.goto); }));
  describeMode();
}

/* ---------- a rough picture of the chosen layout ---------- */
function updatePreview() {
  const s = state.style;
  const [aw, ah] = s.aspect.split('/').map(Number);
  const el = $('#layout-preview');
  if (!el) return;

  el.style.aspectRatio = `${aw} / ${ah}`;
  el.dataset.panel = s.panel ? 'on' : 'off';
  el.dataset.band = s.band_position;
  el.style.setProperty('--band-bg', s.band_bg);
  el.style.setProperty('--band-fg', s.band_fg);
  el.style.setProperty('--band-alpha', s.band_bg_opacity);

  el.innerHTML =
    (s.panel ? '<div class="lp-panel"></div>' : '') +
    '<div class="lp-stack">' +
      (s.band_position === 'top' ? '<div class="lp-band"></div>' : '') +
      '<div class="lp-video">your video</div>' +
      (s.band_position === 'bottom' ? '<div class="lp-band"></div>' : '') +
    '</div>';

  $('#layout-tag').textContent =
    `${s.background === 'none' ? 'plain' : s.background} background`;
  const sizes = { '16/9': '1920×1080', '1/1': '1080×1080', '9/16': '1080×1920' };
  $('#layout-size').textContent = `${s.aspect} · exports ${sizes[s.aspect]}`;
}

/* ---------- upload ---------- */
async function upload(file) {
  banner(null);
  $('#panel-status').textContent = 'uploading…';
  const body = new FormData();
  body.append('video', file);
  try {
    const res = await fetch('/api/upload', { method: 'POST', body });
    const data = await res.json();
    if (!res.ok) {
      banner([data.error, data.detail].filter(Boolean).join(' '));
      $('#panel-status').textContent = 'awaiting a recording';
      return;
    }
    state.job = data.job;
    const mins = Math.floor(data.probe.duration / 60);
    const secs = String(Math.round(data.probe.duration % 60)).padStart(2, '0');
    $('#ready-file').textContent = data.job.name;
    $('#ready-dur').textContent = `${mins}:${secs}`;
    $('#ready-score').textContent = `${data.probe.width}×${data.probe.height}`;
    $('#panel-status').textContent = 'ready to render';
    updatePreview();
    show('ready');
  } catch (err) {
    banner('The upload failed. Is the file still available?');
    $('#panel-status').textContent = 'awaiting a recording';
  }
}

/* ---------- render ---------- */
function metadata() {
  const out = {};
  $$('#meta-fields input').forEach((i) => { out[i.id.replace(/^m-/, '')] = i.value.trim(); });
  return out;
}

async function render() {
  if (!state.job) return;
  const score = $('#score-select').value;
  if (!score) { banner('Pick a score first.'); return; }

  $('#generate-btn').disabled = true;
  try {
    const res = await fetch(`/api/jobs/${state.job.id}/render`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ score, mode: state.mode, style: state.style, meta: metadata() }),
    });
    const data = await res.json();
    if (!res.ok) { banner(data.error); show('ready'); return; }
    state.job = data.job;
    $('#work-meta').textContent = `${data.job.name} · ${score}`;
    show('working');
    listen(state.job.id);
  } catch (err) {
    banner('Could not start the render.');
  } finally {
    $('#generate-btn').disabled = false;
  }
}

function renderStages(job) {
  const labels = { prepare: 'Prepare', align: 'Align', bands: 'Bands',
                   strip: 'Timing', encode: 'Encode', done: 'Done' };
  $('#work-stages').innerHTML = Object.entries(job.stages).map(([name, status]) =>
    `<div class="p-stage ${status === 'active' ? 'active' : status === 'done' ? 'done' : ''}">
       <span class="bullet"></span>${labels[name] || name}
     </div>`).join('');
}

function listen(jobId) {
  const source = new EventSource(`/api/jobs/${jobId}/events`);
  source.onmessage = (e) => {
    const evt = JSON.parse(e.data);
    if (evt.type === 'ping') return;
    const job = evt.job;
    if (!job) return;
    state.job = job;
    renderStages(job);
    if (evt.detail || job.detail) $('#work-detail').textContent = evt.detail || job.detail;
    if (job.mode_label) $('#work-headline').textContent = job.mode_label;

    if (evt.type === 'done') {
      source.close();
      const mb = job.output_bytes ? (job.output_bytes / 1e6).toFixed(1) : '?';
      $('#done-meta').textContent =
        `${job.mode_label} · ${mb} MB · ${job.elapsed}s`;
      $('#done-name').textContent = job.name.replace(/\.[^.]+$/, '') + '_score_sync.mp4';
      $('#download-btn').href = job.download;
      show('done');
    } else if (evt.type === 'error') {
      source.close();
      banner(job.error || 'The render failed.');
      show('ready');
    }
  };
  source.onerror = () => {
    source.close();
    banner('Lost contact with the server while rendering.');
    show('ready');
  };
}

document.addEventListener('DOMContentLoaded', boot);
