"""Accessible, self-contained report dashboard over sanitized runner records."""

from collections import Counter
from html import escape
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import quote, urlparse


LABELS = {
    "passed": "Passed",
    "failed": "Failed",
    "timed_out": "Timed out",
    "canceled": "Canceled",
    "blocked": "Blocked",
    "not_reached": "Not reached",
    "not_selected": "Not selected",
    "running": "Running",
    "not_started": "Not run",
    "retained": "Retained",
    "disabled": "Disabled",
    "deferred": "Deferred",
}
BAD = {"failed", "timed_out", "canceled"}
PHASES = [
    ("configure", "Configure APD"),
    ("runtime", "Validate EE"),
    ("setup", "Install demos"),
    ("baseline", "Fixture guard"),
    ("cloud", "Cloud tests"),
    ("linux", "Linux tests"),
    ("windows", "Windows tests"),
    ("infrastructure", "Infrastructure tests"),
    ("network", "Network tests"),
    ("cleanup", "Cleanup"),
]


def status(value):
    value = str(value).split(" ")[0]
    return value if value in LABELS else "not_started"


def badge(value):
    value = status(value)
    icon = (
        "✓"
        if value == "passed"
        else "!"
        if value in BAD
        else "↻"
        if value == "running"
        else "—"
    )
    return f'<span class="badge {value}"><span aria-hidden="true">{icon}</span> {LABELS[value]}</span>'


def origin(value):
    parsed = urlparse(str(value or ""))
    if (
        parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and parsed.path in ("", "/")
        and not parsed.query
        and not parsed.fragment
    ):
        return str(value).rstrip("/")
    return None


def job_url(report, row):
    base = origin(report.get("metadata", {}).get("controller_url"))
    identifier = row.get("job_id")
    if not base or type(identifier) is not int or identifier <= 0:
        return None
    kind = {
        "workflow_job_template": "workflow",
        "workflow_job": "workflow",
        "inventory_update": "inventory",
        "project_update": "project",
    }.get(row.get("kind"), "playbook")
    return f"{base}/execution/jobs/{kind}/{identifier}/output"


def phase(row):
    if row.get("phase") in dict(PHASES):
        return row["phase"]
    name = row.get("name", "")
    if "runtime" in name.lower():
        return "runtime"
    if "setup" in name.lower() or "credential" in name.lower():
        return "setup"
    if name.startswith("Fixture"):
        return "baseline"
    if "NETWORK" in name or "ContainerLab" in name:
        return "network"
    if name.startswith(("Cloud", "Deploy Cloud", "LINUX", "WINDOWS", "AWS Inventory")):
        return "cloud"
    return "configure"


def display_time(value):
    try:
        return (
            datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .strftime("%d %b %Y · %H:%M UTC")
        )
    except (ValueError, TypeError):
        return "See Actions run"


def duration(value):
    try:
        seconds = max(0, int(float(value)))
    except (ValueError, TypeError):
        return "—"
    return f"{seconds // 60}m {seconds % 60:02}s" if seconds >= 60 else f"{seconds}s"


def link(url, label, class_=""):
    return f'<a class="{class_}" href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{escape(label)}</a>'


def event_table(report, events):
    rows = []
    metadata = report.get("metadata", {})
    repository = metadata.get("repository", "")
    revision = metadata.get("candidate_commit", "")
    for event in events:
        source = event.get("source", "")
        source_link = escape(source) or "Controller event"
        if (
            re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+", repository)
            and re.fullmatch(r"[a-f0-9]{40}", revision)
            and source
            and not source.startswith("/")
            and ".." not in source.split("/")
        ):
            line = event.get("line", 1)
            line = line if type(line) is int and line > 0 else 1
            source_link = link(
                f"{repository}/blob/{revision}/{quote(source, safe='/')}#L{line}",
                f"{source}:{line}",
            )
        note = (
            "Task output protected by no_log"
            if event.get("redacted")
            else "Raw task results remain in Controller"
        )
        if event.get("unsupported_parameters"):
            note = "Unsupported parameters: " + ", ".join(
                event["unsupported_parameters"]
            )
        rows.append(
            f'<tr><td>{badge("failed")}</td><td><strong>{escape(event.get("task", "Task"))}</strong><small>{escape(event.get("action", ""))} · {escape(event.get("host", "target host"))}</small></td><td>{source_link}<small>{note}</small></td></tr>'
        )
    return (
        '<div class="table-scroll"><table><thead><tr><th>Event</th><th>Task / module / host</th><th>Source / output</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table></div>"
        if rows
        else '<p class="muted">No task events were captured. Open the Controller job for authenticated logs; raw stdout is not included in this artifact.</p>'
    )


