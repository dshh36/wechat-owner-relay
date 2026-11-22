import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import watcher
from watcher import (
    avatar_matches, fresh_candidates, parse_previews, preview_key, rebase_previews,
    selected_events,
)


def line(text, x, y, score=0.99):
    return {"text": text, "x": x, "y": y, "score": score}


class WatcherTests(unittest.TestCase):
    def test_save_retries_transient_permission_error(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "status.json"
            target.write_text('{"status":"old"}', encoding="utf-8")
            replace = watcher.os.replace
            attempts = 0

            def transient_lock(source, destination):
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise PermissionError(5, "temporarily locked")
                replace(source, destination)

            with patch.object(watcher.os, "replace", side_effect=transient_lock), \
                    patch.object(watcher.time, "sleep") as sleep:
                watcher.save(target, {"status": "running"})

            self.assertEqual(watcher.load(target, {}), {"status": "running"})
            self.assertEqual(attempts, 3)
            self.assertEqual(sleep.call_count, 2)

    def test_save_retries_permission_error_beyond_short_lock_window(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "status.json"
            target.write_text('{"status":"old"}', encoding="utf-8")
            replace = watcher.os.replace
            attempts = 0

            def delayed_unlock(source, destination):
                nonlocal attempts
                attempts += 1
                if attempts < 7:
                    raise PermissionError(5, "temporarily locked")
                replace(source, destination)

            with patch.object(watcher.os, "replace", side_effect=delayed_unlock), \
                    patch.object(watcher.time, "sleep") as sleep:
                watcher.save(target, {"status": "running"})

            self.assertEqual(watcher.load(target, {}), {"status": "running"})
            self.assertEqual(attempts, 7)
            self.assertEqual(sleep.call_count, 6)

    def test_only_sidebar_names_and_previews_are_candidates(self):
        lines = [
            line("friend", 140, 100), line("23:40", 274, 100),
            line("hello", 160, 122), line("unrelated main chat", 510, 122),
            line("send button", 830, 605),
        ]
        items = parse_previews(lines, 882, 641, 1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["contact_candidate"], "friend")
        self.assertEqual(items[0]["preview"], "hello")
        self.assertEqual(items[0]["direction"], "unknown")
        self.assertTrue(items[0]["needs_visual_verification"])

    def test_baseline_and_repeated_scan_do_not_requeue_history(self):
        first = {"contact_candidate": "friend", "preview": "old", "displayed_time": "10:00"}
        new = {**first, "preview": "new", "displayed_time": "10:01"}
        state = {"seen": [preview_key(first)]}
        self.assertEqual(fresh_candidates([first], state), [])
        self.assertEqual(fresh_candidates([new], state), [new])
        self.assertEqual(fresh_candidates([new, first], state), [])

    def test_low_confidence_contact_is_not_actionable(self):
        self.assertEqual(
            parse_previews([line("uncertain", 130, 100, 0.6), line("hi", 130, 122)],
                           882, 641, 1), [],
        )

    def test_midnight_relabel_does_not_create_new_message(self):
        item = {"contact_candidate": "friend", "preview": "hello",
                "displayed_time": "23:44"}
        state = {"seen": [preview_key(item)]}
        self.assertEqual(fresh_candidates([item], state), [])
        yesterday = {**item, "displayed_time": "昨天 23:44"}
        self.assertEqual(fresh_candidates([yesterday], state), [])
        self.assertEqual(fresh_candidates([yesterday], state), [])

    def test_identical_text_at_new_clock_time_remains_candidate(self):
        item = {"contact_candidate": "friend", "preview": "hello",
                "displayed_time": "23:44"}
        state = {"seen": [preview_key(item)]}
        fresh_candidates([item], state)
        new = {**item, "displayed_time": "00:10"}
        self.assertEqual(fresh_candidates([new], state), [new])

    def test_ellipsis_ocr_jitter_does_not_wake_model_again(self):
        old = {"contact_candidate": "friend", "preview": "clipped...",
               "displayed_time": "01:45"}
        state = {"seen": [preview_key(old)]}
        fresh_candidates([old], state)
        self.assertEqual(fresh_candidates([{**old, "preview": "clipped.."}], state), [])
        self.assertEqual(fresh_candidates([{**old, "preview": "clipped…"}], state), [])
        changed = {**old, "preview": "different.."}
        self.assertEqual(fresh_candidates([changed], state), [changed])

    def test_ellipsis_repeat_at_new_time_still_triggers(self):
        old = {"contact_candidate": "friend", "preview": "clipped...",
               "displayed_time": "01:45"}
        state = {"seen": [preview_key(old)]}
        fresh_candidates([old], state)
        new = {**old, "preview": "clipped..", "displayed_time": "01:46"}
        self.assertEqual(fresh_candidates([new], state), [new])

    def test_rebound_rebases_visible_sidebar_without_creating_candidates(self):
        visible = {"contact_candidate": "friend", "preview": "current",
                   "displayed_time": "10:02"}
        state = {"seen": [preview_key({
            "contact_candidate": "friend", "preview": "old",
            "displayed_time": "10:00",
        })]}
        rebase_previews([visible], state)
        self.assertEqual(fresh_candidates([visible], state), [])
        self.assertEqual(state["last_previews"]["friend"]["preview"], "current")

    def test_event_query_never_returns_other_contacts_history(self):
        queue = [{"id": "a", "preview": "requested"}, {"id": "b", "preview": "private history"}]
        with patch("watcher.load", return_value=queue):
            self.assertEqual(selected_events(["a"]), [queue[0]])
            with self.assertRaises(ValueError):
                selected_events(["missing"])

    def test_account_change_requires_reverification(self):
        self.assertTrue(avatar_matches([90] * 192, [92] * 192))
        self.assertFalse(avatar_matches([90] * 192, [170] * 192))
        self.assertFalse(avatar_matches([], []))

    def test_physical_dpi_coordinates_and_system_chats(self):
        lines = [
            line("friend", 210, 150), line("23:40", 411, 150),
            line("hello", 240, 183), line("微信团队", 210, 249),
            line("system notice", 240, 282),
        ]
        items = parse_previews(lines, 1342, 971, 1.5)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["contact_candidate"], "friend")


if __name__ == "__main__":
    unittest.main()
