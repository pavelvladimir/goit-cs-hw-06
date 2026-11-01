"""Real processes exercise mutual exclusion, cleanup and kernel lock release."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import multiprocessing
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import BinaryIO
from unittest.mock import MagicMock

import pytest

from scripts import database_archive as module
from scripts.database_archive import (
    ArchiveError,
    DatabaseArchive,
    checksum,
    project_lock,
)

PROJECT = "archive-test"
COMMAND = ["docker", "compose", "-p", PROJECT]


@pytest.fixture(autouse=True)
def private_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "LOCK_ROOT", tmp_path / "locks")


def make_backup(directory: Path) -> None:
    directory.mkdir()
    data = directory / "archive.bson"
    data.write_bytes(b"test archive")
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "format": 1,
                "database": "messages",
                "sha256": checksum(data),
            }
        )
    )


def archive_worker(
    locks: Path,
    operation: str,
    stage: str,
    directory: Path,
    trace: Path,
    control: Connection,
) -> None:
    module.LOCK_ROOT = locks

    def pause() -> None:
        control.send("held")
        if control.recv() == "error":
            raise ArchiveError("simulated failure")

    class ObservedArchive(DatabaseArchive):
        def run(
            self,
            *args: str,
            source: BinaryIO | None = None,
            destination: BinaryIO | None = None,
        ) -> bytes:
            with trace.open("a") as output:
                output.write(json.dumps(args) + "\n")
            if (stage == "restart" and args[0] == "up") or (
                stage == "cleanup" and "rm" in args
            ):
                pause()
            if destination is not None:
                destination.write(b"archive")
            return b"running" if args[0] == "ps" else b""

        def tool(self, archive: str, database: str | None = None) -> None:
            if stage == "tool":
                pause()

    archive = ObservedArchive(COMMAND, {})
    try:
        if operation == "backup":
            archive.backup(directory)
        else:
            archive.restore(directory, "restore_test")
    except BaseException as error:
        control.send(type(error).__name__)
    else:
        control.send("done")
    finally:
        control.close()


@contextmanager
def holder(
    tmp_path: Path,
    operation: str = "backup",
    stage: str = "tool",
) -> Iterator[tuple[BaseProcess, Connection]]:
    directory = tmp_path / "first"
    if operation == "restore":
        make_backup(directory)
    context = multiprocessing.get_context("spawn")
    control, child = context.Pipe()
    process = context.Process(
        target=archive_worker,
        args=(
            module.LOCK_ROOT,
            operation,
            stage,
            directory,
            tmp_path / "trace",
            child,
        ),
    )
    process.start()
    child.close()
    try:
        assert control.poll(8), "Archive worker did not reach the guarded operation"
        assert control.recv() == "held"
        yield process, control
    finally:
        if process.is_alive():
            try:
                control.send("release")
            except (BrokenPipeError, EOFError):
                pass
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        control.close()
        assert not process.is_alive()
        process.close()


@pytest.mark.parametrize("first", ["backup", "restore"])
@pytest.mark.parametrize("second", ["backup", "restore"])
def test_processes_exclude_every_pair_before_docker_or_backup_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first: str,
    second: str,
) -> None:
    archive = DatabaseArchive(COMMAND, {"COMPOSE_PROJECT_NAME": "ignored-env-name"})
    run = MagicMock(side_effect=AssertionError("Docker must not be called"))
    monkeypatch.setattr(archive, "run", run)
    with holder(tmp_path, first):
        path = next(module.LOCK_ROOT.glob("*.lock"))
        inode = path.stat().st_ino
        assert module.LOCK_ROOT.stat().st_mode & 0o777 == 0o700
        assert path.stat().st_mode & 0o777 == 0o600
        with pytest.raises(ArchiveError, match="already running"):
            if second == "backup":
                archive.backup(tmp_path / "blocked")
            else:
                archive.restore(tmp_path / "blocked", "restore_new")
        run.assert_not_called()
        assert not (tmp_path / "blocked").exists()
        with project_lock("different-project"):
            pass
    with archive.operation_lock():
        assert path.stat().st_ino == inode  # No unlink/recreate race after release.


@pytest.mark.parametrize(
    "operation,stage",
    [
        ("backup", "restart"),
        ("backup", "cleanup"),
        ("restore", "cleanup"),
    ],
)
def test_lock_remains_held_through_cleanup_and_app_restart(
    tmp_path: Path,
    operation: str,
    stage: str,
) -> None:
    with holder(tmp_path, operation, stage):
        with pytest.raises(ArchiveError, match="already running"):
            with project_lock(PROJECT):
                pytest.fail("Maintenance lock released too early")
    with project_lock(PROJECT):
        pass


@pytest.mark.parametrize("interruption", ["exception", "sigint", "sigterm", "sigkill"])
def test_error_and_process_termination_release_without_deleting_lock(
    tmp_path: Path,
    interruption: str,
) -> None:
    with holder(tmp_path) as (process, control):
        path = next(module.LOCK_ROOT.glob("*.lock"))
        inode = path.stat().st_ino
        if interruption == "exception":
            control.send("error")
            assert control.poll(5) and control.recv() == "ArchiveError"
        else:
            assert process.pid is not None
            os.kill(
                process.pid,
                {
                    "sigint": signal.SIGINT,
                    "sigterm": signal.SIGTERM,
                    "sigkill": signal.SIGKILL,
                }[interruption],
            )
            if interruption == "sigint":
                assert control.poll(5) and control.recv() == "KeyboardInterrupt"
        process.join(timeout=5)
        assert not process.is_alive()
        with project_lock(PROJECT):
            assert path.stat().st_ino == inode
        if interruption in {"exception", "sigint"}:
            calls = [
                json.loads(line)
                for line in (tmp_path / "trace").read_text().splitlines()
            ]
            assert calls[-1][0] == "up"
            assert not (tmp_path / "first").exists()


def test_busy_cli_from_another_directory_has_safe_nonzero_exit(tmp_path: Path) -> None:
    elsewhere = tmp_path / "another-checkout"
    elsewhere.mkdir()
    code = (
        "import sys; from pathlib import Path; "
        "from scripts import database_archive as m; "
        "m.LOCK_ROOT = Path(sys.argv.pop(1)); sys.exit(m.main())"
    )
    with holder(tmp_path):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                str(module.LOCK_ROOT),
                "backup",
                str(tmp_path / "blocked"),
                "--project-name",
                PROJECT,
            ],
            cwd=elsewhere,
            env={**os.environ, "PYTHONPATH": str(module.ROOT)},
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    assert result.returncode == 1
    assert "Another backup or restore is already running" in result.stdout
    assert not result.stderr
    assert not (tmp_path / "blocked").exists()


@pytest.mark.parametrize("command", [[], ["-p"], ["-p", "one", "-p", "two"]])
def test_ambiguous_project_is_rejected_before_side_effects(
    tmp_path: Path,
    command: list[str],
) -> None:
    with pytest.raises(ArchiveError, match="explicit Compose"):
        DatabaseArchive(command, {}).backup(tmp_path / "blocked")
    assert not (tmp_path / "blocked").exists()


@pytest.mark.parametrize("project", ["", "Uppercase", "../other", "-invalid"])
def test_invalid_project_name_is_rejected(project: str) -> None:
    with pytest.raises(ArchiveError, match="Invalid Compose"):
        with project_lock(project):
            pytest.fail("Invalid name accepted")


@pytest.mark.parametrize(
    "kind",
    ["directory-mode", "directory-link", "file-mode", "file-link", "hardlink", "fifo"],
)
def test_unsafe_lock_paths_fail_closed(tmp_path: Path, kind: str) -> None:
    if kind == "directory-link":
        module.LOCK_ROOT.symlink_to(tmp_path, target_is_directory=True)
    else:
        module.LOCK_ROOT.mkdir(mode=0o700)
        if kind == "directory-mode":
            module.LOCK_ROOT.chmod(0o755)
        else:
            name = hashlib.sha256(PROJECT.encode()).hexdigest() + ".lock"
            path = module.LOCK_ROOT / name
            target = tmp_path / "private-file"
            target.write_text("must stay unchanged")
            target.chmod(0o600)
            if kind == "file-link":
                path.symlink_to(target)
            elif kind == "hardlink":
                path.hardlink_to(target)
            elif kind == "fifo":
                os.mkfifo(path, mode=0o600)
            else:
                path.touch(mode=0o644)
                path.chmod(0o644)
    with pytest.raises(ArchiveError):
        with project_lock(PROJECT):
            pytest.fail("Unsafe lock path accepted")
    if (tmp_path / "private-file").exists():
        assert (tmp_path / "private-file").read_text() == "must stay unchanged"


@pytest.mark.parametrize("number", [errno.EACCES, errno.EAGAIN, errno.EIO])
def test_lock_errors_are_safe_and_close_the_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    number: int,
) -> None:
    descriptor = None

    def fail(fd: int, operation: int) -> None:
        nonlocal descriptor
        descriptor = fd
        raise OSError(number, "private-system-detail")

    with monkeypatch.context() as patch:
        patch.setattr(fcntl, "flock", fail)
        with pytest.raises(ArchiveError) as error:
            with project_lock(PROJECT):
                pytest.fail("Failed lock accepted")
    assert "private" not in str(error.value)
    assert descriptor is not None
    with pytest.raises(OSError):
        os.fstat(descriptor)
    with project_lock(PROJECT):
        pass
