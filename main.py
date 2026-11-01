"""Framework-free HTTP → TCP → MongoDB application, in two worker processes."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import multiprocessing
import os
import re
import signal
import socket
import struct
import sys
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BufferedReader, RawIOBase, StringIO
from multiprocessing.synchronize import Event
from pathlib import Path
from socketserver import BaseRequestHandler, BaseServer, TCPServer, ThreadingMixIn
from threading import BoundedSemaphore, Thread
from time import perf_counter
from traceback import walk_tb
from types import FrameType
from typing import Any, cast
from urllib.parse import parse_qs, quote, quote_plus, unquote, urlsplit
from uuid import UUID, uuid4

from dotenv import dotenv_values
from pymongo import MongoClient
from pymongo.errors import PyMongoError

ROOT = Path(__file__).resolve().parent
ENV_FILE: Path | None = ROOT / ".env"
MONGO_APPLICATION_NAME = "goit-cs-hw-06"
FRONTEND = ROOT / "frontend" / "out"
HTTP_PORT = 3000
SOCKET_PORT = 5000
MAX_BODY = 64 * 1024
REQUEST_HEADER = struct.Struct("!4s16s")
REQUEST_MAGIC = b"MSG1"
USERNAME_LIMIT = 80
MESSAGE_LIMIT = 4000
IO_TIMEOUT = 5.0
LOG = logging.getLogger("messages")
STOP_REQUESTED = False
REQUEST_ID: ContextVar[str | None] = ContextVar("request_id", default=None)
HTML_PAGES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/message": "message.html",
    "/message.html": "message.html",
    "/message/sent": "message/sent.html",
    "/message/invalid": "message/invalid.html",
    "/message/unavailable": "message/unavailable.html",
    "/docs": "docs.html",
}
API_RESPONSES = {
    201: {"status": "saved"},
    400: {"error": "invalid_message"},
    503: {"error": "storage_unavailable"},
}


class ConfigurationError(ValueError):
    """Only the setting name and a fixed reason, never the supplied value."""


@dataclass(frozen=True)
class Settings:
    mongo_host: str = "127.0.0.1"
    mongo_port: int = 27017
    mongo_database: str = "messages"
    mongo_user: str = "messages_app"
    password: str = field(default="", repr=False)
    db_connect_timeout: int = 2
    shutdown_grace_seconds: int = 20
    http_max_connections: int = 32
    tcp_max_connections: int = 32
    receive_timeout: int = 5
    log_level: str = "INFO"

    @classmethod
    def load(cls) -> Settings:
        values: dict[str, str] = {}
        if ENV_FILE is not None and ENV_FILE.exists():
            try:
                content = ENV_FILE.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                raise ConfigurationError(
                    "ENV_FILE: cannot read UTF-8 configuration"
                ) from None
            values = {
                key: value
                for key, value in dotenv_values(
                    stream=StringIO(content), interpolate=False
                ).items()
                if value is not None
            }
        values.update(os.environ)

        def integer(name: str, default: int, minimum: int, maximum: int) -> int:
            try:
                value = int(values.get(name, str(default)))
                if minimum <= value <= maximum:
                    return value
            except ValueError:
                pass
            raise ConfigurationError(f"{name}: expected {minimum}..{maximum}")

        host = values.get("MONGO_HOST", "127.0.0.1")
        if not re.fullmatch(r"[a-zA-Z0-9_.:\[\]-]+", host):
            raise ConfigurationError("MONGO_HOST: expected a hostname or IP address")
        names = {}
        for name, default in (
            ("MONGO_DATABASE", "messages"),
            ("MONGO_USER", "messages_app"),
        ):
            value = values.get(name, default)
            if not re.fullmatch(
                r"[a-zA-Z][a-zA-Z0-9_-]{0,62}", value
            ) or value.lower() in {"admin", "local", "config", "root"}:
                raise ConfigurationError(
                    f"{name}: expected a dedicated application name"
                )
            names[name] = value
        password = values.get("MONGO_PASSWORD", "")
        password_file = values.get("MONGO_PASSWORD_FILE", "")
        if bool(password) == bool(password_file):
            raise ConfigurationError(
                "MONGO_PASSWORD: set exactly one of "
                "MONGO_PASSWORD or MONGO_PASSWORD_FILE"
            )
        if password_file:
            try:
                path = Path(password_file)
                if not path.is_absolute():
                    path = (ENV_FILE.parent if ENV_FILE is not None else ROOT) / path
                password = path.read_text(encoding="utf-8").rstrip("\r\n")
            except (OSError, UnicodeError, ValueError):
                raise ConfigurationError(
                    "MONGO_PASSWORD_FILE: cannot read UTF-8 secret"
                ) from None
        if not password.strip() or any(char in password for char in "\r\n\x00"):
            raise ConfigurationError(
                "MONGO_PASSWORD_FILE / MONGO_PASSWORD: "
                "expected a nonblank single-line secret"
            )
        level = values.get("LOG_LEVEL", "INFO").upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError(
                "LOG_LEVEL: expected DEBUG, INFO, WARNING, ERROR or CRITICAL"
            )
        timeout = integer("DB_CONNECT_TIMEOUT", 2, 1, 30)
        grace = integer("SHUTDOWN_GRACE_SECONDS", 20, 16, 120)
        if grace < timeout + 13:
            raise ConfigurationError(
                "SHUTDOWN_GRACE_SECONDS: must allow DB_CONNECT_TIMEOUT + 13 seconds"
            )
        return cls(
            mongo_host=host,
            mongo_port=integer("MONGO_PORT", 27017, 1, 65535),
            mongo_database=names["MONGO_DATABASE"],
            mongo_user=names["MONGO_USER"],
            password=password,
            db_connect_timeout=timeout,
            shutdown_grace_seconds=grace,
            http_max_connections=integer("HTTP_MAX_CONNECTIONS", 32, 1, 128),
            tcp_max_connections=integer("TCP_MAX_CONNECTIONS", 32, 1, 128),
            receive_timeout=integer("RECEIVE_TIMEOUT", 5, 1, 5),
            log_level=level,
        )


def openapi_schema() -> dict[str, Any]:
    """One API contract shared by the server, snapshot and generated client."""
    responses: dict[str, Any] = {
        str(status): {
            "description": "Acknowledged write" if status == 201 else "Request failed",
            "headers": {
                "X-Request-ID": {"schema": {"type": "string", "format": "uuid"}}
            },
            "content": {
                "application/json": {
                    "schema": {"$ref": f"#/components/schemas/{name}"},
                    "example": API_RESPONSES[status],
                }
            },
        }
        for status, name in (
            (201, "MessageSaved"),
            (400, "InvalidMessage"),
            (503, "StorageUnavailable"),
        )
    }
    for status, description in (
        (408, "Body timeout"),
        (411, "Content-Length required"),
        (413, "Body too large"),
        (415, "URL-encoded form required"),
    ):
        responses[str(status)] = {
            "description": description,
            "content": {"text/plain": {"schema": {"type": "string"}}},
        }
    responses["400"]["content"]["text/plain"] = {"schema": {"type": "string"}}
    responses["303"] = {
        "description": (
            "Native form without Accept: application/json; redirect to /message/sent"
        )
    }
    schemas: dict[str, Any] = {
        "MessageInput": {
            "type": "object",
            "additionalProperties": False,
            "required": ["username", "message"],
            "properties": {
                "username": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": USERNAME_LIMIT,
                    "description": "Trimmed, nonblank; no NUL characters",
                },
                "message": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MESSAGE_LIMIT,
                    "description": (
                        "Nonblank; no NUL characters; original whitespace preserved"
                    ),
                },
            },
        }
    }
    for status, name in (
        (201, "MessageSaved"),
        (400, "InvalidMessage"),
        (503, "StorageUnavailable"),
    ):
        payload = API_RESPONSES[status]
        schemas[name] = {
            "type": "object",
            "additionalProperties": False,
            "required": list(payload),
            "properties": {
                key: {"type": "string", "const": value}
                for key, value in payload.items()
            },
        }
    return {
        "openapi": "3.1.0",
        "info": {"title": "GoIT Messages API", "version": "1.0.0"},
        "paths": {
            "/api/message": {
                "post": {
                    "operationId": "submitMessage",
                    "description": (
                        "Send Accept: application/json. Success follows a confirmed "
                        "TCP/MongoDB write. No automatic retries."
                    ),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/x-www-form-urlencoded": {
                                "schema": {"$ref": "#/components/schemas/MessageInput"}
                            }
                        },
                    },
                    "responses": responses,
                }
            },
            "/health": {
                "get": {
                    "operationId": "checkHealth",
                    "responses": {
                        str(status): {
                            "description": text.strip(),
                            "content": {
                                "text/plain": {
                                    "schema": {"type": "string", "const": text}
                                }
                            },
                        }
                        for status, text in ((200, "ok\n"), (503, "unavailable\n"))
                    },
                }
            },
        },
        "components": {"schemas": schemas},
    }


class JsonFormatter(logging.Formatter):
    """Only structured, application-owned fields; never serialize exception text."""

    def __init__(self, password: str = "") -> None:
        super().__init__()
        self.secrets = sorted(
            {
                password,
                quote(password, safe=""),
                quote_plus(password),
                json.dumps(password, ensure_ascii=False)[1:-1],
                json.dumps(password, ensure_ascii=True)[1:-1],
            }
            - {""},
            key=len,
            reverse=True,
        )

    def redact(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
        elif isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items()}
        elif isinstance(value, list):
            return [self.redact(item) for item in value]
        return value

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "process": record.processName,
            "message": record.getMessage(),
        }
        for key in (
            "request_id",
            "method",
            "path",
            "status",
            "duration_ms",
            "operation",
            "outcome",
        ):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if "request_id" not in payload and REQUEST_ID.get() is not None:
            payload["request_id"] = REQUEST_ID.get()
        if record.exc_info and record.exc_info[1]:
            payload["exception"] = type(record.exc_info[1]).__name__
            payload["frames"] = (
                [
                    {
                        "file": frame.f_code.co_filename,
                        "line": line,
                        "function": frame.f_code.co_name,
                    }
                    for frame, line in walk_tb(record.exc_info[2])
                ]
                if record.exc_info[2]
                else []
            )
        return json.dumps(
            self.redact(payload), ensure_ascii=False, separators=(",", ":")
        )


def configure_logging() -> None:
    settings = Settings.load()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(settings.password))
    logging.basicConfig(level=settings.log_level, handlers=[handler], force=True)
    logging.getLogger("pymongo").setLevel(logging.CRITICAL)


def static_file(path: str) -> Path | None:
    """Serve only regular files inside the Next.js export, including RSC files."""
    try:
        decoded = unquote(path, errors="strict")
        if "\x00" in decoded or "\\" in decoded or ".." in decoded.split("/"):
            return None
        relative = HTML_PAGES.get(decoded, decoded.lstrip("/"))
        candidate = (FRONTEND / relative).resolve()
        if candidate.is_relative_to(FRONTEND.resolve()) and candidate.is_file():
            return candidate
    except (ValueError, OSError):
        pass
    return None


@lru_cache(maxsize=32)
def content_security_policy(content: bytes, api_docs: bool = False) -> str:
    """Allow Next's exact exported inline scripts, without unsafe-inline/eval."""
    hashes = {
        "'sha256-" + base64.b64encode(hashlib.sha256(script).digest()).decode() + "'"
        for script in re.findall(rb"<script\b[^>]*>(.*?)</script>", content, re.DOTALL)
        if script
    }
    styles = (
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        if api_docs
        else "style-src 'self'; "
    )
    return (
        "default-src 'self'; script-src 'self' "
        + " ".join(sorted(hashes))
        + "; "
        + styles
        + "form-action 'self'; base-uri 'none'; "
        "object-src 'none'; frame-ancestors 'none'"
    )


