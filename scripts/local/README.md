# mindMe / scripts / local

Utility scripts that run **from the laptop** — only on-demand, never on a
schedule. The daily morning briefing pipeline lives in Azure
(`harness/function_app.py`). See `docs/architecture.md` for the full picture.

The sync reads the Personal OS at `%USERPROFILE%\OneDrive\.vscode\.me` directly
(override via `ME_OS_ROOT` in the environment or the repo's `.env`). This legacy
default is unchanged: explicitly configure and verify the intended root before
running. No additional vault or data source is discovered automatically.

---

## sync_os_to_blob.py — push OS edits to Azure

After you've edited the Personal OS, run this to refresh the cloud mirror so
the Function App reads your latest content at briefing time:

```powershell
# From the mindMe repository root:
.\.venv\Scripts\python.exe scripts\local\sync_os_to_blob.py
```

- Uploads `*.md` under the OS root to the private `personal-os` container
  (or the explicitly configured private container).
- Excludes `.git`, `.venv`, `node_modules`, `__pycache__`, `.vscode`, and `.cache`
  directories, and skips symbolic links. **Does not read `.gitignore`.** A
  gitignored markdown file outside these fixed exclusions is still uploaded;
  do not use `.gitignore` as the privacy boundary.
- Reads and SHA-256 hashes each candidate on every run. Skips upload only when
  blob size and `sha256` metadata match those bytes, irrespective of timestamps.
  Existing blobs without this metadata are uploaded once to seed it.
- Writes `_manifest.json` with sync time, counts and a complete `source_files`
  inventory after every candidate has been read and uploaded or hash-matched.
  Inventory entries are relative filenames only, kept in the same private
  container; no absolute source path or file content is included. Logs contain
  counts, sizes, durations and error types, not names,
  paths, content or raw exception messages.
- Exits `0` on success, `2` for invalid/missing configuration, and `1` on local
  scan/read or Azure errors. A failure stops the run without publishing a fresh
  manifest. Earlier uploads are **not rolled back**; a lost response to the
  manifest upload can leave its server-side status uncertain.
- **Upload-only, not an exact mirror:** deleting or renaming a local file, or
  newly excluding it, does not remove the old cloud blob. No pruning is done.
  Runtime readers use the inventory to hide these retained source blobs.
  A successful empty-root sync records an empty inventory: no source files
  remain visible, although the cloud bytes are retained. Managed
  `system/mindme/` preferences and onboarding state remain independent.
- **Not** a scheduled task. Run it manually, wire it into a VS Code task, or
  hang it off a git pre-push hook later. Cloud execution does not require the
  laptop online, but briefing freshness still depends on a successful push.

Sync while the source tree is not being edited and do not run concurrent syncs:
uploads and the final manifest are not a transactional snapshot. Hash metadata
assumes this sync owns the mirrored blobs; another writer preserving stale
metadata can invalidate the comparison.

One inventory is loaded and validated per runtime snapshot, then discarded;
the next snapshot sees the next completed sync. A missing inventory is legacy
compatibility mode: old blobs remain readable, but a recent timestamp cannot
claim current freshness (an old timestamp still reports its stale age).
Malformed inventories fail visibly instead of exposing every retained blob.

---

## test_briefing_snapshot.py — local validator

Imports `harness/function_app.py::_build_briefing_snapshot()` and runs it
against the live `personal-os` container. Use this to verify the snapshot
content after editing the OS or the dashboard parser:

```powershell
.\.venv\Scripts\python.exe scripts\local\test_briefing_snapshot.py
```

Output is the JSON the Foundry agent receives from `get_briefing_context()`.
**Not an offline unit test:** it reads live personal data and prints the full
snapshot. Do not run it in CI, agent logs, or shared terminal/session captures.
Use `harness/tests/test_sync_os_to_blob.py` for mocked sync regression tests;
that file never reads the real `.env` or either vault and never contacts Azure.

```powershell
.\.venv\Scripts\python.exe -m pytest harness\tests\test_sync_os_to_blob.py --basetemp=.pytest_cache\sync-os-tests -q
```

---

## briefing_builder.py — DEPRECATED 2026-05-16

Was the laptop-scheduled job that built, AES-GCM encrypted, and uploaded
`briefing-context/today.bin` daily at 07:25. Replaced by:

| Old (laptop)                               | New (cloud)                                      |
|---|---|
| Task Scheduler `mindMe-briefing-builder`   | None — no laptop schedule                        |
| `briefing_builder.py` (this folder)        | `harness/function_app.py::_build_briefing_snapshot()` |
| `briefing-context/today.bin` (encrypted blob) | Live read from `personal-os/` container       |
| `briefing-encryption-key` in Key Vault     | Not used (private container + RBAC only)         |

File kept for historical reference, not a maintained or verified fallback.
It belongs to the old encrypted-briefing pipeline and should not be run to
refresh the current cloud snapshot. Do **not** schedule it.

---

## Prerequisites (for the sync + test scripts)

- `az login` with the personal account that owns `foundrylab-rg`:
  ```powershell
  az account set --subscription "Visual Studio Enterprise Subscription"
  ```
- `.env` populated with `AZURE_STORAGE_ACCOUNT` (and optionally
  `ME_OS_ROOT` and `AZURE_STORAGE_PERSONAL_OS_CONTAINER` to override the legacy
  root and `personal-os` container). The sync loads `.env` **before** resolving
  these settings; existing shell environment values retain precedence.
- The signed-in user has `Storage Blob Data Contributor` on the storage
  account. Grant once:
  ```powershell
  $said='/subscriptions/<sub>/resourceGroups/foundrylab-rg/providers/Microsoft.Storage/storageAccounts/stmindmeymcpt'
  az role assignment create --assignee <your-object-id> --role 'Storage Blob Data Contributor' --scope $said
  ```
