"""Exercise complete inventories, published-file evidence, policy scopes and diffs."""

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts import audit_dependencies as module
from scripts import audit_diff
from scripts import audit_image as common


def package(
    name: str, version: str = "1.0", *, dev: bool = False, python: bool = False
) -> dict[str, Any]:
    return {
        "Name": name,
        "Version": version,
        "Dev": dev,
        "Identifier": {"PURL": f"pkg:{'pypi' if python else 'npm'}/{name}@{version}"},
    }


def scan_for(scope: str) -> dict[str, Any]:
    python = scope.startswith("python")
    packages = [package("pymongo" if python else "react", python=python)]
    if scope.endswith("all"):
        packages += [
            package("pytest" if python else "swagger-ui-dist", dev=True, python=python)
        ]
    findings = (
        []
        if python or not scope.endswith("all")
        else [
            {
                "VulnerabilityID": "CVE-2099-1234",
                "PkgName": "swagger-ui-dist",
                "InstalledVersion": "1.0",
                "Severity": "MEDIUM",
            }
        ]
    )
    return {
        "SchemaVersion": 2,
        "ArtifactType": "filesystem",
        "Results": [
            {
                "Type": "uv" if python else "pnpm",
                "Target": "uv.lock" if python else "pnpm-lock.yaml",
                "Packages": packages,
                "Vulnerabilities": findings,
            }
        ],
    }


def sbom_for(scan: dict[str, Any]) -> dict[str, Any]:
    return {
        "bomFormat": "CycloneDX",
        "components": [
            {"purl": p["Identifier"]["PURL"]}
            for r in scan["Results"]
            for p in r["Packages"]
        ],
    }


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(common, "ROOT", tmp_path)
    for inputs in module.INPUTS.values():
        for name in inputs:
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("snapshot content")
    (tmp_path / "frontend/published-assets.json").write_text(
        json.dumps(
            [
                {
                    "package": "swagger-ui-dist",
                    "destination": "vendor/swagger",
                    "files": ["swagger-ui-bundle.js", "LICENSE"],
                }
            ]
        )
    )
    for base in (
        "frontend/node_modules/swagger-ui-dist",
        "frontend/out/vendor/swagger",
    ):
        root = tmp_path / base
        root.mkdir(parents=True)
        (root / "swagger-ui-bundle.js").write_bytes(b"same JS bytes")
        (root / "LICENSE").write_bytes(b"license")
    common.write_json(
        tmp_path / "frontend/node_modules/swagger-ui-dist/package.json",
        {
            "name": "swagger-ui-dist",
            "version": "1.0",
        },
    )
    (tmp_path / "security").mkdir()
    for filename in ("dependency-exceptions.json", "image-exceptions.json"):
        common.write_json(
            tmp_path / "security" / filename, {"schema_version": 1, "exceptions": []}
        )
    return tmp_path


