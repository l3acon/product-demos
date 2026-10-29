"""Exercise Controller's asynchronous deletion and strict pre-provisioning recovery."""

import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from controller import Controller, ControllerError
from recovery import previous_evidence, reconcile_preprovisioning
from test_runner import FakeController


class DeletionTests(unittest.TestCase):
    def response(self, status, body=None):
        response = Mock()
        response.status_code = status
        response.json.side_effect = ValueError("empty body") if body is None else None
        response.json.return_value = body
        return response

    def test_empty_accepted_delete_waits_until_resource_is_absent(self):
        session = Mock()
        accepted = self.response(202)
        pending = self.response(200, {"pending_deletion": True})
        missing = self.response(404)
        session.request.side_effect = [accepted, pending, missing]
        client = Controller(
            "https://aap.example.test",
            "user",
            "password",
            session=session,
            sleep=lambda _: None,
        )
        self.assertEqual(client.delete("/api/controller/v2/inventories/4/"), {})
        accepted.json.assert_not_called()
        self.assertEqual(
            [call.args[0] for call in session.request.call_args_list],
            ["DELETE", "GET", "GET"],
        )

    def test_synchronous_delete_also_verifies_removal(self):
        session = Mock()
        session.request.side_effect = [self.response(204), self.response(404)]
        client = Controller(
            "https://aap.example.test", "user", "password", session=session
        )
        self.assertEqual(client.delete("/api/controller/v2/job_templates/10/"), {})

    def test_already_deleted_resource_is_idempotent(self):
        session = Mock()
        session.request.return_value = self.response(404)
        client = Controller(
            "https://aap.example.test", "user", "password", session=session
        )
        self.assertIsNone(client.delete("/api/controller/v2/inventories/4/"))
        self.assertEqual(session.request.call_count, 1)

    def test_deletion_timeout_is_a_real_cleanup_failure(self):
        session = Mock()
        session.request.side_effect = [
            self.response(202),
            self.response(200, {"pending_deletion": True}),
        ]
        current = [0]
        client = Controller(
            "https://aap.example.test",
            "user",
            "password",
            session=session,
            clock=lambda: current[0],
            sleep=lambda _: current.__setitem__(0, current[0] + 2),
        )
        with self.assertRaisesRegex(ControllerError, "deletion did not complete"):
            client.delete("/api/controller/v2/inventories/4/", timeout=1)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeController()
        self.owner = "apd-ci-" + "a" * 24
        self.run_id = "40901551102-1"
        self.lease = self.api.add(
            "inventories",
            {
                "name": "APD integration lease",
                "organization": self.api.org,
                "variables": json.dumps({"owner": self.owner, "run_id": self.run_id}),
            },
        )
        self.temporary = self.api.add(
            "inventories",
            {"name": self.owner + " / localhost", "organization": self.api.org},
        )
        self.template = self.api.add(
            "job_templates",
            {"name": self.owner + " / runtime", "organization": self.api.org},
        )
        self.ledger = {
            "owner": self.owner,
            "run_id": self.run_id,
            "cloud_started": False,
            "network_started": False,
            "baseline_passed": False,
            "active": [],
            "created": [
                {"kind": kind, "id": row["id"], "name": row["name"]}
                for kind, row in [
                    ("inventories", self.lease),
                    ("inventories", self.temporary),
                    ("job_templates", self.template),
                ]
            ],
        }
        self.report = {
            "metadata": {"resource_owner": self.owner, "run_id": self.run_id},
            "cleanup": [
                {
                    "resource": "original Controller bindings and project checkout",
                    "status": "passed",
                },
                {"resource": "inventories", "status": "failed"},
            ],
        }

    def recover(self):
        return reconcile_preprovisioning(
            self.api,
            self.lease,
            self.ledger,
            self.report,
            self.api.org,
            self.api.project,
        )

    def assert_no_deletes(self):
        self.assertFalse(any(method == "DELETE" for method, _, _ in self.api.calls))

    def test_only_recorded_temporary_objects_are_removed_in_dependency_order(self):
        self.assertEqual(self.recover(), self.run_id)
        deleted = [path for method, path, _ in self.api.calls if method == "DELETE"]
        self.assertEqual(
            deleted, [self.template["url"], self.temporary["url"], self.lease["url"]]
        )
        self.assertIsNotNone(
            self.api.unique("inventories", "Ansible Product Demos Inventory")
        )

    def test_any_infrastructure_intent_prevents_automatic_recovery(self):
        for key in ("cloud_started", "network_started", "baseline_passed"):
            with self.subTest(key=key):
                self.ledger[key] = True
                with self.assertRaises(ControllerError):
                    self.recover()
                self.ledger[key] = False
                self.assert_no_deletes()

    def test_changed_owner_is_not_deleted(self):
        self.api.objects[self.temporary["url"]]["organization"] = 999
        with self.assertRaisesRegex(ControllerError, "ownership changed"):
            self.recover()
        self.assert_no_deletes()

    def test_unverified_configuration_restore_retains_the_lease(self):
        self.report["cleanup"][0]["status"] = "failed"
        with self.assertRaises(ControllerError):
            self.recover()
        self.assert_no_deletes()

    def test_unsupported_ledger_resource_never_reaches_delete(self):
        self.ledger["created"].append(
            {
                "kind": "projects",
                "id": self.api.project,
                "name": "Ansible Product Demos",
            }
        )
        with self.assertRaises(ControllerError):
            self.recover()
        self.assert_no_deletes()

    def test_active_apd_jobs_prevent_recovery(self):
        self.api.add("unified_jobs", {"status": "running", "project": self.api.project})
        with self.assertRaisesRegex(ControllerError, "jobs are active"):
            self.recover()
        self.assert_no_deletes()

    def test_prior_workflow_must_be_completed_before_artifact_download(self):
        with patch("recovery.subprocess.run") as command:
            command.return_value = Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "status": "in_progress",
                        "path": ".github/workflows/apd-integration.yml",
                    }
                ),
            )
            with self.assertRaisesRegex(ControllerError, "active"):
                previous_evidence(
                    "https://github.com/l3acon/product-demos", self.run_id
                )
            self.assertEqual(command.call_count, 1)

    def test_evidence_must_match_the_observed_run(self):
        self.report["metadata"]["run_id"] = "another-run"
        with self.assertRaises(ControllerError):
            self.recover()
        self.assert_no_deletes()


if __name__ == "__main__":
    unittest.main()
