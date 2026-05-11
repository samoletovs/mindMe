# Dev/smoke-test scripts for comes
#
# These are **disposable scaffolding** for v1 — local Python that proves the
# Telegram <-> Foundry round-trip works before Azure Functions is added in
# phase 2 of the project plan. None of this code ships to production.
#
# Scripts:
#   create_agent.py   one-shot: creates/updates the Foundry agent `companion`
#                     in project `comes-me`. Writes agent id back to .env.
#   telegram_bridge.py  long-running: Telegram long-poll <-> Foundry agent.
#                       Single-user allowlist enforced on every update.
