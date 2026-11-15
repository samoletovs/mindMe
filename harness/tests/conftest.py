"""Pytest bootstrap for the harness unit tests.

Makes the harness modules (``github_reapers``, …) importable: pytest's default
"prepend" import mode would otherwise only add this ``tests/`` directory to
``sys.path``, not its parent where the modules live.
"""

import importlib.metadata
import pathlib
import re
import sys

import pytest

_HARNESS = pathlib.Path(__file__).resolve().parent.parent
if str(_HARNESS) not in sys.path:
    sys.path.insert(0, str(_HARNESS))


def pin_drift(requirements: pathlib.Path, installed=importlib.metadata.version) -> list[str]:
    """Return each exact runtime pin that the running interpreter does not satisfy."""
    drift = []
    for line in requirements.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([A-Za-z0-9._-]+)==([^\s;#]+)", line.split("#", 1)[0].strip())
        if not match:
            continue
        name, pinned = match.groups()
        try:
            version = installed(name)
        except importlib.metadata.PackageNotFoundError:
            version = "missing"
        if version != pinned:
            drift.append(f"{name} {version} (pinned {pinned})")
    return drift


def pytest_report_header(config):
    # A local pass under a different SDK than production is weaker evidence; CI installs the pins.
    drift = pin_drift(_HARNESS / "requirements.txt")
    if not drift:
        return "runtime pins: installed versions match harness/requirements.txt"
    return "runtime pins: differ from harness/requirements.txt: " + ", ".join(drift)


@pytest.fixture(autouse=True)
def isolate_harness_services(monkeypatch):
    import function_app

    def unexpected_io():
        raise AssertionError("External services must be mocked in unit tests")

    monkeypatch.setattr(function_app, "_claim_onboarding", lambda: False)
    monkeypatch.setattr(function_app, "_http_client", unexpected_io)
    monkeypatch.setattr(function_app, "_os_container_client", unexpected_io)
    monkeypatch.setattr(function_app, "_foundry", unexpected_io)
