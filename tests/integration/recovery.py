"""Recover only proven pre-provisioning leftovers from this repository's CI artifacts."""

import json
from pathlib import Path
import re
import subprocess
import tempfile

from controller import ControllerError


def previous_evidence(repository, run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9]+-[0-9]+", run_id):
        raise ControllerError("Existing lease has no valid GitHub run reference")
    number = run_id.split("-")[0]
    repo = repository.removeprefix("https://github.com/").removesuffix(".git")

    def gh(arguments):
        try:
            result = subprocess.run(
                ["gh", *arguments], capture_output=True, text=True, timeout=120
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ControllerError(
                "Could not inspect prior GitHub run for lease recovery"
            ) from None
        if result.returncode:
            raise ControllerError(
                "Prior-run artifact access failed; reconcile the lease manually"
            )
        return result.stdout

    try:
        run = json.loads(gh(["api", f"repos/{repo}/actions/runs/{number}"]))
        if (
            run.get("status") != "completed"
            or run.get("path") != ".github/workflows/apd-integration.yml"
        ):
            raise ControllerError(
                "Previous integration run is active or has unexpected provenance"
            )
        with tempfile.TemporaryDirectory() as directory:
            gh(
                [
                    "run",
                    "download",
                    number,
                    "--repo",
                    repo,
                    "--name",
                    "apd-live-report-" + run_id,
                    "--dir",
                    directory,
                ]
            )

            def read(name):
                path = Path(directory) / name
                if path.stat().st_size > 1024 * 1024:
                    raise ControllerError("Prior-run evidence is unexpectedly large")
                return json.loads(path.read_text())

            ledger, report = read("ledger.json"), read("report.json")
            if report.get("sample") is not False or report.get("metadata", {}).get(
                "candidate_commit"
            ) != run.get("head_sha"):
                raise ControllerError(
                    "Prior report does not match the completed workflow revision"
                )
            return ledger, report
    except (OSError, ValueError, KeyError):
        raise ControllerError(
            "Prior-run recovery evidence is missing or invalid"
        ) from None


def reconcile_preprovisioning(api, lease, ledger, report, organization, project):
    """No AWS deletion and no restoration of unknown configuration is permitted here."""
    lease_variables = lease.get("variables", {})
    if isinstance(lease_variables, str):
        lease_variables = json.loads(lease_variables)
    owner = lease_variables.get("owner", "")
    run_id = lease_variables.get("run_id")
    if not re.fullmatch(r"apd-ci-[a-f0-9]{24}", owner):
        raise ControllerError("Lease owner cannot be verified for recovery")
    if (
        ledger.get("owner") != owner
        or ledger.get("run_id") != run_id
        or report.get("metadata", {}).get("run_id") != run_id
        or report.get("metadata", {}).get("resource_owner") != owner
    ):
        raise ControllerError("Prior-run evidence does not match the existing lease")
    if (
        any(
            ledger.get(key) is not False
            for key in ("cloud_started", "network_started", "baseline_passed")
        )
        or ledger.get("active") != []
        or bool(ledger.get("pending_cleanup"))
    ):
        raise ControllerError(
            "Prior run may have provisioned infrastructure; automatic lease recovery refused"
        )
    cleanup = report.get("cleanup", [])
    if not any(
        row.get("resource") == "original Controller bindings and project checkout"
        and row.get("status") == "passed"
        for row in cleanup
    ):
        raise ControllerError(
            "Previous Controller configuration restoration was not verified"
        )
    if any(
        row.get("status") == "failed"
        and row.get("resource")
        not in ("inventories", "job_templates", "credentials", "integration lease")
        for row in cleanup
    ):
        raise ControllerError(
            "Previous run has unresolved cleanup outside temporary Controller objects"
        )
    if any(
        row.get("status") in ("new", "pending", "waiting", "running")
        and (row.get("organization") == organization or row.get("project") == project)
        for row in api.collection("unified_jobs")
    ):
        raise ControllerError("APD jobs are active; lease recovery refused")

    owned = []
    recorded_lease = False
    for resource in ledger.get("created", []):
        kind = resource.get("kind")
        if (
            kind not in ("inventories", "job_templates", "credentials")
            or type(resource.get("id")) is not int
        ):
            raise ControllerError("Prior ledger contains an unsupported resource")
        is_lease = (
            kind == "inventories"
            and resource["id"] == lease["id"]
            and resource.get("name") == "APD integration lease"
        )
        if not is_lease and not resource.get("name", "").startswith(owner + " / "):
            raise ControllerError("Prior resource name is outside the recorded run")
        path = f"/api/controller/v2/{kind}/{resource['id']}/"
        observed = api.get(path, missing_ok=True)
        if observed is not None:
            actual_org = observed.get("organization") or observed.get(
                "summary_fields", {}
            ).get("organization", {}).get("id")
            if observed.get("name") != resource["name"] or actual_org != organization:
                raise ControllerError(
                    "Prior resource ownership changed; recovery refused"
                )
        if is_lease:
            recorded_lease = True
        else:
            owned.append(path)
    if not recorded_lease:
        raise ControllerError("Prior ledger does not identify the observed lease")
    # Validate the whole ledger before deleting anything; dependent templates go first.
    for path in reversed(owned):
        api.delete(path)
    observed = api.get(lease["url"], missing_ok=True)
    if observed is not None:
        current = observed.get("variables", {})
        current = json.loads(current) if isinstance(current, str) else current
        if current.get("owner") != owner or current.get("run_id") != run_id:
            raise ControllerError("Lease changed during recovery")
        api.delete(lease["url"])
    return run_id
