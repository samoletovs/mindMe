"""Model-retirement contract: nothing this repo owns resolves to a retiring model.

gpt-4o, gpt-4o-mini, gpt-4.1(-mini/-nano) retire 2027-04-14 (o3/o4-mini earlier).
Their deployments auto-upgrade to gpt-6-luna, which rejects ``max_tokens``.
"""

import importlib.util
import re
from pathlib import Path
from unittest.mock import Mock

import pytest

import function_app as fa

ROOT = Path(__file__).resolve().parents[2]
RETIRING = re.compile(r"(?<![\w.-])(?:gpt-4o[\w.-]*|gpt-4\.1[\w.-]*|o[34]-mini[\w.-]*|o3)(?![\w.-])")
OWNED = [
    ROOT / ".env.example",
    ROOT / "harness" / "local.settings.json.example",
    ROOT / "infrastructure" / "main.bicep",
    ROOT / "infrastructure" / "main.bicepparam",
    ROOT / "scripts" / "dev" / "create_agent.py",
    *sorted((ROOT / "harness").glob("*.py")),
    *sorted((ROOT / "agent").glob("*.json")),
    *sorted((ROOT / ".github" / "workflows").glob("*.yml")),
]


def _retiring(text: str) -> list[str]:
    # Transcription models follow a separate, later retirement and are out of scope.
    return [hit for hit in RETIRING.findall(text) if not hit.endswith("-transcribe")]


def test_retiring_pattern_catches_every_retiring_family():
    sample = "gpt-4o gpt-4o-mini gpt-4.1 gpt-4.1-mini gpt-4.1-nano o3 o4-mini o3-mini gpt-4o-mini-transcribe"
    assert _retiring(sample) == ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "o3", "o4-mini", "o3-mini"]


@pytest.mark.parametrize("path", OWNED, ids=lambda path: str(path.relative_to(ROOT)))
def test_owned_defaults_and_config_do_not_name_a_retiring_model(path):
    assert _retiring(path.read_text(encoding="utf-8")) == []


def _capture(monkeypatch, surface: str) -> Mock:
    client = Mock()
    create = getattr(client.with_options.return_value, surface)
    (create.completions.create if surface == "chat" else create.create).side_effect = RuntimeError("captured")
    monkeypatch.setattr(fa, "_foundry", lambda: (None, client))
    monkeypatch.setattr(fa, "_http_client", lambda: Mock())
    return create.completions.create if surface == "chat" else create.create


SOURCE = {
    "path": "wiki/sources/synthetic.md", "revision": "a" * 40, "digest": "b" * 64, "kind": "wiki",
    "title": "Synthetic", "text": "Synthetic evidence sentence for a contract test.",
    "url": "https://github.com/example/mindVault/blob/" + "a" * 40 + "/wiki/sources/synthetic.md",
}
RESPONSES_PATHS = {
    "evolve": (fa._generate_evolve_review, "MINDME_BRIEFING_MODEL", {"sources": []}),
    "briefing": (fa._generate_action_plan, "MINDME_BRIEFING_MODEL", {"sources": [SOURCE]}),
    "knowledge": (fa._generate_knowledge, "MINDME_KNOWLEDGE_MODEL", {
        "action": "explain", "query": "Explain", "sources": [SOURCE], "memories": [],
    }),
}


OLD_DEPLOYMENTS = {"MINDME_BRIEFING_MODEL": "gpt-4o-mini", "MINDME_KNOWLEDGE_MODEL": "gpt-4.1"}
# Prompts carry a fresh random data fence per call; every other request parameter must match.
PROMPT_KEYS = {"model", "input", "messages"}


def _assert_luna_sampling_is_safe(body) -> None:
    """gpt-6-luna returns HTTP 400 for temperature/top_p unless reasoning effort is none."""
    get = body.get if isinstance(body, dict) else lambda key: getattr(body, key, None)
    if get("temperature") is not None or get("top_p") is not None:
        reasoning = get("reasoning")
        effort = reasoning.get("effort") if isinstance(reasoning, dict) else getattr(reasoning, "effort", None)
        assert get("reasoning_effort") == "none" or effort == "none"


