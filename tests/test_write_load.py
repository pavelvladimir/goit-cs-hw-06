"""The load report must detect data loss and must not mistake 503 for no write."""

from http.client import IncompleteRead
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests import write_load
from tests.write_load import Attempt, reconcile, run_stage, summarize


def attempt(message: str, outcome: str = "confirmed") -> Attempt:
    return Attempt(
        "id", message, 201 if outcome == "confirmed" else 503, outcome, 5, "id"
    )


def document(message: str) -> dict[str, Any]:
    return {"username": "load", "message": message, "date": "2026-11-01"}


def test_reconciliation_accounts_for_lost_acknowledgements() -> None:
    attempts = [
        attempt("confirmed"),
        attempt("lost-ack", "unconfirmed"),
        attempt("rejected", "unconfirmed"),
        attempt("lost-response", "transport_error"),
    ]
    records = [
        document(message) for message in ("confirmed", "lost-ack", "lost-response")
    ]
    records.append({"username": "unrelated", "message": "outside this run"})
    result = reconcile(attempts, records, "load")
    assert result == {
        "documents": 3,
        "confirmed_stored": 1,
        "confirmed_missing": 0,
        "unconfirmed_stored": 2,
        "unconfirmed_absent": 1,
        "duplicate_documents": 0,
        "unexpected_documents": 0,
        "invalid_document_fields": 0,
        "response_id_mismatches": 0,
    }


def test_reconciliation_detects_data_loss_duplicates_and_wrong_ids() -> None:
    attempts = [
        attempt("missing"),
        attempt("duplicate"),
        Attempt("sent-id", "corrupt", 201, "confirmed", 1, "wrong-id"),
    ]
    records = [document("duplicate"), document("duplicate"), document("changed")]
    records[0]["extra"] = "unexpected metadata"
    result = reconcile(attempts, records, "load")
    assert result["confirmed_missing"] == 2
    assert result["duplicate_documents"] == 1
    assert result["unexpected_documents"] == 1
    assert result["invalid_document_fields"] == 1
    assert result["response_id_mismatches"] == 1


@pytest.mark.parametrize(
    "status,body,failure,expected",
    [
        (201, b'{"status":"saved"}', None, "confirmed"),
        (503, b'{"error":"storage_unavailable"}', None, "unconfirmed"),
        (500, b"{}", None, "unexpected_response"),
        (201, b'{"status":"wrong"}', None, "unexpected_response"),
        (201, b"not-json", None, "unexpected_response"),
        (201, b"\xff", None, "unexpected_response"),
        (201, b"", IncompleteRead(b"private", 50), "transport_error"),
    ],
)
def test_load_client_validates_ack_and_never_retries(
    status: int,
    body: bytes,
    failure: Exception | None,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = MagicMock()
    response = connection.getresponse.return_value
    response.status = status
    response.getheader.return_value = "server-id"
    response.read.return_value = body
    response.read.side_effect = failure
    monkeypatch.setattr(
        write_load, "HTTPConnection", lambda *args, **kwargs: connection
    )
    result = write_load.submit(3000, "load", "private-message")
    assert result.outcome == expected
    assert result.status == status
    assert result.duration_ms >= 0
    assert "private" not in (result.error or "")
    connection.request.assert_called_once()
    connection.close.assert_called_once()


def test_load_client_counts_connection_failure_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = MagicMock()
    connection.request.side_effect = TimeoutError("private-timeout")
    monkeypatch.setattr(
        write_load, "HTTPConnection", lambda *args, **kwargs: connection
    )
    result = write_load.submit(3000, "load", "message")
    assert result.status is None and result.outcome == "transport_error"
    assert result.error == "TimeoutError"
    connection.request.assert_called_once()
    connection.close.assert_called_once()


def test_workload_uses_unique_messages_and_separates_confirmed_latency() -> None:
    def sender(port: int, username: str, message: str) -> Attempt:
        assert port == 3000 and username == "load"
        return attempt(message, "unconfirmed")

    results, elapsed = run_stage(3000, "load", 3, 0.15, sender=sender)
    assert 3 <= len(results) <= 12
    assert len({result.message for result in results}) == len(results)
    assert {result.message.split(":")[1] for result in results} == {"0", "1", "2"}
    report = summarize(results, elapsed)
    assert report["outcomes"] == {"unconfirmed": len(results)}
    assert report["confirmed_writes_per_second"] == 0
    assert report["confirmed_latency_ms"]["p95"] is None
    assert report["unconfirmed_latency_ms"]["p95"] == 5


@pytest.mark.parametrize("concurrency,seconds", [(0, 1), (33, 1), (1, 0), (1, 61)])
def test_load_limits_are_bounded(concurrency: int, seconds: float) -> None:
    with pytest.raises(ValueError):
        run_stage(3000, "load", concurrency, seconds)
