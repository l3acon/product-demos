"""Build the source EE, validate it locally, and publish a digest for the APD gate."""

import argparse
import configparser
import hashlib
from html.parser import HTMLParser
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urljoin

import requests
import yaml

from report import render
from resolve_image import resolve
from galaxy_cli import token_kind


TOKENS = (
    "ANSIBLE_GALAXY_SERVER_CERTIFIED_TOKEN",
    "ANSIBLE_GALAXY_SERVER_VALIDATED_TOKEN",
)
RPM_DIRECTORY = "https://mirror.openshift.com/pub/openshift-v4/amd64/dependencies/rpms/4.18-el9-beta/"
HUB_AUTH_URL = (
    "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token"
)


class BuildError(RuntimeError):
    pass


def check_hub_tokens(tokens, report):
    config = configparser.ConfigParser(interpolation=None)
    config.read(
        Path(__file__).resolve().parents[2] / "execution_environments/ansible.cfg"
    )
    for name, token in tokens.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
            raise BuildError(
                f"{name} must contain the Automation Hub offline token only, without quotes or a Bearer prefix"
            )
        response = requests.post(
            HUB_AUTH_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": "cloud-services",
                "refresh_token": token,
            },
            timeout=60,
            allow_redirects=False,
        )
        try:
            result = response.json()
        except ValueError:
            result = {}
        if not isinstance(result, dict):
            result = {}
        if response.status_code != 200 or not isinstance(
            result.get("access_token"), str
        ):
            error = result.get("error")
            if error not in (
                "invalid_grant",
                "invalid_client",
                "invalid_request",
                "unauthorized_client",
            ):
                error = "unclassified_authentication_error"
            raise BuildError(
                f"Automation Hub rejected {name}: HTTP {response.status_code} ({error}). Set both Hub secrets to the same current offline token from console.redhat.com/ansible/automation-hub/token; no token values or raw response are reported."
            )
        report["cases"].append(
            {
                "name": name.replace(
                    "ANSIBLE_GALAXY_SERVER_", "Automation Hub "
                ).replace("_TOKEN", " authentication"),
                "status": "passed",
                "summary": "Supplied token type: " + token_kind(token),
            }
        )
        server = "certified" if "CERTIFIED" in name else "validated"
        url = config["galaxy_server." + server]["url"]
        response = requests.get(
            url,
            headers={"Authorization": "Bearer " + result["access_token"]},
            timeout=60,
            allow_redirects=False,
        )
        report["cases"].append(
            {
                "name": "Automation Hub " + server + " content access",
                "status": "passed" if response.status_code == 200 else "failed",
                "summary": f"Content API returned HTTP {response.status_code}; no response body recorded.",
            }
        )
        if response.status_code != 200:
            raise BuildError(
                f"Automation Hub {server} content access failed: HTTP {response.status_code}; SSO token refresh succeeded."
            )


