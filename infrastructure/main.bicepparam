using 'main.bicep'

// Fill `suffix` with 5 lowercase chars (e.g. `r4k2p`) to avoid collisions
// on globally-unique resources. Re-running with the same suffix is idempotent.
//
// Generate one:
//   -join (1..5 | ForEach-Object { [char[]](0..25) | ForEach-Object { [char]([byte][char]'a' + $_) } | Get-Random })

param namePrefix = 'mindme'
param location = 'swedencentral'
param suffix = 'ymcpt'

// Function App + plan name only. ROOT CAUSE of the original wedge (4 days of
// persistent 503 on publish across site/plan/storage recreations): Flex
// Consumption host storage using managed-identity auth against the
// shared-key-DISABLED data storage wedges the SCM endpoint permanently. Fixed
// in main.bicep by moving host/deploy storage to a dedicated shared-key
// (connection-string) account. The LIVE app `func-mindme-ymcptc` was ultimately
// hand-created via `az functionapp create` (see docs/deploy.md) and runs on the
// auto-created plan `ASP-foundrylabrg-c0d2`, so a fresh `az deployment group
// create` from this template will provision a NEW `plan-mindme-ymcptc` rather
// than adopt the live plan. Treat this template as the clean-rebuild recipe.
// (2026-06-30)
param functionSuffix = 'ymcptc'

param foundryAccountName = 'foundrylab-aiservices'
param foundryProjectName = 'mindMe'

// Approved and live-verified production selection; the template default stays off.
param actionBriefing = {
  enabled: true
  modelDeployment: 'gpt-4o-mini'
}

// Reuse the capture model for explicit source reasoning; keep routine briefings on mini.
param knowledgeModelDeployment = 'gpt-4.1'

// Daily mindVault review approved 2026-09-14; no work-vault scheduling.
param dailyEvolveEnabled = true

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
