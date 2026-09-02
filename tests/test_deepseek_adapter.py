import json
import unittest

from cofactor_bench.deepseek_adapter import (
    API_ENDPOINT,
    MAX_OUTPUT_TOKENS,
    build_request_body,
    error_event,
    response_event_stream,
)
from cofactor_bench.runner import parse_codex_stdout


class DeepSeekAdapterContractTests(unittest.TestCase):
    def test_request_is_fixed_to_official_flash_json_thinking_contract(self) -> None:
        body = json.loads(build_request_body("return json for this sequence"))

        self.assertEqual(API_ENDPOINT, "https://api.deepseek.com/chat/completions")
        self.assertEqual(
            body,
            {
                "model": "deepseek-v4-flash",
                "messages": [
                    {"role": "user", "content": "return json for this sequence"}
                ],
                "thinking": {"type": "enabled"},
                "reasoning_effort": "high",
                "response_format": {"type": "json_object"},
                "max_tokens": MAX_OUTPUT_TOKENS,
                "stream": False,
            },
        )
        self.assertEqual(MAX_OUTPUT_TOKENS, 16_384)

    def test_response_is_preserved_and_adapted_to_strict_replay_stream(self) -> None:
        prediction = {
            "schema_version": "cofactor9.1.response.v2",
            "sample_id": "sample_0123456789abcdef0123456789abcdef",
            "status": "predict",
            "predicted_cofactors": ["CHEBI:1"],
            "primary_guess": "CHEBI:1",
            "confidence_complete": 0.75,
        }
        envelope = {
            "id": "response-123",
            "model": "deepseek-v4-flash-0731",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(prediction),
                        "reasoning_content": "private chain of thought",
                        "role": "assistant",
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "prompt_tokens_details": {"cached_tokens": 80},
            },
        }

        stream = response_event_stream(
            json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        )
        output = parse_codex_stdout(stream)
        events = [json.loads(line) for line in stream.splitlines()]

        self.assertEqual(output.message_text, json.dumps(prediction))
        self.assertEqual(output.thread_id, "response-123")
        self.assertEqual(
            output.usage,
            {"completion_tokens": 20, "prompt_tokens": 100, "total_tokens": 120},
        )
        self.assertEqual(events[-1]["provider_response"], envelope)
        self.assertNotIn("private chain of thought", output.message_text)

    def test_empty_content_is_replayable_as_a_retryable_prediction_failure(self) -> None:
        envelope = {
            "id": "response-empty",
            "model": "deepseek-v4-flash-0731",
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"content": "", "reasoning_content": "reasoning"},
                }
            ],
            "usage": {"completion_tokens": 16_384},
        }

        output = parse_codex_stdout(
            response_event_stream(json.dumps(envelope).encode("utf-8"))
        )

        self.assertEqual(output.message_text, "")
        self.assertEqual(output.usage, {"completion_tokens": 16_384})

    def test_error_events_have_stable_retry_classification_markers(self) -> None:
        self.assertIn("invalid api key", error_event(401, "bad credential"))
        self.assertIn("quota exceeded", error_event(402, "balance"))
        self.assertIn("too many requests", error_event(429, "busy"))
        self.assertIn("service unavailable", error_event(503, "busy"))
        self.assertNotIn("bad credential", error_event(401, "bad credential"))

    def test_external_response_rejects_duplicate_keys_and_malformed_shapes(self) -> None:
        invalid = (
            b'{"id":"a","id":"b"}',
            b'{"id":"a","model":"m","choices":[],"usage":{}}',
            b'{"id":"a","model":"m","choices":[{"message":{"content":7}}],"usage":{}}',
            b'{"id":"a","model":"m","choices":[{"message":{"content":"{}"}}],"usage":{"total_tokens":true}}',
        )
        for raw in invalid:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    response_event_stream(raw)


if __name__ == "__main__":
    unittest.main()
