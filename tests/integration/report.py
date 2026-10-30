"""Render sanitized CI records; raw Controller responses are never report inputs."""

from collections import Counter
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from report_view import dashboard, job_url, status


def render(report, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    rows = report.get("cases", [])
    cleanup_checks = [
        {
            "name": "Cleanup / " + row.get("resource", "resource"),
            "kind": "cleanup",
            "status": "failed"
            if row.get("status") == "retained"
            else status(row.get("status")),
            "summary": row.get("summary", ""),
        }
        for row in report.get("cleanup", [])
    ]
    checks = rows + cleanup_checks
    metadata = report.get("metadata", {})
    if (
        metadata.get("state") == "incomplete"
        or report.get("pending_jobs")
        and metadata.get("state") not in ("running", "finalizing")
    ) and not any(
        row["name"] == "Cleanup / Controller job completion" for row in checks
    ):
        checks.append(
            {
                "name": "Controller job completion",
                "kind": "lifecycle",
                "status": "blocked",
                "summary": "Owned jobs remain active or unverified; this report is incomplete.",
            }
        )
    counts = Counter(row["status"] for row in checks)
    (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    suite = ET.Element("testsuite", name="APD integration", tests=str(len(checks)))
    properties = ET.SubElement(suite, "properties")
    ET.SubElement(
        properties,
        "property",
        name="report_state",
        value=str(metadata.get("state", "legacy")),
    )
    ET.SubElement(
        properties,
        "property",
        name="pending_jobs",
        value=str(len(report.get("pending_jobs", []))),
    )
    for row in checks:
        test = ET.SubElement(
            suite, "testcase", name=row["name"], classname=row.get("kind", "preflight")
        )
        if row["status"] in ("failed", "timed_out", "canceled"):
            ET.SubElement(
                test, "failure", message=row.get("summary", "Execution failed")
            )
        elif row["status"] in ("blocked", "not_selected", "not_reached", "disabled"):
            ET.SubElement(test, "skipped", message=row.get("summary", "Not executed"))
    suite.set(
        "failures",
        str(
            sum(row["status"] in ("failed", "timed_out", "canceled") for row in checks)
        ),
    )
    suite.set(
        "skipped",
        str(
            sum(
                row["status"] in ("blocked", "not_selected", "not_reached", "disabled")
                for row in checks
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
    html = dashboard(report)
    (directory / "report.html").write_text(html)
    markdown = [
        f"# {title}",
        "",
        report.get("scope", ""),
        "",
        summary,
        "",
        "**Report state:** "
        + str(
            metadata.get("state", "finished" if metadata.get("finished") else "interim")
        ),
        "",
    ]
    if report.get("pending_jobs"):
        markdown += [
            "## Jobs still active or unverified",
            "",
            "This report is incomplete until these jobs and their task events settle.",
            "",
        ]
        for row in report["pending_jobs"]:
            url = job_url(report, row)
            label = f"Job {row.get('job_id', 'unknown')}"
            markdown.append(
                f"- [{label}]({url}) — {row.get('controller_status', 'unverified')}"
                if url
                else f"- {label} — unverified"
            )
        markdown.append("")
    failed = [
        row for row in rows if row["status"] in ("failed", "timed_out", "canceled")
    ]
    if failed:
        markdown += ["## ❌ Failures", ""]
        for row in failed:
            text = (
                str(row.get("summary", "Execution failed"))
                .replace("<", "&lt;")
                .replace("\n", " ")
            )
            url = job_url(report, row)
            suffix = f" [Controller logs]({url})" if url else ""
            markdown.append(f"- **{row['name']}** — {text}{suffix}")
        markdown.append("")
    image = report.get("metadata", {}).get("candidate_image")
    if image:
        markdown += ["**Image under test:** `" + str(image) + "`", ""]
    markdown += ["| Case | Status | Job | Result |", "| --- | --- | --- | --- |"]
    for row in rows:
        values = [
            str(row.get(k, ""))
            .replace("|", "&#124;")
            .replace("\n", " ")
            .replace("<", "&lt;")
            for k in ("name", "status", "job_id", "summary")
        ]
        values[1] = {
            "passed": "✅ Passed",
            "failed": "❌ Failed",
            "blocked": "⏸ Blocked",
            "timed_out": "⏱ Timed out",
            "canceled": "⏹ Canceled",
        }.get(row["status"], values[1])
        url = job_url(report, row)
        if url:
            values[2] = f"[{row['job_id']}]({url})"
        markdown.append("| " + " | ".join(values) + " |")
    markdown += [
        "",
        "Download **report.html** for the phase timeline, highlighted failures, EE versions and authenticated Controller log links.",
        "",
    ]
    log = [title, "Report state: " + str(metadata.get("state", "legacy")), summary, ""]
    for pending in report.get("pending_jobs", []):
        log.append(
            f"PENDING job {pending.get('job_id')}: {pending.get('controller_status', 'unverified')}; task events pending={pending.get('events_pending', False)}"
        )
    for row in rows:
        at = row.get("timestamp", "")
        log.append(
            f"{at} [{row['status'].upper()}] [{row.get('phase', 'execution')}] {row['name']}: {row.get('summary', '')}"
        )
        if row.get("workflow_id"):
            log.append(
                f"  Workflow {row['workflow_id']}, node {row.get('node_identifier', '')}; Controller status: {row.get('controller_status', 'unavailable')}"
            )
        url = job_url(report, row)
        if url:
            log.append("  Controller logs: " + url)
        for event in row.get("events", []):
            log.append(
                f"  {event.get('status', 'failed')}: {event.get('task', 'task')} / {event.get('action', '')} / {event.get('host', 'target host')}"
            )
            if event.get("unsupported_parameters"):
                log.append(
                    "  Unsupported module parameters: "
                    + ", ".join(event["unsupported_parameters"])
                )
            if event.get("redacted"):
                log.append("  Task output protected by no_log; raw result omitted.")
    (directory / "run.log").write_text("\n".join(log) + "\n")
    (directory / "summary.md").write_text("\n".join(markdown) + "\n")


def sample(graph, output):
    report = {
        "schema_version": 1,
        "sample": True,
        "scope": "Illustrative report only. No APD connection, EE validation or infrastructure changes occurred.",
        "metadata": {
            "profile": "catalog",
            "fixture_guard": "disabled",
            "state": "finished",
            "candidate_commit": "a" * 40,
            "candidate_image": "ghcr.io/example/product-demos/apd-ee-27@sha256:"
            + "a" * 64,
            "repository": "https://github.com/example/product-demos",
            "controller_url": "https://controller.example.test",
            "started": "2026-10-29T12:00:00+00:00",
            "finished": "2026-10-29T12:06:30+00:00",
            "runtime_digest": "unobservable; configured digest verified only",
        },
        "cases": [
            {
                "name": "Candidate runtime workflow",
                "status": "passed",
                "kind": "diagnostic",
                "job_id": 101,
                "phase": "runtime",
                "elapsed": 32,
                "ee_provenance": "configured digest verified",
                "hosts": 1,
                "summary": "Runtime and collection constraints satisfied (sample).",
            },
            {
                "name": "APD | Multi-demo setup",
                "status": "passed",
                "kind": "job_template",
                "job_id": 102,
                "phase": "setup",
                "elapsed": 240,
                "ee_provenance": "configured digest verified",
                "hosts": 2,
                "summary": "Cloud, Infrastructure, Linux, Network and Windows templates installed (sample).",
            },
            {
                "name": "Fixture guard disabled",
                "status": "disabled",
                "kind": "diagnostic",
                "phase": "baseline",
                "summary": "Baseline and ownership diagnostics are disabled; demo jobs run directly (sample).",
            },
            {
                "name": "Deploy Cloud Stack in AWS",
                "status": "failed",
                "kind": "workflow_job_template",
                "phase": "cloud",
                "job_id": 103,
                "summary": "An actual deployment child job failed; its result is shown below (sample).",
            },
            {
                "name": "Cloud | AWS | Create VM",
                "workflow_id": 103,
                "workflow_name": "Deploy Cloud Stack in AWS",
                "node_identifier": "Create VM (sample)",
                "controller_status": "failed",
                "status": "failed",
                "kind": "job_template",
                "phase": "cloud",
                "job_id": 104,
                "elapsed": 7,
                "ee_provenance": "configured digest verified",
                "summary": "EC2 instance deployment failed with an illustrative Python dependency error (sample).",
                "failure_categories": ["python_dependency"],
                "events": [
                    {
                        "task": "Create instance",
                        "action": "amazon.aws.ec2_instance",
                        "host": "localhost",
                        "status": "failed",
                        "redacted": True,
                        "source": "collections/ansible_collections/demo/cloud/roles/aws/tasks/create_vm.yml",
                        "line": 99,
                    }
                ],
            },
            {
                "name": "NETWORK | Deploy Containerlab Stack",
                "status": "passed",
                "kind": "workflow_job_template",
                "phase": "network",
                "job_id": 105,
                "summary": "The independent Containerlab workflow still ran after the cloud failure (sample).",
            },
            {
                "name": "LINUX | Fact Scan",
                "status": "blocked",
                "kind": "coverage",
                "phase": "linux",
                "summary": "The required cloud hosts were not deployed (sample).",
            },
        ],
        "runtime": {
            "core": "2.16.19",
            "compatible": True,
            "architecture": "x86_64",
            "collections": [
                {
                    "name": "amazon.aws",
                    "version": "9.5.2",
                    "source_constraint": ">=7.5.0,<10.0.0",
                    "status": "compatible",
                },
                {
                    "name": "community.aws",
                    "version": "9.3.0",
                    "source_constraint": ">=7.0.0,<10.0.0",
                    "status": "compatible",
                },
            ],
            "packages": {"passlib": "1.7.4", "ncclient": "0.7.0", "pywinrm": "0.5.0"},
            "tools": {"helm": True, "terraform": True, "oc": True},
        },
        "ee_build": {"mode": "built", "platform": "linux/amd64"},
        "coverage": {entry["name"]: "blocked" for entry in graph["templates"]},
        "scenarios": [
            {
                "id": "cloud-stack",
                "status": "failed",
                "summary": "A deployment child failed (sample).",
            },
            {
                "id": "containerlab",
                "status": "passed",
                "summary": "Independent deployment continued (sample).",
            },
            {
                "id": "linux-general",
                "status": "blocked",
                "summary": "Cloud hosts were not deployed (sample).",
            },
        ],
        "workflow_reuse": {
            "Cloud | AWS | Create VM": {
                "workflows": ["Deploy Cloud Stack in AWS"],
                "job_ids": [104],
                "status": "failed",
                "summary": "The workflow child is the test execution; no duplicate standalone launch (sample).",
            }
        },
        "cleanup": [
            {
                "resource": "temporary Controller project / EE / templates",
                "status": "passed (sample)",
            }
        ],
    }
    report["build_runtime"] = report["runtime"]
    for row in report["cases"]:
        if row["name"] in report["coverage"]:
            report["coverage"][row["name"]] = row["status"]
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