def command(arguments, *, capture=False, stdin=None, cwd=None):
    try:
        result = subprocess.run(
            arguments,
            input=stdin,
            text=True,
            capture_output=capture,
            cwd=cwd,
            timeout=5400 if arguments[:2] == ["podman", "build"] else 300,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise BuildError(f"{arguments[0]} could not complete") from None
    if result.returncode:
        raise BuildError(
            f"{arguments[0]} failed with exit code {result.returncode}; inspect the build step"
        )
    return result.stdout if capture else None


def secure_galaxy_steps(containerfile):
    """Keep tokens in secret mounts; never substitute their values into build input."""
    lines, count = [], 0
    mounts = " ".join(
        f"--mount=type=secret,id={token},required=true" for token in TOKENS
    )
    environment = " ".join(f'{token}="$(cat /run/secrets/{token})"' for token in TOKENS)
    for line in containerfile.splitlines():
        if line in ["ARG " + token for token in TOKENS]:
            continue
        if line.startswith("RUN ") and "ansible-galaxy " in line:
            line = f"RUN {mounts} {environment} {line[4:]}"
            count += 1
        lines.append(line)
    if count != 2:
        raise BuildError(
            "Unexpected ansible-builder Galaxy steps; refusing to pass build secrets"
        )
    return "\n".join(lines) + "\n"


def client_rpm():
    class Links(HTMLParser):
        links = set()

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                for key, value in attrs:
                    if key == "href" and re.fullmatch(
                        r"openshift-clients-4[\w.+-]+\.x86_64\.rpm", value
                    ):
                        self.links.add(value)

    response = requests.get(RPM_DIRECTORY, timeout=60)
    response.raise_for_status()
    links = Links()
    links.feed(response.text)
    if len(links.links) != 1:
        raise BuildError(
            "OpenShift client RPM listing is ambiguous; update the build's RPM directory"
        )
    return urljoin(RPM_DIRECTORY, links.links.pop())


def validate_metadata(path, revision, image):
    """Accept only our small provenance schema before putting it into live reports."""
    path = Path(path)
    if path.stat().st_size > 16384:
        raise BuildError("EE build metadata is unexpectedly large")
    metadata = json.loads(path.read_text())
    if not isinstance(metadata, dict):
        raise BuildError("EE build metadata must be an object")
    allowed = {
        "mode",
        "candidate_commit",
        "image",
        "aap_series",
        "platform",
        "base_image",
        "definition_sha256",
        "requirements_sha256",
        "ansible_cfg_sha256",
        "containerfile_sha256",
        "builder_version",
        "openshift_client_rpm",
        "image_tag",
    }
    if (
        set(metadata) - allowed
        or metadata.get("candidate_commit") != revision
        or metadata.get("image") != image
        or metadata.get("mode") not in ("built", "external")
        or any(
            not isinstance(value, str) or len(value) > 1024
            for value in metadata.values()
        )
    ):
        raise BuildError(
            "EE build metadata does not match the selected revision and image"
        )
    if metadata["mode"] == "built":
        if (
            metadata.get("aap_series") != "27"
            or metadata.get("platform") != "linux/amd64"
        ):
            raise BuildError("Unsupported EE build platform or AAP series")
        for key in (
            "definition_sha256",
            "requirements_sha256",
            "ansible_cfg_sha256",
            "containerfile_sha256",
        ):
            if not re.fullmatch(r"[a-f0-9]{64}", metadata.get(key, "")):
                raise BuildError("EE build source checksum is missing or invalid")
        if not re.fullmatch(
            r"[^\s@]+@sha256:[a-f0-9]{64}", metadata.get("base_image", "")
        ):
            raise BuildError("EE build has no immutable base image")
    return metadata


def build(args, report):
    root = Path(__file__).resolve().parents[2]
    source = root / "execution_environments"
    definition = source / "apd-ee-27.yml"
    required = (
        *TOKENS,
        "REDHAT_REGISTRY_USERNAME",
        "REDHAT_REGISTRY_PASSWORD",
        "GH_TOKEN",
        "GITHUB_ACTOR",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise BuildError(
            "Missing EE build credentials: "
            + ", ".join(missing)
            + ". Configure repository secrets before retrying."
        )
    tokens = {name: os.environ[name].strip() for name in TOKENS}
    check_hub_tokens(tokens, report)
    metadata = report["ee_build"]
    tag = (
        f"ghcr.io/{args.repository.lower()}/apd-ee-27:ci-{args.revision}-{args.run_id}"
    )
    metadata.update(
        mode="built",
        image_tag=tag,
        aap_series="27",
        platform="linux/amd64",
        builder_version=importlib.metadata.version("ansible-builder"),
    )
    for name, key in (
        ("apd-ee-27.yml", "definition_sha256"),
        ("requirements-27.yml", "requirements_sha256"),
        ("ansible.cfg", "ansible_cfg_sha256"),
    ):
        metadata[key] = hashlib.sha256((source / name).read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="apd-ee-") as directory:
        private = Path(directory)
        authfile = str(private / "auth.json")
        for registry, username, password in (
            (
                "registry.redhat.io",
                os.environ["REDHAT_REGISTRY_USERNAME"],
                os.environ["REDHAT_REGISTRY_PASSWORD"],
            ),
            ("ghcr.io", os.environ["GITHUB_ACTOR"], os.environ["GH_TOKEN"]),
        ):
            command(
                [
                    "podman",
                    "login",
                    "--authfile",
                    authfile,
                    "--username",
                    username,
                    "--password-stdin",
                    registry,
                ],
                capture=True,
                stdin=password + "\n",
            )
        base = yaml.safe_load(definition.read_text())["images"]["base_image"]["name"]
        inspected = json.loads(
            command(
                [
                    "skopeo",
                    "inspect",
                    "--authfile",
                    authfile,
                    "--no-tags",
                    "docker://" + base,
                ],
                capture=True,
            )
        )
        digest = inspected["Digest"]
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise BuildError("Base image registry returned an invalid digest")
        base = base.rsplit(":", 1)[0] + "@" + digest
        metadata["base_image"] = base
        metadata["openshift_client_rpm"] = client_rpm()
        context = private / "context"
        command(
            [
                "ansible-builder",
                "create",
                "--file",
                definition.name,
                "--context",
                str(context),
            ],
            cwd=source,
        )
        containerfile = context / "Containerfile"
        secured = secure_galaxy_steps(containerfile.read_text())
        (context / "_build").mkdir(exist_ok=True)
        shutil.copyfile(
            root / "tests/integration/galaxy_cli.py", context / "_build/ci_galaxy.py"
        )
        containerfile.write_text(
            secured.replace("ansible-galaxy ", "$PYCMD /build/ci_galaxy.py ")
        )
        metadata["containerfile_sha256"] = hashlib.sha256(
            containerfile.read_bytes()
        ).hexdigest()
        secrets = []
        for token in TOKENS:
            secret = private / token
            secret.write_text(tokens[token])
            secret.chmod(0o600)
            secrets += ["--secret", f"id={token},src={secret}"]
        command(
            [
                "podman",
                "build",
                "--authfile",
                authfile,
                "--platform",
                "linux/amd64",
                "--pull=always",
                "--tag",
                tag,
                "--build-arg",
                "EE_BASE_IMAGE=" + base,
                "--build-arg",
                "OPENSHIFT_CLIENT_RPM=" + metadata["openshift_client_rpm"],
                "--label",
                "org.opencontainers.image.source=https://github.com/" + args.repository,
                "--label",
                "org.opencontainers.image.revision=" + args.revision,
                *secrets,
                str(context),
            ]
        )
        report["cases"].append({"name": "Build source EE", "status": "passed"})
        # Only the probe and source constraints are mounted; no build credentials enter the container.
        probe = private / "probe"
        probe.mkdir()
        shutil.copyfile(
            root / "tests/integration/probe_runtime.py", probe / "probe_runtime.py"
        )
        shutil.copyfile(source / "requirements-27.yml", probe / "requirements.yml")
        runtime = json.loads(
            command(
                [
                    "podman",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "--cap-drop",
                    "all",
                    "--security-opt",
                    "no-new-privileges",
                    "--tmpfs",
                    "/tmp",
                    "--tmpfs",
                    "/runner",
                    "--tmpfs",
                    "/home/runner",
                    "--volume",
                    f"{probe}:/ci:ro,Z",
                    "--entrypoint",
                    "/usr/bin/python3.12",
                    tag,
                    "/ci/probe_runtime.py",
                    "/ci/requirements.yml",
                ],
                capture=True,
            )
        )
        # Use the same allowlist as live Controller diagnostics, before publishing any artifacts.
        from run import public_runtime

        report["runtime"] = public_runtime(runtime)
        compatible = runtime.get("compatible") is True
        report["cases"].append(
            {
                "name": "Built EE runtime compatibility",
                "status": "passed" if compatible else "failed",
                "summary": "Source constraints, collection/core compatibility, Python packages and executable tools checked.",
            }
        )
        if not compatible:
            raise BuildError(
                "Built EE is incompatible; inspect the EE build report. APD was not accessed."
            )
        digestfile = private / "digest"
        command(
            [
                "podman",
                "push",
                "--authfile",
                authfile,
                "--digestfile",
                str(digestfile),
                tag,
            ]
        )
        digest = digestfile.read_text().strip()
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise BuildError("Published image has no valid digest")
        metadata["image"] = tag.rsplit(":", 1)[0] + "@" + digest
        report["cases"].append({"name": "Publish candidate EE", "status": "passed"})
    return metadata["image"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--external-image", default="")
    parser.add_argument(
        "--output", type=Path, default=Path("integration-results/ee-build")
    )
    args = parser.parse_args()
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository)
        or not re.fullmatch(r"[a-f0-9]{40}", args.revision)
        or not re.fullmatch(r"[0-9]+-[0-9]+", args.run_id)
    ):
        parser.error(
            "A GitHub repository, immutable commit and numeric run-attempt are required"
        )
    report = {
        "schema_version": 1,
        "sample": False,
        "scope": "EE build and local amd64 validation only. No APD jobs or infrastructure launched.",
        "metadata": {"candidate_commit": args.revision},
        "cases": [],
        "runtime": {},
        "ee_build": {"candidate_commit": args.revision},
    }
    try:
        if args.external_image:
            image = resolve(args.external_image)
            report["ee_build"].update(mode="external", image=image)
            report["cases"].append(
                {
                    "name": "Resolve externally built EE",
                    "status": "passed",
                    "summary": "Explicit image override; no source build provenance claimed. Runtime is checked in APD after approval.",
                }
            )
        else:
            image = build(args, report)
        report["metadata"]["candidate_image"] = image
        args.output.mkdir(parents=True, exist_ok=True)
        metadatafile = args.output / "build-metadata.json"
        metadatafile.write_text(json.dumps(report["ee_build"], indent=2) + "\n")
        validate_metadata(metadatafile, args.revision, image)
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                output.write(
                    f"image={image}\nbuilt={str(not bool(args.external_image)).lower()}\n"
                )
        return 0
    except (BuildError, ValueError, KeyError, requests.RequestException) as error:
        message = (
            str(error)
            if isinstance(error, (BuildError, ValueError))
            else "EE build metadata, authentication or download request failed; inspect the build step"
        )
        report["cases"].append(
            {"name": "Candidate EE preparation", "status": "failed", "summary": message}
        )
        print(message, flush=True)
        return 1
    finally:
        render(report, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
