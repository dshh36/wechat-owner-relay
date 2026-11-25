import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import local_monitor


class LocalMonitorTests(unittest.TestCase):
    def test_stop_file_interrupts_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            stop = Path(directory) / "STOP"
            stop.touch()
            with patch.object(local_monitor, "STOP", stop):
                self.assertTrue(local_monitor.wait_for_stop(20))

    def test_scan_timeout_pauses_monitor(self):
        with patch.object(local_monitor._scanner, "scan", return_value={
                "status": "paused", "reason": "Local scan timed out"}):
            self.assertEqual(local_monitor.scan_once()["status"], "paused")

    def test_scanner_identity_failure_is_not_retried_as_success(self):
        with patch.object(local_monitor._scanner, "scan",
                          return_value={"status": "paused", "reason": "Account changed"}):
            status = local_monitor.scan_once()
            self.assertEqual(status["status"], "paused")
            self.assertEqual(status["reason"], "Account changed")

    def run_clock(self, in_flight=False):
        clock = [0]

        def wait(seconds):
            clock[0] += seconds
            return clock[0] >= 7

        state = {"scans": 0}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(local_monitor, "STOP", Path(directory) / "STOP"), \
                patch.object(local_monitor.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(local_monitor, "wait_for_stop", side_effect=wait), \
                patch.object(local_monitor, "in_flight", return_value=in_flight), \
                patch.object(local_monitor, "scan_once",
                             return_value={"status": "scan_ok", "checked_at": "now"}) as scan, \
                patch.object(local_monitor, "notifier_tick",
                             return_value={"status": "idle"}) as tick, \
                patch.object(local_monitor, "maybe_dispatch", return_value={"status": "disabled"}), \
                patch.object(local_monitor, "desktop_dispatch", return_value={"status": "disabled"}):
            local_monitor.monitor_loop(5, 0, state, lambda *args, **kwargs: None, 1)
        return scan.call_count, tick.call_count

    def test_result_poll_does_not_wait_for_next_ocr_scan(self):
        self.assertEqual(self.run_clock(), (2, 7))

    def test_verification_turn_prevents_concurrent_scan_queue_writes(self):
        self.assertEqual(self.run_clock(in_flight=True), (0, 7))

    def test_window_resize_waits_then_recovers_without_dispatching_while_unreadable(self):
        state = {"scans": 0}
        records = []
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(local_monitor, "STOP", Path(directory) / "STOP"), \
                patch.object(local_monitor.time, "monotonic", side_effect=[0, 0, 5, 5]), \
                patch.object(local_monitor, "wait_for_stop", return_value=False), \
                patch.object(local_monitor, "in_flight", return_value=False), \
                patch.object(local_monitor, "scan_once", side_effect=[
                    {"status": "paused", "reason":
                     "Window session or size changed; recalibration required"},
                    {"status": "scan_ok", "checked_at": "now"},
                ]) as scan, \
                patch.object(local_monitor, "notifier_tick",
                             return_value={"status": "idle"}) as tick, \
                patch.object(local_monitor, "maybe_dispatch",
                             return_value={"status": "disabled"}) as dispatch, \
                patch.object(local_monitor, "desktop_dispatch",
                             return_value={"status": "disabled"}):
            local_monitor.monitor_loop(
                5, 1, state, lambda status, **fields: records.append((status, fields)))
        self.assertEqual(scan.call_count, 2)
        self.assertEqual(tick.call_count, 1)
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(records[0][0], "waiting_for_window")
        self.assertIsNone(records[1][1]["reason"])
        self.assertEqual(state["scans"], 1)

    def test_identity_change_still_stops_without_retry_or_notification(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(local_monitor, "STOP", Path(directory) / "STOP"), \
                patch.object(local_monitor, "in_flight", return_value=False), \
                patch.object(local_monitor, "scan_once", return_value={
                    "status": "paused", "reason": "Account avatar changed; identity verification required",
                }) as scan, \
                patch.object(local_monitor, "notifier_tick") as tick, \
                patch.object(local_monitor, "wait_for_stop") as wait:
            records = []
            local_monitor.monitor_loop(5, 0, {"scans": 0},
                                       lambda status, **fields: records.append(status))
        self.assertEqual(records, ["paused"])
        scan.assert_called_once()
        tick.assert_not_called()
        wait.assert_not_called()


if __name__ == "__main__":
    unittest.main()
