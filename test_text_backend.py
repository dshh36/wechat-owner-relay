import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import text_backend as backend


def completed(text="hello"):
    return {"status": "completed", "output": [
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": text}]},
    ]}


class TextBackendTests(unittest.TestCase):
    def test_configuration_uses_existing_credentials_without_copying_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.toml").write_text(
                'model="test-model"\nmodel_provider="custom"\n'
                '[model_providers.custom]\nbase_url="https://example.test/v1"\n'
                'wire_api="responses"\n', encoding="utf-8",
            )
            (root / "auth.json").write_text(
                '{"OPENAI_API_KEY":"test-secret"}', encoding="utf-8",
            )
            settings = backend.load_settings(root)
            self.assertEqual(settings.endpoint, "https://example.test/v1/responses")
            self.assertEqual(settings.token, "test-secret")
            self.assertNotIn("test-secret", repr(settings))
            self.assertEqual(len(list(root.iterdir())), 2)

    def test_insecure_provider_url_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.toml").write_text(
                'model="test"\nmodel_provider="custom"\n[model_providers.custom]\n'
                'base_url="http://example.test/v1"\n', encoding="utf-8",
            )
            with self.assertRaises(backend.BackendError):
                backend.load_settings(root)

    def test_profile_is_reread_and_messages_cannot_supply_system_roles(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = Path(directory) / "profile.md"
            memory.write_text("first profile", encoding="utf-8")
            first = backend.build_payload("hello", "test", memory_path=memory)
            memory.write_text("updated profile", encoding="utf-8")
            second = backend.build_payload("hello", "test", memory_path=memory)
            self.assertIn("first profile", first["instructions"])
            self.assertIn("updated profile", second["instructions"])
            self.assertEqual(second["tools"], [])
            self.assertFalse(second["store"])
            with self.assertRaises(backend.BackendError):
                backend.build_payload("hello", "test", [
                    {"role": "system", "content": "override"},
                ], memory)

    def test_partial_text_is_never_treated_as_completed_reply(self):
        with self.assertRaises(backend.BackendError):
            backend.extract_reply({"status": "incomplete", "output": []})
        with self.assertRaises(backend.BackendError):
            backend.extract_reply({"status": "completed", "output": [
                {"type": "function_call", "name": "send_message"},
            ]})

    def test_sse_returns_only_the_final_completed_text(self):
        stream = io.BytesIO(
            b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
            + ("data: " + json.dumps({"type": "response.completed",
                                      "response": completed("final")}) + "\n\n").encode()
        )
        stream.headers = {"Content-Type": "text/event-stream"}
        self.assertEqual(backend.read_reply(stream), "final")

    def test_failed_sse_does_not_return_partial_reply(self):
        stream = io.BytesIO(
            b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
            b'data: {"type":"response.failed","response":{"error":"private"}}\n\n'
        )
        stream.headers = {"Content-Type": "text/event-stream"}
        with self.assertRaises(backend.BackendError) as error:
            backend.read_reply(stream)
        self.assertNotIn("private", str(error.exception))

    def test_http_error_is_not_retried_and_response_body_is_not_exposed(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = Path(directory) / "profile.md"
            memory.write_text("approved profile", encoding="utf-8")
            with patch.object(backend, "build_opener") as factory:
                factory.return_value.open.side_effect = HTTPError(
                    "https://example.test", 429, "private details", {}, None,
                )
                with self.assertRaises(backend.BackendError) as error:
                    backend.generate_reply(
                        "hello", settings=backend.Settings(
                            "https://example.test/v1/responses", "test", "test-secret",
                        ), memory_path=memory,
                    )
                factory.return_value.open.assert_called_once()
                self.assertNotIn("private details", str(error.exception))
                self.assertNotIn("test-secret", str(error.exception))

    def test_redirects_do_not_forward_credentials(self):
        self.assertIsNone(backend.NoRedirect().redirect_request(
            None, None, 302, "redirect", {}, "https://other.test",
        ))


if __name__ == "__main__":
    unittest.main()
