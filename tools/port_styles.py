"""Lift the mockup's stylesheet into the app's static assets.

Keeps the design identical to what was signed off, instead of re-typing it.
The first <style> block is only inlined @font-face rules (already stripped
in page.readable.html); everything after it is the design system.

Usage:
    python tools/port_styles.py
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "design" / "mockup" / "page.readable.html"
DEST = ROOT / "app" / "static" / "app.css"

HEADER = """/* Ported verbatim from design/mockup/page.html (Claude Design export).
 * Regenerate with:  python tools/port_styles.py
 * Hand edits belong in overrides.css, not here — this file is overwritten.
 */
"""


def main() -> int:
    if not SRC.is_file():
        print(f"ERROR: {SRC} missing. Run tools/extract_mockup.py first.")
        return 1

    html = SRC.read_text(encoding="utf-8")
    blocks = re.findall(r"<style>(.*?)</style>", html, flags=re.S)
    if not blocks:
        print("ERROR: no <style> blocks found")
        return 1

    # Drop blocks that are only stripped font comments.
    useful = [b for b in blocks if len(b.strip()) > 200]
    css = "\n\n".join(b.strip() for b in useful)

    DEST.parent.mkdir(parents=True, exist_ok=True)
    DEST.write_text(HEADER + "\n" + css + "\n", encoding="utf-8")
    print(f"wrote {DEST}  ({len(css):,} chars from {len(useful)} block(s))")

    selectors = sorted(set(re.findall(r"^\s*\.([a-z][\w-]*)", css, flags=re.M)))
    print(f"{len(selectors)} classes, e.g. {', '.join(selectors[:14])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
