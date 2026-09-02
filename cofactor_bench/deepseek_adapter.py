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
import http.client
import json
import os
import sys
from typing import Any
import urllib.error
import urllib.request


API_ENDPOINT = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-v4-flash"
REASONING_EFFORT = "none"
SERVICE_TIER = "default"
THINKING_TYPE = "disabled"
MAX_OUTPUT_TOKENS = 2_048
ADAPTER_VERSION = "cofactor9.1-deepseek-adapter 1.2.0"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 600.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


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
        "thinking": {"type": THINKING_TYPE},
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


def perform_request(
    prompt: str,
    api_key: str,
    *,
    opener: Any | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Make one HTTPS request and return a validated replay event stream."""

    if (
        not isinstance(api_key, str)
        or not api_key
        or any(ord(character) < 32 for character in api_key)
    ):
        raise ValueError("API key is missing or malformed")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not 0 < float(timeout) < 86_400
    ):
        raise ValueError("timeout must be positive and below one day")
    request = urllib.request.Request(
        API_ENDPOINT,
        data=build_request_body(prompt),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Cofactor9.1/DeepSeekAdapter-1.0",
        },
        method="POST",
    )
    transport = (
        urllib.request.build_opener(_NoRedirect()) if opener is None else opener
    )
    try:
        with transport.open(request, timeout=float(timeout)) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except http.client.IncompleteRead as error:
        raise urllib.error.URLError("incomplete DeepSeek HTTP response") from error
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("DeepSeek response exceeds the fixed size limit")
    return response_event_stream(raw)


def _disabled_features(argv: list[str]) -> list[str]:
    features: list[str] = []
    index = 0
    while index < len(argv):
        if argv[index] != "--disable" or index + 1 >= len(argv):
            raise ValueError("feature preflight arguments are invalid")
        feature = argv[index + 1]
        if not feature or feature in features:
            raise ValueError("feature preflight contains an invalid duplicate")
        features.append(feature)
        index += 2
    return features


def _option_value(argv: list[str], option: str) -> str:
    if argv.count(option) != 1:
        raise ValueError(f"adapter requires exactly one {option}")
    index = argv.index(option)
    if index + 1 >= len(argv):
        raise ValueError(f"adapter option {option} has no value")
    return argv[index + 1]


def _validate_exec_arguments(argv: list[str]) -> None:
    if not argv or argv[0] != "exec" or argv[-1] != "-":
        raise ValueError("adapter requires the hardened exec command")
    if _option_value(argv, "--model") != MODEL:
        raise ValueError("adapter model differs from its frozen contract")
    configs = [argv[index + 1] for index, item in enumerate(argv[:-1]) if item == "-c"]
    required = {
        f'model_reasoning_effort="{REASONING_EFFORT}"',
        f'service_tier="{SERVICE_TIER}"',
        'approval_policy="never"',
        'web_search="disabled"',
    }
    if not required.issubset(configs):
        raise ValueError("adapter execution settings differ from its frozen contract")
    for flag in ("--json", "--output-schema", "-C"):
        if flag not in argv:
            raise ValueError(f"adapter requires {flag}")


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--version"]:
        print(ADAPTER_VERSION)
        return 0
    if arguments[:2] == ["features", "list"]:
        try:
            features = _disabled_features(arguments[2:])
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
        for feature in features:
            print(f"{feature} stable false")
        return 0
    try:
        _validate_exec_arguments(arguments)
        prompt = sys.stdin.read()
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        stream = perform_request(prompt, key)
    except urllib.error.HTTPError as error:
        sys.stdout.write(error_event(error.code))
        print(f"DeepSeek HTTP {error.code}", file=sys.stderr)
        return 1
    except urllib.error.URLError:
        sys.stdout.write(error_event(None))
        print("DeepSeek transport failure", file=sys.stderr)
        return 1
    except ValueError as error:
        message = str(error)
        status = 401 if "API key" in message else 400
        sys.stdout.write(error_event(status))
        print("DeepSeek adapter rejected local input", file=sys.stderr)
        return 1
    finally:
        if "key" in locals():
            key = ""
    sys.stdout.write(stream)
    return 0


__all__ = [
    "ADAPTER_VERSION",
    "API_ENDPOINT",
    "MAX_OUTPUT_TOKENS",
    "MAX_RESPONSE_BYTES",
    "MODEL",
    "REASONING_EFFORT",
    "SERVICE_TIER",
    "THINKING_TYPE",
    "build_request_body",
    "error_event",
    "main",
    "perform_request",
    "response_event_stream",
]


if __name__ == "__main__":
    raise SystemExit(main())
