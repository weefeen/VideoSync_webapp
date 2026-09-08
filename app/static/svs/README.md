# Score Video Sync — Minimal

Chopin performance in, score-synced video out. Four steps: upload · confirm the piece · how it looks · send.

## Files

    Score Video Sync - Minimal.html   the whole interface (markup + all CSS)
    svs-min.js                        state, schematic renderer, screens, upload/recognition/delivery
    svs-min-direct.js                 step 3 direct manipulation: regions, inspector, drag, editing
    Credits.html                      score-source attribution page (CC BY 4.0)
    assets/fb-group-avatar-2x.png     Facebook group avatar (96px, 48px fallback beside it)
    assets/weefeen-logo.svg           full Weefeen lockup — source of the inline glyph in the panel

Open the HTML directly; no build, no network calls. Load order matters:
`svs-min-direct.js` before `svs-min.js`.

## What is real and what is mocked

Real, client-side, no upload:
- aspect ratio and duration read from the chosen file
- five candidate stills pulled from the video via `<video>` + canvas (`grabPosters`)
- logo upload previewed from an object URL; 16:9 backdrop validation (`checkBdImage`)
- rights gate, email validation, per-browser address memory (`localStorage` key `svs.verified`)

Mocked, needs a backend:
- piece recognition — `CANDS` in svs-min.js is a fixed ranking; swap for the audio-match response
- the score band is schematic (`staveHTML`) — real notation comes from the NIFC score at render
- `#submit` moves screens only; wire it to the render queue
- the confirmation email and its link (screen `verify`)

## Server hooks to wire

1. **Recognition** — POST the audio fingerprint, return `[{id, confidence}]`; replace `CANDS`.
2. **Submit** — POST `{file, piece, template state, email}`; `S` holds the whole template.
3. **Address confirmation** — send one link per address, nothing user-authored in the mail, one
   pending request per address, rate-limit per address and IP. On click, mark confirmed and queue.
4. **Bot check** — invisible Turnstile/hCaptcha at submit. No UI space needed.

## Score attribution (required)

Scores from the Fryderyk Chopin Institute Humdrum first editions are CC BY 4.0. The attribution
block is in the page footer and on Credits.html, and each library work carries
`src: 'nifc-first-editions'` with `ppr`/`ppp` (the `!!!PPR` / `!!!PPP` header fields) in `WORKS`.
Only works with that `src` show the per-score line. Replace the placeholder publisher/city values
with the real ones from your `.krn` files, and keep the original header lines intact in any
`.krn` you serve.

## Conventions worth keeping

- Palette and type live in `:root` and the top of the stylesheet — ivory paper, aubergine, one
  magenta accent. Body copy uses `--soft` or darker; `--faint` is only for uppercase mono labels.
- Editor-only affordances (region tags, panel wash, ghost slots, video hatch) never appear in the
  render; they are the language that tells the user what is editable.
- The preview frame must never change size when an option is clicked.
