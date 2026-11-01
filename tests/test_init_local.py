"""Bootstrap must never replace configuration or existing database credentials."""

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import init_local as module
from scripts.init_local import SECRET_PATHS, InitError, initialize

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "init_local.py"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "project with spaces"
    directory.mkdir()
    shutil.copyfile(ROOT / ".env.example", directory / ".env.example")
    for key in SECRET_PATHS:
        monkeypatch.delenv(key, raising=False)
    return directory


def snapshot(directory: Path) -> dict[str, tuple[bytes, int, int, int]]:
    return {
        str(path.relative_to(directory)): (
            path.read_bytes(),
            stat.S_IMODE(path.stat().st_mode),
            path.stat().st_mtime_ns,
            path.stat().st_ino,
        )
        for path in directory.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("failure", ["none", "file", "directory"])
def test_create_file_sync_failure_never_publishes_partial_or_replaces_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path = tmp_path / "password"
    descriptors: list[int] = []
    synced: list[str] = []
    original_sync = os.fsync

    def sync(descriptor: int) -> None:
        info = os.fstat(descriptor)
        phase = "directory" if stat.S_ISDIR(info.st_mode) else "file"
        descriptors.append(descriptor)
        synced.append(phase)
        assert path.exists() == (phase == "directory")
        if phase == "file":
            assert os.pread(descriptor, 64, 0) == b"test-password\n"
            assert stat.S_IMODE(info.st_mode) == 0o444
        else:
            assert path.read_bytes() == b"test-password\n"
        if phase == failure:
            raise OSError("sync failed")
        original_sync(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", sync)
        if failure == "none":
            assert module.create_file(path, "test-password\n", 0o444)
        else:
            with pytest.raises(OSError, match="sync failed"):
                module.create_file(path, "test-password\n", 0o444)
    assert synced == (["file"] if failure == "file" else ["file", "directory"])
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert not list(tmp_path.glob(".init-*"))
    assert path.exists() == (failure != "file")
    if path.exists():
        before = snapshot(tmp_path)
        assert not module.create_file(path, "replacement\n", 0o444)
        assert snapshot(tmp_path) == before


def test_fresh_init_creates_private_configuration_and_random_passwords(
    project: Path,
) -> None:
    created = initialize(project)
    assert len(created) == 3
    assert (project / ".env").read_bytes() == (project / ".env.example").read_bytes()
    assert stat.S_IMODE((project / ".env").stat().st_mode) == 0o600
    assert stat.S_IMODE((project / "secrets").stat().st_mode) == 0o700
    passwords = []
    for name in SECRET_PATHS.values():
        path = project / name
        assert stat.S_IMODE(path.stat().st_mode) == 0o444
        password = path.read_text()
        assert re.fullmatch(r"[0-9a-f]{64}\n", password)
        passwords.append(password)
    assert passwords[0] != passwords[1]
    assert not list(project.rglob(".init-*"))


def test_repeated_init_preserves_custom_settings_contents_modes_and_mtimes(
    project: Path,
) -> None:
    initialize(project)
    env_file = project / ".env"
    env_file.write_text(env_file.read_text().replace("APP_PORT=3000", "APP_PORT=8123"))
    password = project / "secrets/mongo_password"
    password.chmod(0o600)
    password.write_text("existing-password-@:/?#%$&\n")
    password.chmod(0o444)
    before = snapshot(project)
    assert initialize(project) == []
    assert initialize(project) == []
    assert snapshot(project) == before


def test_partial_init_creates_only_missing_secret(project: Path) -> None:
    initialize(project)
    missing = project / "secrets/mongo_admin_password"
    missing.unlink()
    before = snapshot(project)
    assert initialize(project) == [missing]
    assert {key: snapshot(project)[key] for key in before} == before
    assert missing.read_text() != (project / "secrets/mongo_password").read_text()


def test_quoted_custom_paths_and_missing_parent_directories(project: Path) -> None:
    env_file = project / ".env"
    absolute = project / "private admin" / "password"
    env_file.write_text(
        "APP_PORT=8123\n"
        "MONGO_PASSWORD_PATH='private nested/app secrets/password' # app\n"
        f'MONGO_ADMIN_PASSWORD_PATH="{absolute}"\n'
    )
    env_file.chmod(0o600)
    before = env_file.read_bytes()
    created = initialize(project)
    assert created == [project / "private nested/app secrets/password", absolute]
    assert env_file.read_bytes() == before
    assert not (project / "secrets").exists()
    for directory in [path for path in project.rglob("*") if path.is_dir()]:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_environment_overrides_select_compose_secret_paths(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialize(project)
    before = snapshot(project)
    monkeypatch.setenv("MONGO_PASSWORD_PATH", "overrides/app")
    monkeypatch.setenv("MONGO_ADMIN_PASSWORD_PATH", "overrides/admin")
    assert initialize(project) == [
        project / "overrides/app",
        project / "overrides/admin",
    ]
    assert {key: snapshot(project)[key] for key in before} == before


@pytest.mark.parametrize(
    ("target", "mode", "required"),
    [
        (".env", 0o644, "0600"),
        ("secrets", 0o755, "0700"),
        ("secrets/mongo_password", 0o600, "0444"),
    ],
)
def test_insecure_permissions_are_reported_without_changing_files(
    project: Path, target: str, mode: int, required: str
) -> None:
    initialize(project)
    (project / target).chmod(mode)
    before = snapshot(project)
    with pytest.raises(InitError, match=required):
        initialize(project)
    assert snapshot(project) == before
    assert stat.S_IMODE((project / target).stat().st_mode) == mode


@pytest.mark.parametrize("password", ["", " \n", "one\ntwo\n", "one\x00two"])
def test_invalid_existing_password_is_never_replaced(
    project: Path, password: str
) -> None:
    initialize(project)
    path = project / "secrets/mongo_password"
    path.chmod(0o600)
    path.write_text(password)
    path.chmod(0o444)
    before = snapshot(project)
    with pytest.raises(InitError, match="nonempty, single-line"):
        initialize(project)
    assert snapshot(project) == before


def test_symlink_is_not_followed_or_replaced(project: Path) -> None:
    target = project / "untouched"
    target.write_text("do not change\n")
    (project / ".env").symlink_to(target)
    with pytest.raises(InitError, match="not a symlink"):
        initialize(project)
    assert (project / ".env").is_symlink()
    assert target.read_text() == "do not change\n"
    assert not (project / "secrets").exists()


@pytest.mark.parametrize(
    "value", ["./secrets/mongo_admin_password", "${SECRET_DIR}/app"]
)
def test_ambiguous_secret_paths_fail_before_creating_any_files(
    project: Path, value: str
) -> None:
    env_file = project / ".env"
    env_file.write_text(f"MONGO_PASSWORD_PATH='{value}'\n")
    env_file.chmod(0o600)
    before = snapshot(project)
    with pytest.raises(InitError, match="different files|literal path"):
        initialize(project)
    assert snapshot(project) == before
    assert not (project / "secrets").exists()


def test_cli_reports_permission_error_without_traceback(project: Path) -> None:
    env_file = project / ".env"
    env_file.write_text("APP_PORT=8123\n")
    env_file.chmod(0o644)
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert "must have mode 0600" in result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout == ""
    assert env_file.read_text() == "APP_PORT=8123\n"


def test_concurrent_initializations_publish_complete_files_without_leaking_passwords(
    project: Path,
) -> None:
    with (
        subprocess.Popen(
            [sys.executable, str(SCRIPT)],
            cwd=project,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as first,
        subprocess.Popen(
            [sys.executable, str(SCRIPT)],
            cwd=project,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as second,
    ):
        outputs = [process.communicate(timeout=10) for process in (first, second)]
        assert first.returncode == second.returncode == 0, outputs
    before = snapshot(project)
    assert initialize(project) == []
    assert snapshot(project) == before
    assert not list(project.rglob(".init-*"))
    for name in SECRET_PATHS.values():
        password = (project / name).read_text().strip()
        assert re.fullmatch(r"[0-9a-f]{64}", password)
        assert all(password not in stdout + stderr for stdout, stderr in outputs)
    assert (project / "secrets/mongo_password").read_text() != (
        project / "secrets/mongo_admin_password"
    ).read_text()


def test_nested_secret_symlink_is_rejected(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = project / "outside"
    outside.mkdir(mode=0o700)
    (outside / "nested").mkdir(mode=0o700)
    (project / "linked").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("MONGO_PASSWORD_PATH", "linked/nested/password")
    with pytest.raises(InitError, match="symlink"):
        initialize(project)
    assert not (project / ".env").exists()
    assert not list(outside.rglob("password"))


def test_existing_identical_passwords_are_rejected(project: Path) -> None:
    initialize(project)
    app = project / "secrets/mongo_password"
    admin = project / "secrets/mongo_admin_password"
    admin.chmod(0o600)
    admin.write_bytes(app.read_bytes())
    admin.chmod(0o444)
    with pytest.raises(InitError, match="passwords must be different"):
        initialize(project)


def test_invalid_utf8_is_reported_without_content(project: Path) -> None:
    env = project / ".env"
    env.write_bytes(b"secret-canary-\xff")
    env.chmod(0o600)
    with pytest.raises(InitError, match="UTF-8") as error:
        initialize(project)
    assert "secret-canary" not in str(error.value)


def test_cli_success_and_idempotence(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.init_local import main

    monkeypatch.chdir(project)
    assert main() == 0
    assert "Created: .env" in capsys.readouterr().out
    assert main() == 0
    assert "existing files left unchanged" in capsys.readouterr().out
    (project / ".env").chmod(0o644)
    assert main() == 1
    assert "must have mode 0600" in capsys.readouterr().err


def test_atomic_publish_never_replaces_existing_file(project: Path) -> None:
    from scripts.init_local import create_file

    path = project / "existing"
    assert create_file(path, "first", 0o600)
    assert not create_file(path, "second", 0o600)
    assert path.read_text() == "first"
    assert not list(project.glob(".init-*"))
