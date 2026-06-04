# mindMe / scripts / local

Utility scripts that run **from the laptop** — but only on-demand, never on a
schedule. The daily morning briefing pipeline lives entirely in Azure
(`harness/function_app.py`). See `docs/architecture.md` for the full picture.

These scripts touch the Personal OS at `%USERPROFILE%\OneDrive\.vscode\.me`
directly (override via `ME_OS_ROOT` env var).

---

## sync_os_to_blob.py — push OS edits to Azure

After you've edited the Personal OS, run this to refresh the cloud mirror so
the Function App reads your latest content at briefing time:

```powershell
cd c:\vsCode\.nauroLabs\mindMe
.\.venv\Scripts\python.exe scripts\local\sync_os_to_blob.py
```

- Uploads every `*.md` under the OS root to the `personal-os` container.
- Skips files where `size` and `last-modified` already match (cheap idempotent
  re-runs).
- Writes a `_manifest.json` blob at the container root with the sync time.
- **Not** a scheduled task. Run it manually, wire it into a VS Code task, or
  hang it off a git pre-push hook later — your choice. The Function App does
  not depend on this running on any particular cadence.

---

## test_briefing_snapshot.py — local validator

Imports `harness/function_app.py::_build_briefing_snapshot()` and runs it
against the live `personal-os` container. Use this to verify the snapshot
content after editing the OS or the dashboard parser:

```powershell
.\.venv\Scripts\python.exe scripts\local\test_briefing_snapshot.py
```

Output is the JSON the Foundry agent receives from `get_briefing_context()`.

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

File kept for reference and fallback. Do **not** schedule it.

---

## Prerequisites (for the sync + test scripts)

- `az login` with the personal account that owns `foundrylab-rg`:
  ```powershell
  az account set --subscription "Visual Studio Enterprise Subscription"
  ```
- `.env` populated with `AZURE_STORAGE_ACCOUNT` (and optionally
  `AZURE_STORAGE_PERSONAL_OS_CONTAINER` if you want to override the
  `personal-os` default).
- The signed-in user has `Storage Blob Data Contributor` on the storage
  account. Grant once:
  ```powershell
  $said='/subscriptions/<sub>/resourceGroups/foundrylab-rg/providers/Microsoft.Storage/storageAccounts/stmindmeymcpt'
  az role assignment create --assignee <your-object-id> --role 'Storage Blob Data Contributor' --scope $said
  ```
