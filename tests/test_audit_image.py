"""An audit must not silently pass missing inventory, stale data or broad exceptions."""

import copy
import hashlib
import json
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts import audit_image as module

IMAGE_ID = "sha256:" + "a" * 64
TODAY = date(2026, 11, 5)


@pytest.fixture
def scan() -> dict[str, Any]:
    return {
        "SchemaVersion": 2,
        "ArtifactType": "container_image",
        "Metadata": {
            "ImageID": IMAGE_ID,
            "OS": {"Family": "debian", "Name": "13.7"},
        },
        "Results": [
            {
                "Type": "debian",
                "Target": "image.tar (debian 13.7)",
                "Packages": [
                    {"Identifier": {"PURL": "pkg:deb/debian/libc6@2.41-12"}},
                    {"Identifier": {"PURL": "pkg:deb/debian/tzdata@2026b-1"}},
                ],
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-2099-1234",
                        "PkgName": "libc6",
                        "InstalledVersion": "2.41-12",
                        "Severity": "HIGH",
                        "Status": "affected",
                    }
                ],
            },
            {
                "Type": "python-pkg",
                "Target": "Python",
                "Packages": [{"Identifier": {"PURL": "pkg:pypi/pymongo@4.15.3"}}],
                "Vulnerabilities": None,
            },
        ],
    }


@pytest.fixture
def sbom(scan: dict[str, Any]) -> dict[str, Any]:
    return {
        "bomFormat": "CycloneDX",
        "metadata": {
            "component": {
                "properties": [
                    {"name": "aquasecurity:trivy:ImageID", "value": IMAGE_ID}
                ]
            }
        },
        "components": [
            {"purl": p["Identifier"]["PURL"]}
            for result in scan["Results"]
            for p in result["Packages"]
        ],
    }


@pytest.fixture
def exception() -> dict[str, str]:
    return {
        "type": "debian",
        "package": "libc6",
        "version": "2.41-12",
        "vulnerability": "CVE-2099-1234",
        "reason": "Fixture only: this is not an approved project exception.",
        "owner": "test-owner",
        "expires": "2026-11-05",
        "reference": "https://example.com/test-analysis",
    }


@pytest.mark.parametrize("severity", module.SEVERITIES)
@pytest.mark.parametrize("fixed", ["", "2.41-13"])
def test_gate_keeps_all_findings_including_unfixed(
    scan: dict[str, Any], severity: str, fixed: str
) -> None:
    scan["Results"][0]["Vulnerabilities"][0].update(
        Severity=severity, FixedVersion=fixed
    )
    report = module.evaluate(scan, [], TODAY)
    assert report["status"] == (
        "fail" if severity in {"CRITICAL", "HIGH", "UNKNOWN"} else "pass"
    )
    assert report["counts"][severity] == 1
    assert report["findings"][0]["fixed_version"] == fixed
    assert report["exceptions"] == []


def test_no_vulnerabilities_is_valid_but_unknown_severity_is_not(
    scan: dict[str, Any],
) -> None:
    scan["Results"][0]["Vulnerabilities"][0]["Severity"] = "high"
    with pytest.raises(module.AuditError, match="Unknown severity"):
        module.evaluate(scan, [], TODAY)
    del scan["Results"][0]["Vulnerabilities"]
    assert module.evaluate(scan, [], TODAY)["status"] == "pass"
    scan["Metadata"]["OS"]["EOSL"] = True
    assert module.evaluate(scan, [], TODAY)["status"] == "fail"


def test_exception_is_visible_and_expires_after_utc_expiry_day(
    scan: dict[str, Any], exception: dict[str, str]
) -> None:
    report = module.evaluate(scan, [exception], TODAY)
    assert report["status"] == "pass"
    assert report["counts"]["HIGH"] == 1
    assert report["findings"][0]["decision"] == "exception"
    assert report["exceptions"] == [{**exception, "state": "applied"}]
    report = module.evaluate(scan, [exception], TODAY + timedelta(days=1))
    assert report["status"] == "fail"
    assert report["blocked_findings"] == 1
    assert report["exceptions"][0]["state"] == "expired"
    assert report["policy_errors"]


@pytest.mark.parametrize("field", module.MATCH_FIELDS)
def test_exception_cannot_hide_another_package_version_type_or_cve(
    scan: dict[str, Any], exception: dict[str, str], field: str
) -> None:
    exception[field] += "-different"
    report = module.evaluate(scan, [exception], TODAY)
    assert report["status"] == "fail"
    assert report["blocked_findings"] == 1
    assert report["exceptions"][0]["state"] == "unused"