def parse_message(payload: bytes) -> dict[str, str]:
    """Decode the original form bytes in the socket worker; reject ambiguity."""
    if not payload or len(payload) > MAX_BODY:
        raise ValueError("Invalid message size")
    text = payload.decode("utf-8", errors="strict")
    if re.search(r"%(?![0-9a-fA-F]{2})", text):
        raise ValueError("Invalid percent encoding")
    values = parse_qs(
        text,
        keep_blank_values=True,
        strict_parsing=True,
        encoding="utf-8",
        errors="strict",
        max_num_fields=2,
    )
    if set(values) != {"username", "message"}:
        raise ValueError("Expected username and message")
    username, message = values["username"][0].strip(), values["message"][0]
    if not username or not message.strip() or "\x00" in username + message:
        raise ValueError("Both fields must contain text")
    if len(username) > USERNAME_LIMIT or len(message) > MESSAGE_LIMIT:
        raise ValueError("Field is too long")
    return {"username": username, "message": message}


def receive_exact(
    connection: socket.socket, size: int, *, deadline: float | None = None
) -> bytes:
    data = bytearray()
    while len(data) < size:
        if deadline is not None:
            remaining = deadline - perf_counter()
            if remaining <= 0:
                raise TimeoutError("Frame deadline exceeded")
            connection.settimeout(min(IO_TIMEOUT, remaining))
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise ConnectionError("Incomplete TCP frame")
        data.extend(chunk)
    return bytes(data)


