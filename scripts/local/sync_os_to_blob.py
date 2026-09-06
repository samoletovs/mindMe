r"""Sync the Personal OS markdown tree to Azure Blob Storage.

Replaces the daily encrypted briefing-context blob with an upload-only cloud
copy of the OS, so the Function App can build the morning briefing entirely
in Azure (no laptop dependency at run-time).

Run this whenever you've edited the OS and want the cloud copy refreshed:

    .\.venv\Scripts\python.exe scripts\local\sync_os_to_blob.py

Or wire it into a VS Code task / git pre-push hook later if you want push-style
semantics. This is NOT a scheduled task — that's the whole point.

What's uploaded
---------------
- Every `*.md` file under `%USERPROFILE%\OneDrive\.vscode\.me` (override via
  ME_OS_ROOT in the environment or the repo's .env)
- Plus a tiny `_manifest.json` at the container root recording the sync time
  and the complete `source_files` inventory (relative filenames only), so the
  Function can exclude retained, deleted source files and detect staleness.

What's NOT uploaded
-------------------
- Non-markdown files (binaries, scripts, data exports)
- Anything under `.git`, `.venv`, `node_modules`, `__pycache__`, `.vscode`, `.cache`
- Symbolic links to files or directories

`.gitignore` is NOT consulted. Deleted or newly excluded files are NOT removed
from the container. A failed read, directory scan or upload exits nonzero and
does not publish a fresh manifest; already uploaded files are not rolled back.

Security model
--------------
- Container `personal-os` is private. RBAC-gated. Only the signed-in user and
  the Function App's managed identity can read or write.
- Storage at-rest encryption is Microsoft-managed (default). The previous
  application-layer AES-GCM is dropped — see docs/architecture.md for the
  rationale and how to re-enable it if you change your mind.

Logs
----
Sizes + counts + timings + error types. Never names, paths or content.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import sys
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

from azure.core.exceptions import AzureError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings
from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / ".env"

EXCLUDE_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".vscode", ".cache"}

log = logging.getLogger("mindMe.sync-os-to-blob")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # SDK credential warnings can contain paths and raw exception messages.
    logging.getLogger("azure").setLevel(logging.CRITICAL)
    # Explicit child levels bypass the parent's threshold; restore inheritance.
    for name in tuple(logging.Logger.manager.loggerDict):
        if name.startswith("azure."):
            logging.getLogger(name).setLevel(logging.NOTSET)


def _raise_walk_error(error: OSError) -> None:
    raise error


def _iter_markdown(root: Path) -> Iterator[Path]:
    # rglob suppresses scan errors, which would falsely certify a partial tree.
    for directory, directories, filenames in os.walk(
        root, onerror=_raise_walk_error, followlinks=False
    ):
        directories[:] = sorted(name for name in directories if name not in EXCLUDE_DIRS)
        for name in sorted(filenames):
            path = Path(directory) / name
            if name not in EXCLUDE_DIRS and path.match("*.md") and not path.is_symlink():
                yield path


def _blob_path_for(root: Path, abs_path: Path) -> str:
    """Use forward-slashes, mirror the OS tree."""
    rel = abs_path.relative_to(root)
    return rel.as_posix()


def main() -> int:
    _setup_logging()
    try:
        load_dotenv(ENV_PATH)
    except OSError as exc:
        log.error(f"Configuration read failed error_type={type(exc).__name__}")
        return 2

    root_value = os.environ.get(
        "ME_OS_ROOT",
        os.path.expandvars(r"%USERPROFILE%\OneDrive\.vscode\.me"),
    )
    container_name = os.environ.get("AZURE_STORAGE_PERSONAL_OS_CONTAINER", "personal-os")
    account = os.environ.get("AZURE_STORAGE_ACCOUNT")
    if not account or not account.strip():
        log.error("AZURE_STORAGE_ACCOUNT is not configured")
        return 2
    if not root_value.strip() or not container_name.strip():
        log.error("Personal OS root and container must not be blank")
        return 2
    root = Path(root_value)
    try:
        root_mode = root.stat().st_mode
    except OSError as exc:
        log.error(f"Personal OS root unavailable error_type={type(exc).__name__}")
        return 2
    if not stat.S_ISDIR(root_mode):
        log.error("Personal OS root is not a directory")
        return 2

    started = time.monotonic()
    uploaded = 0
    skipped_unchanged = 0
    total_bytes = 0
    source_files: list[str] = []

    try:
        with DefaultAzureCredential() as credential, BlobServiceClient(
            account_url=f"https://{account}.blob.core.windows.net",
            credential=credential,
        ) as bsc:
            container_client = bsc.get_container_client(container_name)
            md_settings = ContentSettings(content_type="text/markdown; charset=utf-8")

            existing: dict[str, tuple[int, str | None]] = {}
            for blob in container_client.list_blobs(include=["metadata"]):
                existing[blob.name] = (blob.size, (blob.metadata or {}).get("sha256"))
            log.info(f"existing blob count={len(existing)}")

            for path in _iter_markdown(root):
                rel = _blob_path_for(root, path)
                data = path.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                source_files.append(rel)
                if existing.get(rel) == (len(data), digest):
                    skipped_unchanged += 1
                    continue

                container_client.upload_blob(
                    name=rel,
                    data=data,
                    overwrite=True,
                    content_settings=md_settings,
                    metadata={"sha256": digest},
                )
                uploaded += 1
                total_bytes += len(data)
                log.info(f"uploaded size={len(data)}")

            # Publish freshness only after every candidate has been read and synced.
            manifest = {
                "synced_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "files_uploaded": uploaded,
                "files_unchanged": skipped_unchanged,
                "bytes_uploaded": total_bytes,
                "source_files": sorted(source_files),
            }
            container_client.upload_blob(
                name="_manifest.json",
                data=json.dumps(manifest, indent=2).encode("utf-8"),
                overwrite=True,
                content_settings=ContentSettings(content_type="application/json"),
            )
    except (OSError, AzureError) as exc:
        log.error(
            f"sync failed error_type={type(exc).__name__} uploaded={uploaded} "
            f"unchanged={skipped_unchanged} bytes={total_bytes}"
        )
        return 1

    duration = time.monotonic() - started
    log.info(
        f"sync complete uploaded={uploaded} unchanged={skipped_unchanged} "
        f"bytes={total_bytes} duration={duration:.2f}s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
