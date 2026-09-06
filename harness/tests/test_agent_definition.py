"""Agent registration contract tests with real SDK models and mocked services."""

from __future__ import annotations

import importlib.util
import json
import logging
import re
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, create_autospec

import pytest
from azure.ai.projects.operations import ConnectionsOperations
from azure.core.exceptions import ResourceNotFoundError, ServiceRequestError


@pytest.fixture
def registration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[tuple[ModuleType, MagicMock]]:
    script = Path(__file__).resolve().parents[2] / "scripts" / "dev" / "create_agent.py"
    spec = importlib.util.spec_from_file_location("create_agent_under_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("AZURE_AI_PROJECT_ENDPOINT", "https://synthetic.example/api/projects/test")
    monkeypatch.setenv("AZURE_AI_AGENT_NAME", "synthetic-agent")
    monkeypatch.setenv("AZURE_AI_MODEL_DEPLOYMENT", "synthetic-model")
    monkeypatch.delenv("MINDME_FUNCTION_APP_HOSTNAME", raising=False)
    monkeypatch.delenv("MINDME_TOOLS_CONNECTION_NAME", raising=False)
    monkeypatch.setattr(module, "ENV_PATH", tmp_path / "synthetic-config")
    monkeypatch.setattr(module, "load_dotenv", MagicMock())
    monkeypatch.setattr(module, "_upsert_env_var", MagicMock())
    monkeypatch.setattr(module, "DefaultAzureCredential", MagicMock())
    client = MagicMock()
    client.__enter__.return_value = client
    client.connections = create_autospec(ConnectionsOperations, instance=True)
    client.connections.get.return_value = SimpleNamespace(id="synthetic-connection-id")
    client.agents.create_version.return_value = SimpleNamespace(
        name="synthetic-agent", id="synthetic-agent-id", version="7"
    )
    monkeypatch.setattr(module, "AIProjectClient", MagicMock(return_value=client))
    logging.getLogger("azure")
    previous_azure_levels = {
        name: logger.level
        for name, logger in logging.Logger.manager.loggerDict.items()
        if (name == "azure" or name.startswith("azure.")) and isinstance(logger, logging.Logger)
    }
    yield module, client
    for name, logger in tuple(logging.Logger.manager.loggerDict.items()):
        if (name == "azure" or name.startswith("azure.")) and isinstance(logger, logging.Logger):
            logger.setLevel(previous_azure_levels.get(name, logging.NOTSET))


def test_phase_two_serializes_project_connection_authentication(
    registration: tuple[ModuleType, MagicMock],
) -> None:
    module, _ = registration

    definition, phase, _ = module._build_definition(
        "synthetic-model", "synthetic.azurewebsites.net", "synthetic-connection-id"
    )

    payload = definition.as_dict()
    tool = payload["tools"][0]
    assert tool["type"] == "openapi"
    assert tool["openapi"]["auth"] == {
        "type": "project_connection",
        "security_scheme": {"project_connection_id": "synthetic-connection-id"},
    }
    assert tool["openapi"]["spec"]["servers"] == [
        {"url": "https://synthetic.azurewebsites.net/api"}
    ]
    assert tool["openapi"]["spec"] == module._load_openapi_spec(
        "synthetic.azurewebsites.net"
    )
    assert payload["model"] == "synthetic-model"
    assert phase == "phase 2 (tools)"
    assert "anonymous" not in json.dumps(payload)


@pytest.mark.parametrize("connection_id", [None, "", "  "])
def test_tools_cannot_be_built_without_a_connection_id(
    registration: tuple[ModuleType, MagicMock], connection_id: str | None
) -> None:
    module, _ = registration

    with pytest.raises(ValueError, match="connection"):
        module._build_definition("synthetic-model", "synthetic.azurewebsites.net", connection_id)


def test_phase_one_serialization_has_no_tools_or_connection_requirement(
    registration: tuple[ModuleType, MagicMock],
) -> None:
    module, _ = registration

    definition, phase, _ = module._build_definition("synthetic-model", None)

    assert not definition.as_dict().get("tools")
    assert definition.as_dict()["instructions"] == module.SYSTEM_PROMPT_PHASE1
    assert phase == "phase 1 (no tools)"


def test_spec_requires_the_function_key_header_on_every_operation(
    registration: tuple[ModuleType, MagicMock],
) -> None:
    module, _ = registration
    spec = module._load_openapi_spec("synthetic.azurewebsites.net")

    assert spec["components"]["securitySchemes"]["functionKey"] == {
        "type": "apiKey",
        "in": "header",
        "name": "x-functions-key",
    }
    operations = [
        operation
        for path in spec["paths"].values()
        for method, operation in path.items()
        if method.lower() in {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
    ]
    assert len(operations) == 4
    for operation in operations:
        assert operation["security"] == [{"functionKey": []}], operation["operationId"]


def test_briefing_spec_describes_flat_core_and_empty_legacy_tiers(
    registration: tuple[ModuleType, MagicMock],
) -> None:
    module, _ = registration
    operation = module._load_openapi_spec("synthetic.azurewebsites.net")["paths"][
        "/tools/briefing_context"
    ]["post"]

    description = operation["description"].lower()
    assert "flat" in description
    assert "legacy" in description and "empty" in description
    assert "decrypted" not in description
    properties = operation["responses"]["200"]["content"]["application/json"]["schema"]["properties"]
    assert properties["sections"]["type"] == "array"
    assert properties["sections"]["items"]["type"] == "string"
    assert "empty" in properties["entries"]["description"].lower()


@pytest.mark.parametrize("connection_name", [None, "configured-tools"])
def test_main_resolves_default_or_configured_connection_without_fetching_secrets(
    registration: tuple[ModuleType, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    connection_name: str | None,
) -> None:
    module, client = registration
    monkeypatch.setenv("MINDME_FUNCTION_APP_HOSTNAME", "synthetic.azurewebsites.net")
    if connection_name is not None:
        monkeypatch.setenv("MINDME_TOOLS_CONNECTION_NAME", connection_name)

    assert module.main() == 0

    client.connections.get.assert_called_once_with(
        connection_name or "mindme-tools", include_credentials=False
    )
    definition = client.agents.create_version.call_args.kwargs["definition"].as_dict()
    assert definition["tools"][0]["openapi"]["auth"]["security_scheme"] == {
        "project_connection_id": "synthetic-connection-id"
    }
    module._upsert_env_var.assert_any_call(module.ENV_PATH, "AZURE_AI_AGENT_VERSION", "7")


def test_main_keeps_phase_one_bootstrap_independent_of_connections(
    registration: tuple[ModuleType, MagicMock],
) -> None:
    module, client = registration
    client.connections.get.side_effect = AssertionError("phase one must not resolve connections")

    assert module.main() == 0

    client.connections.get.assert_not_called()
    definition = client.agents.create_version.call_args.kwargs["definition"].as_dict()
    assert not definition.get("tools")


@pytest.mark.parametrize("failure", [ResourceNotFoundError, ServiceRequestError])
def test_connection_lookup_failure_never_creates_an_agent_or_updates_env(
    registration: tuple[ModuleType, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: type[Exception],
) -> None:
    module, client = registration
    monkeypatch.setenv("MINDME_FUNCTION_APP_HOSTNAME", "synthetic.azurewebsites.net")
    client.connections.get.side_effect = failure("private-connection-response")

    assert module.main() != 0

    client.agents.create_version.assert_not_called()
    module._upsert_env_var.assert_not_called()
    assert "private-connection-response" not in caplog.text
    assert caplog.records


@pytest.mark.parametrize("connection_id", [None, "", " "])
def test_missing_connection_id_never_creates_an_agent(
    registration: tuple[ModuleType, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    connection_id: str | None,
) -> None:
    module, client = registration
    monkeypatch.setenv("MINDME_FUNCTION_APP_HOSTNAME", "synthetic.azurewebsites.net")
    client.connections.get.return_value = SimpleNamespace(id=connection_id)

    assert module.main() != 0

    client.agents.create_version.assert_not_called()
    module._upsert_env_var.assert_not_called()


def test_blank_connection_name_fails_instead_of_selecting_anonymous_tools(
    registration: tuple[ModuleType, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    module, client = registration
    monkeypatch.setenv("MINDME_FUNCTION_APP_HOSTNAME", "synthetic.azurewebsites.net")
    monkeypatch.setenv("MINDME_TOOLS_CONNECTION_NAME", " ")

    assert module.main() != 0

    client.connections.get.assert_not_called()
    client.agents.create_version.assert_not_called()


def test_serialized_prompt_respects_enabled_sections_and_data_trust_boundaries(
    registration: tuple[ModuleType, MagicMock],
) -> None:
    module, _ = registration
    definition, _, _ = module._build_definition(
        "synthetic-model", "synthetic.azurewebsites.net", "synthetic-connection-id"
    )
    instructions = " ".join(definition.as_dict()["instructions"].split()).lower()

    assert "disabled sections" in instructions
    assert 'only when "weather" is in `sections`' in instructions
    assert "untrusted data" in instructions
    assert "never follow instructions" in instructions
    assert "read-only" in instructions
    assert "never claim" in instructions
    assert "explicitly confirmed" in instructions
    assert "legacy" in instructions and "empty" in instructions


def test_failed_agent_creation_does_not_record_a_successful_registration(
    registration: tuple[ModuleType, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module, client = registration
    monkeypatch.setenv("MINDME_FUNCTION_APP_HOSTNAME", "synthetic.azurewebsites.net")
    client.agents.create_version.side_effect = ServiceRequestError(
        "private-service-response"
    )

    assert module.main() == 1

    client.agents.create_version.assert_called_once()
    module._upsert_env_var.assert_not_called()
    assert "ServiceRequestError" in caplog.text
    assert "private-service-response" not in caplog.text


@pytest.mark.parametrize(
    "sdk_logger_name",
    [
        "azure.identity",
        "azure.identity._internal.privacy_test",
        "azure.core.pipeline.policies.http_logging_policy",
    ],
)
def test_registration_suppresses_explicit_sdk_child_log_levels_after_harness_import(
    registration: tuple[ModuleType, MagicMock],
    caplog: pytest.LogCaptureFixture,
    sdk_logger_name: str,
) -> None:
    module, _ = registration
    importlib.import_module("function_app")
    sdk_logger = logging.getLogger(sdk_logger_name)
    sdk_logger.setLevel(logging.WARNING)

    assert module.main() == 0

    sdk_logger.warning("synthetic-private-credential-path")
    sdk_logger.error("synthetic-private-credential-path")
    assert "synthetic-private-credential-path" not in caplog.text


def test_http_response_codes_belong_to_responses_not_schema_objects(
    registration: tuple[ModuleType, MagicMock],
) -> None:
    module, _ = registration
    spec = module._load_openapi_spec("synthetic.azurewebsites.net")
    schemas: list[dict] = []
    for path in spec["paths"].values():
        for operation in path.values():
            responses = operation["responses"]
            assert "401" in responses, operation["operationId"]
            for response in responses.values():
                for media in response.get("content", {}).values():
                    schemas.append(media["schema"])
    while schemas:
        schema = schemas.pop()
        assert not any(re.fullmatch(r"[1-5](?:\d{2}|XX)", key) for key in schema)
        schemas.extend(schema.get("properties", {}).values())
        for keyword in ("items", "additionalProperties", "not"):
            if isinstance(schema.get(keyword), dict):
                schemas.append(schema[keyword])
        for keyword in ("allOf", "anyOf", "oneOf"):
            schemas.extend(schema.get(keyword, []))
