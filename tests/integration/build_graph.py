"""Build the offline APD integration-test design; never contact or launch Controller."""

import argparse
from collections import Counter, defaultdict
import graphlib
import hashlib
import json
from pathlib import Path
import re
import subprocess

import yaml


class SourceLoader(yaml.SafeLoader):
    """Keep Ansible unsafe scalar values as text without evaluating Jinja."""


SourceLoader.add_constructor(
    "!unsafe", lambda loader, node: loader.construct_scalar(node)
)


def load(path):
    return yaml.load(path.read_text(), Loader=SourceLoader)


def template_id(kind, category, name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    suffix = hashlib.sha256(name.encode()).hexdigest()[:8]
    return f"{kind}:{category}:{slug}-{suffix}"


def catalog(root, excluded):
    result = []
    resources = defaultdict(set)
    definitions = {}
    excluded_templates = []
    paths = [root / "common/bootstrap-vars.yml"] + sorted(root.glob("*/setup.yml"))
    for path in paths:
        category = (
            "bootstrap" if path.name == "bootstrap-vars.yml" else path.parent.name
        )
        document = load(path)
        if not isinstance(document, dict):
            continue
        for key, kind in (
            ("controller_inventory_sources", "inventory_source"),
            ("controller_projects", "project"),
        ):
            for resource in document.get(key, []):
                if category not in excluded:
                    resources[resource["name"]].add(kind)
        for key, kind in (
            ("controller_templates", "job_template"),
            ("controller_workflows", "workflow_job_template"),
        ):
            for template in document.get(key, []):
                name = template["name"]
                if category in excluded:
                    excluded_templates.append(
                        {"category": category, "name": name, "kind": kind}
                    )
                    continue
                identifier = template_id(kind, category, name)
                if name in definitions:
                    raise ValueError(f"Ambiguous template name: {name}")
                survey = template.get("survey", template.get("survey_spec", {}))
                credentials = template.get("credentials", [])
                if isinstance(credentials, str):
                    credentials = [credentials]
                entry = {
                    "id": identifier,
                    "category": category,
                    "name": name,
                    "kind": kind,
                    "source": str(path.relative_to(root)),
                    "playbook": template.get("playbook"),
                    "job_type": template.get("job_type", "run")
                    if kind == "job_template"
                    else None,
                    "inventory": template.get("inventory"),
                    "execution_environment": template.get("execution_environment"),
                    "credentials": credentials,
                    "survey": survey.get("spec", []),
                    "prompt_flags": {
                        k: v for k, v in template.items() if k.startswith("ask_")
                    },
                    "template_extra_vars": template.get("extra_vars", {}),
                }
                result.append(entry)
                definitions[name] = (entry, template)
                resources[name].add(kind)
    return result, definitions, resources, excluded_templates


def workflow_graph(entry, definition, resources):
    raw_nodes = definition.get(
        "simplified_workflow_nodes", definition.get("workflow_nodes", [])
    )
    identifiers = [node["identifier"] for node in raw_nodes]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError(f"Duplicate workflow node in {entry['name']}")
    nodes = []
    edges = []
    parents = {identifier: set() for identifier in identifiers}
    for raw in raw_nodes:
        target = raw.get("unified_job_template")
        kind = None
        if isinstance(target, dict):
            kind = target.get("type")
            target = target["name"]
        if raw.get("approval_node"):
            kind = "workflow_approval"
            target = raw["approval_node"]["name"]
        elif kind is None:
            matches = resources.get(target, set())
            if len(matches) != 1:
                raise ValueError(
                    f"Unresolved workflow target {target!r} in {entry['name']}"
                )
            kind = next(iter(matches))
        elif kind not in resources.get(target, set()):
            raise ValueError(f"Missing {kind} target {target!r} in {entry['name']}")
        nodes.append(
            {
                "identifier": raw["identifier"],
                "target": target,
                "kind": kind,
                "all_parents_must_converge": raw.get(
                    "all_parents_must_converge", False
                ),
                "launch_overrides": {
                    k: raw[k]
                    for k in (
                        "extra_data",
                        "inventory",
                        "limit",
                        "job_type",
                        "credentials",
                    )
                    if k in raw
                },
            }
        )
        related = raw.get("related", raw)
        for field, condition in (
            ("success_nodes", "success"),
            ("failure_nodes", "failure"),
            ("always_nodes", "always"),
        ):
            for child in related.get(field, []):
                child = child["identifier"] if isinstance(child, dict) else child
                if child not in parents:
                    raise ValueError(f"Dangling workflow edge to {child!r}")
                parents[child].add(raw["identifier"])
                edges.append(
                    {"from": raw["identifier"], "to": child, "condition": condition}
                )
    order = list(graphlib.TopologicalSorter(parents).static_order())
    return {
        "template_id": entry["id"],
        "name": entry["name"],
        "nodes": nodes,
        "edges": edges,
        "topological_order": order,
        "roots": [key for key, value in parents.items() if not value],
    }


def build(root, plan):
    templates, definitions, resources, excluded = catalog(
        root, plan["excluded_categories"]
    )
    specialized = plan["ee_under_test"]["specialized_job_bindings"]
    for name in specialized:
        if name not in definitions or definitions[name][0]["kind"] != "job_template":
            raise ValueError(f"Unknown specialized EE template: {name}")
    for entry in templates:
        if entry["kind"] == "job_template":
            entry["test_ee_binding"] = specialized.get(entry["name"], "candidate")
    workflows = {
        entry["name"]: workflow_graph(entry, template, resources)
        for entry, template in definitions.values()
        if entry["kind"] == "workflow_job_template"
    }
    scenarios = plan["scenarios"]
    scenario_ids = [scenario["id"] for scenario in scenarios]
    if len(set(scenario_ids)) != len(scenario_ids):
        raise ValueError("Duplicate scenario ID")
    dependencies = {identifier: set() for identifier in scenario_ids}
    scenario_edges = []
    coverage = defaultdict(list)
    for scenario in scenarios:
        identifier = scenario["id"]
        for gate in scenario.get("requires", []):
            if gate not in plan["gates"]:
                raise ValueError(f"Undefined gate {gate!r}")
        for source in scenario.get("evidence", []):
            if not (root / source).is_file():
                raise ValueError(f"Missing evidence source: {source}")
        for field, condition in (
            ("needs", "success"),
            ("after_completion", "terminal"),
        ):
            for dependency in scenario.get(field, []):
                if dependency not in dependencies:
                    raise ValueError(f"Missing scenario dependency: {dependency}")
                dependencies[identifier].add(dependency)
                scenario_edges.append(
                    {"from": dependency, "to": identifier, "condition": condition}
                )
        for index, step in enumerate(scenario["steps"], 1):
            name = step.get("template", step.get("observe_template"))
            if "inventory" in step and "inventory_source" not in resources.get(
                step["inventory"], set()
            ):
                raise ValueError(f"Missing inventory source: {step['inventory']}")
            if not name:
                continue
            if name not in definitions:
                raise ValueError(f"Missing scenario template: {name}")
            entry, _ = definitions[name]
            accepted = {question["variable"] for question in entry["survey"]}
            if not entry["prompt_flags"].get("ask_variables_on_launch", False):
                unknown = set(step.get("bindings", {})) - accepted
                if unknown:
                    raise ValueError(
                        f"Unprompted bindings for {name}: {sorted(unknown)}"
                    )
            coverage[name].append(
                {
                    "scenario": identifier,
                    "step": index,
                    "mode": "direct"
                    if "template" in step
                    else "observe_external_trigger",
                }
            )
            if name in workflows:
                for node in workflows[name]["nodes"]:
                    if node["kind"] not in ("job_template", "workflow_job_template"):
                        continue
                    coverage[node["target"]].append(
                        {
                            "scenario": identifier,
                            "mode": "workflow_child",
                            "workflow": name,
                            "node": node["identifier"],
                        }
                    )
    order = list(graphlib.TopologicalSorter(dependencies).static_order())
    missing = sorted(set(definitions) - set(coverage))
    if missing:
        raise ValueError(f"Templates without a planned execution: {missing}")
    return {
        "schema_version": plan["schema_version"],
        "status": plan["status"],
        "source_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "execution_policy": plan["execution_policy"],
        "ee_under_test": plan["ee_under_test"],
        "gates": plan["gates"],
        "excluded_templates": excluded,
        "templates": templates,
        "scenarios": scenarios,
        "scenario_edges": scenario_edges,
        "scenario_topological_order": order,
        "workflows": list(workflows.values()),
        "coverage": {name: coverage[name] for name in sorted(coverage)},
    }


def write_outputs(graph, output):
    output.mkdir(parents=True, exist_ok=True)
    (output / "dependency-graph.json").write_text(json.dumps(graph, indent=2) + "\n")
    lines = [
        "# Planned template coverage",
        "",
        "Generated by `build_graph.py`; this is a design inventory, not execution evidence.",
        "",
        f"Source revision: `{graph['source_revision']}`. OpenShift is excluded by source category.",
        "",
        "A workflow-child assignment is planned coverage only. Untaken failure branches, skipped tasks,",
        "missing hosts and missing external triggers must not be counted as successful execution.",
        "",
        "| Category | Jobs | Workflows |",
        "| --- | ---: | ---: |",
    ]
    counts = Counter((item["category"], item["kind"]) for item in graph["templates"])
    for category in sorted({item["category"] for item in graph["templates"]}):
        lines.append(
            f"| {category} | {counts[category, 'job_template']} | "
            f"{counts[category, 'workflow_job_template']} |"
        )
    lines += [
        "",
        "## Scenario order",
        "",
        "Topological order; success and terminal dependencies differ.",
        "",
    ]
    lines += [
        f"{i}. `{name}`"
        for i, name in enumerate(graph["scenario_topological_order"], 1)
    ]
    for category in sorted({item["category"] for item in graph["templates"]}):
        lines += [
            "",
            f"## {category.title()}",
            "",
            "| Template | Kind | Planned scenarios |",
            "| --- | --- | --- |",
        ]
        for entry in graph["templates"]:
            if entry["category"] != category:
                continue
            cases = sorted(
                {row["scenario"] for row in graph["coverage"][entry["name"]]}
            )
            name = entry["name"].replace("|", "&#124;")
            lines.append(f"| {name} | {entry['kind']} | {', '.join(cases)} |")
    (output / "coverage.md").write_text("\n".join(lines) + "\n")
    mermaid = ["flowchart TD"]
    for scenario in graph["scenarios"]:
        identifier = scenario["id"].replace("-", "_")
        mermaid.append(f'  {identifier}["{scenario["id"]}"]')
    for edge in graph["scenario_edges"]:
        start, end = edge["from"].replace("-", "_"), edge["to"].replace("-", "_")
        arrow = "-->" if edge["condition"] == "success" else "-. terminal .->"
        mermaid.append(f"  {start} {arrow} {end}")
    (output / "scenario-graph.mmd").write_text("\n".join(mermaid) + "\n")
    lines = [
        "# Configured workflow graphs",
        "",
        "Generated from setup YAML, including conditional edges and join behavior.",
        "",
    ]
    for workflow in graph["workflows"]:
        lines += [f"## {workflow['name']}", "", "```mermaid", "flowchart TD"]
        ids = {node["identifier"]: f"n{i}" for i, node in enumerate(workflow["nodes"])}
        for node in workflow["nodes"]:
            label = node["identifier"].replace('"', "'")
            join = " / ALL parents" if node["all_parents_must_converge"] else ""
            lines.append(f'  {ids[node["identifier"]]}["{label}{join}"]')
        for edge in workflow["edges"]:
            lines.append(
                f'  {ids[edge["from"]]} -->|{edge["condition"]}| {ids[edge["to"]]}'
            )
        lines += ["```", "", "| Node | Type | Target |", "| --- | --- | --- |"]
        for node in workflow["nodes"]:
            target = node["target"].replace("|", "&#124;")
            lines.append(f"| {node['identifier']} | {node['kind']} | {target} |")
        lines.append("")
    (output / "workflows.md").write_text("\n".join(lines).rstrip() + "\n")
    print(
        f"Validated {len(graph['templates'])} templates, {len(graph['workflows'])} workflows, "
        f"{len(graph['scenarios'])} scenarios; no missing coverage, unresolved edges or cycles."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path(__file__).resolve().parent / "generated"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    plan = load(Path(__file__).with_name("scenarios.yml"))
    write_outputs(build(root, plan), args.output)


if __name__ == "__main__":
    main()