def dashboard(report):
    e = lambda value: escape(str(value))
    rows = [dict(row) for row in report.get("cases", [])]
    for index, row in enumerate(rows):
        if row.get("name") == "Run preflight / execution" and not row.get("phase"):
            previous = next(
                (
                    item
                    for item in reversed(rows[:index])
                    if status(item.get("status")) in BAD
                ),
                None,
            )
            if previous:
                row["phase"] = phase(previous)
    cleanup = report.get("cleanup", [])
    metadata = report.get("metadata", {})
    runtime = report.get("runtime", {})
    build_only = report.get("kind") == "ee_build" or bool(
        report.get("ee_build") and not metadata.get("profile")
    )
    failures = [
        (index, row)
        for index, row in enumerate(rows)
        if status(row.get("status")) in BAD
    ]
    cleanup_failed = any(
        status(row.get("status")) in BAD or row.get("status") == "retained"
        for row in cleanup
    )
    complete = bool(
        metadata.get("finished")
        or build_only
        and any(
            row.get("name") in ("Publish candidate EE", "Resolve externally built EE")
            and row.get("status") == "passed"
            for row in rows
        )
    )
    incomplete = any(row.get("status") == "blocked" for row in rows)
    state = metadata.get("state")
    pending = report.get("pending_jobs", [])
    unsettled = state == "incomplete" or bool(pending) and state == "finished"
    if state in ("running", "finalizing"):
        complete = False
        outcome = "running"
    elif unsettled:
        complete = False
        outcome = "blocked"
    else:
        outcome = (
            "failed"
            if failures or cleanup_failed
            else "blocked"
            if complete and incomplete
            else "passed"
            if complete
            else "running"
        )
    title = "EE build report" if build_only else "APD integration report"
    headline = {
        "failed": "Run failed — inspect the highlighted checks",
        "passed": "Run completed successfully",
        "running": "Run in progress — failures recorded"
        if failures
        else "Run in progress",
        "blocked": "Report incomplete — Controller jobs remain active or unverified"
        if unsettled
        else "Run incomplete — some templates could not execute",
    }[outcome]
    if state == "finalizing":
        headline = "Finalizing — waiting for jobs and cleanup"
    pending_rows = "".join(
        f'<li><strong>{e(row.get("name", "Controller job"))}</strong> · job {e(row.get("job_id", "unknown"))} · {e(row.get("controller_status", "unverified"))}'
        + (" · task events pending" if row.get("events_pending") else "")
        + (
            " · child observation incomplete"
            if row.get("observation_incomplete")
            else ""
        )
        + "</li>"
        for row in pending
    )
    lifecycle_note = (
        '<section class="lifecycle-note"><h2>Execution is not yet fully accounted for</h2><p>This is an interim or incomplete report. Cleanup and binding restoration must wait for owned jobs and workflow children.</p><ul>'
        + pending_rows
        + "</ul></section>"
        if pending or unsettled
        else ""
    )
    counts = Counter(status(row.get("status")) for row in rows + cleanup)
    failure_count = len(failures) + sum(
        status(row.get("status")) in BAD or row.get("status") == "retained"
        for row in cleanup
    )
    banner = (
        '<div class="sample-banner">SAMPLE DATA · This preview did not connect to APD or provision resources.</div>'
        if report.get("sample")
        else ""
    )
    image = (
        metadata.get("candidate_image")
        or report.get("ee_build", {}).get("image_tag")
        or "Image not resolved yet"
    )
    source = (
        "Upstream CI build · GHCR"
        if str(image).startswith("ghcr.io/")
        and report.get("ee_build", {}).get("mode") == "built"
        else "Selected execution environment"
    )
    versions = {
        row["name"]: row.get("version", "unavailable")
        for row in runtime.get("collections", [])
    }
    chips = "".join(
        f'<span class="version-chip"><b>{e(name)}</b> {e(value)}</span>'
        for name, value in [
            ("Ansible core", runtime.get("core", "pending")),
            ("amazon.aws", versions.get("amazon.aws", "pending")),
            ("community.aws", versions.get("community.aws", "pending")),
        ]
    )
    phase_order = (
        [
            ("configure", "Authenticate"),
            ("setup", "Build image"),
            ("runtime", "Validate runtime"),
            ("cloud", "Publish GHCR"),
        ]
        if build_only
        else PHASES
    )
    groups = {key: [] for key, _ in phase_order}
    for index, row in enumerate(rows):
        key = phase(row)
        if build_only:
            key = (
                "runtime"
                if "compatibility" in row["name"]
                else "setup"
                if row["name"] == "Build source EE"
                else "cloud"
                if "Publish" in row["name"] or "externally" in row["name"]
                else "configure"
            )
        groups.setdefault(key, []).append((index, row))

    def group_status(key):
        values = [status(row.get("status")) for _, row in groups[key]]
        if key == "cleanup" and cleanup:
            values += [status(row.get("status")) for row in cleanup]
        return (
            "failed"
            if any(value in BAD or value == "retained" for value in values)
            else "disabled"
            if values and all(value == "disabled" for value in values)
            else "passed"
            if values and all(value in ("passed", "disabled") for value in values)
            else "blocked"
            if values
            else "not_started"
        )

    phases = "".join(
        f'<li class="{group_status(key)}"><span class="step-number">{index + 1:02}</span><strong>{e(label)}</strong>{badge(group_status(key))}<small>{len(groups[key]) + (len(cleanup) if key == "cleanup" else 0)} checks</small></li>'
        for index, (key, label) in enumerate(phase_order)
    )
    failure_cards = []
    for index, row in failures:
        url = job_url(report, row)
        log = (
            link(url, f"Controller job {row['job_id']} ↗", "button subtle")
            if url
            else f'<span class="muted">{e(row.get("kind", "assertion"))}</span>'
        )
        failure_cards.append(
            f'<article class="failure-card"><div>{badge(row["status"])} <span class="eyebrow">{e(dict(PHASES).get(phase(row), "Build"))}</span></div><h3><a href="#case-{index}">{e(row["name"])}</a></h3><p>{e(row.get("summary", "Execution failed"))}</p>{log}</article>'
        )
    if cleanup_failed:
        failure_cards.append(
            '<article class="failure-card"><h3>Cleanup needs attention</h3><p>Review the cleanup section and resource ledger before reusing this APD instance.</p><a href="#cleanup">Inspect cleanup ↓</a></article>'
        )
    failure_panel = (
        '<section id="failures"><div class="section-heading"><h2>What failed</h2><span>Start here</span></div><div class="failure-grid">'
        + "".join(failure_cards)
        + "</div></section>"
        if failure_cards
        else '<section class="success-note">'
        + (
            "✓ No failures reported. Cleanup is complete."
            if outcome == "passed"
            else "Some templates could not execute. Review blocked cases and scenario prerequisites below."
            if outcome == "blocked"
            else "No failures recorded yet. This run has not finished."
        )
        + "</section>"
    )
    executions = []
    for key, label in phase_order:
        if key == "cleanup":
            continue
        cases = []
        for index, row in groups[key]:
            url = job_url(report, row)
            job_link = (
                link(url, f"Open job {row['job_id']} logs ↗", "button") if url else ""
            )
            categories = ", ".join(row.get("failure_categories", [])) or "None"
            details = event_table(report, row.get("events", []))
            evidence = e(row.get("ee_provenance", "Not a Controller execution"))
            parent_url = job_url(
                report,
                {"job_id": row.get("workflow_id"), "kind": "workflow_job_template"},
            )
            parent_note = (
                '<p class="workflow-context">Workflow: '
                + link(
                    parent_url,
                    row.get("workflow_name", "Parent workflow")
                    + " · "
                    + str(row["workflow_id"]),
                )
                + " · node "
                + e(row.get("node_identifier", ""))
                + " · Controller status: "
                + e(row.get("controller_status", row["status"]))
                + "</p>"
                if parent_url
                else ""
            )
            cases.append(
                f'<details class="case {status(row["status"])}" id="case-{index}" {"open" if status(row["status"]) in BAD else ""}><summary>{badge(row["status"])}<strong>{e(row["name"])}</strong><span class="case-duration">{duration(row.get("elapsed"))}</span></summary><div class="case-body">{parent_note}<p>{e(row.get("summary", ""))}</p><div class="case-meta"><span>EE: {evidence}</span><span>Hosts: {e(row.get("hosts", "—"))}</span><span>Category: {e(categories)}</span></div>{job_link}{details}</div></details>'
            )
        if cases:
            executions.append(
                f'<section><div class="section-heading"><h2>{e(label)}</h2>{badge(group_status(key))}</div>'
                + "".join(cases)
                + "</section>"
            )
    expected = {
        row["name"]: row.get("version")
        for row in report.get("build_runtime", {}).get("collections", [])
    }
    collection_rows = "".join(
        f'<tr class="{"problem" if row.get("status") != "compatible" else ""}"><td><code>{e(row["name"])}</code></td><td>{e(expected.get(row["name"], "—"))}</td><td><strong>{e(row.get("version", "unavailable"))}</strong></td><td>{e(row.get("source_constraint", "—"))}</td><td>{badge("passed" if row.get("status") == "compatible" else "failed")}<small>{e(row.get("status", ""))}</small></td></tr>'
        for row in runtime.get("collections", [])
    )
    checks = "".join(
        f'<div class="runtime-check">{badge("passed" if value and value != "unavailable" else "failed")}<strong>{e(name)}</strong><span>{e(value) if group == "packages" else "Executable available" if value else "Check failed"}</span></div>'
        for group in ("packages", "tools")
        for name, value in runtime.get(group, {}).items()
    )
    runtime_fail = (
        '<p class="inline-failure">EE validation failures are highlighted below.</p>'
        if runtime.get("compatible") is False
        else ""
    )
    cleanup_rows = "".join(
        f'<tr><td>{badge(row.get("status"))}</td><td><strong>{e(row.get("resource", "Resource"))}</strong><small>{e(row.get("summary", ""))}</small></td></tr>'
        for row in cleanup
    )
    coverage = report.get("coverage", {})
    reached = len(
        {
            row["name"]
            for row in rows
            if row.get("job_id") is not None and row["name"] in coverage
        }
    )
    coverage_rows = "".join(
        f"<tr><td>{e(name)}</td><td>{badge(value)}</td></tr>"
        for name, value in sorted(coverage.items())
    )
    job_count = len({row["job_id"] for row in rows if row.get("job_id") is not None})
    restoration_note = (
        '<p class="muted">The original APD image and project settings were restored after this run. For a manual retest, select this candidate image and project revision.</p>'
        if any(
            row.get("resource") == "original Controller bindings and project checkout"
            and row.get("status") == "passed"
            for row in cleanup
        )
        else ""
    )
    scenario_rows = "".join(
        f'<tr><td><strong>{e(row["id"])}</strong></td><td>{badge(row["status"])}</td><td>{e(row.get("summary", ""))}</td></tr>'
        for row in report.get("scenarios", [])
    )
    scenario_panel = (
        '<section id="scenarios"><div class="section-heading"><h2>Scenario dependencies and blockers</h2></div><div class="panel table-scroll"><table><thead><tr><th>Scenario</th><th>Result</th><th>Reason / execution evidence</th></tr></thead><tbody>'
        + scenario_rows
        + "</tbody></table></div></section>"
        if scenario_rows
        else ""
    )
    coverage_note = (
        "Every non-OpenShift template is accounted for. Blocked entries did not execute and do not count as reached or passing. Workflow children count only when Controller actually launched them."
        if metadata.get("profile") == "catalog"
        else "This profile exercises a subset of the catalog. Not-selected cases are not counted as passing. OpenShift is excluded."
    )
    reuse_rows = "".join(
        f'<tr><td>{e(name)}</td><td>{e(", ".join(row.get("workflows", [])))}</td><td>{e(", ".join(map(str, row.get("job_ids", []))) or "Branch not executed")}</td><td>{badge(row["status"])}</td></tr>'
        for name, row in sorted(report.get("workflow_reuse", {}).items())
    )
    reuse_panel = (
        '<details class="panel" open><summary>Workflow coverage · duplicate standalone launches avoided</summary><p>These templates run through their workflows. Failures remain failures; untaken branches are not passing coverage.</p><div class="table-scroll"><table><thead><tr><th>Template</th><th>Workflow</th><th>Actual child jobs</th><th>Result</th></tr></thead><tbody>'
        + reuse_rows
        + "</tbody></table></div></details>"
        if reuse_rows
        else ""
    )
    stylesheet = Path(__file__).with_name("report.css").read_text()
    advanced = e(
        json.dumps(
            {"metadata": metadata, "ee_build": report.get("ee_build", {})}, indent=2
        )
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><title>{e(title)}</title><style>{stylesheet}</style></head>
<body>{banner}<header><span class="brand">APD <span>/ Integration</span></span><nav><a href="#failures">Failures</a><a href="#executions">Executions</a><a href="#runtime">EE versions</a><a href="#cleanup">Cleanup</a></nav></header>
<main><section class="hero {outcome}"><div class="eyebrow">{e(title)} · {e(metadata.get('run_id', 'Build evidence'))}</div><div class="hero-title"><h1>{headline}</h1>{badge(outcome)}</div><p>{e(report.get('scope', ''))}</p><div class="hero-meta"><span>Profile <b>{e(metadata.get('profile', 'EE build / amd64'))}</b></span><span>Commit <code>{e(str(metadata.get('candidate_commit', 'pending'))[:12])}</code></span><span>Started <b>{e(display_time(metadata.get('started')))}</b></span></div></section>
<div class="metrics"><article><span>Controller jobs</span><strong>{job_count}</strong></article><article><span>Passed checks</span><strong class="green">{counts['passed']}</strong></article><article><span>Failed checks</span><strong class="{'red' if failure_count else ''}">{failure_count}</strong></article><article><span>Catalog templates reached</span><strong>{reached}<small> / {len(coverage)}</small></strong></article></div>
{lifecycle_note}{failure_panel}<section class="image-card"><div class="eyebrow">Image under test</div><h2>{source}</h2><code class="image-ref">{e(image)}</code><div class="version-chips">{chips}</div><p class="muted">{e(metadata.get('runtime_digest', 'Build digest and source checksums are recorded in the evidence.'))}</p>{restoration_note}</section>
<section><div class="section-heading"><h2>Run progression</h2><span>Status includes failure and skipped phases</span></div><ol class="phases {'build-phases' if build_only else ''}">{phases}</ol></section>
{scenario_panel}{reuse_panel}<div id="executions">{''.join(executions)}</div>
<section id="runtime"><div class="section-heading"><h2>Execution environment versions</h2>{badge('passed' if runtime.get('compatible') is True else 'failed' if runtime.get('compatible') is False else 'not_started')}</div>{runtime_fail}<div class="runtime-checks">{checks}</div><details class="panel" {'open' if runtime.get('compatible') is False else ''}><summary>Collection inventory · {len(runtime.get('collections', []))} collections · compare CI build and Controller</summary><div class="table-scroll"><table><thead><tr><th>Collection</th><th>CI build</th><th>Observed</th><th>Source constraint</th><th>Compatibility</th></tr></thead><tbody>{collection_rows}</tbody></table></div></details></section>
<section id="cleanup"><div class="section-heading"><h2>Cleanup and restoration</h2>{badge('failed' if cleanup_failed else 'passed' if cleanup else 'not_started')}</div><div class="panel table-scroll"><table><thead><tr><th>Outcome</th><th>Resource / action</th></tr></thead><tbody>{cleanup_rows or '<tr><td colspan="2">No cleanup result recorded yet.</td></tr>'}</tbody></table></div></section>
<details class="panel"><summary>Full catalog coverage · {reached} / {len(coverage)} reached</summary><p class="muted">{coverage_note}</p><div class="table-scroll"><table><thead><tr><th>Template</th><th>Coverage</th></tr></thead><tbody>{coverage_rows}</tbody></table></div></details>
<details class="panel"><summary>EE build provenance and run metadata</summary><pre>{advanced}</pre></details>
<footer><h2>Download evidence</h2><div class="downloads"><a href="run.log">Sanitized run log</a><a href="report.json">JSON report</a><a href="junit.xml">JUnit results</a></div><p>Task output can contain passwords and tokens. This report includes canonical source task names and normalized failures; full logs remain behind Controller authentication. Source metadata and log links contain no login credentials.</p></footer></main></body></html>"""
