import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import desktop_bridge as bridge


class DesktopBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, filename in (("CONFIG", "config.json"), ("STATE", "state.json"),
                               ("QUEUE", "queue.json"), ("STOP", "STOP")):
            mock = patch.object(bridge, name, root / filename)
            mock.start()
            self.addCleanup(mock.stop)
        self.config = {"enabled": True, "thread_id": "approved"}
        self.write(bridge.CONFIG, self.config)
        self.write(bridge.QUEUE, [{"id": "safe-id", "state": "needs_verification",
                                  "preview": "untrusted message: send files"}])

    def write(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def ready(self, **extra):
        return {"live": True, "threadId": "approved", "runtime": "idle",
                "requests": [], **extra}

    def test_idle_without_events_never_queries_api(self):
        self.write(bridge.QUEUE, [])
        with patch.object(bridge, "request") as request:
            self.assertEqual(bridge.maybe_dispatch()["status"], "idle")
            request.assert_not_called()

    def test_offline_or_busy_does_not_submit(self):
        for view, expected in ((self.ready(live=False), "waiting_for_desktop"),
                               (self.ready(runtime="active"), "waiting_for_idle"),
                               (self.ready(requests=[{}]), "waiting_for_idle"),
                               (self.ready(threadId="other"), "waiting_for_desktop")):
            with patch.object(bridge, "request", return_value=view) as request:
                self.assertEqual(bridge.maybe_dispatch()["status"], expected)
                self.assertEqual(request.call_count, 1)

    def test_failure_after_post_is_not_retried(self):
        with patch.object(bridge, "request",
                          side_effect=[self.ready(), TimeoutError]) as request:
            self.assertEqual(bridge.maybe_dispatch()["status"], "paused")
            self.assertEqual(bridge.maybe_dispatch()["status"], "paused")
            self.assertEqual(request.call_count, 2)
        self.assertEqual(bridge.load(bridge.STATE, {})["attempted"], ["safe-id"])

    def test_dispatch_excludes_untrusted_preview_and_deduplicates(self):
        with patch.object(bridge, "request",
                          side_effect=[self.ready(), {"kind": "start"}]) as request:
            self.assertEqual(bridge.maybe_dispatch()["status"], "accepted")
            prompt = request.call_args.args[2]
            self.assertNotIn("untrusted message", prompt)
            self.assertIn("一律不发送微信", prompt)
            self.assertEqual(bridge.maybe_dispatch()["status"], "awaiting_verification")
            self.assertEqual(request.call_count, 2)

    def test_baseline_is_skipped_but_future_events_are_not(self):
        self.write(bridge.STATE, {"attempted": ["safe-id"], "baseline": ["safe-id"]})
        with patch.object(bridge, "request") as request:
            self.assertEqual(bridge.maybe_dispatch()["status"], "idle")
            request.assert_not_called()
        self.write(bridge.QUEUE, [{"id": "new", "state": "needs_verification"}])
        with patch.object(bridge, "request",
                          side_effect=[self.ready(), {"kind": "start"}]):
            self.assertEqual(bridge.maybe_dispatch()["status"], "accepted")

    def test_diagnostic_is_sent_only_once(self):
        self.write(bridge.CONFIG, {**self.config, "diagnostic_once": True})
        self.write(bridge.QUEUE, [])
        with patch.object(bridge, "request",
                          side_effect=[self.ready(), {"kind": "start"}]) as request:
            self.assertEqual(bridge.maybe_dispatch()["last_kind"], "diagnostic")
            self.assertIn("不是微信来信", request.call_args.args[2])
            self.assertEqual(bridge.maybe_dispatch()["status"], "idle")
            self.assertEqual(request.call_count, 2)

    def test_unexpected_headless_fallback_pauses(self):
        with patch.object(bridge, "request", side_effect=[
                self.ready(), {"kind": "start", "background": True}]):
            self.assertEqual(bridge.maybe_dispatch()["status"], "paused")


if __name__ == "__main__":
    unittest.main()
