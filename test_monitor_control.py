from contextlib import nullcontext
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

import monitor_control as control


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name in ("STOP", "MONITOR_STATUS", "NOTIFIER_STATE", "CONTROL_LOCK"):
            p = patch.object(control, name, root / name)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(control.notifier, "QUEUE", root / "QUEUE")
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(control, "file_lock", return_value=nullcontext())
        p.start()
        self.addCleanup(p.stop)

    def test_off_only_signals_detector_and_waits(self):
        with patch.object(control, "wait_until_stopped") as wait, \
                patch.object(control, "monitor_running", return_value=False), \
                patch.object(control.subprocess, "Popen") as launch:
            result = control.set_enabled(False)
        self.assertTrue(control.STOP.exists())
        self.assertFalse(result["enabled"])
        wait.assert_called_once()
        launch.assert_not_called()
        self.assertEqual(sorted(p.name for p in Path(self.temp.name).iterdir()), ["STOP"])

    def test_on_does_not_launch_duplicate_running_monitor(self):
        with patch.object(control, "monitor_running", return_value=True), \
                patch.object(control.subprocess, "Popen") as launch:
            self.assertTrue(control.set_enabled(True)["enabled"])
            launch.assert_not_called()

    def test_on_starts_only_detector_and_retains_existing_state(self):
        control.STOP.touch()
        control.NOTIFIER_STATE.write_text('{"status":"paused"}', encoding="utf-8")
        with patch.object(control, "wait_until_stopped") as wait, \
                patch.object(control, "monitor_running", side_effect=[True, True]), \
                patch.object(control.subprocess, "Popen") as launch:
            result = control.set_enabled(True)
        wait.assert_called_once()
        self.assertFalse(control.STOP.exists())
        self.assertTrue(result["enabled"])
        self.assertEqual(result["notifier_status"], "paused")
        self.assertIn("local_monitor.py", launch.call_args.args[0][1])
        self.assertNotIn("clawbot_relay", str(launch.call_args))

    def test_stop_timeout_preserves_stop_request_and_never_relaunches(self):
        control.STOP.touch()
        with patch.object(control, "wait_until_stopped", side_effect=TimeoutError), \
                patch.object(control.subprocess, "Popen") as launch:
            with self.assertRaises(TimeoutError):
                control.set_enabled(True)
            launch.assert_not_called()
        self.assertTrue(control.STOP.exists())

    def test_stale_pid_does_not_count_as_running(self):
        control.MONITOR_STATUS.write_text('{"pid":1234}', encoding="utf-8")
        with patch.object(control, "monitor_running", return_value=False):
            result = control.status()
        self.assertFalse(result["enabled"])
        self.assertIsNone(result["pid"])

    def test_status_preserves_waiting_and_fatal_pause_reasons(self):
        for running, phase in ((True, "waiting_for_window"), (False, "paused")):
            with self.subTest(phase=phase):
                control.MONITOR_STATUS.write_text(json.dumps({
                    "status": phase, "reason": "explanation", "pid": 12,
                }), encoding="utf-8")
                with patch.object(control, "monitor_running", return_value=running):
                    result = control.status()
                self.assertEqual(result["status"], phase)
                self.assertEqual(result["enabled"], running)
                self.assertEqual(result["reason"], "explanation")
                self.assertNotEqual(control.detector_label(result), "已关闭")

    def test_explicit_stop_is_off_even_after_failure(self):
        control.STOP.touch()
        control.MONITOR_STATUS.write_text('{"status":"paused"}', encoding="utf-8")
        with patch.object(control, "monitor_running", return_value=False):
            self.assertEqual(control.status()["status"], "off")

    def test_notification_recovery_is_available_only_for_verified_pause_state(self):
        eligible = {
            "status": "paused", "last_result": "verification_unavailable",
            "notification_accepted_at": "then", "reply_seen_at": "then",
        }
        control.NOTIFIER_STATE.write_text(json.dumps(eligible), encoding="utf-8")
        control.notifier.QUEUE.write_text("[]", encoding="utf-8")
        self.assertTrue(control.notification_resume_available())

        for state, queue in (
            ({**eligible, "last_result": "send_uncertain"}, []),
            ({**eligible, "notification_accepted_at": None}, []),
            (eligible, [{"state": "reply_started"}]),
        ):
            control.NOTIFIER_STATE.write_text(json.dumps(state), encoding="utf-8")
            control.notifier.QUEUE.write_text(json.dumps(queue), encoding="utf-8")
            self.assertFalse(control.notification_resume_available())

    def test_resume_notifications_calls_guarded_recovery(self):
        control.NOTIFIER_STATE.write_text(json.dumps({
            "status": "paused", "last_result": "verification_unavailable",
            "notification_accepted_at": "then", "reply_seen_at": "then",
        }), encoding="utf-8")
        control.notifier.QUEUE.write_text("[]", encoding="utf-8")
        with patch.object(control.notifier, "resume_after_verification_failure",
                          return_value={"status": "enabled"}) as resume:
            self.assertEqual(
                control.resume_notifications(), {"status": "enabled"})
        resume.assert_called_once_with()

    def test_resume_notifications_refuses_uncertain_state(self):
        control.NOTIFIER_STATE.write_text(json.dumps({
            "status": "paused", "last_result": "send_uncertain",
        }), encoding="utf-8")
        control.notifier.QUEUE.write_text("[]", encoding="utf-8")
        with patch.object(control.notifier, "resume_after_verification_failure") as resume:
            with self.assertRaises(control.ControlError):
                control.resume_notifications()
        resume.assert_not_called()


class RelayControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for owner, names in (
            (control, ("CONTROL_LOCK", "STOP")),
            (control.relay, ("STOP", "LOCK", "STATUS", "CONFIG", "STATE")),
        ):
            for name in names:
                p = patch.object(owner, name, root / (
                    "relay_" + name if owner is control.relay else name))
                p.start()
                self.addCleanup(p.stop)
        for p in (
            patch.object(control, "file_lock", return_value=nullcontext()),
            patch.object(control.relay, "binding",
                         return_value=({}, {}, "account", "peer", "identity")),
            patch.object(control.relay, "ClawBotClient"),
            patch.object(control.relay, "desktop_request", return_value={
                "threadId": "thread", "live": True, "runtime": "idle"}),
        ):
            p.start()
            self.addCleanup(p.stop)
        control.relay.CONFIG.write_text(
            '{"enabled":true,"thread_id":"thread"}', encoding="utf-8")
        control.relay.STATE.write_text(json.dumps({
            "binding_fingerprint": "identity",
            "events": {"old": {"stage": "delivered"}},
        }), encoding="utf-8")

    def test_busy_off_and_reconnect_leave_process_untouched(self):
        for enabled, reconnect in ((False, False), (True, True)):
            with self.subTest(enabled=enabled), \
                    patch.object(control, "relay_status", return_value={
                        "busy": True, "running": True, "enabled": True}), \
                    patch.object(control.subprocess, "Popen") as launch:
                with self.assertRaises(control.ControlError):
                    control.set_relay_enabled(enabled, reconnect)
                self.assertFalse(control.relay.STOP.exists())
                launch.assert_not_called()

    def test_on_already_enabled_is_noop_even_during_reply(self):
        with patch.object(control, "relay_status", return_value={
                "busy": True, "running": True, "enabled": True}), \
                patch.object(control, "validate_relay_start") as validate, \
                patch.object(control.subprocess, "Popen") as launch:
            self.assertTrue(control.set_relay_enabled(True)["enabled"])
            validate.assert_not_called()
            launch.assert_not_called()

    def test_changed_binding_refused_before_stop_or_launch(self):
        states = (
            {"binding_fingerprint": "other", "events": {}},
        )
        for state in states:
            control.relay.STATE.write_text(json.dumps(state), encoding="utf-8")
            with self.subTest(state=state), \
                    patch.object(control, "relay_status", return_value={
                        "busy": False, "running": True, "enabled": True}), \
                    patch.object(control.subprocess, "Popen") as launch:
                with self.assertRaises(control.ControlError):
                    control.set_relay_enabled(True, reconnect=True)
                self.assertFalse(control.relay.STOP.exists())
                launch.assert_not_called()

    def test_reconnect_abandons_old_replies_without_sending_or_resetting_cursor(self):
        for stage in ("before_post", "awaiting_reply", "before_send"):
            with self.subTest(stage=stage):
                control.relay.STATE.write_text(json.dumps({
                    "binding_fingerprint": "identity", "cursor": "keep",
                    "native_processed_keys": ["keep-key"],
                    "events": {
                        "pending": {"stage": stage, "marker": "keep-marker", "chunk": 0},
                        "done": {"stage": "delivered"},
                    },
                }), encoding="utf-8")
                with patch.object(control, "relay_status", side_effect=[
                        {"busy": False, "running": False, "enabled": False},
                        {"busy": False, "running": True, "enabled": True}]), \
                        patch.object(control, "process_running", side_effect=[False, True]), \
                        patch.object(control.subprocess, "Popen") as launch:
                    result = control.set_relay_enabled(True, reconnect=True)
                state = json.loads(control.relay.STATE.read_text(encoding="utf-8"))
                self.assertTrue(result["enabled"])
                self.assertEqual(state["cursor"], "keep")
                self.assertEqual(state["native_processed_keys"], ["keep-key"])
                self.assertEqual(state["events"]["done"]["stage"], "delivered")
                event = state["events"]["pending"]
                self.assertEqual(event["stage"], "discarded")
                self.assertEqual(event["discarded_from_stage"], stage)
                self.assertEqual(event["marker"], "keep-marker")
                self.assertEqual(event["chunk"], 0)
                launch.assert_called_once()
                control.relay.ClawBotClient.return_value.send.assert_not_called()
                control.relay.ClawBotClient.return_value.updates.assert_not_called()

    def test_reconnect_stops_idle_writer_before_discard(self):
        order = []
        with patch.object(control, "relay_status", return_value={
                "busy": False, "running": True, "enabled": True}), \
                patch.object(control, "stop_relay", side_effect=lambda: order.append("stop")), \
                patch.object(control, "validate_relay_start",
                             side_effect=lambda **kw: order.append(("validate", kw))), \
                patch.object(control, "process_running", return_value=True), \
                patch.object(control.subprocess, "Popen"):
            control.set_relay_enabled(True, reconnect=True)
        self.assertEqual(order, ["stop", ("validate", {"discard_pending": True})])

    def test_unavailable_desktop_preserves_pending_before_stop_or_discard(self):
        control.relay.STATE.write_text(json.dumps({
            "binding_fingerprint": "identity", "cursor": "keep",
            "events": {"pending": {"stage": "before_send"}},
        }), encoding="utf-8")
        before = control.relay.STATE.read_bytes()
        for view in (
            {"threadId": "thread", "live": False, "runtime": "notLoaded"},
            {"threadId": "other", "live": True, "runtime": "idle"},
        ):
            with self.subTest(view=view), \
                    patch.object(control, "relay_status", return_value={
                        "busy": False, "running": True, "enabled": True}), \
                    patch.object(control.relay, "desktop_request", return_value=view), \
                    patch.object(control, "stop_relay") as stop, \
                    patch.object(control.subprocess, "Popen") as launch:
                with self.assertRaises(control.ControlError):
                    control.set_relay_enabled(True, reconnect=True)
                stop.assert_not_called()
                launch.assert_not_called()
            self.assertEqual(control.relay.STATE.read_bytes(), before)
            self.assertFalse(control.relay.STOP.exists())

    def test_paused_relay_preserves_failure_reason_after_exit(self):
        control.relay.STATUS.write_text(json.dumps({
            "status": "paused", "reason": "Desktop connection unavailable",
        }), encoding="utf-8")
        with patch.object(control, "process_running", return_value=False):
            current = control.relay_status()
        self.assertEqual(current["status"], "paused")
        self.assertIn("Desktop connection unavailable", control.relay_label(current))
        self.assertEqual(control.relay_label({
            "status": "waiting_for_desktop_connection"}), "桌面会话通信未连接")

    def test_discard_on_reconnect_refuses_active_writer_or_unknown_stage(self):
        for running, stage in ((True, "before_send"), (False, "unknown")):
            control.relay.STATE.write_text(json.dumps({
                "binding_fingerprint": "identity",
                "events": {"pending": {"stage": stage}},
            }), encoding="utf-8")
            before = control.relay.STATE.read_bytes()
            with self.subTest(running=running, stage=stage), \
                    patch.object(control, "process_running", return_value=running):
                with self.assertRaises(control.ControlError):
                    control.validate_relay_start(discard_pending=True)
            self.assertEqual(control.relay.STATE.read_bytes(), before)

    def test_normal_start_still_refuses_uncertain_send(self):
        control.relay.STATE.write_text(json.dumps({
            "binding_fingerprint": "identity",
            "events": {"pending": {"stage": "before_send"}},
        }), encoding="utf-8")
        with self.assertRaises(control.ControlError):
            control.validate_relay_start()

    def test_native_receiver_prevents_start(self):
        with patch.object(control.relay, "binding", return_value=(
                {"weixinConnectEnabled": True}, {}, "account", "peer", "identity")):
            with self.assertRaises(control.ControlError):
                control.validate_relay_start()

    def test_discard_marks_only_the_single_waiting_reply_and_preserves_cursor(self):
        control.relay.STATE.write_text(json.dumps({
            "binding_fingerprint": "identity",
            "cursor": "keep-this-cursor",
            "native_processed_keys": ["keep-this-key"],
            "events": {
                "pending": {"stage": "awaiting_reply", "marker": "[marker]"},
                "done": {"stage": "delivered"},
            },
        }), encoding="utf-8")
        with patch.object(control, "relay_status", return_value={
                "running": False, "enabled": False, "busy": False}), \
                patch.object(control.relay, "binding", return_value=(
                    {"weixinConnectEnabled": False}, {}, "account", "peer", "identity")):
            result = control.discard_relay_event("pending")

        state = json.loads(control.relay.STATE.read_text(encoding="utf-8"))
        self.assertEqual(result["remaining_pending"], 0)
        self.assertEqual(state["events"]["pending"]["stage"], "discarded")
        self.assertEqual(state["events"]["done"]["stage"], "delivered")
        self.assertEqual(state["cursor"], "keep-this-cursor")
        self.assertEqual(state["native_processed_keys"], ["keep-this-key"])

    def test_discard_refuses_uncertain_send_or_multiple_pending_events(self):
        for events in (
            {"pending": {"stage": "before_send"}},
            {"pending": {"stage": "awaiting_reply"},
             "other": {"stage": "awaiting_reply"}},
        ):
            control.relay.STATE.write_text(json.dumps({
                "binding_fingerprint": "identity", "events": events,
            }), encoding="utf-8")
            before = control.relay.STATE.read_bytes()
            with patch.object(control, "relay_status", return_value={
                    "running": False, "enabled": False, "busy": False}), \
                    patch.object(control.relay, "binding", return_value=(
                        {"weixinConnectEnabled": False}, {}, "account", "peer", "identity")):
                with self.assertRaises(control.ControlError):
                    control.discard_relay_event("pending")
            self.assertEqual(control.relay.STATE.read_bytes(), before)

    def test_off_only_sets_relay_stop_preserves_ledger(self):
        before = control.relay.STATE.read_bytes()
        with patch.object(control, "process_running", return_value=False):
            self.assertFalse(control.set_relay_enabled(False)["enabled"])
        self.assertTrue(control.relay.STOP.exists())
        self.assertFalse(control.STOP.exists())
        self.assertEqual(before, control.relay.STATE.read_bytes())

    def test_on_starts_existing_relay_preserves_ledger(self):
        before = control.relay.STATE.read_bytes()
        control.relay.STOP.touch()
        with patch.object(control, "process_running", side_effect=[False, True, True]), \
                patch.object(control.subprocess, "Popen") as launch:
            self.assertTrue(control.set_relay_enabled(True)["enabled"])
        self.assertFalse(control.relay.STOP.exists())
        self.assertFalse(control.STOP.exists())
        self.assertEqual(before, control.relay.STATE.read_bytes())
        self.assertTrue(launch.call_args.args[0][1].endswith("clawbot_relay.py"))
        self.assertEqual(launch.call_args.args[0][2:], ["--run"])
        launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
