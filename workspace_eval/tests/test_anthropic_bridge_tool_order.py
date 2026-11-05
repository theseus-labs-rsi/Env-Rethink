"""Regression tests for mixed tool_result/text messages in the chat bridge.
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
import anthropic_responses_bridge as bridge  # noqa: E402


class ChatCompletionsToolOrderTests(unittest.TestCase):
    def convert(self, payload):
        """Run the real handler with an in-memory fake upstream response."""
        handler = bridge.BridgeHandler.__new__(bridge.BridgeHandler)
        handler.server = SimpleNamespace(
            target_base_url="https://invalid.example/v1",
            provider_config={},
            record_usage=lambda _: None,
        )
        handler._send_json = Mock()
        captured = []

        def fake_urlopen(request, timeout):
            self.assertEqual(timeout, 300)
            captured.append(json.loads(request.data.decode("utf-8")))
            return io.BytesIO(json.dumps({
                "id": "fake-response",
                "model": "test-model",
                "choices": [{"message": {"content": "ok"}}],
            }).encode("utf-8"))

        with (
            patch.object(bridge, "resolve_provider_api_key", return_value="fake-key"),
            patch.object(bridge.urllib.request, "urlopen", side_effect=fake_urlopen),
        ):
            bridge.BridgeHandler._handle_chat_completions(
                handler, payload, "test-model"
            )
        self.assertEqual(len(captured), 1)
        handler._send_json.assert_called_once()
        return captured[0]["messages"]

    def test_tool_result_precedes_user_text_regardless_of_block_order(self):
        call = {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {}}
        result = {"type": "tool_result", "tool_use_id": "call_1", "content": "found"}
        text = {"type": "text", "text": "Continue"}

        for blocks in ([result, text], [text, result]):
            with self.subTest(blocks=[block["type"] for block in blocks]):
                messages = self.convert({"messages": [
                    {"role": "assistant", "content": [call]},
                    {"role": "user", "content": blocks},
                ]})
                self.assertEqual([msg["role"] for msg in messages],
                                 ["assistant", "tool", "user"])
                self.assertEqual(messages[0]["tool_calls"][0]["id"], "call_1")
                self.assertEqual(messages[1]["tool_call_id"], "call_1")
                self.assertEqual(messages[1]["content"], "found")
                self.assertEqual(messages[2]["content"], "Continue")

    def test_multiple_tool_results_precede_text_without_loss(self):
        messages = self.convert({"messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "call_a", "name": "lookup", "input": {}},
                {"type": "tool_use", "id": "call_b", "name": "lookup", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_a", "content": "A"},
                {"type": "tool_result", "tool_use_id": "call_b", "content": "B"},
                {"type": "text", "text": "Next"},
            ]},
        ]})
        self.assertEqual([msg["role"] for msg in messages],
                         ["assistant", "tool", "tool", "user"])
        self.assertEqual([msg["tool_call_id"] for msg in messages[1:3]],
                         ["call_a", "call_b"])
        self.assertEqual([msg["content"] for msg in messages[1:]],
                         ["A", "B", "Next"])

    def test_plain_text_and_tool_only_messages(self):
        messages = self.convert({"messages": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "call_1", "name": "lookup", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": "Done"},
            ]},
        ]})
        self.assertEqual([msg["role"] for msg in messages],
                         ["user", "assistant", "tool"])
        self.assertEqual(messages[0]["content"], "Hello")
        self.assertEqual(messages[2]["content"], "Done")


if __name__ == "__main__":
    unittest.main()
