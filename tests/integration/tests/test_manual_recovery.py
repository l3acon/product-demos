"""Manual recovery is run-scoped, reviewable, retryable and releases locks last."""

import argparse
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_runner import FakeController
from controller import ControllerError
from recover_apd import Recovery, fingerprint, request_from_message, trigger
from recovery_aws import AWSRecovery, RecoveryError


OWNER = "apd-ci-" + "a" * 24


class RecoveryAPI(FakeController):
    base = "https://aap.example.test"

    def list(self, path):
        if "unified_jobs/?status__in=" in path:
            return [
                copy.deepcopy(row)
                for row in self.objects.values()
                if row.get("type") in ("job", "workflow_job")
                and row.get("status") in ("new", "pending", "waiting", "running")
            ]
        return super().list(path)


class ManualRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.api = RecoveryAPI()
        self.lease = self.api.add(
            "inventories",
            {
                "name": "APD integration lease",
                "organization": self.api.org,
                "variables": json.dumps({"owner": OWNER, "run_id": "41083465329-2"}),
            },
        )
        self.temporary = self.api.add(
            "inventories",
            {"name": OWNER + " / localhost", "organization": self.api.org},
        )
        self.ledger = {
            "owner": OWNER,
            "run_id": "41083465329-2",
            "created": [
                {"kind": "inventories", "id": row["id"], "name": row["name"]}
                for row in (self.lease, self.temporary)
            ],
            "active": [],
            "pending_cleanup": [],
            "cloud_started": True,
            "network_started": True,
        }
        self.source = {
            "sample": False,
            "metadata": {
                "controller_url": self.api.base,
                "candidate_commit": "b" * 40,
                "resource_owner": OWNER,
                "run_id": "41083465329-2",
                "region": "us-east-2",
            },
            "cases": [],
        }
        self.aws = {
            "owner": OWNER,
            "region": "us-east-2",
            "account": "123456789012",
            "instances": [{"id": "i-a", "name": "aws-rhel9", "state": "stopped"}],
            "vpcs": [],
            "keys": [],
            "untagged_keys": [],
            "bucket": None,
            "blockers": [],
        }
        self.recovery = Recovery(
            self.api,
            "l3acon/product-demos",
            "c" * 40,
            Path(directory.name),
            "555-1",
            sleep=lambda _: None,
        )
        self.recovery.utility = Mock()
        self.recovery.aws = Mock(return_value=self.aws)
        patcher = patch(
            "recover_apd.previous_evidence", return_value=(self.ledger, self.source)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def plan(self):
        result = self.recovery.plan("41083465329-2", "us-east-2")
        self.recovery.finish()
        self.recovery.aws.return_value = {**self.aws, "instances": []}
        return result

    def test_plan_does_not_delete_prior_objects_or_call_aws_apply(self):
        plan = self.plan()
        self.assertIsNotNone(self.api.get(self.lease["url"]))
        self.assertIsNotNone(self.api.get(self.temporary["url"]))
        self.assertEqual(self.recovery.aws.call_args.args[0]["mode"], "plan")
        copy_ = dict(plan)
        copy_.pop("fingerprint")
        self.assertEqual(plan["fingerprint"], fingerprint(copy_))
        self.assertIn(
            "Recovery plan — review before apply",
            (self.recovery.output / "report.html").read_text(),
        )

        self.assertNotIn(
            "No failures reported. Cleanup is complete.",
            (self.recovery.output / "report.html").read_text(),
        )

    def test_apply_releases_old_lock_last_and_preserves_base_objects(self):
        plan = self.plan()
        self.recovery.operation = "apply"
        self.recovery.apply(plan, [])
        deletes = [path for method, path, _ in self.api.calls if method == "DELETE"]
        self.assertEqual(deletes[-1], self.lease["url"])
        self.assertIn(self.temporary["url"], deletes)
        self.assertIsNotNone(self.api.unique("projects", "Ansible Product Demos"))
        self.assertIsNotNone(self.api.unique("credentials", "AWS"))
        self.assertIsNotNone(self.api.unique("credentials", "APD Machine Credential"))
        self.assertTrue(self.recovery.report["ready_for_test"])

    def test_failed_aws_cleanup_retains_old_lock_and_prior_objects(self):
        plan = self.plan()
        self.recovery.operation = "apply"
        self.recovery.aws.side_effect = ControllerError("Mock AWS cleanup failure")
        with self.assertRaisesRegex(ControllerError, "AWS cleanup"):
            self.recovery.apply(plan, [])
        self.assertIsNotNone(self.api.get(self.lease["url"]))
        self.assertIsNotNone(self.api.get(self.temporary["url"]))

    def test_nonempty_verification_never_releases_the_integration_lock(self):
        plan = self.plan()
        self.recovery.operation = "apply"
        self.recovery.aws.return_value = self.aws
        with self.assertRaisesRegex(ControllerError, "verification still has"):
            self.recovery.apply(plan, [])
        self.assertIsNotNone(self.api.get(self.lease["url"]))
        self.assertIsNotNone(self.api.get(self.temporary["url"]))

    def test_plan_reviews_active_jobs_without_canceling_them(self):
        job = self.api.add(
            "jobs",
            {
                "name": "Manual APD job",
                "type": "job",
                "status": "running",
                "project": self.api.project,
            },
        )
        plan = self.plan()
        self.assertIn(job["id"], [row["id"] for row in plan["jobs"]])
        self.assertEqual(self.api.get(job["url"])["status"], "running")
        self.assertFalse(any(path.endswith("cancel/") for _, path, _ in self.api.calls))
        self.assertEqual(self.recovery.report["metadata"]["state"], "finished")

    def test_ci_cannot_start_while_manual_recovery_holds_its_lock(self):
        from run import Runner

        self.recovery.source("41083465329-2")
        self.recovery.acquire("41083465329-2")
        args = argparse.Namespace(
            output=self.recovery.output / "ci",
            revision="c" * 40,
            repository="https://github.com/l3acon/product-demos",
            image="ghcr.io/l3acon/product-demos/apd-ee-27@sha256:" + "d" * 64,
            run_id="123-1",
            profile="baseline-smoke",
            timeout=100,
            poll_interval=0,
            region="us-east-2",
            access_cidr="0.0.0.0/0",
            fixture_guard=False,
        )
        before = len(self.api.calls)
        runner = Runner(self.api, args, sleep=lambda _: None)
        self.assertEqual(runner.run(), 1)
        self.assertEqual(self.api.calls[before:], [])
        self.assertIn(
            "Manual APD recovery is active", runner.report["cases"][-1]["summary"]
        )

    def test_new_active_apd_job_is_not_canceled_or_deleted_without_a_new_plan(self):
        plan = self.plan()
        job = self.api.add(
            "jobs",
            {
                "name": "Another APD job",
                "type": "job",
                "status": "running",
                "project": self.api.project,
            },
        )
        before = len(self.api.calls)
        with self.assertRaisesRegex(ControllerError, "New APD jobs"):
            self.recovery.apply(plan, [])
        self.assertFalse(
            any(
                path == job["url"] + "cancel/" for _, path, _ in self.api.calls[before:]
            )
        )
        self.assertIsNotNone(self.api.get(self.lease["url"]))

    def test_changed_lock_or_foreign_aws_owner_is_refused_before_delete(self):
        plan = self.plan()
        plan["aws"]["owner"] = "apd-ci-" + "d" * 24
        copy_ = dict(plan)
        copy_.pop("fingerprint")
        plan["fingerprint"] = fingerprint(copy_)
        before = len(self.api.calls)
        with self.assertRaisesRegex(ControllerError, "owner differs"):
            self.recovery.apply(plan, [])
        self.assertFalse(
            any(method == "DELETE" for method, _, _ in self.api.calls[before:])
        )

    def test_source_for_another_apd_instance_is_refused(self):
        self.source["metadata"]["controller_url"] = "https://other.example.test"
        with self.assertRaisesRegex(ControllerError, "different APD"):
            self.recovery.plan("41083465329-2", "us-east-2")
        self.recovery.utility.assert_not_called()

    def test_referenced_registry_credential_is_not_deleted(self):
        credential = self.api.add(
            "credentials",
            {"name": OWNER + " / image pull", "organization": self.api.org},
        )
        self.ledger["created"].append(
            {"kind": "credentials", "id": credential["id"], "name": credential["name"]}
        )
        self.api.objects[f"/api/controller/v2/execution_environments/{self.api.ee}/"][
            "credential"
        ] = credential["id"]
        with self.assertRaisesRegex(ControllerError, "still bound"):
            self.recovery.plan("41083465329-2", "us-east-2")
        self.recovery.aws.assert_not_called()

    def test_credentials_are_not_in_plan_or_receipt(self):
        self.api.objects[self.api.callback["url"]]["inputs"] = {
            "password": "DO-NOT-PUBLISH"
        }
        self.plan()
        for path in self.recovery.output.glob("*"):
            if path.is_file():
                self.assertNotIn("DO-NOT-PUBLISH", path.read_text())

    def test_request_validation_refuses_shell_fragments_and_wrong_regions(self):
        valid = request_from_message(
            "[apd-recovery apply 555-1 keys=key-0123456789abcdef0]"
        )
        self.assertEqual(valid["keys"], "key-0123456789abcdef0")
        for message in (
            "[apd-recovery plan 123-1]",
            "[apd-recovery plan 123-1 region=eu-west-1]",
            "[apd-recovery apply 555-1 keys=$(id)]",
            "[apd-recovery apply 555-1 keys=all]",
        ):
            with self.assertRaises(ControllerError):
                request_from_message(message)

    def test_automatic_project_sync_is_waited_instead_of_launched_again(self):
        project = self.api.add(
            "projects",
            {
                "name": "utility",
                "organization": self.api.org,
                "scm_revision": "c" * 40,
                "related": {"current_job": "/api/v2/project_updates/999/"},
            },
        )
        self.recovery.wait = Mock(return_value={"status": "successful"})
        before = len(self.api.calls)
        self.recovery.sync_project(project)
        self.recovery.wait.assert_called_once_with(
            "/api/controller/v2/project_updates/999/", utility=True
        )
        self.assertFalse(
            any(
                path == project["url"] + "update/"
                for _, path, _ in self.api.calls[before:]
            )
        )

    def test_active_recovery_prevents_a_second_cleanup_process(self):
        self.recovery.source("41083465329-2")
        self.recovery.acquire("41083465329-2")
        with patch(
            "recover_apd.gh_json",
            return_value={
                "path": ".github/workflows/apd-recovery.yml",
                "status": "in_progress",
            },
        ):
            with self.assertRaisesRegex(ControllerError, "still active"):
                self.recovery.acquire("41083465329-2")

    def test_region_change_is_refused_before_creating_utility_objects(self):
        self.source["metadata"]["region"] = "us-east-2"
        with self.assertRaisesRegex(ControllerError, "region differs"):
            self.recovery.plan("41083465329-2", "us-west-2")
        self.recovery.utility.assert_not_called()

    def test_legacy_source_without_region_proof_cannot_clear_its_lock(self):
        self.source["metadata"].pop("region")
        with self.assertRaisesRegex(ControllerError, "no proven AWS region"):
            self.recovery.plan("41083465329-2", "us-west-2")
        self.recovery.utility.assert_not_called()
        self.assertIsNotNone(self.api.get(self.lease["url"]))

    def test_legacy_region_is_bound_to_each_provisioned_fixture_launch(self):
        self.source["metadata"].pop("region")
        for name, key in (
            ("Deploy Cloud Stack in AWS", "create_vm_aws_region"),
            ("NETWORK | Deploy Containerlab Stack", "clab_aws_region"),
        ):
            job = self.api.add(
                "workflow_jobs", {"extra_vars": json.dumps({key: "us-east-2"})}
            )
            self.source["cases"].append(
                {"name": name, "kind": "workflow_job_template", "job_id": job["id"]}
            )
        self.recovery.check_region(self.ledger, self.source, "us-east-2")
        with self.assertRaisesRegex(ControllerError, "region differs"):
            self.recovery.check_region(self.ledger, self.source, "us-west-2")

    def test_failed_utility_cleanup_keeps_recovery_lease_for_reconciliation(self):
        self.recovery.source("41083465329-2")
        self.recovery.acquire("41083465329-2")
        credential = self.recovery.create(
            "credentials",
            {"name": self.recovery.uid + " / registry", "organization": self.api.org},
        )
        original = self.api.delete

        def fail_one(path):
            if path == credential["url"]:
                raise ControllerError("Mock cleanup failure")
            original(path)

        with patch.object(self.api, "delete", side_effect=fail_one):
            self.recovery.finish()
        self.assertIsNotNone(self.recovery.lock)
        self.assertIsNotNone(self.api.get(self.recovery.lock["url"]))
        self.assertEqual(self.recovery.report["metadata"]["state"], "incomplete")

    def test_apply_does_not_release_source_lock_before_utility_cleanup(self):
        plan = self.plan()
        self.recovery.operation = "apply"

        def utility(_):
            self.recovery.create(
                "credentials",
                {
                    "name": self.recovery.uid + " / registry",
                    "organization": self.api.org,
                },
            )

        self.recovery.utility.side_effect = utility
        original = self.api.delete

        def fail_credentials(path):
            if "/credentials/" in path:
                raise ControllerError("Mock credential delete failure")
            return original(path)

        with patch.object(self.api, "delete", side_effect=fail_credentials):
            with self.assertRaisesRegex(ControllerError, "both locks retained"):
                self.recovery.apply(plan, [])
        self.assertIsNotNone(self.api.get(self.lease["url"]))
        self.assertIsNotNone(self.api.get(self.recovery.lock["url"]))
        self.assertFalse(self.recovery.report.get("ready_for_test"))

    def test_completed_action_does_not_imply_its_unrecorded_utility_job_has_stopped(
        self,
    ):
        self.recovery.source("41083465329-2")
        self.recovery.acquire("41083465329-2")
        project = self.recovery.create(
            "projects",
            {"name": self.recovery.uid + " / project", "organization": self.api.org},
        )
        self.api.add(
            "jobs",
            {
                "name": self.recovery.uid + " / AWS recovery",
                "type": "job",
                "status": "running",
                "project": project["id"],
            },
        )
        before = len(self.api.calls)
        with patch(
            "recover_apd.gh_json",
            return_value={
                "path": ".github/workflows/apd-recovery.yml",
                "status": "completed",
            },
        ):
            with self.assertRaisesRegex(
                ControllerError, "utility jobs are still active"
            ):
                self.recovery.acquire("41083465329-2")
        self.assertFalse(
            any(method == "DELETE" for method, _, _ in self.api.calls[before:])
        )


class AWSRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.ec2, self.s3 = Mock(), Mock()
        self.vpcs = [
            {
                "VpcId": "vpc-a",
                "Tags": [
                    {"Key": "owner", "Value": OWNER},
                    {"Key": "Name", "Value": "aws-test-vpc"},
                ],
            }
        ]
        self.instances = [
            {
                "InstanceId": "i-a",
                "VpcId": "vpc-a",
                "State": {"Name": "stopped"},
                "Tags": [
                    {"Key": "owner", "Value": OWNER},
                    {"Key": "Name", "Value": "aws-rhel9"},
                ],
            }
        ]
        self.keys = [
            {
                "KeyPairId": "key-a",
                "KeyName": "aws-test-key",
                "Tags": [{"Key": "owner", "Value": OWNER}],
            }
        ]
        self.ec2.describe_vpcs.side_effect = lambda **kw: {
            "Vpcs": copy.deepcopy(self.vpcs)
        }
        self.ec2.describe_instances.side_effect = lambda **kw: {
            "Reservations": [{"Instances": copy.deepcopy(self.instances)}]
        }
        self.ec2.describe_key_pairs.side_effect = lambda **kw: {
            "KeyPairs": copy.deepcopy(self.keys)
        }
        for method, key in (
            ("describe_subnets", "Subnets"),
            ("describe_security_groups", "SecurityGroups"),
            ("describe_route_tables", "RouteTables"),
            ("describe_network_interfaces", "NetworkInterfaces"),
            ("describe_internet_gateways", "InternetGateways"),
            ("describe_nat_gateways", "NatGateways"),
            ("describe_vpc_endpoints", "VpcEndpoints"),
            ("describe_vpc_peering_connections", "VpcPeeringConnections"),
            (
                "describe_transit_gateway_vpc_attachments",
                "TransitGatewayVpcAttachments",
            ),
        ):
            getattr(self.ec2, method).return_value = {key: []}
        self.ec2.terminate_instances.side_effect = lambda **kw: [
            item["State"].update(Name="terminated")
            for item in self.instances
            if item["InstanceId"] in kw["InstanceIds"]
        ]
        self.ec2.delete_vpc.side_effect = lambda **kw: self.vpcs.clear()
        self.ec2.delete_key_pair.side_effect = lambda **kw: self.keys.__setitem__(
            slice(None),
            [key for key in self.keys if key["KeyPairId"] != kw["KeyPairId"]],
        )
        missing = RuntimeError("DO-NOT-PUBLISH")
        missing.response = {"Error": {"Code": "404"}}
        self.s3.head_bucket.side_effect = missing
        self.recovery = AWSRecovery(self.ec2, self.s3, "123456789012")

    def test_read_only_plan_omits_addresses_credentials_and_raw_api_fields(self):
        self.instances[0].update(
            PrivateIpAddress="DO-NOT-PUBLISH", PublicIpAddress="DO-NOT-PUBLISH"
        )
        self.keys[0]["KeyMaterial"] = "DO-NOT-PUBLISH"
        plan = self.recovery.inventory(OWNER, "us-east-2")
        self.assertNotIn("DO-NOT-PUBLISH", json.dumps(plan))
        self.ec2.terminate_instances.assert_not_called()
        self.ec2.delete_vpc.assert_not_called()

    def test_foreign_instance_inside_owned_vpc_prevents_every_deletion(self):
        self.instances[0]["Tags"][0]["Value"] = "another-owner"
        plan = self.recovery.inventory(OWNER, "us-east-2")
        self.assertTrue(plan["blockers"])
        with self.assertRaises(RecoveryError):
            self.recovery.apply(plan)
        self.ec2.terminate_instances.assert_not_called()
        self.ec2.delete_vpc.assert_not_called()

    def test_new_resource_after_plan_is_not_deleted(self):
        plan = self.recovery.inventory(OWNER, "us-east-2")
        self.instances.append({**copy.deepcopy(self.instances[0]), "InstanceId": "i-b"})
        with self.assertRaisesRegex(RecoveryError, "inventory changed"):
            self.recovery.apply(plan)
        self.ec2.terminate_instances.assert_not_called()

    def test_untagged_key_requires_exact_reviewed_id_then_retry_is_idempotent(self):
        self.keys.append({"KeyPairId": "key-b", "KeyName": "clab-key"})
        plan = self.recovery.inventory(OWNER, "us-east-2")
        with self.assertRaisesRegex(RecoveryError, "adopt"):
            self.recovery.apply(plan)
        self.ec2.terminate_instances.assert_not_called()
        self.recovery.apply(plan, ["key-b"])
        self.assertFalse(self.vpcs)
        self.assertFalse(self.keys)
        before = self.ec2.terminate_instances.call_count
        self.recovery.apply(plan, ["key-b"])
        self.assertEqual(self.ec2.terminate_instances.call_count, before)

    def test_account_change_refuses_cleanup(self):
        plan = self.recovery.inventory(OWNER, "us-east-2")
        self.recovery.account = "999999999999"
        with self.assertRaisesRegex(RecoveryError, "inventory changed"):
            self.recovery.apply(plan)
        self.ec2.terminate_instances.assert_not_called()

    def test_an_unreviewed_interface_is_not_deleted_after_instance_termination(self):
        plan = self.recovery.inventory(OWNER, "us-east-2")
        self.ec2.describe_network_interfaces.side_effect = [
            {"NetworkInterfaces": []},
            {
                "NetworkInterfaces": [
                    {"NetworkInterfaceId": "eni-new", "Status": "available"}
                ]
            },
        ]
        with self.assertRaisesRegex(RecoveryError, "Unreviewed network"):
            self.recovery.apply(plan)
        self.ec2.delete_network_interface.assert_not_called()


if __name__ == "__main__":
    unittest.main()