@pytest.fixture
def fake_tools(project: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"failure": None, "calls": [], "time": datetime.now(UTC)}

    def run(*command: str, capture: bool = False) -> str:
        assert capture
        if command[:2] == ("git", "rev-parse"):
            return "a" * 40
        assert command[:2] == ("git", "status")
        return ""

    def trivy(work: Path, cache: Path, *args: str, capture: bool = False) -> str:
        state["calls"].append((work.name, args))
        if state["failure"] == "scan" and args[0] == "fs":
            raise subprocess.CalledProcessError(1, args)
        if args[0] == "fs":
            assert cache == project / ".cache/trivy"
            assert args[-1] == "/work/inputs"
            assert ("--include-dev-deps" in args) == work.name.endswith("all")
            assert "--ignore-unfixed" not in args
            assert "--skip-db-update" not in args
            scan = scan_for(work.name)
            if state["failure"] == "missing-dev" and work.name.endswith("all"):
                scan["Results"][0]["Packages"].pop()
            common.write_json(work / "trivy.json", scan)
        elif args[0] == "convert":
            common.write_json(
                work / "sbom.cdx.json", sbom_for(common.read_json(work / "trivy.json"))
            )
        else:
            assert args[0] == "--version" and capture
            now = state["time"]
            next_update = now + timedelta(
                days=-1 if state["failure"] == "stale-db" else 1
            )
            return json.dumps(
                {
                    "Version": "0.76.2",
                    "VulnerabilityDB": {
                        "UpdatedAt": now.isoformat(),
                        "DownloadedAt": now.isoformat(),
                        "NextUpdate": next_update.replace(tzinfo=None).isoformat()
                        if state["failure"] == "naive-db"
                        else next_update.isoformat(),
                    },
                }
            )
        return ""

    def checked_run(command: list[str], log: Path) -> int:
        state["calls"].append(command)
        log.write_text("diagnostics")
        if state["failure"] == "build" and command == ["make", "build-frontend"]:
            return 1
        if state["failure"] == "mutate-build" and command == ["make", "build-frontend"]:
            (project / "frontend/package.json").write_text("changed")
        if state["failure"] == "mutate-audit" and command == ["make", "audit"]:
            (project / "uv.lock").write_text("changed")
        return (
            1
            if state["failure"] == "native" and command == ["make", "audit-frontend"]
            else 0
        )

    def image_audit(image: str | None, output: Path, policy: Path) -> int:
        state["calls"].append("image")
        output.mkdir()
        status = "error" if state["failure"] == "image" else "fail"
        common.write_json(output / "report.json", {"status": status})
        pkg = package(
            "wrong" if state["failure"] == "runtime-drift" else "pymongo", python=True
        )
        common.write_json(
            output / "trivy.json",
            {"Results": [{"Type": "python-pkg", "Packages": [pkg]}]},
        )
        return 2 if status == "error" else 1

    monkeypatch.setattr(common, "run", run)
    monkeypatch.setattr(common, "trivy", trivy)
    monkeypatch.setattr(common, "audit", image_audit)
    monkeypatch.setattr(module, "checked_run", checked_run)
    return state


@pytest.mark.parametrize("target", ["python", "frontend", "all"])
def test_end_to_end_reports_and_complete_inventory(
    project: Path, fake_tools: dict[str, Any], target: str
) -> None:
    out = project / "reports" / target
    assert module.audit(target, out, None) == (1 if target == "all" else 0)
    summary = common.read_json(out / "report.json")
    scopes = [s for s in module.SCOPES if target == "all" or s.startswith(target)]
    for scope in scopes:
        report = common.read_json(out / scope / "report.json")
        assert report["status"] == "pass"
        assert report["source"]["checkout_commit"] == "a" * 40
        assert report["inputs_sha256"]
        for name, digest in report["files_sha256"].items():
            assert module.sha256(out / scope / name) == digest
        for name, digest in report["inputs_sha256"].items():
            assert (
                module.sha256(out / scope / "inputs" / name.removeprefix("frontend/"))
                == digest
            )
        assert report["components"]
        assert "SHA-256" in (out / scope / "report.md").read_text()
    if target != "python":
        full = common.read_json(out / "frontend-all/report.json")
        declared = common.read_json(out / "frontend-declared/report.json")
        assert "pkg:npm/swagger-ui-dist@1.0" not in {
            c["purl"] for c in declared["components"]
        }
        assert len(full["published_assets"]) == 2
        assert {a["purl"] for a in full["published_assets"]} == {
            "pkg:npm/swagger-ui-dist@1.0"
        }
        assert full["counts"]["MEDIUM"] == 1
    if target == "all":
        assert summary["results"]["image"]["status"] == "fail"
        assert summary["results"]["pip-audit"]["status"] == "pass"
        assert ["make", "audit"] in fake_tools["calls"]
        assert ["make", "audit-frontend"] in fake_tools["calls"]
    before = (out / "report.json").read_bytes()
    with pytest.raises(FileExistsError):
        module.audit(target, out, None)
    assert (out / "report.json").read_bytes() == before


