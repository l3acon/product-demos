"""Configure a disposable APD instance and execute approved template integration tests."""

import argparse
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from build_graph import build, load
from build_ee import BuildError, validate_metadata
from catalog import Catalog
from controller import Controller, ControllerError
from diagnostics import public_events
from report import render
from recovery import previous_evidence, reconcile_preprovisioning
from workflow_observer import WorkflowObserver, ready
import yaml


TERMINAL = {"successful", "failed", "error", "canceled"}
JOB_STATUS = {
    "successful": "passed",
    "failed": "failed",
    "error": "failed",
    "canceled": "canceled",
}
SUPPORTED = {
    "cloud": ["LINUX | Fact Scan", "WINDOWS | Test Connectivity"],
    "network": [
        "NETWORK | Containerlab | Configure Devices",
        "NETWORK | Containerlab | Validate Devices",
        "NETWORK | Containerlab | Troubleshoot",
        "NETWORK | Backup",
        "NETWORK | Report",
    ],
}


def now():
    return datetime.now(timezone.utc).isoformat()


def variables(value):
    result = yaml.safe_load(value) if isinstance(value, str) else value
    if result is not None and not isinstance(result, dict):
        raise ControllerError("Controller variables must be a mapping")
    return result or {}


def fresh_machine_inputs(existing, key_factory=None):
    """Fill missing bootstrap authentication without exporting existing secrets."""

    def rsa_key():
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "apd-ci-key"
            subprocess.run(
                [
                    "ssh-keygen",
                    "-q",
                    "-t",
                    "rsa",
                    "-b",
                    "3072",
                    "-N",
                    "",
                    "-f",
                    str(path),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            return path.read_text()

    inputs = dict(existing)
    missing = lambda value: value in (None, "", "REPLACEME")
    if missing(inputs.get("username")):
        inputs["username"] = "ec2-user"
    if missing(inputs.get("password")):
        inputs["password"] = secrets.token_urlsafe(24) + "aA1!"
    if missing(inputs.get("ssh_key_data")):
        inputs["ssh_key_data"] = (key_factory or rsa_key)()
    return inputs


def callback_inputs(connection, credential_type):
    """Apply the installed credential schema instead of assuming version-based types."""
    fields = {
        field["id"]: field
        for field in credential_type.get("inputs", {}).get("fields", [])
    }
    if not fields or set(connection) - fields.keys():
        raise ControllerError(
            "AAP callback credential type does not support the protected connection fields"
        )
    result = dict(connection)
    if "request_timeout" in fields:
        type_ = fields["request_timeout"].get("type", "string")
        if type_ == "string":
            result["request_timeout"] = "10"
        elif type_ in ("integer", "number"):
            result["request_timeout"] = 10
        else:
            raise ControllerError(
                "AAP callback credential has an unsupported request_timeout field type"
            )
    return result


def failure_category(message):
    message = str(message).lower()
    for pattern, category in (
        ("not configured to accept", "workflow_prompt_configuration"),
        ("couldn't resolve module", "missing_collection_or_module"),
        ("could not find", "missing_dependency"),
        ("permission denied", "authentication_or_permissions"),
        ("unauthorized", "authentication_or_permissions"),
        ("unreachable", "host_unreachable"),
        ("unsupported parameter", "unsupported_module_arguments"),
        ("timed out", "timeout"),
        ("already exist", "baseline_not_empty"),
    ):
        if pattern in message:
            return category
    return "execution_failed_inspect_controller"


def public_runtime(value):
    """Never forward arbitrary set_stats data to a public artifact."""

    def version(item):
        item = str(item)
        return (
            item
            if re.fullmatch(r"\d+(?:\.\d+){1,3}(?:[a-z0-9.+-]{0,20})", item)
            else "unavailable"
        )

    result = {key: version(value[key]) for key in ("core", "python") if key in value}
    result["architecture"] = (
        value.get("architecture")
        if value.get("architecture") in ("x86_64", "aarch64", "arm64", "amd64")
        else "unavailable"
    )
    result["compatible"] = value.get("compatible") is True
    result["collections"] = [
        {
            "name": row["name"],
            "version": version(row.get("version")),
            "requires_ansible": row.get("requires_ansible")
            if re.fullmatch(
                r"[<>=!~0-9.,* ]{1,100}", str(row.get("requires_ansible", ""))
            )
            else "unavailable",
            "source_constraint": row.get("source_constraint")
            if re.fullmatch(
                r"[<>=!~0-9.,* ]{1,100}", str(row.get("source_constraint", ""))
            )
            else "unavailable",
            "status": row.get("status")
            if row.get("status")
            in (
                "compatible",
                "incompatible_core",
                "outside_source_constraint",
                "missing",
                "missing_or_invalid_metadata",
            )
            else "invalid",
        }
        for row in value.get("collections", [])[:200]
        if isinstance(row, dict)
        and re.fullmatch(
            r"[a-z][a-z0-9_]{0,40}\.[a-z][a-z0-9_]{0,40}", str(row.get("name", ""))
        )
    ]
    result["tools"] = {
        key: value.get("tools", {}).get(key) is True
        for key in ("helm", "terraform", "oc")
    }
    result["packages"] = {
        key: version(value.get("packages", {}).get(key))
        for key in ("ncclient", "passlib", "pywinrm")
    }
    return result


class Runner:
    def __init__(self, client, args, clock=time.monotonic, sleep=time.sleep):
        self.api, self.args, self.clock, self.sleep = client, args, clock, sleep
        self.phase = "configure"
        self.expected_runtime = None
        self.root = Path(__file__).resolve().parents[2]
        self.graph = build(self.root, load(Path(__file__).with_name("scenarios.yml")))
        self.uid = uuid.uuid4().hex[:24]
        self.owner = "apd-ci-" + self.uid
        self.deadline = clock() + args.timeout
        self.created = []
        self.snapshots = []
        self.active = set()
        self.recorded_jobs = set()
        self.recorded_results = {}
        self.observer = WorkflowObserver(self)
        self.cloud_started = False
        self.network_started = False
        self.baseline_passed = False
        self.fixture_guard = getattr(args, "fixture_guard", False)
        self.pending_cleanup = {}
        self.key_ids = []
        self.lease = None
        self.registry_credential = None
        self.probe_template = None
        self.guard_template = None
        self.bucket_template = None
        self.bucket = "apd-ci-" + self.uid
        self.report = {
            "schema_version": 1,
            "sample": False,
            "scope": (
                "Catalog execution in README dependency order. Every non-OpenShift template is accounted for; missing fixtures and unsupported integrations are blocked explicitly. Template execution results do not imply all planned functional assertions were implemented."
                if args.profile == "catalog"
                else "Smoke profile: runtime validation, category setup, AWS cloud and/or Containerlab, representative jobs, and cleanup. This is not full-catalog acceptance."
            ),
            "metadata": {
                "run_id": args.run_id,
                "resource_owner": self.owner,
                "profile": args.profile,
                "candidate_commit": args.revision,
                "candidate_image": args.image,
                "repository": args.repository,
                "fixture_guard": "enabled" if self.fixture_guard else "disabled",
                "state": "running",
                "started": now(),
                "runtime_digest": "configured immutable digest checked; actual pod image evidence unavailable",
            },
            "cases": [],
            "runtime": {},
            "cleanup": [],
            "coverage": {
                entry["name"]: "not_selected" for entry in self.graph["templates"]
            },
        }
        if getattr(args, "build_metadata", None):
            self.report["ee_build"] = validate_metadata(
                args.build_metadata, args.revision, args.image
            )
        if getattr(args, "require_built_image", False):
            expected = (
                args.repository.removeprefix("https://github.com/")
                .removesuffix(".git")
                .lower()
            )
            if self.report.get("ee_build", {}).get(
                "mode"
            ) != "built" or not args.image.startswith(
                f"ghcr.io/{expected}/apd-ee-27@sha256:"
            ):
                raise ControllerError(
                    "CI requires the immutable GHCR image from its preceding build job"
                )
            source = getattr(args, "build_report", None)
            if not source or source.stat().st_size > 1024 * 1024:
                raise ControllerError(
                    "The CI build runtime report is missing or invalid"
                )
            evidence = json.loads(source.read_text())
            if (
                evidence.get("sample") is not False
                or evidence.get("metadata", {}).get("candidate_commit") != args.revision
                or evidence.get("metadata", {}).get("candidate_image") != args.image
                or evidence.get("runtime", {}).get("compatible") is not True
            ):
                raise ControllerError(
                    "CI build runtime evidence does not match the selected image and revision"
                )
            self.expected_runtime = public_runtime(evidence["runtime"])
            self.report["build_runtime"] = self.expected_runtime
        base = getattr(self.api, "base", None)
        if base:
            self.report["metadata"]["controller_url"] = base
        self.checkpoint()

    def checkpoint(self):
        self.report["pending_jobs"] = self.observer.pending()
        render(self.report, self.args.output)
        ledger = {
            "run_id": self.args.run_id,
            "owner": self.owner,
            "cloud_started": self.cloud_started,
            "network_started": self.network_started,
            "baseline_passed": self.baseline_passed,
            "fixture_guard": self.fixture_guard,
            "pending_cleanup": list(self.pending_cleanup),
            "created": self.created,
            "active": sorted(self.active),
            "unresolved_workflows": sorted(self.observer.unresolved),
            "pending_jobs": self.report["pending_jobs"],
            "snapshot_fields": [
                {"path": path, "fields": sorted(data)} for path, data in self.snapshots
            ],
        }
        (self.args.output / "ledger.json").write_text(
            json.dumps(ledger, indent=2) + "\n"
        )

    def case(self, name, status, kind="assertion", **values):
        row = {
            "name": name,
            "status": status,
            "kind": kind,
            "phase": self.phase,
            "timestamp": now(),
            **values,
        }
        self.report["cases"].append(row)
        print(f"{status}: {name}", flush=True)
        if status == "failed" and values.get("summary"):
            print(values["summary"], flush=True)
        self.checkpoint()
        return row

    def create(self, kind, data):
        row = self.api.post(f"/api/controller/v2/{kind}/", data)
        self.created.append({"kind": kind, "id": row["id"], "name": data["name"]})
        self.checkpoint()
        return row

    def patch(self, path, data):
        original = self.api.get(path)
        self.snapshots.append((path, {key: original[key] for key in data}))
        self.checkpoint()
        return self.api.patch(path, data)

    def wait(self, path, timeout=3600):
        end = min(self.deadline, self.clock() + timeout)
        heartbeat = self.clock() + 60
        self.active.add(path)
        self.checkpoint()
        events_deadline = None
        inspection_failures = 0
        while self.clock() < end:
            row = self.api.get(path)
            self.observer.remember(row)
            if row.get("type") == "workflow_job":
                try:
                    settled = self.observer.inspect(row)
                    inspection_failures = 0
                except ControllerError as error:
                    inspection_failures += 1
                    if inspection_failures >= 3:
                        raise ControllerError(
                            "Workflow child observation failed after retries: "
                            + str(error)
                        ) from None
                    settled = False
                if settled:
                    self.checkpoint()
                    return row
            elif ready(row):
                self.active.discard(path)
                self.checkpoint()
                return row
            elif row.get("status") in TERMINAL:
                if events_deadline is None:
                    events_deadline = self.clock() + 120
                if self.clock() >= events_deadline:
                    raise ControllerError(
                        "Terminal job task events did not finish processing; execution evidence remains incomplete"
                    )
            if self.clock() >= heartbeat:
                print(
                    f"waiting: Controller job {row['id']} is {row.get('status', 'pending')}; waiting for its children and task events",
                    flush=True,
                )
                heartbeat = self.clock() + 60
            self.checkpoint()
            self.sleep(self.args.poll_interval)
        try:
            self.api.post(path + "cancel/")
            # Canceling a workflow does not prove that its children have stopped.
            for _ in range(30):
                row = self.api.get(path)
                self.observer.remember(row)
                if row.get("type") == "workflow_job":
                    if self.observer.inspect(row):
                        break
                elif ready(row):
                    self.active.discard(path)
                    break
                self.sleep(2)
        except ControllerError:
            pass
        self.checkpoint()
        raise ControllerError(
            "Job timeout; cancellation requested and remaining jobs recorded"
        )

    def settle_jobs(self, timeout=180):
        """No teardown or binding restoration may overtake owned jobs."""
        for path in list(self.observer.roots):
            try:
                self.observer.inspect(self.api.get(path))
            except ControllerError:
                self.active.add(path)
                self.observer.unresolved.add(path)
        for path in list(self.active):
            try:
                # Ordinary test failures do not cancel siblings. wait() requests
                # cancellation only if the bounded drain time is exhausted.
                self.wait(path, timeout)
            except ControllerError:
                pass
        # Recheck roots after cancellation to discover any late scheduler children.
        for path in list(self.observer.roots):
            try:
                self.observer.inspect(self.api.get(path))
            except ControllerError:
                self.active.add(path)
                self.observer.unresolved.add(path)
        self.checkpoint()
        quiet = not (self.active or self.observer.unresolved)
        if quiet:
            for path in self.observer.roots:
                self.workflow_children(self.observer.jobs[path])
        return quiet

    def inventory_source(self, name):
        return self.api.unique("inventory_sources", name)

    def template(self, name, workflow=False):
        return self.api.unique(
            "workflow_job_templates" if workflow else "job_templates", name, self.org
        )

    def attach_credentials(self, template, credentials):
        for credential in credentials:
            self.api.post(template["url"] + "credentials/", {"id": credential})

    def diagnostic_template(self, suffix, playbook, extra_vars=None, credentials=()):
        row = self.create(
            "job_templates",
            {
                "name": self.owner + " / " + suffix,
                "project": self.project["id"],
                "inventory": self.local_inventory["id"],
                "execution_environment": self.ee["id"],
                "playbook": playbook,
                "ask_variables_on_launch": True,
                "extra_vars": json.dumps(extra_vars or {}),
                "timeout": 600,
            },
        )
        self.attach_credentials(row, credentials)
        return row

    def configure(self):
        self.org = self.api.unique("organizations", "Ansible Product Demos (APD)")["id"]
        self.project = self.api.unique("projects", "Ansible Product Demos", self.org)
        existing_leases = [
            row
            for row in self.api.collection("inventories")
            if row["name"] == "APD integration lease"
            and row["organization"] == self.org
        ]
        if existing_leases:
            if len(existing_leases) != 1:
                raise ControllerError(
                    "Multiple integration leases exist; manual reconciliation required"
                )
            old = existing_leases[0]
            state = variables(old.get("variables"))
            ledger, report = previous_evidence(
                self.args.repository, state.get("run_id")
            )
            previous = reconcile_preprovisioning(
                self.api, old, ledger, report, self.org, self.project["id"]
            )
            self.case(
                "Previous pre-provisioning lease recovery",
                "passed",
                summary="Verified a completed prior run with no infrastructure provisioning; removed only its temporary Controller objects.",
                previous_run=previous,
            )
        self.lease = self.create(
            "inventories",
            {
                "name": "APD integration lease",
                "organization": self.org,
                "variables": json.dumps(
                    {"owner": self.owner, "run_id": self.args.run_id, "started": now()}
                ),
            },
        )
        self.ee = self.api.unique("execution_environments", "Product Demos EE")
        selected = {"image": self.args.image, "pull": "always"}
        registry = [
            os.environ.get("APD_REGISTRY_" + key, "")
            for key in ("HOST", "USERNAME", "PASSWORD")
        ]
        if any(registry):
            host, username, password = registry
            if (
                not all(registry)
                or host != "ghcr.io"
                or not self.args.image.startswith(host + "/")
            ):
                raise ControllerError(
                    "Registry authentication requires a matching GHCR image and all registry fields"
                )
            credential_type = self.api.unique("credential_types", "Container Registry")
            self.registry_credential = self.create(
                "credentials",
                {
                    "name": self.owner + " / image pull",
                    "organization": self.org,
                    "credential_type": credential_type["id"],
                    "inputs": {
                        "host": host,
                        "username": username,
                        "password": password,
                        "verify_ssl": True,
                    },
                },
            )
            selected["credential"] = self.registry_credential["id"]
        self.patch(self.ee["url"], selected)
        self.ee = self.api.get(self.ee["url"])
        self.patch(
            self.project["url"],
            {
                "scm_url": self.args.repository,
                "scm_branch": self.args.revision,
                "default_environment": self.ee["id"],
                "scm_update_on_launch": False,
            },
        )
        self.patch(
            f"/api/controller/v2/organizations/{self.org}/",
            {"default_environment": self.ee["id"]},
        )
        update = self.api.post(self.project["url"] + "update/")
        result = self.wait(update["url"], 1200)
        if result["status"] != "successful":
            raise ControllerError("Candidate project sync failed")
        synced = self.api.get(self.project["url"])
        if synced["scm_revision"] != self.args.revision:
            raise ControllerError(
                "Controller project revision differs from the reviewed candidate"
            )
        self.case(
            "Candidate project revision",
            "passed",
            summary="Controller synchronized the exact candidate commit.",
        )
        gateway = self.api.get("/api/gateway/v1/ping/")
        version = re.match(r"^(2)\.(5|6|7)(?:\.|$)", gateway.get("version", ""))
        if not version:
            raise ControllerError(
                "Only AAP 2.5, 2.6 and 2.7 are supported by this preview"
            )
        self.series = version[1] + version[2]
        if (
            self.report.get("ee_build", {}).get("mode") == "built"
            and self.series != self.report["ee_build"]["aap_series"]
        ):
            raise ControllerError(
                "The built EE targets AAP 2.7; the supplied APD version differs"
            )
        self.report["metadata"]["aap_series"] = self.series
        self.local_inventory = self.create(
            "inventories",
            {"name": self.owner + " / localhost", "organization": self.org},
        )
        self.api.post(
            self.local_inventory["url"] + "hosts/",
            {
                "name": "localhost",
                "variables": json.dumps({"ansible_connection": "local"}),
            },
        )
        self.probe_template = self.diagnostic_template(
            "runtime",
            "tests/integration/runtime_probe.yml",
            {"ci_aap_series": self.series},
        )

    def bind(self):
        setup_names = {"APD | Single demo setup", "APD | Multi-demo setup"}
        for row in self.api.collection("job_templates"):
            if row.get("project") != self.project["id"] or row["name"].startswith(
                "OpenShift |"
            ):
                continue
            spec = next(
                (
                    entry
                    for entry in self.graph["templates"]
                    if entry["name"] == row["name"]
                ),
                None,
            )
            if (
                row["name"] in setup_names
                or spec
                and spec.get("test_ee_binding") == "candidate"
            ) and row.get("execution_environment") != self.ee["id"]:
                self.patch(row["url"], {"execution_environment": self.ee["id"]})
            if row["name"] in setup_names:
                pinned = variables(row.get("extra_vars"))
                pinned.update(
                    apd_ee_image=self.args.image,
                    apd_git_repo=self.args.repository,
                    apd_git_repo_branch=self.args.revision,
                )
                changed = {}
                if pinned != variables(row.get("extra_vars")):
                    changed["extra_vars"] = json.dumps(pinned)
                if not row.get("ask_variables_on_launch"):
                    changed["ask_variables_on_launch"] = True
                if changed:
                    self.patch(row["url"], changed)
        for row in self.api.collection("inventory_sources"):
            if (
                row["name"]
                in ("AWS Inventory", "ContainerLab Hypervisor", "Insights Inventory")
                and row.get("execution_environment") != self.ee["id"]
            ):
                self.patch(row["url"], {"execution_environment": self.ee["id"]})
        selected = {"image": self.args.image, "pull": "always"}
        if self.registry_credential:
            selected["credential"] = self.registry_credential["id"]
        self.api.patch(self.ee["url"], selected)
        for path in (
            self.project["url"],
            f"/api/controller/v2/organizations/{self.org}/",
        ):
            if self.api.get(path).get("default_environment") != self.ee["id"]:
                self.api.patch(path, {"default_environment": self.ee["id"]})

    def record_job(self, job, name=None, kind="job_template", require_hosts=False):
        if job["url"] in self.recorded_jobs:
            return self.recorded_results[job["url"]]
        if not ready(job):
            self.active.add(job["url"])
            raise ControllerError(
                "A nonterminal job or unfinished task events cannot be recorded as a final result"
            )
        name = name or job.get("name", "Controller job")
        status = JOB_STATUS.get(job["status"], "failed")
        summary = "Execution completed."
        evidence = "not applicable"
        if job.get("execution_environment"):
            environment = self.api.get(
                f"/api/controller/v2/execution_environments/{job['execution_environment']}/"
            )
            evidence = (
                "configured digest verified"
                if environment["image"] == self.args.image
                else "mismatch"
            )
            if evidence == "mismatch":
                status, summary = (
                    "failed",
                    "Job selected an execution environment other than the candidate digest.",
                )
        elif job.get("type") == "job":
            status, summary, evidence = (
                "failed",
                "Job has no candidate execution-environment evidence.",
                "unverified",
            )
        runtime = job.get("artifacts", {}).get("ci_runtime")
        if not isinstance(runtime, dict) and name.startswith(
            "Candidate runtime validation"
        ):
            events = self.api.list(
                job["url"] + "job_events/?event=runner_on_ok&page_size=200"
            )
            for event in events:
                result = event.get("event_data", {}).get("res", {})
                observed = (
                    result.get("ansible_stats", {}).get("data", {}).get("ci_runtime")
                )
                if isinstance(observed, dict):
                    runtime = observed
            if not isinstance(runtime, dict):
                # The registered command result is another structured source if stats were not persisted.
                for event in events:
                    if (
                        event.get("task", event.get("event_data", {}).get("task"))
                        != "Inspect installed collection requirements and runtime metadata"
                    ):
                        continue
                    try:
                        observed = json.loads(
                            event.get("event_data", {}).get("res", {}).get("stdout", "")
                        )
                    except (ValueError, TypeError):
                        continue
                    if isinstance(observed, dict):
                        runtime = observed
        if isinstance(runtime, dict):
            self.report["runtime"] = public_runtime(runtime)
        hosts = sum(job.get("host_status_counts", {}).values())
        if require_hosts and not hosts:
            status, summary = (
                "failed",
                "No target hosts executed; a green empty play is not test coverage.",
            )
        if require_hosts and job.get("type") == "job":
            summaries = self.api.list(job["url"] + "job_host_summaries/?page_size=200")
            reached = {
                row["host_name"]
                for row in summaries
                if row.get("ok", 0) > 0
                and row.get("failures", 0) == 0
                and row.get("dark", 0) == 0
            }
            devices = set(
                load(
                    self.root
                    / "collections/ansible_collections/demo/network/roles/containerlab/defaults/main.yml"
                )["clab_device_catalog"]
            )
            expected = {
                "LINUX | Fact Scan": {"aws-rhel9", "aws-rhel10", "reports"},
                "WINDOWS | Test Connectivity": {"aws-dc", "aws_win1"},
                "NETWORK | Containerlab | Configure Devices": devices,
                "NETWORK | Containerlab | Validate Devices": devices
                | {"clab-hypervisor"},
                "NETWORK | Report": devices | {"clab-hypervisor"},
                "NETWORK | Backup": {"cat8kv1"},
            }.get(name, set())
            if (
                name == "WINDOWS | Test Connectivity"
                and self.observer.parents.get(job["url"], {}).get("workflow_name")
                == "Setup Active Directory Domain"
            ):
                expected = {"dc01", "winston", "winthrop"}
            if not expected <= reached:
                status, summary = (
                    "failed",
                    "Expected target host execution was missing or unsuccessful.",
                )
            required_tasks = {
                "LINUX | Fact Scan": {"Get packages", "Get services"},
                "WINDOWS | Test Connectivity": {"Ping the windows host"},
            }.get(name, set())
            if required_tasks:
                events = self.api.list(
                    job["url"] + "job_events/?event=runner_on_ok&page_size=200"
                )
                completed = {
                    event.get("task", event.get("event_data", {}).get("task"))
                    for event in events
                    if not event.get("event_data", {})
                    .get("res", {})
                    .get("skipped", False)
                }
                if not required_tasks <= completed:
                    status, summary = (
                        "failed",
                        "Required package/service/connectivity tasks did not complete.",
                    )
        if job["status"] != "successful":
            summary = "Controller execution failed. Inspect this job ID in Controller."
        if job.get("type") == "job" and job.get("scm_revision") != self.args.revision:
            status, summary = (
                "failed",
                "Job did not record the reviewed candidate project revision.",
            )
        categories = set()
        diagnostics = []
        if job.get("type") == "job":
            events = self.api.list(
                job["url"] + "job_events/?event=runner_on_failed&page_size=100"
            )
            categories = {
                failure_category(
                    event.get("event_data", {}).get("res", {}).get("msg", "")
                )
                for event in events
            }
            if job["status"] != "successful":
                events += self.api.list(
                    job["url"] + "job_events/?event=runner_on_unreachable&page_size=40"
                )
                allowed_hosts = {
                    "localhost",
                    "aws-dc",
                    "aws_win1",
                    "aws-rhel9",
                    "aws-rhel10",
                    "reports",
                    "clab-hypervisor",
                    "dc01",
                    "winston",
                    "winthrop",
                }
                allowed_hosts.update(
                    load(
                        self.root
                        / "collections/ansible_collections/demo/network/roles/containerlab/defaults/main.yml"
                    )["clab_device_catalog"]
                )
                diagnostics = public_events(events, self.root, allowed_hosts)
                unsupported = next(
                    (
                        event
                        for event in diagnostics
                        if event.get("unsupported_parameters")
                    ),
                    None,
                )
                if unsupported:
                    summary = f"{unsupported['unsupported_module']} rejected unsupported parameters: {', '.join(unsupported['unsupported_parameters'])}. Inspect the source task and tested collection versions."
            if categories and job["status"] != "successful":
                summary = (
                    "Failure categories: "
                    + ", ".join(sorted(categories))
                    + ". Inspect the Controller job for full output."
                )
        if name.startswith("Candidate runtime validation"):
            if self.report["runtime"]:
                checked = self.report["runtime"]
                bad = [
                    row["name"]
                    for row in checked.get("collections", [])
                    if row["status"] != "compatible"
                ]
                missing = [
                    key
                    for key, value in checked.get("packages", {}).items()
                    if value == "unavailable"
                ]
                tools = [
                    key for key, value in checked.get("tools", {}).items() if not value
                ]
                if not checked.get("compatible"):
                    status = "failed"
                    categories = {"ee_compatibility"}
                    summary = f"EE validation failed: collections={', '.join(bad) or 'none'}; missing Python packages={', '.join(missing) or 'none'}; tool checks={', '.join(tools) or 'none'}."
                elif self.expected_runtime and checked != self.expected_runtime:
                    status = "failed"
                    categories = {"build_runtime_mismatch"}
                    summary = "Controller runtime versions differ from the upstream CI build probe; the run is not testing the built runtime."
            else:
                status = "failed"
                categories.add("runtime_metadata_unavailable")
                summary = "Runtime probe failed without structured metadata; inspect the Controller job."
        row = self.case(
            name,
            status,
            kind,
            job_id=job["id"],
            ee_provenance=evidence,
            hosts=hosts,
            summary=summary,
            failure_categories=sorted(categories),
            elapsed=job.get("elapsed", 0),
            events=diagnostics,
            controller_status=job["status"],
            **self.observer.parents.get(job["url"], {}),
        )
        if name in self.report["coverage"]:
            prior = self.report["coverage"][name]
            self.report["coverage"][name] = (
                "failed" if prior == "failed" or row["status"] != "passed" else "passed"
            )
        self.recorded_jobs.add(job["url"])
        self.recorded_results[job["url"]] = status == "passed"
        self.checkpoint()
        return status == "passed"

    def workflow_children(self, job):
        passed = True
        nodes = self.observer.nodes.get(job["url"])
        if nodes is None:
            nodes = self.api.list(job["url"] + "workflow_nodes/?page_size=200")
        definition = next(
            (row for row in self.graph["workflows"] if row["name"] == job["name"]), None
        )
        if definition and {node["identifier"] for node in nodes} != {
            node["identifier"] for node in definition["nodes"]
        }:
            self.case(
                job["name"] + " / workflow graph",
                "failed",
                summary="Executed workflow nodes differ from the candidate source definition.",
            )
            passed = False
        for node in nodes:
            identifier = node.get("identifier", "workflow node")
            target = (
                node.get("summary_fields", {})
                .get("unified_job_template", {})
                .get("name")
            )
            if not node.get("job"):
                missing_template = (
                    "unified_job_template" in node
                    and node["unified_job_template"] is None
                )
                if any(
                    row.get("workflow_id") == job["id"]
                    and row.get("node_identifier") == str(identifier)
                    and row.get("kind") == "workflow_node"
                    for row in self.report["cases"]
                ):
                    passed = (not missing_template) and passed
                    continue
                self.case(
                    target or identifier,
                    "failed" if missing_template else "not_reached",
                    "workflow_node",
                    workflow_id=job["id"],
                    workflow_name=job.get("name", "Workflow"),
                    node_identifier=str(identifier),
                    summary="Workflow node has no job template; no child execution was possible."
                    if missing_template
                    else "Conditional branch was not executed.",
                )
                if missing_template:
                    passed = False
                continue
            self.report["cases"] = [
                row
                for row in self.report["cases"]
                if not (
                    row.get("workflow_id") == job["id"]
                    and row.get("node_identifier") == str(identifier)
                    and row.get("status") == "not_reached"
                )
            ]
            try:
                child_path = self.observer.child_path(node)
                child = self.observer.jobs.get(child_path) or self.api.get(child_path)
                self.observer.parents[child_path] = {
                    "workflow_id": job["id"],
                    "workflow_name": job.get("name", "Workflow"),
                    "node_identifier": str(identifier),
                }
                if not ready(child):
                    child = self.wait(child_path, 1800)
                kind = (
                    "job_template"
                    if child.get("type") == "job"
                    else child.get("type", "supporting_update")
                )
                child_passed = self.record_job(
                    child, target, kind, require_hosts=child.get("type") == "job"
                )
                if child.get("type") == "workflow_job":
                    child_passed = self.workflow_children(child) and child_passed
                passed = child_passed and passed
            except ControllerError as error:
                if not any(
                    row.get("workflow_id") == job["id"]
                    and row.get("node_identifier") == str(identifier)
                    and row.get("kind") == "workflow_node"
                    for row in self.report["cases"]
                ):
                    self.case(
                        target or str(identifier),
                        "failed",
                        "workflow_node",
                        workflow_id=job["id"],
                        workflow_name=job.get("name", "Workflow"),
                        node_identifier=str(identifier),
                        summary="Could not collect this workflow child: " + str(error),
                    )
                if target in self.report["coverage"]:
                    self.report["coverage"][target] = "failed"
                passed = False
        if not passed:
            self.recorded_results[job["url"]] = False
            for row in self.report["cases"]:
                if row.get("job_id") == job["id"]:
                    row.update(
                        status="failed",
                        summary="Workflow execution or child observation failed; inspect its child results.",
                    )
            if job.get("name") in self.report["coverage"]:
                self.report["coverage"][job["name"]] = "failed"
        return passed

    def launch(
        self,
        name,
        variables=None,
        workflow=False,
        diagnostic=False,
        require_hosts=False,
        timeout=3600,
        template=None,
    ):
        if self.clock() >= self.deadline:
            raise ControllerError("Run time budget exhausted before launch")
        self.bind()
        template = template or self.template(name, workflow)
        variables = variables or {}
        if variables and not template.get("ask_variables_on_launch"):
            self.patch(template["url"], {"ask_variables_on_launch": True})
        if template.get("survey_enabled"):
            survey = self.api.get(template["url"] + "survey_spec/").get("spec", [])
            for question in survey:
                key = question["variable"]
                if key not in variables and question.get("default") not in (None, ""):
                    variables[key] = question["default"]
                if question.get("required") and variables.get(key) in (None, "", []):
                    raise ControllerError(
                        "Required survey input is missing for "
                        + name
                        + (
                            ": " + key
                            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,80}", key)
                            else ""
                        )
                    )
        launch = self.api.post(template["url"] + "launch/", {"extra_vars": variables})
        path = launch.get("url")
        if not path:
            raise ControllerError("Controller launch response has no job URL")
        self.active.add(path)
        self.checkpoint()
        print(f"running: {name} (Controller job {launch['id']})", flush=True)
        if launch.get("ignored_fields"):
            self.api.post(path + "cancel/")
            self.wait(path, 120)
            raise ControllerError("Controller rejected launch fields; job canceled")
        kind = (
            "diagnostic"
            if diagnostic
            else ("workflow_job_template" if workflow else "job_template")
        )
        try:
            result = self.wait(path, timeout)
        except ControllerError as error:
            self.case(name, "failed", kind, job_id=launch["id"], summary=str(error))
            if name in self.report["coverage"]:
                self.report["coverage"][name] = "failed"
            self.checkpoint()
            raise
        passed = self.record_job(result, name, kind, require_hosts)
        if workflow:
            passed = self.workflow_children(result) and passed
            if not passed:
                for row in self.report["cases"]:
                    if row.get("job_id") == result["id"]:
                        row["status"] = "failed"
                        row[
                            "summary"
                        ] = "Workflow execution or one of its child jobs failed; inspect the child results."
                if name in self.report["coverage"]:
                    self.report["coverage"][name] = "failed"
                self.checkpoint()
        return passed, result

    def cleanup_template(self, key):
        name, bindings = self.pending_cleanup[key]
        try:
            ok, _ = self.launch(name, dict(bindings), timeout=1800)
        except ControllerError as error:
            self.case(name, "failed", summary=str(error))
            ok = False
        self.report["cleanup"].append(
            {"resource": key, "status": "passed" if ok else "failed"}
        )
        if ok:
            del self.pending_cleanup[key]
        self.checkpoint()
        return ok

    def refresh_callback(self):
        callback = self.api.unique("credentials", "AAP Credential", self.org)
        try:
            credential_type = self.api.get(
                f"/api/controller/v2/credential_types/{callback['credential_type']}/"
            )
            inputs = callback_inputs(self.api.credential_inputs(), credential_type)
            self.api.patch(callback["url"], {"inputs": inputs})
        except ControllerError as error:
            self.case("AAP callback credential", "failed", summary=str(error))
            raise ControllerError(
                "AAP callback credential refresh failed before category setup; infrastructure was not provisioned"
            ) from None
        self.case(
            "AAP callback credential",
            "passed",
            summary="Refreshed from the protected connection; no credential values are reported.",
        )

    def setup(self):
        machine = self.api.unique("credentials", "APD Machine Credential", self.org)
        original_inputs = machine.get("inputs", {})
        prepared_inputs = fresh_machine_inputs(original_inputs)
        if prepared_inputs != original_inputs:
            self.api.patch(machine["url"], {"inputs": prepared_inputs})
        self.case(
            "APD machine credential",
            "passed",
            summary="Existing authentication preserved; missing test SSH key/password initialized without export.",
        )
        self.refresh_callback()
        self.bind()
        ok, _ = self.launch("APD | Single demo setup", {"demo": "cloud"})
        if not ok:
            raise ControllerError("Cloud category setup failed")
        ok, _ = self.launch(
            "APD | Multi-demo setup",
            {"demos": ["cloud", "infrastructure", "linux", "network", "windows"]},
            timeout=3600,
        )
        if not ok:
            raise ControllerError("Category setup failed")
        self.bind()
        aws = self.api.unique("credentials", "AWS", self.org)["id"]
        if self.fixture_guard:
            self.guard_template = self.diagnostic_template(
                "fixture guard",
                "tests/integration/fixture_guard.yml",
                credentials=[aws],
            )
        self.bucket_template = self.diagnostic_template(
            "bucket cleanup", "tests/integration/cleanup_bucket.yml", credentials=[aws]
        )
        report_template = self.template("Cloud | AWS | VPC Report")
        extra = variables(report_template.get("extra_vars"))
        extra.update(
            reports_aws_bucket_name=self.bucket, create_vm_aws_region=self.args.region
        )
        self.patch(report_template["url"], {"extra_vars": json.dumps(extra)})
        # Set source inventory region explicitly; never silently test a different account region.
        source = self.inventory_source("AWS Inventory")
        vars_ = variables(source.get("source_vars"))
        vars_["regions"] = [self.args.region]
        self.patch(source["url"], {"source_vars": json.dumps(vars_)})

    def guard(self, mode):
        ok, job = self.launch(
            "Fixture " + mode,
            {
                "ci_guard_mode": mode,
                "ci_region": self.args.region,
                "ci_owner": self.owner,
                "ci_owned_keys": self.key_ids,
            },
            diagnostic=True,
            template=self.guard_template,
        )
        artifacts = job.get("artifacts", {})
        if not artifacts.get("ci_guard_reason") and not ok:
            for event in self.api.list(
                job["url"] + "job_events/?event=runner_on_ok&page_size=200"
            ):
                artifacts.update(
                    event.get("event_data", {})
                    .get("res", {})
                    .get("ansible_stats", {})
                    .get("data", {})
                )
        if not ok:
            row = next(
                case
                for case in reversed(self.report["cases"])
                if case.get("job_id") == job["id"]
            )
            reason = artifacts.get("ci_guard_reason")
            if reason == "resources_present":
                row[
                    "summary"
                ] = "Reserved AWS fixtures already exist; provisioning was refused."
                row["failure_categories"] = ["baseline_not_empty"]
            elif reason == "aws_inspection_failed":
                module = artifacts.get("ci_guard_module")
                module = (
                    module
                    if module
                    in (
                        "amazon.aws.ec2_vpc_net_info",
                        "amazon.aws.ec2_key_info",
                        "amazon.aws.ec2_instance_info",
                    )
                    else "AWS module"
                )
                error = artifacts.get("ci_guard_error")
                error = (
                    error
                    if error
                    in (
                        "unsupported_module_arguments",
                        "aws_authentication",
                        "aws_permissions",
                        "python_dependency",
                    )
                    else "aws_api_error"
                )
                row[
                    "summary"
                ] = f"AWS fixture inspection failed in {module}: {error}. This check did not create or delete resources."
                row["failure_categories"] = [error]
            print("Fixture diagnosis: " + row["summary"], flush=True)
            self.checkpoint()
        if mode == "capture":
            self.key_ids = artifacts.get("ci_key_ids", [])
        return ok

    def sync_inventory(self, name):
        self.bind()
        source = self.inventory_source(name)
        update = self.api.post(source["url"] + "update/")
        result = self.wait(update["url"], 900)
        return self.record_job(result, name, "inventory_update")

    def workflow_prerequisites(self, job, targets):
        definition = next(
            row for row in self.graph["workflows"] if row["name"] == job["name"]
        )
        required = {
            node["identifier"]
            for node in definition["nodes"]
            if node["target"] in targets
        }
        passed = {
            row.get("node_identifier")
            for row in self.report["cases"]
            if row.get("workflow_id") == job["id"]
            and row.get("job_id")
            and row["status"] == "passed"
        }
        return bool(required) and required <= passed

    def cloud(self, deploy_only=False):
        self.cloud_started = True
        self.checkpoint()
        ok, job = self.launch(
            "Deploy Cloud Stack in AWS",
            {
                "create_vm_aws_region": self.args.region,
                "create_vm_aws_owner_tag": self.owner,
                "vm_environment": "Dev",
                "email": "apd-ci@example.invalid",
            },
            workflow=True,
            timeout=5400,
        )
        provisioned = ok or self.workflow_prerequisites(
            job,
            {
                "Cloud | AWS | Create VPC",
                "Cloud | AWS | Create Keypair",
                "Cloud | AWS | Create VM",
            },
        )
        if not provisioned or not self.sync_inventory("AWS Inventory"):
            return False
        inventory = self.api.unique(
            "inventories", "Ansible Product Demos Inventory", self.org
        )
        hosts = {
            row["name"]
            for row in self.api.list(inventory["url"] + "hosts/?page_size=200")
        }
        expected = {"aws-rhel9", "aws-rhel10", "aws-dc", "aws_win1", "reports"}
        if not expected <= hosts:
            self.case(
                "Cloud inventory readiness",
                "failed",
                summary="Not all five expected stack hosts were discovered.",
            )
            return False
        self.case(
            "Cloud inventory readiness",
            "passed",
            hosts=5,
            summary="All five expected hosts were discovered; connection tests follow.",
        )
        if deploy_only:
            return True
        self.launch(
            "LINUX | Fact Scan",
            {"_hosts": "aws-rhel9:aws-rhel10:reports"},
            require_hosts=True,
            timeout=1800,
        )
        self.launch(
            "WINDOWS | Test Connectivity",
            {"_hosts": "aws-dc:aws_win1"},
            require_hosts=True,
            timeout=1800,
        )

    def network(self, deploy_only=False):
        self.network_started = True
        self.checkpoint()
        ok, job = self.launch(
            "NETWORK | Deploy Containerlab Stack",
            {
                "clab_aws_region": self.args.region,
                "clab_aws_owner_tag": self.owner,
                "clab_aws_instance_type": "c8i.4xlarge",
                "clab_access_cidr": self.args.access_cidr,
            },
            workflow=True,
            timeout=7200,
        )
        provisioned = ok or self.workflow_prerequisites(
            job,
            {
                "NETWORK | Containerlab | Provision Hypervisor",
                "NETWORK | Containerlab | Deploy Topology",
            },
        )
        if not provisioned:
            return False
        if not self.sync_inventory("ContainerLab Hypervisor"):
            return False
        if deploy_only:
            return True
        for name in SUPPORTED["network"]:
            ok, _ = self.launch(name, require_hosts=True, timeout=1800)
            if not ok:
                break

    def finalizers(self):
        self.phase = "cleanup"
        # Cleanup has a separate time budget after the test budget expires.
        self.deadline = self.clock() + 3600
        self.report["metadata"]["state"] = "finalizing"
        quiescent = self.settle_jobs()
        if (
            (self.baseline_passed or not self.fixture_guard)
            and (self.cloud_started or self.network_started or self.pending_cleanup)
            and quiescent
        ):
            try:
                if self.fixture_guard and not self.guard("capture"):
                    raise ControllerError(
                        "Could not record the newly created keypair identifiers"
                    )
                if self.fixture_guard and not self.guard("ownership"):
                    raise ControllerError("Cleanup ownership validation failed")
                for name in list(reversed(self.pending_cleanup)):
                    self.cleanup_template(name)
                for started, name in (
                    (self.network_started, "NETWORK | Destroy Containerlab Stack"),
                    (self.cloud_started, "Destroy Cloud Stack in AWS"),
                ):
                    if started:
                        variables = (
                            {"clab_aws_region": self.args.region}
                            if name.startswith("NETWORK")
                            else {"create_vm_aws_region": self.args.region}
                        )
                        try:
                            ok, _ = self.launch(
                                name, variables, workflow=True, timeout=1800
                            )
                        except ControllerError as error:
                            self.case(name, "failed", summary=str(error))
                            ok = False
                        self.report["cleanup"].append(
                            {"resource": name, "status": "passed" if ok else "failed"}
                        )
                if self.cloud_started:
                    ok, _ = self.launch(
                        "Run report bucket cleanup",
                        {"ci_bucket": self.bucket, "ci_region": self.args.region},
                        template=self.bucket_template,
                        diagnostic=True,
                    )
                    self.report["cleanup"].append(
                        {
                            "resource": "unique report bucket",
                            "status": "passed" if ok else "failed",
                        }
                    )
                if self.fixture_guard:
                    clean = self.guard("baseline")
                    self.report["cleanup"].append(
                        {
                            "resource": "reserved AWS fixture resources absent",
                            "status": "passed" if clean else "failed",
                        }
                    )
            except ControllerError as error:
                self.report["cleanup"].append(
                    {
                        "resource": "AWS fixtures",
                        "status": "failed",
                        "summary": str(error),
                    }
                )
        elif self.cloud_started or self.network_started or self.pending_cleanup:
            self.report["cleanup"].append(
                {
                    "resource": "AWS fixtures",
                    "status": "failed",
                    "summary": "Active jobs could not be stopped; automatic deletion refused.",
                }
            )
        quiescent = self.settle_jobs() and quiescent
        if not quiescent:
            self.report["cleanup"].append(
                {
                    "resource": "Controller job completion",
                    "status": "failed",
                    "summary": "Owned jobs or workflow children remain active or unverified; Controller bindings and temporary resources are retained.",
                }
            )
        restored = quiescent
        if quiescent:
            for path, snapshot in reversed(self.snapshots):
                try:
                    self.api.patch(path, snapshot)
                except ControllerError:
                    restored = False
        # Restore the cached checkout as well as its source configuration.
        if self.snapshots and quiescent:
            try:
                update = self.api.post(self.project["url"] + "update/")
                restored = (
                    self.wait(update["url"], 900)["status"] == "successful" and restored
                )
            except ControllerError:
                restored = False
        self.report["cleanup"].append(
            {
                "resource": "original Controller bindings and project checkout",
                "status": "passed" if restored else "failed",
            }
        )
        quiescent = self.settle_jobs() and quiescent
        for resource in reversed(self.created) if quiescent else []:
            if (
                resource["kind"] == "inventories"
                and resource["name"] == "APD integration lease"
            ):
                continue
            if resource["kind"] == "credentials" and not restored:
                self.report["cleanup"].append(
                    {
                        "resource": "credentials",
                        "status": "failed",
                        "summary": "Registry credential retained because Controller bindings could not be restored.",
                    }
                )
                continue
            try:
                self.api.delete(
                    f"/api/controller/v2/{resource['kind']}/{resource['id']}/"
                )
            except ControllerError as error:
                self.report["cleanup"].append(
                    {
                        "resource": resource["kind"],
                        "id": resource["id"],
                        "name": resource["name"],
                        "status": "failed",
                        "summary": str(error),
                    }
                )
        failed_cleanup = not quiescent or any(
            row["status"] == "failed" for row in self.report["cleanup"]
        )
        if self.lease and not failed_cleanup:
            try:
                self.api.delete(self.lease["url"])
            except ControllerError:
                self.report["cleanup"].append(
                    {"resource": "integration lease", "status": "failed"}
                )
        elif self.lease:
            self.report["cleanup"].append(
                {
                    "resource": "integration lease",
                    "status": "retained",
                    "summary": "Reconcile cleanup before another run; see ledger.json.",
                }
            )
        self.checkpoint()

    def run(self):
        try:
            self.configure()
            self.phase = "runtime"
            ok, _ = self.launch(
                "Candidate runtime validation",
                diagnostic=True,
                template=self.probe_template,
            )
            if not ok:
                raise ControllerError(
                    "Candidate runtime validation failed; infrastructure was not provisioned"
                )
            self.phase = "setup"
            self.setup()
            self.phase = "runtime"
            if self.expected_runtime:
                ok, _ = self.launch(
                    "Candidate runtime validation after setup",
                    diagnostic=True,
                    template=self.probe_template,
                )
                if not ok:
                    raise ControllerError(
                        "Category setup changed the candidate runtime; infrastructure was not provisioned"
                    )
            self.phase = "baseline"
            if self.fixture_guard and not self.guard("baseline"):
                raise ControllerError(
                    "AWS fixture baseline could not be validated; provisioning refused. Inspect the Fixture baseline case for the reason."
                )
            self.baseline_passed = self.fixture_guard
            if not self.fixture_guard:
                self.case(
                    "Fixture guard disabled",
                    "disabled",
                    summary="AWS baseline and ownership guard jobs are disabled for this disposable preview. Demo provisioning and teardown jobs run directly; fixture absence and ownership are not independently verified.",
                )
            self.checkpoint()
            if self.args.profile == "catalog":
                Catalog(self).run()
            if self.args.profile in ("baseline-smoke", "cloud-smoke"):
                self.phase = "cloud"
                try:
                    self.cloud()
                except ControllerError as error:
                    self.case("Cloud smoke lane", "failed", summary=str(error))
            if self.args.profile in ("baseline-smoke", "network-smoke"):
                self.phase = "network"
                try:
                    self.network()
                except ControllerError as error:
                    self.case("Network smoke lane", "failed", summary=str(error))
        except (ControllerError, KeyboardInterrupt) as error:
            self.case(
                "Run preflight / execution",
                "failed",
                summary=str(error) or "Run interrupted; cleanup requested.",
            )
        except Exception:
            self.case(
                "Run preflight / execution",
                "failed",
                summary="Unexpected runner error; raw response data omitted. Inspect the Controller run and test the adapter.",
            )
        finally:
            if self.lease:
                try:
                    self.finalizers()
                except Exception:
                    self.report["cleanup"].append(
                        {
                            "resource": "finalizer",
                            "status": "failed",
                            "summary": "Cleanup incomplete; reconcile resources using ledger.json and the retained integration lease.",
                        }
                    )
                    self.checkpoint()
            else:
                self.checkpoint()
            if self.args.profile == "catalog":
                Catalog(self).complete_coverage()
            if self.active or self.observer.unresolved:
                self.report["metadata"].pop("finished", None)
                self.report["metadata"].update(state="incomplete", runner_stopped=now())
                if not any(
                    row.get("resource") == "Controller job completion"
                    for row in self.report["cleanup"]
                ):
                    self.report["cleanup"].append(
                        {
                            "resource": "Controller job completion",
                            "status": "failed",
                            "summary": "The report is incomplete because owned jobs remain active or unverified.",
                        }
                    )
            else:
                self.report["metadata"].update(state="finished", finished=now())
            self.checkpoint()
        return (
            1
            if any(
                row["status"] in ("failed", "canceled", "timed_out", "blocked")
                for row in self.report["cases"] + self.report["cleanup"]
            )
            else 0
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--image")
    parser.add_argument(
        "--profile",
        choices=("catalog", "baseline-smoke", "cloud-smoke", "network-smoke"),
        default="catalog",
    )
    parser.add_argument(
        "--region", choices=("us-east-2", "us-west-2"), default="us-east-2"
    )
    parser.add_argument("--access-cidr", default="0.0.0.0/0")
    parser.add_argument("--run-id", default="local")
    parser.add_argument(
        "--fixture-guard",
        action="store_true",
        help="Opt in to AWS baseline and cleanup ownership diagnostics (disabled for this preview)",
    )
    parser.add_argument("--build-metadata", type=Path)
    parser.add_argument("--build-report", type=Path)
    parser.add_argument("--require-built-image", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("integration-results/live"))
    parser.add_argument("--timeout", type=int, default=14400)
    parser.add_argument("--poll-interval", type=float, default=10)
    args = parser.parse_args()
    if args.require_built_image and not args.image:
        try:
            if not args.build_metadata or args.build_metadata.stat().st_size > 16384:
                raise ValueError()
            args.image = json.loads(args.build_metadata.read_text())["image"]
        except (OSError, ValueError, KeyError, TypeError):
            parser.error("The upstream build metadata must supply the CI image digest")
    if not isinstance(args.image, str):
        parser.error(
            "Supply --image for a local run, or --require-built-image with CI build evidence"
        )
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision):
        parser.error("revision must be an immutable 40-character commit SHA")
    if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", args.image):
        parser.error("image must be an immutable registry image digest")
    if not re.fullmatch(
        r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?",
        args.repository,
    ):
        parser.error("repository must be an HTTPS GitHub repository URL")
    try:
        ipaddress.ip_network(args.access_cidr)
    except ValueError:
        parser.error("access-cidr must be a valid network CIDR")
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        base = os.environ["APD_URL"]
        token = os.environ.get("APD_TOKEN")
        if not token and (
            not os.environ.get("APD_USERNAME") or not os.environ.get("APD_PASSWORD")
        ):
            raise KeyError("APD_USERNAME / APD_PASSWORD")
        client = Controller(
            base, os.environ.get("APD_USERNAME"), os.environ.get("APD_PASSWORD"), token
        )
        return Runner(client, args).run()
    except (KeyError, ControllerError, BuildError, ValueError, OSError) as error:
        message = (
            "Required APD environment secrets are missing."
            if isinstance(error, KeyError)
            else str(error)
            if isinstance(error, (ControllerError, BuildError))
            else "EE build metadata could not be validated."
        )
        render(
            {
                "schema_version": 1,
                "scope": "Preflight failed before execution.",
                "metadata": {
                    "candidate_commit": args.revision,
                    "candidate_image": args.image,
                },
                "cases": [
                    {
                        "name": "APD connection preflight",
                        "status": "failed",
                        "summary": message,
                    }
                ],
            },
            args.output,
        )
        print(message)
        return 1


if __name__ == "__main__":
    sys.exit(main())
