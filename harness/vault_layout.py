"""Folder-role map, per vault.

Folder names are configuration, not code, so this agent can be pointed at any vault that
follows the standard (mySkills/docs/vault-concept.md) rather than only at the ones it was
written against. It reads two vaults with independent owners — `personal-os` (the
sensitive vault, over blob) and `mindvault` (the synced one, over the GitHub API) — so a
layout belongs to a vault, never to the agent.

Resolution order, first hit wins:

1. ``VAULT_LAYOUT_<VAULT>_<ROLE>`` in the environment — for a one-off or a test.
2. ``vault-layout.json`` beside this file — the normal place. It ships inside the
   deployment package, so a rename is one config edit here rather than an Azure app
   setting somebody has to remember to change before deploying.
3. ``DEFAULTS`` — the standard layout, used by any vault that has no deviations.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path

DEFAULTS: dict[str, str] = {
    "inbox": "inbox",
    "projects": "projects",
    "areas": "areas",
    "resources": "resources",
    "archive": "archive",
    "journal": "journal",
}

PERSONAL_OS = "personal-os"
MINDVAULT = "mindvault"

_CONFIG_PATH = Path(__file__).with_name("vault-layout.json")


@lru_cache(maxsize=1)
def _configured() -> dict[str, dict[str, str]]:
    try:
        with _CONFIG_PATH.open(encoding="utf-8") as f:
            vaults = json.load(f).get("vaults") or {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        # Falling back to the standard layout would silently point the agent at folders
        # that may not exist, so say so loudly rather than fail the whole request.
        logging.exception("vault-layout.json unreadable; using the standard layout")
        return {}
    return {k: (v.get("layout") or {}) for k, v in vaults.items()}


def _env_key(vault: str, role: str) -> str:
    return f"VAULT_LAYOUT_{vault.replace('-', '_').upper()}_{role.upper()}"


def folder(vault: str, role: str) -> str:
    """Vault-relative folder for `role` in `vault`, without a trailing slash."""
    try:
        default = DEFAULTS[role]
    except KeyError:
        raise KeyError(f"unknown vault role {role!r}; known roles: {sorted(DEFAULTS)}") from None
    configured = _configured().get(vault, {}).get(role)
    return (os.environ.get(_env_key(vault, role)) or configured or default).strip("/")


def prefix(vault: str, role: str) -> str:
    """Blob prefix for `role` — the folder plus the trailing slash blob listing needs."""
    return f"{folder(vault, role)}/"
