"""Hardened, append-only Codex exec transport for sequence-only cases."""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from typing import Literal

from cofactor_bench.prediction import (
    Prediction,
    PredictionValidationError,
    parse_prediction_json,
    validate_prediction,
)
from cofactor_bench.prompt import PROMPT_VERSION, PromptCase, render_prompt


MODEL = "gpt-5.6-sol"
REASONING_EFFORT = "max"
SERVICE_TIER = "fast"
MAX_ATTEMPTS = 3
MAX_CONCURRENCY = 16
DEFAULT_CIRCUIT_BREAKER_THRESHOLD = 3

CIRCUIT_BREAKER_ERROR_CODES = frozenset(
    {
        "AUTH_ERROR",
        "CAPACITY_ERROR",
        "CODEX_EVENT_ERROR",
        "PROCESS_EXIT",
        "PROCESS_START_ERROR",
        "TIMEOUT",
        "TRANSPORT_ERROR",
        "TRUNCATED_STDOUT",
    }
)

DISABLED_FEATURES = (
    "unbounded_connection_retries",
    "shell_tool",
    "unified_exec",
    "apps",
    "plugins",
    "multi_agent",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "image_generation",
    "skill_search",
    "view_image",
    "standalone_web_search",
    "hooks",
    "goals",
)


class RunnerError(RuntimeError):
    """Base class for runner failures."""


class CompletedCaseError(RunnerError):
    """Raised rather than overwriting an existing terminal record."""


