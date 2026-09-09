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
 *     /app/                      all three
 *     /app/?motion=drift         just the engraving behind the headline
 *     /app/?motion=rubato        the headline arrives in musical time
 *     /app/?motion=count         the tally counts up
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

  /* ── 1. engraved pages behind the whole page ───────────────────────── */
  /* Not a band any more: the plates as they were engraved, standing behind
   * everything from the headline to the footer, drifting upward slowly
   * enough that you would have to watch to see it move.
   *
   * A band is a strip cut for a video, and it looked like one — a stripe
   * behind a headline. A page looks like sheet music on a desk, which is
   * what this is about.
   *
   * It is fixed rather than scrolled, so the reader moves down the page and
   * the music stays behind them, and it holds six percent opacity so it can
   * never compete with anything anyone is trying to read. Vector, so the
   * whole backdrop is a few tens of kilobytes.
   */
  async function drift() {
    if (document.querySelector('.driftpages')) return;

    let name = null, rule = 1;
    try {
      const data = await (await fetch('/api/library')).json();
      const works = data.works || [];
      if (!works.length) return;
      name = works[0].id;
      rule = data.page_rule || 1;
    } catch (err) { return; }        // offline: no backdrop, no complaint
    if (!name) return;

    // One plate, fetched once, tiled by the browser and scrolled by moving
    // the background rather than the element. Six image elements pointing
    // at three URLs was three requests and three decodes of a third of a
    // megabyte each, to draw the same thing this draws with one.
    const run = document.createElement('div');
    run.className = 'driftrun';
    // The rule number is in the address on purpose: the plate is cached
    // for an hour, so without it a change to how it is cropped stays
    // invisible until the copy in the browser expires.
    run.style.backgroundImage =
      `url("/api/library/${encodeURIComponent(name)}/page?n=1&v=${rule}")`;

    // Built from single-gradient masks only, nested.
    //
    // A mask on a parent multiplies with a mask on its child, which every
    // browser has always done — where mask-composite, which the same
    // effect needs in one element, did not take here at all. So:
    //
    //   .driftpages    the fade at the edges of the window
    //     .driftzone   WHERE the score may show — one per region
    //       .drifttilt the angle
    //         .driftrun the moving engraving
    //
    // Two zones, so the regions add up without compositing: the margins
    // beside the reading column, and a strip across the header. Both draw
    // the same image from the same address, so it is still one request.
    const plate = `/api/library/${encodeURIComponent(name)}/page?n=1&v=${rule}`;

    const zone = (kind) => {
      const run = document.createElement('div');
      run.className = 'driftrun';
      run.style.backgroundImage = `url("${plate}")`;
      const tilt = document.createElement('div');
      tilt.className = 'drifttilt';
      tilt.appendChild(run);
      const box = document.createElement('div');
      box.className = `driftzone drift-${kind}`;
      box.appendChild(tilt);
      return box;
    };

    const layer = document.createElement('div');
    layer.className = 'driftpages';
    layer.setAttribute('aria-hidden', 'true');
    layer.appendChild(zone('margins'));
    layer.appendChild(zone('head'));
    document.body.insertBefore(layer, document.body.firstChild);
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

  const MOTION = { drift, rubato, count };

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
