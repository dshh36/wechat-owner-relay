import tempfile
from pathlib import Path
import unittest

from project_settings import candidate_command, owner_contact
from setup_project import initialize


class PublicationTests(unittest.TestCase):
    def test_setup_is_disabled_and_refuses_to_overwrite_existing_config(self):
        import json
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            initialize(root, "00000000-0000-0000-0000-000000000001", "example-owner")
            for name in ("clawbot_relay_config.json", "wechat_notifier_config.json",
                         "bridge_config.json", "desktop_bridge_config.json"):
                self.assertFalse(json.loads((root / name).read_text())["enabled"])
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            with self.assertRaises(FileExistsError):
                initialize(root, "00000000-0000-0000-0000-000000000001")
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})

    def test_setup_can_separate_native_binding_from_relay_reply_thread(self):
        import json
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            initialize(root, "00000000-0000-0000-0000-000000000001",
                       relay_thread_id="00000000-0000-0000-0000-000000000002")
            config = json.loads((root / "clawbot_relay_config.json").read_text())
            self.assertEqual(config["binding_thread_id"],
                             "00000000-0000-0000-0000-000000000001")
            self.assertEqual(config["thread_id"],
                             "00000000-0000-0000-0000-000000000002")
            self.assertFalse(config["enabled"])

    def test_owner_contact_is_configurable_without_personal_default(self):
        self.assertEqual(owner_contact({}), "owner")
        self.assertEqual(owner_contact({"owner_contact": "example"}), "example")

    def test_candidate_command_uses_current_interpreter(self):
        import sys
        self.assertIn(sys.executable.replace("'", "''"), candidate_command(["a" * 64]))
        self.assertIn("--events " + "a" * 64, candidate_command(["a" * 64]))


if __name__ == "__main__":
    unittest.main()
