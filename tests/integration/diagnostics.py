"""Turn job events into bounded public diagnostics without copying task results."""

from functools import lru_cache
from pathlib import Path
import re

import yaml


@lru_cache(maxsize=200)
def source_task(root, task_path):
    match = re.search(r"(?:/runner/project/|^)([^:]+):(\d+)$", task_path)
    if not match:
        return None
    relative, line = match[1], int(match[2])
    path = (Path(root) / relative).resolve()
    if (
        not path.is_relative_to(Path(root).resolve())
        or path.suffix not in (".yml", ".yaml")
        or not path.is_file()
    ):
        return None
    try:
        document = yaml.compose(path.read_text())
    except (OSError, yaml.YAMLError):
        return None
    found = []
    parameter_names = set()

    def visit(node):
        if isinstance(node, yaml.MappingNode):
            values = {
                key.value: value
                for key, value in node.value
                if isinstance(key, yaml.ScalarNode)
            }
            parameter_names.update(str(key) for key in values)
            name = values.get("name")
            if (
                isinstance(name, yaml.ScalarNode)
                and node.start_mark.line + 1 <= line <= node.end_mark.line + 1
            ):
                found.append((node.start_mark.line, name.value[:200]))
            for _, value in node.value:
                visit(value)
        elif isinstance(node, yaml.SequenceNode):
            for value in node.value:
                visit(value)

    if document:
        visit(document)
    return (
        {
            "task": max(found)[1],
            "source": relative,
            "line": line,
            "parameters": parameter_names,
        }
        if found
        else None
    )


def public_events(events, root, allowed_hosts):
    output = []
    for event in events[:40]:
        data = event.get("event_data", {})
        action = data.get("task_action", "")
        action = (
            action
            if re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){0,2}", str(action))
            else "task"
        )
        source = source_task(str(root), str(data.get("task_path", ""))) or {}
        timestamp = str(event.get("created", ""))
        result = data.get("res", {})
        row = {
            "status": "unreachable"
            if event.get("event") == "runner_on_unreachable"
            else "failed",
            "task": source.get("task", action),
            "action": action,
            "host": data.get("host")
            if data.get("host") in allowed_hosts
            else "target host",
            "redacted": bool(result.get("_ansible_no_log") or result.get("censored")),
        }
        if re.fullmatch(r"[0-9TZ:+. -]{10,40}", timestamp):
            row["timestamp"] = timestamp
        if source:
            row.update(
                {key: value for key, value in source.items() if key != "parameters"}
            )
        unsupported = re.search(
            r"Unsupported parameters for \(([a-z][a-z0-9_.]+)\) module: ([a-z0-9_, ]+)\.",
            str(result.get("msg", "")),
            re.IGNORECASE,
        )
        if unsupported and unsupported[1].split(".")[-1] == action.split(".")[-1]:
            parameters = sorted(
                set(unsupported[2].replace(" ", "").split(","))
                & set(source.get("parameters", []))
            )
            if parameters:
                row["unsupported_module"] = unsupported[1]
                row["unsupported_parameters"] = parameters
        # No event stdout, invocation, message, exception, or rendered task name is copied.
        output.append(row)
    return output
