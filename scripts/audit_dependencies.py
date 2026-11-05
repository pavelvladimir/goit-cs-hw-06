"""Audit locked Python/frontend dependencies and retain reproducible SBOM evidence."""

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts import audit_image as common

ROOT = common.ROOT
SCOPES = {
    "python-runtime": ("uv", False),
    "python-all": ("uv", True),
    "frontend-declared": ("pnpm", False),
    "frontend-all": ("pnpm", True),
}
INPUTS = {
    "uv": ("pyproject.toml", "uv.lock"),
    "pnpm": (
        "frontend/package.json",
        "frontend/pnpm-lock.yaml",
        "frontend/pnpm-workspace.yaml",
        "frontend/published-assets.json",
        "frontend/scripts/build-docs.mjs",
    ),
}
ERRORS = (
    common.AuditError,
    OSError,
    ValueError,
    KeyError,
    TypeError,
    IndexError,
    subprocess.CalledProcessError,
)


def sha256(path: Path) -> str:
    """Hash file bytes for input snapshots, published assets and report integrity."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(
    scan: dict[str, Any], kind: str, include_dev: bool
) -> list[dict[str, Any]]:
    """Validate a lockfile scan and deduplicate PURLs while retaining package roles."""
    if scan.get("SchemaVersion") != 2 or scan.get("ArtifactType") != "filesystem":
        raise common.AuditError("Expected a Trivy filesystem report, schema 2")
    results = scan.get("Results", [])
    target = "uv.lock" if kind == "uv" else "pnpm-lock.yaml"
    if (
        len(results) != 1
        or results[0]["Type"] != kind
        or results[0]["Target"] != target
    ):
        raise common.AuditError(f"Expected exactly one {target} result")
    packages = results[0].get("Packages", [])
    if not packages:
        raise common.AuditError("Empty package inventory")
    if not include_dev and any(p.get("Dev") for p in packages):
        raise common.AuditError("Production inventory contains dev-only dependencies")
    # PURLs can repeat for peer resolutions; keep them in the raw scan, deduplicate
    # component identities for comparisons. A component can have multiple roles.
    components: dict[str, dict[str, Any]] = {}
    for p in packages:
        purl = p["Identifier"]["PURL"]
        if not purl.startswith("pkg:") or not p["Name"] or not p["Version"]:
            raise common.AuditError("Incomplete package identity")
        role = (
            "project"
            if p.get("Relationship") == "root"
            else "development"
            if p.get("Dev")
            else "production"
        )
        item = components.setdefault(
            purl,
            {"purl": purl, "name": p["Name"], "version": p["Version"], "roles": []},
        )
        if role not in item["roles"]:
            item["roles"].append(role)
    return sorted(components.values(), key=lambda p: p["purl"])


def validate_sbom(sbom: dict[str, Any], components: list[dict[str, Any]]) -> None:
    """Require the CycloneDX component identities to match the scan exactly."""
    if sbom.get("bomFormat") != "CycloneDX":
        raise common.AuditError("Expected a CycloneDX SBOM")
    actual = {p["purl"] for p in sbom["components"] if p.get("purl")}
    if actual != {p["purl"] for p in components}:
        raise common.AuditError("SBOM and scanned package identities differ")


def safe_child(root: Path, relative: str) -> Path:
    """Resolve an asset path and reject escapes, including through symlinks."""
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise common.AuditError(f"Asset escapes its directory: {relative}")
    return path


def published_assets(components: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Verify files copied verbatim by the shared build-docs manifest."""
    config = json.loads((ROOT / "frontend/published-assets.json").read_text())
    if not isinstance(config, list) or not config:
        raise common.AuditError("Published asset manifest must not be empty")
    assets = []
    expected = set()
    for entry in config:
        package = safe_child(ROOT / "frontend/node_modules", entry["package"])
        metadata = common.read_json(package / "package.json")
        matches = [
            p
            for p in components
            if p["name"] == entry["package"] and p["version"] == metadata["version"]
        ]
        if (
            metadata["name"] != entry["package"]
            or len(matches) != 1
            or not entry["files"]
        ):
            raise common.AuditError(
                "Installed published package does not match lockfile"
            )
        for filename in entry["files"]:
            source = safe_child(package, filename)
            output = safe_child(
                ROOT / "frontend/out", f"{entry['destination']}/{filename}"
            )
            if output in expected or sha256(source) != sha256(output):
                raise common.AuditError(
                    f"Published asset is duplicate or differs: {output.name}"
                )
            expected.add(output)
            assets.append(
                {
                    "purl": matches[0]["purl"],
                    "output": str(output.relative_to(ROOT)),
                    "sha256": sha256(output),
                }
            )
    actual = {
        p.resolve() for p in (ROOT / "frontend/out/vendor").rglob("*") if p.is_file()
    }
    if actual != expected:
        raise common.AuditError("Published vendor files differ from the asset manifest")
    return assets