def receive_frame(
    connection: socket.socket,
    *,
    deadline: float | None = None,
    max_size: int = MAX_BODY,
) -> bytes:
    size = struct.unpack("!I", receive_exact(connection, 4, deadline=deadline))[0]
    if size > max_size:
        raise ValueError("TCP frame is too large")
    return receive_exact(connection, size, deadline=deadline)


def send_frame(connection: socket.socket, payload: bytes) -> None:
    connection.sendall(struct.pack("!I", len(payload)) + payload)


def encode_request(payload: bytes, request_id: str) -> bytes:
    """Versioned metadata followed by the unchanged URL-encoded form bytes."""
    if len(payload) > MAX_BODY:
        raise ValueError("TCP request body is too large")
    return REQUEST_HEADER.pack(REQUEST_MAGIC, UUID(request_id).bytes) + payload


def decode_request(packet: bytes) -> tuple[str, bytes]:
    if not REQUEST_HEADER.size <= len(packet) <= MAX_BODY + REQUEST_HEADER.size:
        raise ValueError("Invalid TCP request size")
    magic, identifier = REQUEST_HEADER.unpack_from(packet)
    if magic != REQUEST_MAGIC:
        raise ValueError("Unsupported TCP request version")
    return str(UUID(bytes=identifier)), packet[REQUEST_HEADER.size :]


