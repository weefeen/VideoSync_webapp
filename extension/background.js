/* The two ways in that cannot break.
 *
 * The button this extension injects into the YouTube page is the nicest of
 * the three and the only one that depends on somebody else's markup.
 * YouTube rearranges that markup whenever it likes. The right-click menu
 * and the toolbar icon are built on extension APIs instead, so when the
 * injected button stops appearing these still work and the extension is
 * degraded rather than dead. That is the whole reason there are three.
 *
 * NOTHING IS PARSED HERE. The extension never decides whether an address
 * is a video; it hands the address to the site and the site decides, with
 * the same reader the server uses. A copy of that logic living out here
 * would be a third place to keep in step and the first to drift.
 */

const DEFAULT_BASE = 'https://chopin.weefeen.com';

async function base() {
  try {
    const got = await chrome.storage.sync.get({ base: DEFAULT_BASE });
    return (got.base || DEFAULT_BASE).replace(/\/+$/, '');
  } catch (err) {
    // A storage read can fail in a freshly installed or corrupted profile.
    // A default that works beats an error nobody sees.
    return DEFAULT_BASE;
  }
}

async function openWith(url) {
  if (!url) return;
  const target = (await base()) + '/app/?url=' + encodeURIComponent(url);
  chrome.tabs.create({ url: target });
}

/* Context menus must be declared from `onInstalled`: a service worker is
   torn down when idle, and anything created at top level is created again
   on every wake, which throws on a duplicate id. */
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    /* On a LINK, anywhere on the web -- a YouTube address pasted into a
       forum, a chat, a mail. Filtered by pattern so the item does not
       appear on links it could do nothing with. */
    chrome.contextMenus.create({
      id: 'weefeen-link',
      title: 'Open with Weefeen score',
      contexts: ['link'],
      targetUrlPatterns: [
        '*://*.youtube.com/*', '*://youtu.be/*', '*://*.youtube-nocookie.com/*'
      ]
    });
    /* On the PAGE or the video itself, while on YouTube: the common case,
       where the address you want is the one you are already looking at. */
    chrome.contextMenus.create({
      id: 'weefeen-page',
      title: 'Open with Weefeen score',
      contexts: ['page', 'video'],
      documentUrlPatterns: [
        '*://*.youtube.com/*', '*://youtu.be/*', '*://*.youtube-nocookie.com/*'
      ]
    });
  });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  /* linkUrl for the link case, pageUrl for the page case. srcUrl is the
     media file itself and is never what we want: it is a googlevideo.com
     stream address, not a video anybody can name. */
  openWith(info.linkUrl || info.pageUrl || (tab && tab.url));
});

/* The toolbar icon: whatever tab you are on. It is enabled everywhere
   rather than only on YouTube, because a disabled icon with no explanation
   is worse than one that says why it cannot help -- and the site's own
   refusal is that explanation. */
chrome.action.onClicked.addListener((tab) => {
  openWith(tab && tab.url);
});

/* The injected button asks the worker to open, rather than opening a tab
   itself, so there is one place that knows how the address is built. */
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg && msg.type === 'weefeen-open') {
    openWith(msg.url || (sender.tab && sender.tab.url));
    reply && reply({ ok: true });
  }
  return false;
});
