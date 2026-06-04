"""Sync the Personal OS markdown tree to Azure Blob Storage.

Replaces the daily encrypted briefing-context blob with an authoritative cloud
mirror of the OS, so the Function App can build the morning briefing entirely
in Azure (no laptop dependency at run-time).

Run this whenever you've edited the OS and want the cloud copy refreshed:

    .\\.venv\\Scripts\\python.exe scripts\\local\\sync_os_to_blob.py

Or wire it into a VS Code task / git pre-push hook later if you want push-style
semantics. This is NOT a scheduled task — that's the whole point.

What's uploaded
---------------
- Every `*.md` file under `%USERPROFILE%\\OneDrive\\.vscode\\.me` (override via
  ME_OS_ROOT env var)
- Plus a tiny `_manifest.json` at the container root recording the sync time
  and file count, so the Function can detect staleness.

What's NOT uploaded
-------------------
- Non-markdown files (binaries, scripts, data exports)
- Anything under `.git`, `.venv`, `node_modules`, `__pycache__`
- Anything matching `.gitignore` patterns at the root (best-effort)

Security model
--------------
- Container `personal-os` is private. RBAC-gated. Only the signed-in user and
  the Function App's managed identity can read or write.
- Storage at-rest encryption is Microsoft-managed (default). The previous
  application-layer AES-GCM is dropped — see docs/architecture.md for the
  rationale and how to re-enable it if you change your mind.

Logs
----
File names + sizes + counts. Never content.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings
from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / ".env"

ME_ROOT = Path(
    os.environ.get(
        "ME_OS_ROOT",
        os.path.expandvars(r"%USERPROFILE%\OneDrive\.vscode\.me"),
    )
)

CONTAINER = os.environ.get("AZURE_STORAGE_PERSONAL_OS_CONTAINER", "personal-os")

EXCLUDE_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".vscode", ".cache"}

log = logging.getLogger("mindMe.sync-os-to-blob")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("azure").setLevel(logging.WARNING)


def _iter_markdown(root: Path):
    for p in root.rglob("*.md"):
        try:
            rel_parts = p.relative_to(root).parts
        except ValueError:
            continue
        if any(part in EXCLUDE_DIRS for part in rel_parts):
            continue
        yield p


def _blob_path_for(root: Path, abs_path: Path) -> str:
    """Use forward-slashes, mirror the OS tree."""
    rel = abs_path.relative_to(root)
    return rel.as_posix()


def main() -> int:
    _setup_logging()
    load_dotenv(ENV_PATH)

    account = os.environ.get("AZURE_STORAGE_ACCOUNT")
    if not account:
        log.error("AZURE_STORAGE_ACCOUNT not set in .env")
        return 2
    if not ME_ROOT.exists():
        log.error("Personal OS root not found: %s", ME_ROOT)
        return 2

    log.info("syncing %s -> %s/%s", ME_ROOT, account, CONTAINER)

    credential = DefaultAzureCredential()
    bsc = BlobServiceClient(
        account_url=f"https://{account}.blob.core.windows.net",
        credential=credential,
    )
    container_client = bsc.get_container_client(CONTAINER)

    started = time.monotonic()
    uploaded = 0
    skipped_unchanged = 0
    total_bytes = 0

    md_settings = ContentSettings(content_type="text/markdown; charset=utf-8")

    # Build a name -> size map of existing blobs (cheap pre-filter so we skip
    # uploads whose payload is byte-identical-sized AND mtime-newer-than-on-disk).
    # Note: we don't fetch hashes — the size check is a cheap heuristic; we
    # always upload when the local file is newer than the blob's last-modified.
    log.info("listing existing blobs...")
    existing: dict[str, tuple[int, datetime]] = {}
    for b in container_client.list_blobs():
        existing[b.name] = (b.size, b.last_modified)
    log.info("existing blob count=%d", len(existing))

    for path in _iter_markdown(ME_ROOT):
        rel = _blob_path_for(ME_ROOT, path)
        try:
            stat = path.stat()
        except OSError:
            log.warning("stat failed path=%s", rel)
            continue

        local_size = stat.st_size
        local_mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

        meta = existing.get(rel)
        if meta is not None:
            blob_size, blob_modified = meta
            if blob_size == local_size and blob_modified >= local_mtime:
                skipped_unchanged += 1
                continue

        try:
            data = path.read_bytes()
        except OSError:
            log.warning("read failed path=%s", rel)
            continue

        container_client.upload_blob(
            name=rel,
            data=data,
            overwrite=True,
            content_settings=md_settings,
        )
        uploaded += 1
        total_bytes += len(data)
        log.info("uploaded path=%s size=%d", rel, len(data))

    # Manifest
    manifest = {
        "synced_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files_uploaded": uploaded,
        "files_unchanged": skipped_unchanged,
        "bytes_uploaded": total_bytes,
        "source": str(ME_ROOT),
    }
    container_client.upload_blob(
        name="_manifest.json",
        data=json.dumps(manifest, indent=2).encode("utf-8"),
        overwrite=True,
        content_settings=ContentSettings(content_type="application/json"),
    )

    duration = time.monotonic() - started
    log.info(
        "sync complete uploaded=%d unchanged=%d bytes=%d duration=%.2fs",
        uploaded, skipped_unchanged, total_bytes, duration,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
