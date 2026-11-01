"""Local tests never inherit developer secrets; Docker requires explicit opt-in."""

from pathlib import Path

import pytest

import main


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--integration",
        action="store_true",
        help="Enable disposable Docker integration tests",
    )
    parser.addoption(
        "--write-load-seconds",
        type=int,
        default=10,
        help="Seconds per POST load stage (1..60); disposable integration stack only",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if not 1 <= config.getoption("--write-load-seconds") <= 60:
        raise pytest.UsageError("--write-load-seconds must be between 1 and 60")
    if config.getoption("--integration"):
        return
    excluded = [item for item in items if item.get_closest_marker("integration")]
    items[:] = [item for item in items if item not in excluded]
    config.hook.pytest_deselected(items=excluded)


@pytest.fixture(autouse=True)
def isolated_settings(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    if request.node.get_closest_marker("integration"):
        return
    import os

    for key in os.environ:
        if key.startswith(("MONGO_", "COMPOSE_")) or key in {
            "APP_PORT",
            "LOG_LEVEL",
            "DB_CONNECT_TIMEOUT",
            "SHUTDOWN_GRACE_SECONDS",
            "HTTP_MAX_CONNECTIONS",
            "TCP_MAX_CONNECTIONS",
            "RECEIVE_TIMEOUT",
        }:
            monkeypatch.delenv(key)
    monkeypatch.setattr(main, "ENV_FILE", None)
    password = tmp_path / "unit-password"
    password.write_text("unit-test-only")
    monkeypatch.setenv("MONGO_PASSWORD_FILE", str(password))
