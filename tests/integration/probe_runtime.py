"""Emit only allowlisted runtime metadata from inside the candidate EE."""

import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

from ansible import release
from ansible.plugins.loader import init_plugin_loader
from packaging.specifiers import SpecifierSet
from packaging.version import Version
import yaml


def inspect(requirements):
    init_plugin_loader()
    core = Version(release.__version__)
    result = {
        "core": str(core),
        "python": platform.python_version(),
        "architecture": platform.machine(),
        "collections": [],
        "packages": {},
        "tools": {},
    }
    for name, command in {
        "helm": ["helm", "version", "--short"],
        "terraform": ["terraform", "version", "-json"],
        "oc": ["oc", "version", "--client"],
    }.items():
        result["tools"][name] = False
        if shutil.which(name):
            try:
                checked = subprocess.run(
                    command,
                    capture_output=True,
                    timeout=20,
                    env={**os.environ, "CHECKPOINT_DISABLE": "1"},
                )
                result["tools"][name] = checked.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                pass
    for requirement in yaml.safe_load(Path(requirements).read_text())["collections"]:
        name = requirement["name"]
        row = {
            "name": name,
            "version": None,
            "requires_ansible": None,
            "source_constraint": requirement.get("version"),
            "status": "missing",
        }
        try:
            spec = importlib.util.find_spec("ansible_collections." + name)
            location = Path(next(iter(spec.submodule_search_locations)))
            manifest = json.loads((location / "MANIFEST.json").read_text())
            row["version"] = manifest["collection_info"]["version"]
            metadata = location / "meta/runtime.yml"
            runtime = yaml.safe_load(metadata.read_text()) if metadata.exists() else {}
            row["requires_ansible"] = (runtime or {}).get("requires_ansible")
            row["status"] = "compatible"
            if row["requires_ansible"] and core not in SpecifierSet(
                row["requires_ansible"]
            ):
                row["status"] = "incompatible_core"
            constraint = requirement.get("version")
            if constraint:
                constraint = (
                    constraint
                    if any(c in constraint for c in "<>=!~")
                    else "==" + constraint
                )
                if Version(row["version"]) not in SpecifierSet(constraint):
                    row["status"] = "outside_source_constraint"
        except (
            ImportError,
            AttributeError,
            StopIteration,
            OSError,
            ValueError,
            KeyError,
        ):
            row["status"] = "missing_or_invalid_metadata"
        result["collections"].append(row)
    for package in ("ncclient", "passlib", "pywinrm"):
        try:
            result["packages"][package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result["packages"][package] = None
    result["compatible"] = (
        all(row["status"] == "compatible" for row in result["collections"])
        and all(result["packages"].values())
        and all(result["tools"].values())
    )
    return result


if __name__ == "__main__":
    print(json.dumps(inspect(sys.argv[1]), sort_keys=True))
