import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import event_bridge as bridge


class BridgeTests(unittest.TestCase):
    def test_standalone_process_does_not_inherit_the_app_core_identity(self):
        with patch.dict(bridge.os.environ, {
            "CODEX_WINDOWS_REGISTERED_CORE": "app-core",
            "CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY": "app-family",
        }):
            env = bridge.standalone_environment()
            self.assertNotIn("CODEX_WINDOWS_REGISTERED_CORE", env)
            self.assertNotIn("CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY", env)
            self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
    def test_dedup_and_pending_confirmation_block_new_dispatch(self):
        queue = [{"id": "a", "state": "needs_verification"}]
        self.assertEqual(bridge.eligible_events(queue, {"attempted": []}), ["a"])
        self.assertEqual(bridge.eligible_events(queue, {"attempted": ["a"]}), [])
        queue.append({"id": "b", "state": "presented"})
        self.assertEqual(bridge.eligible_events(queue, {"attempted": []}), [])
        queue = [{"id": "a", "state": "needs_verification"},
                 {"id": "b", "state": "needs_verification"}]
        self.assertEqual(bridge.eligible_events(queue, {"attempted": ["a"]}), [])

    def test_preview_content_is_not_interpolated_into_command_or_prompt(self):
        config = {"codex": "codex.exe", "thread_id": "approved", "cwd": "."}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(bridge, "ROOT", Path(directory)), \
             patch.object(bridge.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 9)) as run:
            self.assertEqual(bridge.trigger(config, ["safe-event-id"]), 9)
            args, kwargs = run.call_args
            self.assertEqual(args[0][-1], "-")
            self.assertIn("--ephemeral", args[0])
            self.assertNotIn("resume", args[0])
            self.assertIn("一律不发送微信", kwargs["input"])
            self.assertEqual(args[0][3:5], ["--sandbox", "workspace-write"])

    def test_disabled_bridge_never_calls_model(self):
        with patch.object(bridge, "load", return_value={"enabled": False}), \
             patch.object(bridge, "trigger") as trigger:
            self.assertEqual(bridge.maybe_dispatch()["status"], "disabled")
            trigger.assert_not_called()

    def test_empty_queue_does_not_call_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            queue = root / "queue.json"
            config.write_text(json.dumps({
                "enabled": True, "session_path": "session.jsonl",
            }), encoding="utf-8")
            queue.write_text("[]", encoding="utf-8")
            with patch.object(bridge, "CONFIG", config), \
                 patch.object(bridge, "QUEUE", queue), \
                 patch.object(bridge, "BRIDGE_STATE", root / "state.json"), \
                 patch.object(bridge, "STOP", root / "STOP"), \
                 patch.object(bridge, "trigger") as trigger:
                self.assertEqual(bridge.maybe_dispatch()["status"], "idle")
                trigger.assert_not_called()

    def test_failed_dispatch_is_recorded_and_never_automatically_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            queue = root / "queue.json"
            config.write_text(json.dumps({
                "enabled": True, "session_path": "session.jsonl",
            }), encoding="utf-8")
            queue.write_text(json.dumps([
                {"id": "event", "state": "needs_verification"},
            ]), encoding="utf-8")
            with patch.object(bridge, "CONFIG", config), \
                 patch.object(bridge, "QUEUE", queue), \
                 patch.object(bridge, "BRIDGE_STATE", root / "state.json"), \
                 patch.object(bridge, "STOP", root / "STOP"), \
                 patch.object(bridge, "trigger", return_value=1) as trigger:
                self.assertEqual(bridge.maybe_dispatch()["status"], "paused")
                self.assertEqual(bridge.maybe_dispatch()["status"], "paused")
                trigger.assert_called_once()

    def test_successful_model_call_without_verified_event_is_not_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            queue = root / "queue.json"
            config.write_text(json.dumps({"enabled": True}), encoding="utf-8")
            queue.write_text(json.dumps([
                {"id": "event", "state": "needs_verification"},
            ]), encoding="utf-8")
            with patch.object(bridge, "CONFIG", config), \
                 patch.object(bridge, "QUEUE", queue), \
                 patch.object(bridge, "BRIDGE_STATE", root / "state.json"), \
                 patch.object(bridge, "STOP", root / "STOP"), \
                 patch.object(bridge, "trigger", return_value=0):
                result = bridge.maybe_dispatch()
                self.assertEqual(result["status"], "paused")
                self.assertIn("verification", result["reason"])


if __name__ == "__main__":
    unittest.main()
