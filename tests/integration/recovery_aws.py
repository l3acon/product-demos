"""Read or remove exact run-owned AWS resources inside an isolated recovery EE.

Input/output are bounded JSON. No credential values, IPs or raw API errors leave
this process. Plan is the default; apply rechecks ownership and exact resource IDs.
"""

import hashlib
import json
import re
import sys
import time


VPC_NAMES = {"aws-test-vpc", "clab-vpc"}
KEY_NAMES = {"aws-test-key", "clab-key"}
INSTANCE_NAMES = {
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


class RecoveryError(RuntimeError):
    pass


def tags(item):
    return {tag["Key"]: tag["Value"] for tag in item.get("Tags", [])}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class AWSRecovery:
    def __init__(self, ec2, s3, account):
        self.ec2, self.s3, self.account = ec2, s3, account

    def rows(self, operation, key, **kwargs):
        rows = []
        while True:
            response = getattr(self.ec2, operation)(**kwargs)
            rows.extend(response.get(key, []))
            if not response.get("NextToken"):
                return rows
            kwargs["NextToken"] = response["NextToken"]

    def inventory(self, owner, region):
        if not re.fullmatch(r"apd-ci-[a-f0-9]{24}", owner) or region not in (
            "us-east-2",
            "us-west-2",
        ):
            raise RecoveryError("Invalid CI resource owner or region")
        blockers = []
        vpcs = self.rows("describe_vpcs", "Vpcs")
        selected = [vpc for vpc in vpcs if tags(vpc).get("owner") == owner]
        for vpc in vpcs:
            if tags(vpc).get("Name") in VPC_NAMES and tags(vpc).get("owner") != owner:
                blockers.append(
                    "Reserved VPC belongs to a different or unverified owner: "
                    + vpc["VpcId"]
                )
        reservations = self.rows("describe_instances", "Reservations")
        instances = [item for row in reservations for item in row.get("Instances", [])]
        vpc_ids = {vpc["VpcId"] for vpc in selected}
        owned_instances = [
            item for item in instances if tags(item).get("owner") == owner
        ]
        for item in instances:
            if item["State"]["Name"] == "terminated":
                continue
            if (
                item.get("VpcId") in vpc_ids
                or tags(item).get("Name") in INSTANCE_NAMES
                or tags(item).get("deployment") == "containerlab"
            ):
                if tags(item).get("owner") != owner:
                    blockers.append(
                        "Instance belongs to a different or unverified owner: "
                        + item["InstanceId"]
                    )
        networks = []
        for vpc in selected:
            vpc_id = vpc["VpcId"]
            filters = [{"Name": "vpc-id", "Values": [vpc_id]}]
            subnets = self.rows("describe_subnets", "Subnets", Filters=filters)
            groups = self.rows(
                "describe_security_groups", "SecurityGroups", Filters=filters
            )
            routes = self.rows("describe_route_tables", "RouteTables", Filters=filters)
            interfaces = self.rows(
                "describe_network_interfaces", "NetworkInterfaces", Filters=filters
            )
            gateways = self.rows(
                "describe_internet_gateways",
                "InternetGateways",
                Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}],
            )
            for resource in subnets + groups + routes + interfaces + gateways:
                if tags(resource).get("owner", owner) != owner:
                    blockers.append(
                        "VPC contains a resource tagged to another owner: " + vpc_id
                    )
            owned_ids = {item["InstanceId"] for item in owned_instances}
            for interface in interfaces:
                if interface.get("RequesterManaged") or interface.get(
                    "Attachment", {}
                ).get("InstanceId", "") not in owned_ids | {""}:
                    blockers.append(
                        "VPC contains an interface not attached to a run-owned instance: "
                        + interface["NetworkInterfaceId"]
                    )
            nat = self.rows("describe_nat_gateways", "NatGateways", Filter=filters)
            endpoints = self.rows(
                "describe_vpc_endpoints", "VpcEndpoints", Filters=filters
            )
            peer = self.rows(
                "describe_vpc_peering_connections", "VpcPeeringConnections"
            )
            transit = self.rows(
                "describe_transit_gateway_vpc_attachments",
                "TransitGatewayVpcAttachments",
                Filters=filters,
            )
            if (
                any(item.get("State") not in ("deleted", "failed") for item in nat)
                or endpoints
                or any(
                    vpc_id
                    in (
                        p.get("RequesterVpcInfo", {}).get("VpcId"),
                        p.get("AccepterVpcInfo", {}).get("VpcId"),
                    )
                    and p.get("Status", {}).get("Code")
                    not in ("deleted", "rejected", "expired", "failed")
                    for p in peer
                )
                or any(
                    item.get("State") not in ("deleted", "failed") for item in transit
                )
            ):
                blockers.append(
                    "VPC has NAT, endpoint, peering or transit dependencies requiring a separate cleanup adapter: "
                    + vpc_id
                )
            networks.append(
                {
                    "id": vpc_id,
                    "subnets": sorted(item["SubnetId"] for item in subnets),
                    "groups": sorted(
                        item["GroupId"]
                        for item in groups
                        if item["GroupName"] != "default"
                    ),
                    "routes": sorted(
                        item["RouteTableId"]
                        for item in routes
                        if not any(a.get("Main") for a in item.get("Associations", []))
                    ),
                    "interfaces": sorted(
                        item["NetworkInterfaceId"] for item in interfaces
                    ),
                    "gateways": sorted(item["InternetGatewayId"] for item in gateways),
                }
            )
        keys = self.rows("describe_key_pairs", "KeyPairs")
        selected_keys = []
        untagged_keys = []
        for key in keys:
            tag = tags(key).get("owner")
            public = {"id": key["KeyPairId"], "name": key["KeyName"]}
            if tag == owner:
                selected_keys.append(public)
            elif key["KeyName"] in KEY_NAMES and not tag:
                untagged_keys.append(public)
            elif key["KeyName"] in KEY_NAMES:
                blockers.append(
                    "Reserved keypair belongs to another owner: " + key["KeyPairId"]
                )
        bucket = owner
        exists = False
        try:
            self.s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=self.account)
            exists = True
        except Exception as error:
            code = str(getattr(error, "response", {}).get("Error", {}).get("Code", ""))
            if code not in ("404", "NoSuchBucket", "NotFound"):
                raise RecoveryError(
                    "Could not verify access to the run report bucket"
                ) from None
        return {
            "account": self.account,
            "region": region,
            "owner": owner,
            "instances": sorted(
                [
                    {
                        "id": i["InstanceId"],
                        "name": tags(i).get("Name", "run-owned instance"),
                        "state": i["State"]["Name"],
                    }
                    for i in owned_instances
                ],
                key=lambda i: i["id"],
            ),
            "vpcs": sorted(networks, key=lambda v: v["id"]),
            "keys": sorted(selected_keys, key=lambda k: k["id"]),
            "untagged_keys": sorted(untagged_keys, key=lambda k: k["id"]),
            "bucket": bucket if exists else None,
            "blockers": sorted(set(blockers)),
        }

    @staticmethod
    def shape(plan):
        # Instance states and auto-removed ENIs may change as cancellation settles.
        return {
            **{
                key: plan[key]
                for key in (
                    "account",
                    "region",
                    "owner",
                    "keys",
                    "untagged_keys",
                    "bucket",
                )
            },
            "instances": [
                {"id": row["id"], "name": row["name"]} for row in plan["instances"]
            ],
            "vpcs": [
                {key: value for key, value in row.items() if key != "interfaces"}
                for row in plan["vpcs"]
            ],
        }

    def apply(self, plan, adopt_keys=()):
        current = self.inventory(plan["owner"], plan["region"])
        approved = self.shape(plan)
        observed = self.shape(current)
        same_scope = all(
            current[key] == plan[key] for key in ("account", "region", "owner")
        )
        subset = all(
            item in approved[key]
            for key in ("instances", "keys", "untagged_keys")
            for item in observed[key]
        )
        for vpc in current["vpcs"]:
            previous = next(
                (item for item in plan["vpcs"] if item["id"] == vpc["id"]), None
            )
            subset = (
                subset
                and previous is not None
                and all(
                    set(vpc[key]) <= set(previous[key])
                    for key in ("subnets", "groups", "routes", "interfaces", "gateways")
                )
            )
        if (
            current["blockers"]
            or not same_scope
            or not subset
            or current["bucket"] not in (None, plan["bucket"])
        ):
            raise RecoveryError(
                "AWS inventory changed or has ownership/dependency blockers; generate a new plan"
            )
        untagged = {row["id"] for row in current["untagged_keys"]}
        if not untagged <= set(adopt_keys) or not set(adopt_keys) <= {
            row["id"] for row in plan["untagged_keys"]
        }:
            raise RecoveryError(
                "Explicitly adopt the exact untagged keypair IDs from the reviewed plan, or resolve them separately"
            )
        ids = [
            item["id"] for item in current["instances"] if item["state"] != "terminated"
        ]
        if ids:
            self.ec2.terminate_instances(InstanceIds=ids)
            self.ec2.get_waiter("instance_terminated").wait(
                InstanceIds=ids, WaiterConfig={"Delay": 10, "MaxAttempts": 90}
            )
        for vpc in current["vpcs"]:
            filters = [{"Name": "vpc-id", "Values": [vpc["id"]]}]
            for attempt in range(12):
                remaining = self.rows(
                    "describe_network_interfaces", "NetworkInterfaces", Filters=filters
                )
                if any(
                    item["NetworkInterfaceId"] not in vpc["interfaces"]
                    for item in remaining
                ):
                    raise RecoveryError(
                        "Unreviewed network interface appeared; generate a new plan"
                    )
                if all(item.get("Status") == "available" for item in remaining):
                    break
                if attempt < 11:
                    time.sleep(5)
            for item in remaining:
                if item.get("Status") != "available" or item.get("RequesterManaged"):
                    raise RecoveryError(
                        "Network interfaces have not detached; retry after they settle"
                    )
                self.ec2.delete_network_interface(
                    NetworkInterfaceId=item["NetworkInterfaceId"]
                )
            # Revoke cross-group references before deleting security groups.
            for group in self.rows(
                "describe_security_groups", "SecurityGroups", Filters=filters
            ):
                if group["GroupId"] in vpc["groups"]:
                    for field, operation in (
                        ("IpPermissions", "revoke_security_group_ingress"),
                        ("IpPermissionsEgress", "revoke_security_group_egress"),
                    ):
                        if group.get(field):
                            getattr(self.ec2, operation)(
                                GroupId=group["GroupId"], IpPermissions=group[field]
                            )
            for subnet in vpc["subnets"]:
                self.ec2.delete_subnet(SubnetId=subnet)
            for route in vpc["routes"]:
                self.ec2.delete_route_table(RouteTableId=route)
            for group in vpc["groups"]:
                self.ec2.delete_security_group(GroupId=group)
            for gateway in vpc["gateways"]:
                self.ec2.detach_internet_gateway(
                    InternetGatewayId=gateway, VpcId=vpc["id"]
                )
                self.ec2.delete_internet_gateway(InternetGatewayId=gateway)
            self.ec2.delete_vpc(VpcId=vpc["id"])
        for key in current["keys"] + current["untagged_keys"]:
            self.ec2.delete_key_pair(KeyPairId=key["id"])
        if current["bucket"]:
            bucket = current["bucket"]
            # The exact unique run bucket can contain versioned report objects.
            for operation, kwargs, key in (
                ("list_object_versions", {}, "Versions"),
                ("list_object_versions", {}, "DeleteMarkers"),
                ("list_objects_v2", {}, "Contents"),
            ):
                paginator = self.s3.get_paginator(operation)
                for page in paginator.paginate(
                    Bucket=bucket, ExpectedBucketOwner=self.account, **kwargs
                ):
                    objects = [
                        {
                            "Key": item["Key"],
                            **(
                                {"VersionId": item["VersionId"]}
                                if "VersionId" in item
                                else {}
                            ),
                        }
                        for item in page.get(key, [])
                    ]
                    for offset in range(0, len(objects), 1000):
                        result = self.s3.delete_objects(
                            Bucket=bucket,
                            ExpectedBucketOwner=self.account,
                            Delete={
                                "Objects": objects[offset : offset + 1000],
                                "Quiet": True,
                            },
                        )
                        if result.get("Errors"):
                            raise RecoveryError(
                                "Report object deletion failed; bucket retained"
                            )
            self.s3.delete_bucket(Bucket=bucket, ExpectedBucketOwner=self.account)
        after = self.inventory(plan["owner"], plan["region"])
        if (
            after["vpcs"]
            or after["keys"]
            or after["untagged_keys"]
            or after["bucket"]
            or any(i["state"] != "terminated" for i in after["instances"])
            or after["blockers"]
        ):
            raise RecoveryError(
                "Cleanup verification found remaining resources; APD lock must be retained"
            )
        return after