@pytest.mark.parametrize(
    "failure",
    [
        "build",
        "scan",
        "stale-db",
        "naive-db",
        "image",
        "runtime-drift",
        "missing-dev",
        "mutate-build",
        "mutate-audit",
    ],
)
def test_pipeline_fails_closed_but_keeps_available_evidence(
    project: Path, fake_tools: dict[str, Any], failure: str
) -> None:
    fake_tools["failure"] = failure
    out = project / "reports"
    assert module.audit("all", out, None) == 2
    summary = common.read_json(out / "report.json")
    assert summary["status"] == "error"
    if failure == "build":
        assert not (out / "frontend-all/published-assets.json").exists()
        assert summary["results"]["frontend-all"]["status"] == "error"
        assert (out / "frontend-declared/sbom.cdx.json").exists()
    if failure in {"stale-db", "naive-db"}:
        assert (out / "python-all/trivy.json").exists()
        assert (out / "python-all/sbom.cdx.json").exists()
    if failure == "runtime-drift":
        assert "differ" in summary["error"]


def test_native_audit_failure_is_not_swallowed(
    project: Path, fake_tools: dict[str, Any]
) -> None:
    fake_tools["failure"] = "native"
    out = project / "reports"
    assert module.audit("all", out, None) == 1
    result = common.read_json(out / "report.json")["results"]["pnpm-audit"]
    assert result["status"] == "fail" and result["exit_code"] == 1
    assert (out / result["log"]).read_text() == "diagnostics"


@pytest.mark.parametrize(
    "problem", ["version", "bytes", "extra", "empty", "escape", "duplicate"]
)
def test_published_assets_reject_wrong_version_stale_files_and_unlisted_files(
    project: Path, problem: str
) -> None:
    if problem == "version":
        common.write_json(
            project / "frontend/node_modules/swagger-ui-dist/package.json",
            {"name": "swagger-ui-dist", "version": "2.0"},
        )
    elif problem == "bytes":
        (project / "frontend/out/vendor/swagger/swagger-ui-bundle.js").write_text(
            "stale"
        )
    elif problem == "extra":
        (project / "frontend/out/vendor/unknown.js").write_text("unlisted")
    else:
        path = project / "frontend/published-assets.json"
        config = json.loads(path.read_text())
        if problem == "empty":
            config = []
        elif problem == "escape":
            config[0]["files"][0] = "../escape.js"
        else:
            config[0]["files"].append("LICENSE")
        path.write_text(json.dumps(config))
    components = module.inventory(scan_for("frontend-all"), "pnpm", True)
    with pytest.raises(common.AuditError):
        module.published_assets(components)


@pytest.mark.parametrize(
    "problem", ["schema", "target", "empty", "dev", "identity", "bom", "missing"]
)
def test_inventory_completeness_is_required(problem: str) -> None:
    scan = scan_for("python-runtime")
    if problem == "schema":
        scan["ArtifactType"] = "container_image"
    elif problem == "target":
        scan["Results"][0]["Target"] = "other.lock"
    elif problem == "empty":
        scan["Results"][0]["Packages"] = []
    elif problem == "dev":
        scan["Results"][0]["Packages"][0]["Dev"] = True
    elif problem == "identity":
        scan["Results"][0]["Packages"][0]["Identifier"]["PURL"] = ""
    with pytest.raises(common.AuditError):
        components = module.inventory(scan, "uv", False)
        sbom = sbom_for(scan)
        if problem == "bom":
            sbom["bomFormat"] = "SPDX"
        elif problem == "missing":
            sbom["components"] = []
        module.validate_sbom(sbom, components)


def test_peer_duplicates_keep_both_roles_and_root_project() -> None:
    scan = scan_for("frontend-all")
    packets = scan["Results"][0]["Packages"]
    packets += [package("react", dev=True), package("react")]
    packets.append({**package("project"), "Relationship": "root"})
    components = module.inventory(scan, "pnpm", True)
    react = next(p for p in components if p["name"] == "react")
    assert react["roles"] == ["production", "development"]
    assert next(p for p in components if p["name"] == "project")["roles"] == ["project"]


