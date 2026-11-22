import unittest
from unittest.mock import Mock, patch

import relay_desktop as desktop
from clawbot_relay import correlated_reply


THREAD = "00000000-0000-4000-8000-000000000001"


def snapshot(status="completed", flags=None):
    return {
        "thread": {"id": THREAD, "hostId": "local",
                   "status": {"type": "active", "activeFlags": flags or []}},
        "turns": [{"id": "reply", "status": status, "error": None, "items": [
            {"type": "userMessage", "content": [{"type": "text", "text": "[marker]"}]},
            {"type": "agentMessage", "phase": "commentary", "text": "private progress"},
            {"type": "agentMessage", "phase": "final_answer", "text": "owner reply"},
        ]}],
    }


class DesktopToolsTests(unittest.TestCase):
    def test_normalized_reply_preserves_exact_correlation_and_final_phase(self):
        view = desktop.normalize_view(snapshot(), THREAD)
        self.assertTrue(view["live"])
        self.assertEqual(view["runtime"], "idle")
        self.assertEqual(correlated_reply(view, "[marker]", set()),
                         ("reply", "owner reply"))
        self.assertIsNone(correlated_reply(view, "[other]", set()))
        self.assertIsNone(correlated_reply(view, "[marker]", {"reply"}))

    def test_in_progress_and_approval_flags_never_establish_idle(self):
        for data in (snapshot("inProgress"), snapshot(flags=["waitingOnApproval"])):
            view = desktop.normalize_view(data, THREAD)
            self.assertEqual(view["runtime"], "active")

    def test_empty_or_unknown_snapshot_never_establishes_live(self):
        data = snapshot()
        data["turns"] = []
        self.assertFalse(desktop.normalize_view(data, THREAD)["live"])
        data = snapshot()
        data["thread"]["status"]["type"] = "unavailable"
        self.assertFalse(desktop.normalize_view(data, THREAD)["live"])

    def test_wrong_thread_or_remote_host_rejected(self):
        for key, value in (("id", "other"), ("hostId", "remote")):
            data = snapshot()
            data["thread"][key] = value
            with self.assertRaises(RuntimeError):
                desktop.normalize_view(data, THREAD)

    def test_truncated_answer_and_marker_rejected(self):
        for index in (0, 2):
            data = snapshot()
            data["turns"][0]["items"][index]["textTruncated"] = True
            with self.assertRaises(RuntimeError):
                desktop.normalize_view(data, THREAD)

    def test_dispatch_uses_only_bound_thread_without_permission_or_model_overrides(self):
        client = Mock()
        client.process.poll.return_value = None
        client.tool.return_value = {"threadId": THREAD}
        with patch.object(desktop, "_client", client), \
                patch.object(desktop, "load", return_value={"thread_id": THREAD}):
            result = desktop.request(THREAD, "messages", "owner prompt")
        self.assertEqual(result["kind"], "start")
        self.assertFalse(result["background"])
        client.tool.assert_called_once_with(THREAD, "send_message_to_thread", {
            "threadId": THREAD, "hostId": "local", "prompt": "owner prompt",
        })

    def test_uncertain_dispatch_is_never_retried_or_falls_back(self):
        client = Mock()
        client.process.poll.return_value = None
        client.tool.side_effect = TimeoutError
        with patch.object(desktop, "_client", client), \
                patch.object(desktop, "load", return_value={"thread_id": THREAD}):
            with self.assertRaises(TimeoutError):
                desktop.request(THREAD, "messages", "owner prompt")
        self.assertEqual(client.tool.call_count, 1)

    def test_unapproved_thread_is_rejected_before_connecting(self):
        with patch.object(desktop, "load", return_value={"thread_id": "other"}), \
                patch.object(desktop, "AppToolsClient") as launch:
            with self.assertRaises(RuntimeError):
                desktop.request(THREAD)
            launch.assert_not_called()

    def test_read_requests_relay_tool_input_and_uses_bounded_recent_turns(self):
        client = Mock()
        client.process.poll.return_value = None
        client.tool.return_value = snapshot()
        with patch.object(desktop, "_client", client), \
                patch.object(desktop, "load", return_value={"thread_id": THREAD}):
            self.assertTrue(desktop.request(THREAD)["live"])
        args = client.tool.call_args.args[2]
        self.assertTrue(args["includeOutputs"])
        self.assertEqual(args["turnLimit"], 10)

    def test_official_delegation_input_is_correlated_without_forwarding_other_outputs(self):
        data = snapshot()
        envelope = (
            "<codex_delegation>\n  <source_thread_id>" + THREAD
            + "</source_thread_id>\n  <input>[ClawBot relay test]\npayload"
            + "</input>\n</codex_delegation>"
        )
        data["turns"][0]["items"] = [
            {"type": "functionCallOutput", "name": "send_message_to_thread",
             "namespace": "codex_app", "output": {"text": envelope, "truncated": False}},
            {"type": "commandExecution", "output": {"text": envelope}},
            {"type": "agentMessage", "phase": "final_answer", "text": "owner reply"},
        ]
        view = desktop.normalize_view(data, THREAD)
        self.assertEqual(correlated_reply(view, "[ClawBot relay test]", set()),
                         ("reply", "owner reply"))
        self.assertEqual(len(view["turns"][0]["items"]), 2)
        data["turns"][0]["items"][0]["namespace"] = "other"
        self.assertIsNone(correlated_reply(desktop.normalize_view(data, THREAD),
                                          "[ClawBot relay test]", set()))

    def test_foreign_source_and_truncated_delegations_are_not_accepted(self):
        data = snapshot()
        tool = {"type": "functionCallOutput", "name": "send_message_to_thread",
                "namespace": "codex_app", "output": {
                    "text": "<codex_delegation>\n  <source_thread_id>other"
                            "</source_thread_id>\n  <input>[ClawBot relay test]"
                            "</input>\n</codex_delegation>", "truncated": False}}
        data["turns"][0]["items"] = [tool]
        self.assertEqual(desktop.normalize_view(data, THREAD)["turns"][0]["items"], [])
        tool["output"]["truncated"] = True
        with self.assertRaises(RuntimeError):
            desktop.normalize_view(data, THREAD)

    def test_mcp_error_is_not_treated_as_success(self):
        client = object.__new__(desktop.AppToolsClient)
        client.rpc = Mock(return_value={"isError": True, "content": [
            {"type": "text", "text": "private failure"}]})
        with patch.object(desktop, "load", return_value={"thread_id": THREAD}):
            with self.assertRaises(RuntimeError):
                client.tool(THREAD, "send_message_to_thread", {})


if __name__ == "__main__":
    unittest.main()
