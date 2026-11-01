"""Admission control and absolute deadlines using real, deliberately slow sockets."""

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.client import HTTPConnection
from socketserver import BaseServer
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from pymongo import MongoClient

import main


@contextmanager
def running[T: BaseServer](server: T) -> Iterator[T]:
    with server:
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=2)
            assert not thread.is_alive()


def health(server: main.WebServer) -> int:
    connection = HTTPConnection(*server.server_address, timeout=2)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def test_http_rejects_excess_connections_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    original = main.WebHandler.setup

    def setup(handler: main.WebHandler) -> None:
        original(handler)
        started.set()

    monkeypatch.setattr(main.WebHandler, "setup", setup)
    with running(
        main.WebServer(("127.0.0.1", 0), ("127.0.0.1", 1), max_connections=1)
    ) as server:
        with socket.create_connection(server.server_address, timeout=2):
            assert started.wait(1)
            connection = HTTPConnection(*server.server_address, timeout=2)
            try:
                connection.request("GET", "/")
                response = connection.getresponse()
                assert response.status == 503
                assert response.getheader("Retry-After") == "1"
                assert response.getheader("X-Request-ID")
                assert response.read() == b'{"error": "storage_unavailable"}'
            finally:
                connection.close()
        deadline = time.monotonic() + 2
        while health(server) != 200:
            assert time.monotonic() < deadline
            time.sleep(0.01)


def test_tcp_rejects_excess_connections_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    original = main.MessageHandler.handle

    def handle(handler: main.MessageHandler) -> None:
        started.set()
        original(handler)

    monkeypatch.setattr(main.MessageHandler, "handle", handle)
    client = cast(MongoClient[dict[str, Any]], MagicMock())
    with running(
        main.MessageServer(("127.0.0.1", 0), client, "messages", max_connections=1)
    ) as server:
        with socket.create_connection(server.server_address, timeout=2):
            assert started.wait(1)
            with socket.create_connection(server.server_address, timeout=2) as excess:
                assert excess.recv(1) == b""
        deadline = time.monotonic() + 2
        while True:
            try:
                assert main.forward_message(b"", server.server_address) == b"OK"
                break
            except OSError:
                assert time.monotonic() < deadline
                time.sleep(0.01)


@pytest.mark.parametrize("phase", ["headers", "body", "tcp"])
def test_trickle_cannot_extend_the_total_receive_deadline(phase: str) -> None:
    client = cast(MongoClient[dict[str, Any]], MagicMock())
    server = (
        main.MessageServer(("127.0.0.1", 0), client, "messages", receive_timeout=0.3)
        if phase == "tcp"
        else main.WebServer(("127.0.0.1", 0), ("127.0.0.1", 1), receive_timeout=0.3)
    )
    prefix = {
        "headers": b"POST /api/message HTTP/1.0\r\nX-Slow: ",
        "body": (
            b"POST /api/message HTTP/1.0\r\nContent-Length: 100\r\n"
            b"Content-Type: application/x-www-form-urlencoded\r\n\r\n"
        ),
        "tcp": b"\x00\x00\x00\x64",
    }[phase]
    with running(server):
        with socket.create_connection(server.server_address, timeout=2) as connection:
            stop = threading.Event()

            def trickle() -> None:
                while not stop.wait(0.04):
                    try:
                        connection.sendall(b"x")
                    except OSError:
                        break

            connection.sendall(prefix)
            sender = threading.Thread(target=trickle)
            started = time.monotonic()
            sender.start()
            try:
                response = connection.recv(4096)
                assert time.monotonic() - started < 1.5
                if phase == "body":
                    assert response.startswith(b"HTTP/1.0 408")
                else:
                    assert response == b""
            finally:
                stop.set()
                sender.join(timeout=1)
                assert not sender.is_alive()


def test_expired_budget_prevents_another_socket_read() -> None:
    connection, peer = socket.socketpair()
    with connection, peer:
        with pytest.raises(TimeoutError):
            main.receive_exact(connection, 1, deadline=time.perf_counter() - 1)
        reader = main.DeadlineReader(connection, -1)
        with pytest.raises(TimeoutError):
            reader.readinto(bytearray(1))


def test_thread_start_failure_releases_admission_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with main.WebServer(
        ("127.0.0.1", 0), ("127.0.0.1", 1), max_connections=1
    ) as server:
        with monkeypatch.context() as patch:
            patch.setattr(
                threading.Thread, "start", MagicMock(side_effect=RuntimeError)
            )
            with pytest.raises(RuntimeError):
                server.process_request(MagicMock(), ("127.0.0.1", 1234))
        assert server.slots.acquire(blocking=False)
        server.slots.release()
