from __future__ import annotations

import json
import unittest
from unittest import mock

from ppt_agent.providers.openai_compatible import (
    ModelResponseFormatError,
    OpenAICompatibleProvider,
)


class OpenAICompatibleProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = OpenAICompatibleProvider(api_key="test", model="test-model")

    @staticmethod
    def response(content: str) -> dict:
        return {"choices": [{"message": {"content": content}}]}

    def test_invalid_json_is_repaired_with_location_and_correction(self) -> None:
        self.provider._post = mock.Mock(
            side_effect=[
                self.response('{"action":"first"}{"action":"second"}'),
                self.response('{"action":"first","arguments":{}}'),
            ]
        )

        result = self.provider.complete_messages([{"role": "user", "content": "return JSON"}])

        self.assertEqual(result["action"], "first")
        retry_payload = self.provider._post.call_args_list[1].args[1]
        repair = json.loads(retry_payload["messages"][-1]["content"])["response_error"]
        self.assertEqual(repair["code"], "model_json_response_invalid")
        self.assertEqual(repair["location"]["parser_message"], "Extra data")
        self.assertIn("a second JSON object or action", repair["correction"]["forbidden"])

    def test_repeated_invalid_json_raises_structured_error(self) -> None:
        self.provider._post = mock.Mock(
            side_effect=[self.response("not json") for _ in range(self.provider.json_attempts)]
        )

        with self.assertRaises(ModelResponseFormatError) as raised:
            self.provider.complete_json(system="system", prompt="prompt")

        self.assertEqual(raised.exception.detail["code"], "model_json_response_invalid")
        self.assertIn("Return exactly one", raised.exception.detail["correction"]["required"])
        self.assertEqual(self.provider._post.call_count, 4)

    def test_later_attempts_use_focused_json_syntax_repair(self) -> None:
        self.provider._post = mock.Mock(
            side_effect=[
                self.response('{"action":"broken"'),
                self.response('{"action":"still broken"'),
                self.response('{"action":"fixed","arguments":{}}'),
            ]
        )

        result = self.provider.complete_messages([{"role": "user", "content": "return an action"}])

        self.assertEqual(result["action"], "fixed")
        third_payload = self.provider._post.call_args_list[2].args[1]
        self.assertIn("JSON syntax repairer", third_payload["messages"][0]["content"])
        repair_request = json.loads(third_payload["messages"][1]["content"])
        self.assertIn("invalid_response", repair_request)
        self.assertEqual(repair_request["parse_error"]["location"]["field"], "model_response")


if __name__ == "__main__":
    unittest.main()
