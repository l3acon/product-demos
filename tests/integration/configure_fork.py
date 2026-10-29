"""Configure a protected APD slot and upload credentials without command-line secrets."""

import argparse
import getpass
import json
import subprocess

from controller import Controller


def gh(*arguments, value=None):
    result = subprocess.run(
        ["gh", *arguments], input=value, text=True, capture_output=True
    )
    if result.returncode:
        raise RuntimeError(
            "GitHub configuration failed; check gh authentication and repository admin access"
        )
    return result.stdout


def protect(repository, environment, reviewer, branch):
    identity = json.loads(gh("api", "users/" + reviewer))
    payload = {
        "wait_timer": 0,
        "prevent_self_review": False,
        "reviewers": [{"type": "User", "id": identity["id"]}],
        "can_admins_bypass": False,
        "deployment_branch_policy": {
            "protected_branches": False,
            "custom_branch_policies": True,
        },
    }
    base = f"repos/{repository}/environments/{environment}"
    gh("api", "--method", "PUT", base, "--input", "-", value=json.dumps(payload))
    policies = json.loads(gh("api", base + "/deployment-branch-policies"))[
        "branch_policies"
    ]
    if not any(row["name"] == branch for row in policies):
        gh(
            "api",
            "--method",
            "POST",
            base + "/deployment-branch-policies",
            "--input",
            "-",
            value=json.dumps({"name": branch, "type": "branch"}),
        )
    observed = json.loads(gh("api", base))
    if not any(
        rule["type"] == "required_reviewers"
        and any(row["reviewer"]["id"] == identity["id"] for row in rule["reviewers"])
        for rule in observed["protection_rules"]
    ):
        raise RuntimeError("Required reviewer protection was not applied")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--environment", default="apd-integration")
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--branch", default="codex/apd-integration-ci")
    parser.add_argument("--protect-only", action="store_true")
    args = parser.parse_args()
    protect(args.repo, args.environment, args.reviewer, args.branch)
    if not args.protect_only:
        url = input("Fresh APD URL (https://...): ").strip()
        username = input("APD username: ").strip()
        password = getpass.getpass("APD password: ")
        Controller(url, username, password)  # Validate URL without connecting.
        if not username or not password:
            raise RuntimeError("Username and password are required")
        for name, value in (
            ("APD_URL", url),
            ("APD_USERNAME", username),
            ("APD_PASSWORD", password),
        ):
            gh(
                "secret",
                "set",
                name,
                "--repo",
                args.repo,
                "--env",
                args.environment,
                value=value,
            )
    print(
        "Protected APD environment configured. Review the candidate commit and image before approving its deployment."
    )


if __name__ == "__main__":
    main()
