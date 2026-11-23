import tempfile
import copy
import sys
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
    def test_old_dates_and_ocr_changes_never_requeue_history(self):
        state = {"seen": []}
        for stamp in ("星期六", "周日", "昨天 23:00", "前天", "11月21日", "2026/11/21"):
            with self.subTest(stamp=stamp):
                item = {"contact_candidate": "friend", "preview": "old text...",
                        "displayed_time": stamp}
                self.assertEqual(fresh_candidates([item], state), [])
                self.assertEqual(fresh_candidates([{**item, "preview": "o1d text.."}], state), [])
        new = {**item, "preview": "new arrival", "displayed_time": "16:21"}
        self.assertEqual(fresh_candidates([new], state), [new])

    def test_whitespace_jitter_but_not_new_text_sharing_prefix_or_time(self):
        item = {"contact_candidate": "friend", "preview": "long message...",
                "displayed_time": "16:20"}
        state = {"seen": []}
        fresh_candidates([item], state)
        expanded = {**item, "preview": "long message with more text"}
        self.assertEqual(fresh_candidates([expanded], state), [expanded])
        self.assertEqual(fresh_candidates([{**expanded, "preview": "longmessage with more text"}], state), [])
        changed = {**item, "preview": "different incoming text"}
        self.assertEqual(fresh_candidates([changed], state), [changed])
        new = {**item, "displayed_time": "16:21"}
        self.assertEqual(fresh_candidates([new], state), [new])

    def outgoing_scene(self, scale=1, latest_incoming=False):
        from PIL import Image, ImageDraw
        image = Image.new("RGB", (int(900 * scale), int(820 * scale)), (30, 30, 30))
        draw = ImageDraw.Draw(image)
        draw.rectangle(tuple(int(v * scale) for v in (480, 485, 820, 525)), fill=(40, 205, 150))
        title = line("friend", 340 * scale, 54 * scale)
        first = {**line("outgoing message with", 640 * scale, 495 * scale),
                 "bounds": [v * scale for v in (500, 488, 780, 503)]}
        second = {**line("two lines", 600 * scale, 515 * scale),
                  "bounds": [v * scale for v in (500, 508, 700, 522)]}
        lines = [title, first, second]
        if latest_incoming:
            lines.append({**line("new reply", 420 * scale, 560 * scale),
                          "bounds": [v * scale for v in (370, 550, 470, 570)]})
        item = {"contact_candidate": "friend", "preview": "outgoing message...",
                "displayed_time": "16:20", "direction": "unknown"}
        return image, lines, item

    def test_outgoing_multiline_bubble_is_filtered_at_each_dpi(self):
        for scale in (1, 1.5, 2):
            image, lines, item = self.outgoing_scene(scale)
            watcher.classify_visible_outgoing([item], lines, image, scale)
            self.assertEqual(item["direction"], "outgoing")
            state = {"seen": []}
            self.assertEqual(fresh_candidates([item], state), [])
            incoming = {**item, "preview": "immediate reply", "direction": "unknown"}
            self.assertEqual(fresh_candidates([incoming], state), [incoming])

    def test_outgoing_single_dot_clipping_is_filtered_without_contact_cooldown(self):
        for scale in (1, 1.5, 2):
            image, lines, item = self.outgoing_scene(scale)
            item["preview"] = "outgoing message."
            watcher.classify_visible_outgoing([item], lines, image, scale)
            self.assertEqual(item["direction"], "outgoing")
            state = {"seen": []}
            self.assertEqual(fresh_candidates([item], state), [])
            incoming = {**item, "preview": "outgoing message? really?", "direction": "unknown"}
            self.assertEqual(fresh_candidates([incoming], state), [incoming])

    def test_single_dot_needs_visible_outgoing_evidence_and_long_prefix(self):
        image, lines, item = self.outgoing_scene(latest_incoming=True)
        item["preview"] = "outgoing message."
        watcher.classify_visible_outgoing([item], lines, image, 1)
        self.assertEqual(item["direction"], "unknown")
        self.assertEqual(fresh_candidates([item], {"seen": []}), [item])
        self.assertFalse(watcher.same_clipped_preview("short.", "short.new", allow_expansion=True))
        self.assertFalse(watcher.same_clipped_preview("outgoing message.", "outgoing message changed"))

    def test_other_chat_incoming_last_or_missing_evidence_stays_unverified(self):
        for case in ("other_chat", "incoming_last", "no_bounds", "low_confidence", "other_text", "gray"):
            with self.subTest(case=case):
                image, lines, item = self.outgoing_scene(latest_incoming=case == "incoming_last")
                if case == "other_chat":
                    item["contact_candidate"] = "different"
                elif case == "no_bounds":
                    lines[-1].pop("bounds")
                elif case == "low_confidence":
                    lines[-1]["score"] = 0.7
                elif case == "other_text":
                    item["preview"] = "a different new arrival"
                elif case == "gray":
                    from PIL import ImageDraw
                    ImageDraw.Draw(image).rectangle((480, 485, 820, 525), fill=(55, 55, 55))
                watcher.classify_visible_outgoing([item], lines, image, 1)
                self.assertEqual(item["direction"], "unknown")
                self.assertEqual(fresh_candidates([item], {"seen": []}), [item])

    def test_read_retries_transient_status_file_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            path.write_text('{"status":"running"}', encoding="utf-8")
            with patch.object(Path, "read_text", side_effect=[PermissionError(), '{"status":"running"}']), \
                    patch.object(watcher.time, "sleep"):
                self.assertEqual(watcher.load(path, {}), {"status": "running"})

    def test_maximized_border_translation_matches_original_avatar(self):
        from PIL import Image
        import random
        rng = random.Random(42)
        baseline = Image.frombytes("RGB", (350, 450), rng.randbytes(350 * 450 * 3))
        state = {"scale": 1, "avatar": watcher.avatar_signature(baseline, 1)}
        moved = Image.new("RGB", (600, 700))
        moved.paste(baseline, (8, 8))
        aligned, scale, origin = watcher.approved_layout(moved, state, 1)
        self.assertEqual(scale, 1)
        self.assertEqual(origin, [8, 8])
        self.assertEqual(watcher.avatar_signature(aligned, 1), state["avatar"])

    def test_recovery_keeps_new_reply_and_baselines_newly_exposed_history(self):
        old = {"contact_candidate": "friend", "preview": "old", "displayed_time": "10:00"}
        new = {**old, "preview": "reply", "displayed_time": "10:01"}
        history = {**old, "contact_candidate": "another"}
        state = {"seen": []}
        rebase_previews([old], state)
        self.assertEqual(watcher.recovered_candidates([new, history], state), [new])
        self.assertEqual(fresh_candidates([new, history], state), [])

    def test_scale_change_requires_original_avatar_and_keeps_baseline(self):
        state = {"scale": 1.5, "avatar": [90] * 192}
        before = copy.deepcopy(state)
        with patch.object(watcher, "avatar_signature", side_effect=lambda image, scale:
                          [90 if scale == 1 else 170] * 192):
            self.assertEqual(watcher.approved_scale(None, state, 1), 1)
            self.assertIsNone(watcher.approved_scale(None, state, 2))
        self.assertEqual(state, before)

    def run_adaptive_scan(self, size, scale, identity_changed=False, avatar_changed=False,
                          outgoing=False):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = {"contact_candidate": "friend", "preview": "old", "displayed_time": "10:00"}
            state = {"hwnd": 10, "pid": 20, "size": [1342, 1230], "scale": 1.5,
                     "avatar": [90] * 192, "window_identity": {"exe": "approved"}, "seen": []}
            rebase_previews([old], state)
            paths = {name: root / (name + ".json") for name in ("STATE", "QUEUE", "STATUS")}
            watcher.save(paths["STATE"], state)
            watcher.save(paths["QUEUE"], [])
            lines = [line("friend", 140 * scale, 100 * scale),
                     line("10:01", 274 * scale, 100 * scale),
                     line("reply", 160 * scale, 122 * scale)]
            image = Image.new("RGB", size)
            if outgoing:
                image, chat_lines, preview = self.outgoing_scene(scale)
                lines[-1]["text"] = preview["preview"]
                lines += chat_lines
            from contextlib import ExitStack
            with ExitStack() as stack:
                for name, path in paths.items():
                    stack.enter_context(patch.object(watcher, name, path))
                stack.enter_context(patch.object(watcher, "ROOT", root))
                stack.enter_context(patch.object(watcher, "STOP", root / "STOP"))
                stack.enter_context(patch.object(sys, "argv", ["watcher", "--scan"]))
                stack.enter_context(patch.object(watcher, "scan_window", return_value=(
                    11, image, 21,
                    {"exe": "different" if identity_changed else "approved"})))
                stack.enter_context(patch.object(watcher, "window_scale", return_value=scale))
                stack.enter_context(patch.object(watcher.time, "sleep"))
                stack.enter_context(patch.object(watcher, "avatar_signature",
                                               return_value=[170 if avatar_changed else 90] * 192))
                ocr = stack.enter_context(patch.object(watcher, "read_ocr", return_value=lines))
                watcher.main()
            return (watcher.load(paths["STATUS"], {}), watcher.load(paths["STATE"], {}),
                    watcher.load(paths["QUEUE"], []), ocr.call_count, state)

    def test_scan_does_not_queue_visible_outgoing_after_recovery(self):
        status, state, queue, calls, original = self.run_adaptive_scan((900, 820), 1, outgoing=True)
        self.assertEqual(status["status"], "scan_ok")
        self.assertEqual(status["new_candidates"], 0)
        self.assertEqual(queue, [])
        self.assertEqual(calls, 1)

    def test_resized_restarted_and_dpi_changed_window_recovers(self):
        for size, scale in (((883, 815), 1), ((1500, 1100), 1.5), ((1766, 1630), 2)):
            with self.subTest(size=size, scale=scale):
                status, state, queue, calls, original = self.run_adaptive_scan(size, scale)
                self.assertEqual(status["status"], "scan_ok")
                self.assertTrue(status["layout_adapted"])
                self.assertEqual(state["size"], list(size))
                self.assertEqual(state["scale"], scale)
                self.assertEqual(state["hwnd"], 11)
                self.assertEqual(state["avatar"], original["avatar"])
                self.assertEqual([event["preview"] for event in queue], ["reply"])

    def test_changed_identity_never_updates_state_or_reads_chats(self):
        for flags in ({"identity_changed": True}, {"avatar_changed": True}):
            with self.subTest(flags=flags):
                status, state, queue, calls, original = self.run_adaptive_scan((883, 815), 1, **flags)
                self.assertEqual(status["status"], "paused")
                self.assertEqual(state, original)
                self.assertEqual(queue, [])
                self.assertEqual(calls, 0)

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
