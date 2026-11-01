from __future__ import annotations

import base64
import hashlib
import json
import re
import socket
import struct
import threading
from collections.abc import Iterator
from datetime import datetime
from http.client import HTTPConnection
from typing import Any, cast
from unittest.mock import MagicMock
from urllib.parse import urlencode

import pytest
from pymongo import MongoClient
from pymongo.errors import AutoReconnect

import main


def form(username: str = "Pavel", message: str = "Привіт + světe & = 🌍") -> bytes:
    return urlencode({"username": username, "message": message}).encode()


@pytest.mark.parametrize("stage", ["before", "body", "timeout"])
def test_incomplete_responses_keep_request_id(
    web: main.WebServer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    stage: str,
) -> None:
    import logging
    from http.client import HTTPException
    from uuid import uuid4

    request_id = str(uuid4())
    done = threading.Event()

    def fail(handler: main.WebHandler) -> None:
        if stage != "before":
            handler.send_response(200)
            handler.send_header("Content-Length", "100")
            handler.end_headers()
        if stage == "timeout":
            raise TimeoutError("private-body")
        raise BrokenPipeError("private-body")

    monkeypatch.setattr(main.WebHandler, "do_GET", fail)
    monkeypatch.setattr(web, "handle_error", lambda *args: done.set())
    caplog.set_level(logging.INFO, logger="messages")
    with pytest.raises((OSError, HTTPException)):
        request(web, headers={"X-Request-ID": request_id})
    if stage != "timeout":
        assert done.wait(2)
    records = [r for r in caplog.records if r.getMessage() == "request_incomplete"]
    assert len(records) == 1
    payload = json.loads(main.JsonFormatter().format(records[0]))
    assert payload["request_id"] == request_id
    assert payload["status"] == (500 if stage == "before" else 200)
    assert not any(r.getMessage() == "request_completed" for r in caplog.records)
    serialized = "\n".join(main.JsonFormatter().format(r) for r in caplog.records)
    assert "private-body" not in serialized


