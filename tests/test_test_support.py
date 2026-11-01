"""Failure-path regression checks for disposable infrastructure cleanup."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.test_integration import Stack


@pytest.mark.parametrize("failure", ["none", "logs", "write", "down", "both"])
def test_cleanup_always_attempts_down(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = Stack(
        ["docker", "compose", "-p", "hw06-test-unit"], {}, passwords=("private-value",)
    )
    calls: list[str] = []

    def compose(*args: str, **kwargs: object) -> str:
        calls.append(args[0])
        if (args[0] == "logs" and failure in {"logs", "both"}) or (
            args[0] == "down" and failure in {"down", "both"}
        ):
            raise RuntimeError("compose failed")
        return "safe private-value logs"

    monkeypatch.setattr(stack, "compose", compose)
    artifacts = tmp_path / "artifacts"
    if failure == "write":
        artifacts.write_text("blocks directory creation")
    if failure == "none":
        stack.cleanup(artifacts)
        assert (
            artifacts / "integration-containers.log"
        ).read_text() == "safe [REDACTED] logs"
    else:
        with pytest.raises((RuntimeError, OSError)):
            stack.cleanup(artifacts)
    assert calls == ["logs", "down"]


def test_compose_failure_redacts_test_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    stack = Stack([], {"PRIVATE": "environment-secret"}, passwords=("password-secret",))
    monkeypatch.setattr(
        subprocess,
        "run",
        MagicMock(
            return_value=subprocess.CompletedProcess(
                [], 1, "password-secret", "failure"
            )
        ),
    )
    with pytest.raises(RuntimeError) as error:
        stack.compose("up")
    assert "password-secret" not in str(error.value)
    assert "environment-secret" not in repr(stack)
