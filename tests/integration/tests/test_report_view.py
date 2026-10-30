"""Exercise the human report and event redaction at the public artifact boundary."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diagnostics import public_events
from report import render, sample
from report_view import job_url


class ReportViewTests(unittest.TestCase):
    def test_sample_shows_disabled_guard_and_actual_template_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(__file__).resolve().parents[3]
            graph = json.loads(
                (root / "tests/integration/generated/dependency-graph.json").read_text()
            )
            sample(graph, Path(directory))
            report = json.loads((Path(directory) / "report.json").read_text())
            cases = {row["name"]: row for row in report["cases"]}
            self.assertTrue(report["sample"])
            self.assertEqual(cases["Fixture guard disabled"]["status"], "disabled")
            self.assertEqual(cases["Cloud | AWS | Create VM"]["status"], "failed")
            self.assertEqual(
                cases["NETWORK | Deploy Containerlab Stack"]["status"], "passed"
            )
            self.assertNotIn("Fixture baseline", cases)

    def test_unsupported_ec2_parameter_is_reported_without_invocation(self):
        root = Path(__file__).resolve().parents[3]
        path = (
            "collections/ansible_collections/demo/cloud/roles/aws/tasks/create_vm.yml"
        )
        line = next(
            index
            for index, text in enumerate((root / path).read_text().splitlines(), 1)
            if text.strip() == "- name: Create instance"
        )
        event = {
            "event_data": {
                "task_path": "/runner/project/" + path + ":" + str(line),
                "task_action": "amazon.aws.ec2_instance",
                "host": "localhost",
                "res": {
                    "msg": "Unsupported parameters for (amazon.aws.ec2_instance) module: tenancy. Supported parameters include placement.",
                    "invocation": {
                        "module_args": {
                            "access_key": "DO-NOT-PUBLISH",
                            "secret_key": "DO-NOT-PUBLISH",
                        }
                    },
                },
            }
        }
        output = public_events([event], root, {"localhost"})
        self.assertEqual(output[0]["unsupported_parameters"], ["tenancy"])
        self.assertNotIn("DO-NOT-PUBLISH", json.dumps(output))

    def test_logs_use_canonical_source_task_names_and_drop_rendered_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tasks.yml").write_text(
                "- name: Inspect reserved VPCs\n  amazon.aws.ec2_vpc_net_info:\n    region: '{{ ci_region }}'\n"
            )
            events = [
                {
                    "event": "runner_on_failed",
                    "event_data": {
                        "task": "DO-NOT-PUBLISH",
                        "task_path": "/runner/project/tasks.yml:1",
                        "task_action": "amazon.aws.ec2_vpc_net_info",
                        "host": "localhost",
                        "res": {
                            "msg": "DO-NOT-PUBLISH",
                            "exception": "DO-NOT-PUBLISH",
                            "stdout": "DO-NOT-PUBLISH",
                            "_ansible_no_log": True,
                        },
                    },
                }
            ]
            public = public_events(events, root, {"localhost"})
            self.assertEqual(public[0]["task"], "Inspect reserved VPCs")
            self.assertTrue(public[0]["redacted"])
            self.assertNotIn("DO-NOT-PUBLISH", json.dumps(public))
            report = {
                "metadata": {
                    "controller_url": "https://aap.example.test",
                    "profile": "baseline-smoke",
                },
                "cases": [
                    {
                        "name": "Fixture baseline",
                        "status": "failed",
                        "phase": "baseline",
                        "job_id": 33,
                        "summary": "AWS inspection failed",
                        "events": public,
                    }
                ],
            }
            render(report, root / "out")
            for filename in ("report.html", "report.json", "run.log"):
                self.assertNotIn(
                    "DO-NOT-PUBLISH", (root / "out" / filename).read_text()
                )
            html = (root / "out/report.html").read_text()
            for text in (
                "What failed",
                "Run progression",
                "EE versions",
                "Inspect reserved VPCs",
                "https://aap.example.test/execution/jobs/playbook/33/output",
            ):
                self.assertIn(text, html)

    def test_path_traversal_unknown_hosts_and_actions_are_not_published(self):
        with tempfile.TemporaryDirectory() as root:
            events = [
                {
                    "event_data": {
                        "task_path": "/runner/project/../../private.yml:1",
                        "task": "SECRET",
                        "host": "SECRET",
                        "task_action": "SECRET password",
                        "res": {},
                    }
                }
            ]
            output = public_events(events, root, {"localhost"})
            self.assertNotIn("SECRET", json.dumps(output))
            self.assertNotIn("source", output[0])

    def test_log_links_reject_credentials_and_active_url_schemes(self):
        for base in (
            "javascript:alert(1)",
            "https://user:password@aap.example.test",
            "http://aap.example.test",
        ):
            self.assertIsNone(
                job_url({"metadata": {"controller_url": base}}, {"job_id": 4})
            )

    def test_cleanup_failure_makes_a_green_job_run_visually_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            render(
                {
                    "cases": [{"name": "Smoke test", "status": "passed"}],
                    "cleanup": [{"resource": "AWS stack", "status": "failed"}],
                },
                directory,
            )
            html = (Path(directory) / "report.html").read_text()
            self.assertIn("Cleanup needs attention", html)
            self.assertIn('class="hero failed"', html)
            self.assertIn('failures="1"', (Path(directory) / "junit.xml").read_text())


if __name__ == "__main__":
    unittest.main()
