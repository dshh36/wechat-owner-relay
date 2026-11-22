import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import watcher


class ReplyLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, path in (("ROOT", self.root), ("QUEUE", self.root / "pending.json"),
                           ("STOP", self.root / "STOP")):
            p = patch.object(watcher, name, path)
            p.start()
            self.addCleanup(p.stop)
        watcher.save(self.root / "wechat_notifier_config.json", {
            "enabled": True, "owner_contact": "carry", "carry_replies": True, "carry_replies_since": "2026-11-20T04:00",
        })
        self.event = {"id": "event", "contact_candidate": "carry",
                      "state": "needs_verification", "detected_at": "2026-11-20T04:01"}
        watcher.save(watcher.QUEUE, [self.event])

    def test_register_then_finish_and_reject_duplicate_send(self):
        self.assertEqual(watcher.reply_transition("event")["state"], "reply_started")
        with self.assertRaises(ValueError):
            watcher.reply_transition("event")
        self.assertEqual(watcher.reply_transition("event", sent=True)["state"], "replied")
        with self.assertRaises(ValueError):
            watcher.reply_transition("event")

    def test_old_other_stopped_or_uncertain_events_cannot_begin_reply(self):
        for extra in ({"contact_candidate": "other"}, {"state": "presented"},
                      {"detected_at": "2026-11-20T03:00"}):
            watcher.save(watcher.QUEUE, [{**self.event, **extra}])
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                watcher.reply_transition("event")
        watcher.save(watcher.QUEUE, [self.event, {"id": "older", "state": "reply_started"}])
        with self.assertRaises(ValueError):
            watcher.reply_transition("event")
        watcher.save(watcher.QUEUE, [self.event])
        watcher.STOP.touch()
        with self.assertRaises(ValueError):
            watcher.reply_transition("event")

    def test_sent_requires_prior_registration(self):
        with self.assertRaises(ValueError):
            watcher.reply_transition("event", sent=True)


if __name__ == "__main__":
    unittest.main()
