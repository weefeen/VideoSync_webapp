/* A Weefeen button in YouTube's own row of buttons.
 *
 * THIS IS THE PART THAT WILL BREAK, and it is written expecting to.
 * YouTube's markup is not an interface anybody promised us: the action row
 * has changed id and shape several times, and every selector below is a
 * guess that was true on the day it was written. So:
 *
 *   - several selectors are tried, most specific first;
 *   - failing to find a place to put the button is silent, not an error.
 *     The right-click menu and the toolbar icon still work, and a console
 *     full of red on somebody else's site helps nobody;
 *   - the button is identified by a data attribute of ours, never by
 *     position, so a re-render cannot leave two behind.
 *
 * YouTube is also a single-page app: it swaps videos without loading a
 * page, so there is no second `document_idle` to hook. `yt-navigate-finish`
 * is YouTube's own event and is the cheap path; the observer is the honest
 * fallback for when that event is renamed too.
 */
(() => {
  'use strict';

  const MARK = 'data-weefeen';
  const ROW_SELECTORS = [
    '#actions #top-level-buttons-computed',
    'ytd-watch-metadata #top-level-buttons-computed',
    '#top-level-buttons-computed',
    '#menu ytd-menu-renderer #top-level-buttons-computed',
    '#actions-inner #menu',
    'ytd-watch-metadata #actions'
  ];

  function onAVideo() {
    const p = location.pathname;
    return p === '/watch' || p.startsWith('/shorts/') || p.startsWith('/live/');
  }

  function row() {
    for (const sel of ROW_SELECTORS) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
    return null;
  }

  function button() {
    const b = document.createElement('button');
    b.setAttribute(MARK, '1');
    b.className = 'weefeen-btn';
    b.type = 'button';
    b.title = 'Open this performance next to its score';
    /* The mark is inline SVG rather than a packaged file: a content script
       reading its own asset needs web_accessible_resources, which exposes
       the file to every page on the web to be sniffed for. A path is
       cheaper than a permission.

       THE FEATHER ALONE, not the roundel. The logo file is one path with
       evenodd -- a disc with the feather knocked out of it -- and at 16px
       that knockout closes up and the mark reads as a plain white dot.
       This is the second subpath of that file, filled rather than
       knocked out, on the viewBox its own ink occupies. */
    b.innerHTML =
      '<svg viewBox="72 62 127 148" aria-hidden="true" width="15" height="17">' +
      '<path fill="currentColor" d="M190.52 206.493C187.779 197.09 175.254 197.786 160.445 198.608C146.207 199.399 129.856 200.307 118.059 192.469C99.5672 180.196 90.4549 132.541 84.2595 100.141C81.1217 83.7311 78.7322 71.2344 76.2513 69.23C76.1814 68.8286 102.138 76.4552 114.486 135.823C114.818 137.421 115.144 139.019 115.469 140.612C119.201 158.923 122.79 176.529 134.517 186.281C137.344 188.635 141.965 191.396 147.124 192.125C134.74 184.026 128.936 165.076 127.274 146.541C125.051 121.658 122.512 99.6714 107.74 85.472C107.706 85.2994 107.669 85.1238 107.632 84.9479L107.632 84.9474L107.593 84.7642C107.582 84.7079 107.57 84.6516 107.558 84.5955C132.771 91.3346 141.882 117.893 145.432 149.213C145.783 152.299 146.041 155.379 146.294 158.411C147.407 171.709 148.444 184.101 156.947 192.188C159.148 192.123 161.569 192.051 164.081 192.018C156.425 185.164 156.99 171.548 157.588 157.138V157.137C157.954 148.299 158.333 139.161 156.835 131.102C154.082 116.27 148.633 104.695 137.419 97.6428C137.559 97.1295 138.23 96.4253 138.433 97.0423C160.675 100.014 174.893 120.905 173.618 145.954C173.382 150.532 172.298 155.54 171.178 160.719C168.915 171.187 166.501 182.348 170.63 192.045C184.432 192.392 197.684 194.929 190.52 206.493Z"/>' +
      '</svg><span>Score</span>';
    b.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      try {
        chrome.runtime.sendMessage({ type: 'weefeen-open', url: location.href });
      } catch (err) {
        /* The extension was reloaded or updated while this page stayed
           open, so the message port is gone. Going directly is a worse
           path -- it duplicates how the address is built -- but it is far
           better than a button that silently does nothing. */
        window.open('https://chopin.weefeen.com/app/?url='
                    + encodeURIComponent(location.href), '_blank');
      }
    });
    return b;
  }

  function place() {
    if (!onAVideo()) return;
    const host = row();
    if (!host) return;                 // silent on purpose, see the header
    if (host.querySelector('[' + MARK + ']')) return;
    host.appendChild(button());
  }

  /* Both, deliberately. The event is right when it fires and the observer
     covers the day it stops firing; `place` is idempotent, so being called
     twice for one navigation costs a querySelector. */
  document.addEventListener('yt-navigate-finish', () => setTimeout(place, 400));

  let pending = null;
  const observer = new MutationObserver(() => {
    if (pending) return;
    pending = setTimeout(() => { pending = null; place(); }, 500);
  });
  observer.observe(document.documentElement, { childList: true, subtree: true });

  setTimeout(place, 800);
})();
