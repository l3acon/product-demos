"""Plan and approve an exact tested GHCR digest, then copy it to Quay unchanged."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from build_ee import BuildError, validate_metadata


class PromotionError(RuntimeError):
    pass


def command(arguments, *, stdin=None, timeout=120):
    try:
        result = subprocess.run(
            arguments, input=stdin, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        raise PromotionError(f"{arguments[0]} could not complete") from None
    if result.returncode:
        raise PromotionError(
            f"{arguments[0]} failed; verify artifact access or registry credentials. Raw response omitted."
        )
    return result.stdout


def destination(repository, tag):
    if not re.fullmatch(
        r"quay\.io/[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9_.-]*", repository
    ) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
        raise PromotionError(
            "Provide a Quay repository and a plain image tag without credentials or digests"
        )
    return repository + ":" + tag


def verified_plan(repository, run_id, attempt, run, jobs, report, quay_repository, tag):
    if (
        run.get("path", "").split("@")[0] != ".github/workflows/apd-integration.yml"
        or run.get("event") not in ("push", "workflow_dispatch")
        or run.get("head_repository", {}).get("full_name", "").lower()
        != repository.lower()
    ):
        raise PromotionError(
            "Only this repository's integration workflow may supply a tested image"
        )
    test_jobs = [
        row
        for row in jobs
        if row.get("name")
        in ("Approved fresh APD smoke test", "Approved APD template integration test")
    ]
    if (
        len(test_jobs) != 1
        or test_jobs[0].get("status") != "completed"
        or test_jobs[0].get("conclusion") not in ("success", "failure")
    ):
        raise PromotionError("The selected integration test attempt has not completed")
    metadata = report.get("metadata", {})
    if metadata.get("state") in ("running", "finalizing", "incomplete") or report.get(
        "pending_jobs"
    ):
        raise PromotionError(
            "The integration report still has active or unverified jobs"
        )
    revision, image = metadata.get("candidate_commit", ""), metadata.get(
        "candidate_image", ""
    )
    if (
        report.get("sample") is not False
        or metadata.get("run_id") != f"{run_id}-{attempt}"
        or not metadata.get("finished")
        or revision != run.get("head_sha")
        or not re.fullmatch(r"[a-f0-9]{40}", revision)
    ):
        raise PromotionError(
            "The integration report does not match the selected run and attempt"
        )
    if not re.fullmatch(
        re.escape(f"ghcr.io/{repository.lower()}/apd-ee-27") + r"@sha256:[a-f0-9]{64}",
        image,
    ):
        raise PromotionError(
            "Promotion requires the tested immutable image built in this repository's GHCR namespace"
        )
    build = report.get("ee_build", {})
    if (
        build.get("mode") != "built"
        or report.get("runtime", {}).get("compatible") is not True
        or not any(
            row.get("name") == "Candidate runtime validation"
            and row.get("status") == "passed"
            and row.get("ee_provenance") == "configured digest verified"
            for row in report.get("cases", [])
        )
    ):
        raise PromotionError(
            "A successful built-image runtime check inside APD is required"
        )
    if any(
        "runtime validation" in row.get("name", "").lower()
        and row.get("status") != "passed"
        for row in report.get("cases", [])
    ):
        raise PromotionError("A runtime validation check failed; promotion refused")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "build.json"
        path.write_text(json.dumps(build))
        try:
            validate_metadata(path, revision, image)
        except (BuildError, ValueError, OSError):
            raise PromotionError(
                "Build provenance does not match the tested image"
            ) from None
    tag = tag or f"tested-{revision[:12]}-{run_id}-{attempt}"
    return {
        "schema_version": 1,
        "repository": repository,
        "source_run": str(run_id),
        "source_attempt": str(attempt),
        "candidate_commit": revision,
        "source_image": image,
        "destination_image": destination(quay_repository, tag),
        "integration_result": test_jobs[0]["conclusion"],
        "case_counts": dict(
            Counter(row.get("status", "unknown") for row in report.get("cases", []))
        ),
        "cleanup_failed": any(
            row.get("status") in ("failed", "retained")
            for row in report.get("cleanup", [])
        ),
        "source_url": f"https://github.com/{repository}/actions/runs/{run_id}",
        "scope": report.get("scope", "Baseline smoke test"),
    }


def plan(args):
    run = json.loads(
        command(["gh", "api", f"repos/{args.repository}/actions/runs/{args.run_id}"])
    )
    jobs = json.loads(
        command(
            [
                "gh",
                "api",
                f"repos/{args.repository}/actions/runs/{args.run_id}/attempts/{args.attempt}/jobs?per_page=100",
            ]
        )
    )["jobs"]
    with tempfile.TemporaryDirectory() as directory:
        command(
            [
                "gh",
                "run",
                "download",
                args.run_id,
                "--repo",
                args.repository,
                "--name",
                f"apd-live-report-{args.run_id}-{args.attempt}",
                "--dir",
                directory,
            ]
        )
        report_path = Path(directory) / "report.json"
        if report_path.stat().st_size > 1024 * 1024:
            raise PromotionError("Integration evidence is unexpectedly large")
        report = json.loads(report_path.read_text())
    result = verified_plan(
        args.repository,
        args.run_id,
        args.attempt,
        run,
        jobs,
        report,
        args.quay_repository,
        args.tag,
    )
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "promotion.json").write_text(json.dumps(result, indent=2) + "\n")
    summary = f"""# Quay publication awaiting approval

