# mindMe / scripts / local

Scripts that run **on the laptop** (not in Azure). These touch the Personal OS
at `c:\vsCode\.me` directly. They are not in the Function App.

## briefing_builder.py

Runs at 07:25 daily via Windows Task Scheduler. Builds the sanitized briefing
JSON, AES-GCM encrypts it (key from Key Vault), uploads to
`briefing-context/today.bin`.

Current blob schema is tiered:
- `core` (always loaded): today's focus, goals, open loops, mood/energy, area headlines, urgent deadlines.
- `extended` (on demand): scored summaries with metadata.
- `deep` (rare fallback): compressed excerpts (`zlib+base64`) for extra context.

Optional size controls via `.env`:
- `BRIEFING_CORE_MAX_BYTES` (default `3500`)
- `BRIEFING_EXTENDED_MAX_BYTES` (default `7000`)
- `BRIEFING_DEEP_MAX_BYTES` (default `9000`)
- `BRIEFING_DEEP_ZLIB_LEVEL` (default `9`)

### Run once manually

```powershell
cd c:\vsCode\.nauroLabs\mindMe
.\.venv\Scripts\python.exe scripts\local\briefing_builder.py
```

### Install as a scheduled task (one-time)

```powershell
$action = New-ScheduledTaskAction `
    -Execute "c:\vsCode\.nauroLabs\mindMe\.venv\Scripts\python.exe" `
    -Argument "c:\vsCode\.nauroLabs\mindMe\scripts\local\briefing_builder.py" `
    -WorkingDirectory "c:\vsCode\.nauroLabs\mindMe"

$trigger = New-ScheduledTaskTrigger -Daily -At 07:25

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName "mindMe-briefing-builder" `
    -Action $action -Trigger $trigger -Settings $settings -Description "Build + encrypt + upload mindMe briefing context daily at 07:25."
```

### Prerequisites

- `az login` (personal account) — `DefaultAzureCredential` uses Azure CLI cached
  creds.
- `.env` populated with `AZURE_KEYVAULT_NAME` and `AZURE_STORAGE_ACCOUNT` from
  Bicep output.
- Key Vault contains `briefing-encryption-key` (32 random bytes, base64):
  ```powershell
  $key = [Convert]::ToBase64String((1..32 | ForEach-Object { Get-Random -Maximum 256 } | ForEach-Object { [byte]$_ }))
  az keyvault secret set --vault-name kv-mindme-<suffix> --name briefing-encryption-key --value $key
  ```
- The signed-in user has `Key Vault Secrets User` (or higher) on the Vault.
