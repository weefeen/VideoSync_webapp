# Weefeen — Watch with the score

A browser extension that takes the YouTube video you are looking at and
opens it on the score site, so the performance plays with its score beside
it. It is the desktop answer to the share sheet: on a PC, YouTube's own
Share button opens YouTube's own dialog and no extension can add a row to
it, so the way in has to come from the browser instead.

## Three ways in, and why there are three

| | where | breaks when |
|---|---|---|
| **Button in the page** | next to Like / Share / Download on a watch page | YouTube rearranges its markup |
| **Right-click → Open with Weefeen score** | on a YouTube link anywhere, or on a YouTube page | never — it is a browser API |
| **Toolbar icon** | any tab | never — it is a browser API |

The button is the nicest and the only one that depends on somebody else's
HTML. `content.js` tries six selectors and gives up silently if none
match, so the day YouTube changes its action row the extension goes quiet
rather than broken — the other two still work. If the button disappears,
that is the thing to fix, and it is the only thing.

## What it does NOT do

It does not parse links, call the API, or decide whether something is a
video. It opens `…/app/?url=<the address>` and lets the site decide, using
the same reader the server uses. There is deliberately no copy of that
logic here: a third copy is a third thing to keep in step and the first to
drift.

It asks for no host permissions. `contextMenus` and `storage` are all it
gets, and the content script is limited to YouTube.

## Installing it while it is unpacked

Chrome or Edge:

1. Go to `chrome://extensions` (Edge: `edge://extensions`).
2. Turn on **Developer mode**.
3. **Load unpacked** → choose this `extension/` folder.

It stays until removed. After editing a file, press reload on the card;
after editing `manifest.json`, remove and load it again.

## Firefox

Firefox supports MV3 but not `background.service_worker`. To load it
there, change the `background` block to:

```json
"background": { "scripts": ["background.js"] }
```

and add a `browser_specific_settings.gecko.id`. `chrome.*` is aliased to
`browser.*` in Firefox, so nothing else needs touching. Not done here
because it cannot be verified from this machine, and an untested port is
worse than an absent one.

## The one setting

Right-click the toolbar icon → Options sets which site to open. It
defaults to `https://chopin.weefeen.com` and exists so the extension can
be pointed at a local server while the site is being worked on. Nobody
using it normally ever opens it.

## Publishing it

The Chrome Web Store wants a one-off developer account (about $5), a
zip of this folder, a 128px icon (there is one in `icons/`), screenshots
and a privacy statement. The honest privacy statement is short: the
extension stores one setting, sends nothing anywhere, and the only network
request it causes is the tab it opens on the score site.

## Testing it

`scratchpad/exttest.py` in the working session loaded this into a real
Chromium with `--load-extension`, intercepted `www.youtube.com` so the
content script matched a real YouTube address while the markup was a
stand-in, and checked the manifest, the injection, the styling, that a
re-render does not leave two buttons, that a non-video page gets none, and
that clicking opens the right deep link. Extensions do not load in
headless Chromium, so that run has to be headed.
