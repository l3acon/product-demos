"""Exercise the reboot decision without rebooting any hosts."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = ROOT / "cloud/patch_rhel.yml"
SETUP = ROOT / "cloud/setup.yml"


class AnsibleSafeLoader(yaml.SafeLoader):
    pass


AnsibleSafeLoader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))


class PatchRebootTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tasks = yaml.safe_load(PLAYBOOK.read_text())[0]["tasks"]
        cls.reboot_task = next(t for t in tasks if t["name"] == "Reboot RHEL host when requested and required")

    def test_survey_defaults_to_false(self):
        data = yaml.load(SETUP.read_text(), Loader=AnsibleSafeLoader)
        workflow = next(w for w in data["controller_workflows"] if w["name"] == "Patch Cloud Stack in AWS")
        question = next(q for q in workflow["survey"]["spec"] if q["variable"] == "reboot")
        self.assertEqual(question["default"], "false")
        self.assertEqual(question["choices"], ["false", "true"])
        self.assertTrue(question["required"])

    def test_reboot_decision(self):
        cases = [
            ({"rhsm_registered": True, "reboot_required": True}, False),
            ({"rhsm_registered": True, "reboot_required": True, "reboot": "false"}, False),
            ({"rhsm_registered": True, "reboot_required": False, "reboot": "true"}, False),
            ({"rhsm_registered": False, "reboot_required": True, "reboot": "true"}, False),
            ({"rhsm_registered": True, "reboot_required": True, "reboot": "true"}, True),
            ({"rhsm_registered": True, "reboot_required": True, "reboot": True}, True),
        ]
        for variables, expected in cases:
            with self.subTest(variables=variables), tempfile.TemporaryDirectory(prefix="apd-reboot-test-") as tmp:
                play = [{
                    "hosts": "localhost", "connection": "local", "gather_facts": False,
                    "vars": variables,
                    "tasks": [
                        {"name": "Evaluate the actual reboot conditions", "ansible.builtin.debug": {"msg": "selected"},
                         "when": self.reboot_task["when"], "register": "decision"},
                        {"ansible.builtin.assert": {"that": [
                            "(decision is not skipped) == " + str(expected).lower()
                        ]}},
                    ],
                }]
                path = Path(tmp) / "test.yml"
                path.write_text(yaml.safe_dump(play, sort_keys=False))
                env = dict(os.environ, ANSIBLE_NOCOLOR="1")
                result = subprocess.run(
                    ["ansible-playbook", "-i", "localhost,", str(path)],
                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
