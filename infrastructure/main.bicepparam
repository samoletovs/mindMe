using 'main.bicep'

// Fill `suffix` with 5 lowercase chars (e.g. `r4k2p`) to avoid collisions
// on globally-unique resources. Re-running with the same suffix is idempotent.
//
// Generate one:
//   -join (1..5 | ForEach-Object { [char[]](0..25) | ForEach-Object { [char]([byte][char]'a' + $_) } | Get-Random })

param namePrefix = 'mindme'
param location = 'swedencentral'
param suffix = 'ymcpt'

param foundryAccountName = 'foundrylab-aiservices'
param foundryProjectName = 'mindMe'

// Hard Rule 2 single-user allowlist. Read from local env var so the value
// never lives in source. Set TELEGRAM_ALLOWED_CHAT_ID in .env or the
// session shell before running `az deployment group create -p main.bicepparam`.
param telegramAllowedChatId = readEnvironmentVariable('TELEGRAM_ALLOWED_CHAT_ID', '')

param tags = {
  project: 'mindMe'
  owner: 'samoletovs'
  costCenter: 'personal'
  environment: 'prod'
}
