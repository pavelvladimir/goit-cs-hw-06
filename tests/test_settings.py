"""Validate startup, preserve secret bytes and keep diagnostics private."""

import json
import logging
import multiprocessing
import sys
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import quote, quote_plus

import pytest

import main


@pytest.mark.parametrize(
    "name,value",
    [
        ("MONGO_PORT", "0"),
        ("MONGO_PORT", "65536"),
        ("MONGO_PORT", "private-invalid"),
        ("MONGO_HOST", ""),
        ("MONGO_HOST", "mongodb://private@host"),
        ("MONGO_DATABASE", "admin"),
        ("MONGO_DATABASE", "../private"),
        ("MONGO_USER", "root"),
        ("MONGO_USER", ""),
        ("LOG_LEVEL", "private-invalid"),
        ("DB_CONNECT_TIMEOUT", "0"),
        ("DB_CONNECT_TIMEOUT", "31"),
        ("SHUTDOWN_GRACE_SECONDS", "15"),
        ("SHUTDOWN_GRACE_SECONDS", "121"),
        ("HTTP_MAX_CONNECTIONS", "0"),
        ("HTTP_MAX_CONNECTIONS", "129"),
        ("TCP_MAX_CONNECTIONS", "0"),
        ("TCP_MAX_CONNECTIONS", "129"),
        ("RECEIVE_TIMEOUT", "0"),
        ("RECEIVE_TIMEOUT", "6"),
    ],
)
def test_invalid_settings(
    name: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(main.ConfigurationError, match=name) as error:
        main.Settings.load()
    assert "private" not in str(error.value)


def test_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    assert main.Settings.load() == main.Settings(password="unit-test-only")
    for key, value in {
        "MONGO_HOST": "::1",
        "MONGO_PORT": "65535",
        "LOG_LEVEL": "debug",
        "DB_CONNECT_TIMEOUT": "30",
        "SHUTDOWN_GRACE_SECONDS": "43",
    }.items():
        monkeypatch.setenv(key, value)
    settings = main.Settings.load()
    assert (settings.mongo_host, settings.mongo_port, settings.log_level) == (
        "::1",
        65535,
        "DEBUG",
    )
    factory = MagicMock()
    monkeypatch.setattr(main, "MongoClient", factory)
    main.mongo_client()
    assert factory.call_args.kwargs["timeoutMS"] == 30000
    monkeypatch.setenv("SHUTDOWN_GRACE_SECONDS", "42")
    with pytest.raises(main.ConfigurationError, match="SHUTDOWN_GRACE_SECONDS"):
        main.Settings.load()


@pytest.mark.parametrize(
    "key,value",
    [("MONGO_HOST", "localhost\x00.example"), ("MONGO_PORT", "27017\x00ignored")],
)
def test_nul_address_from_dotenv_never_reaches_mongo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    # Unlike os.environ, .env can contain NUL: cover CVE-2026-116566's input path.
    """Reject NUL-containing file configuration before constructing MongoClient."""
    env = tmp_path / ".env"
    env.write_text(f"{key}='{value}'\n")
    monkeypatch.setattr(main, "ENV_FILE", env)
    monkeypatch.delenv(key, raising=False)
    client = MagicMock()
    monkeypatch.setattr(main, "MongoClient", client)
    with pytest.raises(main.ConfigurationError, match=key):
        main.mongo_client()
    client.assert_not_called()


@pytest.mark.parametrize(
    "content", [b"", b" \n", b"two\nlines", b"null\x00byte", b"\xff"]
)
def test_invalid_secret(
    content: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "secret"
    secret.write_bytes(content)
    monkeypatch.setenv("MONGO_PASSWORD_FILE", str(secret))
    with pytest.raises(main.ConfigurationError, match="MONGO_PASSWORD_FILE"):
        main.Settings.load()


def test_password_spaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = tmp_path / "secret"
    secret.write_bytes("  secret +/ž  \r\n".encode())
    monkeypatch.setenv("MONGO_PASSWORD_FILE", str(secret))
    settings = main.Settings.load()
    assert settings.password == "  secret +/ž  "
    assert "secret" not in repr(settings)
    secret.unlink()
    with pytest.raises(main.ConfigurationError, match="cannot read"):
        main.Settings.load()


def test_startup_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MONGO_PORT", "private-value")
    spawn = MagicMock()
    monkeypatch.setattr(multiprocessing, "get_context", spawn)
    assert main.main() == 1
    spawn.assert_not_called()
    output = capsys.readouterr().err
    assert json.loads(output)["message"] == "Invalid configuration"
    assert "private-value" not in output


def test_redaction_and_safe_frames() -> None:
    password = 'p@ss +/ž"\\word'
    formatter = main.JsonFormatter(password)
    variants = [
        password,
        quote(password, safe=""),
        quote_plus(password),
        json.dumps(password)[1:-1],
        json.dumps(password, ensure_ascii=False)[1:-1],
    ]
    for encoded in variants:
        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 17, "failure %s", (encoded,), None
        )
        record.path = "/" + encoded
        payload = json.loads(formatter.format(record))
        assert payload["message"] == "failure [REDACTED]"
        assert payload["path"] == "/[REDACTED]"
        assert formatter.redact([{"value": encoded}, 201]) == [
            {"value": "[REDACTED]"},
            201,
        ]
    token = main.REQUEST_ID.set("test-request")
    try:
        try:
            raise RuntimeError("private-exception-body")
        except RuntimeError:
            record = logging.LogRecord(
                "test", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
            )
        payload = json.loads(formatter.format(record))
    finally:
        main.REQUEST_ID.reset(token)
    assert payload["request_id"] == "test-request"
    assert payload["frames"][-1]["function"] == "test_redaction_and_safe_frames"
    assert set(payload["frames"][-1]) == {"file", "line", "function"}
    assert "private-exception-body" not in json.dumps(payload)
    assert main.REQUEST_ID.get() is None


def test_logging_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = tmp_path / "secret"
    secret.write_text("secret-value")
    monkeypatch.setenv("MONGO_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    configure = MagicMock()
    monkeypatch.setattr(logging, "basicConfig", configure)
    main.configure_logging()
    assert configure.call_args.kwargs["level"] == "WARNING"
    handler = configure.call_args.kwargs["handlers"][0]
    record = logging.LogRecord("test", logging.WARNING, "", 1, "secret-value", (), None)
    assert "secret-value" not in handler.format(record)


@pytest.mark.parametrize("password,file", [("", ""), ("one", "two")])
def test_exactly_one_secret_is_required(
    password: str, file: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MONGO_PASSWORD", password)
    monkeypatch.setenv("MONGO_PASSWORD_FILE", file)
    with pytest.raises(main.ConfigurationError, match="exactly one"):
        main.Settings.load()


@pytest.mark.parametrize("password", [" ", "two\nlines"])
def test_direct_secret_validation(
    password: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MONGO_PASSWORD_FILE")
    monkeypatch.setenv("MONGO_PASSWORD", password)
    with pytest.raises(main.ConfigurationError, match="single-line"):
        main.Settings.load()


def test_dotenv_precedence_literal_password_and_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MONGO_PASSWORD_FILE")
    env_file = tmp_path / ".env"
    monkeypatch.setattr(main, "ENV_FILE", env_file)
    env_file.write_text(
        "MONGO_HOST=file-host\nMONGO_PASSWORD='  literal-${NOT_EXPANDED}-ž  '\n"
    )
    settings = main.Settings.load()
    assert settings.mongo_host == "file-host"
    assert settings.password == "  literal-${NOT_EXPANDED}-ž  "
    monkeypatch.setenv("MONGO_HOST", "environment-host")
    monkeypatch.setenv("MONGO_PASSWORD", "environment-secret")
    assert main.Settings.load().password == "environment-secret"
    assert main.Settings.load().mongo_host == "environment-host"
    monkeypatch.delenv("MONGO_PASSWORD")
    (tmp_path / "secret").write_text("  file-secret  \n")
    env_file.write_text("MONGO_PASSWORD_FILE=secret\nIGNORED_KEY\n")
    assert main.Settings.load().password == "  file-secret  "


@pytest.mark.parametrize("invalid", ["utf8", "directory"])
def test_unreadable_dotenv_is_safe(
    invalid: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    if invalid == "utf8":
        env_file.write_bytes(b"\xff")
    else:
        env_file.mkdir()
    monkeypatch.setattr(main, "ENV_FILE", env_file)
    with pytest.raises(main.ConfigurationError, match="ENV_FILE: cannot read"):
        main.Settings.load()
