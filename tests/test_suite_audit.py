"""
Suite audit — keeps the documented test inventory honest (NFR-TST-*).

Runs ``pytest --collect-only`` as a subprocess and compares the collected
counts (total + per-module) against ``tests/expected_test_counts.json``.
When tests are added or removed, regenerate the manifest with::

    python3 scripts/audit_test_counts.py --write

(or update it by hand — the audit test prints the expected values).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = Path(__file__).parent / "expected_test_counts.json"


def _collect_module_counts() -> dict[str, int]:
    """Return {module_name: collected_test_count} via a subprocess collect."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )
    if result.returncode != 0:
        raise AssertionError(f"pytest --collect-only failed:\n{result.stderr}")

    counts: dict[str, int] = {}
    for line in result.stdout.splitlines():
        if "::" not in line or not line.startswith("tests/"):
            continue
        module = line.split("::", 1)[0].split("/")[-1]
        counts[module] = counts.get(module, 0) + 1
    return counts


class TestSuiteInventoryAudit:
    """The collected suite must match the documented manifest."""

    def test_manifest_is_consistent(self):
        """total in the manifest must equal the sum of its modules."""
        expected = json.loads(MANIFEST.read_text())
        assert expected["total"] == sum(expected["modules"].values())

    def test_total_and_per_module_counts_match_manifest(self):
        actual = _collect_module_counts()
        expected = json.loads(MANIFEST.read_text())

        assert set(actual) == set(expected["modules"]), (
            "Module set drifted. Add/remove entries in "
            f"{MANIFEST.relative_to(PROJECT_ROOT)}.\n"
            f"Actual modules: {sorted(actual)}"
        )
        assert actual == expected["modules"], (
            "Per-module test counts drifted from the manifest.\n"
            f"Actual:   {actual}\n"
            f"Expected: {expected['modules']}"
        )
        assert sum(actual.values()) == expected["total"], (
            f"Total count drifted: actual {sum(actual.values())} "
            f"vs manifest {expected['total']}"
        )
