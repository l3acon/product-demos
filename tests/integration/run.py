"""Configure a disposable APD instance and execute an approved baseline smoke run."""

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
from controller import Controller, ControllerError
from report import render
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
        self.root = Path(__file__).resolve().parents[2]
        self.graph = build(self.root, load(Path(__file__).with_name("scenarios.yml")))
        self.uid = uuid.uuid4().hex[:24]
        self.owner = "apd-ci-" + self.uid
        self.deadline = clock() + args.timeout
        self.created = []
        self.snapshots = []
        self.active = set()
        self.recorded_jobs = set()
        self.cloud_started = False
        self.network_started = False
        self.baseline_passed = False
        self.key_ids = []
        self.lease = None
        self.probe_template = None
        self.guard_template = None
        self.bucket_template = None
        self.bucket = "apd-ci-" + self.uid
        self.report = {
            "schema_version": 1,
            "sample": False,
            "scope": "Baseline smoke profile: runtime validation, category setup, fresh AWS cloud stack and/or fresh Containerlab, representative APD jobs, and cleanup. Other catalog cases remain not_selected; this is not full-catalog acceptance.",
            "metadata": {
                "run_id": args.run_id,
                "resource_owner": self.owner,
                "profile": args.profile,
                "candidate_commit": args.revision,
                "candidate_image": args.image,
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
        self.checkpoint()

    def checkpoint(self):
        render(self.report, self.args.output)
        ledger = {
            "run_id": self.args.run_id,
            "owner": self.owner,
            "cloud_started": self.cloud_started,
            "network_started": self.network_started,
            "baseline_passed": self.baseline_passed,
            "created": self.created,
            "active": sorted(self.active),
            "snapshot_fields": [
                {"path": path, "fields": sorted(data)} for path, data in self.snapshots
            ],
        }
        (self.args.output / "ledger.json").write_text(
            json.dumps(ledger, indent=2) + "\n"
        )

    def case(self, name, status, kind="assertion", **values):
        row = {"name": name, "status": status, "kind": kind, **values}
        self.report["cases"].append(row)
        print(f"{status}: {name}", flush=True)
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
        while self.clock() < end:
            row = self.api.get(path)
            if row.get("status") in TERMINAL:
                self.active.discard(path)
                self.checkpoint()
                return row
            if self.clock() >= heartbeat:
                print(
                    f"waiting: Controller job {row['id']} is {row.get('status', 'pending')}",
                    flush=True,
                )
                heartbeat = self.clock() + 60
            self.sleep(self.args.poll_interval)
        try:
            self.api.post(path + "cancel/")
            for _ in range(30):
                row = self.api.get(path)
                if row.get("status") in TERMINAL:
                    self.active.discard(path)
                    break
                self.sleep(2)
        except ControllerError:
            pass
        raise ControllerError("Job timeout; cancellation requested")

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
            raise ControllerError(
                "Another integration lease exists; reconcile that run before retrying"
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
        self.patch(self.ee["url"], {"image": self.args.image, "pull": "always"})
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
                spec
                and spec.get("test_ee_binding") == "candidate"
                and row.get("execution_environment") != self.ee["id"]
            ):
                self.patch(row["url"], {"execution_environment": self.ee["id"]})
        for row in self.api.collection("inventory_sources"):
            if (
                row["name"]
                in ("AWS Inventory", "ContainerLab Hypervisor", "Insights Inventory")
                and row.get("execution_environment") != self.ee["id"]
            ):
                self.patch(row["url"], {"execution_environment": self.ee["id"]})
        self.api.patch(self.ee["url"], {"image": self.args.image, "pull": "always"})

    def record_job(self, job, name=None, kind="job_template", require_hosts=False):
        if job["url"] in self.recorded_jobs:
            return job["status"] == "successful"
        self.recorded_jobs.add(job["url"])
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
            if categories and job["status"] != "successful":
                summary = (
                    "Failure categories: "
                    + ", ".join(sorted(categories))
                    + ". Inspect the Controller job for full output."
                )
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
        )
        if name in self.report["coverage"]:
            prior = self.report["coverage"][name]
            self.report["coverage"][name] = (
                "failed" if prior == "failed" or row["status"] != "passed" else "passed"
            )
        self.checkpoint()
        return status == "passed"

    def workflow_children(self, job):
        passed = True
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
                self.case(
                    target or identifier,
                    "not_reached",
                    "workflow_node",
                    summary="Conditional branch was not executed.",
                )
                continue
            child = self.api.get(f"/api/controller/v2/unified_jobs/{node['job']}/")
            # Unified-job detail may omit job-specific provenance; follow its typed URL.
            collections = {
                "job": "jobs",
                "inventory_update": "inventory_updates",
                "project_update": "project_updates",
                "workflow_job": "workflow_jobs",
                "workflow_approval": "workflow_approvals",
            }
            child = self.api.get(
                f"/api/controller/v2/{collections[child['type']]}/{child['id']}/"
            )
            kind = (
                "job_template"
                if child.get("type") == "job"
                else child.get("type", "supporting_update")
            )
            passed = self.record_job(child, target, kind) and passed
            if child.get("type") == "workflow_job":
                passed = self.workflow_children(child) and passed
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
        if template.get("survey_enabled"):
            survey = self.api.get(template["url"] + "survey_spec/").get("spec", [])
            for question in survey:
                key = question["variable"]
                if key not in variables and question.get("default") not in (None, ""):
                    variables[key] = question["default"]
                if question.get("required") and variables.get(key) in (None, "", []):
                    raise ControllerError(
                        "Required survey input is missing for " + name
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
        return passed, result

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
        if hasattr(self.api, "credential_inputs"):
            callback = self.api.unique("credentials", "AAP Credential", self.org)
            inputs = self.api.credential_inputs()
            if self.series == "27":
                inputs["request_timeout"] = 10
            self.api.patch(callback["url"], {"inputs": inputs})
            self.case(
                "AAP callback credential",
                "passed",
                summary="Refreshed from the protected connection; no credential values are reported.",
            )
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
        self.guard_template = self.diagnostic_template(
            "fixture guard", "tests/integration/fixture_guard.yml", credentials=[aws]
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
        if mode == "capture":
            self.key_ids = artifacts.get("ci_key_ids", [])
        return ok

    def sync_inventory(self, name):
        self.bind()
        source = self.inventory_source(name)
        update = self.api.post(source["url"] + "update/")
        result = self.wait(update["url"], 900)
        return self.record_job(result, name, "inventory_update")

    def cloud(self):
        self.cloud_started = True
        self.checkpoint()
        ok, _ = self.launch(
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
        if not ok or not self.sync_inventory("AWS Inventory"):
            return
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
            return
        self.case(
            "Cloud inventory readiness",
            "passed",
            hosts=5,
            summary="All five expected hosts were discovered; connection tests follow.",
        )
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

    def network(self):
        self.network_started = True
        self.checkpoint()
        ok, _ = self.launch(
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
        if not ok:
            return
        if not self.sync_inventory("ContainerLab Hypervisor"):
            return
        for name in SUPPORTED["network"]:
            ok, _ = self.launch(name, require_hosts=True, timeout=1800)
            if not ok:
                break

    def finalizers(self):
        # Cleanup has a separate time budget after the test budget expires.
        self.deadline = self.clock() + 3600
        quiescent = True
        for path in list(self.active):
            try:
                if self.api.get(path).get("status") in TERMINAL:
                    self.active.discard(path)
                    continue
                self.api.post(path + "cancel/")
                self.wait(path, 180)
            except ControllerError:
                quiescent = False
        if (
            self.baseline_passed
            and (self.cloud_started or self.network_started)
            and quiescent
        ):
            try:
                if not self.guard("capture"):
                    raise ControllerError(
                        "Could not record the newly created keypair identifiers"
                    )
                if not self.guard("ownership"):
                    raise ControllerError("Cleanup ownership validation failed")
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
                        ok, _ = self.launch(
                            name, variables, workflow=True, timeout=1800
                        )
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
        elif self.cloud_started or self.network_started:
            self.report["cleanup"].append(
                {
                    "resource": "AWS fixtures",
                    "status": "failed",
                    "summary": "Active jobs could not be stopped; automatic deletion refused.",
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
        for resource in reversed(self.created) if quiescent else []:
            if (
                resource["kind"] == "inventories"
                and resource["name"] == "APD integration lease"
            ):
                continue
            try:
                self.api.delete(
                    f"/api/controller/v2/{resource['kind']}/{resource['id']}/"
                )
            except ControllerError:
                self.report["cleanup"].append(
                    {"resource": resource["kind"], "status": "failed"}
                )
        failed_cleanup = any(
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
        self.report["metadata"]["finished"] = now()
        self.checkpoint()

    def run(self):
        try:
            self.configure()
            ok, _ = self.launch(
                "Candidate runtime validation",
                diagnostic=True,
                template=self.probe_template,
            )
            if not ok:
                raise ControllerError(
                    "Candidate runtime validation failed; infrastructure was not provisioned"
                )
            self.setup()
            if not self.guard("baseline"):
                raise ControllerError(
                    "Existing reserved resources detected; provisioning refused"
                )
            self.baseline_passed = True
            self.checkpoint()
            if self.args.profile in ("baseline-smoke", "cloud-smoke"):
                try:
                    self.cloud()
                except ControllerError as error:
                    self.case("Cloud smoke lane", "failed", summary=str(error))
            if self.args.profile in ("baseline-smoke", "network-smoke"):
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
        return (
            1
            if any(
                row["status"] in ("failed", "canceled", "timed_out")
                for row in self.report["cases"] + self.report["cleanup"]
            )
            else 0
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument(
        "--profile",
        choices=("baseline-smoke", "cloud-smoke", "network-smoke"),
        default="baseline-smoke",
    )
    parser.add_argument(
        "--region", choices=("us-east-2", "us-west-2"), default="us-east-2"
    )
    parser.add_argument("--access-cidr", default="0.0.0.0/0")
    parser.add_argument("--run-id", default="local")
    parser.add_argument("--output", type=Path, default=Path("integration-results/live"))
    parser.add_argument("--timeout", type=int, default=14400)
    parser.add_argument("--poll-interval", type=float, default=10)
    args = parser.parse_args()
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
    except (KeyError, ControllerError) as error:
        message = (
            "Required APD environment secrets are missing."
            if isinstance(error, KeyError)
            else str(error)
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
