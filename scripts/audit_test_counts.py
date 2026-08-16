#!/usr/bin/env python3
"""Regenerate tests/expected_test_counts.json from the collected suite.

Usage:
    python3 scripts/audit_test_counts.py            # print diff vs manifest
    python3 scripts/audit_test_counts.py --write    # rewrite the manifest

The manifest backs the offline audit in tests/test_suite_audit.py.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = PROJECT_ROOT / "tests" / "expected_test_counts.json"


def collect_counts() -> dict[str, int]:
    """Return {module_name: test_count} via pytest --collect-only."""
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
        raise SystemExit(f"pytest --collect-only failed:\n{result.stderr}")

    counts: dict[str, int] = {}
    for line in result.stdout.splitlines():
        if "::" not in line or not line.startswith("tests/"):
            continue
        module = line.split("::", 1)[0].split("/")[-1]
        counts[module] = counts.get(module, 0) + 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write", action="store_true", help="rewrite the manifest file"
    )
    args = parser.parse_args()

    counts = collect_counts()
    manifest = {
        "total": sum(counts.values()),
        "modules": dict(sorted(counts.items())),
    }

    if not MANIFEST.exists():
        print(f"Manifest missing — creating {MANIFEST.relative_to(PROJECT_ROOT)}")
        MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
        return

    expected = json.loads(MANIFEST.read_text())
    if manifest == expected:
        print("Manifest is up to date.")
        return

    print("Drift detected:")
    print(f"  actual:   {manifest}")
    print(f"  manifest: {expected}")
    if args.write:
        MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"Manifest rewritten: {MANIFEST.relative_to(PROJECT_ROOT)}")
    else:
        print("Run with --write to update the manifest.")


if __name__ == "__main__":
    main()