def test_unused_exception_also_fails_a_clean_scan(
    scan: dict[str, Any], exception: dict[str, str]
) -> None:
    scan["Results"][0]["Vulnerabilities"] = []
    report = module.evaluate(scan, [exception], TODAY)
    assert report["status"] == "fail"
    assert report["blocked_findings"] == 0


@pytest.mark.parametrize(
    "problem",
    [
        "root",
        "schema",
        "list",
        "missing",
        "empty",
        "type",
        "wildcard",
        "duplicate",
        "url",
        "date",
    ],
)
def test_invalid_exceptions_are_rejected(
    tmp_path: Path, exception: dict[str, str], problem: str
) -> None:
    policy: dict[str, Any] = {"schema_version": 1, "exceptions": [exception]}
    if problem == "root":
        policy["ignore_unfixed"] = True
    elif problem == "schema":
        policy["schema_version"] = 2
    elif problem == "list":
        policy["exceptions"] = {}
    elif problem == "missing":
        del exception["owner"]
    elif problem == "empty":
        exception["reason"] = "  "
    elif problem == "type":
        policy["exceptions"] = [42]
    elif problem == "wildcard":
        exception["version"] = "*"
    elif problem == "duplicate":
        policy["exceptions"].append(exception)
    elif problem == "url":
        exception["reference"] = "no source"
    elif problem == "date":
        exception["expires"] = "someday"
    path = tmp_path / "policy.json"
    module.write_json(path, policy)
    with pytest.raises((module.AuditError, ValueError)):
        module.load_exceptions(path)


def test_valid_exception_policy_and_invalid_json(
    tmp_path: Path, exception: dict[str, str]
) -> None:
    path = tmp_path / "policy.json"
    module.write_json(path, {"schema_version": 1, "exceptions": [exception]})
    assert module.load_exceptions(path) == [exception]
    module.write_json(path, [])
    with pytest.raises(module.AuditError, match="JSON object"):
        module.read_json(path)


@pytest.mark.parametrize(
    "problem",
    [
        "none",
        "schema",
        "scan_id",
        "os",
        "missing_os",
        "missing_python",
        "bom_format",
        "bom_id",
        "missing_component",
    ],
)
def test_sbom_must_include_nonvulnerable_packages_and_the_same_image(
    scan: dict[str, Any], sbom: dict[str, Any], problem: str
) -> None:
    if problem == "schema":
        scan["SchemaVersion"] = 3
    elif problem == "scan_id":
        scan["Metadata"]["ImageID"] = "sha256:other"
    elif problem == "os":
        scan["Metadata"]["OS"]["Family"] = ""
    elif problem == "missing_os":
        scan["Results"][0]["Packages"] = []
    elif problem == "missing_python":
        scan["Results"].pop()
    elif problem == "bom_format":
        sbom["bomFormat"] = "other"
    elif problem == "bom_id":
        sbom["metadata"]["component"]["properties"] = []
    elif problem == "missing_component":
        sbom["components"].pop(1)  # tzdata has no CVE; it still belongs in the SBOM.
    if problem == "none":
        module.validate_inventory(scan, sbom, IMAGE_ID)
    else:
        with pytest.raises(module.AuditError):
            module.validate_inventory(scan, sbom, IMAGE_ID)


