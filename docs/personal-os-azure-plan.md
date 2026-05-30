# Personal OS on Azure — MindMe Plan

> Status: proposal draft for review.
> Scope: secure personal knowledge system with OneDrive source data, MindMe agent core, Telegram notifications, and GitHub-based chat access.

## Goals

- Keep personal information organized, searchable, and private.
- Enable chat access from laptop and iPhone.
- Keep operating cost predictable.
- Use strong security defaults from day one.

## Target user flow

1. You keep source files in OneDrive.
2. MindMe sync/index pipeline imports approved content.
3. You chat with MindMe from:
   - Telegram bot (mobile-first notifications + quick actions),
   - GitHub chat bridge (GitHub app/web),
   - local VS Code workflow.
4. Sensitive data stays in controlled storage with strict auth and audit logging.

## Recommended architecture (personal, secure, cost-aware)

- **Identity/Auth:** Microsoft Entra ID (OAuth2/OIDC, delegated scopes).
- **Agent API host:** Azure Container Apps (or Function App for timer/webhook workloads).
- **Database:** Azure Database for PostgreSQL (single server, small burstable tier initially).
- **Cache/queue (optional):** Azure Cache for Redis + Storage Queue.
- **Secrets:** Azure Key Vault (tokens, app secrets, encryption material).
- **Storage:** Azure Blob Storage (snapshots, backups, artifacts).
- **Monitoring:** Application Insights + Log Analytics.
- **Integrations:**
  - Microsoft Graph for OneDrive access,
  - Telegram Bot API for notifications,
  - GitHub bridge service for chat relay.

## Estimated monthly costs (rough ranges)

### Minimal secure setup (~$50/month target)

- Container Apps/Functions: $8–20
- PostgreSQL (small): $20–35
- Key Vault + Storage: $3–8
- Monitoring/Logs: $5–12
- Network/misc: $3–10

**Total:** ~$39–85/month

### Recommended personal production (~$100/month target)

- Container Apps (separate API + worker): $15–35
- PostgreSQL (higher headroom + backups): $30–60
- Redis (basic, optional): $15–35
- Key Vault + Storage + backup retention: $5–15
- Monitoring/alerts: $8–20

**Total:** ~$73–165/month

### Notes

- LLM/API usage (OpenAI/Azure OpenAI) is separate and can dominate spend.
- Log retention and database size are common hidden drivers.
- Start lean, then scale only bottlenecks.

## Security baseline (required)

- Least-privilege Graph scopes for OneDrive.
- Key Vault-only secret management (no plaintext secrets in repo/app settings).
- Encryption in transit and at rest.
- Per-user data isolation in DB/storage.
- Token refresh/revocation handling and easy “disconnect OneDrive” action.
- Audit logs for sync, access, and admin actions.
- Budget alerts + anomaly detection in Azure Cost Management.
- Backup policy with periodic restore test.

## Access from iPhone (practical approach)

- Primary mobile UX: Telegram bot (fastest, most reliable).
- GitHub app access: use a GitHub chat bridge endpoint that relays to MindMe.
- Keep both channels behind the same authorization policy and audit trail.

## Implementation phases

1. **Foundation:** Entra app registration, Key Vault, base hosting, PostgreSQL.
2. **OneDrive integration:** OAuth flow + Graph sync/index worker.
3. **Interfaces:** Telegram bot + GitHub chat bridge + notification routing.
4. **Security hardening:** audit, budgets, alerts, backup/restore drill.
5. **Optimization:** tune costs, autoscaling, retention, and indexing policy.

## How to check your subscription cost now

I cannot directly inspect your subscription from here, so use:

1. Azure Portal → **Cost Management + Billing**.
2. Select your subscription (account: `146099412+samoletovs@users.noreply.github.com`).
3. Open **Cost analysis** (month-to-date + forecast).
4. Create **Budget alerts** at 50/80/100%.
5. Enable anomaly alerts and review top cost-by-resource.

## Decision guidance

Azure is a good option for a secure personal OS if security and long-term reliability are top priorities. If minimizing monthly cost is the top priority, keep the initial footprint minimal and defer optional services (like Redis) until required by load.