def forward_message(
    payload: bytes,
    address: tuple[str, int],
    *,
    timeout: float = IO_TIMEOUT + 3,
    request_id: str | None = None,
) -> bytes:
    """One connection per request; wait for the database acknowledgement."""
    packet = encode_request(payload, request_id or REQUEST_ID.get() or str(uuid4()))
    with socket.create_connection(address, timeout=timeout) as connection:
        deadline = perf_counter() + timeout
        send_frame(connection, packet)
        return receive_frame(connection, deadline=deadline)


class BoundedThreads(ThreadingMixIn):
    """Acquire capacity before creating a thread; never queue unbounded work."""

    daemon_threads = False
    slots: BoundedSemaphore
    receive_timeout: float

    def configure_limits(self, maximum: int, receive_timeout: float) -> None:
        self.slots = BoundedSemaphore(maximum)
        self.receive_timeout = receive_timeout
        self.handlers: list[Thread] = []

    def reject_connection(self, request: socket.socket) -> None:
        LOG.warning("TCP capacity exhausted")

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.slots.acquire(blocking=False):
            try:
                self.reject_connection(request)
            finally:
                cast(BaseServer, self).shutdown_request(request)
            return
        try:
            self.handlers = [thread for thread in self.handlers if thread.is_alive()]
            thread = Thread(
                target=self.process_request_thread,
                args=(request, client_address),
                daemon=False,
            )
            thread.start()
            self.handlers.append(thread)
        except BaseException:
            self.slots.release()
            raise

    def server_close(self) -> None:
        super().server_close()
        for thread in self.handlers:
            thread.join()

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class DeadlineReader(RawIOBase):
    """One deadline shared by the HTTP request line, headers and body."""

    def __init__(self, connection: socket.socket, timeout: float) -> None:
        super().__init__()
        self.connection = connection
        self.deadline = perf_counter() + timeout

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        remaining = self.deadline - perf_counter()
        if remaining <= 0:
            raise TimeoutError("Request deadline exceeded")
        self.connection.settimeout(min(IO_TIMEOUT, remaining))
        return self.connection.recv_into(buffer)


