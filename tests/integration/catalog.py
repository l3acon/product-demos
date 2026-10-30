"""Execute available README scenarios and account for every catalog template.

The graph also contains future functional assertions. Only actual Controller
executions count as execution coverage; unavailable fixtures are explicit blocks.
"""

from pathlib import Path
import subprocess
import tempfile

from controller import ControllerError


# These scenarios need resources beyond the vanilla APD + AWS test fixtures, or
# an adapter that correlates external events / safely resets destructive fixtures.
UNAVAILABLE = {
    "insights": "Insights organization, registration inputs and an assigned compliance profile are not configured for this run.",
    "event-driven-drift": "EDA event correlation and activation cleanup adapters are not implemented.",
    "mattermost": "Mattermost resource cleanup adapter is not implemented.",
    "orchestrator": "AO cluster, LiteMaaS integration and specialized EE fixtures are not configured.",
    "containerlab-upgrade": "A licensed IOS upgrade image, SCP fixture and checksum are not configured.",
    "panos": "PAN-OS Marketplace subscription, firewall and bastion fixtures are not configured.",
    "rosa": "ROSA credentials, specialized EE and cluster fixture are not configured.",
    "vault": "A separate target cluster and scoped Vault namespace are not configured.",
    "policy": "A scoped policy fixture and Controller policy restoration adapter are not implemented.",
    "mcp": "A target AAP operator fixture and CR restoration adapter are not configured.",
    "satellite": "Satellite server, RHEL8 fixture and workflow approval adapter are not configured.",
    "feedback": "A test feedback sink is not configured; no feedback is sent.",
    "cloud-recovery": "Snapshot completion and exact instance/volume restore validation adapter is not implemented.",
    "linux-hardening": "Separate disposable Linux fixtures and reset adapters for each hardening profile are not configured.",
    "windows-hardening": "A separate disposable Windows hardening fixture is not configured.",
}
CLEANUPS = {
    "panos-cleanup": "panos",
    "rosa-cleanup": "rosa",
    "vault-cleanup": "vault",
    "orchestrator-cleanup": "orchestrator",
}


