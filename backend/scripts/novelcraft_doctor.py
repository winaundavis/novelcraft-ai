"""Usage: python scripts/novelcraft_doctor.py PROJECT_ID"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.doctor_service import inspect_project


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: novelcraft_doctor.py PROJECT_ID")
    report = inspect_project(sys.argv[1])
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["status"] != "FAIL" else 2)
