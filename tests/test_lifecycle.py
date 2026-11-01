"""Exercise supervisor decisions without timing-dependent real process mocks."""

import multiprocessing
import signal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import main
from scripts.export_openapi import export


def test_contract_drift_is_detected(tmp_path: Path) -> None:
    target = tmp_path / "openapi.json"
    with pytest.raises(SystemExit, match="stale"):
        export(target, check=True)
    export(target)
    export(target, check=True)
    target.write_text("{}")
    with pytest.raises(SystemExit, match="stale"):
        export(target, check=True)


def test_missing_frontend_fails_before_starting_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "configure_logging", lambda: None)
    monkeypatch.setattr(main, "FRONTEND", tmp_path)
    with pytest.raises(SystemExit, match="Frontend export missing"):
        main.main()


@pytest.mark.parametrize("failure", ["none", "dead", "stuck", "exit", "startup"])
def test_supervisor_drains_http_before_tcp(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = MagicMock()
    http_stop, tcp_stop = MagicMock(), MagicMock()
    context.Event.side_effect = [http_stop, tcp_stop]
    tcp, http = MagicMock(), MagicMock()
    tcp.name, http.name = "socket-server", "http-server"
    context.Process.side_effect = [tcp, http]
    for worker in (tcp, http):
        worker.is_alive.return_value = False
        worker.exitcode = 0
    http_stop.wait.return_value = failure != "dead"
    if failure == "exit":
        tcp.exitcode = 1
    if failure == "stuck":
        http.is_alive.return_value = True
    if failure == "startup":
        http.start.side_effect = OSError("cannot spawn")
    events: list[str] = []
    http_stop.set.side_effect = lambda: events.append("http-stop")
    tcp_stop.set.side_effect = lambda: events.append("tcp-stop")
    http.join.side_effect = lambda **kwargs: events.append("http-joined")
    tcp.join.side_effect = lambda **kwargs: events.append("tcp-joined")
    monkeypatch.setattr(multiprocessing, "get_context", lambda _: context)
    monkeypatch.setattr(main, "configure_logging", lambda: None)
    monkeypatch.setattr(signal, "signal", lambda *args: None)
    if failure == "startup":
        with pytest.raises(OSError, match="cannot spawn"):
            main.main()
        assert events == ["tcp-stop", "tcp-joined"]
    else:
        assert main.main() == int(failure != "none")
        assert events.index("http-joined") < events.index("tcp-stop")
        assert http.kill.called == (failure == "stuck")
        http.join.assert_any_call(timeout=15)
        tcp.join.assert_any_call(timeout=5)


def test_worker_lifecycle_and_signal_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "STOP_REQUESTED", False)
    monkeypatch.setattr(main, "configure_logging", lambda: None)
    signals = MagicMock()
    monkeypatch.setattr(signal, "signal", signals)
    server, stop = MagicMock(), MagicMock()
    stop.is_set.side_effect = [False, True]
    main.serve(server, stop)
    server.handle_request.assert_called_once()
    server.__exit__.assert_called_once()
    main.request_stop(15, None)
    assert main.STOP_REQUESTED
    main.configure_worker()
    assert signals.call_count == 2


def test_workers_construct_resources_in_child(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_CONNECT_TIMEOUT", "5")
    monkeypatch.setattr(main, "configure_worker", lambda: None)
    client, serve = MagicMock(), MagicMock()
    monkeypatch.setattr(main, "mongo_client", lambda: client)
    monkeypatch.setattr(main, "serve", serve)
    tcp, http = MagicMock(), MagicMock()
    monkeypatch.setattr(main, "MessageServer", tcp)
    monkeypatch.setattr(main, "WebServer", http)
    stop = MagicMock()
    main.run_socket(stop)
    client.__enter__.return_value.admin.command.assert_called_once_with("ping")
    serve.assert_called_with(tcp.return_value, stop)
    client.__exit__.assert_called_once()
    main.run_http(stop)
    http.assert_called_once_with(
        ("0.0.0.0", 3000),
        ("127.0.0.1", 5000),
        socket_timeout=11,
        max_connections=32,
        receive_timeout=5,
    )
    serve.assert_called_with(http.return_value, stop)


def test_mongo_configuration_reads_secret_without_uri(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    password = tmp_path / "password"
    password.write_text("private-@:/password\n")
    monkeypatch.setenv("MONGO_PASSWORD_FILE", str(password))
    factory = MagicMock()
    monkeypatch.setattr(main, "MongoClient", factory)
    main.mongo_client()
    options = factory.call_args.kwargs
    assert options["password"] == "private-@:/password"
    assert options["authSource"] == "messages"
    assert options["retryWrites"] is False and options["w"] == 1
    monkeypatch.delenv("MONGO_PASSWORD_FILE")
    with pytest.raises(main.ConfigurationError, match="exactly one"):
        main.mongo_client()


@pytest.mark.parametrize("worker", [main.run_http, main.run_socket])
def test_worker_failure_logs_only_exception_type(
    worker: object, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from collections.abc import Callable
    from typing import cast

    monkeypatch.setattr(main, "configure_worker", lambda: None)
    monkeypatch.setattr(
        main, "WebServer", MagicMock(side_effect=OSError("private-secret"))
    )
    monkeypatch.setattr(
        main, "mongo_client", MagicMock(side_effect=OSError("private-secret"))
    )
    with pytest.raises(SystemExit) as error:
        cast(Callable[..., None], worker)(MagicMock())
    assert error.value.code == 1
    assert "private-secret" not in "\n".join(
        main.JsonFormatter().format(r) for r in caplog.records
    )
