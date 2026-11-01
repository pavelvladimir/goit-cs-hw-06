"""Quiesced Compose backups and non-destructive restores into new databases."""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
# One local namespace per OS user, independent of checkout, cwd and backup path.
LOCK_ROOT = Path("/tmp") / f"goit-cs-hw-06-archive-{os.getuid()}"


class ArchiveError(RuntimeError):
    """A fixed diagnostic; subprocess output may contain sensitive data."""


def open_lock(project: str) -> int:
    """Keep a stable inode; never unlink a lock file, even after releasing it."""
    LOCK_ROOT.mkdir(mode=0o700, exist_ok=True)
    directory = os.open(LOCK_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor = None
    try:
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ArchiveError(
                "Maintenance lock directory must be private and owned by you"
            )
        name = hashlib.sha256(project.encode()).hexdigest() + ".lock"
        descriptor = os.open(
            name,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=directory,
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise ArchiveError(
                "Maintenance lock must be a private regular file owned by you"
            )
        return descriptor
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise
    finally:
        os.close(directory)


@contextmanager
def project_lock(project: str) -> Iterator[None]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project):
        raise ArchiveError("Invalid Compose project name")
    try:
        descriptor = open_lock(project)
    except OSError:
        raise ArchiveError("Maintenance lock is unavailable") from None
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                raise ArchiveError(
                    "Another backup or restore is already running "
                    "for this Compose project"
                ) from None
            raise ArchiveError("Maintenance lock is unavailable") from None
        yield
    finally:
        # close also releases flock on exceptions; the kernel does so on exit.
        os.close(descriptor)


class DatabaseArchive:
    def __init__(self, command: list[str], environment: dict[str, str]) -> None:
        self.command = command
        self.environment = environment

    def operation_lock(self) -> AbstractContextManager[None]:
        # All callers pass an explicit -p, which overrides Compose environment.
        positions = [i for i, value in enumerate(self.command) if value == "-p"]
        if len(positions) != 1 or positions[0] + 1 >= len(self.command):
            raise ArchiveError("Maintenance requires one explicit Compose -p project")
        return project_lock(self.command[positions[0] + 1])

    def run(
        self,
        *args: str,
        source: BinaryIO | None = None,
        destination: BinaryIO | None = None,
    ) -> bytes:
        try:
            result = subprocess.run(
                [*self.command, *args],
                env=self.environment,
                stdin=source,
                stdout=destination if destination is not None else subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=180,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ArchiveError("Database operation could not complete") from None
        if result.returncode:
            raise ArchiveError("Database operation failed; original data preserved")
        return result.stdout or b""

    def tool(self, archive: str, database: str | None = None) -> None:
        arguments = (
            ["mongodump", "--db=messages", "--archive=" + archive]
            if database is None
            else [
                "mongorestore",
                "--archive=" + archive,
                "--nsInclude=messages.*",
                "--nsFrom=messages.*",
                "--nsTo=" + database + ".*",
                "--stopOnError",
            ]
        )
        # mongosh's Node runtime writes a private config; secrets never enter argv.
        script = r"""
const fs = require('fs');
const password = fs.readFileSync('/run/secrets/mongo_admin_password', 'utf8')
  .replace(/[\r\n]+$/, '');
const temporary = fs.mkdtempSync('/tmp/hw06-db-config-');
try {
  const config = temporary + '/config.json';
  fs.writeFileSync(config, JSON.stringify({password}), {mode: 0o600});
  const database = DATABASE;
  if (database !== null) {
    const admin = db.getSiblingDB('admin');
    admin.auth('root', password);
    const names = admin.runCommand({listDatabases: 1, nameOnly: true});
    if (names.ok !== 1 || names.databases.some(item => item.name === database)) {
      throw new Error('Restore target exists');
    }
  }
  const [tool, ...args] = ARGUMENTS;
  const result = require('child_process').spawnSync(tool, [
    '--config=' + config, '--host=127.0.0.1', '--username=root',
    '--authenticationDatabase=admin', ...args
  ], {stdio: 'ignore', timeout: 120000});
  if (result.status !== 0) throw new Error('Database tool failed');
} finally {
  fs.rmSync(temporary, {recursive: true, force: true});
}
"""
        script = script.replace("DATABASE", json.dumps(database)).replace(
            "ARGUMENTS", json.dumps(arguments)
        )
        self.run(
            "exec", "-T", "mongodb", "mongosh", "--quiet", "--norc", "--eval", script
        )

    def backup(self, directory: Path) -> None:
        with self.operation_lock():
            self._backup(directory)

    def _backup(self, directory: Path) -> None:
        directory.mkdir(mode=0o700, parents=False, exist_ok=False)
        remote = "/tmp/hw06-archive-" + uuid4().hex
        running = False
        completed = False
        try:
            running = bool(self.run("ps", "--status", "running", "-q", "app").strip())
            try:
                if running:
                    self.run("stop", "app")
                # The shell only receives a generated path, never user input.
                self.run(
                    "exec", "-T", "mongodb", "sh", "-c", f"umask 077; touch {remote}"
                )
                self.tool(remote)
                archive = directory / "archive.bson"
                with archive.open("xb") as output:
                    archive.chmod(0o600)
                    self.run("exec", "-T", "mongodb", "cat", remote, destination=output)
                manifest = {
                    "format": 1,
                    "database": "messages",
                    "created_at": datetime.now(UTC).isoformat(),
                    "sha256": checksum(archive),
                }
                metadata = directory / "manifest.json"
                metadata.write_text(json.dumps(manifest, indent=2) + "\n")
                metadata.chmod(0o600)
                # make backup may have just created backups/ in the project root.
                for durable in (archive, metadata, directory, *directory.parents[:2]):
                    fsync_path(durable)
                completed = True
            finally:
                try:
                    self.run("exec", "-T", "mongodb", "rm", "-f", remote)
                finally:
                    if running:
                        # Older Compose v2 supports --wait on up, but not start.
                        # Resume the existing app without changing its container.
                        self.run(
                            "up",
                            "--no-recreate",
                            "--no-build",
                            "--no-deps",
                            "--wait",
                            "--wait-timeout",
                            "60",
                            "app",
                        )
        except BaseException:
            if not completed:
                shutil.rmtree(directory)
            raise

    def restore(self, directory: Path, database: str) -> None:
        with self.operation_lock():
            self._restore(directory, database)

    def _restore(self, directory: Path, database: str) -> None:
        if not re.fullmatch(r"restore_[a-z0-9_]{1,40}", database):
            raise ArchiveError("Target must be a new database named restore_<name>")
        archive = directory / "archive.bson"
        metadata = directory / "manifest.json"
        if directory.is_symlink() or archive.is_symlink() or metadata.is_symlink():
            raise ArchiveError("Backup paths must not be symlinks")
        try:
            manifest = json.loads(metadata.read_text())
            valid = (
                isinstance(manifest, dict)
                and manifest.get("format") == 1
                and manifest.get("database") == "messages"
                and manifest.get("sha256") == checksum(archive)
            )
        except (OSError, ValueError):
            raise ArchiveError("Backup or manifest is unreadable") from None
        if not valid:
            raise ArchiveError("Backup checksum or format does not match")
        remote = "/tmp/hw06-archive-" + uuid4().hex
        try:
            with archive.open("rb") as source:
                self.run(
                    "exec",
                    "-T",
                    "mongodb",
                    "sh",
                    "-c",
                    f"umask 077; cat > {remote}",
                    source=source,
                )
            self.tool(remote, database)
        finally:
            self.run("exec", "-T", "mongodb", "rm", "-f", remote)


def fsync_path(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
        # macOS fsync alone does not flush the drive's write cache.
        full_sync = getattr(fcntl, "F_FULLFSYNC", None)
        if full_sync is not None:
            fcntl.fcntl(descriptor, full_sync)
    finally:
        os.close(descriptor)


def checksum(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("backup", "restore"))
    parser.add_argument("directory", type=Path)
    parser.add_argument(
        "--database", help="New restore_<name> database; never replaces existing data"
    )
    parser.add_argument("--project-name", default="goit-cs-hw-06")
    arguments = parser.parse_args()
    if arguments.operation == "restore" and not arguments.database:
        parser.error("restore requires --database restore_<name>")
    service = DatabaseArchive(
        [
            "docker",
            "compose",
            "--project-directory",
            str(ROOT),
            "-f",
            str(ROOT / "docker-compose.yaml"),
            "-p",
            arguments.project_name,
        ],
        dict(os.environ),
    )
    try:
        if arguments.operation == "backup":
            service.backup(arguments.directory)
        else:
            service.restore(arguments.directory, arguments.database)
    except (ArchiveError, OSError) as error:
        print(
            str(error)
            if isinstance(error, ArchiveError)
            else "Backup path is unavailable"
        )
        return 1
    print(
        "Backup created"
        if arguments.operation == "backup"
        else "Restored into " + arguments.database
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
