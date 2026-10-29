"""Behavioral tests use an in-memory Controller; no live credentials or jobs."""

import argparse
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from controller import Controller, ControllerError
from report import render
from run import Runner, callback_inputs, fresh_machine_inputs, public_runtime, variables


ROOT = Path(__file__).resolve().parents[3]


class FakeController:
    def __init__(self):
        self.objects = {}
        self.counter = 10
        self.calls = []
        self.runtime_ok = True
        self.baseline_ok = True
        self.cloud_ok = True
        self.empty_targets = False
        self.ignored = False
        self.rows = {}
        self.add(
            "organizations",
            {"name": "Ansible Product Demos (APD)", "default_environment": None},
        )
        self.org = self.collection("organizations")[0]["id"]
        self.add(
            "execution_environments",
            {
                "name": "Product Demos EE",
                "image": "quay.io/example/original:latest",
                "pull": "missing",
                "organization": None,
                "credential": None,
            },
        )
        self.ee = self.collection("execution_environments")[0]["id"]
        self.add(
            "projects",
            {
                "name": "Ansible Product Demos",
                "organization": self.org,
                "scm_url": "https://github.com/ansible/product-demos",
                "scm_branch": "main",
                "scm_revision": "b" * 40,
                "default_environment": self.ee,
                "scm_update_on_launch": False,
            },
        )
        self.project = self.collection("projects")[0]["id"]
        for name in ("Ansible Product Demos Inventory", "ContainerLab Inventory"):
            self.add("inventories", {"name": name, "organization": self.org})
        self.add("credentials", {"name": "AWS", "organization": self.org})
        self.add(
            "credentials",
            {
                "name": "APD Machine Credential",
                "organization": self.org,
                "inputs": {
                    "username": "ec2-user",
                    "password": "$encrypted$",
                    "ssh_key_data": "$encrypted$",
                },
            },
        )
        self.add(
            "job_templates",
            self.template_data("APD | Single demo setup", "setup_demo.yml"),
        )
        self.add(
            "job_templates",
            self.template_data("APD | Multi-demo setup", "multi_select_setup.yml"),
        )
        callback_type = self.add(
            "credential_types",
            {
                "name": "Red Hat Ansible Automation Platform",
                "inputs": {
                    "fields": [
                        {
                            "id": key,
                            "type": "boolean" if key == "verify_ssl" else "string",
                        }
                        for key in (
                            "host",
                            "username",
                            "password",
                            "oauth_token",
                            "verify_ssl",
                            "request_timeout",
                        )
                    ]
                },
            },
        )
        self.callback = self.add(
            "credentials",
            {
                "name": "AAP Credential",
                "organization": self.org,
                "credential_type": callback_type["id"],
                "inputs": {},
            },
        )
        self.initial = copy.deepcopy(self.objects)

    def credential_inputs(self):
        return {
            "host": "https://aap.example.test",
            "username": "admin",
            "password": "DO-NOT-REPORT",
            "oauth_token": "",
            "verify_ssl": True,
        }

    def template_data(self, name, playbook):
        return {
            "name": name,
            "playbook": playbook,
            "project": self.project,
            "organization": self.org,
            "inventory": 14,
            "execution_environment": self.ee,
            "extra_vars": "{}",
            "survey_enabled": False,
            "ask_variables_on_launch": True,
        }

    def add(self, kind, row):
        self.counter += 1
        row = {
            **copy.deepcopy(row),
            "id": self.counter,
            "url": f"/api/controller/v2/{kind}/{self.counter}/",
        }
        self.objects[row["url"]] = row
        self.rows.setdefault(kind, []).append(row["url"])
        return copy.deepcopy(row)

    def collection(self, kind):
        return [
            copy.deepcopy(self.objects[url])
            for url in self.rows.get(kind, [])
            if url in self.objects
        ]

    def unique(self, kind, name, organization=None):
        rows = [
            row
            for row in self.collection(kind)
            if row["name"] == name
            and (organization is None or row.get("organization") == organization)
        ]
        if len(rows) != 1:
            raise ControllerError("Missing or ambiguous mock resource: " + name)
        return rows[0]

    def get(self, path, **kwargs):
        if kwargs.get("missing_ok") and path not in self.objects:
            return None
        if path == "/api/gateway/v1/ping/":
            return {"version": "2.7.0"}
        if path.endswith("survey_spec/"):
            return {"spec": []}
        if "/unified_jobs/" in path:
            identifier = int(path.rstrip("/").split("/")[-1])
            return next(
                copy.deepcopy(row)
                for row in self.objects.values()
                if row["id"] == identifier
            )
        return copy.deepcopy(self.objects[path])

    def list(self, path):
        if "workflow_nodes/" in path:
            return copy.deepcopy(
                self.objects[path.split("workflow_nodes/")[0]].get("nodes", [])
            )
        if "job_events/" in path:
            return (
                [
                    {"task": task}
                    for task in (
                        "Get packages",
                        "Get services",
                        "Ping the windows host",
                    )
                ]
                if "runner_on_ok" in path
                else []
            )
        if "job_host_summaries/" in path:
            if self.empty_targets:
                return []
            return [
                {"host_name": name, "ok": 3, "dark": 0, "failures": 0}
                for name in (
                    "aws-rhel9",
                    "aws-rhel10",
                    "reports",
                    "aws-dc",
                    "aws_win1",
                    "cat8kv1",
                    "veos2",
                    "veos4",
                    "vjunos3",
                    "clab-hypervisor",
                )
            ]
        if "hosts/" in path:
            prefix = path.split("hosts/")[0]
            inventory = self.get(prefix)
            if inventory["name"] == "Ansible Product Demos Inventory":
                return [
                    {"name": name}
                    for name in (
                        "aws-rhel9",
                        "aws-rhel10",
                        "aws-dc",
                        "aws_win1",
                        "reports",
                    )
                ]
            return []
        raise AssertionError(path)

    def patch(self, path, data):
        self.calls.append(("PATCH", path, copy.deepcopy(data)))
        if path == self.callback["url"] and "inputs" in data:
            schema = self.objects[
                f"/api/controller/v2/credential_types/{self.callback['credential_type']}/"
            ]["inputs"]["fields"]
            types = {field["id"]: field.get("type", "string") for field in schema}
            for name, value in data["inputs"].items():
                expected = {"string": str, "integer": int, "boolean": bool}.get(
                    types.get(name)
                )
                if expected is None or type(value) is not expected:
                    raise ControllerError(
                        "Controller API PATCH returned HTTP 400; invalid input field: "
                        + name
                    )
        self.objects[path].update(copy.deepcopy(data))
        return self.get(path)

    def post(self, path, data=None):
        data = data or {}
        self.calls.append(("POST", path, copy.deepcopy(data)))
        if path.endswith("cancel/"):
            self.objects[path[:-7]]["status"] = "canceled"
            return {}
        if (
            path.endswith("credentials/") and path != "/api/controller/v2/credentials/"
        ) or path.endswith("hosts/"):
            return {}
        if path.endswith("update/"):
            source = self.get(path[:-7])
            kind = "project_update" if "/projects/" in path else "inventory_update"
            if kind == "project_update":
                self.objects[source["url"]]["scm_revision"] = (
                    source["scm_branch"]
                    if len(source["scm_branch"]) == 40
                    else "b" * 40
                )
            return self.add(
                kind + "s",
                {
                    "name": source["name"],
                    "type": kind,
                    "status": "successful",
                    "execution_environment": self.ee,
                },
            )
        if path.endswith("launch/"):
            template = self.get(path[:-7])
            workflow = "/workflow_job_templates/" in path
            status = "successful"
            artifacts = {}
            if "runtime_probe.yml" in template.get("playbook", ""):
                status = "successful" if self.runtime_ok else "failed"
                artifacts = {
                    "ci_runtime": {
                        "core": "2.16.19",
                        "python": "3.12.1",
                        "architecture": "x86_64",
                        "compatible": self.runtime_ok,
                        "collections": [],
                    }
                }
            if "fixture_guard.yml" in template.get("playbook", ""):
                mode = data["extra_vars"]["ci_guard_mode"]
                if mode == "baseline" and not self.baseline_ok:
                    status = "failed"
                artifacts = {
                    "ci_guard_passed": status == "successful",
                    "ci_key_ids": ["key-ci"],
                }
            if template["name"] == "Deploy Cloud Stack in AWS" and not self.cloud_ok:
                status = "failed"
            if template["name"] == "APD | Multi-demo setup":
                self.install_catalog()
            row = self.add(
                "workflow_jobs" if workflow else "jobs",
                {
                    "name": template["name"],
                    "type": "workflow_job" if workflow else "job",
                    "status": status,
                    "execution_environment": None
                    if workflow
                    else template["execution_environment"],
                    "host_status_counts": {} if self.empty_targets else {"ok": 3},
                    "artifacts": artifacts,
                    "scm_revision": self.unique("projects", "Ansible Product Demos")[
                        "scm_revision"
                    ],
                },
            )
            if workflow:
                definition = next(
                    item
                    for item in self.workflow_definitions
                    if item["name"] == template["name"]
                )
                nodes = []
                for node in definition["nodes"]:
                    incoming = [
                        edge
                        for edge in definition["edges"]
                        if edge["to"] == node["identifier"]
                    ]
                    item = {
                        "identifier": node["identifier"],
                        "summary_fields": {
                            "unified_job_template": {"name": node["target"]}
                        },
                        "job": None,
                    }
                    if not incoming or any(
                        edge["condition"] != "failure" for edge in incoming
                    ):
                        child_kind = (
                            "job"
                            if node["kind"] == "job_template"
                            else "inventory_update"
                        )
                        child = self.add(
                            "jobs" if child_kind == "job" else "inventory_updates",
                            {
                                "name": node["target"],
                                "type": child_kind,
                                "status": "successful",
                                "execution_environment": self.ee,
                                "host_status_counts": {"ok": 1},
                                "artifacts": {},
                                "scm_revision": self.unique(
                                    "projects", "Ansible Product Demos"
                                )["scm_revision"],
                            },
                        )
                        item["job"] = child["id"]
                    nodes.append(item)
                self.objects[row["url"]]["nodes"] = nodes
                row = self.get(row["url"])
            if self.ignored:
                row["ignored_fields"] = {"extra_vars": {"secret": "DO-NOT-REPORT"}}
            return row
        kind = path.rstrip("/").split("/")[-1]
        return self.add(kind, data)

    def delete(self, path):
        self.calls.append(("DELETE", path, {}))
        self.objects.pop(path, None)
        return {}

    def install_catalog(self):
        from build_graph import build, load

        graph = build(ROOT, load(ROOT / "tests/integration/scenarios.yml"))
        self.workflow_definitions = graph["workflows"]
        for entry in graph["templates"]:
            kind = (
                "workflow_job_templates"
                if entry["kind"] == "workflow_job_template"
                else "job_templates"
            )
            if any(row["name"] == entry["name"] for row in self.collection(kind)):
                continue
            if kind == "job_templates":
                payload = self.template_data(entry["name"], entry["playbook"])
            else:
                payload = {
                    "name": entry["name"],
                    "organization": self.org,
                    "survey_enabled": False,
                }
            self.add(kind, payload)
        for name in ("AWS Inventory", "ContainerLab Hypervisor", "Insights Inventory"):
            if not any(
                row["name"] == name for row in self.collection("inventory_sources")
            ):
                self.add(
                    "inventory_sources",
                    {
                        "name": name,
                        "source_vars": "---\nfilters: {}\n",
                        "execution_environment": None,
                    },
                )


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.args = argparse.Namespace(
            output=Path(self.temporary.name),
            revision="a" * 40,
            repository="https://github.com/l3acon/product-demos",
            image="quay.io/example/candidate@sha256:" + "c" * 64,
            run_id="test-1",
            profile="baseline-smoke",
            timeout=10000,
            poll_interval=0,
            region="us-east-2",
            access_cidr="0.0.0.0/0",
        )
        self.api = FakeController()

    def runner(self):
        return Runner(self.api, self.args, sleep=lambda _: None)

    def test_fresh_setup_provision_smoke_and_cleanup(self):
        runner = self.runner()
        self.assertEqual(runner.run(), 0)
        calls = self.api.calls
        launches = [
            (self.api.objects.get(path[:-7], {}).get("name"), data)
            for method, path, data in calls
            if path.endswith("launch/")
        ]
        names = [name for name, _ in launches]
        self.assertLess(
            names.index("APD | Single demo setup"),
            names.index("Deploy Cloud Stack in AWS"),
        )
        self.assertIn("NETWORK | Deploy Containerlab Stack", names)
        self.assertIn("NETWORK | Destroy Containerlab Stack", names)
        self.assertIn("Destroy Cloud Stack in AWS", names)
        multi = next(
            data for name, data in launches if name == "APD | Multi-demo setup"
        )
        self.assertNotIn("openshift", multi["extra_vars"]["demos"])
        self.assertNotIn("satellite", multi["extra_vars"]["demos"])
        self.assertEqual(
            self.api.unique("projects", "Ansible Product Demos")["scm_branch"], "main"
        )
        self.assertEqual(
            self.api.unique("execution_environments", "Product Demos EE")["image"],
            "quay.io/example/original:latest",
        )
        self.assertFalse(
            any(
                row["name"] == "APD integration lease"
                for row in self.api.collection("inventories")
            )
        )
        self.assertEqual(runner.report["coverage"]["LINUX | Fact Scan"], "passed")
        self.assertEqual(runner.report["coverage"]["Patch Dev"], "not_selected")
        self.assertEqual(
            self.api.get(self.api.callback["url"])["inputs"]["request_timeout"], "10"
        )

    def test_callback_timeout_follows_installed_schema_and_remains_optional(self):
        connection = self.api.credential_inputs()
        base_fields = [{"id": key, "type": "string"} for key in connection]
        for type_, expected in (("string", "10"), ("integer", 10), ("number", 10)):
            schema = {
                "inputs": {
                    "fields": [*base_fields, {"id": "request_timeout", "type": type_}]
                }
            }
            self.assertEqual(
                callback_inputs(connection, schema)["request_timeout"], expected
            )
        self.assertNotIn(
            "request_timeout",
            callback_inputs(connection, {"inputs": {"fields": base_fields}}),
        )
        self.assertNotIn("request_timeout", connection)

    def test_callback_update_failure_has_its_own_case_and_never_provisions(self):
        original = self.api.patch

        def reject(path, data):
            if path == self.api.callback["url"]:
                raise ControllerError(
                    "Controller API PATCH returned HTTP 400; rejected credential input fields: request_timeout"
                )
            return original(path, data)

        self.api.patch = reject
        runner = self.runner()
        self.assertEqual(runner.run(), 1)
        self.assertTrue(runner.report["runtime"]["compatible"])
        self.assertFalse(runner.cloud_started or runner.network_started)
        failure = next(
            case
            for case in runner.report["cases"]
            if case["name"] == "AAP callback credential"
        )
        self.assertEqual(failure["status"], "failed")
        self.assertIn("request_timeout", failure["summary"])
        self.assertNotIn(
            "DO-NOT-REPORT", (self.args.output / "report.json").read_text()
        )

    def test_runtime_failure_prevents_all_provisioning(self):
        self.api.runtime_ok = False
        runner = self.runner()
        self.assertEqual(runner.run(), 1)
        self.assertFalse(runner.cloud_started or runner.network_started)
        self.assertFalse(
            any(
                "setup_demo.yml" == self.api.objects.get(path[:-7], {}).get("playbook")
                for method, path, _ in self.api.calls
                if path.endswith("launch/")
            )
        )
        self.assertFalse(
            any(
                row["name"] == "APD integration lease"
                for row in self.api.collection("inventories")
            )
        )

    def test_private_image_uses_temporary_registry_credential_and_restores_binding(
        self,
    ):
        self.api.add("credential_types", {"name": "Container Registry"})
        self.args.image = "ghcr.io/l3acon/product-demos/apd-ee-27@sha256:" + "a" * 64
        registry = {
            "APD_REGISTRY_HOST": "ghcr.io",
            "APD_REGISTRY_USERNAME": "l3acon",
            "APD_REGISTRY_PASSWORD": "DO-NOT-REPORT",
        }
        with patch.dict("os.environ", registry):
            runner = self.runner()
            runner.configure()
            temporary = runner.registry_credential
            self.assertEqual(
                self.api.get(runner.ee["url"])["credential"], temporary["id"]
            )
            # Setup can reset image fields; each launch restores the candidate binding.
            self.api.patch(runner.ee["url"], {"credential": None})
            runner.bind()
            self.assertEqual(
                self.api.get(runner.ee["url"])["credential"], temporary["id"]
            )
            runner.finalizers()
        self.assertIsNone(self.api.get(runner.ee["url"])["credential"])
        self.assertIsNone(self.api.get(temporary["url"], missing_ok=True))
        for name in ("report.json", "ledger.json", "report.html"):
            self.assertNotIn("DO-NOT-REPORT", (self.args.output / name).read_text())

    def test_registry_credential_is_retained_when_original_binding_cannot_be_restored(
        self,
    ):
        self.api.add("credential_types", {"name": "Container Registry"})
        self.args.image = "ghcr.io/l3acon/product-demos/apd-ee-27@sha256:" + "a" * 64
        with patch.dict(
            "os.environ",
            {
                "APD_REGISTRY_HOST": "ghcr.io",
                "APD_REGISTRY_USERNAME": "l3acon",
                "APD_REGISTRY_PASSWORD": "DO-NOT-REPORT",
            },
        ):
            runner = self.runner()
            runner.configure()
        original = self.api.patch

        def fail_restore(path, data):
            if path == runner.ee["url"] and data.get("credential") is None:
                raise ControllerError("restore failed")
            return original(path, data)

        self.api.patch = fail_restore
        runner.finalizers()
        self.assertIsNotNone(self.api.get(runner.registry_credential["url"]))
        self.assertTrue(
            any(
                row["resource"] == "credentials" and row["status"] == "failed"
                for row in runner.report["cleanup"]
            )
        )
        self.assertIsNotNone(self.api.get(runner.lease["url"]))

    def test_nonempty_baseline_never_provisions_or_destroys_existing_resources(self):
        self.api.baseline_ok = False
        runner = self.runner()
        self.assertEqual(runner.run(), 1)
        self.assertFalse(runner.cloud_started or runner.network_started)
        for _, path, _ in self.api.calls:
            if path.endswith("launch/") and path[:-7] in self.api.objects:
                self.assertNotIn(
                    self.api.get(path[:-7])["name"],
                    (
                        "Deploy Cloud Stack in AWS",
                        "Destroy Cloud Stack in AWS",
                        "NETWORK | Deploy Containerlab Stack",
                        "NETWORK | Destroy Containerlab Stack",
                    ),
                )

    def test_failed_cloud_still_tests_network_and_cleans_partial_fixtures(self):
        self.api.cloud_ok = False
        runner = self.runner()
        self.assertEqual(runner.run(), 1)
        self.assertTrue(runner.cloud_started and runner.network_started)
        cleanup = {row["resource"]: row["status"] for row in runner.report["cleanup"]}
        self.assertEqual(cleanup["Destroy Cloud Stack in AWS"], "passed")
        self.assertEqual(cleanup["NETWORK | Destroy Containerlab Stack"], "passed")

    def test_green_empty_play_fails_coverage(self):
        self.api.empty_targets = True
        runner = self.runner()
        self.assertEqual(runner.run(), 1)
        self.assertEqual(runner.report["coverage"]["LINUX | Fact Scan"], "failed")

    def test_an_existing_lease_prevents_controller_mutations(self):
        self.api.add(
            "inventories",
            {"name": "APD integration lease", "organization": self.api.org},
        )
        runner = self.runner()
        self.assertEqual(runner.run(), 1)
        self.assertEqual(self.api.calls, [])

    def test_ignored_inputs_are_canceled_and_never_published(self):
        runner = self.runner()
        runner.configure()
        self.api.ignored = True
        with self.assertRaisesRegex(ControllerError, "rejected launch fields"):
            runner.launch("runtime", template=runner.probe_template)
        self.assertTrue(any(path.endswith("cancel/") for _, path, _ in self.api.calls))
        self.assertNotIn(
            "DO-NOT-REPORT", (self.args.output / "report.json").read_text()
        )

    def test_cleanup_failure_retains_lease(self):
        runner = self.runner()
        runner.configure()
        runner.baseline_passed = True
        runner.cloud_started = True
        runner.guard = lambda mode: False
        runner.finalizers()
        self.assertTrue(
            any(
                row["name"] == "APD integration lease"
                for row in self.api.collection("inventories")
            )
        )
        self.assertTrue(
            any(row["status"] == "failed" for row in runner.report["cleanup"])
        )

    def test_runtime_metadata_drops_arbitrary_secret_fields(self):
        result = public_runtime(
            {
                "core": "sensitive-token",
                "python": "3.12.1",
                "architecture": "secret",
                "secret": "DO-NOT-REPORT",
                "tools": {"helm": True},
                "collections": [
                    {
                        "name": "amazon.aws",
                        "version": "9.5.2",
                        "requires_ansible": "a-secret-value",
                        "status": "sensitive",
                    }
                ],
            }
        )
        encoded = json.dumps(result)
        for forbidden in ("DO-NOT-REPORT", "sensitive", "a-secret-value"):
            self.assertNotIn(forbidden, encoded)

    def test_yaml_and_json_source_variables_are_supported(self):
        self.assertEqual(
            variables("---\nregions: [us-east-2]"), {"regions": ["us-east-2"]}
        )
        self.assertEqual(
            variables({"regions": ["us-east-2"]}), {"regions": ["us-east-2"]}
        )

    def test_runtime_stats_are_recovered_when_job_artifacts_are_empty(self):
        runner = self.runner()
        runner.configure()
        runtime = {
            "core": "2.16.19",
            "compatible": False,
            "tools": {"helm": False, "oc": True, "terraform": True},
            "packages": {"ncclient": "0.7.0", "passlib": "1.7.4", "pywinrm": "0.5.0"},
            "collections": [
                {
                    "name": "amazon.aws",
                    "version": "10.0.0",
                    "requires_ansible": ">=2.17.0",
                    "source_constraint": ">=7.5.0,<10.0.0",
                    "status": "outside_source_constraint",
                }
            ],
            "secret": "DO-NOT-REPORT",
        }
        original = self.api.list
        self.api.list = (
            lambda path: [
                {
                    "event_data": {
                        "res": {"ansible_stats": {"data": {"ci_runtime": runtime}}}
                    }
                }
            ]
            if "runner_on_ok" in path
            else original(path)
        )
        job = self.api.add(
            "jobs",
            {
                "name": "runtime",
                "type": "job",
                "status": "failed",
                "execution_environment": self.api.ee,
                "scm_revision": self.args.revision,
                "artifacts": {},
            },
        )
        self.assertFalse(
            runner.record_job(job, "Candidate runtime validation", "diagnostic")
        )
        self.assertEqual(runner.report["runtime"]["core"], "2.16.19")
        self.assertIn("amazon.aws", runner.report["cases"][-1]["summary"])
        self.assertIn("helm", runner.report["cases"][-1]["summary"])
        self.assertNotIn(
            "DO-NOT-REPORT", (self.args.output / "report.json").read_text()
        )
        self.assertIn(
            "EE validation failures", (self.args.output / "report.html").read_text()
        )

    def test_terminal_execution_waits_for_event_ingestion(self):
        current = [0]
        runner = Runner(
            self.api,
            self.args,
            clock=lambda: current[0],
            sleep=lambda _: current.__setitem__(0, current[0] + 1),
        )
        job = self.api.add(
            "jobs",
            {
                "name": "finished",
                "type": "job",
                "status": "successful",
                "event_processing_finished": False,
            },
        )
        original = self.api.get
        calls = []

        def get(path, **kwargs):
            row = original(path, **kwargs)
            calls.append(path)
            row["event_processing_finished"] = len(calls) > 1
            return row

        self.api.get = get
        finished = runner.wait(job["url"], timeout=10)
        self.assertTrue(finished["event_processing_finished"])
        self.assertEqual(len(calls), 2)
        self.assertFalse(runner.active)

    def test_wrong_image_cannot_count_as_candidate_coverage(self):
        runner = self.runner()
        runner.configure()
        other = self.api.add(
            "execution_environments",
            {"name": "wrong image", "image": "quay.io/example/wrong:latest"},
        )
        job = self.api.add(
            "jobs",
            {
                "name": "LINUX | Fact Scan",
                "type": "job",
                "status": "successful",
                "execution_environment": other["id"],
                "scm_revision": self.args.revision,
            },
        )
        self.assertFalse(runner.record_job(job))
        self.assertEqual(runner.report["coverage"]["LINUX | Fact Scan"], "failed")

    def test_wrong_project_revision_fails_even_with_correct_image(self):
        runner = self.runner()
        runner.configure()
        job = self.api.add(
            "jobs",
            {
                "name": "LINUX | Fact Scan",
                "type": "job",
                "status": "successful",
                "execution_environment": self.api.ee,
                "scm_revision": "b" * 40,
            },
        )
        self.assertFalse(runner.record_job(job))

    def test_child_failure_is_visible_and_failure_text_is_not_exported(self):
        runner = self.runner()
        runner.configure()
        child = self.api.add(
            "jobs",
            {
                "name": "LINUX | Fact Scan",
                "type": "job",
                "status": "failed",
                "execution_environment": self.api.ee,
                "scm_revision": self.args.revision,
            },
        )
        workflow = self.api.add(
            "workflow_jobs",
            {
                "name": "test workflow",
                "type": "workflow_job",
                "status": "successful",
                "nodes": [
                    {
                        "identifier": "child",
                        "job": child["id"],
                        "summary_fields": {
                            "unified_job_template": {"name": "LINUX | Fact Scan"}
                        },
                    }
                ],
            },
        )
        self.assertFalse(runner.workflow_children(workflow))
        self.assertEqual(runner.report["coverage"]["LINUX | Fact Scan"], "failed")

    def test_changed_workflow_definition_is_rejected(self):
        runner = self.runner()
        workflow = self.api.add(
            "workflow_jobs", {"name": "Deploy Cloud Stack in AWS", "nodes": []}
        )
        self.assertFalse(runner.workflow_children(workflow))

    def test_timeout_requests_cancellation_before_cleanup(self):
        current = [0]
        runner = Runner(
            self.api,
            self.args,
            clock=lambda: current[0],
            sleep=lambda seconds: current.__setitem__(0, current[0] + max(seconds, 1)),
        )
        job = self.api.add("jobs", {"name": "pending", "status": "running"})
        with self.assertRaisesRegex(ControllerError, "timeout"):
            runner.wait(job["url"], timeout=2)
        self.assertEqual(self.api.get(job["url"])["status"], "canceled")
        self.assertFalse(runner.active)