class AttemptFailure(RunnerError):
    """A classified failure safe for retry and terminal-ledger decisions."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retry_disposition: Literal["retryable", "nonretryable"],
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retry_disposition = retry_disposition


@dataclass(frozen=True, slots=True)
class CodexOutput:
    """Validated outer Codex event stream before inner response parsing."""

    message_text: str
    usage: dict[str, object]
    thread_id: str


@dataclass(frozen=True, slots=True)
class TerminalResult:
    """One immutable terminal outcome for a benchmark case."""

    sample_id: str
    status: Literal["success", "terminal_error"]
    attempt_count: int
    prediction: Prediction | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class CaseRunFailure:
    """An in-memory worker exception that was never made into a terminal result."""

    sample_id: str
    error: Exception


class RunCasesError(RunnerError):
    """Aggregate worker failures without discarding other completed results."""

    def __init__(
        self,
        *,
        completed_results: tuple[TerminalResult, ...],
        failures: tuple[CaseRunFailure, ...],
        message: str | None = None,
    ) -> None:
        self.completed_results = completed_results
        self.failures = failures
        super().__init__(
            message
            or f"{len(failures)} case worker(s) failed; "
            f"{len(completed_results)} result(s) completed"
        )


class RunAborted(RunCasesError):
    """A circuit-broken batch with explicit append-only resume information."""

    def __init__(
        self,
        *,
        error_code: str,
        threshold: int,
        completed_results: tuple[TerminalResult, ...],
        failures: tuple[CaseRunFailure, ...],
        pending_sample_ids: tuple[str, ...],
    ) -> None:
        self.error_code = error_code
        self.threshold = threshold
        self.pending_sample_ids = pending_sample_ids
        super().__init__(
            completed_results=completed_results,
            failures=failures,
            message=(
                f"run aborted after {threshold} consecutive {error_code} results; "
                f"{len(pending_sample_ids)} case(s) were not started and may be "
                "continued with resume=True"
            ),
        )


@dataclass(frozen=True, slots=True)
class _ProcessCapture:
    argv: tuple[str, ...]
    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool
    start_error: str | None
    duration_seconds: float


_ALLOWED_ENVIRONMENT_NAMES = (
    "PATH",
    "HOME",
    "CODEX_HOME",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "COLORTERM",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)
_SENSITIVE_NAME_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD")


_ALLOWED_EVENT_TYPES = frozenset(
    {
        "thread.started",
        "turn.started",
        "item.started",
        "item.completed",
        "turn.completed",
        "turn.failed",
        "error",
    }
)
_ALLOWED_ITEM_TYPES = frozenset({"reasoning", "agent_message"})
_TOOL_ITEM_TYPES = frozenset(
    {
        "command_execution",
        "file_change",
        "mcp_tool_call",
        "web_search",
        "computer",
        "browser",
        "app",
    }
)

_SYSTEMATIC_FAILURE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "AUTH_ERROR",
        (
            "authentication failed",
            "invalid api key",
            "invalid credential",
            "not authenticated",
            "permission denied",
            "unauthorized",
        ),
    ),
    (
        "CAPACITY_ERROR",
        (
            "capacity",
            "overloaded",
            "quota exceeded",
            "rate limit",
            "too many requests",
        ),
    ),
    (
        "TRANSPORT_ERROR",
        (
            "connection refused",
            "connection reset",
            "dns failure",
            "network connection",
            "service unavailable",
            "tls handshake",
        ),
    ),
)


def _classify_systematic_failure(value: str) -> str | None:
    normalized = value.casefold()
    for code, markers in _SYSTEMATIC_FAILURE_MARKERS:
        if any(marker in normalized for marker in markers):
            return code
    return None


def _decode_outer_event(line: str, line_number: int) -> Mapping[str, object]:
    def reject_duplicate_keys(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise AttemptFailure(
                    "INVALID_EVENT_STREAM",
                    f"outer JSON line {line_number} has duplicate key {key!r}",
                    retry_disposition="retryable",
                )
            result[key] = value
        return result

    def reject_nonfinite_number(value: str) -> object:
        raise AttemptFailure(
            "INVALID_EVENT_STREAM",
            f"outer JSON line {line_number} contains non-finite number {value!r}",
            retry_disposition="retryable",
        )

    try:
        event = json.loads(
            line,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_nonfinite_number,
        )
    except AttemptFailure:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise AttemptFailure(
            "TRUNCATED_STDOUT",
            f"outer JSON line {line_number} is incomplete or malformed",
            retry_disposition="retryable",
        ) from error
    if not isinstance(event, Mapping):
        raise AttemptFailure(
            "INVALID_EVENT_STREAM",
            f"outer JSON line {line_number} is not an object",
            retry_disposition="retryable",
        )
    return event


def parse_codex_stdout(stdout: str) -> CodexOutput:
    """Strictly decode Codex JSONL and reject all tool/unknown events."""

    if not isinstance(stdout, str) or not stdout.strip():
        raise AttemptFailure(
            "TRUNCATED_STDOUT",
            "Codex stdout is empty",
            retry_disposition="retryable",
        )

    seen_thread = False
    seen_turn = False
    seen_completed = False
    thread_id = ""
    messages: list[str] = []
    usage: dict[str, object] = {}
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            raise AttemptFailure(
                "INVALID_EVENT_STREAM",
                f"outer JSON line {line_number} is blank",
                retry_disposition="retryable",
            )
        event = _decode_outer_event(line, line_number)
        event_type = event.get("type")
        if not isinstance(event_type, str) or event_type not in _ALLOWED_EVENT_TYPES:
            raise AttemptFailure(
                "UNKNOWN_EVENT",
                f"unrecognized Codex event type {event_type!r}",
                retry_disposition="nonretryable",
            )
        if seen_completed:
            raise AttemptFailure(
                "INVALID_EVENT_STREAM",
                "event appeared after turn.completed",
                retry_disposition="retryable",
            )

        if event_type == "thread.started":
            candidate_thread_id = event.get("thread_id")
            if seen_thread or not isinstance(candidate_thread_id, str):
                raise AttemptFailure(
                    "INVALID_EVENT_STREAM",
                    "thread.started is duplicate or malformed",
                    retry_disposition="retryable",
                )
            seen_thread = True
            thread_id = candidate_thread_id
        elif event_type == "turn.started":
            if not seen_thread or seen_turn:
                raise AttemptFailure(
                    "INVALID_EVENT_STREAM",
                    "turn.started is out of order or duplicate",
                    retry_disposition="retryable",
                )
            seen_turn = True
        elif event_type in {"item.started", "item.completed"}:
            if not seen_turn:
                raise AttemptFailure(
                    "INVALID_EVENT_STREAM",
                    "item event appeared before turn.started",
                    retry_disposition="retryable",
                )
            item = event.get("item")
            item_type = item.get("type") if isinstance(item, Mapping) else None
            if item_type in _TOOL_ITEM_TYPES:
                raise AttemptFailure(
                    "TOOL_POLLUTION",
                    f"forbidden Codex tool item {item_type!r}",
                    retry_disposition="nonretryable",
                )
            if item_type not in _ALLOWED_ITEM_TYPES:
                raise AttemptFailure(
                    "UNKNOWN_EVENT",
                    f"unrecognized Codex item type {item_type!r}",
                    retry_disposition="nonretryable",
                )
            if event_type == "item.completed" and item_type == "agent_message":
                text = item.get("text")
                if not isinstance(text, str):
                    raise AttemptFailure(
                        "INVALID_EVENT_STREAM",
                        "completed agent_message has no text",
                        retry_disposition="retryable",
                    )
                messages.append(text)
        elif event_type == "turn.completed":
            if not seen_turn:
                raise AttemptFailure(
                    "INVALID_EVENT_STREAM",
                    "turn.completed appeared before turn.started",
                    retry_disposition="retryable",
                )
            candidate_usage = event.get("usage", {})
            if not isinstance(candidate_usage, Mapping):
                raise AttemptFailure(
                    "INVALID_EVENT_STREAM",
                    "turn.completed usage is malformed",
                    retry_disposition="retryable",
                )
            usage = dict(candidate_usage)
            seen_completed = True
        elif event_type in {"turn.failed", "error"}:
            code = _classify_systematic_failure(
                json.dumps(event, ensure_ascii=True, sort_keys=True)
            )
            raise AttemptFailure(
                code or "CODEX_EVENT_ERROR",
                "Codex reported an error event",
                retry_disposition=(
                    "nonretryable" if code == "AUTH_ERROR" else "retryable"
                ),
            )

    if not seen_thread or not seen_turn or not seen_completed or len(messages) != 1:
        raise AttemptFailure(
            "TRUNCATED_STDOUT",
            "Codex event stream did not contain one complete final message",
            retry_disposition="retryable",
        )
    return CodexOutput(
        message_text=messages[0],
        usage=usage,
        thread_id=thread_id,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sensitive_values(environment: Mapping[str, str]) -> tuple[str, ...]:
    values = {
        value
        for name, value in environment.items()
        if len(value) >= 16
        and any(marker in name.upper() for marker in _SENSITIVE_NAME_MARKERS)
    }
    return tuple(sorted(values, key=len, reverse=True))


def _redact(value: str, sensitive_values: tuple[str, ...]) -> tuple[str, int]:
    redacted = value
    count = 0
    for secret in sensitive_values:
        occurrences = redacted.count(secret)
        if occurrences:
            redacted = redacted.replace(secret, "[REDACTED]")
            count += occurrences
    return redacted, count


def _subprocess_environment(source: Mapping[str, str]) -> dict[str, str]:
    environment: dict[str, str] = {}
    for name in _ALLOWED_ENVIRONMENT_NAMES:
        if any(marker in name.upper() for marker in _SENSITIVE_NAME_MARKERS):
            continue
        value = source.get(name)
        if value is not None:
            environment[name] = value
    environment.setdefault("PATH", os.defpath)
    environment["NO_COLOR"] = "1"
    return environment


def _terminate_process_group(
    process: subprocess.Popen[str],
) -> tuple[str, str]:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (PermissionError, ProcessLookupError):
        pass
    try:
        return process.communicate(timeout=1.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (PermissionError, ProcessLookupError):
            pass
        try:
            return process.communicate(timeout=1.0)
        except subprocess.TimeoutExpired as final_timeout:
            stdout = final_timeout.stdout or ""
            stderr = final_timeout.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
            return stdout, stderr


def _invoke_codex(
    *,
    executable: str | Path,
    schema_path: Path,
    prompt: str,
    timeout_seconds: float,
) -> _ProcessCapture:
    started = time.monotonic()
    stdout = ""
    stderr = ""
    returncode: int | None = None
    timed_out = False
    start_error: str | None = None
    argv: list[str] = []
    with tempfile.TemporaryDirectory(
        prefix="cofactor9.1-attempt-",
        dir="/private/tmp",
    ) as temporary_directory:
        working_directory = Path(temporary_directory)
        working_directory.chmod(0o700)
        argv = build_codex_argv(
            executable=executable,
            schema_path=schema_path,
            working_directory=working_directory,
        )
        try:
            process = subprocess.Popen(
                argv,
                cwd=working_directory,
                env=_subprocess_environment(os.environ),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                start_new_session=True,
            )
        except OSError as error:
            start_error = f"{type(error).__name__}: {error}"
        else:
            try:
                stdout, stderr = process.communicate(
                    input=prompt,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                timed_out = True
                stdout, stderr = _terminate_process_group(process)
            returncode = process.returncode
    return _ProcessCapture(
        argv=tuple(argv),
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
        timed_out=timed_out,
        start_error=start_error,
        duration_seconds=time.monotonic() - started,
    )


def _write_text_exclusive(path: Path, value: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(value)


def _write_json_exclusive(path: Path, value: Mapping[str, object]) -> None:
    _write_text_exclusive(
        path,
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n",
    )


def _attempt_numbers(attempts_directory: Path) -> tuple[int, ...]:
    if not attempts_directory.exists():
        return ()
    numbers: list[int] = []
    for path in attempts_directory.iterdir():
        name = path.name
        if (
            not path.is_dir()
            or not name.startswith("attempt-")
            or not name.removeprefix("attempt-").isdigit()
        ):
            raise RunnerError(f"unexpected append-only attempt entry {name!r}")
        numbers.append(int(name.removeprefix("attempt-")))
    if len(numbers) != len(set(numbers)):
        raise RunnerError("duplicate attempt numbers")
    return tuple(sorted(numbers))


def _load_terminal(path: Path, case: PromptCase) -> TerminalResult:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise RunnerError(f"terminal record is unreadable: {error}") from error
    if not isinstance(payload, Mapping) or payload.get("sample_id") != case.sample_id:
        raise RunnerError("terminal record does not match requested sample")
    expected_prompt_hash = _sha256_text(render_prompt(case))
    if payload.get("prompt_sha256") != expected_prompt_hash:
        raise RunnerError("terminal record prompt does not match requested case")
    status = payload.get("status")
    attempt_count = payload.get("attempt_count")
    if (
        isinstance(attempt_count, bool)
        or not isinstance(attempt_count, int)
        or attempt_count < 1
    ):
        raise RunnerError("terminal record has invalid attempt_count")
    if status == "success":
        try:
            prediction = validate_prediction(
                payload.get("prediction"),
                expected_sample_id=case.sample_id,
                allowed_labels=case.allowed_labels,
            )
        except PredictionValidationError as error:
            raise RunnerError(f"saved terminal prediction is invalid: {error}") from error
        return TerminalResult(
            sample_id=case.sample_id,
            status="success",
            attempt_count=attempt_count,
            prediction=prediction,
        )
    if status == "terminal_error":
        error_code = payload.get("error_code")
        error_message = payload.get("error_message")
        if not isinstance(error_code, str) or not isinstance(error_message, str):
            raise RunnerError("terminal error record is malformed")
        return TerminalResult(
            sample_id=case.sample_id,
            status="terminal_error",
            attempt_count=attempt_count,
            error_code=error_code,
            error_message=error_message,
        )
    raise RunnerError(f"terminal record has unknown status {status!r}")


def _validated_prediction(
    capture: _ProcessCapture,
    case: PromptCase,
) -> tuple[Prediction, CodexOutput]:
    if capture.start_error is not None:
        raise AttemptFailure(
            "PROCESS_START_ERROR",
            capture.start_error,
            retry_disposition="nonretryable",
        )
    output: CodexOutput | None = None
    outer_failure: AttemptFailure | None = None
    if capture.stdout.strip():
        try:
            output = parse_codex_stdout(capture.stdout)
        except AttemptFailure as failure:
            if failure.retry_disposition == "nonretryable":
                raise
            outer_failure = failure

    if capture.timed_out:
        raise AttemptFailure(
            "TIMEOUT",
            "Codex attempt exceeded its deadline",
            retry_disposition="retryable",
        )
    if capture.returncode != 0:
        systematic_code = _classify_systematic_failure(
            f"{capture.stdout}\n{capture.stderr}"
        )
        raise AttemptFailure(
            systematic_code or "PROCESS_EXIT",
            f"Codex exited with status {capture.returncode}",
            retry_disposition=(
                "nonretryable"
                if systematic_code == "AUTH_ERROR"
                else "retryable"
            ),
        )
    if outer_failure is not None:
        raise outer_failure
    if output is None:
        output = parse_codex_stdout(capture.stdout)
    try:
        prediction = parse_prediction_json(
            output.message_text,
            expected_sample_id=case.sample_id,
            allowed_labels=case.allowed_labels,
        )
    except PredictionValidationError as error:
        message = str(error)
        code = (
            "CASE_MISMATCH"
            if "sample_id" in message and "does not match" in message
            else "INVALID_PREDICTION"
        )
        raise AttemptFailure(
            code,
            message,
            retry_disposition="retryable",
        ) from error
    return prediction, output


def build_codex_argv(
    *,
    executable: str | Path,
    schema_path: Path,
    working_directory: Path,
) -> list[str]:
    """Build the fixed, shell-free, least-capability Codex command."""

    argv = [str(executable), "exec"]
    for feature in DISABLED_FEATURES:
        argv.extend(("--disable", feature))
    argv.extend(
        (
            "--model",
            MODEL,
            "-c",
            'model_reasoning_effort="max"',
            "-c",
            'service_tier="fast"',
            "-c",
            'approval_policy="never"',
            "-c",
            'web_search="disabled"',
            "--enable",
            "fast_mode",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "--json",
            "--output-schema",
            str(schema_path.resolve()),
            "-C",
            str(working_directory.resolve()),
            "-",
        )
    )
    return argv


class CodexExecRunner:
    """Run validated prompt cases through a fixed Codex exec adapter."""

    def __init__(
        self,
        *,
        run_dir: Path,
        executable: str | Path = "codex",
        schema_path: Path | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        timeout_seconds: float = 600.0,
        circuit_breaker_threshold: int = DEFAULT_CIRCUIT_BREAKER_THRESHOLD,
    ) -> None:
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= MAX_ATTEMPTS
        ):
            raise ValueError(f"max_attempts must be from 1 through {MAX_ATTEMPTS}")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        if (
            isinstance(circuit_breaker_threshold, bool)
            or not isinstance(circuit_breaker_threshold, int)
            or circuit_breaker_threshold < 1
        ):
            raise ValueError("circuit_breaker_threshold must be a positive integer")
        default_schema = (
            Path(__file__).parents[1] / "schemas" / "model-response.schema.json"
        )
        self.run_dir = Path(run_dir)
        self.executable = executable
        self.schema_path = Path(schema_path) if schema_path else default_schema
        self.max_attempts = max_attempts
        self.timeout_seconds = float(timeout_seconds)
        self.circuit_breaker_threshold = circuit_breaker_threshold

    def run_case(
        self,
        case: PromptCase,
        *,
        resume: bool = False,
    ) -> TerminalResult:
        if not isinstance(case, PromptCase):
            raise TypeError("case must be a validated PromptCase")
        if not self.schema_path.is_file():
            raise RunnerError(f"response schema does not exist: {self.schema_path}")

        case_directory = self.run_dir / "cases" / case.sample_id
        terminal_path = case_directory / "terminal.json"
        if terminal_path.exists():
            if not resume:
                raise CompletedCaseError(
                    f"case {case.sample_id} already has a terminal record"
                )
            return _load_terminal(terminal_path, case)

        attempts_directory = case_directory / "attempts"
        existing_attempts = _attempt_numbers(attempts_directory)
        if existing_attempts and not resume:
            raise RunnerError(
                f"case {case.sample_id} has existing attempts; resume is required"
            )
        if existing_attempts:
            expected = tuple(range(1, max(existing_attempts) + 1))
            if existing_attempts != expected:
                raise RunnerError("append-only attempt sequence has a gap")

        prompt = render_prompt(case)
        sensitive_values = _sensitive_values(os.environ)
        persisted_prompt, prompt_redactions = _redact(prompt, sensitive_values)
        for existing_attempt in existing_attempts:
            saved_prompt_path = (
                attempts_directory
                / f"attempt-{existing_attempt:04d}"
                / "prompt.txt"
            )
            if (
                saved_prompt_path.exists()
                and saved_prompt_path.read_text(encoding="utf-8") != persisted_prompt
            ):
                raise RunnerError(
                    "existing attempt prompt does not match requested case"
                )

        attempts_directory.mkdir(parents=True, exist_ok=True)
        attempt_number = existing_attempts[-1] if existing_attempts else 0
        if attempt_number >= self.max_attempts:
            result = TerminalResult(
                sample_id=case.sample_id,
                status="terminal_error",
                attempt_count=attempt_number,
                error_code="ATTEMPTS_EXHAUSTED",
                error_message="maximum attempts already exhausted",
            )
            self._write_terminal(terminal_path, case, result)
            return result

        last_failure: AttemptFailure | None = None
        while attempt_number < self.max_attempts:
            attempt_number += 1
            attempt_directory = attempts_directory / f"attempt-{attempt_number:04d}"
            attempt_directory.mkdir(parents=False, exist_ok=False)
            _write_text_exclusive(attempt_directory / "prompt.txt", persisted_prompt)
            started_at = _utc_now()
            capture = _invoke_codex(
                executable=self.executable,
                schema_path=self.schema_path,
                prompt=prompt,
                timeout_seconds=self.timeout_seconds,
            )
            completed_at = _utc_now()
            stdout, stdout_redactions = _redact(
                capture.stdout,
                sensitive_values,
            )
            stderr, stderr_redactions = _redact(
                capture.stderr,
                sensitive_values,
            )
            _write_text_exclusive(attempt_directory / "stdout.jsonl", stdout)
            _write_text_exclusive(attempt_directory / "stderr.txt", stderr)

            prediction: Prediction | None = None
            output: CodexOutput | None = None
            try:
                prediction, output = _validated_prediction(capture, case)
            except AttemptFailure as failure:
                safe_message, message_redactions = _redact(
                    str(failure),
                    sensitive_values,
                )
                last_failure = AttemptFailure(
                    failure.code,
                    safe_message,
                    retry_disposition=failure.retry_disposition,
                )
                error_code: str | None = last_failure.code
                error_message: str | None = safe_message
                retry_disposition = last_failure.retry_disposition
            else:
                message_redactions = 0
                error_code = None
                error_message = None
                retry_disposition = "none"
                _write_json_exclusive(
                    attempt_directory / "prediction.json",
                    prediction.to_dict(),
                )

            attempt_record: dict[str, object] = {
                "schema_version": "cofactor9.1.attempt.v1",
                "sample_id": case.sample_id,
                "attempt_number": attempt_number,
                "started_at": started_at,
                "completed_at": completed_at,
                "duration_seconds": round(capture.duration_seconds, 6),
                "argv": list(capture.argv),
                "model": MODEL,
                "reasoning_effort": REASONING_EFFORT,
                "service_tier": SERVICE_TIER,
                "returncode": capture.returncode,
                "timed_out": capture.timed_out,
                "error_code": error_code,
                "error_message": error_message,
                "retry_disposition": retry_disposition,
                "thread_id": output.thread_id if output else None,
                "usage": output.usage if output else {},
                "prompt_sha256": _sha256_text(persisted_prompt),
                "stdout_sha256": _sha256_text(stdout),
                "stderr_sha256": _sha256_text(stderr),
                "redaction_count": (
                    prompt_redactions
                    + stdout_redactions
                    + stderr_redactions
                    + message_redactions
                ),
                "environment_policy": "fixed-allowlist",
            }
            _write_json_exclusive(
                attempt_directory / "attempt.json",
                attempt_record,
            )

            if prediction is not None:
                result = TerminalResult(
                    sample_id=case.sample_id,
                    status="success",
                    attempt_count=attempt_number,
                    prediction=prediction,
                )
                self._write_terminal(terminal_path, case, result)
                return result
            if last_failure is not None and last_failure.retry_disposition == "nonretryable":
                break

        if last_failure is None:
            raise RunnerError("runner exhausted attempts without a classified outcome")
        result = TerminalResult(
            sample_id=case.sample_id,
            status="terminal_error",
            attempt_count=attempt_number,
            error_code=last_failure.code,
            error_message=str(last_failure),
        )
        self._write_terminal(terminal_path, case, result)
        return result

    def run_cases(
        self,
        cases: list[PromptCase] | tuple[PromptCase, ...],
        *,
        resume: bool = False,
        concurrency: int = 1,
    ) -> tuple[TerminalResult, ...]:
        """Run a prevalidated batch with bounded, order-preserving concurrency."""

        if (
            isinstance(concurrency, bool)
            or not isinstance(concurrency, int)
            or not 1 <= concurrency <= MAX_CONCURRENCY
        ):
            raise ValueError(
                f"concurrency must be an integer from 1 through {MAX_CONCURRENCY}"
            )
        if not isinstance(cases, (list, tuple)):
            raise TypeError("cases must be a list or tuple of PromptCase values")

        validated_cases = tuple(cases)
        seen: set[str] = set()
        for index, case in enumerate(validated_cases):
            if not isinstance(case, PromptCase):
                raise TypeError(
                    f"cases[{index}] must be a validated PromptCase"
                )
            if case.sample_id in seen:
                raise RunnerError(f"duplicate sample_id {case.sample_id!r}")
            seen.add(case.sample_id)
        if not self.schema_path.is_file():
            raise RunnerError(f"response schema does not exist: {self.schema_path}")
        if not validated_cases:
            return ()

        terminal_existed = tuple(
            (
                self.run_dir
                / "cases"
                / case.sample_id
                / "terminal.json"
            ).exists()
            for case in validated_cases
        )
        results_by_index: dict[int, TerminalResult] = {}
        failures_by_index: dict[int, CaseRunFailure] = {}
        futures: dict[Future[TerminalResult], int] = {}
        next_index = 0
        streak_code: str | None = None
        streak_count = 0
        aborted_code: str | None = None

        def submit_until_full(executor: ThreadPoolExecutor) -> None:
            nonlocal next_index
            while (
                aborted_code is None
                and len(futures) < concurrency
                and next_index < len(validated_cases)
                and not (streak_count > 0 and futures)
            ):
                index = next_index
                next_index += 1
                future = executor.submit(
                    self.run_case,
                    validated_cases[index],
                    resume=resume,
                )
                futures[future] = index

        with ThreadPoolExecutor(
            max_workers=concurrency,
            thread_name_prefix="cofactor-case",
        ) as executor:
            submit_until_full(executor)
            while futures:
                done, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
                completed = sorted(done, key=futures.__getitem__)
                for future in completed:
                    index = futures.pop(future)
                    case = validated_cases[index]
                    try:
                        result = future.result()
                    except Exception as error:
                        failures_by_index[index] = CaseRunFailure(
                            sample_id=case.sample_id,
                            error=error,
                        )
                        streak_code = None
                        streak_count = 0
                        continue

                    results_by_index[index] = result
                    error_code = result.error_code
                    is_new_systematic_failure = (
                        not terminal_existed[index]
                        and result.status == "terminal_error"
                        and error_code in CIRCUIT_BREAKER_ERROR_CODES
                    )
                    if not is_new_systematic_failure:
                        streak_code = None
                        streak_count = 0
                        continue
                    if error_code == streak_code:
                        streak_count += 1
                    else:
                        streak_code = error_code
                        streak_count = 1
                    if (
                        streak_count >= self.circuit_breaker_threshold
                        and aborted_code is None
                    ):
                        aborted_code = error_code

                submit_until_full(executor)

        completed_results = tuple(
            results_by_index[index] for index in sorted(results_by_index)
        )
        failures = tuple(
            failures_by_index[index] for index in sorted(failures_by_index)
        )
        if aborted_code is not None:
            raise RunAborted(
                error_code=aborted_code,
                threshold=self.circuit_breaker_threshold,
                completed_results=completed_results,
                failures=failures,
                pending_sample_ids=tuple(
                    case.sample_id for case in validated_cases[next_index:]
                ),
            )
        if failures:
            raise RunCasesError(
                completed_results=completed_results,
                failures=failures,
            )
        return tuple(results_by_index[index] for index in range(len(validated_cases)))

    def _write_terminal(
        self,
        terminal_path: Path,
        case: PromptCase,
        result: TerminalResult,
    ) -> None:
        payload: dict[str, object] = {
            "schema_version": "cofactor9.1.terminal.v1",
            "sample_id": result.sample_id,
            "status": result.status,
            "attempt_count": result.attempt_count,
            "prediction": result.prediction.to_dict() if result.prediction else None,
            "error_code": result.error_code,
            "error_message": result.error_message,
            "completed_at": _utc_now(),
            "model": MODEL,
            "reasoning_effort": REASONING_EFFORT,
            "service_tier": SERVICE_TIER,
            "prompt_version": PROMPT_VERSION,
            "catalog_version": case.catalog_version,
            "prompt_sha256": _sha256_text(render_prompt(case)),
        }
        try:
            _write_json_exclusive(terminal_path, payload)
        except FileExistsError as error:
            raise CompletedCaseError(
                f"terminal record already exists for {case.sample_id}"
            ) from error


__all__ = [
    "CIRCUIT_BREAKER_ERROR_CODES",
    "CaseRunFailure",
    "CodexExecRunner",
    "CompletedCaseError",
    "DEFAULT_CIRCUIT_BREAKER_THRESHOLD",
    "DISABLED_FEATURES",
    "MAX_ATTEMPTS",
    "MAX_CONCURRENCY",
    "MODEL",
    "REASONING_EFFORT",
    "RunAborted",
    "RunCasesError",
    "RunnerError",
    "SERVICE_TIER",
    "TerminalResult",
    "build_codex_argv",
]