def test_concurrent_request_contexts_are_isolated(
    web: main.WebServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from uuid import uuid4

    def echo(handler: main.WebHandler) -> None:
        handler.json_reply(200, {"request_id": main.REQUEST_ID.get() or "missing"})

    monkeypatch.setattr(main.WebHandler, "do_GET", echo)
    identifiers = [str(uuid4()) for _ in range(12)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(
            pool.map(
                lambda identifier: request(web, headers={"X-Request-ID": identifier}),
                identifiers,
            )
        )
    for identifier, (_, headers, body) in zip(identifiers, responses, strict=True):
        assert json.loads(body)["request_id"] == headers["X-Request-ID"] == identifier
    assert main.REQUEST_ID.get() is None


@pytest.fixture
def backend() -> Iterator[tuple[main.MessageServer, MagicMock]]:
    client = MagicMock()
    client["messages"]["messages"].insert_one.return_value.acknowledged = True
    with main.MessageServer(
        ("127.0.0.1", 0), cast(MongoClient[dict[str, Any]], client), "messages"
    ) as server:
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        thread.start()
        try:
            yield server, client
        finally:
            server.shutdown()
            thread.join()


@pytest.fixture
def web(backend: tuple[main.MessageServer, MagicMock]) -> Iterator[main.WebServer]:
    server, _ = backend
    with main.WebServer(("127.0.0.1", 0), server.server_address) as http:
        thread = threading.Thread(
            target=http.serve_forever, kwargs={"poll_interval": 0.01}
        )
        thread.start()
        try:
            yield http
        finally:
            http.shutdown()
            thread.join()


def request(
    web: main.WebServer,
    path: str = "/",
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = HTTPConnection(*web.server_address, timeout=10)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def raw_http(web: main.WebServer, data: bytes) -> bytes:
    with socket.create_connection(web.server_address, timeout=2) as connection:
        connection.sendall(data)
        connection.shutdown(socket.SHUT_WR)
        chunks = []
        while chunk := connection.recv(65536):
            chunks.append(chunk)
        return b"".join(chunks)


def test_unicode_and_special_characters() -> None:
    assert main.parse_message(form("  Ірина  ")) == {
        "username": "Ірина",
        "message": "Привіт + světe & = 🌍",
    }
    assert (
        main.parse_message(form(message="line 1\nline 2"))["message"]
        == "line 1\nline 2"
    )
    assert len(main.parse_message(form("u" * 80, "🌍" * 4000))["message"]) == 4000


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"username=a",
        b"message=x",
        b"username=&message=m",
        b"username=u&message=+",
        b"username=u&username=v&message=x",
        b"username=u&message=m&admin=true",
        b"username=u&message=%FF",
        b"username=u&message=%",
        b"username=u&message=%GG",
        b"username=u&message=\xff",
        b"username=u&message=%00",
        b"username=u&message",
        form("u" * 81),
        form(message="x" * 4001),
        b"x" * (main.MAX_BODY + 1),
    ],
)
def test_invalid_form(payload: bytes) -> None:
    with pytest.raises(ValueError):
        main.parse_message(payload)


def test_fragmented_tcp_frame() -> None:
    left, right = socket.socketpair()
    payload = form()
    packet = struct.pack("!I", len(payload)) + payload

    def send() -> None:
        with left:
            for byte in packet:
                left.sendall(bytes([byte]))

    thread = threading.Thread(target=send)
    thread.start()
    with right:
        assert main.receive_frame(right) == payload
    thread.join()


@pytest.mark.parametrize("packet", [b"", b"\0\0", struct.pack("!I", 10) + b"abc"])
def test_truncated_tcp_frame(packet: bytes) -> None:
    left, right = socket.socketpair()
    with left, right:
        left.sendall(packet)
        left.shutdown(socket.SHUT_WR)
        with pytest.raises(ConnectionError):
            main.receive_frame(right)


def test_socket_inserts_timestamp_and_acknowledges(
    backend: tuple[main.MessageServer, MagicMock],
) -> None:
    server, client = backend
    before = datetime.now()
    assert main.forward_message(form(), server.server_address) == b"OK"
    data = client["messages"]["messages"].insert_one.call_args.args[0]
    assert set(data) == {"date", "username", "message"}
    assert before <= datetime.fromisoformat(data["date"]) <= datetime.now()
    assert len(data["date"]) == 26
    assert data["message"] == "Привіт + světe & = 🌍"


def test_socket_rejects_bad_frames(
    backend: tuple[main.MessageServer, MagicMock],
) -> None:
    server, client = backend
    assert main.forward_message(b"wrong", server.server_address) == b"INVALID"
    with socket.create_connection(server.server_address, timeout=2) as connection:
        connection.sendall(
            struct.pack("!I", main.MAX_BODY + main.REQUEST_HEADER.size + 1)
        )
        assert main.receive_frame(connection) == b"INVALID"
    client["messages"]["messages"].insert_one.assert_not_called()


def test_health_does_not_insert(backend: tuple[main.MessageServer, MagicMock]) -> None:
    server, client = backend
    assert main.forward_message(b"", server.server_address) == b"OK"
    client.admin.command.assert_called_once_with("ping")
    client["messages"]["messages"].insert_one.assert_not_called()


@pytest.mark.parametrize(
    "route,content_type",
    [
        ("/", "text/html"),
        ("/index.html?x=1", "text/html"),
        ("/message.html", "text/html"),
        ("/message", "text/html"),
        ("/style.css?v=1", "text/css"),
        ("/logo.png", "image/png"),
    ],
)
def test_routes(web: main.WebServer, route: str, content_type: str) -> None:
    status, headers, body = request(web, route)
    assert status == 200
    assert headers["Content-Type"].startswith(content_type)
    assert int(headers["Content-Length"]) == len(body) > 0
    assert b"{{NOTICE}}" not in body


@pytest.mark.parametrize(
    "route",
    [
        "/missing",
        "/main.py",
        "/.env",
        "/../main.py",
        "/%2e%2e/.env",
        "/secrets/mongo_password",
        "/error.html",
        "/.git/config",
    ],
)
def test_not_found_uses_error_page(web: main.WebServer, route: str) -> None:
    status, _, body = request(web, route)
    assert status == 404
    assert body == (main.FRONTEND / "error.html").read_bytes()


def test_head(web: main.WebServer) -> None:
    status, headers, body = request(web, method="HEAD")
    assert status == 200 and not body
    assert (
        int(headers["Content-Length"]) == (main.FRONTEND / "index.html").stat().st_size
    )


def test_post_redirect_only_after_insert(
    web: main.WebServer,
    backend: tuple[main.MessageServer, MagicMock],
) -> None:
    status, headers, body = request(
        web,
        "/message",
        "POST",
        form(),
        {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
    )
    assert status == 303 and not body
    assert headers["Location"] == "/message/sent"
    backend[1]["messages"]["messages"].insert_one.assert_called_once()
    assert "Повідомлення збережено" in request(web, headers["Location"])[2].decode()


@pytest.mark.parametrize(
    "body,content_type,status",
    [
        (b"username=x&message=", "application/x-www-form-urlencoded", 400),
        (b"", "application/x-www-form-urlencoded", 400),
        (form(), "application/json", 415),
        (b"x" * (main.MAX_BODY + 1), "application/x-www-form-urlencoded", 413),
    ],
)
def test_invalid_http_post(
    web: main.WebServer,
    body: bytes,
    content_type: str,
    status: int,
    backend: tuple[main.MessageServer, MagicMock],
) -> None:
    assert (
        request(web, "/message", "POST", body, {"Content-Type": content_type})[0]
        == status
    )
    backend[1]["messages"]["messages"].insert_one.assert_not_called()


@pytest.mark.parametrize(
    "headers,status",
    [
        (b"", 411),
        (b"Content-Length: -1\r\n", 400),
        (b"Content-Length: abc\r\n", 400),
        (b"Content-Length: 0\r\nContent-Length: 0\r\n", 400),
        (b"Transfer-Encoding: chunked\r\n", 400),
        (b"Content-Length: " + b"9" * 5000 + b"\r\n", 413),
        (b"Content-Length: 65537\r\n", 413),
        (
            b"Content-Length: 20\r\n"
            b"Content-Type: application/x-www-form-urlencoded\r\n",
            400,
        ),
    ],
)
def test_bad_http_framing(web: main.WebServer, headers: bytes, status: int) -> None:
    response = raw_http(web, b"POST /message HTTP/1.0\r\n" + headers + b"\r\n")
    assert response.startswith(f"HTTP/1.0 {status}".encode())


def test_unknown_post(web: main.WebServer) -> None:
    assert request(web, "/elsewhere", "POST", form())[0] == 404


def test_db_outage_returns_json_without_echoing_user_input(
    web: main.WebServer,
    backend: tuple[main.MessageServer, MagicMock],
) -> None:
    backend[1]["messages"]["messages"].insert_one.side_effect = AutoReconnect("offline")
    body = form("{{MESSAGE}}", '</textarea><script>alert("x")</script>')
    status, _, content = request(
        web,
        "/message",
        "POST",
        body,
        {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    assert status == 503
    assert json.loads(content) == {"error": "storage_unavailable"}


def test_unacknowledged_write_is_not_success(
    backend: tuple[main.MessageServer, MagicMock],
) -> None:
    server, client = backend
    client["messages"]["messages"].insert_one.return_value.acknowledged = False
    assert main.forward_message(form(), server.server_address) == b"UNAVAILABLE"


def test_health_detects_db_outage(
    web: main.WebServer,
    backend: tuple[main.MessageServer, MagicMock],
) -> None:
    assert request(web, "/health")[0] == 200
    backend[1].admin.command.side_effect = AutoReconnect("offline")
    assert request(web, "/health")[0] == 503


def test_unavailable_socket(web: main.WebServer) -> None:
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        web.socket_address = reserved.getsockname()
        assert request(web, "/health")[0] == 503
        assert (
            request(
                web,
                "/message",
                "POST",
                form(),
                {"Content-Type": "application/x-www-form-urlencoded"},
            )[0]
            == 503
        )


def test_short_socket_client_does_not_kill_server(
    backend: tuple[main.MessageServer, MagicMock],
) -> None:
    server, _ = backend
    with socket.create_connection(server.server_address, timeout=2) as connection:
        connection.sendall(b"\0\0")
    assert main.forward_message(form(), server.server_address) == b"OK"


@pytest.mark.parametrize("path", ["/message", "/api/message"])
def test_json_success_after_database_ack(
    web: main.WebServer,
    backend: tuple[main.MessageServer, MagicMock],
    path: str,
) -> None:
    status, headers, body = request(
        web,
        path,
        "POST",
        form(),
        {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    assert status == 201
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert json.loads(body) == {"status": "saved"}
    backend[1]["messages"]["messages"].insert_one.assert_called_once()


@pytest.mark.parametrize("payload", [b"", b"username=x&message=", b"invalid"])
def test_json_validation_error(web: main.WebServer, payload: bytes) -> None:
    status, _, body = request(
        web,
        "/api/message",
        "POST",
        payload,
        {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    assert status == 400
    assert json.loads(body) == {"error": "invalid_message"}


def test_next_assets_and_client_navigation_payload(web: main.WebServer) -> None:
    for path in (main.FRONTEND / "_next" / "static").rglob("*.js"):
        status, headers, body = request(
            web, "/" + path.relative_to(main.FRONTEND).as_posix()
        )
        assert status == 200 and body
        assert "javascript" in headers["Content-Type"]
    for route in ("/message.txt", "/message/__next._full.txt"):
        assert request(web, route)[0] == 200


def test_next_inline_scripts_are_allowed_by_csp(web: main.WebServer) -> None:
    _, headers, body = request(web, "/message")
    policy = headers["Content-Security-Policy"]
    scripts = re.findall(rb"<script\b[^>]*>(.*?)</script>", body, re.DOTALL)
    assert any(scripts), "Next.js hydration scripts are expected in the static export"
    assert "unsafe-inline" not in policy and "unsafe-eval" not in policy
    for script in scripts:
        if script:
            digest = base64.b64encode(hashlib.sha256(script).digest()).decode()
            assert f"'sha256-{digest}'" in policy


@pytest.mark.parametrize(
    "path", ["/../main.py", "/%2e%2e/main.py", "/%00", "/%ff", "/..\\main.py"]
)
def test_export_path_traversal(path: str) -> None:
    assert main.static_file(path) is None


def test_openapi_matches_runtime_and_snapshot(web: main.WebServer) -> None:
    from scripts.export_openapi import export

    status, headers, body = request(web, "/openapi.json")
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == main.openapi_schema()
    export(check=True)


@pytest.mark.parametrize(
    "supplied", ["799eaaeb-f57d-4fc6-9676-536598a66571", "invalid-secret"]
)
def test_request_correlation_and_private_access_logs(
    web: main.WebServer, caplog: pytest.LogCaptureFixture, supplied: str
) -> None:
    import logging
    from uuid import UUID

    caplog.set_level(logging.INFO, logger="messages")
    canary = "private-message-username-password"
    status, headers, _ = request(
        web,
        "/api/message?secret=" + canary,
        "POST",
        form(canary, canary),
        {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "X-Request-ID": supplied,
            "Authorization": "Bearer " + canary,
        },
    )
    assert status == 201
    request_id = headers["X-Request-ID"]
    assert str(UUID(request_id)) == request_id
    assert (request_id == supplied) == supplied.startswith("799e")
    records = [r for r in caplog.records if r.message == "request_completed"]
    assert len(records) == 1
    record = records[0]
    payload = json.loads(main.JsonFormatter().format(record))
    assert payload["request_id"] == request_id
    assert payload["path"] == "/api/message"
    assert payload["status"] == 201 and payload["duration_ms"] >= 0
    assert payload["method"] == "POST"
    serialized = "\n".join(main.JsonFormatter().format(r) for r in caplog.records)
    assert canary not in serialized
    assert "invalid-secret" not in serialized


def test_json_formatter_never_serializes_exception_secrets() -> None:
    import logging
    import sys

    try:
        raise RuntimeError("mongodb://user:private-password@host")
    except RuntimeError:
        record = logging.LogRecord(
            "messages", logging.ERROR, "", 0, "operation_failed", (), sys.exc_info()
        )
    payload = json.loads(main.JsonFormatter().format(record))
    assert payload["exception"] == "RuntimeError"
    assert "private-password" not in json.dumps(payload)


def test_body_timeout_returns_408(
    web: main.WebServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "IO_TIMEOUT", 0.05)
    with socket.create_connection(web.server_address, timeout=2) as connection:
        connection.sendall(
            b"POST /api/message HTTP/1.0\r\nContent-Length: 4\r\n"
            b"Content-Type: application/x-www-form-urlencoded\r\n\r\nx"
        )
        assert b"408" in connection.recv(4096)


def test_protocol_error_and_unknown_method_are_private(
    web: main.WebServer, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    caplog.set_level(logging.INFO, logger="messages")
    assert b"400" in raw_http(web, b"GET /private HTTP/invalid\r\n\r\n")
    assert request(web, "/private", "PRIVATE_SECRET")[0] == 501
    serialized = "\n".join(main.JsonFormatter().format(r) for r in caplog.records)
    assert "PRIVATE_SECRET" not in serialized and "/private" not in serialized
    assert "HTTP protocol error" in serialized


def test_html_errors_and_unavailable_health(
    web: main.WebServer, backend: tuple[main.MessageServer, MagicMock]
) -> None:
    _, client = backend
    client["messages"]["messages"].insert_one.side_effect = AutoReconnect("private")
    status, _, body = request(
        web,
        "/message",
        "POST",
        form(),
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert status == 503 and "Не вдалося".encode() in body
    web.socket_address = ("127.0.0.1", 0)
    assert request(web, "/health")[0] == 503


def test_http_server_logs_setup_failure_once(
    web: main.WebServer, caplog: pytest.LogCaptureFixture
) -> None:
    from uuid import UUID

    try:
        raise OSError("private-setup-details")
    except OSError as error:
        web.handle_error(None, None)
        error.__dict__["_request_logged"] = True
        web.handle_error(None, None)
    records = [
        r
        for r in caplog.records
        if r.getMessage() == "HTTP handler failed outside dispatch"
    ]
    assert len(records) == 1
    payload = json.loads(main.JsonFormatter().format(records[0]))
    assert str(UUID(payload["request_id"])) == payload["request_id"]
    assert "private-setup-details" not in json.dumps(payload)


def test_swagger_csp_is_scoped_to_documentation(web: main.WebServer) -> None:
    for path in ("/docs", "/docs.html"):
        status, headers, content = request(web, path)
        assert status == 200 and b"/openapi.json" in content
        policy = headers["Content-Security-Policy"]
        assert "style-src 'self' 'unsafe-inline'" in policy
        assert "img-src 'self' data:" in policy
        assert "script-src 'self' ;" in policy
        assert "unsafe-eval" not in policy
    _, headers, _ = request(web, "/message")
    assert "unsafe-inline" not in headers["Content-Security-Policy"]
    assert "data:" not in headers["Content-Security-Policy"]


def test_http_handler_does_not_load_developer_configuration(
    web: main.WebServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    loader = MagicMock(
        side_effect=AssertionError("request must not read settings or secrets")
    )
    monkeypatch.setattr(main.Settings, "load", loader)
    assert request(web, "/health")[0] == 200
    assert (
        request(
            web,
            "/api/message",
            method="POST",
            body=form(),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )[0]
        == 201
    )
    loader.assert_not_called()


def test_concurrent_http_tcp_database_correlation(
    web: main.WebServer,
    backend: tuple[main.MessageServer, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging
    from concurrent.futures import ThreadPoolExecutor
    from uuid import uuid4

    identifiers = [str(uuid4()) for _ in range(6)]
    seen: dict[str, str | None] = {}
    contexts_after: list[str | None] = []
    barrier = threading.Barrier(6, timeout=4)
    finished = threading.Event()
    lock = threading.Lock()

    def insert(document: dict[str, str]) -> MagicMock:
        assert set(document) == {"date", "username", "message"}
        with lock:
            seen[document["username"]] = main.REQUEST_ID.get()
        barrier.wait()
        if document["username"] == "private-2":
            raise AutoReconnect("private-database-error")
        return MagicMock(acknowledged=True)

    def finish(handler: main.MessageHandler) -> None:
        with lock:
            contexts_after.append(main.REQUEST_ID.get())
            if len(contexts_after) == len(identifiers):
                finished.set()

    monkeypatch.setattr(main.MessageHandler, "finish", finish)
    backend[1]["messages"]["messages"].insert_one.side_effect = insert
    caplog.set_level(logging.INFO, logger="messages")

    def submit(index: int) -> tuple[int, dict[str, str], bytes]:
        return request(
            web,
            "/api/message",
            "POST",
            form("private-" + str(index), "private-form-body"),
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "X-Request-ID": identifiers[index],
            },
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(submit, range(6)))
    assert finished.wait(2)
    assert contexts_after == [None] * 6
    assert seen == {
        "private-" + str(i): identifier for i, identifier in enumerate(identifiers)
    }
    records = [json.loads(main.JsonFormatter().format(r)) for r in caplog.records]
    for i, (identifier, response) in enumerate(
        zip(identifiers, responses, strict=True)
    ):
        assert response[0] == (503 if i == 2 else 201)
        assert response[1]["X-Request-ID"] == identifier
        events = [
            record for record in records if record.get("request_id") == identifier
        ]
        database = [r for r in events if r["message"] == "database_operation_completed"]
        assert len(database) == 1
        assert database[0]["operation"] == "insert"
        assert database[0]["outcome"] == ("unconfirmed" if i == 2 else "acknowledged")
        assert database[0]["duration_ms"] >= 0
        for event in (
            "request_started",
            "tcp_request_started",
            "tcp_request_completed",
        ):
            assert sum(r["message"] == event for r in events) == 1
    assert "private-" not in json.dumps(records)
    assert main.REQUEST_ID.get() is None


@pytest.mark.parametrize(
    "packet",
    [b"", b"MSG1", b"MSG2" + bytes(16), b"username=old&message=protocol"],
)
def test_request_envelope_rejects_missing_or_unknown_version(
    backend: tuple[main.MessageServer, MagicMock], packet: bytes
) -> None:
    server, client = backend
    with socket.create_connection(server.server_address, timeout=2) as connection:
        main.send_frame(connection, packet)
        assert main.receive_frame(connection) == b"INVALID"
    client["messages"]["messages"].insert_one.assert_not_called()
    client.admin.command.assert_not_called()


def test_envelope_preserves_maximum_form_body_and_uuid() -> None:
    identifier = "799eaaeb-f57d-4fc6-9676-536598a66571"
    body = bytes(range(256)) * 256
    packet = main.encode_request(body, identifier)
    assert len(packet) == main.MAX_BODY + 20
    assert main.decode_request(packet) == (identifier, body)
    with pytest.raises(ValueError):
        main.encode_request(body + b"x", identifier)
    with pytest.raises(ValueError):
        main.decode_request(packet + b"x")
    with pytest.raises(ValueError):
        main.encode_request(b"", "invalid-private-id")


@pytest.mark.parametrize("failure", ["ack", "unexpected"])
def test_tcp_failure_logs_keep_correlation_and_reset_context(
    backend: tuple[main.MessageServer, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    import logging
    from uuid import uuid4

    server, client = backend
    identifier = str(uuid4())
    contexts: list[str | None] = []
    finished = threading.Event()

    def finish(handler: main.MessageHandler) -> None:
        contexts.append(main.REQUEST_ID.get())
        finished.set()

    monkeypatch.setattr(main.MessageHandler, "finish", finish)
    if failure == "ack":
        monkeypatch.setattr(main, "send_frame", MagicMock(side_effect=BrokenPipeError))
    else:
        client["messages"]["messages"].insert_one.side_effect = RuntimeError(
            "private-error"
        )
    caplog.set_level(logging.INFO, logger="messages")
    with socket.create_connection(server.server_address, timeout=2) as connection:
        packet = main.encode_request(form(), identifier)
        connection.sendall(struct.pack("!I", len(packet)) + packet)
        assert connection.recv(1) == b""
    assert finished.wait(2)
    assert contexts == [None]
    records = [json.loads(main.JsonFormatter().format(r)) for r in caplog.records]
    assert all(r["request_id"] == identifier for r in records)
    assert records[-1]["outcome"] == (
        "acknowledgement_lost" if failure == "ack" else "handler_error"
    )
    assert "private-error" not in json.dumps(records)
