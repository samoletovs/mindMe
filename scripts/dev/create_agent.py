"""Create a new version of the Foundry hosted prompt agent ``companion``.

Uses the Foundry Agents v2 pattern: ``project.agents.create_version`` with a
``PromptAgentDefinition``. Each invocation creates a new *version*; the bridge
references the agent by name (uses latest version), so re-running this is the
supported way to evolve the prompt.

Writes ``AZURE_AI_AGENT_NAME`` and ``AZURE_AI_AGENT_VERSION`` back into ``.env``.

Run from the comes/ repo root::

    .\\.venv\\Scripts\\python.exe scripts\\dev\\create_agent.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / ".env"

SYSTEM_PROMPT = """\
You are comes (Latin: companion at the table). A quiet, single-user personal
companion.

Style:
- Speak briefly. One or two sentences is usually right.
- Plain text. No bullet lists, no markdown, no emoji, unless explicitly asked.
- Warm but not effusive. Direct but not blunt.

Capabilities right now:
- This is a smoke test. You have no tools yet. You cannot read his data.
- If asked about briefing, journal, finances, or calendar: say you do not have
  access yet, and that those tools land in phase 2.

Conversation:
- If the user says only "ping", reply only with "pong".
- If the user asks how things are going, ask one specific clarifying question.
- Never pretend to have done a thing you cannot do.
"""


def main() -> int:
    load_dotenv(ENV_PATH)

    endpoint = os.environ.get("AZURE_AI_PROJECT_ENDPOINT")
    agent_name = os.environ.get("AZURE_AI_AGENT_NAME", "companion")
    model = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT", "gpt-4o-mini")

    if not endpoint:
        print("ERROR: AZURE_AI_PROJECT_ENDPOINT not set in .env", file=sys.stderr)
        return 2

    print(f"Connecting to {endpoint}")
    client = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())

    print(f"Creating new version of agent '{agent_name}' on model '{model}'...")
    agent = client.agents.create_version(
        agent_name=agent_name,
        definition=PromptAgentDefinition(
            model=model,
            instructions=SYSTEM_PROMPT,
            temperature=0.7,
        ),
        description="comes - personal companion. Phase 1 smoke test, no tools yet.",
    )

    version = getattr(agent, "version", None)
    agent_id = getattr(agent, "id", None)
    model_used = getattr(getattr(agent, "definition", None), "model", model)

    print(
        f"OK - agent version created:\n"
        f"  id      = {agent_id}\n"
        f"  name    = {agent.name}\n"
        f"  version = {version}\n"
        f"  model   = {model_used}"
    )

    _upsert_env_var(ENV_PATH, "AZURE_AI_AGENT_NAME", agent.name)
    if version is not None:
        _upsert_env_var(ENV_PATH, "AZURE_AI_AGENT_VERSION", str(version))
    print(f"Wrote AZURE_AI_AGENT_NAME and AZURE_AI_AGENT_VERSION to {ENV_PATH}")

    return 0


def _upsert_env_var(env_path: Path, key: str, value: str) -> None:
    """Replace the line for ``key`` in .env, or append it if absent."""
    if not env_path.exists():
        env_path.write_text(f"{key}={value}\n", encoding="utf-8")
        return

    text = env_path.read_text(encoding="utf-8")
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(text):
        new_text = pattern.sub(f"{key}={value}", text)
    else:
        sep = "" if text.endswith("\n") else "\n"
        new_text = f"{text}{sep}{key}={value}\n"
    env_path.write_text(new_text, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
