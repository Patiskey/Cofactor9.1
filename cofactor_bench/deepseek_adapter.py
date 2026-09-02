#!/usr/bin/env python3
"""Standalone OpenAI-compatible adapter for the official DeepSeek API.

The file intentionally depends only on the Python standard library so the run
orchestrator can freeze and execute these exact bytes from an isolated working
directory.

API contract sources:
- https://api-docs.deepseek.com/
- https://api-docs.deepseek.com/guides/json_mode
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any


API_ENDPOINT = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-v4-flash"
REASONING_EFFORT = "high"
SERVICE_TIER = "default"
MAX_OUTPUT_TOKENS = 16_384
ADAPTER_VERSION = "cofactor9.1-deepseek-adapter 1.0.0"


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r}")


def _canonical_line(value: Mapping[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def build_request_body(prompt: str) -> bytes:
    """Build the one frozen request shape accepted by the adapter."""

    if not isinstance(prompt, str) or not prompt:
        raise ValueError("prompt must be a nonempty string")
    value = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "thinking": {"type": "enabled"},
        "reasoning_effort": REASONING_EFFORT,
        "response_format": {"type": "json_object"},
        "max_tokens": MAX_OUTPUT_TOKENS,
        "stream": False,
    }
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _decoded_response(raw: bytes) -> dict[str, object]:
    if not isinstance(raw, bytes) or not raw:
        raise ValueError("DeepSeek response must be nonempty bytes")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("DeepSeek response is not one valid JSON document") from error
    if not isinstance(value, dict):
        raise ValueError("DeepSeek response must be a JSON object")
    return value


def response_event_stream(raw: bytes) -> str:
    """Validate an API envelope and adapt it to the frozen replay protocol."""

    envelope = _decoded_response(raw)
    response_id = envelope.get("id")
    model = envelope.get("model")
    choices = envelope.get("choices")
    usage = envelope.get("usage")
    if not isinstance(response_id, str) or not response_id:
        raise ValueError("DeepSeek response id is invalid")
    if not isinstance(model, str) or not model:
        raise ValueError("DeepSeek response model is invalid")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("DeepSeek response must contain exactly one choice")
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise ValueError("DeepSeek response choice is invalid")
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise ValueError("DeepSeek response message is invalid")
    content = message.get("content")
    if not isinstance(content, str):
        raise ValueError("DeepSeek response content must be a string")
    if not isinstance(usage, Mapping):
        raise ValueError("DeepSeek response usage is invalid")
    flat_usage: dict[str, int] = {}
    for key, value in usage.items():
        if not isinstance(key, str) or not key:
            raise ValueError("DeepSeek usage key is invalid")
        if isinstance(value, Mapping):
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
        ):
            raise ValueError(f"DeepSeek usage value {key!r} is invalid")
        flat_usage[key] = value

    events: tuple[Mapping[str, object], ...] = (
        {"type": "thread.started", "thread_id": response_id},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": content},
        },
        {
            "type": "turn.completed",
            "usage": flat_usage,
            "provider": "deepseek-official",
            "provider_model": model,
            "finish_reason": choice.get("finish_reason"),
            "provider_response": envelope,
        },
    )
    return "".join(_canonical_line(event) + "\n" for event in events)


def error_event(status: int | None, _detail: str = "") -> str:
    """Return a secret-free event with markers used by the durable runner."""

    if status == 401:
        message = "DeepSeek unauthorized: invalid api key"
    elif status == 402:
        message = "DeepSeek quota exceeded: insufficient balance"
    elif status == 429:
        message = "DeepSeek too many requests: rate limit"
    elif status is not None and status >= 500:
        message = "DeepSeek service unavailable"
    elif status is None:
        message = "DeepSeek network connection failure"
    else:
        message = f"DeepSeek request rejected with HTTP {status}"
    return _canonical_line({"type": "error", "message": message}) + "\n"


__all__ = [
    "ADAPTER_VERSION",
    "API_ENDPOINT",
    "MAX_OUTPUT_TOKENS",
    "MODEL",
    "REASONING_EFFORT",
    "SERVICE_TIER",
    "build_request_body",
    "error_event",
    "response_event_stream",
]