class Catalog:
    def __init__(self, runner):
        self.r = runner
        self.templates = {t["name"]: t for t in runner.graph["templates"]}
        self.scenarios = {s["id"]: s for s in runner.graph["scenarios"]}
        self.results = {
            row["id"]: row for row in runner.report.setdefault("scenarios", [])
        }
        self.public_key = None

    def result(self, identifier, outcome, reason=""):
        row = {"id": identifier, "status": outcome, "summary": reason}
        self.results[identifier] = row
        self.r.report["scenarios"] = list(self.results.values())
        self.r.checkpoint()

    def inputs(self, name, bindings):
        r = self.r
        values = dict(bindings)
        if name.startswith("LINUX |"):
            values.setdefault("_hosts", "aws-rhel9")
        if name.startswith("WINDOWS |"):
            values.setdefault("_hosts", "aws_win1")
        values.update(
            {
                "LINUX | Register RHEL with RHSM": {
                    "_hosts": "aws-rhel9:aws-rhel10:reports"
                },
                "LINUX | Fact Scan": {"_hosts": "aws-rhel9:aws-rhel10:reports"},
                "LINUX | Run Shell Script": {
                    "shell_script": "printf 'APD integration test\\n'"
                },
                "LINUX | Temporary Sudo": {"sudo_user": "ec2-user", "sudo_time": 1},
                "LINUX | System Roles": {"system_roles": ["timesync"]},
                "LINUX | Install Web Console (cockpit)": {"system_roles": ["cockpit"]},
                "WINDOWS | Install IIS": {"iis_message": "APD integration test"},
                "WINDOWS | Chocolatey install specific": {"package_name": "7zip"},
                "Setup Active Directory Domain": {
                    "create_vm_aws_region": r.args.region,
                    "create_vm_vm_owner": r.owner,
                    "create_vm_vm_environment": "Dev",
                },
                "WINDOWS | AD | New User": {
                    "_hosts": "dc01",
                    "firstname": "CI",
                    "surname": r.uid[:10],
                },
            }.get(name, {})
        )
        if name == "LINUX | Add SSH Public Key":
            if self.public_key is None:
                with tempfile.TemporaryDirectory() as directory:
                    key = Path(directory) / "ci-key"
                    subprocess.run(
                        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                        check=True,
                        capture_output=True,
                        timeout=30,
                    )
                    self.public_key = key.with_suffix(".pub").read_text().strip()
            values.update(ssh_user="ec2-user", ssh_public_key=self.public_key)
        if name.startswith("Cloud |") or name == "Patch Cloud Stack in AWS":
            values["aws_region"] = r.args.region
        if name.startswith("NETWORK |"):
            values["clab_aws_region"] = r.args.region
        return values

    def execute(self, name, bindings):
        r = self.r
        spec = self.templates[name]
        r.phase = {
            "linux": "linux",
            "windows": "windows",
            "infrastructure": "infrastructure",
            "network": "network",
        }.get(spec["category"], "cloud")
        if r.clock() >= r.deadline:
            r.case(
                name,
                "blocked",
                spec["kind"],
                summary="Run time budget exhausted before this template could launch.",
            )
            return False
        before = len(r.report["cases"])
        try:
            ok, _ = r.launch(
                name,
                self.inputs(name, bindings),
                workflow=spec["kind"] == "workflow_job_template",
                # Even a green Controller job must have real target execution.
                require_hosts=spec["kind"] == "job_template",
                timeout=5400 if spec["kind"] == "workflow_job_template" else 1800,
            )
            return ok
        except ControllerError as error:
            if not any(
                row["name"] == name and row["status"] == "failed"
                for row in r.report["cases"][before:]
            ):
                r.case(name, "failed", spec["kind"], summary=str(error))
            r.report["coverage"][name] = "failed"
            r.checkpoint()
            return False

    def run(self):
        r = self.r
        self.result("bootstrap", "passed", "Candidate category setup completed.")
        for identifier in r.graph["scenario_topological_order"]:
            if identifier == "bootstrap":
                continue
            scenario = self.scenarios[identifier]
            failed_needs = [
                name
                for name in scenario.get("needs", [])
                if self.results.get(name, {}).get("status") != "passed"
            ]
            if failed_needs:
                self.result(
                    identifier,
                    "blocked",
                    "Prerequisite scenarios did not pass: " + ", ".join(failed_needs),
                )
                continue
            if identifier in UNAVAILABLE:
                self.result(identifier, "blocked", UNAVAILABLE[identifier])
                continue
            if identifier in CLEANUPS:
                self.result(
                    identifier,
                    "blocked",
                    "No resources provisioned by scenario "
                    + CLEANUPS[identifier]
                    + ".",
                )
                continue
            if identifier in ("cloud-cleanup", "containerlab-cleanup"):
                started = (
                    r.cloud_started
                    if identifier == "cloud-cleanup"
                    else r.network_started
                )
                self.result(
                    identifier,
                    "deferred" if started else "blocked",
                    "Teardown runs in finalizers after consumers finish."
                    if started
                    else "Provisioning was not attempted.",
                )
                continue
            if identifier in ("peer-network", "transit-network"):
                if self.results.get("cloud-stack", {}).get("status") != "passed":
                    self.result(
                        identifier,
                        "blocked",
                        "The cloud stack must supply the aws-test-key keypair before advanced networking.",
                    )
                    continue
                if (
                    identifier == "transit-network"
                    and self.results.get("peer-network-cleanup", {}).get("status")
                    != "passed"
                ):
                    self.result(
                        identifier,
                        "blocked",
                        "Peer networking cleanup did not pass; its resources share selectors with transit networking.",
                    )
                    continue
                cleanup = (
                    "Cloud | AWS | Delete "
                    + ("Peer" if identifier == "peer-network" else "Transit")
                    + " Infrastructure"
                )
                r.pending_cleanup[cleanup] = (cleanup, {"aws_region": r.args.region})
                r.checkpoint()
            if identifier in ("peer-network-cleanup", "transit-network-cleanup"):
                name = scenario["steps"][0]["template"]
                if name not in r.pending_cleanup:
                    self.result(
                        identifier,
                        "blocked",
                        "Its provisioning scenario was not attempted.",
                    )
                else:
                    self.result(
                        identifier,
                        "passed" if r.cleanup_template(name) else "failed",
                        "Teardown execution recorded in cleanup results.",
                    )
                continue
            steps = scenario["steps"]
            if identifier == "cloud-stack":
                r.phase = "cloud"
                try:
                    ok = r.cloud(deploy_only=True)
                except ControllerError as error:
                    r.case("Cloud deployment", "failed", summary=str(error))
                    ok = False
                if ok:
                    r.pending_cleanup["standalone VM deletion"] = (
                        "Cloud | AWS | Delete VM",
                        {
                            "create_vm_aws_region": r.args.region,
                            "create_vm_vm_name": "aws-rhel10",
                        },
                    )
                self.result(
                    identifier,
                    "passed" if ok else "failed",
                    "Deployment workflow, child jobs and inventory readiness recorded.",
                )
                continue
            if identifier == "containerlab":
                r.phase = "network"
                try:
                    ok = r.network(deploy_only=True)
                except ControllerError as error:
                    r.case("Containerlab deployment", "failed", summary=str(error))
                    ok = False
                if not ok:
                    self.result(
                        identifier,
                        "failed",
                        "Containerlab deployment or inventory update failed; device consumers were not launched.",
                    )
                    continue
                steps = steps[1:]
            if identifier == "active-directory":
                for host in ("dc01", "winston", "winthrop"):
                    r.pending_cleanup["AD VM / " + host] = (
                        "Cloud | AWS | Delete VM",
                        {
                            "create_vm_aws_region": r.args.region,
                            "create_vm_vm_name": host,
                        },
                    )
                r.checkpoint()
            ok = True
            for step in steps:
                if "template" in step:
                    passed = self.execute(step["template"], step.get("bindings", {}))
                    ok = passed and ok
                    if identifier == "active-directory" and not passed:
                        break  # New User requires the domain deployment.
                elif "inventory" in step:
                    try:
                        passed = r.sync_inventory(step["inventory"])
                    except ControllerError as error:
                        r.case(step["inventory"], "failed", summary=str(error))
                        passed = False
                    ok = passed and ok
            self.result(
                identifier,
                "passed" if ok else "failed",
                "Controller template executions recorded; planned external functional assertions are not claimed.",
            )

    def complete_coverage(self):
        """Make missing execution visible in JSON, HTML, JUnit and the run log."""
        r = self.r
        for identifier, result in list(self.results.items()):
            if result["status"] == "deferred":
                names = [
                    step["template"]
                    for step in self.scenarios[identifier]["steps"]
                    if "template" in step
                ]
                outcomes = [r.report["coverage"][name] for name in names]
                self.result(
                    identifier,
                    "passed"
                    if all(value == "passed" for value in outcomes)
                    else "failed",
                    "Finalizer teardown results recorded; independent AWS absence/ownership verification is disabled."
                    if not r.fixture_guard
                    else "Finalizer teardown results recorded.",
                )
        for name, outcome in list(r.report["coverage"].items()):
            if outcome != "not_selected":
                continue
            assignments = r.graph["coverage"][name]
            reasons = []
            for assignment in assignments:
                result = self.results.get(assignment["scenario"])
                if result and result["status"] == "blocked":
                    reason = result["summary"]
                elif result and result["status"] == "failed":
                    reason = (
                        "Scenario "
                        + result["id"]
                        + " failed before this template executed."
                    )
                elif result:
                    reason = "Its workflow branch was not executed; planned coverage is not actual execution."
                else:
                    reason = (
                        "Runner stopped before scenario " + assignment["scenario"] + "."
                    )
                if reason not in reasons:
                    reasons.append(reason)
            r.phase = {
                "linux": "linux",
                "windows": "windows",
                "infrastructure": "infrastructure",
                "network": "network",
                "bootstrap": "setup",
            }.get(self.templates[name]["category"], "cloud")
            r.report["coverage"][name] = "blocked"
            r.case(name, "blocked", "coverage", summary=" ".join(reasons))
        r.checkpoint()
