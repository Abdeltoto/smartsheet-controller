"""CI gate for the manual functional runners under ``tests/functional/``.

These scripts exercise real Smartsheet sheets and are too heavy for every PR,
but we verify they exist, compile, and expose a CLI where applicable.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

FUNCTIONAL_DIR = Path(__file__).resolve().parents[1] / "functional"
RUNNERS = (
    "run_smoke.py",
    "run_formulas.py",
    "run_cross_sheet.py",
    "run_p3_live.py",
)


class TestFunctionalRunnersPresent:
    @pytest.mark.parametrize("name", RUNNERS)
    def test_runner_exists(self, name: str) -> None:
        path = FUNCTIONAL_DIR / name
        assert path.is_file(), f"Missing functional runner: {path}"

    @pytest.mark.parametrize("name", RUNNERS)
    def test_runner_compiles(self, name: str) -> None:
        path = FUNCTIONAL_DIR / name
        compile(path.read_text(encoding="utf-8"), str(path), "exec")

    @pytest.mark.parametrize("name", ("run_formulas.py", "run_cross_sheet.py"))
    def test_runner_help_exits_zero(self, name: str) -> None:
        path = FUNCTIONAL_DIR / name
        proc = subprocess.run(
            [sys.executable, str(path), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr or proc.stdout
