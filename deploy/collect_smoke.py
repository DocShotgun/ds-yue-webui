#!/usr/bin/env python3
"""Collect the smoke-test results into data/smoke-result.json (shared by install.sh and install.ps1).

Usage: collect_smoke.py DATA_DIR
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def read(path: Path):
    if not path.is_file():
        return {"missing": str(path)}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {"unavailable": "not valid JSON"}


def main() -> int:
    data_dir = Path(sys.argv[1])
    yue2 = read(data_dir / "smoke" / "yue2" / "result.json")
    sheetsage = read(data_dir / "smoke" / "sheetsage" / "smoke_status.json")
    passed = yue2.get("status") == "complete" and sheetsage.get("status") == "complete"
    report = {"passed": passed, "yue2": yue2, "sheetsage": sheetsage,
              "note": "Smoke timing is a rough measure, not a benchmark. "
                      "If the relaxed shared env failed, re-run install.sh --strict-pins."}
    (data_dir / "smoke-result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"smoke {'PASSED' if passed else 'FAILED'}; report: {data_dir / 'smoke-result.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
