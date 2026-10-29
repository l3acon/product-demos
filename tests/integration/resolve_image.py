"""Resolve a registry reference once; emit a digest for the approved live job."""

import argparse
import json
import os
import re
import subprocess


def resolve(reference):
    if (
        not re.fullmatch(r"[a-z0-9][a-z0-9./_:@-]+", reference)
        or reference.count("@") > 1
    ):
        raise ValueError(
            "Supply a registry image reference without credentials or whitespace"
        )
    result = subprocess.run(
        ["skopeo", "inspect", "--no-tags", "docker://" + reference],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode:
        raise ValueError(
            "Candidate image could not be resolved; verify registry access"
        )
    digest = json.loads(result.stdout)["Digest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Registry returned an invalid image digest")
    repository = reference.split("@")[0]
    if ":" in repository.rsplit("/", 1)[-1]:
        repository = repository.rsplit(":", 1)[0]
    return repository + "@" + digest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference")
    args = parser.parse_args()
    image = resolve(args.reference)
    print(image)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write("image=" + image + "\n")
