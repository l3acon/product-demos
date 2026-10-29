"""Render sanitized CI records; raw Controller responses are never report inputs."""

from collections import Counter
from html import escape
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def render(report, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    rows = report.get("cases", [])
    counts = Counter(row["status"] for row in rows)
    (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    suite = ET.Element("testsuite", name="APD integration", tests=str(len(rows)))
    for row in rows:
        test = ET.SubElement(
            suite, "testcase", name=row["name"], classname=row.get("kind", "preflight")
        )
        if row["status"] in ("failed", "timed_out", "canceled"):
            ET.SubElement(
                test, "failure", message=row.get("summary", "Execution failed")
            )
        elif row["status"] in ("blocked", "not_selected", "not_reached"):
            ET.SubElement(test, "skipped", message=row.get("summary", "Not executed"))
    suite.set(
        "failures",
        str(sum(row["status"] in ("failed", "timed_out", "canceled") for row in rows)),
    )
    suite.set(
        "skipped",
        str(
            sum(
                row["status"] in ("blocked", "not_selected", "not_reached")
                for row in rows
            )
        ),
    )
    ET.ElementTree(suite).write(
        directory / "junit.xml", encoding="utf-8", xml_declaration=True
    )
    title = (
        "SAMPLE DATA — APD report preview"
        if report.get("sample")
        else "APD integration report"
    )
    summary = (
        ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
        or "No cases completed"
    )
    metadata = report.get("metadata", {})
    items = "".join(
        f"<dt>{escape(str(k))}</dt><dd>{escape(str(v))}</dd>"
        for k, v in metadata.items()
    )
    body = "".join(
        "<tr>"
        + "".join(
            f"<td>{escape(str(row.get(k, '')))}</td>"
            for k in ("name", "status", "job_id", "ee_provenance", "hosts", "summary")
        )
        + "</tr>"
        for row in rows
    )
    coverage = report.get("coverage", {})
    coverage_rows = "".join(
        f"<tr><td>{escape(name)}</td><td>{escape(status)}</td></tr>"
        for name, status in sorted(coverage.items())
    )
    runtime = escape(json.dumps(report.get("runtime", {}), indent=2))
    build = (
        "<h2>EE build provenance</h2><pre>"
        + escape(json.dumps(report["ee_build"], indent=2))
        + "</pre>"
        if report.get("ee_build")
        else ""
    )
    diagnostics = report.get("runtime", {})
    failed_checks = [
        (
            row.get("name"),
            row.get("version"),
            row.get("source_constraint"),
            row.get("requires_ansible"),
            row.get("status"),
        )
        for row in diagnostics.get("collections", [])
        if row.get("status") != "compatible"
    ]
    failed_checks += [
        (name, value, "installed", "", "missing Python package")
        for name, value in diagnostics.get("packages", {}).items()
        if value == "unavailable"
    ]
    failed_checks += [
        (name, "failed", "executable succeeds", "", "tool check failed")
        for name, value in diagnostics.get("tools", {}).items()
        if not value
    ]
    failures = "".join(
        "<tr>"
        + "".join(f"<td>{escape(str(value or ''))}</td>" for value in row)
        + "</tr>"
        for row in failed_checks
    )
    runtime_failures = (
        "<h2>EE validation failures</h2><table><thead><tr><th>Check</th><th>Observed</th><th>Source requirement</th><th>Required core</th><th>Result</th></tr></thead><tbody>"
        + failures
        + "</tbody></table>"
        if failed_checks
        else ""
    )
    html = f"""<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>{title}</title><style>
body{{font:16px system-ui,sans-serif;max-width:1100px;margin:40px auto;padding:0 20px;color:#18222d;background:#f7f9fb}}
h1{{font-size:28px}}h2{{margin-top:32px}}table{{border-collapse:collapse;width:100%;background:white}}
td,th{{padding:12px;text-align:left;border-bottom:1px solid #dce3e9;overflow-wrap:anywhere}}
dl{{display:grid;grid-template-columns:180px 1fr;gap:8px}}dd{{margin:0;overflow-wrap:anywhere}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#eaf0f5;padding:16px}}
.note{{padding:16px;border-left:4px solid #597288;background:#edf2f7}}
@media(max-width:650px){{dl{{display:block}}dt{{font-weight:bold}}table{{font-size:13px}}td,th{{padding:7px}}}}
</style><main><h1>{title}</h1><p>{escape(summary)}</p>
<p class="note">{escape(report.get('scope', ''))}</p><dl>{items}</dl>
<h2>Executions and assertions</h2><table><thead><tr><th>Case</th><th>Status</th><th>Job ID</th><th>EE evidence</th><th>Hosts</th><th>Result</th></tr></thead><tbody>{body}</tbody></table>
{build}{runtime_failures}<h2>Candidate runtime</h2><pre>{runtime}</pre>
<h2>Full catalog coverage</h2><p>Only reached APD template executions count. Diagnostic jobs are listed separately above.</p>
<table><thead><tr><th>Template</th><th>Coverage</th></tr></thead><tbody>{coverage_rows}</tbody></table>
<h2>Cleanup</h2><pre>{escape(json.dumps(report.get('cleanup', []), indent=2))}</pre>
<p>Raw output and input variables are omitted. Inspect failed job IDs in your Controller for detailed diagnostics.</p></main></html>"""
    (directory / "report.html").write_text(html)
    markdown = [
        f"# {title}",
        "",
        report.get("scope", ""),
        "",
        summary,
        "",
        "| Case | Status | Job | Result |",
        "| --- | --- | --- | --- |",
    ]
    for row in rows:
        values = [
            str(row.get(k, ""))
            .replace("|", "&#124;")
            .replace("\n", " ")
            .replace("<", "&lt;")
            for k in ("name", "status", "job_id", "summary")
        ]
        markdown.append("| " + " | ".join(values) + " |")
    markdown += [
        "",
        "Download the report artifact and open report.html for runtime, full coverage and cleanup details.",
    ]
    (directory / "summary.md").write_text("\n".join(markdown) + "\n")


def sample(graph, output):
    report = {
        "schema_version": 1,
        "sample": True,
        "scope": "Illustrative report only. No APD connection, EE validation or infrastructure changes occurred.",
        "metadata": {
            "profile": "ee-smoke",
            "candidate_commit": "sample-commit",
            "candidate_image": "quay.io/example/apd-ee@sha256:" + "a" * 64,
            "runtime_digest": "unobservable; configured digest verified only",
        },
        "cases": [
            {
                "name": "Candidate runtime workflow",
                "status": "passed",
                "kind": "diagnostic",
                "job_id": "sample-101",
                "ee_provenance": "configured digest verified",
                "hosts": 1,
                "summary": "Runtime and collection constraints satisfied (sample).",
            },
            {
                "name": "LINUX | Fact Scan",
                "status": "passed",
                "kind": "job_template",
                "job_id": "sample-102",
                "ee_provenance": "configured digest verified",
                "hosts": 2,
                "summary": "Expected Linux hosts and package/service fact tasks completed (sample).",
            },
            {
                "name": "WINDOWS | Test Connectivity",
                "status": "blocked",
                "kind": "job_template",
                "summary": "No eligible Windows hosts in the selected inventory (sample).",
            },
        ],
        "runtime": {
            "core": "2.16.19",
            "architecture": "x86_64",
            "collections": [
                {"name": "amazon.aws", "version": "9.5.2", "status": "compatible"}
            ],
        },
        "coverage": {entry["name"]: "not_selected" for entry in graph["templates"]},
        "cleanup": [
            {
                "resource": "temporary Controller project / EE / templates",
                "status": "passed (sample)",
            }
        ],
    }
    report["coverage"]["LINUX | Fact Scan"] = "passed (sample)"
    report["coverage"]["WINDOWS | Test Connectivity"] = "blocked (sample)"
    render(report, output)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", action="store_true", required=True)
    parser.add_argument(
        "--graph",
        type=Path,
        default=Path(__file__).with_name("generated") / "dependency-graph.json",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sample(json.loads(args.graph.read_text()), args.output)
