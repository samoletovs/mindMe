from __future__ import annotations

import subprocess
import sys
import json
from pathlib import Path


def test_local_settings_example_is_valid_json_with_private_tracing_defaults():
    path = Path(__file__).resolve().parents[1] / "local.settings.json.example"
    settings = json.loads(path.read_text(encoding="utf-8"))["Values"]
    assert settings["AZURE_TRACING_ENABLED"] == "false"
    assert "azure_sdk" in settings["OTEL_PYTHON_DISABLED_INSTRUMENTATIONS"].split(",")


def test_sdk_request_metadata_is_suppressed_but_manual_spans_work():
    # Azure/OTel configuration is process-global, so exercise startup in isolation.
    code = r'''
import io
import logging
import os
from azure.core.exceptions import ServiceRequestError
from azure.core.pipeline.transport import HttpTransport
from azure.core.settings import settings
from azure.core.tracing.ext.opentelemetry_span import OpenTelemetrySpan
from azure.storage.blob import BlobServiceClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

os.environ.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)
os.environ["AZURE_TRACING_ENABLED"] = "true"
os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "true"
os.environ["OTEL_PYTHON_DISABLED_INSTRUMENTATIONS"] = "redis"
exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)
settings.tracing_implementation = OpenTelemetrySpan
settings.tracing_enabled = True
logs = io.StringIO()
logger = logging.getLogger("azure.core.pipeline.policies.http_logging_policy")
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler(logs))

import function_app as fa

class OfflineTransport(HttpTransport):
    def open(self):
        pass
    def close(self):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.close()
    def send(self, request, **kwargs):
        raise ServiceRequestError("synthetic transport failure")

marker = "SYNTHETIC_PRIVATE_NAME"
client = BlobServiceClient(
    "https://syntheticaccount.blob.core.windows.net",
    transport=OfflineTransport(),
    retry_total=0,
)
with fa.tracer.start_as_current_span("manual.safety"):
    try:
        client.get_blob_client("personal-os", f"areas/family/{marker}.md").download_blob()
    except ServiceRequestError:
        pass
spans = exporter.get_finished_spans()
assert [span.name for span in spans] == ["manual.safety"]
assert marker not in logs.getvalue()
assert os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "false"
assert {"azure_sdk", "httpx", "redis"} <= set(
    os.environ["OTEL_PYTHON_DISABLED_INSTRUMENTATIONS"].split(",")
)
print("offline SDK privacy regression passed")
'''
    harness = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=harness,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "offline SDK privacy regression passed" in result.stdout
