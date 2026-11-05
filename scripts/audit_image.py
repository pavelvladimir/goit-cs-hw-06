"""Audit the final local image and export a CycloneDX inventory from the same scan."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TRIVY_IMAGE = (
    "aquasec/trivy:0.76.2@sha256:"
    "8f6bb947dbc528902e11f2f5be52f7b519f6a39d5d148f41166f2040499768fb"
)
SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")
BLOCKING = {"CRITICAL", "HIGH", "UNKNOWN"}
MATCH_FIELDS = ("type", "package", "version", "vulnerability")
EXCEPTION_FIELDS = {*MATCH_FIELDS, "reason", "owner", "expires", "reference"}


class AuditError(Exception):
    """The audit could not produce a trustworthy result."""


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AuditError(f"{path.name}: expected a JSON object")
    return value


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def run(*command: str, capture: bool = False) -> str:
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout or ""


def exception_key(value: dict[str, Any]) -> tuple[str, ...]:
    return tuple(value[field] for field in MATCH_FIELDS)


def load_exceptions(
    path: Path, *, scopes: set[str] | None = None
) -> list[dict[str, Any]]:
    policy = read_json(path)
    if set(policy) != {"schema_version", "exceptions"} or policy["schema_version"] != 1:
        raise AuditError("Unsupported exception policy")
    entries = policy["exceptions"]
    if not isinstance(entries, list):
        raise AuditError("exceptions must be a list")
    seen = set()
    fields = EXCEPTION_FIELDS | ({"scope"} if scopes is not None else set())
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != fields
            or any(not isinstance(v, str) or not v.strip() for v in entry.values())
        ):
            raise AuditError(
                "Each exception needs exact scope, reason, owner, expiry and reference"
            )
        if scopes is not None and entry["scope"] not in scopes:
            raise AuditError(f"Unknown exception scope: {entry['scope']}")
        key = ((entry["scope"],) if scopes is not None else ()) + exception_key(entry)
        if key in seen or any(re.search(r"[?*\[\]]", part) for part in key):
            raise AuditError("Duplicate or wildcard exception")
        if not entry["reference"].startswith("https://"):
            raise AuditError(
                "Exception reference must be an HTTPS advisory or analysis"
            )
        date.fromisoformat(entry["expires"])
        seen.add(key)
    return entries


def validate_inventory(
    scan: dict[str, Any], sbom: dict[str, Any], image_id: str
) -> None:
    if scan.get("SchemaVersion") != 2 or scan.get("ArtifactType") != "container_image":
        raise AuditError("Expected a Trivy container image report, schema 2")
    metadata = scan["Metadata"]
    if metadata["ImageID"] != image_id:
        raise AuditError("Scanned image ID differs from the exported image")
    if metadata["OS"]["Family"] != "debian" or not metadata["OS"]["Name"]:
        raise AuditError("Expected the Debian Distroless runtime")
    results = scan["Results"]
    for package_type in ("debian", "python-pkg"):
        if not any(r["Type"] == package_type and r.get("Packages") for r in results):
            raise AuditError(f"Missing {package_type} package inventory")
    if sbom.get("bomFormat") != "CycloneDX":
        raise AuditError("Expected a CycloneDX SBOM")
    properties = sbom["metadata"]["component"]["properties"]
    if not any(
        p["name"] == "aquasecurity:trivy:ImageID" and p["value"] == image_id
        for p in properties
    ):
        raise AuditError("SBOM image ID differs from the exported image")
    package_urls = {
        p["Identifier"]["PURL"]
        for result in results
        for p in result.get("Packages", [])
    }
    component_urls = {p.get("purl") for p in sbom["components"]}
    if not package_urls <= component_urls:
        raise AuditError("SBOM is missing packages from the scan")


def evaluate(
    scan: dict[str, Any], exceptions: list[dict[str, Any]], today: date
) -> dict[str, Any]:
    findings = []
    counts: Counter[str] = Counter()
    used = set()
    active = {
        exception_key(e): e
        for e in exceptions
        if date.fromisoformat(e["expires"]) >= today
    }
    for result in scan["Results"]:
        for vulnerability in result.get("Vulnerabilities") or []:
            severity = vulnerability["Severity"]
            if severity not in SEVERITIES:
                raise AuditError(f"Unknown severity: {severity}")
            finding = {
                "type": result["Type"],
                "target": result["Target"],
                "package": vulnerability["PkgName"],
                "version": vulnerability["InstalledVersion"],
                "vulnerability": vulnerability["VulnerabilityID"],
                "severity": severity,
                "fixed_version": vulnerability.get("FixedVersion", ""),
                "status": vulnerability.get("Status", "unknown"),
                "reference": vulnerability.get("PrimaryURL", ""),
            }
            key = exception_key(finding)
            finding["decision"] = (
                "exception"
                if key in active
                else "block"
                if severity in BLOCKING
                else "notice"
            )
            if key in active:
                used.add(key)
            counts[severity] += 1
            findings.append(finding)
    reviewed = [
        {
            **e,
            "state": "expired"
            if date.fromisoformat(e["expires"]) < today
            else "applied"
            if exception_key(e) in used
            else "unused",
        }
        for e in exceptions
    ]
    errors = [
        f"{e['vulnerability']} / {e['package']}: {e['state']} exception"
        for e in reviewed
        if e["state"] != "applied"
    ]
    if scan.get("Metadata", {}).get("OS", {}).get("EOSL"):
        errors.append("The base operating system is end-of-life")
    blocked = sum(f["decision"] == "block" for f in findings)
    packages: Counter[str] = Counter()
    for result in scan["Results"]:
        packages[result["Type"]] += len(result.get("Packages", []))
    return {
        "status": "fail" if blocked or errors else "pass",
        "blocking_severities": sorted(BLOCKING),
        "counts": {s: counts[s] for s in SEVERITIES},
        "blocked_findings": blocked,
        "findings": findings,
        "exceptions": reviewed,
        "policy_errors": errors,
        "packages": dict(packages),
    }


def cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def render_report(report: dict[str, Any]) -> str:
    lines = [
        f"# Аудит: {report.get('scope', 'Docker-образ')}",
        "",
        f"Результат: **{report['status'].upper()}**. Час UTC: {report['started_at']}.",
        "",
        f"Trivy: `{TRIVY_IMAGE}`.",
        "",
    ]
    if "image" in report:
        image = report["image"]
        lines += [
            f"Образ: `{image['id']}`; платформа: `{image['platform']}`.",
            f"Створено: {image['created']}. "
            "Registry digests: "
            f"{', '.join(image['repo_digests']) or 'немає (локальна збірка)'}.",
            "",
        ]
    if "inputs_sha256" in report:
        lines += [
            f"Git checkout: `{report['source']['checkout_commit']}`; "
            f"незбережені зміни: {report['source']['checkout_dirty']}.",
            "",
            "SHA-256 вхідних файлів:",
            "",
            *(
                f"- `{name}`: `{digest}`"
                for name, digest in report["inputs_sha256"].items()
            ),
            "",
        ]
    if report["status"] == "error":
        return "\n".join([*lines, f"Аудит не завершено: {cell(report['error'])}", ""])
    database = report["scanner"]["VulnerabilityDB"]
    lines += [
        f"База CVE оновлена: {database['UpdatedAt']}; "
        f"завантажена: {database['DownloadedAt']}.",
        "",
        f"Пакети: {', '.join(f'{k}: {v}' for k, v in report['packages'].items())}.",
        "Блокують CRITICAL, HIGH та UNKNOWN, зокрема без доступного виправлення.",
        f"Заблокованих знахідок: {report['blocked_findings']}.",
        "",
        "| Рівень | Кількість знахідок |",
        "| --- | --- |",
        *(f"| {s} | {report['counts'][s]} |" for s in SEVERITIES),
        "",
        "Кількість — це пари CVE/встановлений пакет, а не унікальні CVE.",
        "",
        "## Винятки",
        "",
    ]
    if not report["exceptions"]:
        lines += ["Винятків немає.", ""]
    for e in report["exceptions"]:
        lines += [
            f"- {cell(e['vulnerability'])}, {cell(e['type'])}/{cell(e['package'])} "
            f"{cell(e['version'])}: **{e['state']}**.",
            f"  {cell(e['reason'])} Відповідальний: {cell(e['owner'])}. "
            f"До {e['expires']} включно (UTC).",
            f"  Джерело: {cell(e['reference'])}",
            "",
        ]
    lines += [
        *(f"- {cell(error)}" for error in report["policy_errors"]),
        "",
        "## Знахідки",
        "",
        "| CVE | Пакет | Версія | Рівень | Виправлення | Рішення |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for f in report["findings"]:
        lines.append(
            "| "
            + " | ".join(
                cell(v)
                for v in (
                    f["vulnerability"],
                    f["package"],
                    f["version"],
                    f["severity"],
                    f["fixed_version"] or "не вказано",
                    f["decision"],
                )
            )
            + " |"
        )
    lines += [
        "",
        "## Файли та межі перевірки",
        "",
        "`sbom.cdx.json` — CycloneDX; `trivy.json` — повний результат.",
        "`report.json` містить ідентифікацію образу, бази CVE та SHA-256 файлів.",
        "Це локальний SBOM, без підпису чи registry attestation.",
        "",
    ]
    lines += report.get(
        "limitations",
        [
            "Перевірено кінцевий Linux-образ app для однієї платформи.",
            "MongoDB і build stages не охоплені. Для зібраного JS потрібен npm audit.",
        ],
    )
    if report.get("published_assets"):
        lines += ["", "## Перевірені файли у static export", ""]
        for asset in report["published_assets"]:
            lines.append(
                f"- `{asset['purl']}` → `{asset['output']}`; "
                f"SHA-256 `{asset['sha256']}`"
            )
    lines.append("")
    return "\n".join(lines)


def trivy(work: Path, cache: Path, *arguments: str, capture: bool = False) -> str:
    return run(
        "docker",
        "run",
        "--rm",
        "--init",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--volume",
        f"{work}:/work",
        "--volume",
        f"{cache}:/cache",
        TRIVY_IMAGE,
        "--cache-dir",
        "/cache",
        *arguments,
        capture=capture,
    )


def audit(image: str | None, output: Path, policy: Path) -> int:
    # Never overwrite an old PASS with a partial run, or mistake it for a new result.
    output.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "schema_version": 1,
        "scope": "image",
        "status": "error",
        "error": "Audit interrupted",
        "started_at": datetime.now(UTC).isoformat(),
        "scanner_image": TRIVY_IMAGE,
    }
    write_json(output / "report.json", report)
    try:
        exceptions = load_exceptions(policy)
        write_json(
            output / "exceptions.json", {"schema_version": 1, "exceptions": exceptions}
        )
        report["source"] = {
            "checkout_commit": run("git", "rev-parse", "HEAD", capture=True).strip(),
            "checkout_dirty": bool(
                run("git", "status", "--porcelain", capture=True).strip()
            ),
            "built_from_checkout": image is None,
        }
        cache = ROOT / ".cache" / "trivy"
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".scan-", dir=output) as temporary:
            work = Path(temporary)
            if image is None:
                run(
                    "docker",
                    "build",
                    "--provenance=false",
                    "--iidfile",
                    str(work / "image.id"),
                    ".",
                )
                image = (work / "image.id").read_text().strip()
            inspected = json.loads(
                run("docker", "image", "inspect", image, capture=True)
            )[0]
            image_id = inspected["Id"]
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                raise AuditError("Docker did not return an immutable image ID")
            report["image"] = {
                "requested": image,
                "id": image_id,
                "repo_digests": inspected.get("RepoDigests") or [],
                "platform": f"{inspected['Os']}/{inspected['Architecture']}",
                "created": inspected["Created"],
            }
            run(
                "docker", "image", "save", "--output", str(work / "image.tar"), image_id
            )
            trivy(
                work,
                cache,
                "image",
                "--input",
                "/work/image.tar",
                "--scanners",
                "vuln",
                "--pkg-types",
                "os,library",
                "--format",
                "json",
                "--list-all-pkgs",
                "--ignorefile",
                "/dev/null",
                "--skip-version-check",
                "--no-progress",
                "--timeout",
                "10m",
                "--output",
                "/work/trivy.json",
            )
            (work / "trivy.json").replace(output / "trivy.json")
            # Convert the exact raw result, without another scan or database update.
            trivy(
                output,
                cache,
                "convert",
                "--format",
                "cyclonedx",
                "--output",
                "/work/sbom.cdx.json",
                "/work/trivy.json",
            )
            scan = read_json(output / "trivy.json")
            validate_inventory(scan, read_json(output / "sbom.cdx.json"), image_id)
            report["scanner"] = json.loads(
                trivy(work, cache, "--version", "--format", "json", capture=True)
            )
            database = report["scanner"]["VulnerabilityDB"]
            for field in ("UpdatedAt", "DownloadedAt", "NextUpdate"):
                if datetime.fromisoformat(database[field]).tzinfo is None:
                    raise AuditError("Missing database timestamp timezone")
            if datetime.fromisoformat(database["NextUpdate"]) <= datetime.now(UTC):
                raise AuditError("Vulnerability database is overdue for an update")
            report.update(evaluate(scan, exceptions, datetime.now(UTC).date()))
            del report["error"]
        report["files_sha256"] = {
            name: hashlib.sha256((output / name).read_bytes()).hexdigest()
            for name in ("trivy.json", "sbom.cdx.json", "exceptions.json")
        }
    except (
        AuditError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        subprocess.CalledProcessError,
    ) as error:
        report.update(status="error", error=str(error))
    report["finished_at"] = datetime.now(UTC).isoformat()
    write_json(output / "report.json", report)
    (output / "report.md").write_text(render_report(report), encoding="utf-8")
    print(
        f"Image audit: {report['status'].upper()} — {output / 'report.md'}", flush=True
    )
    return {"pass": 0, "fail": 1, "error": 2}[report["status"]]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image", help="Audit an existing local image instead of building"
    )
    parser.add_argument(
        "--output", type=Path, help="New report directory; must not exist"
    )
    parser.add_argument(
        "--exceptions", type=Path, default=ROOT / "security/image-exceptions.json"
    )
    args = parser.parse_args()
    output = args.output or ROOT / "artifacts/image-audit" / datetime.now(UTC).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )
    try:
        return audit(args.image, output.resolve(), args.exceptions)
    except OSError as error:
        print(f"Image audit: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
