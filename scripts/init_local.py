"""Create missing local configuration and secrets without replacing user files."""

import os
import secrets
import stat
import sys
import tempfile
from io import StringIO
from pathlib import Path

from dotenv import dotenv_values

SECRET_PATHS = {
    "MONGO_PASSWORD_PATH": "./secrets/mongo_password",
    "MONGO_ADMIN_PASSWORD_PATH": "./secrets/mongo_admin_password",
}


class InitError(Exception):
    """An actionable setup error that contains no password values."""


def check_file(path: Path, mode: int) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode):
        raise InitError(f"{path} must be a regular file, not a symlink or directory")
    if stat.S_IMODE(info.st_mode) != mode:
        raise InitError(
            f"{path} must have mode {mode:04o}; existing file left unchanged"
        )
    return True


def check_directory(path: Path) -> None:
    if path.is_symlink():
        raise InitError(f"{path} must be a private directory, not a symlink")
    if path.exists() and (
        not path.is_dir() or stat.S_IMODE(path.stat().st_mode) != 0o700
    ):
        raise InitError(f"{path} must be a directory with mode 0700; left unchanged")


def check_secret_parents(path: Path, directory: Path) -> None:
    # The project itself is trusted; reject symlinks in configured descendants
    # and in custom absolute paths before creating any files.
    for parent in path.parents:
        if parent == directory:
            break
        if parent.is_symlink():
            raise InitError(f"{parent} must not be a symlink")


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeError:
        raise InitError(f"{path} must contain UTF-8 text; left unchanged") from None


def read_password(path: Path) -> str:
    value = read_text(path).rstrip("\r\n")
    if not value.strip() or any(char in value for char in "\r\n\x00"):
        raise InitError(f"{path} must contain a nonempty, single-line password")
    return value


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create_file(path: Path, content: str, mode: int) -> bool:
    """Publish a fully written file atomically; a concurrent init cannot clobber it."""
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".init-"
    ) as temporary:
        temporary.write(content)
        temporary.flush()
        os.fchmod(temporary.fileno(), mode)
        os.fsync(temporary.fileno())
        created = True
        try:
            os.link(temporary.name, path)
        except FileExistsError:
            check_file(path, mode)
            created = False
        # A concurrent initializer may have published the name before its sync failed.
        sync_directory(path.parent)
    return created


def initialize(directory: Path) -> list[Path]:
    env_file = directory / ".env"
    env_exists = check_file(env_file, 0o600)
    if env_exists:
        sync_directory(env_file.parent)
    env_content = read_text(env_file if env_exists else directory / ".env.example")
    # Parse quoted values/comments without executing shell code. Compose supports
    # more interpolation forms than dotenv; require literal secret-file paths.
    values = dotenv_values(stream=StringIO(env_content), interpolate=False)
    paths = []
    passwords: list[str | None] = []
    for key, default in SECRET_PATHS.items():
        value = os.environ.get(key, values.get(key)) or default
        if "$" in value or "\n" in value or "\r" in value or "\x00" in value:
            raise InitError(f"{key} must be a literal path, without variable expansion")
        path = directory / value
        check_secret_parents(path, directory)
        check_directory(path.parent)
        if check_file(path, 0o444):
            sync_directory(path.parent)
            passwords.append(read_password(path))
        else:
            passwords.append(None)
        paths.append(path)
    if paths[0].resolve() == paths[1].resolve():
        raise InitError(
            "Application and administrator secrets must use different files"
        )
    if passwords[0] is not None and passwords[0] == passwords[1]:
        raise InitError("Application and administrator passwords must be different")

    created = []
    previous_umask = os.umask(0o077)
    try:
        # Every missing parent is private, including intermediate directories.
        for path in paths:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            check_directory(path.parent)
        if not env_exists and create_file(env_file, env_content, 0o600):
            created.append(env_file)
        for path, password in zip(paths, passwords, strict=True):
            if password is None and create_file(
                path, secrets.token_hex(32) + "\n", 0o444
            ):
                created.append(path)
    finally:
        os.umask(previous_umask)
    return created


def main() -> int:
    try:
        created = initialize(Path.cwd())
    except InitError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(
            f"Error: cannot initialize {error.filename}: {error.strerror}",
            file=sys.stderr,
        )
        return 1
    if created:
        for path in created:
            label = (
                path.relative_to(Path.cwd())
                if path.is_relative_to(Path.cwd())
                else path
            )
            print(f"Created: {label}")
    else:
        print("Local configuration is ready; existing files left unchanged.")
    print("Next: docker compose up --build -d --wait")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
