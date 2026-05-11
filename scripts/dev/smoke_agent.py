"""Smoke test: send one message to the ``companion`` agent and print the reply.

No Telegram involved. Use this to confirm the Foundry round-trip works before
starting the bridge.

Run from the comes/ repo root::

    .\\.venv\\Scripts\\python.exe scripts\\dev\\smoke_agent.py "hello"
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / ".env"


def main(argv: list[str]) -> int:
    load_dotenv(ENV_PATH)

    endpoint = os.environ["AZURE_AI_PROJECT_ENDPOINT"]
    agent_name = os.environ.get("AZURE_AI_AGENT_NAME", "companion")
    user_input = " ".join(argv[1:]).strip() or "ping"

    project = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())
    openai = project.get_openai_client()

    print(f"Creating conversation...")
    conversation = openai.conversations.create()
    print(f"  conversation.id = {conversation.id}")

    print(f"Sending input ({len(user_input)} chars)...")
    response = openai.responses.create(
        conversation=conversation.id,
        input=user_input,
        extra_body={"agent_reference": {"name": agent_name, "type": "agent_reference"}},
    )

    print()
    print("=== reply ===")
    print(response.output_text)
    print("=============")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
