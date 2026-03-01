import asyncio
import unittest
from unittest.mock import patch

from codex_telegram_bot.providers.gemini_provider import GeminiProvider


class TestGeminiToolCalling(unittest.TestCase):
    def test_generate_with_tools_parses_function_call(self):
        provider = GeminiProvider(api_key="test-key", model="gemini-2.0-flash")

        fake_response = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "Let me check that."},
                            {
                                "functionCall": {
                                    "name": "read_file",
                                    "args": {"path": "README.md"},
                                }
                            },
                        ]
                    }
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 5,
                "totalTokenCount": 15,
            },
        }

        with patch.object(provider, "_send_request", return_value=fake_response):
            result = asyncio.run(
                provider.generate_with_tools(
                    messages=[{"role": "user", "content": "read readme"}],
                    tools=[
                        {
                            "name": "read_file",
                            "description": "Read file",
                            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                        }
                    ],
                    system="You are helpful.",
                )
            )

        self.assertEqual(result["stop_reason"], "tool_use")
        blocks = result["content"]
        self.assertTrue(any(b.get("type") == "tool_use" for b in blocks))
        tool = [b for b in blocks if b.get("type") == "tool_use"][0]
        self.assertEqual(tool["name"], "read_file")
        self.assertEqual(tool["input"].get("path"), "README.md")
        self.assertEqual(result["usage"]["total_tokens"], 15)


if __name__ == "__main__":
    unittest.main()

