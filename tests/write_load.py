"""Bounded, paced POST workload for the disposable integration stack only."""

from __future__ import annotations

import json
import math
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException
from threading import Event
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4


@dataclass(frozen=True)
class Attempt:
    request_id: str
    message: str
    status: int | None
    outcome: str
    duration_ms: float
    response_id: str | None = None
    error: str | None = None


def submit(port: int, username: str, message: str) -> Attempt:
    identifier = str(uuid4())
    started = time.monotonic()
    status = None
    response_id = None
    error = None
    connection = HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(
            "POST",
            "/api/message",
            urlencode({"username": username, "message": message}),
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "X-Request-ID": identifier,
            },
        )
        response = connection.getresponse()
        status = response.status
        response_id = response.getheader("X-Request-ID")
        body = json.loads(response.read())
        if status == 201 and body == {"status": "saved"}:
            outcome = "confirmed"
        elif status == 503 and body == {"error": "storage_unavailable"}:
            outcome = "unconfirmed"
        else:
            outcome = "unexpected_response"
    except (OSError, HTTPException) as exception:
        outcome = "transport_error"
        error = type(exception).__name__
    except (ValueError, UnicodeError) as exception:
        outcome = "unexpected_response"
        error = type(exception).__name__
    finally:
        connection.close()
    return Attempt(
        identifier,
        message,
        status,
        outcome,
        (time.monotonic() - started) * 1000,
        response_id,
        error,
    )


def run_stage(
    port: int,
    username: str,
    concurrency: int,
    seconds: float,
    *,
    interval: float = 0.05,
    sender: Callable[[int, str, str], Attempt] = submit,
) -> tuple[list[Attempt], float]:
    """Closed loop; each worker starts at most 20 requests/s, with no retries."""
    if not 1 <= concurrency <= 32 or not 0 < seconds <= 60 or interval < 0.01:
        raise ValueError("Invalid load limits")
    ready = Event()
    started = 0.0

    def worker(number: int) -> list[Attempt]:
        if not ready.wait(timeout=5):
            return []
        attempts: list[Attempt] = []
        while time.monotonic() < started + seconds:
            before = time.monotonic()
            # Unique per attempt, with Unicode, URL punctuation and a line break.
            message = (
                f"{concurrency}:{number}:{len(attempts)} | Привіт + & = 🌍\n"
                + "load sample " * 24
            )
            attempts.append(sender(port, username, message))
            pause = min(before + interval, started + seconds) - time.monotonic()
            if pause > 0:
                time.sleep(pause)
        return attempts

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(worker, number) for number in range(concurrency)]
        started = time.monotonic()
        ready.set()
        results = [attempt for future in futures for attempt in future.result()]
    return results, time.monotonic() - started


def latency(attempts: list[Attempt]) -> dict[str, float | None]:
    durations = sorted(attempt.duration_ms for attempt in attempts)
    return {
        label: durations[max(0, math.ceil(len(durations) * percentile) - 1)]
        if durations
        else None
        for label, percentile in (("p50", 0.5), ("p95", 0.95), ("max", 1))
    }


def summarize(attempts: list[Attempt], elapsed: float) -> dict[str, Any]:
    confirmed = [attempt for attempt in attempts if attempt.outcome == "confirmed"]
    return {
        "attempts": len(attempts),
        "elapsed_seconds": elapsed,
        "attempts_per_second": len(attempts) / elapsed,
        "confirmed_writes_per_second": len(confirmed) / elapsed,
        "responses": dict(Counter(str(attempt.status) for attempt in attempts)),
        "outcomes": dict(Counter(attempt.outcome for attempt in attempts)),
        "transport_errors": dict(
            Counter(
                attempt.error
                for attempt in attempts
                if attempt.outcome == "transport_error"
            )
        ),
        "latency_ms": latency(attempts),
        "confirmed_latency_ms": latency(confirmed),
        "unconfirmed_latency_ms": latency(
            [attempt for attempt in attempts if attempt.outcome != "confirmed"]
        ),
    }


def reconcile(
    attempts: list[Attempt], records: list[dict[str, Any]], username: str
) -> dict[str, int]:
    """503/transport failure is an unknown write result, never proof of absence."""
    documents = [record for record in records if record.get("username") == username]
    counts = Counter(str(record.get("message")) for record in documents)
    expected = {attempt.message for attempt in attempts}
    confirmed = [a for a in attempts if a.outcome == "confirmed"]
    uncertain = [a for a in attempts if a.outcome != "confirmed"]
    return {
        "documents": len(documents),
        "confirmed_stored": sum(counts[a.message] == 1 for a in confirmed),
        "confirmed_missing": sum(counts[a.message] == 0 for a in confirmed),
        "unconfirmed_stored": sum(counts[a.message] > 0 for a in uncertain),
        "unconfirmed_absent": sum(counts[a.message] == 0 for a in uncertain),
        "duplicate_documents": sum(max(0, count - 1) for count in counts.values()),
        "unexpected_documents": sum(
            count for message, count in counts.items() if message not in expected
        ),
        "invalid_document_fields": sum(
            set(record) != {"date", "username", "message"} for record in documents
        ),
        "response_id_mismatches": sum(a.response_id != a.request_id for a in confirmed),
    }
