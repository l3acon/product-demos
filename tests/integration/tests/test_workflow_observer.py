"""Regressions for Controller links, late children, failed siblings and finalization."""

import argparse
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_runner import FakeController
from catalog import Catalog
from controller import ControllerError
from report import render
from run import Runner


class WorkflowObserverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        self.current = 0
        self.args = argparse.Namespace(
            output=self.output,
            revision="a" * 40,
            repository="https://github.com/l3acon/product-demos",
            image="quay.io/example/candidate@sha256:" + "c" * 64,
            run_id="test-1",
            profile="catalog",
            timeout=10000,
            poll_interval=1,
            region="us-east-2",
            access_cidr="0.0.0.0/0",
            fixture_guard=False,
        )
        self.api = FakeController()
        self.runner = Runner(
            self.api, self.args, clock=lambda: self.current, sleep=self.sleep
        )
        self.runner.configure()

    def sleep(self, seconds):
        self.current += max(1, seconds)

    def child(self, name="LINUX | Troubleshoot", status="successful"):
        return self.api.add(
            "jobs",
            {
                "name": name,
                "type": "job",
                "status": status,
                "execution_environment": self.api.ee,
                "scm_revision": self.args.revision,
                "host_status_counts": {"ok": 1},
            },
        )

    def node(self, child, identifier="child"):
        return {
            "identifier": identifier,
            "job": child["id"],
            "related": {"job": child["url"]},
            "do_not_run": False,
            "summary_fields": {"unified_job_template": {"name": child["name"]}},
        }

    def workflow(self, nodes, name="Fixture workflow", status="successful"):
        return self.api.add(
            "workflow_jobs",
            {
                "name": name,
                "type": "workflow_job",
                "status": status,
                "nodes": nodes,
            },
        )

    def sequence(self, path, rows):
        original = self.api.get
        seen = []

        def get(requested, **kwargs):
            if requested == path:
                seen.append(requested)
                return copy.deepcopy(rows[min(len(seen) - 1, len(rows) - 1)])
            return original(requested, **kwargs)

        self.api.get = get
        return seen

    def test_parent_terminal_does_not_finish_before_running_child_and_events(self):
        child = self.child()
        workflow = self.workflow([self.node(child)])
        seen = self.sequence(
            child["url"],
            [
                {**child, "status": "running"},
                {**child, "event_processing_finished": False},
                {**child, "event_processing_finished": True},
            ],
        )
        result = self.runner.wait(workflow["url"], 10)
        self.assertEqual(result["status"], "successful")
        self.assertGreaterEqual(len(seen), 4)
        self.assertFalse(self.runner.active)
        self.assertTrue(self.runner.workflow_children(result))
        row = self.runner.report["cases"][-1]
        self.assertEqual(row["workflow_id"], workflow["id"])
        self.assertEqual(row["controller_status"], "successful")

    def test_scheduler_node_without_job_is_not_mistaken_for_untaken_branch(self):
        child = self.child()
        workflow = self.workflow(
            [{"identifier": "late", "job": None, "do_not_run": False}]
        )
        original = self.api.list
        polls = []

        def list_(path):
            if path == workflow["url"] + "workflow_nodes/?page_size=200":
                polls.append(path)
                if len(polls) >= 3:
                    return [self.node(child, "late")]
            return original(path)

        self.api.list = list_
        self.runner.wait(workflow["url"], 10)
        self.assertGreaterEqual(len(polls), 4)
        self.assertIn(child["url"], self.runner.observer.jobs)
        self.assertFalse(self.runner.active)

    def test_nested_workflow_waits_for_grandchildren(self):
        child = self.child()
        nested = self.workflow([self.node(child)], name="Nested workflow")
        root = self.workflow([self.node(nested)])
        seen = self.sequence(child["url"], [{**child, "status": "running"}, child])
        self.runner.wait(root["url"], 15)
        self.assertGreaterEqual(len(seen), 3)
        self.assertTrue(self.runner.workflow_children(root))
        self.assertFalse(self.runner.active)
        self.assertEqual(
            self.runner.observer.parents[child["url"]]["workflow_id"], nested["id"]
        )

    def test_failed_child_does_not_hide_or_cancel_a_running_sibling(self):
        failed = self.child(status="failed")
        sibling = self.child("LINUX | Podman Webserver")
        root = self.workflow(
            [self.node(failed, "failure"), self.node(sibling, "sibling")],
            status="failed",
        )
        seen = self.sequence(
            sibling["url"], [{**sibling, "status": "running"}, sibling]
        )
        self.runner.wait(root["url"], 10)
        self.assertFalse(self.runner.workflow_children(root))
        self.assertGreaterEqual(len(seen), 3)
        rows = {
            row["job_id"]: row
            for row in self.runner.report["cases"]
            if row.get("job_id")
        }
        self.assertEqual(rows[failed["id"]]["status"], "failed")
        self.assertEqual(rows[sibling["id"]]["status"], "passed")
        self.assertFalse(any(path.endswith("cancel/") for _, path, _ in self.api.calls))

    def test_child_resolution_uses_typed_url_and_list_fallback_not_unified_detail(self):
        child = self.child()
        with self.assertRaises(ControllerError):
            self.api.get(f"/api/controller/v2/unified_jobs/{child['id']}/")
        for node in (
            self.node(child),
            {"job": child["id"]},
            {
                "job": child["id"],
                "related": {"job": child["url"].replace("/controller", "")},
            },
        ):
            self.assertEqual(self.runner.observer.child_path(node), child["url"])

    def test_child_links_with_foreign_origin_or_wrong_id_are_refused(self):
        child = self.child()
        for url in (
            "https://attacker.invalid" + child["url"],
            child["url"].replace(str(child["id"]), "999999"),
        ):
            with self.assertRaises(ControllerError):
                self.runner.observer.child_path(
                    {"job": child["id"], "related": {"job": url}}
                )

    def test_missing_child_lookup_does_not_hide_other_children(self):
        missing = self.child()
        sibling = self.child("LINUX | Podman Webserver")
        root = self.workflow(
            [self.node(missing, "missing"), self.node(sibling, "sibling")]
        )
        del self.api.objects[missing["url"]]
        original = self.api.get

        def get(path, **kwargs):
            if path == missing["url"]:
                raise ControllerError("Controller API GET returned HTTP 404")
            return original(path, **kwargs)

        with patch.object(self.api, "get", side_effect=get):
            with self.assertRaises(ControllerError):
                self.runner.observer.inspect(root)
        self.assertIn(sibling["url"], self.runner.observer.jobs)
        self.assertIn(root["url"], self.runner.observer.unresolved)
        self.assertIn(missing["url"], self.runner.active)

    def test_orphaned_template_is_a_failed_node_not_an_endless_scheduler_wait(self):
        root = self.workflow(
            [
                {
                    "identifier": "orphan",
                    "job": None,
                    "do_not_run": False,
                    "unified_job_template": None,
                }
            ],
            status="failed",
        )
        self.runner.wait(root["url"], 10)
        self.assertFalse(self.runner.workflow_children(root))
        self.assertEqual(self.runner.report["cases"][-1]["status"], "failed")
        self.assertFalse(self.runner.active)

    def test_final_barrier_collects_late_children_without_canceling_them_or_duplicating_branches(
        self,
    ):
        root = self.workflow(
            [{"identifier": "untaken", "job": None, "do_not_run": True}]
        )
        self.runner.wait(root["url"], 10)
        self.runner.workflow_children(root)
        child = self.child()
        self.api.objects[root["url"]]["nodes"].append(self.node(child, "late"))
        self.sequence(child["url"], [{**child, "status": "running"}, child])
        self.assertTrue(self.runner.settle_jobs(timeout=10))
        self.assertTrue(self.runner.settle_jobs(timeout=10))
        self.assertEqual(
            len(
                [
                    row
                    for row in self.runner.report["cases"]
                    if row.get("job_id") == child["id"]
                ]
            ),
            1,
        )
        self.assertEqual(
            len(
                [
                    row
                    for row in self.runner.report["cases"]
                    if row.get("node_identifier") == "untaken"
                ]
            ),
            1,
        )
        self.assertFalse(any(path.endswith("cancel/") for _, path, _ in self.api.calls))

    def test_nonterminal_results_are_not_recorded_as_failed_or_passing(self):
        child = self.child(status="running")
        with self.assertRaises(ControllerError):
            self.runner.record_job(child)
        self.assertNotIn(child["url"], self.runner.recorded_jobs)
        self.assertIn(child["url"], self.runner.active)

    def test_cached_provenance_failure_does_not_turn_into_a_pass(self):
        child = self.child()
        self.api.objects[f"/api/controller/v2/execution_environments/{self.api.ee}/"][
            "image"
        ] = "quay.io/example/wrong:latest"
        self.assertFalse(self.runner.record_job(child))
        self.assertFalse(self.runner.record_job(child))

    def test_workflow_members_defer_then_reuse_actual_failed_result_without_relaunch(
        self,
    ):
        self.api.install_catalog()
        catalog = Catalog(self.runner)
        name = "WINDOWS | Test Connectivity"
        self.assertTrue(catalog.execute(name, {}))
        self.assertEqual(
            self.runner.report["workflow_reuse"][name]["status"], "deferred"
        )
        self.assertEqual(self.runner.report["coverage"][name], "not_selected")
        child = self.child(name, "failed")
        root = self.workflow([self.node(child)], name="Setup Active Directory Domain")
        self.runner.workflow_children(root)
        before = len(self.api.calls)
        self.assertFalse(catalog.execute(name, {}))
        self.assertEqual(len(self.api.calls), before)
        self.assertEqual(
            self.runner.report["workflow_reuse"][name]["job_ids"], [child["id"]]
        )
        self.assertEqual(self.runner.report["coverage"][name], "failed")

    def test_unverified_children_retain_bindings_and_make_final_report_incomplete(self):
        child = self.child(status="running")
        root = self.workflow([self.node(child)])
        self.runner.observer.inspect(root)
        original = self.api.post

        def post(path, data=None):
            return {} if path.endswith("cancel/") else original(path, data)

        with patch.object(self.api, "post", side_effect=post), patch.object(
            self.runner, "configure", side_effect=ControllerError("Stop test execution")
        ), patch.object(
            self.runner,
            "wait",
            side_effect=ControllerError("Unable to verify jobs stopped"),
        ):
            self.assertEqual(self.runner.run(), 1)
        report = json.loads((self.output / "report.json").read_text())
        self.assertEqual(report["metadata"]["state"], "incomplete")
        self.assertNotIn("finished", report["metadata"])
        self.assertTrue(report["pending_jobs"])
        self.assertEqual(
            self.api.unique("execution_environments", "Product Demos EE")["image"],
            self.args.image,
        )
        self.assertTrue(
            any(
                row.get("resource") == "integration lease"
                and row["status"] == "retained"
                for row in report["cleanup"]
            )
        )
        html = (self.output / "report.html").read_text()
        self.assertIn("Report incomplete", html)
        self.assertNotIn("Run completed successfully", html)
        self.assertIn(
            "Jobs still active or unverified", (self.output / "summary.md").read_text()
        )

    def test_failed_check_during_execution_keeps_an_in_progress_heading(self):
        render(
            {
                "metadata": {"state": "running"},
                "cases": [{"name": "Failed child", "status": "failed"}],
            },
            self.output,
        )
        html = (self.output / "report.html").read_text()
        self.assertIn("Run in progress — failures recorded", html)
        self.assertNotIn("Run failed —", html)


if __name__ == "__main__":
    unittest.main()
