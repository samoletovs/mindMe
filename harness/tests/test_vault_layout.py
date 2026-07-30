"""vault_layout — folder names come from config, never from code."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import vault_layout  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    vault_layout._configured.cache_clear()
    for role in vault_layout.DEFAULTS:
        monkeypatch.delenv(f"VAULT_LAYOUT_{role.upper()}", raising=False)
    yield
    vault_layout._configured.cache_clear()


def test_ships_with_the_layout_the_vaults_actually_use():
    # The deployed default must match the live vaults, or every blob prefix misses.
    assert vault_layout.folder("areas") == "02_areas"
    assert vault_layout.prefix("inbox") == "00_inbox/"


def test_env_overrides_the_config_file(monkeypatch):
    monkeypatch.setenv("VAULT_LAYOUT_AREAS", "areas")
    assert vault_layout.folder("areas") == "areas"


def test_falls_back_to_the_standard_when_config_is_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(vault_layout, "_CONFIG_PATH", tmp_path / "missing.json")
    vault_layout._configured.cache_clear()
    assert vault_layout.folder("journal") == "journal"


def test_unreadable_config_does_not_take_the_agent_down(monkeypatch, tmp_path, caplog):
    bad = tmp_path / "vault-layout.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(vault_layout, "_CONFIG_PATH", bad)
    vault_layout._configured.cache_clear()
    assert vault_layout.folder("areas") == "areas"
    assert "unreadable" in caplog.text


def test_unknown_role_is_refused_rather_than_guessed():
    with pytest.raises(KeyError):
        vault_layout.folder("nonsense")


def test_config_file_only_names_known_roles():
    # A typo'd role in the config would be silently ignored, so catch it here instead.
    configured = json.loads(vault_layout._CONFIG_PATH.read_text(encoding="utf-8"))["layout"]
    assert set(configured) <= set(vault_layout.DEFAULTS)