@pytest.mark.parametrize(
    "scenario",
    [
        "build",
        "existing",
        "clean",
        "excepted",
        "build_error",
        "scan_error",
        "convert_error",
        "wrong_id",
        "stale_db",
        "naive_db",
    ],
)
def test_audit_preserves_evidence_and_returns_distinct_exit_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scan: dict[str, Any],
    sbom: dict[str, Any],
    exception: dict[str, str],
    scenario: str,
) -> None:
    monkeypatch.setattr(module, "ROOT", tmp_path)
    today = datetime.now(UTC)
    policy = tmp_path / "exceptions.json"
    exception["expires"] = today.date().isoformat()
    module.write_json(
        policy,
        {
            "schema_version": 1,
            "exceptions": [exception] if scenario == "excepted" else [],
        },
    )
    if scenario == "clean":
        scan["Results"][0]["Vulnerabilities"] = []
    calls: list[tuple[str, ...]] = []

    def fake_run(
        command: tuple[str, ...], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert kwargs["cwd"] == tmp_path
        assert kwargs["check"] is True
        result = ""
        if command[:2] == ("git", "rev-parse"):
            result = "b" * 40
        elif command[:2] == ("git", "status"):
            result = "?? scripts/audit_image.py\n"
        elif command[:2] == ("docker", "build"):
            if scenario == "build_error":
                raise subprocess.CalledProcessError(1, command)
            Path(command[command.index("--iidfile") + 1]).write_text(IMAGE_ID)
        elif command[:3] == ("docker", "image", "inspect"):
            result = json.dumps(
                [
                    {
                        "Id": "mutable:tag" if scenario == "wrong_id" else IMAGE_ID,
                        "Os": "linux",
                        "Architecture": "arm64",
                        "Created": "2026-11-01T00:00:00Z",
                        "RepoDigests": [],
                    }
                ]
            )
        elif command[:3] == ("docker", "image", "save"):
            assert command[-1] == IMAGE_ID
            Path(command[command.index("--output") + 1]).write_bytes(b"image archive")
        elif command[:2] == ("docker", "run"):
            assert module.TRIVY_IMAGE in command
            assert not any("docker.sock" in arg for arg in command)
            work = Path(command[command.index("--volume") + 1].removesuffix(":/work"))
            if "image" in command:
                if scenario == "scan_error":
                    raise subprocess.CalledProcessError(1, command)
                assert "--ignore-unfixed" not in command
                assert "--skip-db-update" not in command
                assert command[command.index("--ignorefile") + 1] == "/dev/null"
                module.write_json(work / "trivy.json", scan)
            elif "convert" in command:
                if scenario == "convert_error":
                    raise subprocess.CalledProcessError(1, command)
                assert module.read_json(work / "trivy.json") == scan
                module.write_json(work / "sbom.cdx.json", sbom)
            else:
                assert "--version" in command
                next_update = today + timedelta(
                    days=-1 if scenario == "stale_db" else 1
                )
                result = json.dumps(
                    {
                        "Version": "0.76.2",
                        "VulnerabilityDB": {
                            "UpdatedAt": today.isoformat(),
                            "DownloadedAt": today.isoformat(),
                            "NextUpdate": next_update.replace(tzinfo=None).isoformat()
                            if scenario == "naive_db"
                            else next_update.isoformat(),
                        },
                    }
                )
        else:
            pytest.fail(f"Unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, stdout=result)

    monkeypatch.setattr(subprocess, "run", fake_run)
    output = tmp_path / "reports" / "new audit"
    existing = scenario == "existing"
    code = module.audit("app:latest" if existing else None, output, policy)
    report = module.read_json(output / "report.json")
    assert not list(output.glob(".scan-*"))
    assert not list(output.rglob("*.tar"))
    assert report["source"]["built_from_checkout"] is not existing
    assert report["source"]["checkout_dirty"] is True
    assert bool([c for c in calls if c[:2] == ("docker", "build")]) is not existing
    if scenario.endswith("error") or scenario in {"wrong_id", "stale_db", "naive_db"}:
        assert code == 2
        assert report["status"] == "error"
        assert "ERROR" in (output / "report.md").read_text()
        if scenario == "convert_error":
            assert module.read_json(output / "trivy.json") == scan
    else:
        assert code == (0 if scenario in {"clean", "excepted"} else 1)
        assert report["image"]["id"] == IMAGE_ID
        assert report["scanner"]["Version"] == "0.76.2"
        assert module.read_json(output / "trivy.json") == scan
        assert module.read_json(output / "sbom.cdx.json") == sbom
        for name, digest in report["files_sha256"].items():
            assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
        if scenario == "excepted":
            markdown = (output / "report.md").read_text()
            assert exception["reason"] in markdown
            assert exception["owner"] in markdown
        original = copy.deepcopy(report)
        with pytest.raises(FileExistsError):
            module.audit(None, output, policy)
        assert module.read_json(output / "report.json") == original


def test_cli_refuses_existing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["audit_image", "--output", str(tmp_path)])
    assert module.main() == 2


def test_cli_defaults_and_markdown_escaping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["audit_image"])

    def audit(image: str | None, output: Path, policy: Path) -> int:
        assert image is None
        assert output.parent == tmp_path / "artifacts/image-audit"
        assert policy == tmp_path / "security/image-exceptions.json"
        return 1

    monkeypatch.setattr(module, "audit", audit)
    assert module.main() == 1
    assert module.cell("a|b\r\nc") == "a\\|b  c"
