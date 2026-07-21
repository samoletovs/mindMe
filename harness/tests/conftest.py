"""Pytest bootstrap for the harness unit tests.

Makes the harness modules (``github_reapers``, …) importable: pytest's default
"prepend" import mode would otherwise only add this ``tests/`` directory to
``sys.path``, not its parent where the modules live.
"""

import pathlib
import sys

_HARNESS = pathlib.Path(__file__).resolve().parent.parent
if str(_HARNESS) not in sys.path:
    sys.path.insert(0, str(_HARNESS))
