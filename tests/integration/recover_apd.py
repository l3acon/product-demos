"""Plan/apply manual recovery of one APD CI run; credentials stay in env/secrets."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from build_ee import BuildError, validate_metadata
from controller import Controller, ControllerError
from recovery import previous_evidence
from report import render
from workflow_observer import WorkflowObserver, ready


ACTIVE = {"new", "pending", "waiting", "running"}
KINDS = {
    "job_template": "jobs",
    "diagnostic": "jobs",
    "workflow_job_template": "workflow_jobs",
    "workflow_job": "workflow_jobs",
    "inventory_update": "inventory_updates",
    "project_update": "project_updates",
}
RECOVERY_MARKER = re.compile(
    r"^\[apd-recovery (plan|apply) ([0-9]+-[0-9]+)(?: region=(us-east-2|us-west-2))?(?: keys=(key-[a-f0-9]+(?:,key-[a-f0-9]+)*))?\](?: .*)?$"
)


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def variables(value):
    value = json.loads(value) if isinstance(value, str) else value
    if not isinstance(value, dict):
        raise ControllerError("Recovery inventory variables are not a mapping")
    return value


def request_from_message(message):
    match = RECOVERY_MARKER.fullmatch(
        message.splitlines()[0] if message.splitlines() else ""
    )
    if not match or match[1] == "plan" and (not match[3] or match[4]):
        raise ControllerError(
            "Use a recovery plan marker with a region, or an apply marker with a reviewed plan run"
        )
    return {
        "mode": match[1],
        "source": match[2],
        "region": match[3] or "us-east-2",
        "keys": match[4] or "",
    }


def gh_json(arguments):
    result = subprocess.run(
        ["gh", *arguments], capture_output=True, text=True, timeout=120
    )
    if result.returncode:
        raise ControllerError(
            "GitHub recovery evidence could not be read; raw response omitted"
        )
    return json.loads(result.stdout)


def load_plan(repository, run_id, output):
    if not re.fullmatch(r"[0-9]+-[0-9]+", run_id):
        raise ControllerError("Plan run must be a GitHub run-attempt identifier")
    number, attempt = run_id.split("-")
    run = gh_json(["api", f"repos/{repository}/actions/runs/{number}"])
    if (
        run.get("path", "").split("@")[0] != ".github/workflows/apd-recovery.yml"
        or run.get("status") != "completed"
        or run.get("head_repository", {}).get("full_name", "").lower()
        != repository.lower()
    ):
        raise ControllerError(
            "Only a completed recovery-plan workflow in this repository can authorize apply"
        )
    path = output / "reviewed-plan"
    path.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "gh",
            "run",
            "download",
            number,
            "--repo",
            repository,
            "--name",
            "apd-recovery-plan-" + run_id,
            "--dir",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode:
        raise ControllerError("Reviewed recovery plan artifact is unavailable")
    target = path / "recovery-plan.json"
    if target.stat().st_size > 1024 * 1024:
        raise ControllerError("Recovery plan artifact is too large")
    plan = json.loads(target.read_text())
    if (
        plan.get("schema_version") != 1
        or plan.get("recovery_run") != run_id
        or plan.get("recovery_revision") != run.get("head_sha")
        or plan.get("repository") != repository
    ):
        raise ControllerError("Recovery plan does not match its workflow provenance")
    observed = dict(plan)
    observed.pop("fingerprint", None)
    if fingerprint(observed) != plan.get("fingerprint"):
        raise ControllerError("Recovery plan fingerprint changed")
    return plan


class Recovery:
    def __init__(
        self, api, repository, revision, output, recovery_run="local", sleep=time.sleep
    ):
        self.api, self.repository, self.revision = api, repository, revision
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.recovery_run, self.sleep = recovery_run, sleep
        self.uid = "apd-recovery-" + uuid.uuid4().hex[:24]
        self.created, self.active = [], set()
        self.utility_paths = set()
        self.operation = "plan"
        self.observer = WorkflowObserver(self)
        self.report = {
            "schema_version": 1,
            "sample": False,
            "scope": "Manual recovery of one failed CI run. Plan inspects; apply deletes only approved run-owned resources and releases its lock last.",
            "metadata": {
                "profile": "manual-recovery",
                "run_id": recovery_run,
                "candidate_commit": revision,
                "repository": "https://github.com/" + repository,
                "controller_url": api.base,
                "state": "running",
                "started": datetime.now(timezone.utc).isoformat(),
            },
            "cases": [],
            "cleanup": [],
        }
        self.org = api.unique("organizations", "Ansible Product Demos (APD)")["id"]
        self.project = api.unique("projects", "Ansible Product Demos", self.org)
        self.lock = None
        self.checkpoint()

    def checkpoint(self):
        pending = self.observer.pending()
        self.report["metadata"]["operation"] = self.operation
        self.report["pending_jobs"] = [
            row
            for row in pending
            if self.operation == "apply"
            or row.get("job_id")
            in {int(path.rstrip("/").split("/")[-1]) for path in self.utility_paths}
        ]
        if self.lock:
            self.api.patch(
                self.lock["url"],
                {
                    "variables": json.dumps(
                        {
                            "source_run": self.report["metadata"].get("source_run"),
                            "recovery_run": self.recovery_run,
                            "prefix": self.uid,
                            "created": self.created,
                            "utility_paths": sorted(self.utility_paths),
                        }
                    )
                },
            )
        render(self.report, self.output)
        (self.output / "recovery-receipt.json").write_text(
            json.dumps(
                {
                    "operation": self.operation,
                    "created": self.created,
                    "active": sorted(self.active),
                    "source_run": self.report["metadata"].get("source_run"),
                    "recovery_run": self.recovery_run,
                    "recovery_lock": self.lock,
                },
                indent=2,
            )
            + "\n"
        )

    def case(self, name, status, **fields):
        self.report["cases"].append({"name": name, "status": status, **fields})
        self.checkpoint()

    def wait(self, path, seconds=1800, utility=False):
        if utility:
            self.utility_paths.add(path)
        end = time.monotonic() + seconds
        heartbeat = time.monotonic() + 60
        print("Waiting for Controller recovery execution " + path, flush=True)
        self.active.add(path)
        self.checkpoint()
        while time.monotonic() < end:
            job = self.api.get(path)
            self.observer.remember(job)
            settled = (
                self.observer.inspect(job)
                if job.get("type") == "workflow_job"
                else ready(job)
            )
            if settled:
                self.active.discard(path)
                self.checkpoint()
                return job
            if time.monotonic() >= heartbeat:
                print(
                    f"Controller job {job['id']} is {job.get('status', 'pending')}; waiting for child jobs and task events",
                    flush=True,
                )
                heartbeat = time.monotonic() + 60
            self.sleep(5)
        raise ControllerError(
            "Recovery job did not settle; locks and recovery resources were retained"
        )

    def source(self, run_id):
        ledger, report = previous_evidence(
            "https://github.com/" + self.repository, run_id
        )
        meta = report["metadata"]
        if meta.get("controller_url", "").rstrip("/") != self.api.base:
            raise ControllerError("Source run belongs to a different APD instance")
        owner = ledger.get("owner")
        if (
            not re.fullmatch(r"apd-ci-[a-f0-9]{24}", str(owner))
            or meta.get("resource_owner") != owner
            or ledger.get("run_id") != run_id
            or meta.get("run_id") != run_id
        ):
            raise ControllerError("Source run and resource owner do not match")
        lease = self.api.unique("inventories", "APD integration lease", self.org)
        state = variables(lease.get("variables"))
        if state.get("owner") != owner or state.get("run_id") != run_id:
            raise ControllerError("The APD lock belongs to a different CI run")
        recorded = [
            row
            for row in ledger.get("created", [])
            if row.get("kind") == "inventories"
            and row.get("id") == lease["id"]
            and row.get("name") == "APD integration lease"
        ]
        if len(recorded) != 1:
            raise ControllerError(
                "Source ledger does not identify the observed APD lock"
            )
        self.report["metadata"].update(source_run=run_id, resource_owner=owner)
        return ledger, report, lease

    def objects(self, ledger, owner, lease):
        result = []
        for item in ledger.get("created", []):
            if item.get("id") == lease["id"] and item.get("kind") == "inventories":
                continue
            kind, identifier = item.get("kind"), item.get("id")
            if (
                kind not in ("inventories", "job_templates", "credentials")
                or type(identifier) is not int
                or not item.get("name", "").startswith(owner + " / ")
            ):
                raise ControllerError(
                    "Ledger contains an object outside the failed run's temporary namespace"
                )
            path = f"/api/controller/v2/{kind}/{identifier}/"
            live = self.api.get(path, missing_ok=True)
            if live is None:
                continue
            organization = live.get("organization") or live.get(
                "summary_fields", {}
            ).get("organization", {}).get("id")
            if live.get("name") != item["name"] or organization != self.org:
                raise ControllerError("Temporary Controller object ownership changed")
            if kind == "credentials" and any(
                ee.get("credential") == identifier
                for ee in self.api.collection("execution_environments")
            ):
                raise ControllerError(
                    "A temporary credential is still bound to an EE; reconcile that binding before deleting it"
                )
            result.append(
                {"kind": kind, "id": identifier, "name": item["name"], "path": path}
            )
        return result

    def jobs(self, ledger, report):
        paths = {
            f"/api/controller/v2/{KINDS[row['kind']]}/{row['job_id']}/"
            for row in report.get("cases", [])
            if type(row.get("job_id")) is int and row.get("kind") in KINDS
        }
        for path in ledger.get("active", []):
            if not re.fullmatch(
                r"/api/controller/v2/(jobs|workflow_jobs|inventory_updates|project_updates)/[0-9]+/",
                path,
            ):
                raise ControllerError("Source ledger contains an unexpected job URL")
            paths.add(path)
        for path in sorted(paths):
            job = self.api.get(path)
            self.observer.remember(job)
            if job.get("type") == "workflow_job":
                settled = self.observer.inspect(job)
                for _ in range(20):
                    if (
                        settled
                        or not ready(job)
                        or any(not ready(item) for item in self.observer.jobs.values())
                        or any(
                            node.get("job") is None
                            and node.get("do_not_run") is False
                            and node.get("unified_job_template", 1) is not None
                            for nodes in self.observer.nodes.values()
                            for node in nodes
                        )
                    ):
                        break
                    settled = self.observer.inspect(self.api.get(path))
            elif not ready(job):
                self.active.add(path)
        # Review all active jobs in this dedicated APD organization, including
        # manually started APD jobs that could still be using the same fixtures.
        for row in self.api.list(
            "/api/controller/v2/unified_jobs/?status__in=new,pending,waiting,running&page_size=200"
        ):
            summary = row.get("summary_fields", {})
            if (
                row.get("organization") == self.org
                or row.get("project") == self.project["id"]
                or summary.get("organization", {}).get("id") == self.org
                or summary.get("project", {}).get("id") == self.project["id"]
            ):
                node = {"job": row["id"], "related": {"job": row["url"]}}
                path = self.observer.child_path(node)
                if path not in self.active:
                    self.observer.remember(self.api.get(path))
                    self.active.add(path)
        result = []
        for path in sorted(self.active):
            job = self.observer.jobs[path]
            result.append(
                {
                    "path": path,
                    "id": job["id"],
                    "kind": job.get("type"),
                    "status": job.get("status"),
                }
            )
        self.checkpoint()
        return result

    def create(self, kind, data):
        row = self.api.post(f"/api/controller/v2/{kind}/", data)
        self.created.append({"kind": kind, "id": row["id"], "name": data["name"]})
        self.checkpoint()
        return row

    def hosts(self, owner, aws):
        import yaml

        instance_ids = {item["id"] for item in aws["instances"]}
        inventory_ids = {
            item["id"]
            for item in self.api.collection("inventories")
            if item.get("organization") == self.org
            and item["name"]
            in ("Ansible Product Demos Inventory", "ContainerLab Inventory")
        }
        result = []
        for inventory in inventory_ids:
            for host in self.api.list(
                f"/api/controller/v2/inventories/{inventory}/hosts/?page_size=200"
            ):
                data = host.get("variables", {})
                data = yaml.safe_load(data) if isinstance(data, str) else data
                data = data if isinstance(data, dict) else {}
                if (
                    data.get("instance_id") in instance_ids
                    or data.get("tags", {}).get("owner") == owner
                ):
                    result.append(
                        {"id": host["id"], "name": host["name"], "inventory": inventory}
                    )
        return sorted(result, key=lambda row: row["id"])

    def utility(self, source):
        image = source.get("ee_build", {}).get("image")
        metadata = source.get("ee_build", {})
        if metadata.get("mode") != "built" or not str(image).startswith(
            "ghcr.io/" + self.repository.lower() + "/apd-ee-27@sha256:"
        ):
            raise ControllerError(
                "Recovery requires the source run's CI-built immutable GHCR EE"
            )
        evidence = self.output / "utility-build-metadata.json"
        evidence.write_text(json.dumps(metadata))
        validate_metadata(evidence, source["metadata"]["candidate_commit"], image)
        self.report["metadata"]["candidate_image"] = image
        credential = None
        if os.environ.get("APD_REGISTRY_PASSWORD"):
            kind = self.api.unique("credential_types", "Container Registry")
            credential = self.create(
                "credentials",
                {
                    "name": self.uid + " / registry",
                    "organization": self.org,
                    "credential_type": kind["id"],
                    "inputs": {
                        "host": "ghcr.io",
                        "username": os.environ["APD_REGISTRY_USERNAME"],
                        "password": os.environ["APD_REGISTRY_PASSWORD"],
                        "verify_ssl": True,
                    },
                },
            )["id"]
        ee = self.create(
            "execution_environments",
            {
                "name": self.uid + " / EE",
                "organization": self.org,
                "image": image,
                "pull": "always",
                **({"credential": credential} if credential else {}),
            },
        )
        project = self.create(
            "projects",
            {
                "name": self.uid + " / project",
                "organization": self.org,
                "scm_type": "git",
                "scm_url": "https://github.com/" + self.repository,
                "scm_branch": self.revision,
                "default_environment": ee["id"],
            },
        )
        self.sync_project(project)
        inventory = self.create(
            "inventories", {"name": self.uid + " / localhost", "organization": self.org}
        )
        self.api.post(
            inventory["url"] + "hosts/",
            {
                "name": "localhost",
                "variables": json.dumps({"ansible_connection": "local"}),
            },
        )
        self.template = self.create(
            "job_templates",
            {
                "name": self.uid + " / AWS recovery",
                "project": project["id"],
                "inventory": inventory["id"],
                "execution_environment": ee["id"],
                "playbook": "tests/integration/recovery_aws.yml",
                "ask_variables_on_launch": True,
            },
        )
        aws = self.api.unique("credentials", "AWS", self.org)
        self.api.post(self.template["url"] + "credentials/", {"id": aws["id"]})

    def sync_project(self, project):
        # Controller automatically starts an initial Git sync on project create.
        # Follow it instead of racing it with a second POST update request.
        observed = self.api.get(project["url"])
        current = self.project_job(observed)
        if current:
            sync = self.wait(current, utility=True)
        elif observed.get("scm_revision") == self.revision:
            return
        else:
            sync = self.wait(
                self.api.post(project["url"] + "update/")["url"], utility=True
            )
        if (
            sync["status"] != "successful"
            or self.api.get(project["url"]).get("scm_revision") != self.revision
        ):
            raise ControllerError(
                "Recovery source project did not sync to the reviewed revision"
            )

    def project_job(self, project):
        current = project.get("related", {}).get("current_job")
        if not current and project.get("current_job"):
            current = f"/api/controller/v2/project_updates/{project['current_job']}/"
        if current:
            match = re.search(r"/([0-9]+)/$", current)
            if not match:
                raise ControllerError(
                    "Recovery project returned an invalid sync job URL"
                )
            return self.observer.child_path(
                {"job": int(match[1]), "related": {"job": current}}
            )
        return None

    def aws(self, request):
        launch = self.api.post(
            self.template["url"] + "launch/",
            {"extra_vars": {"ci_recovery_request": request}},
        )
        job = self.wait(launch["url"], utility=True)
        result = job.get("artifacts", {}).get("ci_recovery")
        if result is None:
            for event in self.api.list(
                job["url"] + "job_events/?event=runner_on_ok&page_size=200"
            ):
                candidate = (
                    event.get("event_data", {})
                    .get("res", {})
                    .get("ansible_stats", {})
                    .get("data", {})
                    .get("ci_recovery")
                )
                if isinstance(candidate, dict):
                    result = candidate
        self.case(
            "AWS recovery / " + request["mode"],
            "passed" if job["status"] == "successful" else "failed",
            kind="diagnostic",
            job_id=job["id"],
            summary="Only sanitized resource identifiers are exported.",
        )
        if (
            not isinstance(result, dict)
            or result.get("ok") is not True
            or job["status"] != "successful"
        ):
            raise ControllerError(
                "AWS recovery failed; inspect the protected Controller job "
                + str(job["id"])
            )
        return result["aws"]

    def acquire(self, source_run):
        existing = [
            row
            for row in self.api.collection("inventories")
            if row["name"] == "APD recovery lease"
            and row.get("organization") == self.org
        ]
        if existing:
            if len(existing) != 1:
                raise ControllerError("Multiple recovery locks exist")
            old = existing[0]
            state = variables(old.get("variables"))
            if (
                state.get("source_run") != source_run
                or not re.fullmatch(
                    r"apd-recovery-[a-f0-9]{24}", str(state.get("prefix"))
                )
                or not re.fullmatch(r"[0-9]+-[0-9]+", str(state.get("recovery_run")))
            ):
                raise ControllerError("Another recovery process owns the recovery lock")
            run = gh_json(
                [
                    "api",
                    f"repos/{self.repository}/actions/runs/{state['recovery_run'].split('-')[0]}",
                ]
            )
            if (
                run.get("path", "").split("@")[0]
                != ".github/workflows/apd-recovery.yml"
                or run.get("status") != "completed"
            ):
                raise ControllerError("The previous recovery action is still active")
            projects = {
                row["id"]
                for row in self.api.collection("projects")
                if row.get("organization") == self.org
                and row["name"].startswith(state["prefix"] + " / ")
            }
            for job in self.api.list(
                "/api/controller/v2/unified_jobs/?status__in=new,pending,waiting,running&page_size=200"
            ):
                if (
                    job.get("project") in projects
                    or job.get("summary_fields", {}).get("project", {}).get("id")
                    in projects
                    or job.get("name", "").startswith(state["prefix"] + " / ")
                ):
                    raise ControllerError(
                        "Previous recovery utility jobs are still active"
                    )
            for path in state.get("utility_paths", []):
                if not re.fullmatch(
                    r"/api/controller/v2/(jobs|project_updates)/[0-9]+/", path
                ) or not ready(self.api.get(path)):
                    raise ControllerError(
                        "Previous recovery utility jobs are not settled"
                    )
            for item in reversed(state.get("created", [])):
                if item.get("kind") == "inventories" and item.get("id") == old["id"]:
                    continue
                if (
                    item.get("kind")
                    not in (
                        "projects",
                        "execution_environments",
                        "credentials",
                        "job_templates",
                        "inventories",
                    )
                    or type(item.get("id")) is not int
                    or not item.get("name", "").startswith(state["prefix"] + " / ")
                ):
                    raise ControllerError(
                        "Previous recovery object is outside its namespace"
                    )
                path = f"/api/controller/v2/{item['kind']}/{item['id']}/"
                live = self.api.get(path, missing_ok=True)
                if live is not None and (
                    live.get("name") != item["name"]
                    or (
                        live.get("organization")
                        or live.get("summary_fields", {})
                        .get("organization", {})
                        .get("id")
                    )
                    != self.org
                ):
                    raise ControllerError("Previous recovery object changed ownership")
                self.api.delete(path)
            self.api.delete(old["url"])
        # Inventory names are unique within an organization: this is the atomic
        # exclusion point for two local/GitHub cleanup processes.
        self.lock = self.create(
            "inventories",
            {
                "name": "APD recovery lease",
                "organization": self.org,
                "variables": json.dumps(
                    {
                        "source_run": source_run,
                        "recovery_run": self.recovery_run,
                        "prefix": self.uid,
                    }
                ),
            },
        )
        self.checkpoint()

    def check_region(self, ledger, source, region):
        """Bind legacy reports to immutable Controller launch variables, never defaults."""
        observed = set()
        recorded = source["metadata"].get("region")
        if recorded:
            observed.add(recorded)
        keys = {
            "Deploy Cloud Stack in AWS": "create_vm_aws_region",
            "Destroy Cloud Stack in AWS": "create_vm_aws_region",
            "NETWORK | Deploy Containerlab Stack": "clab_aws_region",
            "NETWORK | Destroy Containerlab Stack": "clab_aws_region",
            "Run report bucket cleanup": "ci_region",
        }
        proven = set()
        for row in source.get("cases", []):
            key = keys.get(row.get("name"))
            if not key or type(row.get("job_id")) is not int:
                continue
            kind = KINDS.get(row.get("kind"))
            if not kind:
                continue
            job = self.api.get(f"/api/controller/v2/{kind}/{row['job_id']}/")
            value = variables(job.get("extra_vars")).get(key)
            if value:
                if not isinstance(value, str):
                    raise ControllerError("Source job has an invalid AWS region")
                observed.add(value)
                proven.add(key)
        if not recorded and (
            not observed
            or ledger.get("cloud_started")
            and "create_vm_aws_region" not in proven
            or ledger.get("network_started")
            and "clab_aws_region" not in proven
        ):
            raise ControllerError(
                "Legacy source has no proven AWS region for every provisioned fixture; lock retained"
            )
        if observed != {region}:
            raise ControllerError("Requested AWS region differs from the source run")

    def plan(self, run_id, region):
        ledger, source, lease = self.source(run_id)
        self.check_region(ledger, source, region)
        self.acquire(run_id)
        jobs = self.jobs(ledger, source)
        objects = self.objects(ledger, ledger["owner"], lease)
        self.utility(source)
        aws = self.aws({"mode": "plan", "owner": ledger["owner"], "region": region})
        extra = [
            key
            for key in ledger.get("pending_cleanup", [])
            if key not in ("standalone VM deletion",) and not key.startswith("AD VM / ")
        ]
        blockers = aws["blockers"] + [
            "An additional cleanup adapter is required for " + key for key in extra
        ]
        if any(
            row.get("job_id")
            and row["name"]
            in (
                "Patch Cloud Stack in AWS",
                "Cloud | AWS | Snapshot EC2",
                "Cloud | AWS | Restore EC2 from Snapshot",
            )
            for row in source.get("cases", [])
        ):
            blockers.append(
                "Snapshot/restore history needs explicit snapshot and detached-volume reconciliation before releasing this lock"
            )
        plan = {
            "schema_version": 1,
            "repository": self.repository,
            "source_run": run_id,
            "source_revision": source["metadata"]["candidate_commit"],
            "controller_url": self.api.base,
            "organization": self.org,
            "project": self.project["id"],
            "owner": ledger["owner"],
            "lease_id": lease["id"],
            "recovery_run": self.recovery_run,
            "recovery_revision": self.revision,
            "jobs": jobs,
            "objects": objects,
            "hosts": self.hosts(ledger["owner"], aws),
            "aws": aws,
            "blockers": blockers,
        }
        plan["fingerprint"] = fingerprint(plan)
        (self.output / "recovery-plan.json").write_text(
            json.dumps(plan, indent=2) + "\n"
        )
        self.report["recovery_plan"] = plan
        self.case(
            "Recovery plan ready for review",
            "blocked" if blockers else "passed",
            summary=f"Cancel {len(jobs)} APD jobs; inspect {len(aws['instances'])} instances, {len(aws['vpcs'])} VPCs, {len(aws['keys'])} owned keys and {len(aws['untagged_keys'])} untagged keys. No AWS resources or previous-run objects were deleted.",
        )
        return plan

    def apply(self, plan, adopt_keys):
        if (
            plan.get("controller_url") != self.api.base
            or plan.get("repository") != self.repository
            or plan.get("blockers")
        ):
            raise ControllerError(
                "Reviewed plan has blockers or belongs to another APD/repository"
            )
        if plan.get("aws", {}).get("owner") != plan.get("owner"):
            raise ControllerError("AWS resource owner differs from the reviewed CI run")
        self.report["recovery_plan"] = plan
        copy = dict(plan)
        copy.pop("fingerprint", None)
        if fingerprint(copy) != plan.get("fingerprint"):
            raise ControllerError("Reviewed recovery plan fingerprint changed")
        ledger, source, lease = self.source(plan["source_run"])
        self.check_region(ledger, source, plan["aws"]["region"])
        if lease["id"] != plan["lease_id"] or ledger["owner"] != plan["owner"]:
            raise ControllerError("APD lock changed since the recovery plan")
        objects = self.objects(ledger, plan["owner"], lease)
        if any(item not in plan["objects"] for item in objects):
            raise ControllerError(
                "Controller cleanup objects changed; generate a new plan"
            )
        self.acquire(plan["source_run"])
        jobs = self.jobs(ledger, source)
        approved = {row["path"] for row in plan["jobs"]}

        def approved_tree(path):
            seen = set()
            while path not in seen:
                if path in approved:
                    return True
                seen.add(path)
                parent = self.observer.parents.get(path)
                if not parent:
                    return False
                path = f"/api/controller/v2/workflow_jobs/{parent['workflow_id']}/"
            return False

        if any(not approved_tree(row["path"]) for row in jobs):
            raise ControllerError(
                "New APD jobs appeared after planning; generate a new plan before canceling them"
            )
        for row in jobs:
            job = self.api.get(row["path"])
            if job.get("status") in ACTIVE:
                self.api.post(row["path"] + "cancel/")
            self.wait(row["path"], 300)
        # Workflow cancellation may schedule additional descendants. They were
        # authorized as part of the reviewed workflow tree; wait for all of them.
        for path in list(self.observer.roots):
            self.wait(path, 300)
        self.utility(source)
        if self.jobs(ledger, source):
            raise ControllerError(
                "APD jobs became active before AWS deletion; generate a new plan"
            )
        self.report["recovery_verification"] = self.aws(
            {
                "mode": "apply",
                "owner": plan["owner"],
                "region": plan["aws"]["region"],
                "plan": plan["aws"],
                "adopt_keys": adopt_keys,
            }
        )
        after = self.report["recovery_verification"]
        if (
            any(
                after.get(key) != plan["aws"][key]
                for key in ("owner", "region", "account")
            )
            or any(item["state"] != "terminated" for item in after.get("instances", []))
            or any(
                after.get(key)
                for key in ("vpcs", "keys", "untagged_keys", "bucket", "blockers")
            )
        ):
            raise ControllerError(
                "AWS verification still has resources or a different scope; integration lock retained"
            )
        if self.jobs(ledger, source):
            raise ControllerError(
                "APD jobs became active during cleanup; integration lock retained"
            )
        for host in self.hosts(plan["owner"], plan["aws"]):
            if host not in plan.get("hosts", []):
                raise ControllerError(
                    "Run-owned inventory hosts changed after planning; generate a new plan"
                )
            self.api.delete(f"/api/controller/v2/hosts/{host['id']}/")
        for item in sorted(objects, key=lambda row: row["kind"] != "job_templates"):
            self.api.delete(item["path"])
        # Keep both leases until utility objects have also been removed. A
        # credential/project cleanup failure must remain recoverable on retry.
        if not self.finish(release_lock=False):
            raise ControllerError(
                "Recovery utility cleanup is incomplete; both locks retained for retry"
            )
        if self.jobs(ledger, source):
            raise ControllerError(
                "APD jobs became active before lock release; integration lock retained"
            )
        # Revalidate the exact lock immediately before releasing it.
        _, _, current = self.source(plan["source_run"])
        if current["id"] != lease["id"]:
            raise ControllerError("APD lock changed during cleanup")
        self.api.delete(lease["url"])
        self.case(
            "APD is ready for another approved test",
            "passed",
            summary="Run-owned AWS resources were verified absent and its temporary Controller objects and integration lock were removed. Base templates, project and credentials remain.",
        )
        self.report["ready_for_test"] = True

    def finish(self, release_lock=True):
        # Also discover an automatic project sync if the request failed before
        # its job URL could be recorded locally.
        for item in self.created:
            if item["kind"] == "projects":
                project = self.api.get(
                    f"/api/controller/v2/projects/{item['id']}/", missing_ok=True
                )
                current = self.project_job(project or {})
                if current and current not in self.utility_paths:
                    self.utility_paths.add(current)
                    self.active.add(current)
        utility_quiet = all(
            path not in self.active and ready(self.observer.jobs.get(path, {}))
            for path in self.utility_paths
        )
        quiet = utility_quiet and (
            self.operation == "plan" or not self.active and not self.observer.unresolved
        )
        if utility_quiet:
            cleanup_failed = False
            for item in reversed(self.created):
                if (
                    self.lock
                    and item["id"] == self.lock["id"]
                    and item["kind"] == "inventories"
                ):
                    continue
                try:
                    self.api.delete(f"/api/controller/v2/{item['kind']}/{item['id']}/")
                except ControllerError:
                    quiet = False
                    cleanup_failed = True
                    self.report["cleanup"].append(
                        {
                            "resource": item["kind"],
                            "status": "failed",
                            "summary": "Temporary recovery object retained; see recovery-receipt.json.",
                        }
                    )
            if self.lock and not cleanup_failed and release_lock:
                self.api.delete(self.lock["url"])
                self.lock = None
            if not cleanup_failed:
                self.report["cleanup"].append(
                    {
                        "resource": "isolated recovery utility objects",
                        "status": "passed",
                    }
                )
        self.report["metadata"]["state"] = "finished" if quiet else "incomplete"
        if quiet:
            self.report["metadata"]["finished"] = datetime.now(timezone.utc).isoformat()
        self.checkpoint()
        return quiet


def trigger(args):
    root = Path(__file__).resolve().parents[2]

    def git(*command):
        result = subprocess.run(
            ["git", *command], cwd=root, capture_output=True, text=True, timeout=120
        )
        if result.returncode:
            raise ControllerError("Git recovery trigger failed; raw output omitted")
        return result.stdout.strip()

    if git("branch", "--show-current") != "codex/apd-integration-ci" or git(
        "status", "--porcelain"
    ):
        raise ControllerError(
            "Recovery trigger needs a clean codex/apd-integration-ci checkout"
        )
    remote = git("remote", "get-url", "origin")
    if remote not in (
        f"git@github.com:{args.repository}.git",
        f"https://github.com/{args.repository}.git",
        f"https://github.com/{args.repository}",
    ):
        raise ControllerError("Recovery trigger refuses a different origin repository")
    message = f"[apd-recovery {args.mode} {args.source}"
    message += f" region={args.region}" if args.mode == "plan" else ""
    message += " keys=" + ",".join(args.adopt_key) if args.adopt_key else ""
    message += "]"
    request_from_message(message)
    git("commit", "--allow-empty", "-m", message)
    git("push", "origin", "codex/apd-integration-ci")
    print(
        "Recovery request pushed. Review the apd-integration deployment before it accesses APD."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "apply", "request", "trigger"))
    parser.add_argument("--repository", default="l3acon/product-demos")
    parser.add_argument("--revision", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--source", default="")
    parser.add_argument("--mode", choices=("plan", "apply"), default="plan")
    parser.add_argument(
        "--region", choices=("us-east-2", "us-west-2"), default="us-east-2"
    )
    parser.add_argument("--adopt-key", action="append", default=[])
    parser.add_argument("--plan", type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path("integration-results/recovery")
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        parser.error("repository must be owner/name")
    if args.command == "request":
        request = request_from_message(os.environ.get("RECOVERY_MESSAGE", ""))
        path = os.environ["GITHUB_OUTPUT"]
        with open(path, "a") as output:
            output.write("".join(f"{key}={value}\n" for key, value in request.items()))
        return 0
    if not re.fullmatch(r"[0-9]+-[0-9]+", args.source) and not (
        args.command == "apply" and args.plan
    ):
        parser.error("source must be a GitHub run-attempt identifier")
    if args.command == "trigger":
        trigger(args)
        return 0
    if not re.fullmatch(r"[a-f0-9]{40}", args.revision):
        parser.error("revision must be an immutable commit SHA")
    args.output.mkdir(parents=True, exist_ok=True)
    recovery = None
    code = 0
    try:
        api = Controller(
            os.environ["APD_URL"],
            os.environ.get("APD_USERNAME"),
            os.environ.get("APD_PASSWORD"),
            os.environ.get("APD_TOKEN"),
        )
        recovery = Recovery(
            api,
            args.repository,
            args.revision,
            args.output,
            f"{os.environ.get('GITHUB_RUN_ID', 'local')}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}",
        )
        recovery.operation = args.command
        if args.command == "plan":
            recovery.plan(args.source, args.region)
        else:
            plan = (
                json.loads(args.plan.read_text())
                if args.plan
                else load_plan(args.repository, args.source, args.output)
            )
            recovery.apply(plan, args.adopt_key)
    except Exception as error:
        message = (
            str(error)
            if isinstance(error, (ControllerError, BuildError))
            else "Recovery configuration/evidence could not be read; raw error omitted"
        )
        if recovery:
            recovery.case("Manual APD recovery", "failed", summary=message)
        else:
            render(
                {
                    "cases": [
                        {
                            "name": "Recovery preflight",
                            "status": "failed",
                            "summary": message,
                        }
                    ]
                },
                args.output,
            )
        print(message)
        code = 1
    finally:
        if recovery:
            try:
                recovery.finish()
            except ControllerError:
                recovery.report["metadata"]["state"] = "incomplete"
                recovery.report["cleanup"].append(
                    {
                        "resource": "recovery utility objects",
                        "status": "failed",
                        "summary": "Recovery finalization failed; inspect recovery-receipt.json before retrying.",
                    }
                )
                recovery.checkpoint()
                code = 1
            if recovery.report["metadata"]["state"] == "incomplete" or any(
                row["status"] == "failed" for row in recovery.report["cleanup"]
            ):
                recovery.report["ready_for_test"] = False
                recovery.checkpoint()
                code = 1
    return code


if __name__ == "__main__":
    sys.exit(main())
