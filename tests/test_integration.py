"""Real services; every run owns an isolated Compose project, secrets and volume."""

from __future__ import annotations

import errno
import json
import os
import secrets
import socket
import statistics
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.client import HTTPConnection
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

from scripts.database_archive import ArchiveError, DatabaseArchive
from tests.write_load import Attempt, reconcile, run_stage, submit, summarize

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.integration


@dataclass
class Stack:
    command: list[str]
    environment: dict[str, str] = field(repr=False)
    port: int = 0
    passwords: tuple[str, ...] = field(default=(), repr=False)

    def redact(self, value: str) -> str:
        for password in self.passwords:
            value = value.replace(password, "[REDACTED]")
        return value

    def compose(self, *args: str, timeout: int = 180) -> str:
        result = subprocess.run(
            [*self.command, *args],
            env=self.environment,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(self.redact(result.stdout + result.stderr))
        return result.stdout.strip()

    def cleanup(self, artifacts: Path) -> None:
        try:
            logs = self.redact(self.compose("logs", "--no-color"))
            artifacts.mkdir(exist_ok=True)
            (artifacts / "integration-containers.log").write_text(logs)
        finally:
            # Log collection and disk errors must never skip resource cleanup.
            self.compose("down", "--volumes", "--remove-orphans", "--rmi", "local")

    def identity(self) -> tuple[str, dict[str, str]]:
        code = """
import json, os
from pathlib import Path

identities = {}
for path in Path("/proc").glob("[0-9]*/cmdline"):
    if int(path.parent.name) in (1, os.getpid()):
        continue
    try:
        command = path.read_bytes()
        if b"main.py" in command or b"multiprocessing.spawn" in command:
            identities[path.parent.name] = (
                (path.parent / "stat").read_text().rsplit(")", 1)[1].split()[19]
            )
    except FileNotFoundError:
        continue
print(json.dumps(identities))
"""
        identities: dict[str, str] = json.loads(
            self.compose("exec", "-T", "app", "python", "-c", code)
        )
        assert len(identities) == 3
        return self.compose("ps", "-q", "app"), identities

    def database_connections(self) -> int:
        code = r"""
const admin = db.getSiblingDB('admin');
const secret = require('fs').readFileSync('/run/secrets/mongo_admin_password', 'utf8');
admin.auth('root', secret.replace(/[\r\n]+$/, ''));
print(admin.aggregate([
  {$currentOp: {allUsers: true, idleConnections: true}},
  {$match: {'clientMetadata.application.name': 'goit-cs-hw-06'}}
]).toArray().length);
"""
        return int(
            self.compose(
                "exec", "-T", "mongodb", "mongosh", "--quiet", "--norc", "--eval", code
            ).splitlines()[-1]
        )

    def update_port(self) -> None:
        self.port = int(self.compose("port", "app", "3000").rsplit(":", 1)[1])

    def request(
        self,
        path: str,
        message: str | None = None,
        json_response: bool = False,
        timeout: float = 12,
        request_id: str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            if message is None:
                connection.request(
                    "GET",
                    path,
                    headers={"X-Request-ID": request_id} if request_id else {},
                )
            else:
                connection.request(
                    "POST",
                    path,
                    urlencode({"username": "Ірина + Pavel", "message": message}),
                    {
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Accept": "application/json" if json_response else "text/html",
                        **({"X-Request-ID": request_id} if request_id else {}),
                    },
                )
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def records(self) -> list[dict[str, Any]]:
        code = (
            "import main,json; "
            "client=main.mongo_client(); "
            "print(json.dumps(list(client.messages.messages.find({}, {'_id':0})))); "
            "client.close()"
        )
        result: list[dict[str, Any]] = json.loads(
            self.compose("exec", "-T", "app", "python", "-c", code)
        )
        return result

    def wait_ready(self) -> None:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            try:
                if self.request("/health")[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(0.2)
        raise AssertionError("Application did not recover")


@pytest.fixture(scope="module")
def stack(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> Iterator[Stack]:
    if not request.config.getoption("--integration"):
        pytest.skip("Use --integration to start Docker containers")
    directory = tmp_path_factory.mktemp("hw06-compose")
    passwords = []
    for name in ("app_password", "admin_password"):
        password = secrets.token_urlsafe(32)
        if name == "app_password":
            password = "  " + password + "  \r\n"
        (directory / name).write_text(password)
        (directory / name).chmod(0o444)
        passwords.append(password.rstrip("\r\n"))
    environment = {
        **{
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith(("MONGO_", "COMPOSE_"))
            and key.upper()
            not in {
                "APP_PORT",
                "LOG_LEVEL",
                "DB_CONNECT_TIMEOUT",
                "SHUTDOWN_GRACE_SECONDS",
                "HTTP_MAX_CONNECTIONS",
                "TCP_MAX_CONNECTIONS",
                "RECEIVE_TIMEOUT",
            }
        },
        "APP_PORT": "0",
        "MONGO_PASSWORD_PATH": str(directory / "app_password"),
        "MONGO_ADMIN_PASSWORD_PATH": str(directory / "admin_password"),
        "LOG_LEVEL": "INFO",
        "DB_CONNECT_TIMEOUT": "2",
        "SHUTDOWN_GRACE_SECONDS": "20",
        "HTTP_MAX_CONNECTIONS": "8",
        "TCP_MAX_CONNECTIONS": "8",
        "RECEIVE_TIMEOUT": "5",
    }
    service = Stack(
        [
            "docker",
            "compose",
            "--project-directory",
            str(ROOT),
            "--env-file",
            "/dev/null",
            "-p",
            "hw06-test-" + uuid.uuid4().hex[:10],
            "-f",
            str(ROOT / "docker-compose.yaml"),
        ],
        environment,
        passwords=tuple(passwords),
    )
    try:
        service.compose("up", "--build", "-d", "--wait", timeout=300)
        service.update_port()
        yield service
    finally:
        service.cleanup(ROOT / "artifacts")


def test_full_http_tcp_mongodb_roundtrip(stack: Stack) -> None:
    text = "Привіт, světe! & x=1 + 2\nДругий рядок 🌍"
    before = stack.records()
    status, headers, _ = stack.request("/message", text)
    assert status == 303 and headers["Location"] == "/message/sent"
    records = stack.records()
    assert len(records) == len(before) + 1
    record = next(record for record in records if record["message"] == text)
    assert set(record) == {"date", "username", "message"}
    assert record["username"] == "Ірина + Pavel"
    assert len(record["date"]) == 26
    datetime.strptime(record["date"], "%Y-%m-%d %H:%M:%S.%f")


def test_static_files_and_404(stack: Stack) -> None:
    for path in (
        "/",
        "/message.html",
        "/style.css",
        "/logo.png",
        "/docs",
        "/openapi.json",
        "/vendor/swagger/swagger-ui-bundle.js",
    ):
        assert stack.request(path)[0] == 200
    status, _, content = stack.request("/missing")
    assert status == 404
    assert "Сторінку не знайдено" in content.decode()


def test_processes_and_container_permissions(stack: Stack) -> None:
    lines = stack.compose("top", "app").splitlines()
    assert len([line for line in lines if "spawn_main" in line]) == 2
    runtime = json.loads(
        stack.compose(
            "exec",
            "-T",
            "app",
            "python",
            "-c",
            "import importlib.util,json,os,shutil,sys; "
            "print(json.dumps({'uid':os.getuid(),'gid':os.getgid(),"
            "'version':list(sys.version_info[:2]),"
            "'tools':{tool:shutil.which(tool) "
            "for tool in ('sh','bash','apt','dpkg','pip','uv','node','npm')},"
            "'pip':importlib.util.find_spec('pip') is not None}))",
        )
    )
    assert runtime["uid"] == runtime["gid"] == 65532
    assert runtime["version"] == [3, 13]
    assert not any(runtime["tools"].values())
    assert runtime["pip"] is False
    container = stack.compose("ps", "-q", "app")
    result = subprocess.run(
        ["docker", "inspect", container],
        capture_output=True,
        text=True,
        check=True,
    )
    config = json.loads(result.stdout)[0]
    assert config["HostConfig"]["ReadonlyRootfs"] is True
    assert config["HostConfig"]["CapDrop"] == ["ALL"]
    assert config["HostConfig"]["NanoCpus"] == 1_000_000_000
    assert config["HostConfig"]["Memory"] == 256 * 1024 * 1024
    assert config["HostConfig"]["PidsLimit"] == 384
    mongo_container = stack.compose("ps", "-q", "mongodb")
    result = subprocess.run(
        ["docker", "inspect", mongo_container],
        capture_output=True,
        text=True,
        check=True,
    )
    assert not json.loads(result.stdout)[0]["HostConfig"]["PortBindings"]


def test_concurrent_messages(stack: Stack) -> None:
    messages = ["concurrent-" + uuid.uuid4().hex for _ in range(12)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(
            pool.map(lambda message: stack.request("/message", message)[0], messages)
        )
    assert statuses == [303] * len(messages)
    saved = [record["message"] for record in stack.records()]
    assert all(saved.count(message) == 1 for message in messages)


def test_failed_validation_does_not_write(stack: Stack) -> None:
    before = stack.records()
    assert stack.request("/message", " ")[0] == 400
    assert stack.records() == before


def test_database_outage_and_recovery(stack: Stack) -> None:
    before = stack.records()
    assert stack.request("/health")[0] == 200
    identity = stack.identity()
    code = """
import json, main

with main.mongo_client() as client:
    client.admin.command("ping")
    print(
        json.dumps(
            {
                "timeout": client.options.timeout,
                "selection": client.options.server_selection_timeout,
            }
        )
    )
"""
    actual = json.loads(stack.compose("exec", "-T", "app", "python", "-c", code))
    assert actual == {"timeout": 2, "selection": 2}
    budget = actual["timeout"] + 3
    stack.compose("stop", "mongodb")
    try:
        for _ in range(2):
            started = time.monotonic()
            assert stack.request("/health", timeout=budget)[0] == 503
            assert time.monotonic() - started < budget
        assert stack.identity() == identity
        assert stack.request("/docs", timeout=budget)[0] == 200
        started = time.monotonic()
        status, _, body = stack.request(
            "/api/message", "must-not-be-saved", json_response=True, timeout=budget
        )
        assert time.monotonic() - started < budget
        assert status == 503
        assert json.loads(body) == {"error": "storage_unavailable"}
    finally:
        stack.compose("start", "mongodb")
        stack.wait_ready()
    assert stack.records() == before
    assert stack.request("/message", "recovered")[0] == 303
    assert stack.identity() == identity


def test_volume_survives_container_recreation(stack: Stack) -> None:
    text = "persistent-" + uuid.uuid4().hex
    assert stack.request("/message", text)[0] == 303
    before = stack.records()
    stack.compose("down")
    stack.compose("up", "-d", "--wait")
    stack.update_port()
    assert stack.records() == before
    assert stack.request("/health")[0] == 200


def docker_output(stack: Stack, *arguments: str) -> str:
    result = subprocess.run(
        ["docker", *arguments],
        env=stack.environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, stack.redact(result.stdout + result.stderr)
    return result.stdout.strip()


def assert_clean_shutdown(stack: Stack, container: str, since: str) -> None:
    inspection = json.loads(
        docker_output(
            stack,
            "inspect",
            "--format",
            '{"state":{{json .State}},"stop_signal":{{json .Config.StopSignal}},'
            '"stop_timeout":{{json .Config.StopTimeout}}}',
            container,
        )
    )
    # Docker defaults to SIGTERM when no explicit StopSignal is configured.
    assert (inspection["stop_signal"] or "SIGTERM") in {"SIGTERM", "15"}
    assert inspection["stop_timeout"] == 130
    state = inspection["state"]
    assert state["Status"] == "exited"
    assert state["Pid"] == 0
    assert state["Running"] is False
    assert state["OOMKilled"] is False
    assert state["Error"] == ""
    assert state["ExitCode"] == 0

    events = [
        json.loads(line)
        for line in docker_output(
            stack,
            "events",
            "--since",
            since,
            "--until",
            datetime.now(UTC).isoformat(),
            "--filter",
            f"container={container}",
            "--format",
            "{{json .}}",
        ).splitlines()
    ]
    signals = [
        event["Actor"]["Attributes"]["signal"]
        for event in events
        if event["Action"] == "kill"
    ]
    assert signals in (["15"], ["SIGTERM"]), signals
    deaths = [event for event in events if event["Action"] == "die"]
    assert len(deaths) == 1, deaths
    assert int(deaths[0]["Actor"]["Attributes"]["exitCode"]) == state["ExitCode"]
    assert not any(event["Action"] == "oom" for event in events)


def test_graceful_sigterm(stack: Stack) -> None:
    container = stack.compose("ps", "-q", "app")
    assert container
    since = datetime.now(UTC).isoformat()
    try:
        stack.compose("stop", "app")
        assert_clean_shutdown(stack, container, since)
    finally:
        stack.compose("start", "app")
        stack.update_port()
        stack.wait_ready()


def test_worker_failure_stops_sibling_and_exits_nonzero(stack: Stack) -> None:
    code = (
        "import os,signal; from pathlib import Path; "
        "pids=[int(p.parent.name) for p in Path('/proc').glob('[0-9]*/cmdline') "
        "if b'multiprocessing.spawn import spawn_main' in p.read_bytes() "
        "and int(p.parent.name) != os.getpid()]; "
        "os.kill(min(pids), signal.SIGKILL)"
    )
    stack.compose("exec", "-T", "app", "python", "-c", code)
    container = stack.compose("ps", "--all", "-q", "app")
    result = subprocess.run(
        ["docker", "wait", container],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    assert result.stdout.strip() == "1"
    stack.compose("start", "app")
    stack.update_port()
    stack.wait_ready()


def test_react_json_contract_and_static_export(stack: Stack) -> None:
    message = "react-api-" + uuid.uuid4().hex
    status, _, body = stack.request("/api/message", message, json_response=True)
    assert status == 201 and json.loads(body) == {"status": "saved"}
    assert any(record["message"] == message for record in stack.records())
    assert stack.request("/message.txt")[0] == 200
    _, headers, page = stack.request("/message.html")
    assert b"/_next/static/" in page
    assert "'sha256-" in headers["Content-Security-Policy"]


def app_logs(stack: Stack) -> list[dict[str, Any]]:
    text = stack.compose("logs", "--no-color", "--no-log-prefix", "app")
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


def wait_log(stack: Stack, message: str, request_id: str | None = None) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if any(
            record["message"] == message
            and (request_id is None or record.get("request_id") == request_id)
            for record in app_logs(stack)
        ):
            return
        time.sleep(0.1)
    raise AssertionError(f"Missing log event: {message}")


def test_proc_permissions_and_readonly_filesystem(stack: Stack) -> None:
    code = """
import json, os
from pathlib import Path

threads = []
workers = []
for path in Path("/proc").glob("[0-9]*/cmdline"):
    pid = int(path.parent.name)
    if pid == os.getpid():
        continue
    try:
        command = path.read_bytes()
    except FileNotFoundError:
        continue
    if b"multiprocessing.spawn import spawn_main" in command:
        workers.append(pid)
    if pid == 1 or b"main.py" in command or b"multiprocessing." in command:
        for status in (path.parent / "task").glob("*/status"):
            try:
                fields = dict(
                    line.split(":", 1) for line in status.read_text().splitlines()
                )
            except FileNotFoundError:
                continue
            keys = (
                "Uid",
                "Gid",
                "CapInh",
                "CapPrm",
                "CapEff",
                "CapBnd",
                "CapAmb",
                "NoNewPrivs",
            )
            threads.append({"pid": pid, **{key: fields[key].strip() for key in keys}})
write_errors = {}
for target in ("/app/main.py", "/tmp/.readonly-probe"):
    flags = (
        os.O_WRONLY
        if target.endswith("main.py")
        else os.O_WRONLY | os.O_CREAT | os.O_EXCL
    )
    try:
        descriptor = os.open(target, flags, 0o600)
    except OSError as error:
        write_errors[target] = error.errno
    else:
        os.close(descriptor)
        if target.startswith("/tmp/"):
            Path(target).unlink()
        write_errors[target] = None
mounts = [
    line.split()[5].split(",")
    for line in Path("/proc/1/mountinfo").read_text().splitlines()
    if line.split()[4] == "/"
]
print(
    json.dumps(
        {
            "workers": workers,
            "threads": threads,
            "mounts": mounts,
            "write_errors": write_errors,
            "admin_secret": Path("/run/secrets/mongo_admin_password").exists(),
        }
    )
)
"""
    data = json.loads(stack.compose("exec", "-T", "app", "python", "-c", code))
    assert len(data["workers"]) == 2
    assert len({thread["pid"] for thread in data["threads"]}) >= 4
    assert 1 in {thread["pid"] for thread in data["threads"]}
    for thread in data["threads"]:
        assert set(thread["Uid"].split()) == set(thread["Gid"].split()) == {"65532"}
        assert all(
            int(thread[field], 16) == 0
            for field in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
        )
        assert thread["NoNewPrivs"] == "1"
    assert data["mounts"] and all("ro" in mount for mount in data["mounts"])
    assert data["write_errors"] == {
        "/app/main.py": errno.EROFS,
        "/tmp/.readonly-probe": errno.EROFS,
    }
    assert not data["admin_secret"]


def test_structured_logs_do_not_leak_form_or_headers(stack: Stack) -> None:
    canary = "privacy-" + uuid.uuid4().hex
    request_id = str(uuid.uuid4())
    connection = HTTPConnection("127.0.0.1", stack.port, timeout=12)
    try:
        connection.request(
            "POST",
            "/api/message?token=" + canary,
            urlencode({"username": canary, "message": canary}),
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "Authorization": canary,
                "X-Request-ID": request_id,
            },
        )
        response = connection.getresponse()
        assert response.status == 201
        assert response.getheader("X-Request-ID") == request_id
        response.read()
    finally:
        connection.close()
    wait_log(stack, "request_completed", request_id)
    logs = app_logs(stack)
    assert canary not in json.dumps(logs)
    completed = [
        record
        for record in logs
        if record["message"] == "request_completed"
        and record.get("request_id") == request_id
    ]
    assert len(completed) == 1
    assert completed[0]["status"] == 201 and completed[0]["duration_ms"] >= 0
    assert completed[0]["path"] == "/api/message"


def test_real_browser_submissions_with_and_without_javascript(stack: Stack) -> None:
    before = stack.records()
    result = subprocess.run(
        ["pnpm", "--dir", str(ROOT / "frontend"), "test"],
        env={**os.environ, "FRONTEND_BASE_URL": f"http://127.0.0.1:{stack.port}"},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    (ROOT / "artifacts").mkdir(exist_ok=True)
    (ROOT / "artifacts" / "browser-integration.log").write_text(
        result.stdout + result.stderr
    )
    assert result.returncode == 0, result.stdout + result.stderr
    after = stack.records()
    assert len(after) == len(before) + 6
    created = [record for record in after if record not in before]
    assert len({record["username"] for record in created}) == 6
    assert all(
        record["message"]
        == (
            "Привіт & + = 🌍\n<script>private</script>"
            if record["username"].startswith("browser-js-")
            else "Привіт & + = 🌍\r\n<script>private</script>"
        )
        for record in created
    )


def test_sigterm_drains_an_inflight_post_before_stopping_tcp(stack: Stack) -> None:
    import socket
    from http.client import HTTPResponse

    marker = "shutdown-" + uuid.uuid4().hex
    request_id = str(uuid.uuid4())
    body = urlencode({"username": "graceful", "message": marker}).encode()
    assert stack.database_connections() > 0
    container = stack.compose("ps", "-q", "app")
    assert container
    log_offset = len(app_logs(stack))
    with socket.create_connection(("127.0.0.1", stack.port), timeout=15) as connection:
        headers = (
            f"POST /api/message HTTP/1.0\r\nContent-Length: {len(body)}\r\n"
            "Content-Type: application/x-www-form-urlencoded\r\n"
            "Accept: application/json\r\n"
            f"X-Request-ID: {request_id}\r\n\r\n"
        ).encode()
        connection.sendall(headers + body[:1])
        wait_log(stack, "request_started", request_id)
        # A unique marker is needed because earlier tests also stopped the app.
        before = sum(
            record["message"] == "shutdown_started" for record in app_logs(stack)
        )
        since = datetime.now(UTC).isoformat()
        started = time.monotonic()
        with subprocess.Popen(
            [*stack.command, "stop", "app"],
            env=stack.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            try:
                deadline = time.monotonic() + 5
                while (
                    sum(
                        record["message"] == "shutdown_started"
                        for record in app_logs(stack)
                    )
                    <= before
                ):
                    assert time.monotonic() < deadline, (
                        "Supervisor did not receive SIGTERM"
                    )
                    time.sleep(0.1)
                connection.sendall(body[1:])
                response = HTTPResponse(connection)
                response.begin()
                assert response.status == 201
                assert json.loads(response.read()) == {"status": "saved"}
                process.communicate(timeout=20)
                assert process.returncode == 0
                assert time.monotonic() - started < 20
                assert_clean_shutdown(stack, container, since)
                deadline = time.monotonic() + 5
                while stack.database_connections() != 0:
                    assert time.monotonic() < deadline, (
                        "Database connections remain after shutdown"
                    )
                    time.sleep(0.1)
                logs = app_logs(stack)[log_offset:]
                completed = next(
                    index
                    for index, record in enumerate(logs)
                    if record["message"] == "request_completed"
                    and record.get("request_id") == request_id
                )
                closed = next(
                    index
                    for index, record in enumerate(logs)
                    if record["message"] == "MongoDB connections closed"
                )
                stopped = next(
                    index
                    for index, record in enumerate(logs)
                    if record["message"] == "shutdown_completed"
                )
                assert completed < closed < stopped
            finally:
                process.communicate(timeout=25)
                stack.compose("start", "app")
                stack.update_port()
                stack.wait_ready()
    assert sum(record["message"] == marker for record in stack.records()) == 1


def test_database_credentials_and_least_privilege(stack: Stack) -> None:
    code = """
import json, main
from pymongo import MongoClient
from pymongo.errors import OperationFailure

settings = main.Settings.load()
errors = []
for username, database in (
    (settings.mongo_user, settings.mongo_database),
    ("root", "admin"),
):
    with MongoClient(
        settings.mongo_host,
        username=username,
        password="intentionally-wrong",
        authSource=database,
        serverSelectionTimeoutMS=2000,
    ) as client:
        try:
            client.admin.command("ping")
        except OperationFailure as error:
            errors.append(error.code)
        else:
            errors.append(None)
with main.mongo_client() as client:
    roles = client.admin.command("connectionStatus")["authInfo"][
        "authenticatedUserRoles"
    ]
    for database, command in [
        ("admin", {"usersInfo": 1}),
        ("forbidden", {"insert": "messages", "documents": [{"probe": True}]}),
        ("messages", {"createUser": "forbidden-user", "pwd": "unused", "roles": []}),
    ]:
        try:
            client[database].command(command)
        except OperationFailure as error:
            errors.append(error.code)
        else:
            errors.append(None)
print(json.dumps({"errors": errors, "roles": roles}))
"""
    state = json.loads(stack.compose("exec", "-T", "app", "python", "-c", code))
    assert state["errors"] == [18, 18, 13, 13, 13]
    assert state["roles"] == [{"role": "readWrite", "db": "messages"}]


def test_database_initialization_is_idempotent(stack: Stack) -> None:
    code = r"""
const admin = db.getSiblingDB('admin');
const secret = require('fs').readFileSync('/run/secrets/mongo_admin_password','utf8');
admin.auth('root', secret.replace(/[\r\n]+$/, ''));
const before = db.getSiblingDB('messages').getUser('messages_app');
load('/docker-entrypoint-initdb.d/10-app.js');
const after = db.getSiblingDB('messages').getUser('messages_app');
print(JSON.stringify({
  same: JSON.stringify(before) === JSON.stringify(after),
  root: admin.runCommand({usersInfo:'root'}).ok
}));
"""
    state = json.loads(
        stack.compose(
            "exec", "-T", "mongodb", "mongosh", "--quiet", "--norc", "--eval", code
        ).splitlines()[-1]
    )
    assert state == {"same": True, "root": 1}


def test_lost_acknowledgement_never_retries_the_write(stack: Stack) -> None:
    marker = "lost-ack-" + uuid.uuid4().hex
    request_id = str(uuid.uuid4())
    code = r"""
import json, socket, threading
from http.client import HTTPConnection
from socketserver import BaseRequestHandler, TCPServer
from urllib.parse import urlencode
import main

forwarded = []
class DropAcknowledgement(BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        payload = main.receive_frame(
            self.request, max_size=main.MAX_BODY + main.REQUEST_HEADER.size
        )
        with socket.create_connection(('127.0.0.1', 5000), timeout=5) as backend:
            main.send_frame(backend, payload)
            forwarded.append(main.receive_frame(backend).decode())
        # The real MongoDB insert succeeded. Close without forwarding its ACK.

with TCPServer(('127.0.0.1', 0), DropAcknowledgement) as proxy:
    with main.WebServer(('127.0.0.1', 0), proxy.server_address) as http:
        workers = [threading.Thread(target=server.serve_forever)
                   for server in (proxy, http)]
        for worker in workers: worker.start()
        connection = HTTPConnection(*http.server_address, timeout=8)
        try:
            connection.request('POST', '/api/message', urlencode({
                'username': 'lost-ack', 'message': MARKER
            }), {'Content-Type': 'application/x-www-form-urlencoded',
                 'Accept': 'application/json', 'X-Request-ID': IDENTIFIER})
            response = connection.getresponse()
            print(json.dumps({'status': response.status,
                              'body': json.loads(response.read()),
                              'request_id': response.getheader('X-Request-ID'),
                              'forwarded': forwarded}))
        finally:
            connection.close()
            http.shutdown()
            proxy.shutdown()
            for worker in workers: worker.join()
""".replace("MARKER", json.dumps(marker)).replace("IDENTIFIER", json.dumps(request_id))
    result = json.loads(stack.compose("exec", "-T", "app", "python", "-c", code))
    assert result == {
        "status": 503,
        "body": {"error": "storage_unavailable"},
        "forwarded": ["OK"],
        "request_id": request_id,
    }
    assert sum(record["message"] == marker for record in stack.records()) == 1
    accounting = reconcile(
        [Attempt(request_id, marker, 503, "unconfirmed", 0)],
        stack.records(),
        "lost-ack",
    )
    assert accounting["unconfirmed_stored"] == 1
    assert accounting["unconfirmed_absent"] == 0
    wait_log(stack, "database_operation_completed", request_id)
    operations = [
        entry
        for entry in app_logs(stack)
        if entry.get("request_id") == request_id
        and entry["message"] == "database_operation_completed"
    ]
    assert len(operations) == 1
    assert operations[0]["operation"] == "insert"
    assert operations[0]["outcome"] == "acknowledged"
    assert stack.request("/health")[0] == 200


def test_archive_lock_blocks_cli_without_changing_running_services(
    stack: Stack,
    tmp_path: Path,
) -> None:
    archive = DatabaseArchive(stack.command, stack.environment)
    project = stack.command[stack.command.index("-p") + 1]
    identity = stack.identity()
    records = stack.records()
    destination = tmp_path / "blocked"
    with archive.operation_lock():
        for operation in ("backup", "restore"):
            arguments = [
                sys.executable,
                "-m",
                "scripts.database_archive",
                operation,
                str(destination),
                "--project-name",
                project,
            ]
            if operation == "restore":
                arguments.extend(["--database", "restore_blocked"])
            result = subprocess.run(
                arguments,
                cwd=ROOT,
                env=stack.environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert result.returncode == 1
            assert "Another backup or restore is already running" in result.stdout
            assert not result.stderr
            assert not destination.exists()
        assert stack.identity() == identity
        assert stack.records() == records
        assert stack.request("/health")[0] == 200


def test_backup_restores_exact_documents_into_a_new_database(
    stack: Stack,
    tmp_path: Path,
) -> None:
    marker = "backup-" + uuid.uuid4().hex + " Привіт 🌍\nsecond line"
    assert stack.request("/api/message", marker, json_response=True)[0] == 201
    archive = DatabaseArchive(stack.command, stack.environment)
    directory = tmp_path / "backup"
    before = stack.records()
    app_container = stack.identity()[0]
    archive.backup(directory)
    stack.update_port()
    stack.wait_ready()
    assert stack.identity()[0] == app_container
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "archive.bson").stat().st_mode & 0o777 == 0o600
    target = "restore_" + uuid.uuid4().hex
    archive.restore(directory, target)
    code = r"""
const admin = db.getSiblingDB('admin');
const secret = require('fs').readFileSync('/run/secrets/mongo_admin_password','utf8');
admin.auth('root', secret.replace(/[\r\n]+$/, ''));
const original = db.getSiblingDB('messages').messages.find().sort({_id: 1}).toArray();
const restored = db.getSiblingDB(TARGET).messages.find().sort({_id: 1}).toArray();
print(JSON.stringify({same: EJSON.stringify(original) === EJSON.stringify(restored),
                      count: restored.length}));
""".replace("TARGET", json.dumps(target))
    comparison = json.loads(
        stack.compose(
            "exec", "-T", "mongodb", "mongosh", "--quiet", "--norc", "--eval", code
        ).splitlines()[-1]
    )
    assert comparison == {"same": True, "count": len(before)}
    with pytest.raises(ArchiveError):
        archive.restore(directory, target)
    assert stack.records() == before
    # The restore namespace belongs to this disposable Compose project.
    cleanup = (
        code.split("const original")[0]
        + f"db.getSiblingDB({json.dumps(target)}).dropDatabase();"
    )
    stack.compose(
        "exec", "-T", "mongodb", "mongosh", "--quiet", "--norc", "--eval", cleanup
    )


def test_bounded_load_and_recovery_report(stack: Stack) -> None:
    def sample() -> dict[str, int]:
        code = """
import json
from pathlib import Path
print(json.dumps({
  'memory_bytes': int(Path('/sys/fs/cgroup/memory.current').read_text()),
  'tasks': int(Path('/sys/fs/cgroup/pids.current').read_text())
}))
"""
        result: dict[str, int] = json.loads(
            stack.compose("exec", "-T", "app", "python", "-c", code)
        )
        return result

    def measured_request(_: int) -> tuple[int, float]:
        started = time.monotonic()
        status = stack.request("/health")[0]
        return status, (time.monotonic() - started) * 1000

    baseline = sample()
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(measured_request, range(48)))
    assert all(status in (200, 503) for status, _ in results)
    assert any(status == 200 for status, _ in results)
    held = []
    try:
        for _ in range(8):
            request_id = str(uuid.uuid4())
            connection = socket.create_connection(("127.0.0.1", stack.port), timeout=5)
            held.append(connection)
            connection.sendall(
                (
                    "POST /api/message HTTP/1.0\r\nContent-Length: 100\r\n"
                    "Content-Type: application/x-www-form-urlencoded\r\n"
                    f"X-Request-ID: {request_id}\r\n\r\nx"
                ).encode()
            )
            wait_log(stack, "request_started", request_id)
        saturated = sample()
        rejected = [measured_request(index) for index in range(8)]
        assert all(status == 503 for status, _ in rejected)
        assert max(duration for _, duration in rejected) < 1000
        assert saturated["tasks"] - baseline["tasks"] <= 12
        assert saturated["memory_bytes"] < 256 * 1024 * 1024
    finally:
        for connection in held:
            connection.close()
    started = time.monotonic()
    stack.wait_ready()
    recovery_ms = (time.monotonic() - started) * 1000
    durations = [duration for _, duration in results]
    report = {
        "requests": len(results),
        "concurrency": 16,
        "http_capacity": 8,
        "responses": {
            str(code): sum(status == code for status, _ in results)
            for code in (200, 503)
        },
        "latency_ms": {
            "p50": statistics.median(durations),
            "p95": statistics.quantiles(durations, n=20)[18],
            "max": max(durations),
        },
        "baseline": baseline,
        "saturated": saturated,
        "overload_rejections": len(rejected),
        "recovery_ms": recovery_ms,
        "scope": "Disposable Linux container; host-driven sample, not a production SLA",
    }
    (ROOT / "artifacts").mkdir(exist_ok=True)
    (ROOT / "artifacts" / "load-report.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )


def test_request_ids_cross_real_http_tcp_and_database_operations(stack: Stack) -> None:
    identifiers = [str(uuid.uuid4()) for _ in range(6)]
    messages = ["correlation-private-" + uuid.uuid4().hex for _ in identifiers]
    messages[-1] = " "  # Validation fails inside the TCP worker; no insert follows.

    def send(index: int) -> tuple[int, dict[str, str], bytes]:
        return stack.request(
            "/api/message",
            messages[index],
            json_response=True,
            request_id=identifiers[index],
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(send, range(6)))
    for i, (status, headers, _) in enumerate(responses):
        assert status == (400 if i == 5 else 201)
        assert headers["X-Request-ID"] == identifiers[i]
        wait_log(stack, "tcp_request_completed", identifiers[i])
        wait_log(stack, "request_completed", identifiers[i])
    health_id = str(uuid.uuid4())
    assert stack.request("/health", request_id=health_id)[0] == 200
    wait_log(stack, "tcp_request_completed", health_id)
    logs = app_logs(stack)
    for i, identifier in enumerate([*identifiers, health_id]):
        events = [entry for entry in logs if entry.get("request_id") == identifier]
        for name in ("request_started", "tcp_request_started", "tcp_request_completed"):
            assert sum(entry["message"] == name for entry in events) == 1
        database = [e for e in events if e["message"] == "database_operation_completed"]
        if i == 5:
            assert not database
        else:
            assert len(database) == 1
            assert database[0]["operation"] == ("ping" if i == 6 else "insert")
            assert database[0]["outcome"] == ("ok" if i == 6 else "acknowledged")
            assert database[0]["duration_ms"] >= 0
        http = next(e for e in events if e["message"] == "request_started")
        tcp = next(e for e in events if e["message"] == "tcp_request_started")
        assert http["process"] != tcp["process"]
    records = stack.records()
    for message in messages[:-1]:
        assert sum(record["message"] == message for record in records) == 1
        assert message not in json.dumps(logs)


def test_real_write_load_and_database_reconciliation(
    stack: Stack, request: pytest.FixtureRequest
) -> None:
    seconds = request.config.getoption("--write-load-seconds")
    username = "load-" + uuid.uuid4().hex
    runtime = json.loads(
        stack.compose(
            "exec",
            "-T",
            "app",
            "python",
            "-c",
            "import json,platform; print(json.dumps({'os':platform.system(), "
            "'architecture':platform.machine(), 'python':platform.python_version()}))",
        )
    )
    clock_started = time.monotonic()
    phase = "baseline"
    samples: list[dict[str, Any]] = []
    sampling_errors: list[str] = []
    stop = threading.Event()

    def sample() -> dict[str, Any]:
        code = """
import json
from pathlib import Path
print(json.dumps({
  'memory_bytes': int(Path('/sys/fs/cgroup/memory.current').read_text()),
  'tasks': int(Path('/sys/fs/cgroup/pids.current').read_text())
}))
"""
        current_phase = phase
        resources: dict[str, Any] = json.loads(
            stack.compose("exec", "-T", "app", "python", "-c", code, timeout=10)
        )
        return {
            "elapsed_seconds": time.monotonic() - clock_started,
            "phase": current_phase,
            **resources,
        }

    def monitor() -> None:
        while not stop.wait(0.5):
            try:
                samples.append(sample())
            except Exception as error:
                sampling_errors.append(type(error).__name__)
                return

    samples.append(sample())
    sampler = threading.Thread(target=monitor)
    stages: list[dict[str, Any]] = []
    attempts: list[Attempt] = []
    sampler.start()
    try:
        for concurrency in (1, 8, 16):
            phase = f"concurrency_{concurrency}"
            batch, elapsed = run_stage(stack.port, username, concurrency, seconds)
            attempts.extend(batch)
            stages.append({"concurrency": concurrency, **summarize(batch, elapsed)})
        phase = "recovery"
        recovery_started = time.monotonic()
        stack.wait_ready()
        recovery = submit(stack.port, username, "recovery Привіт 🌍")
        recovery_ms = (time.monotonic() - recovery_started) * 1000
        attempts.append(recovery)
        # Record several post-load samples, including PyMongo's idle pool.
        time.sleep(2)
    finally:
        stop.set()
        sampler.join(timeout=12)
    assert not sampler.is_alive()
    samples.append(sample())
    accounting = reconcile(attempts, stack.records(), username)
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "runtime": runtime,
        "workload": "POST /api/message, unique synthetic messages, no retries",
        "model": "Closed loop, at most 20 starts/s per worker; not maximum capacity",
        "seconds_per_stage": seconds,
        "http_capacity": 8,
        "tcp_capacity": 8,
        "stages": stages,
        "reconciliation": accounting,
        "recovery": {
            "status": recovery.status,
            "outcome": recovery.outcome,
            "duration_ms": recovery_ms,
        },
        "resources": {
            "scope": "app cgroup only; each sample includes a short observer process",
            "interval_seconds": 0.5,
            "sampled_peak_memory_bytes": max(s["memory_bytes"] for s in samples),
            "sampled_peak_tasks": max(s["tasks"] for s in samples),
            "samples": samples,
            "errors": sampling_errors,
        },
        "limits": (
            "Host-driven Linux container sample; sampled peaks, not an SLA. "
            "Unconfirmed absence means absent at the final observation, "
            "not proof of failed write."
        ),
    }
    (ROOT / "artifacts").mkdir(exist_ok=True)
    (ROOT / "artifacts" / "write-load-report.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    assert not sampling_errors
    assert recovery.outcome == "confirmed" and recovery_ms < 10_000
    assert all(stage["outcomes"].get("confirmed", 0) > 0 for stage in stages)
    assert all(stage["outcomes"].get("unexpected_response", 0) == 0 for stage in stages)
    assert all(sample["memory_bytes"] < 256 * 1024 * 1024 for sample in samples)
    assert all(sample["tasks"] <= 384 for sample in samples)
    for key in (
        "confirmed_missing",
        "duplicate_documents",
        "unexpected_documents",
        "invalid_document_fields",
        "response_id_mismatches",
    ):
        assert accounting[key] == 0, accounting
    assert accounting["documents"] == (
        accounting["confirmed_stored"] + accounting["unconfirmed_stored"]
    )