def _responses_body(monkeypatch, name, deployment):
    generate, setting, context = RESPONSES_PATHS[name]
    monkeypatch.setenv(setting, deployment)
    create = _capture(monkeypatch, "responses")
    with pytest.raises(RuntimeError, match="captured"):
        generate(context)
    return create.call_args.kwargs


def _chat_body(monkeypatch, deployment):
    monkeypatch.setenv("MINDME_BRIEFING_MODEL", deployment)
    create = _capture(monkeypatch, "chat")
    with pytest.raises(RuntimeError, match="captured"):
        fa._generate_task_preparation({"source": {"text": "Synthetic task."}, "scope": "Outline only."})
    return create.call_args.kwargs


@pytest.mark.parametrize("name", sorted(RESPONSES_PATHS))
def test_luna_responses_paths_disable_reasoning_and_never_send_max_tokens(monkeypatch, name):
    call = _responses_body(monkeypatch, name, "gpt-6-luna")
    assert call["model"] == "gpt-6-luna"
    assert call["reasoning"] == {"effort": "none"}
    assert call["max_output_tokens"] <= 2800
    assert "max_tokens" not in call
    _assert_luna_sampling_is_safe(call)


@pytest.mark.parametrize("name", sorted(RESPONSES_PATHS))
def test_old_deployment_gets_the_same_responses_body_without_reasoning(monkeypatch, name):
    setting = RESPONSES_PATHS[name][1]
    luna = _responses_body(monkeypatch, name, "gpt-6-luna")
    old = _responses_body(monkeypatch, name, OLD_DEPLOYMENTS[setting])
    assert "reasoning" not in old and "max_tokens" not in old
    assert old["model"] == OLD_DEPLOYMENTS[setting]
    assert {k: v for k, v in old.items() if k not in PROMPT_KEYS} == {
        k: v for k, v in luna.items() if k not in PROMPT_KEYS | {"reasoning"}
    }


def test_luna_task_preparation_uses_completion_token_ceiling_and_no_reasoning(monkeypatch):
    call = _chat_body(monkeypatch, "gpt-6-luna")
    assert call["max_completion_tokens"] == 1200
    assert call["reasoning_effort"] == "none"
    assert "max_tokens" not in call
    _assert_luna_sampling_is_safe(call)


def test_old_deployment_gets_the_same_chat_body_without_reasoning(monkeypatch):
    luna = _chat_body(monkeypatch, "gpt-6-luna")
    old = _chat_body(monkeypatch, "gpt-4o-mini")
    assert "reasoning_effort" not in old and "max_tokens" not in old
    assert old["max_completion_tokens"] == 1200
    assert {k: v for k, v in old.items() if k not in PROMPT_KEYS} == {
        k: v for k, v in luna.items() if k not in PROMPT_KEYS | {"reasoning_effort"}
    }


def test_companion_definition_keeps_temperature_and_disables_luna_reasoning(monkeypatch):
    spec = importlib.util.spec_from_file_location("create_agent", ROOT / "scripts" / "dev" / "create_agent.py")
    create_agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(create_agent)
    for hostname, connection in ((None, None), ("synthetic.example", "/connections/synthetic")):
        definition, _, _ = create_agent._build_definition("gpt-6-luna", hostname, connection)
        assert definition.model == "gpt-6-luna"
        assert definition.temperature == 0.7 and definition.top_p is None
        assert definition.reasoning.effort == "none"
        _assert_luna_sampling_is_safe(definition)
    old, _, _ = create_agent._build_definition("gpt-4o-mini", None)
    assert old.temperature == 0.7 and old.reasoning is None


@pytest.mark.parametrize("body", [
    {"model": "gpt-6-luna", "temperature": 0.7},
    {"model": "gpt-6-luna", "top_p": 0.9, "reasoning": {"effort": "low"}},
    {"model": "gpt-6-luna", "temperature": 0.2, "reasoning_effort": "low"},
])
def test_luna_sampling_guard_rejects_temperature_without_reasoning_none(body):
    with pytest.raises(AssertionError):
        _assert_luna_sampling_is_safe(body)