class MessageServer(BoundedThreads, TCPServer):
    server_address: tuple[str, int]
    allow_reuse_address = True
    daemon_threads = False

    def __init__(
        self,
        address: tuple[str, int],
        client: MongoClient[dict[str, Any]],
        database: str,
        *,
        max_connections: int = 32,
        receive_timeout: float = IO_TIMEOUT,
    ) -> None:
        self.configure_limits(max_connections, receive_timeout)
        self.client = client
        self.collection = client[database]["messages"]
        super().__init__(address, MessageHandler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        LOG.error("TCP handler failed", exc_info=True)


class MessageHandler(BaseRequestHandler):
    def handle(self) -> None:
        server = cast(MessageServer, self.server)
        connection = cast(socket.socket, self.request)
        connection.settimeout(IO_TIMEOUT)
        started = perf_counter()
        token = REQUEST_ID.set(str(uuid4()))
        outcome = "incomplete"
        try:
            try:
                packet = receive_frame(
                    connection,
                    deadline=started + server.receive_timeout,
                    max_size=MAX_BODY + REQUEST_HEADER.size,
                )
                request_id, payload = decode_request(packet)
                REQUEST_ID.set(request_id)
                LOG.info("tcp_request_started", extra={"request_id": request_id})
                response = self.database_operation(payload)
            except ValueError:
                response = b"INVALID"
            except OSError:
                return
            try:
                connection.settimeout(IO_TIMEOUT)
                send_frame(connection, response)
                outcome = response.decode("ascii")
            except OSError:
                outcome = "acknowledgement_lost"
        except Exception:
            outcome = "handler_error"
            LOG.error(
                "TCP handler failed",
                exc_info=True,
                extra={"request_id": REQUEST_ID.get()},
            )
        finally:
            LOG.log(
                logging.INFO if outcome == "OK" else logging.WARNING,
                "tcp_request_completed",
                extra={
                    "request_id": REQUEST_ID.get(),
                    "outcome": outcome,
                    "duration_ms": round((perf_counter() - started) * 1000, 3),
                },
            )
            REQUEST_ID.reset(token)

    def database_operation(self, payload: bytes) -> bytes:
        server = cast(MessageServer, self.server)
        received_at = datetime.now().isoformat(sep=" ", timespec="microseconds")
        # Invalid forms never produce a database-operation event.
        data = parse_message(payload) if payload else None
        started = perf_counter()
        outcome = "unconfirmed" if payload else "unavailable"
        try:
            if payload:
                assert data is not None
                result = server.collection.insert_one({"date": received_at, **data})
                if not result.acknowledged:
                    raise PyMongoError("Write was not acknowledged")
                outcome = "acknowledged"
            else:
                # Empty form bytes in the envelope identify a readiness probe.
                server.client.admin.command("ping")
                outcome = "ok"
            return b"OK"
        except PyMongoError:
            return b"UNAVAILABLE"
        finally:
            LOG.log(
                logging.INFO if outcome in {"acknowledged", "ok"} else logging.WARNING,
                "database_operation_completed",
                extra={
                    "request_id": REQUEST_ID.get(),
                    "operation": "insert" if payload else "ping",
                    "outcome": outcome,
                    "duration_ms": round((perf_counter() - started) * 1000, 3),
                },
            )


class WebServer(BoundedThreads, HTTPServer):
    server_address: tuple[str, int]
    daemon_threads = False

    def __init__(
        self,
        address: tuple[str, int],
        socket_address: tuple[str, int],
        *,
        socket_timeout: float = IO_TIMEOUT + 3,
        max_connections: int = 32,
        receive_timeout: float = IO_TIMEOUT,
    ) -> None:
        self.configure_limits(max_connections, receive_timeout)
        self.socket_address = socket_address
        self.socket_timeout = socket_timeout
        super().__init__(address, WebHandler)

    def reject_connection(self, request: socket.socket) -> None:
        request_id = str(uuid4())
        body = json.dumps(API_RESPONSES[503]).encode()
        headers = (
            "HTTP/1.0 503 Service Unavailable\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\nRetry-After: 1\r\n"
            "Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n"
            f"X-Request-ID: {request_id}\r\n\r\n"
        ).encode()
        try:
            request.settimeout(0.05)
            request.sendall(headers + body)
        except OSError:
            pass
        LOG.warning("HTTP capacity exhausted", extra={"request_id": request_id})

    def forward(self, payload: bytes) -> bytes:
        return forward_message(
            payload, self.socket_address, timeout=self.socket_timeout
        )

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Setup/cleanup can also fail outside handle_one_request.
        if not getattr(sys.exception(), "_request_logged", False):
            LOG.error(
                "HTTP handler failed outside dispatch",
                exc_info=True,
                extra={"request_id": str(uuid4())},
            )


class WebHandler(BaseHTTPRequestHandler):
    server_version = "GoITMessages/1.0"

    def handle_one_request(self) -> None:
        self.request_id = str(uuid4())
        self.response_status: int | None = None
        self.request_failed = False
        self.raw_requestline = b""
        token = REQUEST_ID.set(self.request_id)
        complete = False
        started = perf_counter()
        try:
            super().handle_one_request()
            complete = self.response_status is not None and not self.request_failed
        except Exception as error:
            LOG.error(
                "HTTP handler failed",
                exc_info=True,
                extra={"request_id": self.request_id},
            )
            error.__dict__["_request_logged"] = True
            raise
        finally:
            if self.raw_requestline or self.response_status is not None:
                try:
                    path = urlsplit(getattr(self, "path", "")).path
                except ValueError:
                    path = "<invalid>"
                route = (
                    path
                    if path in (*HTML_PAGES, "/health", "/api/message", "/openapi.json")
                    else "<static-or-unmatched>"
                )
                method = getattr(self, "command", "")
                status = self.response_status or 500
                level = logging.ERROR if not complete or status >= 500 else logging.INFO
                if complete and 400 <= status < 500:
                    level = logging.WARNING
                LOG.log(
                    level,
                    "request_completed" if complete else "request_incomplete",
                    extra={
                        "request_id": self.request_id,
                        "method": method
                        if method in ("GET", "HEAD", "POST")
                        else "OTHER",
                        "path": route,
                        "status": status,
                        "duration_ms": round((perf_counter() - started) * 1000, 3),
                    },
                )
            REQUEST_ID.reset(token)

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        supplied = self.headers.get("X-Request-ID", "")
        try:
            if len(supplied) != 36:
                raise ValueError("Expected UUID")
            self.request_id = str(UUID(supplied))
        except ValueError:
            pass
        REQUEST_ID.set(self.request_id)
        LOG.info("request_started", extra={"request_id": self.request_id})
        return True

    def send_response(self, code: int, message: str | None = None) -> None:
        self.connection.settimeout(IO_TIMEOUT)
        self.response_status = code
        super().send_response(code, message)
        self.send_header("X-Request-ID", self.request_id)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        # handle_one_request writes exactly one completed access record.
        pass

    def setup(self) -> None:
        self.request.settimeout(IO_TIMEOUT)
        super().setup()
        self.rfile.close()
        self.rfile = BufferedReader(
            DeadlineReader(
                self.connection, cast(WebServer, self.server).receive_timeout
            )
        )

    def reply(
        self,
        status: int,
        content: bytes,
        content_type: str,
        location: str | None = None,
        *,
        api_docs: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Vary", "Accept")
        self.send_header(
            "Content-Security-Policy",
            content_security_policy(
                content if content_type.startswith("text/html") else b"", api_docs
            ),
        )
        if location:
            self.send_header("Location", location)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def json_reply(self, status: int, data: dict[str, str]) -> None:
        self.reply(status, json.dumps(data).encode(), "application/json; charset=utf-8")

    def wants_json(self) -> bool:
        return "application/json" in self.headers.get("Accept", "")

    def form_error(self, status: int, code: str) -> None:
        if self.wants_json():
            self.json_reply(status, {"error": code})
        else:
            filename = "invalid" if code == "invalid_message" else "unavailable"
            self.reply(
                status,
                (FRONTEND / "message" / f"{filename}.html").read_bytes(),
                "text/html; charset=utf-8",
            )

    def not_found(self) -> None:
        self.reply(
            404, (FRONTEND / "error.html").read_bytes(), "text/html; charset=utf-8"
        )

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/openapi.json":
            self.reply(
                200,
                json.dumps(openapi_schema()).encode(),
                "application/json; charset=utf-8",
            )
        elif path == "/health":
            try:
                ok = cast(WebServer, self.server).forward(b"")
                status = 200 if ok == b"OK" else 503
            except (OSError, ValueError):
                status = 503
            self.reply(
                status, b"ok\n" if status == 200 else b"unavailable\n", "text/plain"
            )
        elif path in ("/error.html", "/404.html"):
            self.not_found()
        elif file := static_file(path):
            content_type = (
                mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            )
            if content_type.startswith("text/"):
                content_type += "; charset=utf-8"
            self.reply(
                200, file.read_bytes(), content_type, api_docs=file.name == "docs.html"
            )
        else:
            self.not_found()

    def do_POST(self) -> None:
        if urlsplit(self.path).path not in ("/message", "/api/message"):
            self.not_found()
            return
        if self.headers.get("Transfer-Encoding"):
            self.reply(400, b"Transfer-Encoding is not supported", "text/plain")
            return
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            self.reply(411, b"Content-Length is required", "text/plain")
            return
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
            self.reply(400, b"Invalid Content-Length", "text/plain")
            return
        # Bound digit count before int(): huge numeric headers must not crash us.
        normalized_length = lengths[0].lstrip("0") or "0"
        if len(normalized_length) > 5:
            self.reply(413, b"Request body is too large", "text/plain")
            return
        size = int(normalized_length)
        if size > MAX_BODY:
            self.reply(413, b"Request body is too large", "text/plain")
            return
        if self.headers.get_content_type() != "application/x-www-form-urlencoded":
            self.reply(415, b"Expected a URL-encoded form", "text/plain")
            return
        try:
            payload = self.rfile.read(size)
        except TimeoutError:
            self.reply(408, b"Request body timed out", "text/plain")
            return
        if len(payload) != size or not payload:
            self.form_error(400, "invalid_message")
            return
        try:
            result = cast(WebServer, self.server).forward(payload)
        except (OSError, ValueError):
            result = b"UNAVAILABLE"
        if result == b"OK":
            if self.wants_json():
                self.json_reply(201, API_RESPONSES[201])
            else:
                self.reply(303, b"", "text/plain", location="/message/sent")
        elif result == b"INVALID":
            self.form_error(400, "invalid_message")
        else:
            self.form_error(503, "storage_unavailable")

    def log_message(self, format: str, *args: Any) -> None:
        if any(isinstance(arg, TimeoutError) for arg in args):
            self.request_failed = True
        # Do not log request bodies, URIs, credentials or user messages.
        LOG.warning("HTTP protocol error", extra={"request_id": self.request_id})


def mongo_client() -> MongoClient[dict[str, Any]]:
    settings = Settings.load()
    return MongoClient(
        host=settings.mongo_host,
        port=settings.mongo_port,
        serverSelectionTimeoutMS=settings.db_connect_timeout * 1000,
        connectTimeoutMS=settings.db_connect_timeout * 1000,
        socketTimeoutMS=settings.db_connect_timeout * 1000,
        timeoutMS=settings.db_connect_timeout * 1000,
        retryWrites=False,
        w=1,
        username=settings.mongo_user,
        password=settings.password,
        authSource=settings.mongo_database,
        appname=MONGO_APPLICATION_NAME,
    )


def serve(server: BaseServer, stop: Event) -> None:
    server.timeout = 0.2
    with server:
        while not STOP_REQUESTED and not stop.is_set():
            server.handle_request()


def request_stop(signum: int, frame: FrameType | None) -> None:
    # Signal handlers must not acquire multiprocessing.Event's non-reentrant
    # lock: a signal can interrupt wait()/is_set() while that lock is held.
    global STOP_REQUESTED
    STOP_REQUESTED = True


def configure_worker() -> None:
    configure_logging()
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, request_stop)


def run_socket(stop: Event) -> None:
    configure_worker()
    # MongoClient is constructed in this process, never inherited from a parent.
    try:
        settings = Settings.load()
        with mongo_client() as client:
            client.admin.command("ping")
            server = MessageServer(
                ("127.0.0.1", SOCKET_PORT),
                client,
                settings.mongo_database,
                max_connections=settings.tcp_max_connections,
                receive_timeout=settings.receive_timeout,
            )
            LOG.info("TCP server listening on port %s", SOCKET_PORT)
            serve(server, stop)
        LOG.info("MongoDB connections closed")
    except Exception:
        LOG.error("TCP worker failed", exc_info=True)
        raise SystemExit(1) from None


def run_http(stop: Event) -> None:
    configure_worker()
    try:
        settings = Settings.load()
        server = WebServer(
            ("0.0.0.0", HTTP_PORT),
            ("127.0.0.1", SOCKET_PORT),
            socket_timeout=IO_TIMEOUT + settings.db_connect_timeout + 1,
            max_connections=settings.http_max_connections,
            receive_timeout=settings.receive_timeout,
        )
        LOG.info("HTTP server listening on port %s", HTTP_PORT)
        serve(server, stop)
    except Exception:
        LOG.error("HTTP worker failed", exc_info=True)
        raise SystemExit(1) from None


def main() -> int:
    global STOP_REQUESTED
    STOP_REQUESTED = False
    try:
        settings = Settings.load()
    except ConfigurationError as error:
        print(
            json.dumps(
                {
                    "level": "ERROR",
                    "message": "Invalid configuration",
                    "setting": str(error),
                }
            ),
            file=sys.stderr,
        )
        return 1
    configure_logging()
    if not (FRONTEND / "index.html").is_file():
        raise SystemExit("Frontend export missing. Run make build-frontend first.")
    context = multiprocessing.get_context("spawn")
    http_stop = context.Event()
    socket_stop = context.Event()
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    workers = [
        context.Process(target=run_socket, args=(socket_stop,), name="socket-server"),
        context.Process(target=run_http, args=(http_stop,), name="http-server"),
    ]
    started = []
    failed = False
    try:
        for worker in workers:
            worker.start()
            started.append(worker)
        while not STOP_REQUESTED and not http_stop.wait(0.2):
            if any(not worker.is_alive() for worker in workers):
                failed = True
                break
    finally:
        LOG.info("shutdown_started")
        # Drain HTTP first: an accepted POST may still be reading its body.
        # Keep TCP/MongoDB available until those requests finish.
        for worker in reversed(started):
            (http_stop if worker.name == "http-server" else socket_stop).set()
            worker.join(
                timeout=settings.shutdown_grace_seconds - 5
                if worker.name == "http-server"
                else 5
            )
            if worker.is_alive():
                worker.kill()
                worker.join()
                failed = True
            failed = failed or worker.exitcode != 0
        LOG.info("shutdown_completed")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
