#!/usr/bin/env python3
"""One-shot extractor: monolithic frontend/index.html -> Vite src layout."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_HTML = ROOT / "frontend" / "index.html"
LEGACY_BACKUP = ROOT / "frontend" / "index.legacy.html"

OUT_CSS = ROOT / "frontend" / "src" / "styles" / "main.css"
OUT_APP = ROOT / "frontend" / "src" / "legacy" / "app.js"
OUT_HTML = ROOT / "frontend" / "index.vite.html"


def main() -> None:
    text = SRC_HTML.read_text(encoding="utf-8")

    style_m = re.search(r"<style>\s*(.*?)\s*</style>", text, re.DOTALL)
    if not style_m:
        raise SystemExit("Could not find <style> block")
    css = style_m.group(1)

    body_m = re.search(r"</head>\s*(<body>.*?)<script>", text, re.DOTALL)
    if not body_m:
        raise SystemExit("Could not find <body> block")
    body = body_m.group(1).rstrip()

    script_m = re.search(r"<script>\s*(.*?)\s*</script>\s*</body>", text, re.DOTALL)
    if not script_m:
        raise SystemExit("Could not find inline <script> block")
    js = script_m.group(1)

    # Top-level function names for inline onclick/oninput handlers.
    fn_names = sorted(set(re.findall(r"^function\s+([A-Za-z_][\w]*)", js, re.MULTILINE)))
    async_fns = sorted(set(re.findall(r"^async function\s+([A-Za-z_][\w]*)", js, re.MULTILINE)))
    all_fns = sorted(set(fn_names + async_fns))

    window_lines = ["", "// Expose handlers for inline HTML attributes (transitional)."]
    for name in all_fns:
        window_lines.append(f"window.{name} = {name};")

    OUT_CSS.parent.mkdir(parents=True, exist_ok=True)
    OUT_APP.parent.mkdir(parents=True, exist_ok=True)
    OUT_CSS.write_text(css, encoding="utf-8")
    OUT_APP.write_text(js + "\n".join(window_lines) + "\n", encoding="utf-8")

    head_links = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Smartsheet Controller — AI-Powered Sheet Management</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/src/styles/main.css">
</head>
"""

    tail = """
<script type="module" src="/src/main.js"></script>
</body>
</html>
"""

    OUT_HTML.write_text(head_links + body + tail, encoding="utf-8")

    if not LEGACY_BACKUP.exists():
        LEGACY_BACKUP.write_text(text, encoding="utf-8")

    print(f"CSS lines: {len(css.splitlines())}")
    print(f"JS lines: {len(js.splitlines())}")
    print(f"HTML body lines: {len(body.splitlines())}")
    print(f"Exported {len(all_fns)} functions to window")
    print(f"Wrote {OUT_CSS.relative_to(ROOT)}")
    print(f"Wrote {OUT_APP.relative_to(ROOT)}")
    print(f"Wrote {OUT_HTML.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