def test_exception_scopes_do_not_leak(project: Path) -> None:
    entry = {
        "scope": "python-all",
        "type": "uv",
        "package": "pytest",
        "version": "1.0",
        "vulnerability": "CVE-2099-1234",
        "reason": "Test only",
        "owner": "test",
        "expires": "2099-01-01",
        "reference": "https://example.com/analysis",
    }
    path = project / "security/dependency-exceptions.json"
    other = {**entry, "scope": "python-runtime"}
    common.write_json(path, {"schema_version": 1, "exceptions": [entry, other]})
    assert len(common.load_exceptions(path, scopes=set(module.SCOPES))) == 2
    other["scope"] = "unknown"
    common.write_json(path, {"schema_version": 1, "exceptions": [other]})
    with pytest.raises(common.AuditError, match="scope"):
        common.load_exceptions(path, scopes=set(module.SCOPES))


def test_scoped_expiry_only_affects_the_matching_audit(
    project: Path, fake_tools: dict[str, Any]
) -> None:
    entry = {
        "scope": "frontend-all",
        "type": "pnpm",
        "package": "swagger-ui-dist",
        "version": "1.0",
        "vulnerability": "CVE-2099-1234",
        "reason": "Test only",
        "owner": "test",
        "expires": "2000-01-01",
        "reference": "https://example.com/analysis",
    }
    common.write_json(
        project / "security/dependency-exceptions.json",
        {
            "schema_version": 1,
            "exceptions": [entry],
        },
    )
    assert module.audit("python", project / "python-reports", None) == 0
    out = project / "frontend-reports"
    assert module.audit("frontend", out, None) == 1
    assert common.read_json(out / "frontend-declared/report.json")["status"] == "pass"
    full = common.read_json(out / "frontend-all/report.json")
    assert full["exceptions"][0]["state"] == "expired"
    entry["expires"] = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    common.write_json(
        project / "security/dependency-exceptions.json",
        {
            "schema_version": 1,
            "exceptions": [entry],
        },
    )
    valid = project / "accepted-reports"
    assert module.audit("frontend", valid, None) == 0
    assert (
        common.read_json(valid / "frontend-all/report.json")["findings"][0]["decision"]
        == "exception"
    )


def test_version_diff_preserves_scoped_packages_registries_and_multiple_versions() -> (
    None
):
    before = {
        "pkg:npm/@org/lib@1.0",
        "pkg:npm/@org/lib@2.0",
        "pkg:pypi/gone@1",
        "pkg:npm/lib@1?repository_url=old",
    }
    after = {
        "pkg:npm/@org/lib@2.0",
        "pkg:npm/@org/lib@3.0",
        "pkg:pypi/new@1",
        "pkg:npm/lib@1?repository_url=new",
    }
    diff = audit_diff.package_changes(before, after)
    assert diff["updated"] == [
        {
            "component": "pkg:npm/@org/lib",
            "before": ["1.0", "2.0"],
            "after": ["2.0", "3.0"],
        }
    ]
    assert "pkg:npm/lib@1?repository_url=new" in diff["added"]
    with pytest.raises(common.AuditError, match="Unversioned"):
        audit_diff.package_changes({"pkg:pypi/no-version"}, set())


