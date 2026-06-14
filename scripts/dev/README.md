# Dev/smoke-test scripts for mindMe
#
# These are local bootstrap and smoke-test helpers. They are useful for
# validating Foundry wiring and older local bot flows, but none of this code
# ships to production now that Azure Functions owns the live Telegram path.
#
# Scripts:
#   create_agent.py   one-shot: creates/updates the Foundry agent `companion`
#                     in project `mindMe`. Writes agent id back to .env.
#   telegram_bridge.py  long-running: Telegram long-poll <-> Foundry agent.
#                       Single-user allowlist enforced on every update.
