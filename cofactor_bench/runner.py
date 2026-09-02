"""Hardened, append-only Codex exec transport for sequence-only cases."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Iterator, Literal

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
DEFAULT_CIRCUIT_BREAKER_THRESHOLD = 1

INFRASTRUCTURE_INCIDENT_CODES = frozenset(
    {
        "AUTH_ERROR",
        "CAPACITY_ERROR",
        "PROCESS_START_ERROR",
        "TRANSPORT_ERROR",
    }
)
SYSTEMATIC_FAILURE_CODES = INFRASTRUCTURE_INCIDENT_CODES | {
    "CODEX_EVENT_ERROR",
    "PROCESS_EXIT",
    "TIMEOUT",
}
CIRCUIT_BREAKER_ERROR_CODES = SYSTEMATIC_FAILURE_CODES

_NON_BUDGET_ERROR_CODES = INFRASTRUCTURE_INCIDENT_CODES | {
    "RUN_CANCELLED",
}

_ATTEMPT_REQUIRED_FILES = frozenset(
    {"attempt.json", "prompt.txt", "stdout.jsonl", "stderr.txt"}
)
_ATTEMPT_ALLOWED_FILES = _ATTEMPT_REQUIRED_FILES | {"prediction.json"}
_ATTEMPT_FIELDS = frozenset(
    {
        "schema_version",
        "sample_id",
        "attempt_number",
        "started_at",
        "completed_at",
        "duration_seconds",
        "argv",
        "model",
        "reasoning_effort",
        "service_tier",
        "returncode",
        "timed_out",
        "error_code",
        "error_message",
        "retry_disposition",
        "thread_id",
        "usage",
        "prompt_sha256",
        "stdout_sha256",
        "stderr_sha256",
        "redaction_count",
        "environment_policy",
    }
)
_INCIDENT_REQUIRED_FILES = frozenset(
    {"incident.json", "prompt.txt", "stdout.jsonl", "stderr.txt"}
)
_INCIDENT_ALLOWED_FILES = _INCIDENT_REQUIRED_FILES | {"prediction.json"}
_INCIDENT_FIELDS = frozenset(
    {
        "schema_version",
        "sample_id",
        "incident_number",
        "tentative_attempt_number",
        "started_at",
        "completed_at",
        "duration_seconds",
        "argv",
        "model",
        "reasoning_effort",
        "service_tier",
        "returncode",
        "timed_out",
        "cancelled",
        "start_error",
        "error_code",
        "error_message",
        "retry_disposition",
        "thread_id",
        "usage",
        "prompt_sha256",
        "stdout_sha256",
        "stderr_sha256",
        "prediction_sha256",
        "redaction_count",
        "environment_policy",
    }
)
_TERMINAL_FIELDS = frozenset(
    {
        "schema_version",
        "sample_id",
        "status",
        "attempt_count",
        "prediction",
        "error_code",
        "error_message",
        "completed_at",
        "model",
        "reasoning_effort",
        "service_tier",
        "prompt_version",
        "catalog_version",
        "prompt_sha256",
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
    "sleep_tool",
    "tool_suggest",
    "code_mode_host",
    "in_app_browser",
)


_DURABLE_LAUNCHER_SOURCE = r'''import datetime
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

gate_fd = int(sys.argv[1])
config = json.loads(sys.argv[2])
cancelled = False
child = None


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def request_cancel(_signum, _frame):
    global cancelled
    cancelled = True


def publish_witness(payload, destination_name, temporary_name):
    destination = Path(destination_name)
    temporary = Path(temporary_name)
    raw = (
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        temporary,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def stop_child(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (PermissionError, ProcessLookupError):
        pass
    try:
        return process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (PermissionError, ProcessLookupError):
            pass
        return process.wait()


def ensure_group_empty(process_group_id):
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return
    except PermissionError as error:
        raise RuntimeError("model process group cannot be inspected") from error
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    raise RuntimeError("model process group survived termination")


signal.signal(signal.SIGTERM, request_cancel)
signal.signal(signal.SIGINT, request_cancel)
token = b""
while len(token) < 1:
    chunk = os.read(gate_fd, 1 - len(token))
    if not chunk:
        os.close(gate_fd)
        raise SystemExit(75)
    token += chunk
extra = os.read(gate_fd, 1)
os.close(gate_fd)
if token != b"1" or extra:
    raise SystemExit(76)

started_at = utc_now()
started = time.monotonic()
returncode = None
timed_out = False
start_error = None
prompt_fd = None
stdout_fd = None
stderr_fd = None
try:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    prompt_fd = os.open(config["prompt_path"], os.O_RDONLY | nofollow)
    stdout_fd = os.open(config["stdout_path"], os.O_WRONLY | os.O_TRUNC | nofollow)
    stderr_fd = os.open(config["stderr_path"], os.O_WRONLY | os.O_TRUNC | nofollow)
    if cancelled:
        pass
    else:
        model_gate_read, model_gate_write = os.pipe()
        exec_gate_source = (
            "import json,os,sys;"
            "f=int(sys.argv[1]);b=os.read(f,1);x=os.read(f,1);os.close(f);"
            "a=json.loads(sys.argv[2]);"
            "sys.exit(75) if b!=b'1' or x else os.execvpe(a[0],a,os.environ)"
        )
        try:
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    exec_gate_source,
                    str(model_gate_read),
                    json.dumps(config["argv"], separators=(",", ":")),
                ],
                cwd=config["working_directory"],
                env=os.environ,
                stdin=prompt_fd,
                stdout=stdout_fd,
                stderr=stderr_fd,
                shell=False,
                start_new_session=True,
                close_fds=True,
                pass_fds=(model_gate_read,),
            )
        except OSError as error:
            os.close(model_gate_read)
            os.close(model_gate_write)
            start_error = type(error).__name__ + ": " + str(error)
        else:
            os.close(model_gate_read)
            try:
                publish_witness(
                    {
                        "schema_version": "cofactor9.1.launch-started.v1",
                        "sample_id": config["sample_id"],
                        "attempt_number": config["attempt_number"],
                        "launch_id": config["launch_id"],
                        "launcher_pid": os.getpid(),
                        "model_pid": child.pid,
                        "prompt_sha256": config["prompt_sha256"],
                        "argv": config["argv"],
                        "started_at": started_at,
                    },
                    config["started_path"],
                    config["started_temporary_path"],
                )
                if not cancelled:
                    os.write(model_gate_write, b"1")
            finally:
                os.close(model_gate_write)
            deadline = time.monotonic() + float(config["timeout_seconds"])
            while child.poll() is None:
                if cancelled:
                    returncode = stop_child(child)
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    returncode = stop_child(child)
                    break
                try:
                    returncode = child.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    continue
            if returncode is None:
                returncode = child.returncode
            ensure_group_empty(child.pid)
finally:
    for descriptor in (stdout_fd, stderr_fd):
        if descriptor is not None:
            os.fsync(descriptor)
    for descriptor in (prompt_fd, stdout_fd, stderr_fd):
        if descriptor is not None:
            os.close(descriptor)

completed_at = utc_now()
duration = time.monotonic() - started
if not math.isfinite(duration) or duration < 0:
    raise SystemExit(77)
publish_witness(
    {
        "schema_version": "cofactor9.1.launch-completion.v1",
        "sample_id": config["sample_id"],
        "attempt_number": config["attempt_number"],
        "launch_id": config["launch_id"],
        "launcher_pid": os.getpid(),
        "model_pid": child.pid if child is not None else None,
        "prompt_sha256": config["prompt_sha256"],
        "argv": config["argv"],
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_seconds": round(duration, 6),
        "returncode": returncode,
        "timed_out": timed_out,
        "cancelled": cancelled and not timed_out,
        "start_error": start_error,
    },
    config["completion_path"],
    config["completion_temporary_path"],
)
'''


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
class AttemptReplayResult:
    """Pure classification of one already-captured Codex invocation."""

    prediction: Prediction | None
    error_code: str | None
    error_message: str | None
    retry_disposition: Literal["none", "retryable", "nonretryable"]
    thread_id: str | None
    usage: dict[str, object]


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


class SystematicFailure(RunnerError):
    """One persisted infrastructure failure that must abort batch retries."""

    def __init__(self, *, sample_id: str, error_code: str, message: str) -> None:
        self.sample_id = sample_id
        self.error_code = error_code
        super().__init__(message)


class RunCancelled(RunCasesError):
    """A signal- or caller-cancelled batch whose incomplete cases are resumable."""

    def __init__(
        self,
        *,
        signal_number: int | None,
        completed_results: tuple[TerminalResult, ...],
        failures: tuple[CaseRunFailure, ...],
        pending_sample_ids: tuple[str, ...],
    ) -> None:
        self.signal_number = signal_number
        self.pending_sample_ids = pending_sample_ids
        reason = (
            f"signal {signal_number}"
            if signal_number is not None
            else "batch cancellation"
        )
        super().__init__(
            completed_results=completed_results,
            failures=failures,
            message=(
                f"run cancelled by {reason}; {len(pending_sample_ids)} case(s) "
                "remain resumable"
            ),
        )


class _CaseCancelled(RunnerError):
    """Internal worker sentinel emitted only after its attempt ledger is sealed."""


@dataclass(frozen=True, slots=True)
class _ProcessCapture:
    argv: tuple[str, ...]
    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool
    cancelled: bool
    start_error: str | None
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class _DurableCapture:
    capture: _ProcessCapture
    started_at: str
    completed_at: str


@dataclass(frozen=True, slots=True)
class _AttemptState:
    number: int
    error_code: str | None
    error_message: str | None
    retry_disposition: Literal["none", "retryable", "nonretryable"]
    prediction: Prediction | None


@dataclass(frozen=True, slots=True)
class _ResumePlan:
    case: PromptCase
    persisted_prompt: str
    prompt_redactions: int
    next_attempt_number: int
    budget_failures: int


class _ProcessSupervisor:
    """Thread-safe registry used to terminate every active process group."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[int, subprocess.Popen[str]] = {}

    def register(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._processes[process.pid] = process

    def unregister(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._processes.pop(process.pid, None)

    def terminate_all(self) -> None:
        with self._lock:
            processes = tuple(self._processes.values())
        for process in processes:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (PermissionError, ProcessLookupError):
                pass


class _BatchControl:
    """One batch-wide stop signal shared by the scheduler and every worker."""

    def __init__(self, supervisor: _ProcessSupervisor) -> None:
        self.event = threading.Event()
        self._supervisor = supervisor
        self._lock = threading.Lock()
        self.systematic_code: str | None = None
        self.signal_number: int | None = None

    def abort_systematic(self, code: str) -> None:
        with self._lock:
            if self.systematic_code is None and self.signal_number is None:
                self.systematic_code = code
            self.event.set()
        self._supervisor.terminate_all()

    def cancel(self, *, signal_number: int | None = None) -> None:
        with self._lock:
            if signal_number is not None and self.signal_number is None:
                self.signal_number = signal_number
            self.event.set()
        self._supervisor.terminate_all()


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


def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _decode_strict_json_object(value: bytes, *, location: str) -> Mapping[str, object]:
    def reject_duplicate_keys(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise RunnerError(f"{location} has duplicate key {key!r}")
            result[key] = item
        return result

    def reject_nonfinite_number(raw: str) -> object:
        raise RunnerError(f"{location} contains non-finite number {raw!r}")

    try:
        decoded = value.decode("utf-8")
        payload = json.loads(
            decoded,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_nonfinite_number,
        )
    except RunnerError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise RunnerError(f"{location} is unreadable: {error}") from error
    if not isinstance(payload, Mapping):
        raise RunnerError(f"{location} is not a JSON object")
    return payload


def _parse_utc_timestamp(value: object, *, location: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RunnerError(f"{location} is not a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise RunnerError(f"{location} is not a UTC timestamp") from error
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise RunnerError(f"{location} is not a UTC timestamp")
    return parsed


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


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


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_directory(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise RunnerError(f"unsafe directory path: {path}")
        return
    parent = path.parent
    if parent != path:
        _ensure_directory(parent)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        if path.is_symlink() or not path.is_dir():
            raise RunnerError(f"unsafe directory path: {path}")
    else:
        _fsync_directory(parent)


def _publish_bytes_exclusive(
    path: Path,
    value: bytes,
    *,
    staging_directory: Path,
) -> None:
    """Durably publish complete bytes with an atomic no-clobber hard link."""

    _ensure_directory(path.parent)
    _ensure_directory(staging_directory)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="publish-",
        suffix=".tmp",
        dir=staging_directory,
        text=False,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass


def _publish_text_exclusive(
    path: Path,
    value: str,
    *,
    staging_directory: Path,
) -> None:
    _publish_bytes_exclusive(
        path,
        value.encode("utf-8"),
        staging_directory=staging_directory,
    )


def _publish_json_exclusive(
    path: Path,
    value: Mapping[str, object],
    *,
    staging_directory: Path,
) -> None:
    _publish_bytes_exclusive(
        path,
        _canonical_json_bytes(value),
        staging_directory=staging_directory,
    )


def _publish_directory_exclusive(
    source: Path,
    destination: Path,
    *,
    staging_directory: Path,
) -> None:
    """Atomically publish one fully-fsynced directory under a process lock."""

    if source.is_symlink() or not source.is_dir():
        raise RunnerError("directory publish source is unsafe")
    _ensure_directory(destination.parent)
    _ensure_directory(staging_directory)
    lock_path = staging_directory / ".directory-publish.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            os.lstat(destination)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(destination)
        for entry in sorted(source.iterdir(), key=lambda candidate: candidate.name):
            if entry.is_symlink() or not entry.is_file():
                raise RunnerError(
                    f"directory publish contains unsafe artifact {entry.name!r}"
                )
            artifact_descriptor = os.open(entry, os.O_RDONLY)
            try:
                os.fsync(artifact_descriptor)
            finally:
                os.close(artifact_descriptor)
        _fsync_directory(source)
        os.rename(source, destination)
        _fsync_directory(destination.parent)
        _fsync_directory(source.parent)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _active_marker_path(
    run_dir: Path,
    sample_id: str,
    attempt_number: int,
) -> Path:
    return (
        run_dir
        / ".active"
        / f"{sample_id}.attempt-{attempt_number:04d}.json"
    )


def _launch_reservation_path(
    run_dir: Path,
    sample_id: str,
    attempt_number: int,
) -> Path:
    return (
        run_dir
        / ".active"
        / f"{sample_id}.attempt-{attempt_number:04d}.reservation.json"
    )


def _launch_completion_path(
    run_dir: Path,
    sample_id: str,
    attempt_number: int,
) -> Path:
    return (
        run_dir
        / ".active"
        / f"{sample_id}.attempt-{attempt_number:04d}.completion.json"
    )


def _launch_started_path(
    run_dir: Path,
    sample_id: str,
    attempt_number: int,
) -> Path:
    return (
        run_dir
        / ".active"
        / f"{sample_id}.attempt-{attempt_number:04d}.started.json"
    )


def _launch_witness_temporary_path(
    run_dir: Path,
    sample_id: str,
    attempt_number: int,
    launch_id: str,
    kind: Literal["started", "completion"],
) -> Path:
    return (
        run_dir
        / ".staging"
        / (
            f"publish-launch-{sample_id}.attempt-{attempt_number:04d}."
            f"{launch_id}.{kind}.tmp"
        )
    )


def _staged_inflight_directory(
    run_dir: Path,
    sample_id: str,
    attempt_number: int,
) -> Path:
    return (
        run_dir
        / ".staging"
        / f"inflight-{sample_id}.attempt-{attempt_number:04d}"
    )


def _remove_operational_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


def _remove_active_state(
    run_dir: Path,
    sample_id: str,
    attempt_number: int,
) -> None:
    reservation = _launch_reservation_path(run_dir, sample_id, attempt_number)
    launch_id: str | None = None
    if reservation.is_file() and not reservation.is_symlink():
        try:
            candidate = _decode_strict_json_object(
                reservation.read_bytes(),
                location="launch reservation",
            ).get("launch_id")
        except RunnerError:
            candidate = None
        if isinstance(candidate, str):
            launch_id = candidate
    for path in (
        _launch_completion_path(run_dir, sample_id, attempt_number),
        _launch_started_path(run_dir, sample_id, attempt_number),
        _active_marker_path(run_dir, sample_id, attempt_number),
        reservation,
    ):
        _remove_operational_file(path)
    if launch_id is not None:
        for kind in ("started", "completion"):
            _remove_operational_file(
                _launch_witness_temporary_path(
                    run_dir,
                    sample_id,
                    attempt_number,
                    launch_id,
                    kind,
                )
            )


def _replace_operational_bytes(
    path: Path,
    value: bytes,
    *,
    expected: bytes,
    staging_directory: Path,
) -> None:
    """Atomically replace an unpublished capture file after exact-byte checking."""

    if path.is_symlink() or not path.is_file():
        raise RunnerError(f"operational capture path is unsafe: {path}")
    if path.read_bytes() != expected:
        raise RunnerError("operational capture changed during finalization")
    _ensure_directory(staging_directory)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="replace-",
        suffix=".tmp",
        dir=staging_directory,
        text=False,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _terminate_process_group(
    process: subprocess.Popen[str],
) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (PermissionError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=1.0)
        return
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (PermissionError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                raise RunnerError(
                    f"launcher process group {process.pid} could not be reaped"
                )


def _invoke_codex(
    *,
    executable: str | Path,
    schema_path: Path,
    prompt: str,
    timeout_seconds: float,
    run_dir: Path,
    sample_id: str,
    attempt_number: int,
    staging_directory: Path,
    supervisor: _ProcessSupervisor,
    cancellation_event: threading.Event | None,
) -> _DurableCapture:
    started = time.monotonic()
    started_at = _utc_now()
    argv: list[str] = []
    active_marker = _active_marker_path(run_dir, sample_id, attempt_number)
    reservation = _launch_reservation_path(run_dir, sample_id, attempt_number)
    completion = _launch_completion_path(run_dir, sample_id, attempt_number)
    launch_started = _launch_started_path(run_dir, sample_id, attempt_number)
    inflight = _inflight_directory(run_dir, sample_id)
    prompt_path = inflight / "prompt.txt"
    stdout_path = inflight / "stdout.jsonl"
    stderr_path = inflight / "stderr.txt"
    launch_id = secrets.token_hex(16)
    prompt_sha256 = _sha256_text(prompt)
    _publish_json_exclusive(
        reservation,
        {
            "schema_version": "cofactor9.1.launch-reservation.v1",
            "sample_id": sample_id,
            "attempt_number": attempt_number,
            "launch_id": launch_id,
            "prompt_sha256": prompt_sha256,
            "reserved_at": started_at,
        },
        staging_directory=staging_directory,
    )
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
        gate_read, gate_write = os.pipe()
        launcher_config = {
            "sample_id": sample_id,
            "attempt_number": attempt_number,
            "launch_id": launch_id,
            "prompt_sha256": prompt_sha256,
            "argv": argv,
            "working_directory": str(working_directory),
            "prompt_path": str(prompt_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "started_path": str(launch_started),
            "started_temporary_path": str(
                _launch_witness_temporary_path(
                    run_dir,
                    sample_id,
                    attempt_number,
                    launch_id,
                    "started",
                )
            ),
            "completion_path": str(completion),
            "completion_temporary_path": str(
                _launch_witness_temporary_path(
                    run_dir,
                    sample_id,
                    attempt_number,
                    launch_id,
                    "completion",
                )
            ),
            "timeout_seconds": timeout_seconds,
        }
        launcher_argv = [
            sys.executable,
            "-c",
            _DURABLE_LAUNCHER_SOURCE,
            str(gate_read),
            json.dumps(
                launcher_config,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ]
        try:
            process = subprocess.Popen(
                launcher_argv,
                cwd=working_directory,
                env=_subprocess_environment(os.environ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                start_new_session=True,
                pass_fds=(gate_read,),
            )
        except OSError as error:
            os.close(gate_read)
            os.close(gate_write)
            completed_at = _utc_now()
            return _DurableCapture(
                capture=_ProcessCapture(
                    argv=tuple(argv),
                    stdout="",
                    stderr="",
                    returncode=None,
                    timed_out=False,
                    cancelled=False,
                    start_error=f"{type(error).__name__}: {error}",
                    duration_seconds=time.monotonic() - started,
                ),
                started_at=started_at,
                completed_at=completed_at,
            )
        else:
            os.close(gate_read)
            supervisor.register(process)
            try:
                committed_at = _utc_now()
                _publish_json_exclusive(
                    active_marker,
                    {
                        "schema_version": "cofactor9.1.active-process.v2",
                        "sample_id": sample_id,
                        "attempt_number": attempt_number,
                        "launch_id": launch_id,
                        "launcher_pid": process.pid,
                        "prompt_sha256": prompt_sha256,
                        "argv": argv,
                        "timeout_seconds": timeout_seconds,
                        "committed_at": committed_at,
                    },
                    staging_directory=staging_directory,
                )
                os.write(gate_write, b"1")
                os.close(gate_write)
                gate_write = -1
                emergency_deadline = time.monotonic() + timeout_seconds + 3.0
                cancellation_sent = False
                while process.poll() is None:
                    if (
                        cancellation_event is not None
                        and cancellation_event.is_set()
                        and not cancellation_sent
                    ):
                        cancellation_sent = True
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                        except (PermissionError, ProcessLookupError):
                            pass
                    if time.monotonic() >= emergency_deadline:
                        _terminate_process_group(process)
                        break
                    time.sleep(0.02)
            except BaseException:
                if gate_write >= 0:
                    os.close(gate_write)
                _terminate_process_group(process)
                raise
            finally:
                supervisor.unregister(process)
            if process.poll() is None:
                raise RunnerError(
                    f"launcher process group {process.pid} could not be reaped; "
                    "resume is unsafe"
                )
            if completion.is_file():
                return _load_launch_completion(
                    completion_path=completion,
                    active_marker=active_marker,
                    reservation_path=reservation,
                    started_path=launch_started,
                    case_sample_id=sample_id,
                    attempt_number=attempt_number,
                    prompt_sha256=prompt_sha256,
                    launcher_pid=process.pid,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                )
            raise RunnerError(
                "launcher exited without a durable completion; resume is required"
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


def _incident_numbers(incidents_directory: Path) -> tuple[int, ...]:
    if not incidents_directory.exists():
        return ()
    numbers: list[int] = []
    for path in incidents_directory.iterdir():
        suffix = path.name.removeprefix("incident-")
        if (
            path.is_symlink()
            or not path.is_dir()
            or not suffix.isdigit()
            or path.name != f"incident-{int(suffix):04d}"
        ):
            raise RunnerError(
                f"unexpected append-only transport incident entry {path.name!r}"
            )
        numbers.append(int(suffix))
    expected = list(range(1, len(numbers) + 1))
    if sorted(numbers) != expected:
        raise RunnerError("append-only transport incident sequence has a gap")
    return tuple(sorted(numbers))


def _inflight_directory(run_dir: Path, sample_id: str) -> Path:
    return run_dir / ".inflight" / sample_id


def _discard_staged_inflight(path: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink() or not path.is_dir():
        raise RunnerError("staged in-flight invocation path is unsafe")
    allowed = {"prompt.txt", "stdout.jsonl", "stderr.txt"}
    for entry in path.iterdir():
        if (
            entry.is_symlink()
            or not entry.is_file()
            or entry.name not in allowed
        ):
            raise RunnerError(
                f"unsafe staged in-flight artifact {entry.name!r}"
            )
    for entry in sorted(path.iterdir(), key=lambda candidate: candidate.name):
        entry.unlink()
    path.rmdir()
    _fsync_directory(path.parent)


def _create_inflight_directory(
    *,
    run_dir: Path,
    sample_id: str,
    attempt_number: int,
    prompt: str,
    staging_directory: Path,
) -> Path:
    """Atomically expose prompt plus direct-capture files as one complete unit."""

    destination = _inflight_directory(run_dir, sample_id)
    source = _staged_inflight_directory(run_dir, sample_id, attempt_number)
    if source.exists():
        raise RunnerError("staged in-flight invocation requires resume recovery")
    _ensure_directory(source.parent)
    source.mkdir(mode=0o700)
    _fsync_directory(source.parent)
    _publish_text_exclusive(
        source / "prompt.txt",
        prompt,
        staging_directory=staging_directory,
    )
    for filename in ("stdout.jsonl", "stderr.txt"):
        _publish_bytes_exclusive(
            source / filename,
            b"",
            staging_directory=staging_directory,
        )
    _publish_directory_exclusive(
        source,
        destination,
        staging_directory=staging_directory,
    )
    return destination


def _remove_inflight_directory(path: Path) -> None:
    if not path.exists():
        return
    allowed = {
        "prompt.txt",
        "stdout.jsonl",
        "stderr.txt",
        "prediction.json",
    }
    observed = {entry.name for entry in path.iterdir()}
    if observed - allowed:
        raise RunnerError(
            f"unsafe in-flight invocation artifacts: {sorted(observed - allowed)}"
        )
    for entry in path.iterdir():
        if entry.is_symlink() or not entry.is_file():
            raise RunnerError(f"unsafe in-flight invocation artifact {entry.name!r}")
    for filename in sorted(observed):
        (path / filename).unlink()
    path.rmdir()
    _fsync_directory(path.parent)


def _active_process_status(
    *,
    run_dir: Path,
    case: PromptCase,
    attempt_number: int,
    prompt_sha256: str,
) -> Literal["none", "never_started", "dead", "alive", "unknown"]:
    marker = _active_marker_path(run_dir, case.sample_id, attempt_number)
    reservation = _launch_reservation_path(
        run_dir, case.sample_id, attempt_number
    )
    completion = _launch_completion_path(
        run_dir, case.sample_id, attempt_number
    )
    started = _launch_started_path(run_dir, case.sample_id, attempt_number)
    if not marker.exists():
        if completion.exists() or started.exists():
            raise RunnerError("launch witness exists without an active marker")
        if reservation.exists():
            _load_launch_reservation(
                reservation,
                case=case,
                attempt_number=attempt_number,
                prompt_sha256=prompt_sha256,
            )
            return "never_started"
        return "none"
    if marker.is_symlink() or not marker.is_file():
        raise RunnerError("active process marker is unsafe")
    try:
        marker_bytes = marker.read_bytes()
    except OSError as error:
        raise RunnerError(f"active process marker is unreadable: {error}") from error
    payload = _decode_strict_json_object(
        marker_bytes,
        location="active process marker",
    )
    expected_identity = {
        "sample_id": case.sample_id,
        "attempt_number": attempt_number,
        "prompt_sha256": prompt_sha256,
    }
    if any(
        payload.get(key) != value for key, value in expected_identity.items()
    ):
        raise RunnerError("active process marker identity differs from attempt")
    if payload.get("schema_version") == "cofactor9.1.active-process.v1":
        expected_fields = frozenset(
            {
                "schema_version",
                "sample_id",
                "attempt_number",
                "pid",
                "prompt_sha256",
            }
        )
        if frozenset(payload) != expected_fields:
            raise RunnerError("legacy active process marker fields differ")
        pid = payload.get("pid")
        if pid is None:
            return "unknown"
    elif payload.get("schema_version") == "cofactor9.1.active-process.v2":
        expected_fields = frozenset(
            {
                "schema_version",
                "sample_id",
                "attempt_number",
                "launch_id",
                "launcher_pid",
                "prompt_sha256",
                "argv",
                "timeout_seconds",
                "committed_at",
            }
        )
        if frozenset(payload) != expected_fields:
            raise RunnerError("active process marker fields differ from contract")
        reservation_payload = _load_launch_reservation(
            reservation,
            case=case,
            attempt_number=attempt_number,
            prompt_sha256=prompt_sha256,
        )
        if payload.get("launch_id") != reservation_payload.get("launch_id"):
            raise RunnerError("active marker launch identity differs from reservation")
        _parse_utc_timestamp(
            payload.get("committed_at"),
            location="active marker committed_at",
        )
        timeout = payload.get("timeout_seconds")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise RunnerError("active marker timeout is invalid")
        argv = payload.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(item, str) or not item for item in argv)
        ):
            raise RunnerError("active marker argv is invalid")
        pid = payload.get("launcher_pid")
        process_group_ids = [pid]
        if started.exists():
            started_payload = _load_launch_started(
                started,
                active_payload=payload,
            )
            process_group_ids.append(started_payload.get("model_pid"))
    else:
        raise RunnerError("active process marker schema is unknown")
    if payload.get("schema_version") == "cofactor9.1.active-process.v1":
        process_group_ids = [pid]
    statuses: list[str] = []
    for process_group_id in process_group_ids:
        if (
            isinstance(process_group_id, bool)
            or not isinstance(process_group_id, int)
            or process_group_id <= 0
        ):
            raise RunnerError("active process marker PID is invalid")
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            statuses.append("dead")
        except PermissionError:
            statuses.append("unknown")
        else:
            statuses.append("alive")
    if "alive" in statuses:
        return "alive"
    if "unknown" in statuses:
        return "unknown"
    return "dead"


def _load_launch_reservation(
    path: Path,
    *,
    case: PromptCase,
    attempt_number: int,
    prompt_sha256: str,
) -> Mapping[str, object]:
    if path.is_symlink() or not path.is_file():
        raise RunnerError("launch reservation is missing or unsafe")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise RunnerError(f"launch reservation is unreadable: {error}") from error
    payload = _decode_strict_json_object(raw, location="launch reservation")
    expected_fields = frozenset(
        {
            "schema_version",
            "sample_id",
            "attempt_number",
            "launch_id",
            "prompt_sha256",
            "reserved_at",
        }
    )
    if frozenset(payload) != expected_fields:
        raise RunnerError("launch reservation fields differ from contract")
    expected = {
        "schema_version": "cofactor9.1.launch-reservation.v1",
        "sample_id": case.sample_id,
        "attempt_number": attempt_number,
        "prompt_sha256": prompt_sha256,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise RunnerError("launch reservation identity differs from attempt")
    launch_id = payload.get("launch_id")
    if (
        not isinstance(launch_id, str)
        or len(launch_id) != 32
        or any(character not in "0123456789abcdef" for character in launch_id)
    ):
        raise RunnerError("launch reservation ID is invalid")
    _parse_utc_timestamp(
        payload.get("reserved_at"),
        location="launch reservation reserved_at",
    )
    return payload


def _load_launch_started(
    path: Path,
    *,
    active_payload: Mapping[str, object],
) -> Mapping[str, object]:
    if path.is_symlink() or not path.is_file():
        raise RunnerError("launch-started witness is missing or unsafe")
    payload = _decode_strict_json_object(
        path.read_bytes(),
        location="launch-started witness",
    )
    expected_fields = frozenset(
        {
            "schema_version",
            "sample_id",
            "attempt_number",
            "launch_id",
            "launcher_pid",
            "model_pid",
            "prompt_sha256",
            "argv",
            "started_at",
        }
    )
    if frozenset(payload) != expected_fields:
        raise RunnerError("launch-started witness fields differ from contract")
    for key in (
        "sample_id",
        "attempt_number",
        "launch_id",
        "launcher_pid",
        "prompt_sha256",
        "argv",
    ):
        if payload.get(key) != active_payload.get(key):
            raise RunnerError("launch-started witness differs from active marker")
    if payload.get("schema_version") != "cofactor9.1.launch-started.v1":
        raise RunnerError("launch-started witness schema is unknown")
    model_pid = payload.get("model_pid")
    if isinstance(model_pid, bool) or not isinstance(model_pid, int) or model_pid <= 0:
        raise RunnerError("launch-started witness model PID is invalid")
    _parse_utc_timestamp(
        payload.get("started_at"),
        location="launch-started witness started_at",
    )
    return payload


def _load_launch_completion(
    *,
    completion_path: Path,
    active_marker: Path,
    reservation_path: Path,
    started_path: Path,
    case_sample_id: str,
    attempt_number: int,
    prompt_sha256: str,
    launcher_pid: int,
    stdout_path: Path,
    stderr_path: Path,
) -> _DurableCapture:
    for path, label in (
        (completion_path, "launch completion"),
        (active_marker, "active process marker"),
        (reservation_path, "launch reservation"),
        (stdout_path, "durable stdout"),
        (stderr_path, "durable stderr"),
    ):
        if path.is_symlink() or not path.is_file():
            raise RunnerError(f"{label} is missing or unsafe")
    completion = _decode_strict_json_object(
        completion_path.read_bytes(),
        location="launch completion",
    )
    active = _decode_strict_json_object(
        active_marker.read_bytes(),
        location="active process marker",
    )
    reservation = _decode_strict_json_object(
        reservation_path.read_bytes(),
        location="launch reservation",
    )
    started = (
        _load_launch_started(started_path, active_payload=active)
        if started_path.exists()
        else None
    )
    expected_fields = frozenset(
        {
            "schema_version",
            "sample_id",
            "attempt_number",
            "launch_id",
            "launcher_pid",
            "model_pid",
            "prompt_sha256",
            "argv",
            "started_at",
            "completed_at",
            "duration_seconds",
            "returncode",
            "timed_out",
            "cancelled",
            "start_error",
        }
    )
    if frozenset(completion) != expected_fields:
        raise RunnerError("launch completion fields differ from contract")
    expected_identity = {
        "schema_version": "cofactor9.1.launch-completion.v1",
        "sample_id": case_sample_id,
        "attempt_number": attempt_number,
        "launcher_pid": launcher_pid,
        "model_pid": started.get("model_pid") if started is not None else None,
        "prompt_sha256": prompt_sha256,
    }
    if any(completion.get(key) != value for key, value in expected_identity.items()):
        raise RunnerError("launch completion identity differs from attempt")
    if (
        active.get("schema_version") != "cofactor9.1.active-process.v2"
        or active.get("sample_id") != case_sample_id
        or active.get("attempt_number") != attempt_number
        or active.get("launcher_pid") != launcher_pid
        or active.get("prompt_sha256") != prompt_sha256
        or active.get("launch_id") != completion.get("launch_id")
        or active.get("argv") != completion.get("argv")
        or reservation.get("launch_id") != completion.get("launch_id")
    ):
        raise RunnerError("durable launch witnesses disagree")
    started_at = _parse_utc_timestamp(
        completion.get("started_at"),
        location="launch completion started_at",
    )
    completed_at = _parse_utc_timestamp(
        completion.get("completed_at"),
        location="launch completion completed_at",
    )
    if completed_at < started_at:
        raise RunnerError("launch completion precedes launch start")
    duration = completion.get("duration_seconds")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration < 0
    ):
        raise RunnerError("launch completion duration is invalid")
    returncode = completion.get("returncode")
    if returncode is not None and (
        isinstance(returncode, bool) or not isinstance(returncode, int)
    ):
        raise RunnerError("launch completion returncode is invalid")
    timed_out = completion.get("timed_out")
    cancelled = completion.get("cancelled")
    start_error = completion.get("start_error")
    if not isinstance(timed_out, bool) or not isinstance(cancelled, bool):
        raise RunnerError("launch completion flags are invalid")
    if timed_out and cancelled:
        raise RunnerError("launch completion has conflicting stop reasons")
    if start_error is not None and (
        not isinstance(start_error, str)
        or not start_error
        or returncode is not None
        or timed_out
        or cancelled
    ):
        raise RunnerError("launch completion start failure is invalid")
    if start_error is None and returncode is None and not cancelled:
        raise RunnerError("launch completion lacks a process outcome")
    argv = completion.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(item, str) or not item for item in argv)
    ):
        raise RunnerError("launch completion argv is invalid")
    stdout = stdout_path.read_bytes().decode("utf-8", errors="replace")
    stderr = stderr_path.read_bytes().decode("utf-8", errors="replace")
    return _DurableCapture(
        capture=_ProcessCapture(
            argv=tuple(argv),
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            timed_out=timed_out,
            cancelled=cancelled,
            start_error=start_error,
            duration_seconds=float(duration),
        ),
        started_at=completion["started_at"],  # type: ignore[arg-type]
        completed_at=completion["completed_at"],  # type: ignore[arg-type]
    )


def _validate_recorded_argv(
    value: object,
    *,
    executable: str | Path,
    schema_path: Path,
    allow_interrupted_sentinel: bool,
) -> None:
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) for item in value
    ):
        raise RunnerError("ledger argv is invalid")
    if allow_interrupted_sentinel and value == [
        str(executable),
        "<outcome-unknown-after-host-interruption>",
    ]:
        return
    if allow_interrupted_sentinel and value == [
        str(executable),
        "<interrupted-before-capture>",
    ]:
        return
    if value.count("-C") != 1:
        raise RunnerError("ledger argv does not contain one working directory")
    index = value.index("-C")
    if index + 1 >= len(value):
        raise RunnerError("ledger argv working directory is missing")
    expected = build_codex_argv(
        executable=executable,
        schema_path=schema_path,
        working_directory=Path(value[index + 1]),
    )
    if value != expected:
        raise RunnerError("ledger argv differs from the hardened command")


def _load_attempt_state(
    *,
    attempt_directory: Path,
    case: PromptCase,
    attempt_number: int,
    persisted_prompt: str,
    executable: str | Path,
    schema_path: Path,
) -> _AttemptState:
    observed: set[str] = set()
    for entry in attempt_directory.iterdir():
        if entry.is_symlink() or not entry.is_file():
            raise RunnerError(f"unsafe attempt artifact {entry.name!r}")
        observed.add(entry.name)
    missing = _ATTEMPT_REQUIRED_FILES - observed
    extra = observed - _ATTEMPT_ALLOWED_FILES
    if missing or extra:
        raise RunnerError(
            f"attempt {attempt_number} artifact set is invalid; "
            f"missing={sorted(missing)}, extra={sorted(extra)}"
        )
    prompt_bytes = (attempt_directory / "prompt.txt").read_bytes()
    stdout_bytes = (attempt_directory / "stdout.jsonl").read_bytes()
    stderr_bytes = (attempt_directory / "stderr.txt").read_bytes()
    if prompt_bytes != persisted_prompt.encode("utf-8"):
        raise RunnerError("existing attempt prompt does not match requested case")
    marker_path = attempt_directory / "attempt.json"
    try:
        marker_bytes = marker_path.read_bytes()
    except OSError as error:
        raise RunnerError(f"attempt completion marker is unreadable: {error}") from error
    payload = _decode_strict_json_object(
        marker_bytes,
        location="attempt completion marker",
    )
    if frozenset(payload) != _ATTEMPT_FIELDS:
        raise RunnerError("attempt completion marker fields differ from contract")
    expected_identity = {
        "schema_version": "cofactor9.1.attempt.v1",
        "sample_id": case.sample_id,
        "attempt_number": attempt_number,
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "service_tier": SERVICE_TIER,
        "environment_policy": "fixed-allowlist",
        "prompt_sha256": _sha256_bytes(prompt_bytes),
        "stdout_sha256": _sha256_bytes(stdout_bytes),
        "stderr_sha256": _sha256_bytes(stderr_bytes),
    }
    if any(payload.get(key) != value for key, value in expected_identity.items()):
        raise RunnerError("attempt completion marker identity or hashes differ")
    _validate_recorded_argv(
        payload.get("argv"),
        executable=executable,
        schema_path=schema_path,
        allow_interrupted_sentinel=False,
    )
    duration = payload.get("duration_seconds")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration < 0
    ):
        raise RunnerError("attempt completion marker duration is invalid")
    started_at = _parse_utc_timestamp(
        payload.get("started_at"),
        location="attempt completion marker started_at",
    )
    completed_at = _parse_utc_timestamp(
        payload.get("completed_at"),
        location="attempt completion marker completed_at",
    )
    if completed_at < started_at:
        raise RunnerError("attempt completion precedes its start")
    returncode = payload.get("returncode")
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        raise RunnerError("attempt completion marker returncode is invalid")
    timed_out = payload.get("timed_out")
    if not isinstance(timed_out, bool):
        raise RunnerError("attempt completion marker timed_out is invalid")
    redactions = payload.get("redaction_count")
    if (
        isinstance(redactions, bool)
        or not isinstance(redactions, int)
        or redactions < 0
    ):
        raise RunnerError("attempt completion marker redaction_count is invalid")
    usage = payload.get("usage")
    if not isinstance(usage, Mapping) or any(
        not isinstance(key, str)
        or not key
        or isinstance(item, bool)
        or not isinstance(item, int)
        or item < 0
        for key, item in usage.items()
    ):
        raise RunnerError("attempt completion marker usage is invalid")
    thread_id = payload.get("thread_id")
    if thread_id is not None and not isinstance(thread_id, str):
        raise RunnerError("attempt completion marker thread_id is invalid")
    retry_disposition = payload.get("retry_disposition")
    if retry_disposition not in {"none", "retryable", "nonretryable"}:
        raise RunnerError("attempt completion marker retry disposition is invalid")
    try:
        stdout = stdout_bytes.decode("utf-8")
        stderr = stderr_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RunnerError("attempt output bytes are not valid UTF-8") from error
    replay = replay_codex_attempt(
        case=case,
        argv=payload.get("argv"),  # type: ignore[arg-type]
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
        timed_out=timed_out,
        duration_seconds=float(duration),
    )
    replay_fields = {
        "error_code": replay.error_code,
        "error_message": replay.error_message,
        "retry_disposition": replay.retry_disposition,
        "thread_id": replay.thread_id,
        "usage": replay.usage,
    }
    if any(payload.get(key) != value for key, value in replay_fields.items()):
        raise RunnerError("attempt completion marker differs from raw-output replay")
    error_code = replay.error_code
    error_message = replay.error_message
    prediction_path = attempt_directory / "prediction.json"
    if error_code in _NON_BUDGET_ERROR_CODES or error_code == "INTERRUPTED_ATTEMPT":
        raise RunnerError(
            "infrastructure and host interruptions belong in the incident ledger"
        )
    if error_code is None:
        prediction = replay.prediction
        if prediction is None or not prediction_path.is_file():
            raise RunnerError("successful attempt lacks prediction artifact")
        if prediction_path.read_bytes() != _canonical_json_bytes(prediction.to_dict()):
            raise RunnerError("saved prediction bytes differ from raw-output replay")
        return _AttemptState(
            number=attempt_number,
            error_code=None,
            error_message=None,
            retry_disposition="none",
            prediction=prediction,
        )
    if error_message is None:
        raise RunnerError("replayed attempt failure lacks an error message")
    if prediction_path.exists():
        raise RunnerError("failed attempt unexpectedly carries a prediction")
    return _AttemptState(
        number=attempt_number,
        error_code=error_code,
        error_message=error_message,
        retry_disposition=replay.retry_disposition,
        prediction=None,
    )


def _load_transport_incident_record(
    *,
    directory: Path,
    incident_number: int,
    case: PromptCase,
    persisted_prompt: str,
    executable: str | Path,
    schema_path: Path,
) -> Mapping[str, object]:
    observed: set[str] = set()
    for entry in directory.iterdir():
        if entry.is_symlink() or not entry.is_file():
            raise RunnerError(f"unsafe transport incident artifact {entry.name!r}")
        observed.add(entry.name)
    missing = _INCIDENT_REQUIRED_FILES - observed
    extra = observed - _INCIDENT_ALLOWED_FILES
    if missing or extra:
        raise RunnerError("transport incident artifact set is incomplete")
    prompt_bytes = (directory / "prompt.txt").read_bytes()
    stdout_bytes = (directory / "stdout.jsonl").read_bytes()
    stderr_bytes = (directory / "stderr.txt").read_bytes()
    if prompt_bytes != persisted_prompt.encode("utf-8"):
        raise RunnerError("transport incident prompt identity differs")
    try:
        marker_bytes = (directory / "incident.json").read_bytes()
    except OSError as error:
        raise RunnerError(f"transport incident marker is unreadable: {error}") from error
    payload = _decode_strict_json_object(
        marker_bytes,
        location="transport incident marker",
    )
    if frozenset(payload) != _INCIDENT_FIELDS:
        raise RunnerError("transport incident marker fields differ from contract")
    expected = {
        "schema_version": "cofactor9.1.transport-incident.v1",
        "sample_id": case.sample_id,
        "incident_number": incident_number,
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "service_tier": SERVICE_TIER,
        "environment_policy": "fixed-allowlist",
        "prompt_sha256": _sha256_bytes(prompt_bytes),
        "stdout_sha256": _sha256_bytes(stdout_bytes),
        "stderr_sha256": _sha256_bytes(stderr_bytes),
        "prediction_sha256": (
            _sha256_bytes((directory / "prediction.json").read_bytes())
            if (directory / "prediction.json").is_file()
            else None
        ),
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise RunnerError("transport incident identity or hashes differ")
    tentative = payload.get("tentative_attempt_number")
    if isinstance(tentative, bool) or not isinstance(tentative, int) or tentative < 1:
        raise RunnerError("transport incident tentative attempt is invalid")
    started_at = _parse_utc_timestamp(
        payload.get("started_at"),
        location="transport incident started_at",
    )
    completed_at = _parse_utc_timestamp(
        payload.get("completed_at"),
        location="transport incident completed_at",
    )
    if completed_at < started_at:
        raise RunnerError("transport incident completion precedes its start")
    duration = payload.get("duration_seconds")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration < 0
    ):
        raise RunnerError("transport incident duration is invalid")
    returncode = payload.get("returncode")
    if returncode is not None and (
        isinstance(returncode, bool) or not isinstance(returncode, int)
    ):
        raise RunnerError("transport incident returncode is invalid")
    timed_out = payload.get("timed_out")
    if timed_out is not False:
        raise RunnerError("transport incident cannot be a timed-out model attempt")
    cancelled = payload.get("cancelled")
    start_error = payload.get("start_error")
    redactions = payload.get("redaction_count")
    if (
        isinstance(redactions, bool)
        or not isinstance(redactions, int)
        or redactions < 0
    ):
        raise RunnerError("transport incident redaction_count is invalid")
    usage = payload.get("usage")
    if not isinstance(usage, Mapping) or any(
        not isinstance(key, str)
        or not key
        or isinstance(item, bool)
        or not isinstance(item, int)
        or item < 0
        for key, item in usage.items()
    ):
        raise RunnerError("transport incident usage is invalid")
    if usage:
        raise RunnerError("transport incident cannot carry completed model usage")
    if payload.get("thread_id") is not None:
        raise RunnerError("transport incident cannot carry a completed thread")
    error_code = payload.get("error_code")
    if error_code not in (
        INFRASTRUCTURE_INCIDENT_CODES | {"RUN_CANCELLED", "INTERRUPTED_ATTEMPT"}
    ):
        raise RunnerError("transport incident error code is invalid")
    if not isinstance(payload.get("error_message"), str) or not payload.get(
        "error_message"
    ):
        raise RunnerError("transport incident error message is invalid")
    expected_disposition = {
        "AUTH_ERROR": "nonretryable",
        "CAPACITY_ERROR": "retryable",
        "TRANSPORT_ERROR": "retryable",
        "PROCESS_START_ERROR": "nonretryable",
        "RUN_CANCELLED": "retryable",
        "INTERRUPTED_ATTEMPT": "retryable",
    }[error_code]
    if payload.get("retry_disposition") != expected_disposition:
        raise RunnerError("transport incident retry disposition conflicts with code")
    argv = payload.get("argv")
    interrupted_sentinels = (
        [str(executable), "<interrupted-before-capture>"],
        [str(executable), "<outcome-unknown-after-host-interruption>"],
    )
    if error_code == "INTERRUPTED_ATTEMPT":
        if argv not in interrupted_sentinels:
            raise RunnerError("interrupted incident argv is not the capture sentinel")
        if (
            returncode is not None
            or cancelled is not None
            or start_error is not None
            or duration != 0
        ):
            raise RunnerError("interrupted incident process fields conflict")
        return payload

    _validate_recorded_argv(
        argv,
        executable=executable,
        schema_path=schema_path,
        allow_interrupted_sentinel=False,
    )
    if not isinstance(cancelled, bool):
        raise RunnerError("transport incident cancelled field is invalid")
    if error_code == "RUN_CANCELLED":
        if cancelled is not True or start_error is not None:
            raise RunnerError("cancelled incident process fields conflict")
    elif error_code == "PROCESS_START_ERROR":
        if (
            cancelled is not False
            or returncode is not None
            or not isinstance(start_error, str)
            or not start_error
            or start_error != payload.get("error_message")
        ):
            raise RunnerError("process-start incident fields conflict")
    elif cancelled is not False or start_error is not None:
        raise RunnerError("transport incident process fields conflict")
    try:
        stdout = stdout_bytes.decode("utf-8")
        stderr = stderr_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RunnerError("transport incident output is not valid UTF-8") from error
    replay = replay_codex_attempt(
        case=case,
        argv=argv,  # type: ignore[arg-type]
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
        timed_out=timed_out,
        cancelled=cancelled,
        start_error=start_error,
        duration_seconds=float(duration),
    )
    replay_fields = {
        "error_code": replay.error_code,
        "error_message": replay.error_message,
        "retry_disposition": replay.retry_disposition,
        "thread_id": replay.thread_id,
        "usage": replay.usage,
    }
    if replay.prediction is not None or any(
        payload.get(key) != value for key, value in replay_fields.items()
    ):
        raise RunnerError("transport incident differs from raw-output replay")
    if (directory / "prediction.json").exists():
        raise RunnerError("captured transport incident carries a prediction")
    return payload


def _validate_transport_incidents(
    *,
    incidents_directory: Path,
    case: PromptCase,
    persisted_prompt: str,
    executable: str | Path,
    schema_path: Path,
) -> tuple[Mapping[str, object], ...]:
    return tuple(
        _load_transport_incident_record(
            directory=incidents_directory / f"incident-{number:04d}",
            incident_number=number,
            case=case,
            persisted_prompt=persisted_prompt,
            executable=executable,
            schema_path=schema_path,
        )
        for number in _incident_numbers(incidents_directory)
    )


def _load_terminal(path: Path, case: PromptCase) -> TerminalResult:
    try:
        terminal_bytes = path.read_bytes()
    except OSError as error:
        raise RunnerError(f"terminal record is unreadable: {error}") from error
    payload = _decode_strict_json_object(terminal_bytes, location="terminal record")
    if frozenset(payload) != _TERMINAL_FIELDS:
        raise RunnerError("terminal record fields differ from contract")
    expected_identity = {
        "schema_version": "cofactor9.1.terminal.v1",
        "sample_id": case.sample_id,
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "service_tier": SERVICE_TIER,
        "prompt_version": PROMPT_VERSION,
        "catalog_version": case.catalog_version,
    }
    if payload.get("prompt_sha256") != _sha256_text(render_prompt(case)):
        raise RunnerError("terminal record prompt does not match requested case")
    if any(payload.get(key) != value for key, value in expected_identity.items()):
        raise RunnerError("terminal record identity or settings differ")
    _parse_utc_timestamp(
        payload.get("completed_at"),
        location="terminal record completed_at",
    )
    status = payload.get("status")
    attempt_count = payload.get("attempt_count")
    if (
        isinstance(attempt_count, bool)
        or not isinstance(attempt_count, int)
        or attempt_count < 1
    ):
        raise RunnerError("terminal record has invalid attempt_count")
    if status == "success":
        if payload.get("error_code") is not None or payload.get("error_message") is not None:
            raise RunnerError("successful terminal error fields conflict")
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
        if (
            payload.get("prediction") is not None
            or not isinstance(error_code, str)
            or not error_code
            or not isinstance(error_message, str)
            or not error_message
        ):
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
    if capture.cancelled:
        raise AttemptFailure(
            "RUN_CANCELLED",
            "Codex attempt was cancelled by the batch controller",
            retry_disposition="retryable",
        )
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


def _classify_captured_attempt(
    capture: _ProcessCapture,
    case: PromptCase,
) -> AttemptReplayResult:
    try:
        prediction, output = _validated_prediction(capture, case)
    except AttemptFailure as failure:
        return AttemptReplayResult(
            prediction=None,
            error_code=failure.code,
            error_message=str(failure),
            retry_disposition=failure.retry_disposition,
            thread_id=None,
            usage={},
        )
    return AttemptReplayResult(
        prediction=prediction,
        error_code=None,
        error_message=None,
        retry_disposition="none",
        thread_id=output.thread_id,
        usage=dict(output.usage),
    )


def replay_codex_attempt(
    *,
    case: PromptCase,
    argv: Sequence[str],
    stdout: str,
    stderr: str,
    returncode: int | None,
    timed_out: bool,
    cancelled: bool = False,
    start_error: str | None = None,
    duration_seconds: float = 0.0,
) -> AttemptReplayResult:
    """Classify saved transport bytes without starting or contacting Codex."""

    if not isinstance(case, PromptCase):
        raise TypeError("case must be a validated PromptCase")
    if (
        isinstance(argv, (str, bytes))
        or not isinstance(argv, Sequence)
        or not argv
        or any(not isinstance(item, str) or not item for item in argv)
    ):
        raise TypeError("argv must be a non-empty sequence of non-empty strings")
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        raise TypeError("stdout and stderr must be strings")
    if returncode is not None and (
        isinstance(returncode, bool) or not isinstance(returncode, int)
    ):
        raise TypeError("returncode must be an integer or None")
    if not isinstance(timed_out, bool) or not isinstance(cancelled, bool):
        raise TypeError("timed_out and cancelled must be booleans")
    if start_error is not None and (
        not isinstance(start_error, str) or not start_error
    ):
        raise TypeError("start_error must be a non-empty string or None")
    if (
        isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, (int, float))
        or not math.isfinite(duration_seconds)
        or duration_seconds < 0
    ):
        raise ValueError("duration_seconds must be finite and non-negative")
    if timed_out and cancelled:
        raise ValueError("an invocation cannot be both timed out and cancelled")
    if start_error is not None and (
        returncode is not None or timed_out or cancelled
    ):
        raise ValueError("process-start failure fields conflict")
    capture = _ProcessCapture(
        argv=tuple(argv),
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
        timed_out=timed_out,
        cancelled=cancelled,
        start_error=start_error,
        duration_seconds=float(duration_seconds),
    )
    return _classify_captured_attempt(capture, case)


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


@contextmanager
def _installed_signal_handlers(control: _BatchControl) -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    supported = tuple(
        candidate
        for candidate in (
            getattr(signal, "SIGINT", None),
            getattr(signal, "SIGTERM", None),
            getattr(signal, "SIGHUP", None),
        )
        if isinstance(candidate, int)
    )
    previous: dict[int, object] = {}

    def handle(signum: int, _frame: object) -> None:
        control.cancel(signal_number=signum)

    try:
        for signum in supported:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, handle)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


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
            or circuit_breaker_threshold != 1
        ):
            raise ValueError(
                "circuit_breaker_threshold must be 1 because any systematic "
                "failure stops new batch work immediately"
            )
        default_schema = (
            Path(__file__).parents[1] / "schemas" / "model-response.schema.json"
        )
        self.run_dir = Path(run_dir)
        self.executable = executable
        self.schema_path = Path(schema_path) if schema_path else default_schema
        self.max_attempts = max_attempts
        self.timeout_seconds = float(timeout_seconds)
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self._staging_directory = self.run_dir / ".staging"
        self._supervisor = _ProcessSupervisor()
        self._batch_lock = threading.Lock()

    def run_case(
        self,
        case: PromptCase,
        *,
        resume: bool = False,
    ) -> TerminalResult:
        prepared = self._prepare_case(case, resume=resume)
        if isinstance(prepared, TerminalResult):
            return prepared
        return self._execute_plan(prepared, control=None)

    def _prepare_case(
        self,
        case: PromptCase,
        *,
        resume: bool,
    ) -> TerminalResult | _ResumePlan:
        if not isinstance(case, PromptCase):
            raise TypeError("case must be a validated PromptCase")
        if not self.schema_path.is_file():
            raise RunnerError(f"response schema does not exist: {self.schema_path}")

        case_directory = self.run_dir / "cases" / case.sample_id
        terminal_path = case_directory / "terminal.json"
        terminal_result: TerminalResult | None = None
        if terminal_path.exists():
            if not resume:
                raise CompletedCaseError(
                    f"case {case.sample_id} already has a terminal record"
                )
            terminal_result = _load_terminal(terminal_path, case)

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
            if len(existing_attempts) > self.max_attempts:
                raise RunnerError("attempt ledger exceeds the configured maximum")

        incidents_directory = (
            self.run_dir / "transport-incidents" / case.sample_id
        )
        existing_incidents = _incident_numbers(incidents_directory)
        inflight = _inflight_directory(self.run_dir, case.sample_id)
        staged_inflight = _staged_inflight_directory(
            self.run_dir,
            case.sample_id,
            len(existing_attempts) + 1,
        )
        if (existing_incidents or inflight.exists() or staged_inflight.exists()) and not resume:
            raise RunnerError(
                f"case {case.sample_id} has transport state; resume is required"
            )
        if staged_inflight.exists():
            _discard_staged_inflight(staged_inflight)

        prompt = render_prompt(case)
        sensitive_values = _sensitive_values(os.environ)
        persisted_prompt, prompt_redactions = _redact(prompt, sensitive_values)
        prompt_hash = _sha256_text(prompt)
        incident_records = _validate_transport_incidents(
            incidents_directory=incidents_directory,
            case=case,
            persisted_prompt=persisted_prompt,
            executable=self.executable,
            schema_path=self.schema_path,
        )
        states: list[_AttemptState] = []
        for existing_attempt in existing_attempts:
            existing_directory = (
                attempts_directory / f"attempt-{existing_attempt:04d}"
            )
            saved_prompt_path = existing_directory / "prompt.txt"
            if saved_prompt_path.is_symlink() or not saved_prompt_path.is_file():
                raise RunnerError("partial attempt lacks immutable prompt identity")
            if saved_prompt_path.read_bytes() != persisted_prompt.encode("utf-8"):
                raise RunnerError(
                    "existing attempt prompt does not match requested case"
                )
            observed_files = {entry.name for entry in existing_directory.iterdir()}
            complete = _ATTEMPT_REQUIRED_FILES.issubset(observed_files)
            if not complete:
                if terminal_result is not None:
                    raise RunnerError("terminal case has a partial attempt ledger")
                if existing_attempt != existing_attempts[-1]:
                    raise RunnerError("only the trailing attempt may be interrupted")
                self._migrate_legacy_partial_attempt(
                    attempt_directory=existing_directory,
                    incident_number=len(incident_records) + 1,
                    case=case,
                    attempt_number=existing_attempt,
                    persisted_prompt=persisted_prompt,
                    prompt_redactions=prompt_redactions,
                    prompt_sha256=prompt_hash,
                )
                return self._prepare_case(case, resume=True)
            active_status = _active_process_status(
                run_dir=self.run_dir,
                case=case,
                attempt_number=existing_attempt,
                prompt_sha256=prompt_hash,
            )
            if active_status == "alive":
                raise RunnerError(
                    f"pre-crash process group for attempt {existing_attempt} is still alive"
                )
            if active_status == "unknown":
                raise RunnerError(
                    "interrupted attempt outcome is unknown and process death "
                    "cannot be confirmed"
                )
            if active_status in {"dead", "never_started"}:
                _remove_active_state(
                    self.run_dir,
                    case.sample_id,
                    existing_attempt,
                )
            states.append(
                _load_attempt_state(
                    attempt_directory=existing_directory,
                    case=case,
                    attempt_number=existing_attempt,
                    persisted_prompt=persisted_prompt,
                    executable=self.executable,
                    schema_path=self.schema_path,
                )
            )

        if terminal_result is not None and inflight.exists():
            raise RunnerError("terminal case has an in-flight invocation")
        if terminal_result is None:
            finalized_attempt = self._recover_inflight(
                case=case,
                persisted_prompt=persisted_prompt,
                prompt_redactions=prompt_redactions,
                tentative_attempt_number=len(states) + 1,
                incident_records=incident_records,
            )
            if finalized_attempt:
                return self._prepare_case(case, resume=True)
        orphan_status = _active_process_status(
            run_dir=self.run_dir,
            case=case,
            attempt_number=len(states) + 1,
            prompt_sha256=prompt_hash,
        )
        if orphan_status == "alive":
            raise RunnerError("pre-crash process group is still alive")
        if orphan_status == "unknown":
            raise RunnerError(
                "interrupted transport outcome is unknown and process death "
                "cannot be confirmed"
            )
        if orphan_status in {"dead", "never_started"} and not inflight.exists():
            _remove_active_state(
                self.run_dir,
                case.sample_id,
                len(states) + 1,
            )

        for state in states[:-1]:
            if state.error_code is None:
                raise RunnerError("attempt ledger continued after a successful attempt")
            if state.retry_disposition == "nonretryable" and (
                state.error_code not in _NON_BUDGET_ERROR_CODES
            ):
                raise RunnerError(
                    "attempt ledger continued after a nonretryable model failure"
                )
        if terminal_result is not None:
            if terminal_result.attempt_count != len(states) or not states:
                raise RunnerError("terminal attempt_count differs from attempt ledger")
            final = states[-1]
            if terminal_result.status == "success":
                if (
                    final.error_code is not None
                    or final.prediction is None
                    or terminal_result.prediction != final.prediction
                    or terminal_result.error_code is not None
                    or terminal_result.error_message is not None
                ):
                    raise RunnerError("terminal success differs from final attempt replay")
            elif (
                final.prediction is not None
                or final.error_code is None
                or terminal_result.prediction is not None
                or terminal_result.error_code != final.error_code
                or terminal_result.error_message != final.error_message
            ):
                raise RunnerError("terminal error differs from final attempt replay")
            return terminal_result
        if states and states[-1].error_code is None:
            prediction = states[-1].prediction
            if prediction is None:
                raise RunnerError("successful final attempt lacks a prediction")
            result = TerminalResult(
                sample_id=case.sample_id,
                status="success",
                attempt_count=len(states),
                prediction=prediction,
            )
            self._write_terminal(terminal_path, case, result)
            return result

        budget_failures = sum(
            state.error_code is not None
            and state.error_code not in _NON_BUDGET_ERROR_CODES
            for state in states
        )
        if states:
            final = states[-1]
            should_close_error = (
                final.error_code not in _NON_BUDGET_ERROR_CODES
                and (
                    final.retry_disposition == "nonretryable"
                    or budget_failures >= self.max_attempts
                )
            )
            if should_close_error:
                if final.error_code is None or final.error_message is None:
                    raise RunnerError("final failed attempt is malformed")
                result = TerminalResult(
                    sample_id=case.sample_id,
                    status="terminal_error",
                    attempt_count=len(states),
                    error_code=final.error_code,
                    error_message=final.error_message,
                )
                self._write_terminal(terminal_path, case, result)
                return result

        return _ResumePlan(
            case=case,
            persisted_prompt=persisted_prompt,
            prompt_redactions=prompt_redactions,
            next_attempt_number=len(states) + 1,
            budget_failures=budget_failures,
        )

    def _migrate_legacy_partial_attempt(
        self,
        *,
        attempt_directory: Path,
        incident_number: int,
        case: PromptCase,
        attempt_number: int,
        persisted_prompt: str,
        prompt_redactions: int,
        prompt_sha256: str,
    ) -> None:
        """Move a pre-v3 partial attempt into the non-budget incident ledger."""

        observed: set[str] = set()
        for entry in attempt_directory.iterdir():
            if entry.is_symlink() or not entry.is_file():
                raise RunnerError(f"unsafe partial attempt artifact {entry.name!r}")
            observed.add(entry.name)
        if "prompt.txt" not in observed:
            raise RunnerError("partial attempt lacks immutable prompt identity")
        if "attempt.json" in observed:
            raise RunnerError(
                "attempt completion marker exists but the attempt is incomplete"
            )
        unexpected = observed - _INCIDENT_ALLOWED_FILES
        if unexpected:
            raise RunnerError(f"unexpected partial attempt files: {sorted(unexpected)}")
        if (attempt_directory / "prompt.txt").read_bytes() != persisted_prompt.encode(
            "utf-8"
        ):
            raise RunnerError("existing attempt prompt does not match requested case")

        active_status = _active_process_status(
            run_dir=self.run_dir,
            case=case,
            attempt_number=attempt_number,
            prompt_sha256=prompt_sha256,
        )
        if "incident.json" in observed:
            _load_transport_incident_record(
                directory=attempt_directory,
                incident_number=incident_number,
                case=case,
                persisted_prompt=persisted_prompt,
                executable=self.executable,
                schema_path=self.schema_path,
            )
            destination = (
                self.run_dir
                / "transport-incidents"
                / case.sample_id
                / f"incident-{incident_number:04d}"
            )
            _publish_directory_exclusive(
                attempt_directory,
                destination,
                staging_directory=self._staging_directory,
            )
        else:
            for filename in ("stdout.jsonl", "stderr.txt"):
                path = attempt_directory / filename
                if not path.exists():
                    _publish_bytes_exclusive(
                        path,
                        b"",
                        staging_directory=self._staging_directory,
                    )
            recovered_at = _utc_now()
            self._write_transport_incident(
                case=case,
                tentative_attempt_number=attempt_number,
                source_directory=attempt_directory,
                failure=AttemptFailure(
                    "INTERRUPTED_ATTEMPT",
                    "host interruption occurred before attempt publication",
                    retry_disposition="retryable",
                ),
                capture=None,
                started_at=recovered_at,
                completed_at=recovered_at,
                redaction_count=prompt_redactions,
            )

        if active_status == "alive":
            raise RunnerError(
                f"pre-crash process group for attempt {attempt_number} is still alive"
            )
        if active_status == "unknown":
            raise RunnerError(
                "interrupted attempt outcome is unknown and process death "
                "cannot be confirmed"
            )
        _remove_active_state(self.run_dir, case.sample_id, attempt_number)

    def _recover_inflight(
        self,
        *,
        case: PromptCase,
        persisted_prompt: str,
        prompt_redactions: int,
        tentative_attempt_number: int,
        incident_records: tuple[Mapping[str, object], ...],
    ) -> bool:
        directory = _inflight_directory(self.run_dir, case.sample_id)
        if not directory.exists():
            return False
        if directory.is_symlink() or not directory.is_dir():
            raise RunnerError("in-flight invocation directory is unsafe")
        observed = {entry.name for entry in directory.iterdir()}
        if not observed:
            active_status = _active_process_status(
                run_dir=self.run_dir,
                case=case,
                attempt_number=tentative_attempt_number,
                prompt_sha256=_sha256_text(render_prompt(case)),
            )
            if active_status != "none":
                raise RunnerError(
                    "empty in-flight directory conflicts with launch state"
                )
            directory.rmdir()
            _fsync_directory(directory.parent)
            return False
        if "prompt.txt" not in observed or observed - {
            "prompt.txt",
            "stdout.jsonl",
            "stderr.txt",
            "prediction.json",
            "attempt.json",
            "incident.json",
        }:
            raise RunnerError("in-flight invocation artifacts are malformed")
        prompt_bytes = (directory / "prompt.txt").read_bytes()
        if prompt_bytes != persisted_prompt.encode("utf-8"):
            raise RunnerError("in-flight invocation prompt identity differs")
        active_status = _active_process_status(
            run_dir=self.run_dir,
            case=case,
            attempt_number=tentative_attempt_number,
            prompt_sha256=_sha256_text(render_prompt(case)),
        )
        if active_status == "alive":
            raise RunnerError("pre-crash process group is still alive")
        if active_status == "unknown":
            raise RunnerError(
                "interrupted transport outcome is unknown and process death "
                "cannot be confirmed"
            )

        if "attempt.json" in observed:
            if "incident.json" in observed:
                raise RunnerError("in-flight invocation has conflicting outcomes")
            _load_attempt_state(
                attempt_directory=directory,
                case=case,
                attempt_number=tentative_attempt_number,
                persisted_prompt=persisted_prompt,
                executable=self.executable,
                schema_path=self.schema_path,
            )
            destination = (
                self.run_dir
                / "cases"
                / case.sample_id
                / "attempts"
                / f"attempt-{tentative_attempt_number:04d}"
            )
            _publish_directory_exclusive(
                directory,
                destination,
                staging_directory=self._staging_directory,
            )
            _remove_active_state(
                self.run_dir, case.sample_id, tentative_attempt_number
            )
            return True

        if "incident.json" in observed:
            incident_number = len(incident_records) + 1
            _load_transport_incident_record(
                directory=directory,
                incident_number=incident_number,
                case=case,
                persisted_prompt=persisted_prompt,
                executable=self.executable,
                schema_path=self.schema_path,
            )
            destination = (
                self.run_dir
                / "transport-incidents"
                / case.sample_id
                / f"incident-{incident_number:04d}"
            )
            _publish_directory_exclusive(
                directory,
                destination,
                staging_directory=self._staging_directory,
            )
            _remove_active_state(
                self.run_dir, case.sample_id, tentative_attempt_number
            )
            return False

        if incident_records and {
            "prompt.txt",
            "stdout.jsonl",
            "stderr.txt",
        }.issubset(observed):
            last = incident_records[-1]
            if (
                last.get("tentative_attempt_number") == tentative_attempt_number
                and last.get("prompt_sha256")
                == _sha256_bytes((directory / "prompt.txt").read_bytes())
                and last.get("stdout_sha256")
                == _sha256_bytes((directory / "stdout.jsonl").read_bytes())
                and last.get("stderr_sha256")
                == _sha256_bytes((directory / "stderr.txt").read_bytes())
                and last.get("prediction_sha256")
                == (
                    _sha256_bytes((directory / "prediction.json").read_bytes())
                    if (directory / "prediction.json").is_file()
                    else None
                )
            ):
                _remove_inflight_directory(directory)
                _remove_active_state(
                    self.run_dir, case.sample_id, tentative_attempt_number
                )
                return False

        completion = _launch_completion_path(
            self.run_dir, case.sample_id, tentative_attempt_number
        )
        if active_status == "dead" and completion.is_file():
            active_marker = _active_marker_path(
                self.run_dir, case.sample_id, tentative_attempt_number
            )
            active_payload = _decode_strict_json_object(
                active_marker.read_bytes(),
                location="active process marker",
            )
            launcher_pid = active_payload.get("launcher_pid")
            if (
                isinstance(launcher_pid, bool)
                or not isinstance(launcher_pid, int)
                or launcher_pid <= 0
            ):
                raise RunnerError("active process marker PID is invalid")
            durable = _load_launch_completion(
                completion_path=completion,
                active_marker=active_marker,
                reservation_path=_launch_reservation_path(
                    self.run_dir, case.sample_id, tentative_attempt_number
                ),
                started_path=_launch_started_path(
                    self.run_dir, case.sample_id, tentative_attempt_number
                ),
                case_sample_id=case.sample_id,
                attempt_number=tentative_attempt_number,
                prompt_sha256=_sha256_text(render_prompt(case)),
                launcher_pid=launcher_pid,
                stdout_path=directory / "stdout.jsonl",
                stderr_path=directory / "stderr.txt",
            )
            _validate_recorded_argv(
                list(durable.capture.argv),
                executable=self.executable,
                schema_path=self.schema_path,
                allow_interrupted_sentinel=False,
            )
            if active_payload.get("timeout_seconds") != self.timeout_seconds:
                raise RunnerError(
                    "active process timeout differs from runner configuration"
                )
            self._finalize_capture(
                case=case,
                persisted_prompt=persisted_prompt,
                prompt_redactions=prompt_redactions,
                attempt_number=tentative_attempt_number,
                durable=durable,
                control=None,
            )
            return True

        if active_status == "never_started":
            interruption_message = (
                "host interruption occurred before the launcher committed; "
                "the native model was never started"
            )
        elif active_status == "dead" and _launch_started_path(
            self.run_dir, case.sample_id, tentative_attempt_number
        ).is_file():
            interruption_message = (
                "launcher and model process groups are dead but no durable "
                "completion exists; the remote outcome is unknown"
            )
        else:
            interruption_message = (
                "host interruption occurred before invocation classification"
            )
        failure = AttemptFailure(
            "INTERRUPTED_ATTEMPT",
            interruption_message,
            retry_disposition="retryable",
        )
        for filename in ("stdout.jsonl", "stderr.txt"):
            path = directory / filename
            if not path.exists():
                _publish_bytes_exclusive(
                    path,
                    b"",
                    staging_directory=self._staging_directory,
                )
        self._write_transport_incident(
            case=case,
            tentative_attempt_number=tentative_attempt_number,
            source_directory=directory,
            failure=failure,
            capture=None,
            started_at=_utc_now(),
            completed_at=_utc_now(),
            redaction_count=prompt_redactions,
        )
        _remove_active_state(
            self.run_dir, case.sample_id, tentative_attempt_number
        )
        return False

    def _write_transport_incident(
        self,
        *,
        case: PromptCase,
        tentative_attempt_number: int,
        source_directory: Path,
        failure: AttemptFailure,
        capture: _ProcessCapture | None,
        started_at: str,
        completed_at: str,
        redaction_count: int,
    ) -> Path:
        incidents = self.run_dir / "transport-incidents" / case.sample_id
        existing = _incident_numbers(incidents)
        incident_number = (existing[-1] + 1) if existing else 1
        destination = incidents / f"incident-{incident_number:04d}"
        prompt_bytes = (source_directory / "prompt.txt").read_bytes()
        stdout_bytes = (source_directory / "stdout.jsonl").read_bytes()
        stderr_bytes = (source_directory / "stderr.txt").read_bytes()
        prediction_path = source_directory / "prediction.json"
        record: dict[str, object] = {
            "schema_version": "cofactor9.1.transport-incident.v1",
            "sample_id": case.sample_id,
            "incident_number": incident_number,
            "tentative_attempt_number": tentative_attempt_number,
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_seconds": round(capture.duration_seconds, 6)
            if capture is not None
            else 0.0,
            "argv": list(capture.argv)
            if capture is not None
            else [str(self.executable), "<interrupted-before-capture>"],
            "model": MODEL,
            "reasoning_effort": REASONING_EFFORT,
            "service_tier": SERVICE_TIER,
            "returncode": capture.returncode if capture is not None else None,
            "timed_out": capture.timed_out if capture is not None else False,
            "cancelled": capture.cancelled if capture is not None else None,
            "start_error": (
                str(failure) if failure.code == "PROCESS_START_ERROR" else None
            ),
            "error_code": failure.code,
            "error_message": str(failure),
            "retry_disposition": failure.retry_disposition,
            "thread_id": None,
            "usage": {},
            "prompt_sha256": _sha256_bytes(prompt_bytes),
            "stdout_sha256": _sha256_bytes(stdout_bytes),
            "stderr_sha256": _sha256_bytes(stderr_bytes),
            "prediction_sha256": (
                _sha256_bytes(prediction_path.read_bytes())
                if prediction_path.is_file()
                else None
            ),
            "redaction_count": redaction_count,
            "environment_policy": "fixed-allowlist",
        }
        _publish_json_exclusive(
            source_directory / "incident.json",
            record,
            staging_directory=self._staging_directory,
        )
        _publish_directory_exclusive(
            source_directory,
            destination,
            staging_directory=self._staging_directory,
        )
        return destination

    def _finalize_capture(
        self,
        *,
        case: PromptCase,
        persisted_prompt: str,
        prompt_redactions: int,
        attempt_number: int,
        durable: _DurableCapture,
        control: _BatchControl | None,
    ) -> _AttemptState:
        """Sanitize, raw-replay, and atomically publish one reaped invocation."""

        capture = durable.capture
        inflight = _inflight_directory(self.run_dir, case.sample_id)
        stdout_path = inflight / "stdout.jsonl"
        stderr_path = inflight / "stderr.txt"
        raw_stdout = stdout_path.read_bytes()
        raw_stderr = stderr_path.read_bytes()
        if raw_stdout.decode("utf-8", errors="replace") != capture.stdout:
            raise RunnerError("durable stdout differs from launcher capture")
        if raw_stderr.decode("utf-8", errors="replace") != capture.stderr:
            raise RunnerError("durable stderr differs from launcher capture")
        sensitive_values = _sensitive_values(os.environ)
        stdout, stdout_redactions = _redact(capture.stdout, sensitive_values)
        stderr, stderr_redactions = _redact(capture.stderr, sensitive_values)
        sanitized_stdout = stdout.encode("utf-8")
        sanitized_stderr = stderr.encode("utf-8")
        if raw_stdout != sanitized_stdout:
            _replace_operational_bytes(
                stdout_path,
                sanitized_stdout,
                expected=raw_stdout,
                staging_directory=self._staging_directory,
            )
        if raw_stderr != sanitized_stderr:
            _replace_operational_bytes(
                stderr_path,
                sanitized_stderr,
                expected=raw_stderr,
                staging_directory=self._staging_directory,
            )

        replay = replay_codex_attempt(
            case=case,
            argv=capture.argv,
            stdout=stdout,
            stderr=stderr,
            returncode=capture.returncode,
            timed_out=capture.timed_out,
            cancelled=capture.cancelled,
            start_error=capture.start_error,
            duration_seconds=capture.duration_seconds,
        )
        failure: AttemptFailure | None = None
        if replay.error_code is not None:
            if replay.error_message is None:
                raise RunnerError("replayed failure lacks an error message")
            safe_message, message_redactions = _redact(
                replay.error_message, sensitive_values
            )
            failure = AttemptFailure(
                replay.error_code,
                safe_message,
                retry_disposition=replay.retry_disposition,
            )
        else:
            message_redactions = 0
        redaction_count = (
            prompt_redactions
            + stdout_redactions
            + stderr_redactions
            + message_redactions
        )

        if failure is not None and failure.code in _NON_BUDGET_ERROR_CODES:
            if control is not None and failure.code in SYSTEMATIC_FAILURE_CODES:
                control.abort_systematic(failure.code)
            self._write_transport_incident(
                case=case,
                tentative_attempt_number=attempt_number,
                source_directory=inflight,
                failure=failure,
                capture=capture,
                started_at=durable.started_at,
                completed_at=durable.completed_at,
                redaction_count=redaction_count,
            )
            _remove_active_state(self.run_dir, case.sample_id, attempt_number)
            if failure.code == "RUN_CANCELLED":
                raise _CaseCancelled(str(failure))
            raise SystematicFailure(
                sample_id=case.sample_id,
                error_code=failure.code,
                message=str(failure),
            )

        if replay.prediction is not None:
            _publish_json_exclusive(
                inflight / "prediction.json",
                replay.prediction.to_dict(),
                staging_directory=self._staging_directory,
            )
        attempt_record: dict[str, object] = {
            "schema_version": "cofactor9.1.attempt.v1",
            "sample_id": case.sample_id,
            "attempt_number": attempt_number,
            "started_at": durable.started_at,
            "completed_at": durable.completed_at,
            "duration_seconds": round(capture.duration_seconds, 6),
            "argv": list(capture.argv),
            "model": MODEL,
            "reasoning_effort": REASONING_EFFORT,
            "service_tier": SERVICE_TIER,
            "returncode": capture.returncode,
            "timed_out": capture.timed_out,
            "error_code": failure.code if failure else None,
            "error_message": str(failure) if failure else None,
            "retry_disposition": failure.retry_disposition if failure else "none",
            "thread_id": replay.thread_id,
            "usage": replay.usage,
            "prompt_sha256": _sha256_text(persisted_prompt),
            "stdout_sha256": _sha256_text(stdout),
            "stderr_sha256": _sha256_text(stderr),
            "redaction_count": redaction_count,
            "environment_policy": "fixed-allowlist",
        }
        _publish_json_exclusive(
            inflight / "attempt.json",
            attempt_record,
            staging_directory=self._staging_directory,
        )
        destination = (
            self.run_dir
            / "cases"
            / case.sample_id
            / "attempts"
            / f"attempt-{attempt_number:04d}"
        )
        _publish_directory_exclusive(
            inflight,
            destination,
            staging_directory=self._staging_directory,
        )
        _remove_active_state(self.run_dir, case.sample_id, attempt_number)
        return _AttemptState(
            number=attempt_number,
            error_code=failure.code if failure else None,
            error_message=str(failure) if failure else None,
            retry_disposition=(
                failure.retry_disposition if failure else "none"
            ),
            prediction=replay.prediction,
        )

    def _execute_plan(
        self,
        plan: _ResumePlan,
        *,
        control: _BatchControl | None,
    ) -> TerminalResult:
        case = plan.case
        prompt = render_prompt(case)
        attempt_number = plan.next_attempt_number
        budget_failures = plan.budget_failures
        terminal_path = self.run_dir / "cases" / case.sample_id / "terminal.json"
        cancellation_event = control.event if control is not None else None
        while budget_failures < self.max_attempts:
            if cancellation_event is not None and cancellation_event.is_set():
                raise _CaseCancelled("case stopped before its next attempt")
            _create_inflight_directory(
                run_dir=self.run_dir,
                sample_id=case.sample_id,
                attempt_number=attempt_number,
                prompt=plan.persisted_prompt,
                staging_directory=self._staging_directory,
            )
            durable = _invoke_codex(
                executable=self.executable,
                schema_path=self.schema_path,
                prompt=prompt,
                timeout_seconds=self.timeout_seconds,
                run_dir=self.run_dir,
                sample_id=case.sample_id,
                attempt_number=attempt_number,
                staging_directory=self._staging_directory,
                supervisor=self._supervisor,
                cancellation_event=cancellation_event,
            )
            state = self._finalize_capture(
                case=case,
                persisted_prompt=plan.persisted_prompt,
                prompt_redactions=plan.prompt_redactions,
                attempt_number=attempt_number,
                durable=durable,
                control=control,
            )
            if state.prediction is not None:
                result = TerminalResult(
                    sample_id=case.sample_id,
                    status="success",
                    attempt_count=attempt_number,
                    prediction=state.prediction,
                )
                self._write_terminal(terminal_path, case, result)
                return result
            if state.error_code is None or state.error_message is None:
                raise RunnerError("attempt has neither prediction nor failure")
            budget_failures += 1
            if control is not None and state.error_code in SYSTEMATIC_FAILURE_CODES:
                control.abort_systematic(state.error_code)
            if (
                state.retry_disposition == "nonretryable"
                or budget_failures >= self.max_attempts
            ):
                result = TerminalResult(
                    sample_id=case.sample_id,
                    status="terminal_error",
                    attempt_count=attempt_number,
                    error_code=state.error_code,
                    error_message=state.error_message,
                )
                self._write_terminal(terminal_path, case, result)
                if control is not None and state.error_code in SYSTEMATIC_FAILURE_CODES:
                    return result
                return result
            if control is not None and state.error_code in SYSTEMATIC_FAILURE_CODES:
                raise SystematicFailure(
                    sample_id=case.sample_id,
                    error_code=state.error_code,
                    message=state.error_message,
                )
            attempt_number += 1
        raise RunnerError("runner exhausted attempts without a classified outcome")

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

        if not self._batch_lock.acquire(blocking=False):
            raise RunnerError("this runner already has an active batch")
        try:
            results_by_index: dict[int, TerminalResult] = {}
            failures_by_index: dict[int, CaseRunFailure] = {}
            plans_by_index: dict[int, _ResumePlan] = {}
            for index, case in enumerate(validated_cases):
                try:
                    prepared = self._prepare_case(case, resume=resume)
                except Exception as error:
                    failures_by_index[index] = CaseRunFailure(
                        sample_id=case.sample_id,
                        error=error,
                    )
                else:
                    if isinstance(prepared, TerminalResult):
                        results_by_index[index] = prepared
                    else:
                        plans_by_index[index] = prepared
            if failures_by_index:
                raise RunCasesError(
                    completed_results=tuple(
                        results_by_index[index] for index in sorted(results_by_index)
                    ),
                    failures=tuple(
                        failures_by_index[index]
                        for index in sorted(failures_by_index)
                    ),
                )
            if not plans_by_index:
                return tuple(
                    results_by_index[index]
                    for index in range(len(validated_cases))
                )

            control = _BatchControl(self._supervisor)
            plan_indexes = sorted(plans_by_index)
            with _installed_signal_handlers(control):
                probe_index = plan_indexes.pop(0)
                probe_case = validated_cases[probe_index]
                try:
                    results_by_index[probe_index] = self._execute_plan(
                        plans_by_index[probe_index],
                        control=control,
                    )
                except SystematicFailure as error:
                    failures_by_index[probe_index] = CaseRunFailure(
                        sample_id=probe_case.sample_id,
                        error=error,
                    )
                except _CaseCancelled:
                    pass
                except Exception as error:
                    failures_by_index[probe_index] = CaseRunFailure(
                        sample_id=probe_case.sample_id,
                        error=error,
                    )
                    control.cancel()

                if not control.event.is_set() and not failures_by_index:
                    futures: dict[Future[TerminalResult], int] = {}
                    next_offset = 0
                    executor = ThreadPoolExecutor(
                        max_workers=concurrency,
                        thread_name_prefix="cofactor-case",
                    )
                    try:
                        def submit_until_full() -> None:
                            nonlocal next_offset
                            while (
                                not control.event.is_set()
                                and not failures_by_index
                                and len(futures) < concurrency
                                and next_offset < len(plan_indexes)
                            ):
                                index = plan_indexes[next_offset]
                                next_offset += 1
                                future = executor.submit(
                                    self._execute_plan,
                                    plans_by_index[index],
                                    control=control,
                                )
                                futures[future] = index

                        submit_until_full()
                        while futures:
                            done, _ = wait(
                                tuple(futures),
                                return_when=FIRST_COMPLETED,
                            )
                            for future in sorted(done, key=futures.__getitem__):
                                index = futures.pop(future)
                                case = validated_cases[index]
                                try:
                                    result = future.result()
                                except SystematicFailure as error:
                                    failures_by_index[index] = CaseRunFailure(
                                        sample_id=case.sample_id,
                                        error=error,
                                    )
                                except _CaseCancelled:
                                    pass
                                except Exception as error:
                                    failures_by_index[index] = CaseRunFailure(
                                        sample_id=case.sample_id,
                                        error=error,
                                    )
                                    control.cancel()
                                else:
                                    results_by_index[index] = result
                            submit_until_full()
                    finally:
                        if control.event.is_set() or failures_by_index:
                            control.cancel(
                                signal_number=control.signal_number
                            )
                            for future in futures:
                                future.cancel()
                        executor.shutdown(wait=True, cancel_futures=True)

            completed_results = tuple(
                results_by_index[index] for index in sorted(results_by_index)
            )
            failures = tuple(
                failures_by_index[index] for index in sorted(failures_by_index)
            )
            pending_sample_ids = tuple(
                validated_cases[index].sample_id
                for index in sorted(plans_by_index)
                if index not in results_by_index
            )
            if control.signal_number is not None:
                raise RunCancelled(
                    signal_number=control.signal_number,
                    completed_results=completed_results,
                    failures=failures,
                    pending_sample_ids=pending_sample_ids,
                )
            if control.systematic_code is not None:
                raise RunAborted(
                    error_code=control.systematic_code,
                    threshold=self.circuit_breaker_threshold,
                    completed_results=completed_results,
                    failures=failures,
                    pending_sample_ids=pending_sample_ids,
                )
            if failures:
                raise RunCasesError(
                    completed_results=completed_results,
                    failures=failures,
                )
            return tuple(
                results_by_index[index] for index in range(len(validated_cases))
            )
        finally:
            self._batch_lock.release()

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
            _publish_json_exclusive(
                terminal_path,
                payload,
                staging_directory=self._staging_directory,
            )
        except FileExistsError as error:
            raise CompletedCaseError(
                f"terminal record already exists for {case.sample_id}"
            ) from error


__all__ = [
    "AttemptReplayResult",
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
    "RunCancelled",
    "RunCasesError",
    "RunnerError",
    "SERVICE_TIER",
    "SYSTEMATIC_FAILURE_CODES",
    "SystematicFailure",
    "TerminalResult",
    "build_codex_argv",
    "replay_codex_attempt",
]
