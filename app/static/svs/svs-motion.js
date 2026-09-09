'use strict';
/* Five ideas for movement on the landing page, so they can be looked at
 * rather than imagined. This file is a proposal, not a decision.
 *
 * Every effect is one function in MOTION below, independent of the others.
 * To keep one and drop the rest, delete the others from that object. To
 * drop all of it, remove the <script> tag for this file — nothing else in
 * the app refers to it.
 *
 * Try them one at a time in the address bar, which is the only way to
 * judge them honestly:
 *
 *     /app/                      all five, which is deliberately too many
 *     /app/?motion=drift         just the engraving behind the headline
 *     /app/?motion=scrub         scrolling plays the piece
 *     /app/?motion=rubato        the headline arrives in musical time
 *     /app/?motion=count         the tally counts up
 *     /app/?motion=settle        notes land on a staff
 *     /app/?motion=drift,count   any combination
 *     /app/?motion=none          nothing
 *
 * Anyone who has asked their system for less movement gets none of it,
 * whatever the address says. That is not politeness: for some people this
 * kind of motion causes nausea, and the setting exists to be obeyed.
 */

(function () {
  const REDUCED = window.matchMedia &&
        window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const ASKED = new URLSearchParams(location.search).get('motion');
  const WANTED = ASKED === null ? null
               : ASKED.split(',').map(s => s.trim().toLowerCase()).filter(Boolean);

  const wants = name =>
    !REDUCED && !(WANTED && (WANTED.includes('none') || !WANTED.includes(name)));

  const $ = sel => document.querySelector(sel);
  const ease = t => t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;

  /* ── 1. the engraving drifting behind the headline ─────────────────── */
  /* The quietest of the five, and the one that suits this design best: it
   * is the subject matter used as texture. A real band from the library,
   * held at low opacity, moving slowly enough that you notice it only if
   * you look. Nothing to read, nothing to chase. */
  async function drift() {
    const hero = $('.hero');
    if (!hero || hero.querySelector('.driftband')) return;

    let href = null;
    try {
      const works = (await (await fetch('/api/library')).json()).works || [];
      if (works.length && works[0].band) href = works[0].band;
    } catch (err) { /* offline: the effect simply does not appear */ }
    if (!href) return;

    const layer = document.createElement('div');
    layer.className = 'driftband';
    layer.setAttribute('aria-hidden', 'true');
    layer.innerHTML =
      `<div class="driftrun"><img src="${href}" alt=""><img src="${href}" alt=""></div>`;
    hero.insertBefore(layer, hero.firstChild);
  }

  /* ── 2. scrolling plays the piece ───────────────────────────────────── */
  /* The visitor does not watch a demonstration, they drive one: the sample
   * is scrubbed by scroll position, so moving down the page advances the
   * music and the score band moves with it. The gesture they were making
   * anyway becomes the playhead.
   *
   * The video is paused for this — letting it play as well would fight the
   * scrubbing — and the position is only touched inside an animation frame,
   * because seeking on every scroll event stutters. */
  function scrub() {
    const video = $('.samplevid');
    const hero = $('.hero');
    if (!video || !hero) return;

    // Only while the hero is on screen. Past it the video goes back to
    // playing on its own: a scrubbed video left behind is a video frozen
    // on its last frame, which reads as broken rather than as finished.
    let owned = false, target = 0, queued = false;
    const apply = () => {
      queued = false;
      if (!video.duration || Number.isNaN(video.duration)) return;
      try { video.currentTime = target * video.duration; } catch (err) { }
    };

    const onScroll = () => {
      const box = hero.getBoundingClientRect();
      const past = -box.top;
      // Spread the piece over twice the hero's height, so sixteen seconds
      // of music take a comfortable amount of scrolling rather than
      // flashing past in a flick of the wheel.
      const span = Math.max(1, box.height * 2);
      const inside = past > -innerHeight * 0.2 && past < span;

      if (inside && !owned) { owned = true; video.pause(); }
      if (!inside && owned) {
        owned = false;
        // Handed back where the scrubbing left it, still playing.
        video.play().catch(() => { });
      }
      if (!owned) return;
      target = Math.max(0, Math.min(1, past / span));
      if (!queued) { queued = true; requestAnimationFrame(apply); }
    };

    const begin = () => {
      addEventListener('scroll', onScroll, { passive: true });
      onScroll();
    };
    if (video.readyState >= 1) begin();
    else video.addEventListener('loadedmetadata', begin, { once: true });
  }

  /* ── 3. the headline in musical time ────────────────────────────────── */
  /* The words arrive in sequence, but not evenly: the gaps lengthen and
   * then recover, which is what rubato is. An even stagger reads as a
   * machine counting; this reads as a phrase. Nobody will notice it and
   * that is the point — it is meant to be felt rather than seen. */
  function rubato() {
    const head = $('.hero .display');
    if (!head || head.dataset.rubato) return;
    head.dataset.rubato = '1';

    // Rebuild word by word, keeping <br> and <em> as they were.
    const parts = [];
    head.childNodes.forEach(node => {
      if (node.nodeType === 3) {
        node.textContent.split(/(\s+)/).forEach(chunk => {
          if (chunk.trim()) parts.push({ text: chunk });
          else if (chunk) parts.push({ space: true });
        });
      } else if (node.nodeName === 'BR') {
        parts.push({ br: true });
      } else {
        parts.push({ html: node.outerHTML });
      }
    });

    head.textContent = '';
    // A phrase pushes forward and then holds back. These are relative
    // durations, not a linear ramp.
    const timing = [0, 0.10, 0.19, 0.26, 0.36, 0.50, 0.62, 0.70, 0.80];
    let n = 0;
    parts.forEach(part => {
      if (part.br) { head.appendChild(document.createElement('br')); return; }
      if (part.space) { head.appendChild(document.createTextNode(' ')); return; }
      const span = document.createElement('span');
      span.className = 'rubatoword';
      if (part.html) span.innerHTML = part.html; else span.textContent = part.text;
      span.style.animationDelay = `${(timing[n] ?? (0.8 + n * 0.06)) + 0.15}s`;
      head.appendChild(span);
      n += 1;
    });
  }

  /* ── 4. the tally counting up ───────────────────────────────────────── */
  /* The oldest device on this list and the one with a job to do: the count
   * was asked for as a marketing signal, and a number that moves is looked
   * at while a number that sits there is not. It counts to whatever is
   * really there and stops — it does not keep climbing. */
  function count() {
    const cell = document.querySelector('.madecount b');
    if (!cell || cell.dataset.counted) return;

    const run = () => {
      const shown = (cell.textContent || '').trim();
      let target = parseInt(shown.replace(/[^0-9]/g, ''), 10);
      let pretend = false;
      if (/[KM]/i.test(shown)) return;            // already shortened; leave it
      // With one video made, counting 0 to 1 shows nothing, and the idea
      // cannot be judged. Asked for by name, it counts to a plausible
      // figure instead and says so in the console — a demonstration, not
      // a number anybody should believe.
      if (!target || target < 12) {
        if (!(WANTED && WANTED.includes('count'))) return;
        target = 1284; pretend = true;
        console.info('motion:count — the real tally is ' + (shown || '0') +
                     '; counting to a made-up 1284 so the effect is visible');
      }
      cell.dataset.counted = '1';
      const started = performance.now();
      const step = now => {
        const t = Math.min(1, (now - started) / 900);
        cell.textContent = String(Math.round(ease(t) * target));
        if (t < 1) requestAnimationFrame(step);
        else cell.textContent = pretend ? '1.3K' : shown;
      };
      requestAnimationFrame(step);
    };
    // Waits until it is actually on screen; counting up out of view is a
    // trick played on nobody.
    if (!('IntersectionObserver' in window)) return run();
    const watch = new IntersectionObserver(entries => {
      entries.forEach(e => { if (e.isIntersecting) { watch.disconnect(); run(); } });
    }, { threshold: 0.6 });
    watch.observe(cell);
  }

  /* ── 5. notes landing on a staff ────────────────────────────────────── */
  /* Five lines draw themselves left to right, then noteheads arrive in
   * playing order. It is a small piece of engraving assembling itself,
   * which is close to what the pipeline actually does — and it is drawn
   * here rather than fetched, so it costs nothing and cannot 404. */
  function settle() {
    const host = $('.hero .artifact');
    if (!host || host.querySelector('.settlestaff')) return;

    const W = 460, H = 62, top = 14, gap = 8;
    const lines = [0, 1, 2, 3, 4].map(i =>
      `<line x1="6" y1="${top + i * gap}" x2="${W - 6}" y2="${top + i * gap}"
             class="sline" style="animation-delay:${i * 0.06}s"/>`).join('');
    // Roughly the opening turn of the Scherzo's octave figure — a shape
    // rather than a transcription.
    const pitches = [3.5, 3.0, 2.5, 3.0, 2.0, 1.5, 2.0, 1.0, 0.5, 1.0, 0.0, 0.5];
    const notes = pitches.map((p, i) =>
      `<ellipse cx="${40 + i * 33}" cy="${top + p * gap}" rx="4.6" ry="3.4"
                class="snote" style="animation-delay:${0.42 + i * 0.055}s"/>`).join('');

    const figure = document.createElement('div');
    figure.className = 'settlestaff';
    figure.setAttribute('aria-hidden', 'true');
    figure.innerHTML =
      `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="">${lines}${notes}</svg>`;
    host.appendChild(figure);
  }

  const MOTION = { drift, scrub, rubato, count, settle };

  function start() {
    Object.entries(MOTION).forEach(([name, run]) => {
      if (!wants(name)) return;
      // One effect throwing must not take the other four — or the page —
      // with it. This file is a proposal; it should never be the reason
      // something else stops working.
      try { run(); } catch (err) { console.warn(`motion:${name}`, err); }
    });
  }

  if (document.readyState === 'loading')
    document.addEventListener('DOMContentLoaded', start);
  else setTimeout(start, 0);
})();
