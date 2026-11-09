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


@pytest.mark.parametrize("name", sorted(RESPONSES_PATHS))
def test_luna_responses_paths_disable_reasoning_and_never_send_max_tokens(monkeypatch, name):
    generate, setting, context = RESPONSES_PATHS[name]
    monkeypatch.setenv(setting, "gpt-6-luna")
    create = _capture(monkeypatch, "responses")
    with pytest.raises(RuntimeError, match="captured"):
        generate(context)
    call = create.call_args.kwargs
    assert call["model"] == "gpt-6-luna"
    assert call["reasoning"] == {"effort": "none"}
    assert call["max_output_tokens"] <= 2800
    assert "max_tokens" not in call and "temperature" not in call and "top_p" not in call


@pytest.mark.parametrize("name", sorted(RESPONSES_PATHS))
def test_other_deployments_keep_the_existing_request_shape(monkeypatch, name):
    generate, setting, context = RESPONSES_PATHS[name]
    monkeypatch.setenv(setting, "existing-model")
    create = _capture(monkeypatch, "responses")
    with pytest.raises(RuntimeError, match="captured"):
        generate(context)
    assert "reasoning" not in create.call_args.kwargs


def test_luna_task_preparation_uses_completion_token_ceiling_and_no_reasoning(monkeypatch):
    monkeypatch.setenv("MINDME_BRIEFING_MODEL", "gpt-6-luna")
    create = _capture(monkeypatch, "chat")
    with pytest.raises(RuntimeError, match="captured"):
        fa._generate_task_preparation({"source": {"text": "Synthetic task."}, "scope": "Outline only."})
    call = create.call_args.kwargs
    assert call["max_completion_tokens"] == 1200
    assert call["reasoning_effort"] == "none"
    assert "max_tokens" not in call and "temperature" not in call


def test_companion_definition_targets_luna_without_sampling_overrides(monkeypatch):
    spec = importlib.util.spec_from_file_location("create_agent", ROOT / "scripts" / "dev" / "create_agent.py")
    create_agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(create_agent)
    for hostname, connection in ((None, None), ("synthetic.example", "/connections/synthetic")):
        definition, _, _ = create_agent._build_definition("gpt-6-luna", hostname, connection)
        assert definition.model == "gpt-6-luna"
        assert definition.temperature is None and definition.top_p is None
        assert definition.reasoning.effort == "none"
