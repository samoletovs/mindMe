"""Pytest bootstrap for the harness unit tests.

Makes the harness modules (``github_reapers``, …) importable: pytest's default
"prepend" import mode would otherwise only add this ``tests/`` directory to
``sys.path``, not its parent where the modules live.
"""

import pathlib
import sys

import pytest

_HARNESS = pathlib.Path(__file__).resolve().parent.parent
if str(_HARNESS) not in sys.path:
    sys.path.insert(0, str(_HARNESS))


@pytest.fixture(autouse=True)
def isolate_harness_services(monkeypatch):
    import function_app

    def unexpected_io():
        raise AssertionError("External services must be mocked in unit tests")

    monkeypatch.setattr(function_app, "_claim_onboarding", lambda: False)
    monkeypatch.setattr(function_app, "_http_client", unexpected_io)
    monkeypatch.setattr(function_app, "_os_container_client", unexpected_io)
    monkeypatch.setattr(function_app, "_foundry", unexpected_io)
