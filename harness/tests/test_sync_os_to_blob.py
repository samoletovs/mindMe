"""Offline sync regressions; all input and storage clients are synthetic."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
from collections.abc import Iterator
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest
from azure.core.exceptions import ServiceRequestError
from dotenv import load_dotenv


@pytest.fixture
def sync_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[ModuleType, Path, MagicMock]]:
    root = tmp_path / "synthetic-root"
    root.mkdir()
    monkeypatch.setenv("ME_OS_ROOT", str(root))
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT", "syntheticaccount")
    monkeypatch.setenv("AZURE_STORAGE_PERSONAL_OS_CONTAINER", "synthetic-container")

    script = Path(__file__).resolve().parents[2] / "scripts" / "local" / "sync_os_to_blob.py"
    spec = importlib.util.spec_from_file_location("sync_os_to_blob_under_test", script)
    assert spec is not None and spec.loader is not None
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    monkeypatch.setattr(sync, "ENV_PATH", tmp_path / "synthetic-config")
    monkeypatch.setattr(sync, "load_dotenv", lambda _: False)
    monkeypatch.setattr(sync, "DefaultAzureCredential", MagicMock())
    container = MagicMock()
    container.list_blobs.return_value = []
    service = MagicMock()
    service.return_value.__enter__.return_value = service.return_value
    service.return_value.get_container_client.return_value = container
    monkeypatch.setattr(sync, "BlobServiceClient", service)
    logging.getLogger("azure")
    previous_azure_levels = {
        name: logger.level
        for name, logger in logging.Logger.manager.loggerDict.items()
        if (name == "azure" or name.startswith("azure.")) and isinstance(logger, logging.Logger)
    }
    yield sync, root, container
    for name, logger in tuple(logging.Logger.manager.loggerDict.items()):
        if (name == "azure" or name.startswith("azure.")) and isinstance(logger, logging.Logger):
            logger.setLevel(previous_azure_levels.get(name, logging.NOTSET))


def _remote_blob(
    data: bytes, metadata: dict[str, str] | None, modified: datetime
) -> SimpleNamespace:
    return SimpleNamespace(
        name="note.md", size=len(data), last_modified=modified, metadata=metadata
    )


def test_dotenv_root_and_container_are_resolved_after_loading(
    sync_env: tuple[ModuleType, Path, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    sync, root, container = sync_env
    selected = root.parent / "configured-root"
    selected.mkdir()
    (selected / "configured.md").write_bytes(b"synthetic configured content")
    sync.ENV_PATH.write_text(
        f"ME_OS_ROOT='{selected.as_posix()}'\n"
        "AZURE_STORAGE_PERSONAL_OS_CONTAINER=configured-container\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ME_OS_ROOT")
    monkeypatch.delenv("AZURE_STORAGE_PERSONAL_OS_CONTAINER")
    monkeypatch.setattr(sync, "load_dotenv", load_dotenv)

    assert sync.main() == 0

    sync.BlobServiceClient.return_value.get_container_client.assert_called_once_with(
        "configured-container"
    )
    assert container.upload_blob.call_args_list[0].kwargs["name"] == "configured.md"


def test_existing_environment_keeps_precedence_over_dotenv(
    sync_env: tuple[ModuleType, Path, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    sync, root, container = sync_env
    (root / "note.md").write_bytes(b"synthetic shell configuration")
    sync.ENV_PATH.write_text(
        "ME_OS_ROOT=should-not-be-used\n"
        "AZURE_STORAGE_PERSONAL_OS_CONTAINER=should-not-be-used\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sync, "load_dotenv", load_dotenv)

    assert sync.main() == 0

    sync.BlobServiceClient.return_value.get_container_client.assert_called_once_with(
        "synthetic-container"
    )
    assert container.upload_blob.call_args_list[0].kwargs["name"] == "note.md"


def test_changed_equal_length_content_uploads_with_restored_timestamp(
    sync_env: tuple[ModuleType, Path, MagicMock],
) -> None:
    sync, root, container = sync_env
    original, changed = b"old!", b"new!"
    note = root / "note.md"
    note.write_bytes(changed)
    os.utime(note, (1, 1))
    container.list_blobs.return_value = [
        _remote_blob(
            original,
            {"sha256": hashlib.sha256(original).hexdigest()},
            datetime.now(timezone.utc),
        )
    ]

    assert sync.main() == 0

    upload = container.upload_blob.call_args_list[0].kwargs
    assert upload["name"] == "note.md"
    assert upload["data"] == changed
    assert upload["metadata"] == {"sha256": hashlib.sha256(changed).hexdigest()}
    container.list_blobs.assert_called_once_with(include=["metadata"])


def test_matching_hash_skips_content_even_when_local_timestamp_is_newer(
    sync_env: tuple[ModuleType, Path, MagicMock],
) -> None:
    sync, root, container = sync_env
    data = b"unchanged synthetic note"
    (root / "note.md").write_bytes(data)
    container.list_blobs.return_value = [
        _remote_blob(
            data,
            {"sha256": hashlib.sha256(data).hexdigest()},
            datetime.fromtimestamp(1, timezone.utc),
        )
    ]

    assert sync.main() == 0

    container.upload_blob.assert_called_once()
    manifest_upload = container.upload_blob.call_args.kwargs
    assert manifest_upload["name"] == "_manifest.json"
    manifest = json.loads(manifest_upload["data"])
    assert manifest["files_unchanged"] == 1
    assert manifest["files_uploaded"] == 0


@pytest.mark.parametrize("metadata", [None, {}, {"sha256": "invalid"}])
def test_blobs_without_a_matching_hash_are_uploaded_once(
    sync_env: tuple[ModuleType, Path, MagicMock], metadata: dict[str, str] | None
) -> None:
    sync, root, container = sync_env
    data = b"synthetic legacy content"
    note = root / "note.md"
    note.write_bytes(data)
    os.utime(note, (1, 1))
    container.list_blobs.return_value = [
        _remote_blob(data, metadata, datetime.now(timezone.utc))
    ]

    assert sync.main() == 0
    upload = container.upload_blob.call_args_list[0].kwargs
    assert upload["name"] == "note.md"
    assert upload["metadata"] == {"sha256": hashlib.sha256(data).hexdigest()}

    container.list_blobs.return_value = [
        _remote_blob(data, upload["metadata"], datetime.now(timezone.utc))
    ]
    container.upload_blob.reset_mock()
    assert sync.main() == 0
    container.upload_blob.assert_called_once()
    assert container.upload_blob.call_args.kwargs["name"] == "_manifest.json"


@pytest.mark.parametrize("failure_type", [PermissionError, FileNotFoundError])
def test_unreadable_file_fails_without_publishing_a_fresh_manifest(
    sync_env: tuple[ModuleType, Path, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_type: type[OSError],
) -> None:
    sync, root, container = sync_env
    good, unreadable = root / "good.md", root / "private-title.md"
    good.write_bytes(b"synthetic good note")
    unreadable.write_bytes(b"synthetic inaccessible note")
    monkeypatch.setattr(sync, "_iter_markdown", lambda _: iter([good, unreadable]))
    original_read = Path.read_bytes

    def read_bytes(path: Path) -> bytes:
        if path == unreadable:
            raise failure_type(f"must not log {unreadable}")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    caplog.set_level(logging.INFO, logger=sync.log.name)

    assert sync.main() == 1

    assert [call.kwargs["name"] for call in container.upload_blob.call_args_list] == [
        "good.md"
    ]
    assert failure_type.__name__ in caplog.text
    assert "sync complete" not in caplog.text
    assert str(root) not in caplog.text
    assert "private-title" not in caplog.text
    assert "synthetic good note" not in caplog.text


def test_unreadable_subdirectory_fails_without_a_manifest(
    sync_env: tuple[ModuleType, Path, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sync, root, container = sync_env
    hidden = root / "private-directory"
    hidden.mkdir()
    (hidden / "note.md").write_bytes(b"synthetic inaccessible subtree")
    original_scandir = os.scandir

    def scandir(path: str | Path) -> AbstractContextManager[Iterator[os.DirEntry[str]]]:
        if Path(path) == hidden:
            raise PermissionError(f"must not log {hidden}")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)

    assert sync.main() == 1

    container.upload_blob.assert_not_called()
    assert "PermissionError" in caplog.text
    assert str(hidden) not in caplog.text


@pytest.mark.parametrize("operation", ["list", "content", "manifest"])
def test_storage_failure_is_nonzero_and_logs_only_the_error_type(
    sync_env: tuple[ModuleType, Path, MagicMock],
    caplog: pytest.LogCaptureFixture,
    operation: str,
) -> None:
    sync, root, container = sync_env
    (root / "note.md").write_bytes(b"synthetic note")
    failure = ServiceRequestError("sensitive-url-or-response-must-not-be-logged")
    if operation == "list":
        container.list_blobs.side_effect = failure
    elif operation == "content":
        container.upload_blob.side_effect = failure
    else:
        container.upload_blob.side_effect = [None, failure]

    assert sync.main() == 1

    assert "ServiceRequestError" in caplog.text
    assert str(failure) not in caplog.text
    assert "sync complete" not in caplog.text
    if operation != "manifest":
        assert "_manifest.json" not in [
            call.kwargs["name"] for call in container.upload_blob.call_args_list
        ]


def test_only_markdown_outside_fixed_exclusions_is_uploaded(
    sync_env: tuple[ModuleType, Path, MagicMock],
) -> None:
    sync, root, container = sync_env
    for name in sync.EXCLUDE_DIRS:
        directory = root / name
        directory.mkdir()
        (directory / "excluded.md").write_bytes(b"excluded synthetic content")
    (root / "notes").mkdir()
    (root / "notes" / "included.md").write_bytes(b"included synthetic content")
    (root / "export.json").write_bytes(b"{}")
    (root / ".gitignore").write_text("notes/\n", encoding="utf-8")

    assert sync.main() == 0

    assert [call.kwargs["name"] for call in container.upload_blob.call_args_list] == [
        "notes/included.md",
        "_manifest.json",
    ]


def test_success_logs_and_manifest_do_not_include_local_paths(
    sync_env: tuple[ModuleType, Path, MagicMock], caplog: pytest.LogCaptureFixture
) -> None:
    sync, root, container = sync_env
    (root / "private-title.md").write_bytes(b"synthetic personal content")
    caplog.set_level(logging.INFO, logger=sync.log.name)

    assert sync.main() == 0

    assert str(root) not in caplog.text
    assert "private-title" not in caplog.text
    assert "synthetic personal content" not in caplog.text
    manifest = json.loads(container.upload_blob.call_args.kwargs["data"])
    assert "source" not in manifest
    assert manifest["files_uploaded"] == 1
    assert manifest["bytes_uploaded"] == len(b"synthetic personal content")


def test_empty_root_publishes_zero_counts_without_deleting_blobs(
    sync_env: tuple[ModuleType, Path, MagicMock],
) -> None:
    sync, _, container = sync_env
    container.list_blobs.return_value = [
        _remote_blob(b"old synthetic note", None, datetime.now(timezone.utc))
    ]

    assert sync.main() == 0

    container.upload_blob.assert_called_once()
    manifest = json.loads(container.upload_blob.call_args.kwargs["data"])
    assert manifest["files_uploaded"] == manifest["files_unchanged"] == 0
    container.delete_blob.assert_not_called()
    container.delete_blobs.assert_not_called()


@pytest.mark.parametrize(
    "setting",
    ["ME_OS_ROOT", "AZURE_STORAGE_ACCOUNT", "AZURE_STORAGE_PERSONAL_OS_CONTAINER"],
)
def test_blank_configuration_fails_before_scanning_or_contacting_storage(
    sync_env: tuple[ModuleType, Path, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
) -> None:
    sync, _, _ = sync_env
    monkeypatch.setenv(setting, "")
    scanner = MagicMock(side_effect=AssertionError("must not scan"))
    monkeypatch.setattr(sync, "_iter_markdown", scanner)

    assert sync.main() == 2

    scanner.assert_not_called()
    sync.DefaultAzureCredential.assert_not_called()
    sync.BlobServiceClient.assert_not_called()


@pytest.mark.parametrize("root_state", ["missing", "file", "unreadable"])
def test_invalid_root_fails_without_logging_its_path(
    sync_env: tuple[ModuleType, Path, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    root_state: str,
) -> None:
    sync, root, _ = sync_env
    if root_state in {"missing", "file"}:
        root.rmdir()
        if root_state == "file":
            root.write_bytes(b"not a directory")
    else:
        original_stat = Path.stat

        def stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
            if path == root:
                raise PermissionError(f"must not log {root}")
            return original_stat(path, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(Path, "stat", stat)

    assert sync.main() == 2

    sync.BlobServiceClient.assert_not_called()
    assert str(root) not in caplog.text


def test_unreadable_configuration_reports_failure_without_a_path(
    sync_env: tuple[ModuleType, Path, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sync, _, _ = sync_env
    monkeypatch.setattr(
        sync,
        "load_dotenv",
        MagicMock(side_effect=PermissionError("private-config-path")),
    )

    assert sync.main() == 2

    sync.BlobServiceClient.assert_not_called()
    assert "PermissionError" in caplog.text
    assert "private-config-path" not in caplog.text


def test_symbolic_markdown_link_is_not_read_or_uploaded(
    sync_env: tuple[ModuleType, Path, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    sync, root, container = sync_env
    link = root / "linked.md"
    link.write_bytes(b"synthetic stand-in for a linked file")
    original_is_symlink = Path.is_symlink

    def is_symlink(path: Path) -> bool:
        return path == link or original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", is_symlink)
    reader = MagicMock(side_effect=AssertionError("must not read a symbolic link"))
    monkeypatch.setattr(Path, "read_bytes", reader)

    assert sync.main() == 0

    reader.assert_not_called()
    container.upload_blob.assert_called_once()
    assert container.upload_blob.call_args.kwargs["name"] == "_manifest.json"


@pytest.mark.parametrize(
    "sdk_logger_name",
    [
        "azure.identity",
        "azure.identity._internal.privacy_test",
        "azure.core.pipeline.policies.http_logging_policy",
    ],
)
def test_sdk_warnings_cannot_bypass_the_redacted_error_logging(
    sync_env: tuple[ModuleType, Path, MagicMock],
    caplog: pytest.LogCaptureFixture,
    sdk_logger_name: str,
) -> None:
    sync, _, _ = sync_env
    importlib.import_module("function_app")
    sdk_logger = logging.getLogger(sdk_logger_name)
    sdk_logger.setLevel(logging.WARNING)

    sync._setup_logging()

    sdk_logger.warning("synthetic-private-credential-path")
    sdk_logger.error("synthetic-private-credential-path")
    assert "synthetic-private-credential-path" not in caplog.text


def test_manifest_inventory_contains_uploaded_and_unchanged_files_not_retained_blobs(
    sync_env: tuple[ModuleType, Path, MagicMock],
) -> None:
    sync, root, container = sync_env
    data = b"synthetic unchanged content"
    (root / "note.md").write_bytes(data)
    (root / "new.md").write_bytes(b"synthetic new content")
    container.list_blobs.return_value = [
        _remote_blob(data, {"sha256": hashlib.sha256(data).hexdigest()}, datetime.now(timezone.utc)),
        SimpleNamespace(name="deleted.md", size=3, metadata={}),
    ]

    assert sync.main() == 0

    manifest = json.loads(container.upload_blob.call_args.kwargs["data"])
    assert manifest["source_files"] == ["new.md", "note.md"]
    assert manifest["files_uploaded"] == manifest["files_unchanged"] == 1
    container.delete_blob.assert_not_called()
    container.delete_blobs.assert_not_called()


def test_manifest_inventory_is_explicitly_empty_for_an_empty_root(
    sync_env: tuple[ModuleType, Path, MagicMock],
) -> None:
    sync, _, container = sync_env

    assert sync.main() == 0

    assert json.loads(container.upload_blob.call_args.kwargs["data"])["source_files"] == []
