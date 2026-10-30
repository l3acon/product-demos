"""Verify publication consumes tested digests and never exposes registry secrets."""

import argparse
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from promote_ee import PromotionError, destination, publish, verified_plan


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.repo = "l3acon/product-demos"
        self.image = "ghcr.io/" + self.repo + "/apd-ee-27@sha256:" + "b" * 64
        self.run = {
            "path": ".github/workflows/apd-integration.yml",
            "event": "push",
            "head_sha": "a" * 40,
            "head_repository": {"full_name": self.repo},
        }
        self.jobs = [
            {
                "name": "Approved fresh APD smoke test",
                "status": "completed",
                "conclusion": "failure",
            }
        ]
        self.report = {
            "sample": False,
            "metadata": {
                "run_id": "123-1",
                "finished": "2026-10-29T12:00:00Z",
                "candidate_commit": "a" * 40,
                "candidate_image": self.image,
            },
            "runtime": {"compatible": True},
            "cases": [
                {
                    "name": "Candidate runtime validation",
                    "status": "passed",
                    "ee_provenance": "configured digest verified",
                }
            ],
            "cleanup": [],
            "ee_build": {
                "mode": "built",
                "candidate_commit": "a" * 40,
                "image": self.image,
                "aap_series": "27",
                "platform": "linux/amd64",
                "base_image": "registry.redhat.io/ee@sha256:" + "c" * 64,
                **{
                    name: "d" * 64
                    for name in (
                        "definition_sha256",
                        "requirements_sha256",
                        "ansible_cfg_sha256",
                        "containerfile_sha256",
                    )
                },
            },
        }

    def plan(self):
        return verified_plan(
            self.repo,
            "123",
            "1",
            self.run,
            self.jobs,
            self.report,
            "quay.io/ansible-product-demos/apd-ee-27",
            "",
        )

    def test_plan_locks_digest_and_exposes_failed_test_outcome_before_approval(self):
        result = self.plan()
        self.assertEqual(result["source_image"], self.image)
        self.assertEqual(result["integration_result"], "failure")
        self.assertTrue(
            result["destination_image"].endswith(":tested-aaaaaaaaaaaa-123-1")
        )
        self.assertNotIn(":latest", result["destination_image"])

    def test_catalog_job_name_is_supported_alongside_previous_smoke_reports(self):
        self.jobs[0]["name"] = "Approved APD template integration test"
        self.assertEqual(self.plan()["source_image"], self.image)

    def test_report_with_pending_controller_jobs_cannot_be_published(self):
        self.report["pending_jobs"] = [{"job_id": 10, "controller_status": "running"}]
        with self.assertRaisesRegex(PromotionError, "active or unverified"):
            self.plan()

    def test_sample_external_wrong_revision_and_wrong_image_are_refused(self):
        original = copy.deepcopy(self.report)
        for mutation in (
            lambda: self.report.update(sample=True),
            lambda: self.report["ee_build"].update(mode="external"),
            lambda: self.report["metadata"].update(candidate_commit="c" * 40),
            lambda: self.report["metadata"].update(
                candidate_image="quay.io/example/ee@sha256:" + "b" * 64
            ),
        ):
            self.report = copy.deepcopy(original)
            mutation()
            with self.assertRaises(PromotionError):
                self.plan()

    def test_incomplete_or_wrong_workflow_is_refused(self):
        self.jobs[0]["status"] = "running"
        with self.assertRaises(PromotionError):
            self.plan()
        self.jobs[0]["status"] = "completed"
        self.run["path"] = ".github/workflows/other.yml"
        with self.assertRaises(PromotionError):
            self.plan()

    def test_any_runtime_failure_prevents_publication(self):
        self.report["cases"].append(
            {"name": "Candidate runtime validation after setup", "status": "failed"}
        )
        with self.assertRaises(PromotionError):
            self.plan()

    def test_destination_rejects_credentials_and_shell_arguments(self):
        for repo, tag in (
            ("quay.io/user:password@host/ee", "tag"),
            ("quay.io/user/ee", "--all"),
            ("quay.io/user/ee", "tag; curl"),
        ):
            with self.assertRaises(PromotionError):
                destination(repo, tag)

    def simulate_copy(self, wrong_digest=False):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            plan = path / "promotion.json"
            plan.write_text(json.dumps(self.plan()))
            args = argparse.Namespace(plan=plan, output=path / "published")

            def execute(arguments, **kwargs):
                calls.append(arguments)
                self.assertNotIn("PRIVATE-PASSWORD", " ".join(arguments))
                if arguments[:2] == ["skopeo", "copy"]:
                    self.assertIn("--preserve-digests", arguments)
                    self.assertIn("docker://" + self.image, arguments)
                    self.assertFalse(
                        any(
                            row.startswith("docker://ghcr.io/")
                            and "@sha256:" not in row
                            for row in arguments
                        )
                    )
                    Path(arguments[arguments.index("--digestfile") + 1]).write_text(
                        "sha256:" + ("c" if wrong_digest else "b") * 64
                    )
                if arguments[:2] == ["skopeo", "inspect"]:
                    return json.dumps({"Digest": "sha256:" + "b" * 64})
                return ""

            with patch.dict(
                "os.environ",
                {
                    "GITHUB_REPOSITORY": self.repo,
                    "GITHUB_ACTOR": "l3acon",
                    "GH_TOKEN": "PRIVATE-PASSWORD",
                    "QUAY_USERNAME": "robot",
                    "QUAY_PASSWORD": "PRIVATE-PASSWORD",
                },
                clear=True,
            ), patch("promote_ee.command", side_effect=execute):
                if wrong_digest:
                    with self.assertRaisesRegex(PromotionError, "differs"):
                        publish(args)
                    self.assertFalse((args.output / "published.json").exists())
                else:
                    publish(args)
                    result = (args.output / "published.json").read_text()
                    self.assertIn("sha256:" + "b" * 64, result)
                    self.assertNotIn("PRIVATE-PASSWORD", result)
        return calls

    def test_copy_preserves_and_independently_checks_digest(self):
        calls = self.simulate_copy()
        self.assertEqual(
            [row[1] for row in calls], ["login", "login", "copy", "inspect"]
        )

    def test_wrong_copied_digest_never_reports_success(self):
        self.simulate_copy(wrong_digest=True)


if __name__ == "__main__":
    unittest.main()
