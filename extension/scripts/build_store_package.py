#!/usr/bin/env python3
"""Build a Chrome Web Store ZIP from extension/ using manifest.prod.json.

Usage (from repo root):

    python extension/scripts/build_store_package.py
    python extension/scripts/build_store_package.py --controller-origin https://chat.example.com

Output: dist/smartsheet-controller-extension-<version>.zip
"""
from __future__ import annotations

import argparse
import json
import re
import zipfile
from pathlib import Path
from urllib.parse import urlparse

EXT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = EXT_DIR.parent
DIST_DIR = REPO_ROOT / "dist"
PROD_MANIFEST = EXT_DIR / "manifest.prod.json"

# Module graph not declared in manifest — ship these alongside the service worker / panels.
EXTRA_FILES = (
    "bridge.js",
    "constants.js",
    "permissions.js",
)

STORE_DIRS = ("icons", "content", "styles")


def _origin_pattern(origin: str) -> str:
    base = origin.strip().rstrip("/")
    if not re.match(r"^https?://", base, re.I):
        raise ValueError(f"Controller origin must include scheme: {origin!r}")
    return f"{base}/*"


def _add_controller_origin(manifest: dict, origin: str) -> None:
    pattern = _origin_pattern(origin)
    optional = manifest.setdefault("optional_host_permissions", [])
    if pattern not in optional:
        optional.append(pattern)

    for block in manifest.get("content_scripts", []):
        matches = block.setdefault("matches", [])
        if pattern not in matches:
            matches.append(pattern)

    for block in manifest.get("web_accessible_resources", []):
        matches = block.setdefault("matches", [])
        if pattern not in matches:
            matches.append(pattern)


def _collect_manifest_paths(manifest: dict) -> set[str]:
    paths: set[str] = set()

    def walk(value: object) -> None:
        if isinstance(value, str):
            if value.startswith(("http://", "https://", "chrome://")):
                return
            if "/" in value or value.endswith((".js", ".html", ".css", ".png", ".json", ".svg")):
                paths.add(value.replace("\\", "/"))
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(manifest)
    return paths


def _files_for_zip(manifest: dict) -> list[Path]:
    required = _collect_manifest_paths(manifest)
    required.update(EXTRA_FILES)

    seen: set[str] = set()
    paths: list[Path] = []
    missing: list[str] = []

    def add(path: Path) -> None:
        if not path.is_file():
            return
        rel = path.relative_to(EXT_DIR).as_posix()
        if rel in seen:
            return
        seen.add(rel)
        paths.append(path)

    for rel in sorted(required):
        path = EXT_DIR / rel
        if path.is_file():
            add(path)
        else:
            missing.append(rel)

    for dirname in STORE_DIRS:
        dirpath = EXT_DIR / dirname
        if dirpath.is_dir():
            for file in sorted(dirpath.rglob("*")):
                add(file)
        elif dirname not in {p.split("/")[0] for p in missing}:
            missing.append(f"{dirname}/")

    if missing:
        raise FileNotFoundError(f"Missing extension files: {', '.join(missing)}")

    return paths


def build_zip(controller_origin: str | None = None) -> Path:
    manifest = json.loads(PROD_MANIFEST.read_text(encoding="utf-8"))
    if controller_origin:
        _add_controller_origin(manifest, controller_origin)

    version = manifest.get("version", "0.0.0")
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DIST_DIR / f"smartsheet-controller-extension-{version}.zip"

    files = _files_for_zip(manifest)
    manifest_bytes = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", manifest_bytes)
        for path in files:
            if path.name == "manifest.json" and path.parent == EXT_DIR:
                continue
            arcname = path.relative_to(EXT_DIR).as_posix()
            zf.write(path, arcname)

    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Chrome Web Store ZIP for the extension.")
    parser.add_argument(
        "--controller-origin",
        help="HTTPS (or HTTP) base URL of a deployed Controller to bake into optional_host_permissions.",
    )
    args = parser.parse_args()

    if args.controller_origin:
        parsed = urlparse(args.controller_origin.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            parser.error("--controller-origin must be a full URL, e.g. https://chat.example.com")

    out = build_zip(args.controller_origin)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