def test_baseline_comparison_and_integrity(
    project: Path, fake_tools: dict[str, Any]
) -> None:
    before, after = project / "before", project / "after"
    assert module.audit("frontend", before, None) == 0
    fake_tools["time"] += timedelta(minutes=1)
    assert module.audit("frontend", after, before) == 0
    changes = common.read_json(after / "changes.json")
    assert changes["scopes"]["frontend-all"]["database_changed"]
    assert changes["scopes"]["frontend-all"]["packages"]["added"] == []
    assert "База CVE змінилася" in (after / "report.md").read_text()
    report_path = after / "frontend-all/report.json"
    report = common.read_json(report_path)
    report["source"]["checkout_commit"] = "b" * 40
    report["findings"][0]["severity"] = "HIGH"
    report["findings"].append(
        {**report["findings"][0], "vulnerability": "CVE-2099-5678"}
    )
    common.write_json(report_path, report)
    diff = audit_diff.compare_runs(before, after, ["frontend-all"])
    assert len(diff["scopes"]["frontend-all"]["changed_findings"]) == 1
    assert len(diff["scopes"]["frontend-all"]["new_findings"]) == 1
    assert "CVE-2099-5678" in audit_diff.render_changes(diff)
    reverse = audit_diff.compare_runs(after, before, ["frontend-all"])
    assert len(reverse["scopes"]["frontend-all"]["removed_findings"]) == 1
    (before / "frontend-all/sbom.cdx.json").write_text("tampered")
    with pytest.raises(common.AuditError, match="Changed baseline"):
        audit_diff.compare_runs(before, after, ["frontend-all"])


def test_invalid_baseline_blocks_aggregate_success(
    project: Path, fake_tools: dict[str, Any]
) -> None:
    out = project / "reports"
    assert module.audit("python", out, project / "missing") == 2
    assert common.read_json(out / "report.json")["status"] == "error"


def test_comparison_rejects_incomplete_reports(
    project: Path, fake_tools: dict[str, Any]
) -> None:
    out = project / "reports"
    module.audit("python", out, None)
    path = out / "python-runtime/report.json"
    report = common.read_json(path)
    report["status"] = "error"
    common.write_json(path, report)
    with pytest.raises(common.AuditError, match="incomplete"):
        audit_diff.load_report(out, "python-runtime")


def test_image_comparison_supports_previous_reports_and_marks_platform_changes(
    project: Path, fake_tools: dict[str, Any]
) -> None:
    before, after = project / "before", project / "after"
    for directory, platform in ((before, "linux/arm64"), (after, "linux/amd64")):
        module.audit("python", directory, None)
        (directory / "python-runtime").rename(directory / "image")
        path = directory / "image/report.json"
        report = common.read_json(path)
        del report["scope"]  # First image-audit version did not write a scope.
        report["image"] = {"platform": platform}
        common.write_json(path, report)
    changes = audit_diff.compare_runs(before, after, ["image"])
    assert changes["scopes"]["image"]["platform_changed"]
    assert "Платформа образу змінилася" in audit_diff.render_changes(changes)
    evidence = after / "image/exception-review.json"
    common.write_json(evidence, {"image_probe": {"uid": 65532}})
    path = after / "image/report.json"
    report = common.read_json(path)
    report["exception_review"] = common.read_json(evidence)
    report["files_sha256"][evidence.name] = hashlib.sha256(
        evidence.read_bytes()
    ).hexdigest()
    common.write_json(path, report)
    audit_diff.load_report(after, "image")
    evidence.write_text("tampered review")
    with pytest.raises(common.AuditError, match="Changed baseline artifact"):
        audit_diff.load_report(after, "image")


def test_checked_run_logs_errors(tmp_path: Path) -> None:
    log = tmp_path / "tool.log"
    assert (
        module.checked_run(
            [
                sys.executable,
                "-c",
                "import sys; print('diagnostic', file=sys.stderr); sys.exit(3)",
            ],
            log,
        )
        == 3
    )
    assert log.read_text().strip() == "diagnostic"


def test_cli_defaults_and_existing_output(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["audit_dependencies", "--output", str(project)])
    assert module.main() == 2
    monkeypatch.setattr(sys, "argv", ["audit_dependencies", "--target", "python"])

    def audit(target: str, output: Path, baseline: Path | None) -> int:
        assert target == "python" and baseline is None
        assert output.parent == project / "artifacts/dependency-audit"
        return 0

    monkeypatch.setattr(module, "audit", audit)
    assert module.main() == 0