def main():
    try:
        request = json.load(sys.stdin)
        import boto3
        from botocore.config import Config

        session = boto3.Session(region_name=request["region"])
        config = Config(
            connect_timeout=10,
            read_timeout=60,
            retries={"max_attempts": 5, "mode": "standard"},
        )
        account = session.client("sts", config=config).get_caller_identity()["Account"]
        recovery = AWSRecovery(
            session.client("ec2", config=config),
            session.client("s3", config=config),
            account,
        )
        if request["mode"] == "plan":
            result = recovery.inventory(request["owner"], request["region"])
        elif request["mode"] == "apply":
            result = recovery.apply(request["plan"], request.get("adopt_keys", []))
        else:
            raise RecoveryError("Unknown recovery operation")
        print(json.dumps({"ok": True, "aws": result}))
        return 0
    except Exception as error:
        code = (
            getattr(error, "response", {})
            .get("Error", {})
            .get("Code", "recovery_failed")
        )
        code = (
            code
            if re.fullmatch(r"[A-Za-z0-9._-]{1,60}", str(code))
            else "recovery_failed"
        )
        message = (
            str(error)
            if isinstance(error, RecoveryError)
            else "AWS recovery could not complete; raw error omitted"
        )
        print(json.dumps({"ok": False, "code": code, "message": message}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
