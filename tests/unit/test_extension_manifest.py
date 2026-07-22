"""Validate Chrome extension manifests and store packaging."""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
EXT_DIR = REPO_ROOT / "extension"
PROD_MANIFEST = EXT_DIR / "manifest.prod.json"
DEV_MANIFEST = EXT_DIR / "manifest.dev.json"
BUILD_SCRIPT = EXT_DIR / "scripts" / "build_store_package.py"

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

EXTRA_MODULE_FILES = {"bridge.js", "constants.js", "permissions.js"}


def _load_builder_module():
    spec = importlib.util.spec_from_file_location("build_store_package", BUILD_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_manifest(path: Path) -> dict:
    assert path.is_file(), f"Missing {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _collect_paths(manifest: dict) -> set[str]:
    paths: set[str] = set()

    def walk(value: object) -> None:
        if isinstance(value, str):
            if value.startswith(("http://", "https://")):
                return
            if "/" in value or value.endswith((".js", ".html", ".css", ".png")):
                paths.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(manifest)
    return paths


class TestExtensionManifests:
    def test_dev_and_prod_manifests_exist(self) -> None:
        assert DEV_MANIFEST.is_file()
        assert PROD_MANIFEST.is_file()
        assert (EXT_DIR / "manifest.json").is_file()

    @pytest.mark.parametrize("path", [DEV_MANIFEST, PROD_MANIFEST, EXT_DIR / "manifest.json"])
    def test_manifest_version_semver(self, path: Path) -> None:
        manifest = _load_manifest(path)
        assert SEMVER.match(manifest["version"]), manifest["version"]

    @pytest.mark.parametrize("path", [DEV_MANIFEST, PROD_MANIFEST])
    def test_referenced_files_exist(self, path: Path) -> None:
        manifest = _load_manifest(path)
        missing = [
            rel
            for rel in sorted(_collect_paths(manifest) | EXTRA_MODULE_FILES)
            if not (EXT_DIR / rel).is_file()
        ]
        assert not missing, f"Missing files for {path.name}: {missing}"

    def test_prod_manifest_minimal_host_permissions(self) -> None:
        manifest = _load_manifest(PROD_MANIFEST)
        hosts = set(manifest.get("host_permissions", []))
        assert hosts == {"https://app.smartsheet.com/*"}
        optional = set(manifest.get("optional_host_permissions", []))
        assert "http://127.0.0.1:8100/*" in optional
        assert "http://localhost:8100/*" in optional

    def test_dev_manifest_includes_localhost(self) -> None:
        manifest = _load_manifest(DEV_MANIFEST)
        hosts = set(manifest.get("host_permissions", []))
        assert "http://127.0.0.1:8100/*" in hosts

    def test_prod_has_homepage_url(self) -> None:
        manifest = _load_manifest(PROD_MANIFEST)
        assert manifest.get("homepage_url", "").startswith("https://")


class TestStorePackageBuilder:
    def test_build_store_zip(self, tmp_path: Path) -> None:
        builder = _load_builder_module()

        original_dist = builder.DIST_DIR
        builder.DIST_DIR = tmp_path
        try:
            out = builder.build_zip()
        finally:
            builder.DIST_DIR = original_dist

        assert out.is_file()
        assert out.suffix == ".zip"

        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
            assert "manifest.json" in names
            assert "background.js" in names
            assert "permissions.js" in names
            assert "icons/icon-128.png" in names
            assert not any(n.startswith("store/") for n in names)
            assert not any(n.startswith("scripts/") for n in names)
            manifest = json.loads(zf.read("manifest.json"))
            assert manifest["version"] == _load_manifest(PROD_MANIFEST)["version"]

    def test_build_with_custom_origin(self, tmp_path: Path) -> None:
        builder = _load_builder_module()

        original_dist = builder.DIST_DIR
        builder.DIST_DIR = tmp_path
        try:
            out = builder.build_zip("https://chat.example.com")
        finally:
            builder.DIST_DIR = original_dist

        with zipfile.ZipFile(out) as zf:
            manifest = json.loads(zf.read("manifest.json"))
            optional = manifest.get("optional_host_permissions", [])
            assert "https://chat.example.com/*" in optional

    def test_build_script_cli(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(BUILD_SCRIPT), "--controller-origin", "https://demo.test"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr or proc.stdout
        zip_path = Path(proc.stdout.strip())
        assert zip_path.is_file()
