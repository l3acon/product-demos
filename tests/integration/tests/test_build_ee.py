"""Test image provenance and the boundaries between build secrets and APD access."""

import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from build_ee import (
    BuildError,
    TOKENS,
    build,
    check_hub_tokens,
    secure_galaxy_steps,
    validate_metadata,
)
from galaxy_cli import instrument


CONTAINERFILE = "\n".join(
    [
        "FROM example AS galaxy",
        *("ARG " + token for token in TOKENS),
        "RUN ansible-galaxy role install -r requirements.yml",
        "RUN ANSIBLE_GALAXY_DISABLE_GPG_VERIFY=1 ansible-galaxy collection install -r requirements.yml",
    ]
)


class BuildTests(unittest.TestCase):
    def test_galaxy_error_trace_omits_query_and_body_but_preserves_client_error(self):
        url = "https://sso.redhat.com/auth/token?secret=DO-NOT-PUBLISH"
        body = json.dumps(
            {"error": "invalid_grant", "error_description": "DO-NOT-PUBLISH"}
        ).encode()
        original = Mock(
            side_effect=HTTPError(url, 400, "Bad Request", {}, io.BytesIO(body))
        )
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(HTTPError) as raised:
            instrument(original)(url)
        self.assertEqual(raised.exception.read(), body)
        self.assertIn("invalid_grant", output.getvalue())
        self.assertNotIn("DO-NOT-PUBLISH", output.getvalue())

    def test_hub_authentication_reports_only_allowlisted_error_codes(self):
        response = Mock(status_code=400)
        response.json.return_value = {
            "error": "invalid_grant",
            "error_description": "DO-NOT-PUBLISH",
        }
        with patch("build_ee.requests.post", return_value=response):
            with self.assertRaises(BuildError) as raised:
                check_hub_tokens({TOKENS[0]: "offline.token.value"}, {"cases": []})
        self.assertIn("invalid_grant", str(raised.exception))
        self.assertNotIn("DO-NOT-PUBLISH", str(raised.exception))
        self.assertNotIn("offline.token.value", str(raised.exception))

    def test_hub_token_format_is_checked_before_authentication(self):
        with patch("build_ee.requests.post") as request:
            with self.assertRaisesRegex(BuildError, "without quotes"):
                check_hub_tokens({TOKENS[0]: '"pasted.token.value"'}, {"cases": []})
            request.assert_not_called()

    def test_hub_content_access_is_checked_after_successful_sso(self):
        token = Mock(status_code=200)
        token.json.return_value = {"access_token": "DO-NOT-PUBLISH"}
        report = {"cases": []}
        with patch("build_ee.requests.post", return_value=token), patch(
            "build_ee.requests.get", return_value=Mock(status_code=403)
        ):
            with self.assertRaisesRegex(BuildError, "content access failed: HTTP 403"):
                check_hub_tokens({TOKENS[0]: "offline.token.value"}, report)
        self.assertEqual(
            [case["status"] for case in report["cases"]], ["passed", "failed"]
        )
        self.assertNotIn("DO-NOT-PUBLISH", json.dumps(report))

    def test_every_galaxy_install_uses_required_secret_mounts(self):
        secured = secure_galaxy_steps(CONTAINERFILE)
        for token in TOKENS:
            self.assertNotIn("ARG " + token, secured)
            self.assertEqual(secured.count(f"id={token},required=true"), 2)
        self.assertEqual(secured.count("ansible-galaxy "), 2)

    def test_changed_builder_layout_fails_closed(self):
        with self.assertRaisesRegex(BuildError, "Unexpected"):
            secure_galaxy_steps("RUN echo no galaxy installation")

    def simulate(self, compatible):
        calls = []
        args = argparse.Namespace(
            repository="l3acon/product-demos", revision="a" * 40, run_id="123-1"
        )
        report = {"ee_build": {"candidate_commit": args.revision}, "cases": []}
        environment = {
            key: "secret-" + key
            for key in (
                *TOKENS,
                "GH_TOKEN",
                "REDHAT_REGISTRY_USERNAME",
                "REDHAT_REGISTRY_PASSWORD",
            )
        }
        environment["GITHUB_ACTOR"] = "l3acon"
        environment["REDHAT_REGISTRY_USERNAME"] = "registry-user"

        def execute(arguments, **kwargs):
            calls.append(arguments)
            self.assertFalse(
                any(
                    value in " ".join(arguments)
                    for value in environment.values()
                    if value.startswith("secret-")
                )
            )
            if arguments[:2] == ["skopeo", "inspect"]:
                return json.dumps({"Digest": "sha256:" + "b" * 64})
            if arguments[:2] == ["ansible-builder", "create"]:
                context = Path(arguments[arguments.index("--context") + 1])
                context.mkdir()
                (context / "Containerfile").write_text(CONTAINERFILE)
            if arguments[:2] == ["podman", "run"]:
                mounted = Path(arguments[arguments.index("--volume") + 1].split(":")[0])
                self.assertEqual(
                    {path.name for path in mounted.iterdir()},
                    {"probe_runtime.py", "requirements.yml"},
                )
                return json.dumps(
                    {
                        "compatible": compatible,
                        "core": "2.16.19",
                        "collections": [],
                        "tools": {},
                        "packages": {},
                        "secret": "DO-NOT-PUBLISH",
                    }
                )
            if arguments[:2] == ["podman", "push"]:
                Path(arguments[arguments.index("--digestfile") + 1]).write_text(
                    "sha256:" + "c" * 64
                )

        with patch("build_ee.check_hub_tokens"), patch.dict(
            "os.environ", environment, clear=True
        ), patch("build_ee.command", side_effect=execute), patch(
            "build_ee.client_rpm",
            return_value="https://mirror.openshift.com/client.rpm",
        ), patch(
            "build_ee.importlib.metadata.version", return_value="3.1.1"
        ):
            if compatible:
                image = build(args, report)
                self.assertEqual(
                    image, "ghcr.io/l3acon/product-demos/apd-ee-27@sha256:" + "c" * 64
                )
                with tempfile.TemporaryDirectory() as directory:
                    metadata = Path(directory) / "build-metadata.json"
                    metadata.write_text(json.dumps(report["ee_build"]))
                    self.assertEqual(
                        validate_metadata(metadata, args.revision, image)[
                            "base_image"
                        ].split("@")[1],
                        "sha256:" + "b" * 64,
                    )
            else:
                with self.assertRaisesRegex(BuildError, "incompatible"):
                    build(args, report)
        self.assertNotIn("DO-NOT-PUBLISH", json.dumps(report))
        return calls

    def test_source_build_is_probed_before_push_and_uses_pinned_base(self):
        calls = self.simulate(True)
        build_command = next(row for row in calls if row[:2] == ["podman", "build"])
        self.assertIn("--pull=always", build_command)
        self.assertTrue(
            any(
                value.startswith("EE_BASE_IMAGE=") and "@sha256:" in value
                for value in build_command
            )
        )
        self.assertLess(
            next(i for i, row in enumerate(calls) if row[:2] == ["podman", "run"]),
            next(i for i, row in enumerate(calls) if row[:2] == ["podman", "push"]),
        )

    def test_failed_probe_never_publishes_an_image(self):
        calls = self.simulate(False)
        self.assertFalse(any(row[:2] == ["podman", "push"] for row in calls))

    def test_metadata_for_another_revision_or_image_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metadata.json"
            path.write_text(
                json.dumps(
                    {
                        "mode": "external",
                        "candidate_commit": "a" * 40,
                        "image": "example@sha256:" + "b" * 64,
                    }
                )
            )
            with self.assertRaises(BuildError):
                validate_metadata(path, "c" * 40, "example@sha256:" + "b" * 64)
            with self.assertRaises(BuildError):
                validate_metadata(path, "a" * 40, "different@sha256:" + "b" * 64)


if __name__ == "__main__":
    unittest.main()