def scan_scope(
    scope: str,
    output: Path,
    snapshots: dict[str, bytes],
    exceptions: list[dict[str, Any]],
    source: dict[str, Any],
) -> int:
    """Audit one input snapshot and preserve its SBOM, policy and failure evidence."""
    output.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "schema_version": 1,
        "scope": scope,
        "status": "error",
        "error": "Audit interrupted",
        "started_at": datetime.now(UTC).isoformat(),
        "source": source,
        "scanner_image": common.TRIVY_IMAGE,
        "inputs_sha256": {
            p: hashlib.sha256(b).hexdigest() for p, b in snapshots.items()
        },
    }
    common.write_json(output / "report.json", report)
    try:
        kind, include_dev = SCOPES[scope]
        selected = [e for e in exceptions if e["scope"] == scope]
        common.write_json(
            output / "exceptions.json", {"schema_version": 1, "exceptions": selected}
        )
        inputs = output / "inputs"
        inputs.mkdir()
        for name, content in snapshots.items():
            # Retain the exact manifest/lockfile snapshot used by this scan.
            path = inputs / name.removeprefix("frontend/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        cache = ROOT / ".cache/trivy"
        cache.mkdir(parents=True, exist_ok=True)
        flags = ["--include-dev-deps"] if include_dev else []
        common.trivy(
            output,
            cache,
            "fs",
            "--scanners",
            "vuln",
            "--pkg-types",
            "library",
            "--format",
            "json",
            "--list-all-pkgs",
            "--ignorefile",
            "/dev/null",
            "--skip-version-check",
            "--no-progress",
            "--disable-telemetry",
            "--timeout",
            "10m",
            *flags,
            "--output",
            "/work/trivy.json",
            "/work/inputs",
        )
        common.trivy(
            output,
            cache,
            "convert",
            "--format",
            "cyclonedx",
            "--output",
            "/work/sbom.cdx.json",
            "/work/trivy.json",
        )
        scan = common.read_json(output / "trivy.json")
        components = inventory(scan, kind, include_dev)
        validate_sbom(common.read_json(output / "sbom.cdx.json"), components)
        report["components"] = components
        report["scanner"] = json.loads(
            common.trivy(output, cache, "--version", "--format", "json", capture=True)
        )
        database = report["scanner"]["VulnerabilityDB"]
        for field in ("UpdatedAt", "DownloadedAt", "NextUpdate"):
            if datetime.fromisoformat(database[field]).tzinfo is None:
                raise common.AuditError("Missing database timestamp timezone")
        if datetime.fromisoformat(database["NextUpdate"]) <= datetime.now(UTC):
            raise common.AuditError("Vulnerability database is overdue for an update")
        if scope == "frontend-all":
            report["published_assets"] = published_assets(components)
            common.write_json(
                output / "published-assets.json", report["published_assets"]
            )
        report["limitations"] = [
            "Lockfile включає платформні варіанти, які можуть не бути встановлені.",
            "Кількість Trivy може включати корінь проєкту та різні peer resolutions.",
            "Production/development позначає групу, а не присутність у JS bundle.",
            "Прямо скопійовані vendor-файли перевірено окремо у frontend-all.",
        ]
        report.update(common.evaluate(scan, selected, datetime.now(UTC).date()))
        report["files_sha256"] = {
            p.name: sha256(p)
            for p in output.iterdir()
            if p.is_file() and p.name != "report.json"
        }
        del report["error"]
    except ERRORS as error:
        report.update(status="error", error=str(error))
    report["finished_at"] = datetime.now(UTC).isoformat()
    common.write_json(output / "report.json", report)
    (output / "report.md").write_text(common.render_report(report), encoding="utf-8")
    print(f"{scope}: {report['status'].upper()} — {output / 'report.md'}", flush=True)
    return {"pass": 0, "fail": 1, "error": 2}[report["status"]]


def checked_run(command: list[str], log: Path) -> int:
    """Capture combined command output and return its unmodified exit status."""
    with log.open("w") as stream:
        return subprocess.run(
            command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, check=False
        ).returncode


def snapshot(kind: str) -> dict[str, bytes]:
    """Read the exact manifest and lockfile bytes for one dependency family."""
    return {name: (ROOT / name).read_bytes() for name in INPUTS[kind]}


def verify_scopes(output: Path, names: list[str]) -> None:
    """Catch accidental omission of development or deployed runtime components."""
    for family, production in (
        ("python", "python-runtime"),
        ("frontend", "frontend-declared"),
    ):
        complete = f"{family}-all"
        if complete not in names:
            continue
        full = common.read_json(output / complete / "report.json")
        prod = common.read_json(output / production / "report.json")
        if "error" in (full["status"], prod["status"]):
            continue
        full_ids = {c["purl"] for c in full["components"]}
        prod_ids = {c["purl"] for c in prod["components"]}
        if not prod_ids < full_ids or not any(
            "development" in c["roles"] for c in full["components"]
        ):
            raise common.AuditError(
                f"{complete} does not include the expected development dependencies"
            )
    if "image" in names:
        image = common.read_json(output / "image/report.json")
        runtime = common.read_json(output / "python-runtime/report.json")
        if "error" not in (image["status"], runtime["status"]):
            raw = common.read_json(output / "image/trivy.json")
            installed = {
                p["Identifier"]["PURL"]
                for r in raw["Results"]
                if r["Type"] == "python-pkg"
                for p in r["Packages"]
            }
            locked = {
                p["purl"] for p in runtime["components"] if "project" not in p["roles"]
            }
            if installed != locked:
                raise common.AuditError(
                    "Python image packages differ from runtime lockfile inventory"
                )


def audit(target: str, output: Path, baseline: Path | None) -> int:
    """Run selected audits and comparison; any failed or incomplete step blocks PASS."""
    output.mkdir(parents=True, exist_ok=False)
    summary: dict[str, Any] = {
        "schema_version": 1,
        "status": "error",
        "error": "Audit interrupted",
        "started_at": datetime.now(UTC).isoformat(),
        "results": {},
    }
    common.write_json(output / "report.json", summary)
    try:
        policy = common.load_exceptions(
            ROOT / "security/dependency-exceptions.json", scopes=set(SCOPES)
        )
        source = {
            "checkout_commit": common.run(
                "git", "rev-parse", "HEAD", capture=True
            ).strip(),
            "checkout_dirty": bool(
                common.run("git", "status", "--porcelain", capture=True).strip()
            ),
        }
        summary["source"] = source
        kinds = (
            ["uv"]
            if target == "python"
            else ["pnpm"]
            if target == "frontend"
            else ["uv", "pnpm"]
        )
        snapshots = {kind: snapshot(kind) for kind in kinds}
        build_ok = True
        if "pnpm" in kinds:
            code = checked_run(
                ["make", "build-frontend"], output / "frontend-build.log"
            )
            build_ok = code == 0
            summary["results"]["frontend-build"] = {
                "status": "pass" if build_ok else "error",
                "exit_code": code,
            }
        for kind in kinds:
            if snapshot(kind) != snapshots[kind]:
                raise common.AuditError(
                    "Input files changed during build; rerun the audit"
                )
        names = [scope for scope, (kind, _) in SCOPES.items() if kind in kinds]
        for scope in names:
            # Lockfile inventories still run after a failed build. The published
            # evidence must never use output left over from a previous build.
            if scope == "frontend-all" and not build_ok:
                summary["results"][scope] = {
                    "status": "error",
                    "error": "Fresh frontend build failed",
                }
                continue
            scan_scope(
                scope, output / scope, snapshots[SCOPES[scope][0]], policy, source
            )
            report = common.read_json(output / scope / "report.json")
            summary["results"][scope] = {
                "status": report["status"],
                "report": f"{scope}/report.json",
            }
        if target == "all":
            common.audit(
                None, output / "image", ROOT / "security/image-exceptions.json"
            )
            image = common.read_json(output / "image/report.json")
            summary["results"]["image"] = {
                "status": image["status"],
                "report": "image/report.json",
            }
            names.append("image")
            for name, command in (
                ("pip-audit", ["make", "audit"]),
                ("pnpm-audit", ["make", "audit-frontend"]),
            ):
                code = checked_run(command, output / f"{name}.log")
                summary["results"][name] = {
                    "status": "pass" if code == 0 else "fail",
                    "exit_code": code,
                    "log": f"{name}.log",
                }
        if build_ok:
            verify_scopes(output, names)
        for kind in kinds:
            if snapshot(kind) != snapshots[kind]:
                raise common.AuditError("Input files changed during audit; rerun it")
        if baseline:
            from scripts.audit_diff import compare_runs

            summary["comparison"] = compare_runs(baseline, output, names)
            common.write_json(output / "changes.json", summary["comparison"])
        statuses = [r["status"] for r in summary["results"].values()]
        summary["status"] = (
            "error" if "error" in statuses else "fail" if "fail" in statuses else "pass"
        )
        del summary["error"]
    except ERRORS as error:
        summary.update(status="error", error=str(error))
    summary["finished_at"] = datetime.now(UTC).isoformat()
    common.write_json(output / "report.json", summary)
    lines = [
        "# Аудит залежностей",
        "",
        f"Результат: **{summary['status'].upper()}**. UTC: {summary['started_at']}.",
        "",
    ]
    for name, result in summary["results"].items():
        link = result.get("report", result.get("log"))
        label = f"[{name}]({link})" if link else name
        lines.append(f"- {label}: **{result['status'].upper()}**")
    if "error" in summary:
        lines += ["", common.cell(summary["error"])]
    if "comparison" in summary:
        from scripts.audit_diff import render_changes

        lines += ["", render_changes(summary["comparison"])]
    lines += [
        "",
        "Деталі та винятки — в окремих report.md/json; SBOM — у sbom.cdx.json.",
        "Ненульовий код pip-audit/pnpm audit: знахідки або збій; див. log.",
        "",
    ]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Audit: {summary['status'].upper()} — {output / 'report.md'}", flush=True)
    return {"pass": 0, "fail": 1, "error": 2}[summary["status"]]


def main() -> int:
    """Parse scope, output and baseline options for a new dependency audit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target", choices=("python", "frontend", "all"), default="all"
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--baseline", type=Path, help="Previous audit directory to compare"
    )
    args = parser.parse_args()
    output = args.output or ROOT / "artifacts/dependency-audit" / datetime.now(
        UTC
    ).strftime("%Y%m%dT%H%M%S%fZ")
    try:
        return audit(args.target, output.resolve(), args.baseline)
    except OSError as error:
        print(f"Audit: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