Source: `{result['source_image']}`

Destination: `{result['destination_image']}`

Candidate commit: `{result['candidate_commit']}`

Integration result: **{result['integration_result']}**. Cleanup failed: **{result['cleanup_failed']}**.

Case counts: `{json.dumps(result['case_counts'], sort_keys=True)}`

[Review the integration report and logs]({result['source_url']}) before approving **quay-publish**.
The image passed runtime compatibility in APD. The smoke suite can still have failures;
approval explicitly accepts the result shown above. Only the tested digest is copied;
there is no rebuild, tag-based source lookup, or implicit update of `latest`.
"""
    (args.output / "summary.md").write_text(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write(summary)


def publish(args):
    if args.plan.stat().st_size > 16384:
        raise PromotionError("Promotion plan is unexpectedly large")
    selected = json.loads(args.plan.read_text())
    source = selected["source_image"]
    repo = os.environ["GITHUB_REPOSITORY"]
    if selected.get("repository") != repo or not re.fullmatch(
        re.escape(f"ghcr.io/{repo.lower()}/apd-ee-27") + r"@sha256:[a-f0-9]{64}", source
    ):
        raise PromotionError("The approved plan has an unexpected source registry")
    target = selected["destination_image"]
    quay_repository, tag = target.rsplit(":", 1)
    destination(quay_repository, tag)
    required = ("GH_TOKEN", "GITHUB_ACTOR", "QUAY_USERNAME", "QUAY_PASSWORD")
    if any(not os.environ.get(name) for name in required):
        raise PromotionError(
            "Missing Quay publication secrets or GHCR read credentials"
        )
    with tempfile.TemporaryDirectory(prefix="apd-quay-") as directory:
        auth = str(Path(directory) / "auth.json")
        for registry, username, password in (
            ("ghcr.io", os.environ["GITHUB_ACTOR"], os.environ["GH_TOKEN"]),
            ("quay.io", os.environ["QUAY_USERNAME"], os.environ["QUAY_PASSWORD"]),
        ):
            command(
                [
                    "skopeo",
                    "login",
                    "--authfile",
                    auth,
                    "--username",
                    username,
                    "--password-stdin",
                    registry,
                ],
                stdin=password + "\n",
            )
        digest_file = Path(directory) / "digest"
        command(
            [
                "skopeo",
                "copy",
                "--all",
                "--preserve-digests",
                "--authfile",
                auth,
                "--digestfile",
                str(digest_file),
                "docker://" + source,
                "docker://" + target,
            ],
            timeout=1800,
        )
        digest = digest_file.read_text().strip()
        if digest != source.split("@")[1]:
            raise PromotionError("Copied image digest differs from the approved source")
        observed = json.loads(
            command(
                [
                    "skopeo",
                    "inspect",
                    "--no-tags",
                    "--authfile",
                    auth,
                    "docker://" + target,
                ]
            )
        )
        if observed.get("Digest") != digest:
            raise PromotionError(
                "Quay tag does not resolve to the approved tested digest"
            )
    args.output.mkdir(parents=True, exist_ok=True)
    result = {
        **selected,
        "published_image": quay_repository + "@" + digest,
        "published_at": datetime.now(timezone.utc).isoformat(),
    }
    (args.output / "published.json").write_text(json.dumps(result, indent=2) + "\n")
    summary = f"# Tested EE published to Quay\n\n`{target}`\n\nVerified digest: `{digest}`\n\n[Source integration run]({selected['source_url']})\n"
    (args.output / "summary.md").write_text(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write(summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("plan")
    prepare.add_argument("--repository", required=True)
    prepare.add_argument("--run-id", required=True)
    prepare.add_argument("--attempt", required=True)
    prepare.add_argument(
        "--quay-repository", default="quay.io/ansible-product-demos/apd-ee-27"
    )
    prepare.add_argument("--tag", default="")
    copy = commands.add_parser("publish")
    copy.add_argument("--plan", required=True, type=Path)
    for sub in (prepare, copy):
        sub.add_argument(
            "--output", type=Path, default=Path("integration-results/quay")
        )
    args = parser.parse_args()
    if args.command == "plan" and (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository)
        or not re.fullmatch(r"[0-9]+", args.run_id)
        or not re.fullmatch(r"[0-9]+", args.attempt)
    ):
        parser.error("Supply a GitHub repository and numeric run / attempt")
    try:
        plan(args) if args.command == "plan" else publish(args)
    except (PromotionError, OSError, ValueError, KeyError) as error:
        print(
            str(error)
            if isinstance(error, PromotionError)
            else "Promotion evidence is missing or invalid; publication refused."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
