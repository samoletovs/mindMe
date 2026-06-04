r"""Local smoke test for the new blob-backed briefing builder.

Imports `function_app._build_briefing_snapshot` and runs it against the live
`personal-os` container using the current az login (DefaultAzureCredential).

Run from repo root:

    .\.venv\Scripts\python.exe scripts\local\test_briefing_snapshot.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Windows console fallback: force UTF-8 so emoji in OS content prints cleanly.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[2]
load_dotenv(REPO / ".env")

# Make `harness/function_app.py` importable.
sys.path.insert(0, str(REPO / "harness"))

import function_app  # noqa: E402

snap = function_app._build_briefing_snapshot()
print(json.dumps(snap, indent=2, ensure_ascii=False))
