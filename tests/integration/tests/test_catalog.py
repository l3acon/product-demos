"""Catalog orchestration progresses past guard errors and retains all results."""

import argparse
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from test_runner import FakeController
from catalog import Catalog
from controller import ControllerError
from report import render
from run import Runner


class CatalogController(FakeController):
    def __init__(self):
        super().__init__()
        self.fail_names = set()

    def post(self, path, data=None):
        result = super().post(path, data)
        if path.endswith("launch/") and self.get(path[:-7])["name"] in self.fail_names:
            result["status"] = "failed"
            self.objects[result["url"]]["status"] = "failed"
        return result


class CatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.args = argparse.Namespace(
            output=Path(temporary.name),
            revision="a" * 40,
            repository="https://github.com/l3acon/product-demos",
            image="quay.io/example/candidate@sha256:" + "c" * 64,
            run_id="test-1",
            profile="catalog",
            timeout=10000,
            poll_interval=0,
            region="us-east-2",
            access_cidr="0.0.0.0/0",
            fixture_guard=False,
        )
        self.api = CatalogController()

    def runner(self):
        return Runner(self.api, self.args, sleep=lambda _: None)

    def launches(self):
        return [
            self.api.objects.get(path[:-7], {}).get("name", "")
            for method, path, _ in self.api.calls
            if path.endswith("launch/")
        ]

    def test_disabled_guard_never_runs_and_does_not_prevent_native_deploy_or_cleanup(
        self,
    ):
        self.args.profile = "baseline-smoke"
        self.api.baseline_ok = False
        runner = self.runner()
        with patch.object(
            runner, "guard", side_effect=AssertionError("guard must not run")
        ):
            self.assertEqual(runner.run(), 0)
        self.assertIsNone(runner.guard_template)
        self.assertIn("Deploy Cloud Stack in AWS", self.launches())
        self.assertIn("NETWORK | Deploy Containerlab Stack", self.launches())
        self.assertIn("Destroy Cloud Stack in AWS", self.launches())
        self.assertIn("NETWORK | Destroy Containerlab Stack", self.launches())
        self.assertFalse(runner.baseline_passed)
        self.assertEqual(runner.report["metadata"]["fixture_guard"], "disabled")
        self.assertTrue(
            any(row["status"] == "disabled" for row in runner.report["cases"])
        )

    def test_catalog_runs_available_scenarios_and_accounts_for_every_source_template(
        self,
    ):
        runner = self.runner()
        self.assertEqual(
            runner.run(), 1
        )  # External fixtures are missing, not a full pass.
        self.assertNotIn("not_selected", runner.report["coverage"].values())
        names = self.launches()
        self.assertLess(
            names.index("Deploy Cloud Stack in AWS"),
            names.index("LINUX | Register RHEL with RHSM"),
        )
        self.assertNotIn("WINDOWS | Test Connectivity", names)
        self.assertIn("Setup Active Directory Domain", names)
        self.assertLess(
            names.index("Cloud | AWS | Delete Peer Infrastructure"),
            names.index("Cloud | AWS | Create Transit Infrastructure"),
        )
        self.assertLess(
            names.index("LINUX | Start Service"),
            names.index("Destroy Cloud Stack in AWS"),
        )
        self.assertIn("WINDOWS | AD | New User", names)
        self.assertIn("Patch Cloud Stack in AWS", names)
        self.assertIn("NETWORK | DISA STIG", names)
        self.assertFalse(any(name.startswith("OpenShift |") for name in names))
        blocked = [row for row in runner.report["cases"] if row["status"] == "blocked"]
        self.assertTrue(
            all(row.get("summary") and "job_id" not in row for row in blocked)
        )
        self.assertEqual(runner.report["coverage"]["Patch Dev"], "blocked")
        self.assertEqual(runner.report["coverage"]["LINUX | Fact Scan"], "passed")
        self.assertEqual(runner.report["coverage"]["Cloud | AWS | Delete VM"], "passed")
        xml = ET.parse(self.args.output / "junit.xml").getroot()
        self.assertGreater(int(xml.get("skipped")), 0)
        self.assertIn(
            "Scenario dependencies and blockers",
            (self.args.output / "report.html").read_text(),
        )

    def test_job_failures_accumulate_without_stopping_other_jobs_in_the_lane(self):
        self.api.fail_names = {"LINUX | Troubleshoot", "WINDOWS | Install IIS"}
        runner = self.runner()
        self.assertEqual(runner.run(), 1)
        self.assertIn("LINUX | Podman Webserver", self.launches())
        self.assertIn("WINDOWS | Chocolatey install specific", self.launches())
        self.assertIn("NETWORK | Backup", self.launches())
        self.assertIn("Setup Active Directory Domain", self.launches())
        self.assertIn(
            "Infrastructure | Configuration management - ACME App - Setup",
            self.launches(),
        )
        self.assertIn("Patch Cloud Stack in AWS", self.launches())
        for name in self.api.fail_names:
            self.assertEqual(runner.report["coverage"][name], "failed")
        self.assertEqual(runner.report["coverage"]["WINDOWS | AD | New User"], "passed")

    def test_failed_cloud_blocks_its_consumers_but_network_still_runs_and_both_clean_up(
        self,
    ):
        self.api.cloud_ok = False
        runner = self.runner()
        self.assertEqual(runner.run(), 1)
        self.assertNotIn("LINUX | Register RHEL with RHSM", self.launches())
        self.assertIn("NETWORK | Deploy Containerlab Stack", self.launches())
        self.assertIn("NETWORK | Containerlab | Configure Devices", self.launches())
        self.assertIn("Destroy Cloud Stack in AWS", self.launches())
        self.assertIn("NETWORK | Destroy Containerlab Stack", self.launches())
        self.assertEqual(runner.report["coverage"]["LINUX | Fact Scan"], "blocked")

    def test_auxiliary_workflow_failure_does_not_hide_successfully_provisioned_fixture(
        self,
    ):
        original = self.api.post

        def post(path, data=None):
            result = original(path, data)
            if (
                path.endswith("launch/")
                and self.api.get(path[:-7])["name"] == "Deploy Cloud Stack in AWS"
            ):
                for node in self.api.objects[result["url"]]["nodes"]:
                    if (
                        node.get("job")
                        and node["summary_fields"]["unified_job_template"]["name"]
                        == "Cloud | AWS | VPC Report"
                    ):
                        self.api.objects[node["related"]["job"]]["status"] = "failed"
            return result

        runner = self.runner()
        with patch.object(self.api, "post", side_effect=post):
            self.assertEqual(runner.run(), 1)
        self.assertEqual(
            runner.report["coverage"]["Cloud | AWS | VPC Report"], "failed"
        )
        self.assertEqual(
            runner.report["coverage"]["Deploy Cloud Stack in AWS"], "failed"
        )
        self.assertIn("LINUX | Fact Scan", self.launches())
        self.assertIn("Patch Cloud Stack in AWS", self.launches())

    def test_cleanup_launch_error_does_not_skip_other_teardown(self):
        self.args.profile = "baseline-smoke"
        runner = self.runner()
        original = runner.launch

        def launch(name, *args, **kwargs):
            if name == "NETWORK | Destroy Containerlab Stack":
                raise ControllerError("Mock launch error")
            return original(name, *args, **kwargs)

        with patch.object(runner, "launch", side_effect=launch):
            self.assertEqual(runner.run(), 1)
        self.assertIn("Destroy Cloud Stack in AWS", self.launches())
        self.assertTrue(
            any(
                row["resource"] == "NETWORK | Destroy Containerlab Stack"
                and row["status"] == "failed"
                for row in runner.report["cleanup"]
            )
        )

    def test_time_budget_blocks_remaining_jobs_without_launching_them(self):
        runner = self.runner()
        runner.deadline = -1
        self.assertFalse(Catalog(runner).execute("LINUX | Fact Scan", {}))
        self.assertEqual(self.api.calls, [])
        self.assertEqual(runner.report["cases"][-1]["status"], "blocked")

    def test_successful_parent_with_failed_child_is_failed_coverage_and_network_continues(
        self,
    ):
        original = self.api.post

        def post(path, data=None):
            result = original(path, data)
            if (
                path.endswith("launch/")
                and self.api.get(path[:-7])["name"] == "Deploy Cloud Stack in AWS"
            ):
                for node in self.api.objects[result["url"]]["nodes"]:
                    if node.get("job"):
                        child = self.api.get(node["related"]["job"])
                        if child["name"] == "Cloud | AWS | Create VM":
                            self.api.objects[child["url"]]["status"] = "failed"
                            break
            return result

        runner = self.runner()
        with patch.object(self.api, "post", side_effect=post):
            self.assertEqual(runner.run(), 1)
        self.assertEqual(
            runner.report["coverage"]["Deploy Cloud Stack in AWS"], "failed"
        )
        self.assertEqual(runner.report["coverage"]["Cloud | AWS | Create VM"], "failed")
        self.assertIn("NETWORK | Deploy Containerlab Stack", self.launches())

    def test_blocked_catalog_is_incomplete_and_disabled_guard_is_not_counted_as_passed(
        self,
    ):
        report = {
            "metadata": {"finished": "2026-10-30T12:00:00Z", "profile": "catalog"},
            "cases": [
                {"name": "Fixture guard", "phase": "baseline", "status": "disabled"},
                {
                    "name": "Needs external fixture",
                    "status": "blocked",
                    "summary": "No fixture",
                },
            ],
            "coverage": {"Needs external fixture": "blocked"},
        }
        render(report, self.args.output)
        html = (self.args.output / "report.html").read_text()
        self.assertIn('class="hero blocked"', html)
        self.assertIn("Run incomplete", html)
        self.assertNotIn("Run completed successfully", html)
        xml = ET.parse(self.args.output / "junit.xml").getroot()
        self.assertEqual(xml.get("skipped"), "2")


if __name__ == "__main__":
    unittest.main()
