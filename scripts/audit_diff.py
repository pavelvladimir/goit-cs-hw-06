"""Compare verified CycloneDX snapshots while distinguishing CVE database changes."""

import hashlib
from collections import defaultdict
from pathlib import Path
from typing import Any

from scripts import audit_image as common


def load_report(directory: Path, scope: str) -> tuple[dict[str, Any], set[str]]:
    report = common.read_json(directory / scope / "report.json")
    if report.get("scope", "image") != scope or report["status"] not in {
        "pass",
        "fail",
    }:
        raise common.AuditError(
            f"Cannot compare incomplete or mismatched {scope} report"
        )
    for filename in (
        "sbom.cdx.json",
        "trivy.json",
        "exceptions.json",
        *(["exception-review.json"] if report.get("exception_review") else []),
    ):
        actual = hashlib.sha256((directory / scope / filename).read_bytes()).hexdigest()
        if actual != report["files_sha256"][filename]:
            raise common.AuditError(f"Changed baseline artifact: {scope}/{filename}")
    sbom = common.read_json(directory / scope / "sbom.cdx.json")
    if sbom.get("bomFormat") != "CycloneDX":
        raise common.AuditError("Expected CycloneDX baseline")
    return report, {c["purl"] for c in sbom["components"] if c.get("purl")}


def package_changes(before: set[str], after: set[str]) -> dict[str, Any]:
    def group(values: set[str]) -> dict[str, set[str]]:
        grouped: dict[str, set[str]] = defaultdict(set)
        for purl in values:
            # Preserve namespace and registry/platform qualifiers, including scoped
            # npm names. Multiple simultaneously locked versions remain explicit.
            path, separator, qualifiers = purl.partition("?")
            identity, at, version = path.rpartition("@")
            if not at or not version or not identity.startswith("pkg:"):
                raise common.AuditError(f"Unversioned package URL: {purl}")
            grouped[identity + (separator + qualifiers if separator else "")].add(
                version
            )
        return grouped

    old, new = group(before), group(after)
    updated = [
        {"component": key, "before": sorted(old[key]), "after": sorted(new[key])}
        for key in sorted(old.keys() & new.keys())
        if old[key] != new[key]
    ]
    return {
        "added": sorted(after - before),
        "removed": sorted(before - after),
        "updated": updated,
    }


def compare_runs(before: Path, after: Path, scopes: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {"baseline": str(before.resolve()), "scopes": {}}
    for scope in scopes:
        old, old_ids = load_report(before, scope)
        new, new_ids = load_report(after, scope)
        old_findings = {common.exception_key(f): f for f in old["findings"]}
        new_findings = {common.exception_key(f): f for f in new["findings"]}
        result["scopes"][scope] = {
            "before_commit": old["source"]["checkout_commit"],
            "after_commit": new["source"]["checkout_commit"],
            "platform_changed": old.get("image", {}).get("platform")
            != new.get("image", {}).get("platform"),
            "database_changed": old["scanner"]["VulnerabilityDB"]["UpdatedAt"]
            != new["scanner"]["VulnerabilityDB"]["UpdatedAt"],
            "packages": package_changes(old_ids, new_ids),
            "new_findings": [
                new_findings[k]
                for k in sorted(new_findings.keys() - old_findings.keys())
            ],
            "removed_findings": [
                old_findings[k]
                for k in sorted(old_findings.keys() - new_findings.keys())
            ],
            "changed_findings": [
                {"before": old_findings[k], "after": new_findings[k]}
                for k in sorted(old_findings.keys() & new_findings.keys())
                if any(
                    old_findings[k][f] != new_findings[k][f]
                    for f in ("severity", "decision", "fixed_version")
                )
            ],
        }
    return result


def render_changes(changes: dict[str, Any]) -> str:
    lines = [
        "## Зміни проти попереднього аудиту",
        "",
        "Оновлення версії також видно як видалення старого й додавання нового PURL.",
        "",
    ]
    for scope, diff in changes["scopes"].items():
        lines += [
            f"### {scope}",
            "",
            f"`{diff['before_commit']}` → `{diff['after_commit']}`.",
            "",
        ]
        for key, label in (("added", "Додано"), ("removed", "Видалено")):
            lines.append(f"{label}: {len(diff['packages'][key])}.")
            lines += [f"- `{purl}`" for purl in diff["packages"][key]]
            lines.append("")
        for package in diff["packages"]["updated"]:
            lines += [
                f"- `{package['component']}`: {', '.join(package['before'])} → "
                f"{', '.join(package['after'])}."
            ]
        lines += [
            "",
            f"Нових знахідок: {len(diff['new_findings'])}; "
            f"зниклих: {len(diff['removed_findings'])}; "
            f"змінених: {len(diff['changed_findings'])}.",
            "",
        ]
        lines += [
            f"- {f['vulnerability']}: {f['package']} {f['version']} ({f['severity']})."
            for f in diff["new_findings"]
        ]
        if diff["database_changed"]:
            lines += [
                "",
                "База CVE змінилася; нові знахідки можливі без змін залежностей.",
            ]
        if diff["platform_changed"]:
            lines += ["", "Платформа образу змінилася; пакети можуть відрізнятися."]
        lines.append("")
    return "\n".join(lines)
