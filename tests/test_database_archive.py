"""Backup failure paths must preserve files, application state and credentials."""

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import BinaryIO
from unittest.mock import MagicMock

import pytest

from scripts import database_archive as module
from scripts.database_archive import ArchiveError, DatabaseArchive, checksum


@pytest.fixture(autouse=True)
def isolated_archive_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "LOCK_ROOT", tmp_path / "locks")


@pytest.mark.parametrize("running", [True, False])
@pytest.mark.parametrize(
    "failure",
    [
        "none",
        "stop",
        "dump",
        "copy",
        "cleanup",
        "restart",
        "archive.bson",
        "manifest.json",
        "backup",
        "parent",
    ],
)
def test_backup_restores_prior_app_state_and_never_leaves_partial_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    running: bool,
    failure: str,
) -> None:
    archive = DatabaseArchive(["docker", "compose", "-p", "archive-test"], {})
    calls: list[tuple[str, ...]] = []
    synced: list[Path] = []
    original_sync = module.fsync_path

    def sync(path: Path) -> None:
        synced.append(path)
        if path.name == failure or (path == tmp_path and failure == "parent"):
            raise OSError("sync failed")
        original_sync(path)

    def run(
        *args: str,
        source: BinaryIO | None = None,
        destination: BinaryIO | None = None,
    ) -> bytes:
        calls.append(args)
        phase = {"stop": "stop", "up": "restart", "cat": "copy", "rm": "cleanup"}
        operation = (
            args[0] if args[0] in {"stop", "up"} else args[3] if len(args) > 3 else ""
        )
        if phase.get(operation) == failure:
            raise ArchiveError("simulated operation failure")
        if destination:
            destination.write(b"test archive data")
        return b"container" if args[0] == "ps" and running else b""

    monkeypatch.setattr(archive, "run", run)
    monkeypatch.setattr(module, "fsync_path", sync)
    monkeypatch.setattr(
        archive,
        "tool",
        MagicMock(
            side_effect=ArchiveError("dump failed") if failure == "dump" else None
        ),
    )
    directory = tmp_path / "backup"
    failed = failure not in {"none"} and (running or failure not in {"stop", "restart"})
    if failed:
        with pytest.raises((ArchiveError, OSError)):
            archive.backup(directory)
    else:
        archive.backup(directory)
    assert any(args[0] == "up" for args in calls) == running
    assert any(len(args) > 3 and args[3] == "rm" for args in calls)
    complete = not failed or failure in {"cleanup", "restart"}
    assert directory.exists() == complete
    if complete:
        assert synced == [
            directory / "archive.bson",
            directory / "manifest.json",
            directory,
            tmp_path,
        ]
        assert directory.stat().st_mode & 0o777 == 0o700
        assert (directory / "archive.bson").stat().st_mode & 0o777 == 0o600
        manifest = json.loads((directory / "manifest.json").read_text())
        assert manifest["sha256"] == checksum(directory / "archive.bson")


@pytest.mark.parametrize("full_sync", [False, True])
def test_sync_failure_propagates_and_closes_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, full_sync: bool
) -> None:
    path = tmp_path / "archive.bson"
    path.write_bytes(b"backup")
    descriptors: list[int] = []

    def fail(descriptor: int, command: int | None = None) -> None:
        descriptors.append(descriptor)
        raise OSError("sync failed")

    if full_sync:
        monkeypatch.setattr(fcntl, "F_FULLFSYNC", 51, raising=False)
        monkeypatch.setattr(fcntl, "fcntl", fail)
    else:
        monkeypatch.delattr(fcntl, "F_FULLFSYNC", raising=False)
        monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError, match="sync failed"):
        module.fsync_path(path)
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert path.read_bytes() == b"backup"


def test_existing_backup_is_never_replaced(tmp_path: Path) -> None:
    with pytest.raises(FileExistsError):
        DatabaseArchive(["docker", "compose", "-p", "archive-test"], {}).backup(
            tmp_path
        )


@pytest.mark.parametrize("kind", ["target", "symlink", "missing", "checksum", "format"])
def test_invalid_restore_does_not_touch_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    archive = DatabaseArchive(["docker", "compose", "-p", "archive-test"], {})
    run = MagicMock()
    monkeypatch.setattr(archive, "run", run)
    data = tmp_path / "archive.bson"
    data.write_bytes(b"backup")
    manifest = {"format": 1, "database": "messages", "sha256": checksum(data)}
    if kind == "checksum":
        data.write_bytes(b"corrupted")
    if kind == "format":
        manifest["format"] = 2
    if kind != "missing":
        (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    if kind == "symlink":
        data.unlink()
        data.symlink_to(tmp_path / "manifest.json")
    with pytest.raises(ArchiveError):
        archive.restore(tmp_path, "messages" if kind == "target" else "restore_test")
    run.assert_not_called()


@pytest.mark.parametrize("failed", [False, True])
def test_restore_always_cleans_temporary_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed: bool,
) -> None:
    data = tmp_path / "archive.bson"
    data.write_bytes(b"backup")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "format": 1,
                "database": "messages",
                "sha256": checksum(data),
            }
        )
    )
    archive = DatabaseArchive(["docker", "compose", "-p", "archive-test"], {})
    calls: list[tuple[str, ...]] = []

    def run(*args: str, **kwargs: object) -> bytes:
        calls.append(args)
        if "mongosh" in args and failed:
            raise ArchiveError("failed")
        return b""

    monkeypatch.setattr(archive, "run", run)
    if failed:
        with pytest.raises(ArchiveError):
            archive.restore(tmp_path, "restore_test")
    else:
        archive.restore(tmp_path, "restore_test")
    assert calls[-1][3] == "rm"
    assert data.read_bytes() == b"backup"


@pytest.mark.parametrize("failure", ["exit", "timeout", "os", "none"])
def test_subprocess_diagnostics_do_not_leak_secrets(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    runner = MagicMock(return_value=subprocess.CompletedProcess([], 0, b"ok", b""))
    if failure == "exit":
        runner.return_value = subprocess.CompletedProcess([], 1, b"secret", b"secret")
    if failure == "timeout":
        runner.side_effect = subprocess.TimeoutExpired("secret", 1)
    if failure == "os":
        runner.side_effect = OSError("secret")
    monkeypatch.setattr(subprocess, "run", runner)
    if failure == "none":
        assert (
            DatabaseArchive(["docker", "compose", "-p", "archive-test"], {}).run("ps")
            == b"ok"
        )
    else:
        with pytest.raises(ArchiveError) as error:
            DatabaseArchive(["docker", "compose", "-p", "archive-test"], {}).run("ps")
        assert "secret" not in str(error.value)


@pytest.mark.parametrize("operation", ["backup", "restore", "error", "path"])
def test_cli_routes_operations_and_reports_safe_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operation: str,
) -> None:
    service = MagicMock()
    if operation in {"error", "path"}:
        service.backup.side_effect = (
            ArchiveError("safe error") if operation == "error" else OSError("secret")
        )
    monkeypatch.setattr(module, "DatabaseArchive", MagicMock(return_value=service))
    args = ["archive", "restore" if operation == "restore" else "backup", str(tmp_path)]
    if operation == "restore":
        args.extend(["--database", "restore_test"])
    monkeypatch.setattr(sys, "argv", args)
    assert module.main() == (1 if operation in {"error", "path"} else 0)
    assert "secret" not in capsys.readouterr().out


def test_restore_cli_requires_explicit_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["archive", "restore", "backup"])
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