class ClientAndReportTests(unittest.TestCase):
    def test_credential_validation_errors_report_field_ids_without_rejected_values(
        self,
    ):
        session = Mock()
        session.request.return_value.status_code = 400
        session.request.return_value.json.return_value = {
            "inputs": [
                "request_timeout: 10 is not of type string",
                "password: DO-NOT-REPORT",
            ]
        }
        client = Controller(
            "https://aap.example.test", "user", "secret", session=session
        )
        with self.assertRaises(ControllerError) as raised:
            client.patch(
                "/api/controller/v2/credentials/1/",
                {"inputs": {"password": "DO-NOT-REPORT", "request_timeout": 10}},
            )
        self.assertIn("request_timeout", str(raised.exception))
        self.assertNotIn("DO-NOT-REPORT", str(raised.exception))

    def test_existing_opaque_machine_secrets_are_not_replaced(self):
        existing = {
            "username": "ec2-user",
            "password": "$encrypted$",
            "ssh_key_data": "$encrypted$",
        }
        factory = Mock()
        self.assertEqual(fresh_machine_inputs(existing, factory), existing)
        factory.assert_not_called()

    def test_missing_machine_authentication_is_created_without_other_inputs(self):
        prepared = fresh_machine_inputs(
            {"password": "REPLACEME"}, lambda: "test-private-key"
        )
        self.assertEqual(prepared["username"], "ec2-user")
        self.assertEqual(prepared["ssh_key_data"], "test-private-key")
        self.assertGreaterEqual(len(prepared["password"]), 30)

    def test_guard_covers_the_real_containerlab_teardown_selectors(self):
        from build_graph import load

        guard = load(ROOT / "tests/integration/fixture_guard.yml")
        teardown = load(ROOT / "network/teardown_hypervisor.yml")
        selections = [
            task["amazon.aws.ec2_instance_info"]["filters"]
            for task in guard[0]["tasks"]
            if "amazon.aws.ec2_instance_info" in task
        ]
        for task in teardown[0]["tasks"]:
            if "amazon.aws.ec2_instance_info" not in task:
                continue
            destructive = task["amazon.aws.ec2_instance_info"]["filters"]

            def covers(selection):
                for key, value in selection.items():
                    if key not in destructive:
                        return False
                    actual = destructive[key]
                    if not set(actual if isinstance(actual, list) else [actual]) <= set(
                        value if isinstance(value, list) else [value]
                    ):
                        return False
                return True

            self.assertTrue(
                any(covers(selection) for selection in selections), destructive
            )

    def test_cross_origin_pagination_is_rejected(self):
        session = Mock()
        client = Controller(
            "https://aap.example.test", "user", "secret", session=session
        )
        with self.assertRaises(ControllerError):
            client.get("https://attacker.example.test/api/")
        session.request.assert_not_called()

    def test_api_errors_do_not_include_server_response_or_credentials(self):
        session = Mock()
        session.request.return_value.status_code = 400
        session.request.return_value.text = "password=DO-NOT-REPORT"
        client = Controller(
            "https://aap.example.test", "user", "secret", session=session
        )
        with self.assertRaises(ControllerError) as error:
            client.post(
                "/api/controller/v2/job_templates/", {"secret": "DO-NOT-REPORT"}
            )
        self.assertNotIn("DO-NOT-REPORT", str(error.exception))
        self.assertFalse(session.request.call_args.kwargs["allow_redirects"])

    def test_report_escapes_job_supplied_text_and_generates_junit(self):
        with tempfile.TemporaryDirectory() as directory:
            render(
                {
                    "cases": [
                        {
                            "name": "<script>alert(1)</script>",
                            "status": "failed",
                            "summary": "<img src=x onerror=alert(2)>",
                        }
                    ]
                },
                directory,
            )
            html = (Path(directory) / "report.html").read_text()
            self.assertNotIn("<script>", html)
            self.assertNotIn("<img", html)
            self.assertIn("&lt;script&gt;", html)
            self.assertIn('failures="1"', (Path(directory) / "junit.xml").read_text())


if __name__ == "__main__":
    unittest.main()
