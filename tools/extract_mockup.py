"""Unpack a Claude Design "standalone" HTML export into readable source.

The export is a self-extracting bundle: one <script type="__bundler/manifest">
line holding every asset base64-encoded, and one <script type="__bundler/template">
line holding the page HTML as a JSON string. This pulls both apart so the design
can be read and ported.

Usage:
    python tools/extract_mockup.py <export.html> [outdir]
"""

import base64
import json
import pathlib
import re
import sys

DEFAULT_SRC = r"C:\Users\msmabq\Downloads\Chopin Video Sync (standalone).html"


def find_bundle_line(lines: list[str], kind: str) -> str | None:
    """Return the payload line following <script type="__bundler/{kind}">."""
    marker = f'__bundler/{kind}'
    for i, line in enumerate(lines):
        if marker in line and "<script" in line:
            return lines[i + 1] if i + 1 < len(lines) else None
    return None


def main() -> int:
    src = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SRC)
    out = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else
                       pathlib.Path(__file__).resolve().parent.parent / "design" / "mockup")
    if not src.is_file():
        print(f"ERROR: no such file: {src}")
        return 1

    out.mkdir(parents=True, exist_ok=True)
    lines = src.read_text(encoding="utf-8", errors="replace").split("\n")

    # --- assets -------------------------------------------------------------
    raw_manifest = find_bundle_line(lines, "manifest")
    manifest = json.loads(raw_manifest) if raw_manifest else {}
    by_mime: dict[str, int] = {}
    for entry in manifest.values():
        mime = entry.get("mime", "?")
        by_mime[mime] = by_mime.get(mime, 0) + 1
    print(f"manifest: {len(manifest)} assets -> {by_mime}")

    assets = out / "assets"
    assets.mkdir(exist_ok=True)
    written = 0
    for uuid, entry in manifest.items():
        mime = entry.get("mime", "application/octet-stream")
        # Skip fonts: bulky, and the port will use webfonts or local files.
        if mime.startswith("font/") or "font" in mime:
            continue
        ext = mime.split("/")[-1].split("+")[0]
        try:
            (assets / f"{uuid}.{ext}").write_bytes(base64.b64decode(entry["data"]))
            written += 1
        except Exception as exc:  # noqa: BLE001 - report and keep going
            print(f"  skip {uuid}: {exc}")
    print(f"wrote {written} non-font assets to {assets}")

    for kind in ("ext_resources", "page_order"):
        payload = find_bundle_line(lines, kind)
        if payload:
            (out / f"{kind}.json").write_text(payload.strip(), encoding="utf-8")
            print(f"{kind}: {payload.strip()[:200]}")

    # --- the page itself ----------------------------------------------------
    raw_template = find_bundle_line(lines, "template")
    if not raw_template:
        print("ERROR: no __bundler/template block found")
        return 1

    page = json.loads(raw_template)
    (out / "page.html").write_text(page, encoding="utf-8")
    print(f"wrote page.html ({len(page):,} chars)")

    # A readable copy: the inlined @font-face blocks are ~90% of the bytes.
    stripped = re.sub(r"@font-face\s*\{[^}]*\}", "", page)
    stripped = re.sub(r'url\("[0-9a-f-]{36}"\)', 'url("FONT")', stripped)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    (out / "page.readable.html").write_text(stripped, encoding="utf-8")
    print(f"wrote page.readable.html ({len(stripped):,} chars)")

    body_at = stripped.find("<body")
    print(f"head: {body_at:,} chars | body: {len(stripped) - body_at:,} chars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
