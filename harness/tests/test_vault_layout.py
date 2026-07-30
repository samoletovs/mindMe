"""vault_layout — folder names come from config, per vault, never from code."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import vault_layout  # noqa: E402


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    vault_layout._configured.cache_clear()
    for vault in (vault_layout.PERSONAL_OS, vault_layout.MINDVAULT):
        for role in vault_layout.DEFAULTS:
            monkeypatch.delenv(vault_layout._env_key(vault, role), raising=False)
    yield
    vault_layout._configured.cache_clear()


def test_both_vaults_are_on_the_standard_layout():
    # Both migrated 2026-07-30. Kept as a guard: if a vault ever deviates again, it must
    # be declared in vault-layout.json rather than discovered in production.
    assert vault_layout.folder(vault_layout.PERSONAL_OS, "areas") == "areas"
    assert vault_layout.folder(vault_layout.MINDVAULT, "areas") == "areas"


def test_a_vault_can_deviate_without_touching_code(monkeypatch, tmp_path):
    cfg = tmp_path / "vault-layout.json"
    cfg.write_text(
        json.dumps({"vaults": {"personal-os": {"layout": {"areas": "02_areas"}}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(vault_layout, "_CONFIG_PATH", cfg)
    vault_layout._configured.cache_clear()
    assert vault_layout.folder(vault_layout.PERSONAL_OS, "areas") == "02_areas"
    assert vault_layout.folder(vault_layout.MINDVAULT, "areas") == "areas"


def test_blob_prefix_has_the_trailing_slash_listing_needs():
    assert vault_layout.prefix(vault_layout.PERSONAL_OS, "inbox") == "inbox/"


def test_env_overrides_the_config_file(monkeypatch):
    monkeypatch.setenv(vault_layout._env_key(vault_layout.PERSONAL_OS, "areas"), "02_areas")
    assert vault_layout.folder(vault_layout.PERSONAL_OS, "areas") == "02_areas"


def test_unknown_vault_falls_back_to_the_standard():
    assert vault_layout.folder("some-new-vault", "journal") == "journal"


def test_unreadable_config_does_not_take_the_agent_down(monkeypatch, tmp_path, caplog):
    bad = tmp_path / "vault-layout.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(vault_layout, "_CONFIG_PATH", bad)
    vault_layout._configured.cache_clear()
    assert vault_layout.folder(vault_layout.PERSONAL_OS, "areas") == "areas"
    assert "unreadable" in caplog.text


def test_unknown_role_is_refused_rather_than_guessed():
    with pytest.raises(KeyError):
        vault_layout.folder(vault_layout.PERSONAL_OS, "nonsense")


def test_config_file_only_names_known_roles():
    # A typo'd role would be silently ignored at runtime, so catch it here instead.
    vaults = json.loads(vault_layout._CONFIG_PATH.read_text(encoding="utf-8"))["vaults"]
    for name, spec in vaults.items():
        assert set(spec.get("layout") or {}) <= set(vault_layout.DEFAULTS), name
