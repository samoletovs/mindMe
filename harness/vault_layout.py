"""Folder-role map for the Personal OS vault.

Folder names are configuration, not code, so this agent can be pointed at any vault that
follows the standard (mySkills/docs/vault-concept.md) rather than only at the one it was
written against.

Resolution order, first hit wins:

1. ``VAULT_LAYOUT_<ROLE>`` in the environment — for a one-off or a test.
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

_CONFIG_PATH = Path(__file__).with_name("vault-layout.json")


@lru_cache(maxsize=1)
def _configured() -> dict[str, str]:
    try:
        with _CONFIG_PATH.open(encoding="utf-8") as f:
            return json.load(f).get("layout") or {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        # Falling back to the standard layout would silently point the agent at folders
        # that may not exist, so say so loudly rather than fail the whole request.
        logging.exception("vault-layout.json unreadable; using the standard layout")
        return {}


def folder(role: str) -> str:
    """Vault-relative folder for `role`, without a trailing slash."""
    try:
        default = DEFAULTS[role]
    except KeyError:
        raise KeyError(f"unknown vault role {role!r}; known roles: {sorted(DEFAULTS)}") from None
    value = os.environ.get(f"VAULT_LAYOUT_{role.upper()}") or _configured().get(role) or default
    return value.strip("/")


def prefix(role: str) -> str:
    """Blob prefix for `role` — the folder plus the trailing slash blob listing needs."""
    return f"{folder(role)}/"
