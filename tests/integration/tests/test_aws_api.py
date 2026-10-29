"""Guard EC2 instance placement compatibility with the supported AWS collection."""

from pathlib import Path
import unittest
import yaml


class InstancePlacementTests(unittest.TestCase):
    def test_every_ec2_instance_task_uses_placement_for_tenancy(self):
        root = Path(__file__).resolve().parents[3]
        paths = list(
            (root / "collections/ansible_collections/demo/cloud/roles").rglob("*.yml")
        )
        checked = []

        def visit(node, path):
            if isinstance(node, yaml.MappingNode):
                for key, value in node.value:
                    if key.value == "amazon.aws.ec2_instance" and isinstance(
                        value, yaml.MappingNode
                    ):
                        fields = {item.value: setting for item, setting in value.value}
                        self.assertNotIn(
                            "tenancy",
                            fields,
                            f"{path}:{key.start_mark.line+1} uses unsupported top-level tenancy",
                        )
                        if "placement" in fields:
                            placement = {
                                item.value for item, _ in fields["placement"].value
                            }
                            if "tenancy" in placement:
                                checked.append(path)
                    visit(value, path)
            elif isinstance(node, yaml.SequenceNode):
                for item in node.value:
                    visit(item, path)

        for path in paths:
            visit(yaml.compose(path.read_text()), path)
        self.assertGreaterEqual(len(checked), 5)


if __name__ == "__main__":
    unittest.main()
