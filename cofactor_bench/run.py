"""Fail-closed orchestration for immutable, resumable benchmark runs.

The transport implementation owns individual attempts and terminal records.
This module owns the enclosing run identity: it binds a run ID to exact input,
model, schema, executable and execution settings before the first case starts.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
import platform
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from types import MappingProxyType
from typing import Any, Protocol

from .build import FORMULA_RULE_VERSION
from .cases import (
    EXPECTED_CASE_COUNT,
    case_artifact_paths,
    load_prompt_cases,
    validate_cases_from_config,
)
from .prediction import SCHEMA_VERSION as RESPONSE_SCHEMA_VERSION, parse_prediction_json
from .prompt import PROMPT_VERSION, PromptCase, render_prompt
from .deepseek_adapter import (
    API_ENDPOINT as DEEPSEEK_API_ENDPOINT,
    MAX_OUTPUT_TOKENS as DEEPSEEK_MAX_OUTPUT_TOKENS,
    MODEL as DEEPSEEK_MODEL,
    REASONING_EFFORT as DEEPSEEK_REASONING_EFFORT,
    SERVICE_TIER as DEEPSEEK_SERVICE_TIER,
    THINKING_TYPE as DEEPSEEK_THINKING_TYPE,
)
from .runner import (
    DEFAULT_CIRCUIT_BREAKER_THRESHOLD,
    DISABLED_FEATURES,
    MAX_ATTEMPTS,
    MAX_CONCURRENCY,
    MODEL,
    REASONING_EFFORT,
    RunnerModelSettings,
    SERVICE_TIER,
    SYSTEMATIC_FAILURE_CODES,
    CodexExecRunner,
    TerminalResult,
    _load_terminal,
    build_codex_argv,
    parse_codex_stdout,
    replay_codex_attempt,
)


RUN_MANIFEST_SCHEMA_VERSION = "cofactor9.1.run-manifest.v1"
TERMINAL_SCHEMA_VERSION = "cofactor9.1.terminal.v1"
DEFAULT_TIMEOUT_SECONDS = 600.0
FROZEN_RESPONSE_SCHEMA_NAME = "response-schema.json"
FROZEN_CODEX_EXECUTABLE_NAME = "codex-executable"
INVOCATION_SCHEMA_VERSION = "cofactor9.1.run-invocation.v1"
INVOCATION_END_SCHEMA_VERSION = "cofactor9.1.run-invocation-end.v1"
TRANSPORT_INCIDENT_SCHEMA_VERSION = "cofactor9.1.transport-incident.v1"

_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]*\Z")
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
_ATTEMPT_REQUIRED_FILES = frozenset(
    {"attempt.json", "prompt.txt", "stdout.jsonl", "stderr.txt"}
)
_ATTEMPT_ALLOWED_FILES = _ATTEMPT_REQUIRED_FILES | {"prediction.json"}
_INCIDENT_REQUIRED_FILES = frozenset(
    {"incident.json", "prompt.txt", "stdout.jsonl", "stderr.txt"}
)
_INCIDENT_ALLOWED_FILES = _INCIDENT_REQUIRED_FILES | {"prediction.json"}
_INCIDENT_FIELDS = _ATTEMPT_FIELDS - {"attempt_number"} | {
    "incident_number",
    "tentative_attempt_number",
    "prediction_sha256",
    "cancelled",
    "start_error",
}
_KNOWN_INCIDENT_CODES = frozenset(
    {"AUTH_ERROR", "CAPACITY_ERROR", "PROCESS_START_ERROR", "TRANSPORT_ERROR",
     "RUN_CANCELLED", "INTERRUPTED_ATTEMPT"}
)
_RUN_TOP_LEVEL_ALLOWED = frozenset(
    {
        "manifest.json",
        FROZEN_RESPONSE_SCHEMA_NAME,
        FROZEN_CODEX_EXECUTABLE_NAME,
        "cases",
        "transport-incidents",
        "invocations",
        ".staging",
        ".inflight",
        ".active",
        "metrics.json",
        "report.md",
    }
)
_EVALUATION_ARTIFACT_KEYS = (
    "public_cases",
    "case_manifest",
    "private_mapping",
    "full_structured",
    "core_provisional",
    "label_catalog",
    "ontology_audit",
    "view_audit",
    "homology_clusters",
    "homology_clusters_manifest",
)
_EVALUATION_IMPLEMENTATION_KEYS = (
    "reporting",
    "scoring",
    "metrics",
    "prediction",
)
_INVOCATION_COMMON_FIELDS = frozenset(
    {
        "invocation_number",
        "run_id",
        "run_manifest_sha256",
        "config_sha256",
        "argv",
        "resume",
        "limit",
        "infrastructure_gate",
        "scheduling",
    }
)
_INVOCATION_START_FIELDS = _INVOCATION_COMMON_FIELDS | {
    "schema_version",
    "started_at",
    "status",
}
_INVOCATION_END_FIELDS = _INVOCATION_COMMON_FIELDS | {
    "schema_version",
    "completed_at",
    "status",
    "error_type",
}
_RUN_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "dataset_version",
        "mode",
        "created_at",
        "contract_sha256",
        "contract",
    }
)


class RunContractError(RuntimeError):
    """Raised before execution or validation when a run contract is unsafe."""


class _Runner(Protocol):
    def run_cases(
        self,
        cases: list[PromptCase] | tuple[PromptCase, ...],
        *,
        resume: bool = False,
        concurrency: int = 1,
    ) -> tuple[TerminalResult, ...]: ...


@dataclass(frozen=True, slots=True)
class RunValidationSummary:
    """Auditable progress for one immutable run directory."""

    run_id: str
    mode: str
    selected_case_count: int
    terminal_count: int
    success_count: int
    terminal_error_count: int
    missing_terminal_count: int
    manifest_sha256: str
    attempt_count: int = 0
    incident_count: int = 0
    invocation_count: int = 0
    total_duration_seconds: float = 0.0
    usage: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    incident_error_code_counts: Mapping[str, int] = field(
        default_factory=lambda: MappingProxyType({})
    )
    incident_composite_sha256: str = hashlib.sha256(b"").hexdigest()
    invocation_composite_sha256: str = hashlib.sha256(b"").hexdigest()
    ledger_composite_sha256: str = hashlib.sha256(b"").hexdigest()

    @property
    def is_complete(self) -> bool:
        return self.terminal_count == self.selected_case_count

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "selected_case_count": self.selected_case_count,
            "terminal_count": self.terminal_count,
            "success_count": self.success_count,
            "terminal_error_count": self.terminal_error_count,
            "missing_terminal_count": self.missing_terminal_count,
            "is_complete": self.is_complete,
            "manifest_sha256": self.manifest_sha256,
            "attempt_count": self.attempt_count,
            "incident_count": self.incident_count,
            "invocation_count": self.invocation_count,
            "total_duration_seconds": self.total_duration_seconds,
            "usage": dict(self.usage),
            "incident_error_code_counts": dict(self.incident_error_code_counts),
            "incident_composite_sha256": self.incident_composite_sha256,
            "invocation_composite_sha256": self.invocation_composite_sha256,
            "ledger_composite_sha256": self.ledger_composite_sha256,
        }


@dataclass(frozen=True, slots=True)
class LedgerBundle:
    """Exact immutable bytes for one attempt or transport incident."""

    number: int
    record_bytes: bytes
    prompt_bytes: bytes
    stdout_bytes: bytes
    stderr_bytes: bytes
    prediction_bytes: bytes | None = None


@dataclass(frozen=True, slots=True)
class InvocationBundle:
    """Exact immutable bytes for one closed run-level invocation."""

    number: int
    start_bytes: bytes
    end_bytes: bytes


@dataclass(frozen=True, slots=True)
class VerifiedRunSnapshot:
    """A byte-stable, fully validated scoring input with no live-file reads."""

    run_id: str
    run_dir: Path
    validation: RunValidationSummary
    manifest_bytes: bytes
    manifest_sha256: str
    evaluation_implementation_sha256: Mapping[str, str]
    artifact_bytes: Mapping[str, bytes]
    artifact_sha256: Mapping[str, str]
    terminal_records: Mapping[str, bytes]
    attempt_bundles: Mapping[str, tuple[LedgerBundle, ...]]
    incident_bundles: Mapping[str, tuple[LedgerBundle, ...]]
    invocation_bundles: tuple[InvocationBundle, ...]
    ledger_composite_sha256: str
    provenance_composite_sha256: str


@dataclass(frozen=True, slots=True)
class _RunInputs:
    config_path: Path
    project_root: Path
    config_bytes: bytes
    config: Mapping[str, Any]
    cases_path: Path
    cases_manifest_path: Path
    cases_bytes: bytes
    cases_manifest_bytes: bytes
    private_mapping_path: Path
    private_mapping_bytes: bytes
    cases: tuple[PromptCase, ...]
    response_schema_path: Path
    response_schema_bytes: bytes
    runs_root: Path
    artifact_paths: Mapping[str, Path]
    artifact_bytes: Mapping[str, bytes]
    artifact_metadata: Mapping[str, Mapping[str, object]]


@dataclass(frozen=True, slots=True)
class _BinaryIdentity:
    requested: str
    launcher_kind: str
    launcher_path: Path
    launcher_sha256: str
    resolved_path: Path
    sha256: str
    version: str
    disabled_feature_states: Mapping[str, bool]
    executable_bytes: bytes = field(repr=False, compare=False)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _reject_constant(value: str) -> None:
    raise RunContractError(f"non-finite JSON number {value!r} is forbidden")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RunContractError(f"duplicate JSON field {key!r} is forbidden")
        result[key] = value
    return result


def _decode_json_object(value: bytes, *, location: str) -> dict[str, Any]:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RunContractError(f"{location} is not UTF-8") from error
    try:
        decoded = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except RunContractError:
        raise
    except json.JSONDecodeError as error:
        raise RunContractError(f"{location} is not valid JSON: {error.msg}") from error
    if not isinstance(decoded, dict):
        raise RunContractError(f"{location} must contain one JSON object")
    return decoded


def _decode_jsonl_objects(
    value: bytes,
    *,
    location: str,
) -> tuple[dict[str, Any], ...]:
    """Strictly decode canonical JSONL without accepting blank records."""

    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RunContractError(f"{location} is not UTF-8") from error
    if value and not value.endswith(b"\n"):
        raise RunContractError(f"{location} must end with one JSONL newline")
    records: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line:
            raise RunContractError(f"{location} contains blank JSONL line {number}")
        records.append(
            _decode_json_object(
                line.encode("utf-8"),
                location=f"{location} line {number}",
            )
        )
    return tuple(records)


def _artifact_descriptor(
    *,
    path: Path,
    project_root: Path,
    value: bytes,
    schema_version: str,
    record_count: int,
    visibility: str = "frozen-evaluation-input",
) -> Mapping[str, object]:
    if not schema_version:
        raise RunContractError(f"artifact {path} has no schema_version")
    if isinstance(record_count, bool) or not isinstance(record_count, int) or record_count < 0:
        raise RunContractError(f"artifact {path} has an invalid record count")
    return MappingProxyType(
        {
            "path": _relative_display(path, project_root),
            "sha256": _sha256(value),
            "byte_count": len(value),
            "schema_version": schema_version,
            "record_count": record_count,
            "visibility": visibility,
        }
    )


def _read_bytes(path: Path, *, location: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise RunContractError(f"cannot read {location} {path}: {error}") from error


def _required_mapping(
    value: Mapping[str, Any],
    key: str,
    *,
    location: str,
) -> Mapping[str, Any]:
    result = value.get(key)
    if not isinstance(result, Mapping):
        raise RunContractError(f"{location}.{key} must be an object")
    return result


def _required_string(
    value: Mapping[str, Any],
    key: str,
    *,
    location: str,
) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise RunContractError(f"{location}.{key} must be a nonempty string")
    return result


def _required_int(
    value: Mapping[str, Any],
    key: str,
    *,
    location: str,
) -> int:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, int):
        raise RunContractError(f"{location}.{key} must be an integer")
    return result


def _resolve_project_path(root: Path, configured: str, *, key: str) -> Path:
    relative = Path(configured)
    if relative.is_absolute():
        raise RunContractError(f"configured path {key!r} must be relative")
    resolved_root = root.resolve()
    resolved = (resolved_root / relative).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise RunContractError(f"configured path {key!r} escapes the project root")
    return resolved


def _validate_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise RunContractError(
            "run_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}"
        )
    if run_id in {".", ".."}:
        raise RunContractError("run_id may not be '.' or '..'")


def _load_inputs(config_path: str | Path) -> _RunInputs:
    config_file = Path(config_path).resolve()
    config_bytes = _read_bytes(config_file, location="benchmark config")
    config = _decode_json_object(config_bytes, location="benchmark config")
    if config.get("schema_version") != "cofactor9.1.config.v1":
        raise RunContractError("benchmark config schema_version is unsupported")
    project_root = config_file.parent.parent.resolve()
    paths = _required_mapping(config, "paths", location="benchmark config")
    cases_configured = _required_string(
        paths, "cases", location="benchmark config.paths"
    )
    cases_path = _resolve_project_path(
        project_root, cases_configured, key="paths.cases"
    )
    case_paths = case_artifact_paths(cases_path)
    cases_manifest_path = case_paths.manifest
    private_mapping_path = case_paths.private_mapping
    cases_bytes = _read_bytes(cases_path, location="public cases")
    case_manifest_bytes = _read_bytes(
        cases_manifest_path, location="case artifact manifest"
    )
    case_manifest = _decode_json_object(
        case_manifest_bytes, location="case artifact manifest"
    )
    if case_manifest.get("schema_version") != "cofactor9.1.case-artifacts.v1":
        raise RunContractError("case artifact manifest schema_version is unsupported")
    if case_manifest.get("dataset_version") != config.get("dataset_version"):
        raise RunContractError("case artifact dataset_version differs from config")
    if case_manifest.get("prompt_version") != PROMPT_VERSION:
        raise RunContractError("case artifact prompt_version differs from code")
    counts = _required_mapping(
        case_manifest, "counts", location="case artifact manifest"
    )
    output_hashes = _required_mapping(
        case_manifest, "output_sha256", location="case artifact manifest"
    )
    if output_hashes.get("prompt_cases") != _sha256(cases_bytes):
        raise RunContractError("public cases SHA-256 differs from case manifest")
    cases = load_prompt_cases(cases_path)
    if _read_bytes(cases_path, location="public cases after parsing") != cases_bytes:
        raise RunContractError("public cases changed while being loaded")
    if counts.get("prompt_cases") != len(cases):
        raise RunContractError("public case count differs from case manifest")
    private_mapping_bytes = _read_bytes(
        private_mapping_path,
        location="private case mapping",
    )
    if output_hashes.get("private_mapping") != _sha256(private_mapping_bytes):
        raise RunContractError("private mapping SHA-256 differs from case manifest")
    private_contract = _required_mapping(
        case_manifest,
        "private_mapping",
        location="case artifact manifest",
    )
    if (
        private_contract.get("visibility") != "private"
        or private_contract.get("file_mode") != "0600"
        or stat.S_IMODE(private_mapping_path.stat().st_mode) != 0o600
    ):
        raise RunContractError("private mapping visibility or file mode is unsafe")

    configured_artifacts = {
        "full_structured": "full_structured",
        "core_provisional": "core_provisional",
        "label_catalog": "label_catalog",
        "homology_clusters": "homology_clusters",
        "homology_clusters_manifest": "homology_clusters_manifest",
    }
    artifact_paths: dict[str, Path] = {
        "public_cases": cases_path,
        "case_manifest": cases_manifest_path,
        "private_mapping": private_mapping_path,
    }
    for name, key in configured_artifacts.items():
        configured = _required_string(
            paths,
            key,
            location="benchmark config.paths",
        )
        artifact_paths[name] = _resolve_project_path(
            project_root,
            configured,
            key=f"paths.{key}",
        )
    full_parent = artifact_paths["full_structured"].parent
    artifact_paths["ontology_audit"] = full_parent / "ontology_audit.json"
    artifact_paths["view_audit"] = full_parent / "view_audit.json"
    for name, path in artifact_paths.items():
        if path.resolve() != path or not path.resolve().is_relative_to(project_root):
            raise RunContractError(f"evaluation artifact {name} is outside project root")

    artifact_bytes: dict[str, bytes] = {
        "public_cases": cases_bytes,
        "case_manifest": case_manifest_bytes,
        "private_mapping": private_mapping_bytes,
    }
    for name in _EVALUATION_ARTIFACT_KEYS:
        if name not in artifact_bytes:
            artifact_bytes[name] = _read_bytes(
                artifact_paths[name],
                location=f"evaluation artifact {name}",
            )

    public_records = _decode_jsonl_objects(cases_bytes, location="public cases")
    private_records = _decode_jsonl_objects(
        private_mapping_bytes,
        location="private case mapping",
    )
    full_records = _decode_jsonl_objects(
        artifact_bytes["full_structured"],
        location="Full-Structured",
    )
    core_records = _decode_jsonl_objects(
        artifact_bytes["core_provisional"],
        location="Core-Provisional",
    )
    cluster_records = _decode_jsonl_objects(
        artifact_bytes["homology_clusters"],
        location="homology clusters",
    )
    label_catalog = _decode_json_object(
        artifact_bytes["label_catalog"],
        location="label catalog",
    )
    ontology_audit = _decode_json_object(
        artifact_bytes["ontology_audit"],
        location="ontology audit",
    )
    view_audit = _decode_json_object(
        artifact_bytes["view_audit"],
        location="view audit",
    )
    cluster_manifest = _decode_json_object(
        artifact_bytes["homology_clusters_manifest"],
        location="homology cluster manifest",
    )

    dataset_version = _required_string(
        config,
        "dataset_version",
        location="benchmark config",
    )
    object_artifacts = {
        "case_manifest": case_manifest,
        "label_catalog": label_catalog,
        "ontology_audit": ontology_audit,
        "view_audit": view_audit,
        "homology_clusters_manifest": cluster_manifest,
    }
    for name, payload in object_artifacts.items():
        if payload.get("dataset_version") != dataset_version:
            raise RunContractError(
                f"evaluation artifact {name} dataset_version differs from config"
            )
    view_rule_version = _required_string(
        case_manifest,
        "view_rule_version",
        location="case artifact manifest",
    )
    if view_audit.get("rule_version") != view_rule_version:
        raise RunContractError("view audit rule_version differs from case manifest")
    if cluster_manifest.get("view_rule_version") != view_rule_version:
        raise RunContractError(
            "homology cluster view_rule_version differs from case manifest"
        )
    if label_catalog.get("catalog_version") != case_manifest.get("catalog_version"):
        raise RunContractError("label catalog version differs from case manifest")
    jsonl_groups = {
        "public_cases": public_records,
        "private_mapping": private_records,
        "full_structured": full_records,
        "core_provisional": core_records,
        "homology_clusters": cluster_records,
    }
    expected_schemas = {
        "public_cases": case_manifest.get("public_case_schema_version"),
        "private_mapping": private_contract.get("schema_version"),
        "full_structured": "cofactor9.1.view-record.v1",
        "core_provisional": "cofactor9.1.view-record.v1",
        "homology_clusters": cluster_manifest.get("cluster_record_schema_version"),
    }
    for name, records in jsonl_groups.items():
        schema = expected_schemas[name]
        if not isinstance(schema, str) or not schema:
            raise RunContractError(f"evaluation artifact {name} schema is undeclared")
        if name != "public_cases" and any(
            record.get("schema_version") != schema for record in records
        ):
            raise RunContractError(
                f"evaluation artifact {name} contains a schema_version mismatch"
            )

    expected_count = len(cases)
    count_expectations = {
        "public_cases": expected_count,
        "private_mapping": expected_count,
        "full_structured": expected_count,
        "homology_clusters": expected_count,
    }
    for name, expected in count_expectations.items():
        if len(jsonl_groups[name]) != expected:
            raise RunContractError(
                f"evaluation artifact {name} count differs from public cases"
            )
    if len(core_records) > expected_count:
        raise RunContractError("Core-Provisional count exceeds Full-Structured")

    full_accessions = tuple(
        _required_string(
            _required_mapping(record, "entry", location="Full-Structured record"),
            "accession",
            location="Full-Structured record.entry",
        )
        for record in full_records
    )
    if len(set(full_accessions)) != expected_count:
        raise RunContractError("Full-Structured accessions are not unique")
    private_sample_ids = tuple(
        _required_string(record, "sample_id", location="private mapping record")
        for record in private_records
    )
    private_accessions = tuple(
        _required_string(record, "accession", location="private mapping record")
        for record in private_records
    )
    if private_sample_ids != tuple(case.sample_id for case in cases):
        raise RunContractError("private mapping sample order differs from public cases")
    if set(private_accessions) != set(full_accessions):
        raise RunContractError("private mapping accessions differ from Full-Structured")
    core_accessions = {
        _required_string(
            _required_mapping(record, "entry", location="Core-Provisional record"),
            "accession",
            location="Core-Provisional record.entry",
        )
        for record in core_records
    }
    if len(core_accessions) != len(core_records):
        raise RunContractError("Core-Provisional accessions are not unique")
    if not core_accessions.issubset(set(full_accessions)):
        raise RunContractError("Core-Provisional is not a subset of Full-Structured")
    cluster_accessions = tuple(
        _required_string(record, "accession", location="homology cluster record")
        for record in cluster_records
    )
    if set(cluster_accessions) != set(full_accessions):
        raise RunContractError("homology cluster accessions differ from Full-Structured")

    labels = label_catalog.get("labels")
    if not isinstance(labels, list):
        raise RunContractError("label catalog labels must be an array")
    catalog_ids = tuple(
        _required_string(label, "chebi_id", location="label catalog label")
        if isinstance(label, Mapping)
        else ""
        for label in labels
    )
    if any(not value for value in catalog_ids) or len(set(catalog_ids)) != len(catalog_ids):
        raise RunContractError("label catalog identifiers are invalid or duplicated")
    if counts.get("catalog_terms") != len(catalog_ids):
        raise RunContractError("label catalog count differs from case manifest")
    if any(tuple(case.allowed_labels) != catalog_ids for case in cases):
        raise RunContractError("public case catalog differs from label catalog")

    case_inputs = _required_mapping(
        case_manifest,
        "input_sha256",
        location="case artifact manifest",
    )
    if case_inputs.get("full_structured") != _sha256(artifact_bytes["full_structured"]):
        raise RunContractError("case manifest Full-Structured hash mismatch")
    if case_inputs.get("label_catalog") != _sha256(artifact_bytes["label_catalog"]):
        raise RunContractError("case manifest label catalog hash mismatch")
    view_outputs = _required_mapping(
        view_audit,
        "output_artifact_sha256",
        location="view audit",
    )
    for name in ("full_structured", "core_provisional", "label_catalog", "ontology_audit"):
        if view_outputs.get(name) != _sha256(artifact_bytes[name]):
            raise RunContractError(f"view audit {name} hash mismatch")
    view_summary = _required_mapping(view_audit, "summary", location="view audit")
    if view_summary.get("full_structured_accessions") != expected_count:
        raise RunContractError("view audit Full-Structured count mismatch")
    if view_summary.get("core_provisional_accessions") != len(core_records):
        raise RunContractError("view audit Core-Provisional count mismatch")

    cluster_inputs = _required_mapping(
        cluster_manifest,
        "input_sha256",
        location="homology cluster manifest",
    )
    cluster_outputs = _required_mapping(
        cluster_manifest,
        "output_sha256",
        location="homology cluster manifest",
    )
    cluster_counts = _required_mapping(
        cluster_manifest,
        "counts",
        location="homology cluster manifest",
    )
    if cluster_inputs.get("full_structured") != _sha256(artifact_bytes["full_structured"]):
        raise RunContractError("homology cluster input hash mismatch")
    if cluster_outputs.get("homology_clusters") != _sha256(
        artifact_bytes["homology_clusters"]
    ):
        raise RunContractError("homology cluster output hash mismatch")
    for key in ("input_records", "output_records", "unique_accessions"):
        if cluster_counts.get(key) != expected_count:
            raise RunContractError(f"homology cluster manifest {key} mismatch")

    artifact_metadata: dict[str, Mapping[str, object]] = {}
    artifact_metadata["public_cases"] = _artifact_descriptor(
        path=cases_path,
        project_root=project_root,
        value=cases_bytes,
        schema_version=str(expected_schemas["public_cases"]),
        record_count=len(public_records),
        visibility="public-model-input",
    )
    artifact_metadata["case_manifest"] = _artifact_descriptor(
        path=cases_manifest_path,
        project_root=project_root,
        value=case_manifest_bytes,
        schema_version=str(case_manifest.get("schema_version", "")),
        record_count=1,
    )
    artifact_metadata["private_mapping"] = _artifact_descriptor(
        path=private_mapping_path,
        project_root=project_root,
        value=private_mapping_bytes,
        schema_version=str(expected_schemas["private_mapping"]),
        record_count=len(private_records),
        visibility="private-hash-only",
    )
    record_metadata = {
        "full_structured": (str(expected_schemas["full_structured"]), len(full_records)),
        "core_provisional": (str(expected_schemas["core_provisional"]), len(core_records)),
        "homology_clusters": (str(expected_schemas["homology_clusters"]), len(cluster_records)),
        "label_catalog": (str(label_catalog.get("schema_version", "")), len(labels)),
        "ontology_audit": (
            str(ontology_audit.get("schema_version", "")),
            len(ontology_audit.get("pairs", []))
            if isinstance(ontology_audit.get("pairs"), list)
            else -1,
        ),
        "view_audit": (str(view_audit.get("schema_version", "")), 1),
        "homology_clusters_manifest": (
            str(cluster_manifest.get("schema_version", "")),
            1,
        ),
    }
    for name, (schema, record_count) in record_metadata.items():
        artifact_metadata[name] = _artifact_descriptor(
            path=artifact_paths[name],
            project_root=project_root,
            value=artifact_bytes[name],
            schema_version=schema,
            record_count=record_count,
        )

    runs_configured = _required_string(
        paths, "runs", location="benchmark config.paths"
    )
    runs_root = _resolve_project_path(
        project_root, runs_configured, key="paths.runs"
    )
    schema_path = _resolve_project_path(
        project_root,
        "schemas/model-response.schema.json",
        key="response schema",
    )
    schema_bytes = _read_bytes(schema_path, location="response schema")
    return _RunInputs(
        config_path=config_file,
        project_root=project_root,
        config_bytes=config_bytes,
        config=config,
        cases_path=cases_path,
        cases_manifest_path=cases_manifest_path,
        cases_bytes=cases_bytes,
        cases_manifest_bytes=case_manifest_bytes,
        private_mapping_path=private_mapping_path,
        private_mapping_bytes=private_mapping_bytes,
        cases=cases,
        response_schema_path=schema_path,
        response_schema_bytes=schema_bytes,
        runs_root=runs_root,
        artifact_paths=MappingProxyType(artifact_paths),
        artifact_bytes=MappingProxyType(artifact_bytes),
        artifact_metadata=MappingProxyType(artifact_metadata),
    )


def _runner_model_settings(config: Mapping[str, Any]) -> RunnerModelSettings:
    run = _required_mapping(config, "run", location="benchmark config")
    transport = _required_string(run, "transport", location="benchmark config.run")
    model = _required_mapping(config, "model", location="benchmark config")
    if transport == "codex_cli_chatgpt_oauth":
        expected_runtime = (MODEL, REASONING_EFFORT, SERVICE_TIER)
    elif transport == "deepseek_official_api":
        expected_runtime = (
            DEEPSEEK_MODEL,
            DEEPSEEK_REASONING_EFFORT,
            DEEPSEEK_SERVICE_TIER,
        )
    else:
        raise RunContractError("benchmark config run.transport is unsupported")
    expected = {
        "name": expected_runtime[0],
        "reasoning_effort": expected_runtime[1],
        "service_tier": expected_runtime[2],
        "prompt_version": PROMPT_VERSION,
        "response_schema_version": RESPONSE_SCHEMA_VERSION,
    }
    for key, expected_value in expected.items():
        if model.get(key) != expected_value:
            raise RunContractError(
                f"benchmark config model.{key} must be {expected_value!r}"
            )
    return RunnerModelSettings(
        model=expected_runtime[0],
        reasoning_effort=expected_runtime[1],
        service_tier=expected_runtime[2],
    )


def _validate_fixed_model_contract(config: Mapping[str, Any]) -> None:
    _runner_model_settings(config)


def _contract_model_settings(
    contract: Mapping[str, Any],
) -> RunnerModelSettings:
    model = _required_mapping(contract, "model", location="run manifest.contract")
    return RunnerModelSettings(
        model=_required_string(model, "name", location="run manifest model"),
        reasoning_effort=_required_string(
            model, "reasoning_effort", location="run manifest model"
        ),
        service_tier=_required_string(
            model, "service_tier", location="run manifest model"
        ),
    )


def _configured_run_settings(
    config: Mapping[str, Any],
    *,
    concurrency: int | None,
    timeout_seconds: float | None,
    circuit_breaker_threshold: int | None,
) -> tuple[int, int, float, int, str]:
    run = _required_mapping(config, "run", location="benchmark config")
    transport = _required_string(run, "transport", location="benchmark config.run")
    if transport not in {
        "codex_cli_chatgpt_oauth",
        "deepseek_official_api",
    }:
        raise RunContractError("benchmark config run.transport is unsupported")
    max_attempts = _required_int(
        run, "max_attempts", location="benchmark config.run"
    )
    if max_attempts != MAX_ATTEMPTS:
        raise RunContractError(f"run.max_attempts must be exactly {MAX_ATTEMPTS}")

    configured_concurrency = _required_int(
        run, "concurrency", location="benchmark config.run"
    )
    if not 1 <= configured_concurrency <= MAX_CONCURRENCY:
        raise RunContractError(
            f"run.concurrency must be from 1 through {MAX_CONCURRENCY}"
        )
    effective_concurrency = (
        configured_concurrency if concurrency is None else concurrency
    )
    if (
        isinstance(effective_concurrency, bool)
        or not isinstance(effective_concurrency, int)
        or not 1 <= effective_concurrency <= MAX_CONCURRENCY
    ):
        raise RunContractError(
            f"concurrency must be an integer from 1 through {MAX_CONCURRENCY}"
        )
    if effective_concurrency != configured_concurrency:
        raise RunContractError("concurrency override must equal benchmark config")

    configured_timeout = run.get("timeout_seconds")
    if (
        isinstance(configured_timeout, bool)
        or not isinstance(configured_timeout, (int, float))
        or not math.isfinite(float(configured_timeout))
        or not 0 < float(configured_timeout) < 86_400
    ):
        raise RunContractError(
            "run.timeout_seconds must be positive, finite, and below one day"
        )
    effective_timeout = configured_timeout if timeout_seconds is None else timeout_seconds
    if (
        isinstance(effective_timeout, bool)
        or not isinstance(effective_timeout, (int, float))
        or not math.isfinite(float(effective_timeout))
        or not 0 < float(effective_timeout) < 86_400
    ):
        raise RunContractError("timeout_seconds must be positive and below one day")
    if float(effective_timeout) != float(configured_timeout):
        raise RunContractError("timeout_seconds override must equal benchmark config")

    configured_breaker = run.get("circuit_breaker_threshold")
    if (
        isinstance(configured_breaker, bool)
        or not isinstance(configured_breaker, int)
        or configured_breaker != DEFAULT_CIRCUIT_BREAKER_THRESHOLD
    ):
        raise RunContractError("circuit_breaker_threshold must be exactly 1")
    effective_breaker = (
        configured_breaker
        if circuit_breaker_threshold is None
        else circuit_breaker_threshold
    )
    if (
        isinstance(effective_breaker, bool)
        or not isinstance(effective_breaker, int)
        or effective_breaker != DEFAULT_CIRCUIT_BREAKER_THRESHOLD
    ):
        raise RunContractError("circuit_breaker_threshold must be exactly 1")
    if effective_breaker != configured_breaker:
        raise RunContractError(
            "circuit_breaker_threshold override must equal benchmark config"
        )
    return (
        max_attempts,
        effective_concurrency,
        float(effective_timeout),
        effective_breaker,
        transport,
    )


def _codex_native_target() -> tuple[str, str]:
    key = (platform.system(), platform.machine().casefold())
    targets = {
        ("Darwin", "arm64"): (
            "@openai/codex-darwin-arm64",
            "aarch64-apple-darwin",
        ),
        ("Darwin", "aarch64"): (
            "@openai/codex-darwin-arm64",
            "aarch64-apple-darwin",
        ),
        ("Darwin", "x86_64"): (
            "@openai/codex-darwin-x64",
            "x86_64-apple-darwin",
        ),
        ("Linux", "aarch64"): (
            "@openai/codex-linux-arm64",
            "aarch64-unknown-linux-musl",
        ),
        ("Linux", "arm64"): (
            "@openai/codex-linux-arm64",
            "aarch64-unknown-linux-musl",
        ),
        ("Linux", "x86_64"): (
            "@openai/codex-linux-x64",
            "x86_64-unknown-linux-musl",
        ),
        ("Linux", "amd64"): (
            "@openai/codex-linux-x64",
            "x86_64-unknown-linux-musl",
        ),
    }
    try:
        return targets[key]
    except KeyError as error:
        raise RunContractError(
            f"the Codex Node wrapper platform is unsupported: {key!r}"
        ) from error


def _resolve_codex_execution_path(
    launcher_path: Path,
    launcher_bytes: bytes,
) -> tuple[str, Path]:
    """Resolve the npm Node launcher to the native process it would spawn."""

    first_line = launcher_bytes.splitlines()[:1]
    is_node_launcher = bool(first_line) and b"node" in first_line[0]
    if not is_node_launcher:
        return "direct", launcher_path
    required_markers = (
        b"PLATFORM_PACKAGE_BY_TARGET",
        b"targetTriple",
        b"findCodexExecutable",
        b"binaryPath",
    )
    if any(marker not in launcher_bytes for marker in required_markers):
        raise RunContractError(
            "Codex Node launcher shape is unsupported; native executable cannot be frozen"
        )
    package_name, target = _codex_native_target()
    package_leaf = package_name.removeprefix("@openai/")
    package_root = launcher_path.parent.parent
    executable_name = "codex.exe" if platform.system() == "Windows" else "codex"
    candidates = (
        package_root
        / "node_modules"
        / "@openai"
        / package_leaf
        / "vendor"
        / target
        / "bin"
        / executable_name,
        package_root / "vendor" / target / "bin" / executable_name,
    )
    for candidate in candidates:
        if candidate.is_file():
            native = candidate.resolve()
            if not os.access(native, os.X_OK):
                raise RunContractError(
                    f"resolved Codex native executable is not executable: {native}"
                )
            return "openai-node-wrapper", native
    raise RunContractError(
        "Codex Node launcher native executable is missing from its frozen package layout"
    )


def _verify_disabled_codex_features(executable: Path) -> Mapping[str, bool]:
    argv = [str(executable), "features", "list"]
    for feature in DISABLED_FEATURES:
        argv.extend(("--disable", feature))
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15.0,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RunContractError(f"cannot verify Codex disabled features: {error}") from error
    if completed.returncode != 0:
        raise RunContractError("Codex native feature preflight failed")
    states: dict[str, bool] = {}
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[-1] in {"true", "false"}:
            states[fields[0]] = fields[-1] == "true"
    missing = [feature for feature in DISABLED_FEATURES if feature not in states]
    # Codex 0.152 reports unified_exec as an always-on internal backend even
    # with an explicit --disable.  It is not exposed when shell_tool is false;
    # record the observed state instead of falsely claiming it was disabled.
    non_exposed_backend_exceptions = {"unified_exec"}
    enabled = [
        feature
        for feature in DISABLED_FEATURES
        if states.get(feature) is True
        and feature not in non_exposed_backend_exceptions
    ]
    if missing or enabled:
        raise RunContractError(
            "Codex disabled-feature preflight did not close: "
            f"missing={missing}, enabled={enabled}"
        )
    return MappingProxyType(
        {feature: states[feature] for feature in DISABLED_FEATURES}
    )


def _binary_identity(executable: str | Path) -> _BinaryIdentity:
    requested = str(executable)
    if not requested:
        raise RunContractError("Codex executable cannot be empty")
    has_separator = os.sep in requested or (os.altsep is not None and os.altsep in requested)
    candidate = Path(requested).expanduser() if has_separator else None
    located = str(candidate) if candidate is not None else shutil.which(requested)
    if located is None:
        raise RunContractError(f"Codex executable is not available: {requested!r}")
    launcher = Path(located).resolve()
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise RunContractError(f"Codex executable is not an executable file: {launcher}")
    launcher_bytes = _read_bytes(launcher, location="Codex launcher")
    launcher_kind, resolved = _resolve_codex_execution_path(
        launcher,
        launcher_bytes,
    )
    binary_bytes = (
        launcher_bytes
        if resolved == launcher
        else _read_bytes(resolved, location="Codex native executable")
    )
    try:
        completed = subprocess.run(
            [str(resolved), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10.0,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RunContractError(f"cannot identify Codex executable: {error}") from error
    version = completed.stdout.strip()
    if completed.returncode != 0 or not version:
        raise RunContractError(
            "Codex executable --version did not return a successful version"
        )
    disabled_feature_states = _verify_disabled_codex_features(resolved)
    return _BinaryIdentity(
        requested=requested,
        launcher_kind=launcher_kind,
        launcher_path=launcher,
        launcher_sha256=_sha256(launcher_bytes),
        resolved_path=resolved,
        sha256=_sha256(binary_bytes),
        version=version,
        disabled_feature_states=disabled_feature_states,
        executable_bytes=binary_bytes,
    )


def _codex_binary_contract(binary: _BinaryIdentity) -> dict[str, object]:
    return {
        "requested": binary.requested,
        "launcher_kind": binary.launcher_kind,
        "launcher_path": str(binary.launcher_path),
        "launcher_sha256": binary.launcher_sha256,
        "resolved_path": str(binary.resolved_path),
        "sha256": binary.sha256,
        "version": binary.version,
        "disabled_feature_states": dict(binary.disabled_feature_states),
        "frozen_path": FROZEN_CODEX_EXECUTABLE_NAME,
        "file_mode": "0555",
    }


def _recorded_binary_identity(value: Mapping[str, Any]) -> _BinaryIdentity:
    expected_fields = {
        "requested",
        "launcher_kind",
        "launcher_path",
        "launcher_sha256",
        "resolved_path",
        "sha256",
        "version",
        "disabled_feature_states",
        "frozen_path",
        "file_mode",
    }
    if set(value) != expected_fields:
        raise RunContractError("recorded Codex binary identity fields differ")
    requested = value.get("requested")
    launcher_kind = value.get("launcher_kind")
    launcher_path = value.get("launcher_path")
    launcher_sha256 = value.get("launcher_sha256")
    resolved_path = value.get("resolved_path")
    binary_sha256 = value.get("sha256")
    version = value.get("version")
    disabled_feature_states = value.get("disabled_feature_states")
    if (
        not isinstance(requested, str)
        or not requested
        or launcher_kind not in {"direct", "openai-node-wrapper"}
        or not isinstance(launcher_path, str)
        or not Path(launcher_path).is_absolute()
        or not isinstance(launcher_sha256, str)
        or _SHA256.fullmatch(launcher_sha256) is None
        or not isinstance(resolved_path, str)
        or not Path(resolved_path).is_absolute()
        or not isinstance(binary_sha256, str)
        or _SHA256.fullmatch(binary_sha256) is None
        or not isinstance(version, str)
        or not version
        or not isinstance(disabled_feature_states, Mapping)
        or set(disabled_feature_states) != set(DISABLED_FEATURES)
        or any(not isinstance(state, bool) for state in disabled_feature_states.values())
        or any(
            disabled_feature_states[feature]
            for feature in DISABLED_FEATURES
            if feature != "unified_exec"
        )
        or value.get("frozen_path") != FROZEN_CODEX_EXECUTABLE_NAME
        or value.get("file_mode") != "0555"
    ):
        raise RunContractError("recorded Codex binary identity is malformed")
    return _BinaryIdentity(
        requested=requested,
        launcher_kind=str(launcher_kind),
        launcher_path=Path(launcher_path),
        launcher_sha256=launcher_sha256,
        resolved_path=Path(resolved_path),
        sha256=binary_sha256,
        version=version,
        disabled_feature_states=MappingProxyType(dict(disabled_feature_states)),
        executable_bytes=b"",
    )


def _launcher_runtime_contract() -> dict[str, str]:
    resolved = Path(sys.executable).resolve()
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise RunContractError(f"Python launcher runtime is unsafe: {resolved}")
    return {
        "kind": "python",
        "resolved_path": str(resolved),
        "sha256": _sha256(_read_bytes(resolved, location="Python launcher runtime")),
        "version": platform.python_version(),
    }


def _recorded_launcher_runtime(value: Mapping[str, Any]) -> dict[str, str]:
    if set(value) != {"kind", "resolved_path", "sha256", "version"}:
        raise RunContractError("recorded launcher runtime fields differ")
    resolved_path = value.get("resolved_path")
    sha256 = value.get("sha256")
    version = value.get("version")
    if (
        value.get("kind") != "python"
        or not isinstance(resolved_path, str)
        or not Path(resolved_path).is_absolute()
        or not isinstance(sha256, str)
        or _SHA256.fullmatch(sha256) is None
        or not isinstance(version, str)
        or not version
    ):
        raise RunContractError("recorded launcher runtime identity is malformed")
    return {
        "kind": "python",
        "resolved_path": resolved_path,
        "sha256": sha256,
        "version": version,
    }


def _relative_display(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _module_sha256(module_file: str | None, *, name: str) -> str:
    if module_file is None:
        raise RunContractError(f"cannot locate {name} implementation module")
    return _sha256(_read_bytes(Path(module_file), location=f"{name} implementation"))


def _selected_id_sha256(cases: tuple[PromptCase, ...]) -> str:
    return _sha256("".join(f"{case.sample_id}\n" for case in cases).encode("ascii"))


def _make_contract(
    inputs: _RunInputs,
    *,
    selected_cases: tuple[PromptCase, ...],
    expected_case_count: int,
    selection_limit: int | None,
    binary: _BinaryIdentity,
    max_attempts: int,
    concurrency: int,
    timeout_seconds: float,
    circuit_breaker_threshold: int,
    transport: str,
    launcher_runtime: Mapping[str, str] | None = None,
) -> dict[str, object]:
    import cofactor_bench.cases as cases_module
    import cofactor_bench.cli as cli_module
    import cofactor_bench.metrics as metrics_module
    import cofactor_bench.prediction as prediction_module
    import cofactor_bench.prompt as prompt_module
    import cofactor_bench.reporting as reporting_module
    import cofactor_bench.run as run_module
    import cofactor_bench.runner as runner_module
    import cofactor_bench.scoring as scoring_module

    model = _required_mapping(inputs.config, "model", location="benchmark config")
    model_settings = _runner_model_settings(inputs.config)
    case_manifest = _decode_json_object(
        inputs.cases_manifest_bytes,
        location="case artifact manifest",
    )
    view_audit = _decode_json_object(
        inputs.artifact_bytes["view_audit"],
        location="view audit",
    )
    evaluation_modules = {
        "reporting": reporting_module,
        "scoring": scoring_module,
        "metrics": metrics_module,
        "prediction": prediction_module,
    }
    return {
        "config": {
            "path": _relative_display(inputs.config_path, inputs.project_root),
            "sha256": _sha256(inputs.config_bytes),
        },
        "cases": {
            "path": _relative_display(inputs.cases_path, inputs.project_root),
            "sha256": _sha256(inputs.cases_bytes),
            "manifest_path": _relative_display(
                inputs.cases_manifest_path, inputs.project_root
            ),
            "manifest_sha256": _sha256(inputs.cases_manifest_bytes),
            "manifest_schema_version": case_manifest.get("schema_version"),
            "catalog_version": case_manifest.get("catalog_version"),
            "total_count": len(inputs.cases),
            "expected_formal_count": expected_case_count,
            "selection_limit": selection_limit,
            "selected_count": len(selected_cases),
            "selected_sample_ids_sha256": _selected_id_sha256(selected_cases),
        },
        "model": {
            "name": model["name"],
            "reasoning_effort": model["reasoning_effort"],
            "service_tier": model["service_tier"],
            "prompt_version": model["prompt_version"],
            "response_schema_version": model["response_schema_version"],
        },
        "evaluation_versions": {
            "dataset_version": inputs.config["dataset_version"],
            "view_rule_version": case_manifest.get("view_rule_version"),
            "formula_rule_version": FORMULA_RULE_VERSION,
            "prompt_version": PROMPT_VERSION,
            "catalog_version": case_manifest.get("catalog_version"),
            "response_schema_version": RESPONSE_SCHEMA_VERSION,
        },
        "evaluation_artifacts": {
            name: dict(inputs.artifact_metadata[name])
            for name in _EVALUATION_ARTIFACT_KEYS
        },
        "evaluation_implementations": {
            name: {
                "sha256": _module_sha256(
                    getattr(module, "__file__", None),
                    name=f"{name} evaluation",
                )
            }
            for name, module in evaluation_modules.items()
        },
        "prompt_implementation": {
            "sha256": _module_sha256(
                getattr(prompt_module, "__file__", None), name="prompt"
            ),
        },
        "prediction_implementation": {
            "sha256": _module_sha256(
                getattr(prediction_module, "__file__", None), name="prediction"
            ),
        },
        "case_implementation": {
            "sha256": _module_sha256(
                getattr(cases_module, "__file__", None), name="case"
            ),
        },
        "orchestration_implementation": {
            "sha256": _module_sha256(
                getattr(run_module, "__file__", None), name="run orchestration"
            ),
        },
        "cli_implementation": {
            "sha256": _module_sha256(
                getattr(cli_module, "__file__", None), name="CLI orchestration"
            ),
        },
        "response_schema": {
            "source_path": _relative_display(
                inputs.response_schema_path, inputs.project_root
            ),
            "frozen_path": FROZEN_RESPONSE_SCHEMA_NAME,
            "file_mode": "0444",
            "sha256": _sha256(inputs.response_schema_bytes),
        },
        "codex_binary": _codex_binary_contract(binary),
        "launcher_runtime": dict(
            _launcher_runtime_contract()
            if launcher_runtime is None
            else launcher_runtime
        ),
        "transport": {
            "kind": transport,
            "adapter": "cofactor_bench.runner.CodexExecRunner",
            "runner_implementation_sha256": _module_sha256(
                getattr(runner_module, "__file__", None), name="runner"
            ),
            "disabled_features": list(DISABLED_FEATURES),
            "disabled_feature_preflight": {
                "command": "native features list with exact --disable overrides",
                "observed_states": dict(binary.disabled_feature_states),
                "all_user_tool_features_false": True,
                "non_exposed_backend_exceptions": [
                    feature
                    for feature in ("unified_exec",)
                    if binary.disabled_feature_states.get(feature) is True
                ],
            },
            "max_attempts": max_attempts,
            "timeout_seconds": timeout_seconds,
            "circuit_breaker_threshold": circuit_breaker_threshold,
            "systematic_failure_codes": sorted(SYSTEMATIC_FAILURE_CODES),
            "model_settings": {
                "model": model_settings.model,
                "reasoning_effort": model_settings.reasoning_effort,
                "service_tier": model_settings.service_tier,
                "output_schema": FROZEN_RESPONSE_SCHEMA_NAME,
                "tools_disabled": list(DISABLED_FEATURES),
            },
            **(
                {
                    "provider": "deepseek-official",
                    "endpoint": DEEPSEEK_API_ENDPOINT,
                    "max_output_tokens": DEEPSEEK_MAX_OUTPUT_TOKENS,
                    "thinking": {"type": DEEPSEEK_THINKING_TYPE},
                    "response_format": {"type": "json_object"},
                    "credential_environment_name": "DEEPSEEK_API_KEY",
                    "internal_http_retries": 0,
                }
                if transport == "deepseek_official_api"
                else {}
            ),
        },
        "execution": {
            "concurrency": concurrency,
            "order": "public-cases-jsonl-order",
            "append_only": True,
            "probe": {
                "case_count": 1,
                "required_before_ramp": True,
                "systematic_failure_abort_threshold": 1,
            },
            "ramp": {
                "maximum_in_flight": concurrency,
                "starts_only_after_probe_success": True,
            },
        },
    }


def _make_manifest(
    *,
    run_id: str,
    mode: str,
    dataset_version: str,
    created_at: str,
    contract: Mapping[str, object],
) -> dict[str, object]:
    contract_value = dict(contract)
    return {
        "schema_version": RUN_MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "dataset_version": dataset_version,
        "mode": mode,
        "created_at": created_at,
        "contract_sha256": _sha256(_canonical_bytes(contract_value)),
        "contract": contract_value,
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _exclusive_process_lock(path: Path, *, busy_message: str):
    """Hold a nonblocking cross-process advisory lock on one safe file."""

    if path.parent.is_symlink():
        raise RunContractError(f"lock directory is unsafe: {path.parent}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise RunContractError(f"cannot open run lock {path}: {error}") from error
    locked = False
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RunContractError(f"run lock is not a regular file: {path}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RunContractError(busy_message) from error
        locked = True
        _fsync_directory(path.parent)
        yield
    finally:
        try:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@contextmanager
def _run_lifecycle_lock(inputs: _RunInputs, run_id: str):
    lock_path = inputs.project_root / ".run-locks" / f"{run_id}.lock"
    if not lock_path.parent.resolve().is_relative_to(inputs.project_root):
        raise RunContractError("run lock escapes the project root")
    with _exclusive_process_lock(
        lock_path,
        busy_message=f"run {run_id!r} has an active invocation",
    ):
        yield


@contextmanager
def _run_owned_lock(run_dir: Path, run_id: str):
    staging = run_dir / ".staging"
    if staging.is_symlink():
        raise RunContractError("run staging directory is unsafe")
    staging.mkdir(mode=0o700, exist_ok=True)
    _fsync_directory(run_dir)
    with _exclusive_process_lock(
        staging / ".run-invocation.lock",
        busy_message=f"run {run_id!r} has an active invocation",
    ):
        yield


def _publish_bytes_exclusive(path: Path, value: bytes) -> None:
    """Durably publish a final invocation record via run-owned staging."""

    invocation_dir = path.parent
    if invocation_dir.parent.name != "invocations":
        raise RunContractError("immutable invocation record path is invalid")
    run_dir = invocation_dir.parent.parent
    staging = run_dir / ".staging"
    if staging.is_symlink():
        raise RunContractError("run staging directory is unsafe")
    staging.mkdir(mode=0o700, exist_ok=True)
    temporary = staging / (
        f"publish-{invocation_dir.name}-{path.name}-{os.getpid()}-"
        f"{os.urandom(8).hex()}.tmp"
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(staging)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as error:
            raise RunContractError(f"immutable ledger already exists: {path}") from error
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        _fsync_directory(staging)


def _write_bytes_exclusive_direct(path: Path, value: bytes) -> None:
    """Write inside an unpublished private directory, then fsync it."""

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    _fsync_directory(path.parent)


def _framed_hash(domain: str, entries: Mapping[str, bytes]) -> str:
    """Hash keyed byte records with unambiguous length framing."""

    digest = hashlib.sha256()
    digest.update(domain.encode("ascii"))
    digest.update(b"\0")
    for key in sorted(entries):
        payload = entries[key]
        digest.update(key.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _redact_invocation_argv(argv: Sequence[str]) -> list[str]:
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        raise RunContractError("invocation argv must contain nonempty strings")
    secrets = tuple(
        value
        for name, value in os.environ.items()
        if value and any(marker in name.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
    )
    result: list[str] = []
    for item in argv:
        redacted = item
        for secret in secrets:
            if secret in redacted:
                redacted = redacted.replace(secret, "<redacted>")
        result.append(redacted)
    return result


def _invocation_common(
    *,
    number: int,
    run_id: str,
    manifest_sha256: str,
    config_sha256: str,
    argv: Sequence[str],
    resume: bool,
    limit: int | None,
    infrastructure_gate: bool,
    contract: Mapping[str, Any],
) -> dict[str, object]:
    transport = _required_mapping(contract, "transport", location="run manifest.contract")
    execution = _required_mapping(contract, "execution", location="run manifest.contract")
    model = _contract_model_settings(contract)
    return {
        "invocation_number": number,
        "run_id": run_id,
        "run_manifest_sha256": manifest_sha256,
        "config_sha256": config_sha256,
        "argv": _redact_invocation_argv(argv),
        "resume": resume,
        "limit": limit,
        "infrastructure_gate": infrastructure_gate,
        "scheduling": {
            "model": model.model,
            "reasoning_effort": model.reasoning_effort,
            "service_tier": model.service_tier,
            "concurrency": execution.get("concurrency"),
            "max_attempts": transport.get("max_attempts"),
            "timeout_seconds": transport.get("timeout_seconds"),
            "circuit_breaker_threshold": transport.get(
                "circuit_breaker_threshold"
            ),
            "probe": execution.get("probe"),
            "ramp": execution.get("ramp"),
        },
    }


def _invocation_directories(run_dir: Path) -> list[tuple[int, Path]]:
    root = run_dir / "invocations"
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        raise RunContractError("invocation ledger root is unsafe")
    numbered: list[tuple[int, Path]] = []
    for entry in root.iterdir():
        suffix = entry.name.removeprefix("invocation-")
        if (
            entry.is_symlink()
            or not entry.is_dir()
            or not entry.name.startswith("invocation-")
            or not suffix.isdigit()
        ):
            raise RunContractError(f"unexpected invocation ledger entry {entry.name!r}")
        number = int(suffix)
        if number < 1 or entry.name != f"invocation-{number:04d}":
            raise RunContractError(f"invalid invocation ledger entry {entry.name!r}")
        numbered.append((number, entry))
    numbered.sort()
    if [number for number, _ in numbered] != list(range(1, len(numbered) + 1)):
        raise RunContractError("invocation ledger is not contiguous")
    return numbered


def _create_invocation(
    *,
    run_dir: Path,
    run_id: str,
    manifest_bytes: bytes,
    inputs: _RunInputs,
    argv: Sequence[str],
    resume: bool,
    limit: int | None,
    infrastructure_gate: bool,
    contract: Mapping[str, Any],
) -> tuple[Path, Mapping[str, object]]:
    invocation_root = run_dir / "invocations"
    if invocation_root.is_symlink():
        raise RunContractError("invocation ledger root is unsafe")
    invocation_root.mkdir(mode=0o700, exist_ok=True)
    _fsync_directory(run_dir)
    staging = run_dir / ".staging"
    if staging.is_symlink():
        raise RunContractError("run staging directory is unsafe")
    staging.mkdir(mode=0o700, exist_ok=True)
    with _exclusive_process_lock(
        staging / ".invocation-publish.lock",
        busy_message=f"run {run_id!r} invocation publication is active",
    ):
        existing = _invocation_directories(run_dir)
        number = len(existing) + 1
        directory = invocation_root / f"invocation-{number:04d}"
        if directory.exists() or directory.is_symlink():
            raise RunContractError("invocation number appeared concurrently")
        common = _invocation_common(
            number=number,
            run_id=run_id,
            manifest_sha256=_sha256(manifest_bytes),
            config_sha256=_sha256(inputs.config_bytes),
            argv=argv,
            resume=resume,
            limit=limit,
            infrastructure_gate=infrastructure_gate,
            contract=contract,
        )
        start = {
            "schema_version": INVOCATION_SCHEMA_VERSION,
            **common,
            "started_at": _utc_now(),
            "status": "started",
        }
        temporary = Path(
            tempfile.mkdtemp(
                prefix=".invocation-",
                suffix=".tmp",
                dir=staging,
            )
        )
        _fsync_directory(staging)
        published = False
        try:
            _write_bytes_exclusive_direct(
                temporary / "start.json",
                _canonical_bytes(start),
            )
            _fsync_directory(temporary)
            if directory.exists() or directory.is_symlink():
                raise RunContractError("invocation number appeared concurrently")
            os.rename(temporary, directory)
            published = True
            _fsync_directory(invocation_root)
            _fsync_directory(staging)
        finally:
            if not published and temporary.exists() and not temporary.is_symlink():
                for entry in tuple(temporary.iterdir()):
                    if entry.is_symlink() or not entry.is_file():
                        break
                    entry.unlink()
                try:
                    temporary.rmdir()
                except OSError:
                    pass
        return directory, common


def _close_invocation(
    directory: Path,
    common: Mapping[str, object],
    *,
    status: str,
    error_type: str | None,
) -> None:
    if status not in {"success", "exception", "host_interrupted"}:
        raise RunContractError("invocation end status is invalid")
    end = {
        "schema_version": INVOCATION_END_SCHEMA_VERSION,
        **dict(common),
        "completed_at": _utc_now(),
        "status": status,
        "error_type": error_type,
    }
    _publish_bytes_exclusive(directory / "end.json", _canonical_bytes(end))


def _validate_created_at(value: object) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RunContractError("run manifest created_at is invalid")
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise RunContractError("run manifest created_at is invalid") from error
    return value


def _validate_timestamp(value: object, *, location: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RunContractError(f"{location} is not a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise RunContractError(f"{location} is not a UTC timestamp") from error
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise RunContractError(f"{location} is not UTC")
    return value


def _recover_staged_invocation_publication(run_dir: Path) -> None:
    """Publish the sole complete staged start left by a killed host."""

    staging = run_dir / ".staging"
    if not staging.exists():
        return
    candidates = sorted(
        entry
        for entry in staging.iterdir()
        if entry.name.startswith(".invocation-") and entry.name.endswith(".tmp")
    )
    if not candidates:
        return
    if len(candidates) != 1:
        raise RunContractError("multiple staged invocation publications are ambiguous")
    temporary = candidates[0]
    if temporary.is_symlink() or not temporary.is_dir():
        raise RunContractError("staged invocation publication is unsafe")
    observed = tuple(temporary.iterdir())
    if not observed:
        temporary.rmdir()
        _fsync_directory(staging)
        return
    if any(entry.is_symlink() or not entry.is_file() for entry in observed):
        raise RunContractError("staged invocation publication is unsafe")
    start_path = temporary / "start.json"
    hidden = tuple(
        entry
        for entry in observed
        if re.fullmatch(r"\.start\.json\.\d+\.[0-9a-f]+\.tmp", entry.name)
    )
    unexpected = {
        entry.name
        for entry in observed
        if entry.name != "start.json" and entry not in hidden
    }
    if unexpected or len(hidden) > 1:
        raise RunContractError("staged invocation publication contains unknown files")
    if not start_path.exists() and hidden:
        try:
            candidate = _decode_json_object(
                _read_bytes(hidden[0], location="staged invocation temporary start"),
                location="staged invocation temporary start",
            )
        except RunContractError:
            hidden[0].unlink()
            temporary.rmdir()
            _fsync_directory(staging)
            return
        if frozenset(candidate) != _INVOCATION_START_FIELDS:
            hidden[0].unlink()
            temporary.rmdir()
            _fsync_directory(staging)
            return
        os.rename(hidden[0], start_path)
        _fsync_directory(temporary)
    elif start_path.exists() and hidden:
        if start_path.read_bytes() != hidden[0].read_bytes():
            raise RunContractError("staged invocation temporary start bytes differ")
        hidden[0].unlink()
        _fsync_directory(temporary)
    if not start_path.is_file():
        raise RunContractError("staged invocation publication lacks a complete start")
    try:
        start = _decode_json_object(
            _read_bytes(start_path, location="staged invocation start"),
            location="staged invocation start",
        )
    except RunContractError:
        start_path.unlink()
        temporary.rmdir()
        _fsync_directory(staging)
        return
    if frozenset(start) != _INVOCATION_START_FIELDS:
        start_path.unlink()
        temporary.rmdir()
        _fsync_directory(staging)
        return
    existing = _invocation_directories(run_dir)
    expected_number = len(existing) + 1
    if start.get("invocation_number") != expected_number:
        raise RunContractError("staged invocation number is not the next contiguous number")
    root = run_dir / "invocations"
    root.mkdir(mode=0o700, exist_ok=True)
    destination = root / f"invocation-{expected_number:04d}"
    if destination.exists() or destination.is_symlink():
        raise RunContractError("staged invocation destination already exists")
    with _exclusive_process_lock(
        staging / ".invocation-publish.lock",
        busy_message="invocation publication is active",
    ):
        if destination.exists() or destination.is_symlink():
            raise RunContractError("staged invocation destination already exists")
        os.rename(temporary, destination)
        _fsync_directory(root)
        _fsync_directory(staging)


def _is_complete_staged_invocation_record(
    destination: Path,
    payload: bytes,
) -> bool:
    try:
        record = _decode_json_object(payload, location="staged invocation record")
        if payload != _canonical_bytes(record):
            return False
        number = int(destination.parent.name.removeprefix("invocation-"))
        if record.get("invocation_number") != number:
            return False
        if destination.name == "start.json":
            _validate_timestamp(record.get("started_at"), location="staged start time")
            return (
                frozenset(record) == _INVOCATION_START_FIELDS
                and record.get("schema_version") == INVOCATION_SCHEMA_VERSION
                and record.get("status")
                in {"started", "recovered_empty_allocation"}
            )
        if destination.name != "end.json":
            return False
        _validate_timestamp(record.get("completed_at"), location="staged end time")
        if (
            frozenset(record) != _INVOCATION_END_FIELDS
            or record.get("schema_version") != INVOCATION_END_SCHEMA_VERSION
            or record.get("status")
            not in {"success", "exception", "host_interrupted"}
        ):
            return False
        error_type = record.get("error_type")
        if (record.get("status") == "success" and error_type is not None) or (
            record.get("status") != "success"
            and (not isinstance(error_type, str) or not error_type)
        ):
            return False
        start_path = destination.parent / "start.json"
        if start_path.is_symlink() or not start_path.is_file():
            return False
        start = _decode_json_object(
            _read_bytes(start_path, location="invocation start"),
            location="invocation start",
        )
        return (
            frozenset(start) == _INVOCATION_START_FIELDS
            and all(start.get(key) == record.get(key) for key in _INVOCATION_COMMON_FIELDS)
        )
    except (RunContractError, TypeError, ValueError):
        return False


def _recover_staged_record_publications(run_dir: Path) -> None:
    staging = run_dir / ".staging"
    if not staging.exists():
        return
    pattern = re.compile(
        r"publish-(invocation-\d{4})-(start|end)\.json-\d+-[0-9a-f]+\.tmp\Z"
    )
    grouped: dict[Path, list[Path]] = {}
    for entry in staging.iterdir():
        if not entry.name.startswith("publish-invocation-"):
            continue
        match = pattern.fullmatch(entry.name)
        if match is None or entry.is_symlink() or not entry.is_file():
            raise RunContractError(f"unknown staged record publication {entry.name!r}")
        destination = (
            run_dir
            / "invocations"
            / match.group(1)
            / f"{match.group(2)}.json"
        )
        grouped.setdefault(destination, []).append(entry)
    for destination, temporaries in grouped.items():
        if len(temporaries) != 1:
            raise RunContractError("duplicate staged record publications are ambiguous")
        temporary = temporaries[0]
        if destination.parent.is_symlink() or not destination.parent.is_dir():
            raise RunContractError("staged record destination is unsafe")
        payload = _read_bytes(temporary, location="staged invocation record")
        if not _is_complete_staged_invocation_record(destination, payload):
            temporary.unlink()
            _fsync_directory(staging)
            continue
        if destination.exists():
            if destination.is_symlink() or not destination.is_file():
                raise RunContractError("staged record destination is unsafe")
            if destination.read_bytes() != payload:
                raise RunContractError("staged and published invocation records differ")
        else:
            try:
                os.link(temporary, destination, follow_symlinks=False)
            except FileExistsError as error:
                raise RunContractError("invocation record appeared concurrently") from error
            _fsync_directory(destination.parent)
        temporary.unlink()
        _fsync_directory(staging)


def _model_ledger_changed_after(run_dir: Path, timestamp_ns: int) -> bool:
    for name in ("cases", "transport-incidents", ".inflight", ".active"):
        root = run_dir / name
        if not root.exists():
            continue
        candidates = (root, *root.rglob("*"))
        for path in candidates:
            if path.is_symlink():
                raise RunContractError("model ledger contains a symlink")
            if path.stat().st_mtime_ns > timestamp_ns:
                return True
    return False


def _audit_trailing_empty_invocation(
    *,
    directory: Path,
    number: int,
    run_dir: Path,
    run_id: str,
    manifest_bytes: bytes,
    config_bytes: bytes,
    manifest_contract: Mapping[str, Any],
) -> None:
    allocation = directory.stat().st_mtime_ns
    if _model_ledger_changed_after(run_dir, allocation):
        raise RunContractError(
            "model ledger changed after empty invocation allocation; recovery refused"
        )
    cases_contract = _required_mapping(
        manifest_contract,
        "cases",
        location="run manifest.contract",
    )
    limit = cases_contract.get("selection_limit")
    common = _invocation_common(
        number=number,
        run_id=run_id,
        manifest_sha256=_sha256(manifest_bytes),
        config_sha256=_sha256(config_bytes),
        argv=("<unavailable-before-atomic-start-publication>",),
        resume=True,
        limit=limit if isinstance(limit, int) and not isinstance(limit, bool) else None,
        infrastructure_gate=limit is not None,
        contract=manifest_contract,
    )
    start = {
        "schema_version": INVOCATION_SCHEMA_VERSION,
        **common,
        "started_at": datetime.fromtimestamp(
            allocation / 1_000_000_000,
            tz=timezone.utc,
        ).isoformat().replace("+00:00", "Z"),
        "status": "recovered_empty_allocation",
    }
    _publish_bytes_exclusive(directory / "start.json", _canonical_bytes(start))
    _close_invocation(
        directory,
        common,
        status="host_interrupted",
        error_type="HostInterruptionBeforeStartPublication",
    )


def _validate_invocations(
    *,
    run_dir: Path,
    run_id: str,
    manifest_bytes: bytes,
    config_bytes: bytes,
    manifest_contract: Mapping[str, Any],
    require_closed: bool,
    seal_trailing_open: bool = False,
    recover_trailing_empty: bool = False,
) -> tuple[InvocationBundle, ...]:
    directories = _invocation_directories(run_dir)
    if directories and recover_trailing_empty:
        number, trailing = directories[-1]
        if not any(trailing.iterdir()):
            _audit_trailing_empty_invocation(
                directory=trailing,
                number=number,
                run_dir=run_dir,
                run_id=run_id,
                manifest_bytes=manifest_bytes,
                config_bytes=config_bytes,
                manifest_contract=manifest_contract,
            )
    bundles: list[InvocationBundle] = []
    expected_manifest_sha = _sha256(manifest_bytes)
    expected_config_sha = _sha256(config_bytes)
    for index, (number, directory) in enumerate(directories):
        observed: set[str] = set()
        for entry in directory.iterdir():
            if entry.is_symlink() or not entry.is_file():
                raise RunContractError("invocation ledger contains an unsafe artifact")
            observed.add(entry.name)
        is_trailing = index == len(directories) - 1
        if observed == {"start.json"} and is_trailing and seal_trailing_open:
            start_bytes = _read_bytes(directory / "start.json", location="invocation start")
            start = _decode_json_object(start_bytes, location="invocation start")
            if frozenset(start) != _INVOCATION_START_FIELDS:
                raise RunContractError("open invocation start fields differ from contract")
            common = {
                key: start[key]
                for key in _INVOCATION_COMMON_FIELDS
            }
            recovered_empty = start.get("status") == "recovered_empty_allocation"
            _close_invocation(
                directory,
                common,
                status="host_interrupted",
                error_type=(
                    "HostInterruptionBeforeStartPublication"
                    if recovered_empty
                    else "HostInterruption"
                ),
            )
            observed.add("end.json")
        if observed == {"start.json"} and not require_closed:
            continue
        if observed != {"start.json", "end.json"}:
            raise RunContractError(
                f"invocation {number} is not a closed two-record ledger"
            )
        start_bytes = _read_bytes(directory / "start.json", location="invocation start")
        end_bytes = _read_bytes(directory / "end.json", location="invocation end")
        start = _decode_json_object(start_bytes, location=f"invocation {number} start")
        end = _decode_json_object(end_bytes, location=f"invocation {number} end")
        if frozenset(start) != _INVOCATION_START_FIELDS:
            raise RunContractError(f"invocation {number} start fields differ")
        if frozenset(end) != _INVOCATION_END_FIELDS:
            raise RunContractError(f"invocation {number} end fields differ")
        if start.get("schema_version") != INVOCATION_SCHEMA_VERSION:
            raise RunContractError(f"invocation {number} start schema differs")
        if end.get("schema_version") != INVOCATION_END_SCHEMA_VERSION:
            raise RunContractError(f"invocation {number} end schema differs")
        for key in _INVOCATION_COMMON_FIELDS:
            if start.get(key) != end.get(key):
                raise RunContractError(
                    f"invocation {number} field {key!r} differs between start and end"
                )
        expected_identity = {
            "invocation_number": number,
            "run_id": run_id,
            "run_manifest_sha256": expected_manifest_sha,
            "config_sha256": expected_config_sha,
        }
        for key, expected in expected_identity.items():
            if start.get(key) != expected:
                raise RunContractError(
                    f"invocation {number} field {key!r} differs from run"
                )
        _validate_timestamp(
            start.get("started_at"),
            location=f"invocation {number} started_at",
        )
        _validate_timestamp(
            end.get("completed_at"),
            location=f"invocation {number} completed_at",
        )
        start_status = start.get("status")
        if start_status not in {"started", "recovered_empty_allocation"}:
            raise RunContractError(f"invocation {number} start status differs")
        if end.get("status") not in {"success", "exception", "host_interrupted"}:
            raise RunContractError(f"invocation {number} end status is invalid")
        error_type = end.get("error_type")
        if end.get("status") == "success":
            if error_type is not None:
                raise RunContractError(f"invocation {number} success has error_type")
        elif not isinstance(error_type, str) or not error_type:
            raise RunContractError(f"invocation {number} failure lacks error_type")
        if not isinstance(start.get("resume"), bool):
            raise RunContractError(f"invocation {number} resume is invalid")
        limit = start.get("limit")
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        ):
            raise RunContractError(f"invocation {number} limit is invalid")
        if not isinstance(start.get("infrastructure_gate"), bool):
            raise RunContractError(
                f"invocation {number} infrastructure_gate is invalid"
            )
        argv = start.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(item, str) or not item for item in argv)
        ):
            raise RunContractError(f"invocation {number} argv is invalid")
        transport = _required_mapping(
            manifest_contract,
            "transport",
            location="run manifest.contract",
        )
        execution = _required_mapping(
            manifest_contract,
            "execution",
            location="run manifest.contract",
        )
        model = _contract_model_settings(manifest_contract)
        expected_scheduling = {
            "model": model.model,
            "reasoning_effort": model.reasoning_effort,
            "service_tier": model.service_tier,
            "concurrency": execution.get("concurrency"),
            "max_attempts": transport.get("max_attempts"),
            "timeout_seconds": transport.get("timeout_seconds"),
            "circuit_breaker_threshold": transport.get(
                "circuit_breaker_threshold"
            ),
            "probe": execution.get("probe"),
            "ramp": execution.get("ramp"),
        }
        if start.get("scheduling") != expected_scheduling:
            raise RunContractError(f"invocation {number} scheduling differs from manifest")
        if start_status == "recovered_empty_allocation":
            if (
                argv != ["<unavailable-before-atomic-start-publication>"]
                or start.get("resume") is not True
                or end.get("status") != "host_interrupted"
                or end.get("error_type")
                != "HostInterruptionBeforeStartPublication"
            ):
                raise RunContractError(
                    f"invocation {number} recovered allocation fields conflict"
                )
            bundles.append(
                InvocationBundle(
                    number=number,
                    start_bytes=start_bytes,
                    end_bytes=end_bytes,
                )
            )
            continue
        if "cofactor_bench.cli" not in argv or "run" not in argv:
            raise RunContractError(f"invocation {number} command is not the run CLI")

        def option_value(flag: str) -> str | None:
            positions = [index for index, value in enumerate(argv) if value == flag]
            inline = [
                value.removeprefix(flag + "=")
                for value in argv
                if value.startswith(flag + "=")
            ]
            if len(positions) + len(inline) > 1:
                raise RunContractError(
                    f"invocation {number} command repeats {flag}"
                )
            if inline:
                if not inline[0]:
                    raise RunContractError(
                        f"invocation {number} command has no value for {flag}"
                    )
                return inline[0]
            if not positions:
                return None
            index = positions[0] + 1
            if index >= len(argv) or argv[index].startswith("--"):
                raise RunContractError(
                    f"invocation {number} command has no value for {flag}"
                )
            return argv[index]

        if option_value("--run-id") != run_id:
            raise RunContractError(f"invocation {number} command run_id differs")
        if ("--resume" in argv) != start["resume"]:
            raise RunContractError(f"invocation {number} command resume differs")
        if ("--infrastructure-gate" in argv) != start["infrastructure_gate"]:
            raise RunContractError(
                f"invocation {number} command infrastructure gate differs"
            )
        command_limit = option_value("--limit")
        expected_limit = start["limit"]
        if (
            (command_limit is None and expected_limit is not None)
            or (
                command_limit is not None
                and command_limit != str(expected_limit)
            )
        ):
            raise RunContractError(f"invocation {number} command limit differs")
        command_settings = {
            "--concurrency": expected_scheduling["concurrency"],
            "--timeout-seconds": expected_scheduling["timeout_seconds"],
            "--circuit-breaker-threshold": expected_scheduling[
                "circuit_breaker_threshold"
            ],
        }
        for flag, expected in command_settings.items():
            observed = option_value(flag)
            if observed is None:
                continue
            try:
                numeric = float(observed) if flag == "--timeout-seconds" else int(observed)
            except ValueError as error:
                raise RunContractError(
                    f"invocation {number} command {flag} is invalid"
                ) from error
            if float(numeric) != float(expected):
                raise RunContractError(
                    f"invocation {number} command {flag} differs"
                )
        secret_flags = (
            "api-key",
            "apikey",
            "token",
            "secret",
            "password",
            "cookie",
        )
        if any(
            value.startswith("--")
            and any(marker in value.casefold() for marker in secret_flags)
            for value in argv
        ):
            raise RunContractError(
                f"invocation {number} command contains a forbidden secret option"
            )
        bundles.append(
            InvocationBundle(
                number=number,
                start_bytes=start_bytes,
                end_bytes=end_bytes,
            )
        )
    return tuple(bundles)


def _write_manifest_new(
    run_dir: Path,
    manifest: Mapping[str, object],
    *,
    response_schema_bytes: bytes,
    codex_executable_bytes: bytes,
) -> Path:
    if run_dir.is_symlink():
        raise RunContractError("run directory may not be a symlink")
    try:
        run_dir.parent.mkdir(parents=True, exist_ok=True)
        run_dir.mkdir(mode=0o700, exist_ok=False)
    except FileExistsError as error:
        raise RunContractError(f"run directory already exists: {run_dir}") from error
    except OSError as error:
        raise RunContractError(f"cannot create run directory {run_dir}: {error}") from error
    path = run_dir / "manifest.json"
    try:
        with path.open("xb") as handle:
            payload = _canonical_bytes(manifest)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        frozen_schema = run_dir / FROZEN_RESPONSE_SCHEMA_NAME
        with frozen_schema.open("xb") as handle:
            handle.write(response_schema_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        frozen_schema.chmod(0o444)
        frozen_executable = run_dir / FROZEN_CODEX_EXECUTABLE_NAME
        with frozen_executable.open("xb") as handle:
            handle.write(codex_executable_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        frozen_executable.chmod(0o555)
        _fsync_directory(run_dir)
    except BaseException:
        # Preserve the newly allocated run directory as an explicit failed
        # allocation.  Reusing its ID would violate fail-closed semantics.
        raise
    return path


def _validate_frozen_response_schema(
    run_dir: Path,
    contract: Mapping[str, Any],
) -> Path:
    schema_contract = _required_mapping(
        contract, "response_schema", location="run manifest.contract"
    )
    if schema_contract.get("frozen_path") != FROZEN_RESPONSE_SCHEMA_NAME:
        raise RunContractError("run manifest frozen response schema path is invalid")
    expected_sha256 = schema_contract.get("sha256")
    if not isinstance(expected_sha256, str) or _SHA256.fullmatch(expected_sha256) is None:
        raise RunContractError("run manifest response schema SHA-256 is invalid")
    frozen_path = run_dir / FROZEN_RESPONSE_SCHEMA_NAME
    if frozen_path.is_symlink() or not frozen_path.is_file():
        raise RunContractError("frozen response schema is missing or unsafe")
    if stat.S_IMODE(frozen_path.stat().st_mode) != 0o444:
        raise RunContractError("frozen response schema file mode must be 0444")
    if _sha256_file_bytes(frozen_path, location="frozen response schema") != expected_sha256:
        raise RunContractError("frozen response schema SHA-256 mismatch")
    return frozen_path


def _validate_frozen_codex_executable(
    run_dir: Path,
    contract: Mapping[str, Any],
) -> Path:
    binary_contract = _required_mapping(
        contract, "codex_binary", location="run manifest.contract"
    )
    if binary_contract.get("frozen_path") != FROZEN_CODEX_EXECUTABLE_NAME:
        raise RunContractError("run manifest frozen Codex path is invalid")
    if binary_contract.get("file_mode") != "0555":
        raise RunContractError("run manifest frozen Codex mode is invalid")
    expected_sha256 = binary_contract.get("sha256")
    if not isinstance(expected_sha256, str) or _SHA256.fullmatch(expected_sha256) is None:
        raise RunContractError("run manifest Codex SHA-256 is invalid")
    frozen_path = run_dir / FROZEN_CODEX_EXECUTABLE_NAME
    if frozen_path.is_symlink() or not frozen_path.is_file():
        raise RunContractError("frozen Codex executable is missing or unsafe")
    if stat.S_IMODE(frozen_path.stat().st_mode) != 0o555:
        raise RunContractError("frozen Codex executable file mode must be 0555")
    if not os.access(frozen_path, os.X_OK):
        raise RunContractError("frozen Codex executable is not executable")
    if _sha256_file_bytes(
        frozen_path,
        location="frozen Codex executable",
    ) != expected_sha256:
        raise RunContractError("frozen Codex executable SHA-256 mismatch")
    return frozen_path


def _load_manifest(run_dir: Path) -> tuple[dict[str, Any], bytes]:
    if run_dir.is_symlink():
        raise RunContractError("run directory may not be a symlink")
    if not run_dir.is_dir():
        raise RunContractError(f"run directory does not exist: {run_dir}")
    path = run_dir / "manifest.json"
    if path.is_symlink():
        raise RunContractError("run manifest may not be a symlink")
    payload = _read_bytes(path, location="run manifest")
    manifest = _decode_json_object(payload, location="run manifest")
    if frozenset(manifest) != _RUN_MANIFEST_FIELDS:
        raise RunContractError("run manifest fields differ from contract")
    if payload != _canonical_bytes(manifest):
        raise RunContractError("run manifest bytes are not canonical")
    if manifest.get("schema_version") != RUN_MANIFEST_SCHEMA_VERSION:
        raise RunContractError("run manifest schema_version is unsupported")
    contract = _required_mapping(manifest, "contract", location="run manifest")
    if manifest.get("contract_sha256") != _sha256(_canonical_bytes(contract)):
        raise RunContractError("run manifest contract SHA-256 is invalid")
    _validate_created_at(manifest.get("created_at"))
    return manifest, payload


def _selection(
    inputs: _RunInputs,
    *,
    expected_case_count: int,
    limit: int | None,
    infrastructure_gate: bool,
) -> tuple[str, tuple[PromptCase, ...]]:
    if (
        isinstance(expected_case_count, bool)
        or not isinstance(expected_case_count, int)
        or expected_case_count < 1
    ):
        raise RunContractError("expected_case_count must be a positive integer")
    if len(inputs.cases) != expected_case_count:
        raise RunContractError(
            f"formal public cases must contain exactly {expected_case_count}; "
            f"observed {len(inputs.cases)}"
        )
    if limit is None and not infrastructure_gate:
        return "formal", inputs.cases
    if limit is None or not infrastructure_gate:
        raise RunContractError(
            "--limit is allowed only for an explicit infrastructure gate"
        )
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit < len(inputs.cases):
        raise RunContractError(
            "infrastructure gate limit must be positive and smaller than the formal set"
        )
    return "infrastructure_gate", inputs.cases[:limit]


def _validate_case_artifacts(
    validator: Callable[[str | Path], object],
    config_path: Path,
) -> None:
    try:
        validator(config_path)
    except RunContractError:
        raise
    except Exception as error:
        raise RunContractError(f"case artifact validation failed: {error}") from error


def execute_run_from_config(
    *,
    config_path: str | Path,
    run_id: str,
    resume: bool = False,
    concurrency: int | None = None,
    limit: int | None = None,
    infrastructure_gate: bool = False,
    executable: str | Path = "codex",
    timeout_seconds: float | None = None,
    circuit_breaker_threshold: int | None = None,
    expected_case_count: int = EXPECTED_CASE_COUNT,
    runner_factory: Callable[..., _Runner] | None = None,
    case_validator: Callable[[str | Path], object] = validate_cases_from_config,
    invocation_argv: Sequence[str] | None = None,
) -> RunValidationSummary:
    """Create or exactly resume a run, then execute its frozen case selection."""

    _validate_run_id(run_id)
    inputs = _load_inputs(config_path)
    with _run_lifecycle_lock(inputs, run_id):
        return _execute_run_from_inputs(
            inputs=inputs,
            run_id=run_id,
            resume=resume,
            concurrency=concurrency,
            limit=limit,
            infrastructure_gate=infrastructure_gate,
            executable=executable,
            timeout_seconds=timeout_seconds,
            circuit_breaker_threshold=circuit_breaker_threshold,
            expected_case_count=expected_case_count,
            runner_factory=runner_factory,
            case_validator=case_validator,
            invocation_argv=invocation_argv,
        )


def _execute_run_from_inputs(
    *,
    inputs: _RunInputs,
    run_id: str,
    resume: bool,
    concurrency: int | None,
    limit: int | None,
    infrastructure_gate: bool,
    executable: str | Path,
    timeout_seconds: float | None,
    circuit_breaker_threshold: int | None,
    expected_case_count: int,
    runner_factory: Callable[..., _Runner] | None,
    case_validator: Callable[[str | Path], object],
    invocation_argv: Sequence[str] | None,
) -> RunValidationSummary:
    _validate_fixed_model_contract(inputs.config)
    _validate_case_artifacts(case_validator, inputs.config_path)
    mode, selected_cases = _selection(
        inputs,
        expected_case_count=expected_case_count,
        limit=limit,
        infrastructure_gate=infrastructure_gate,
    )
    (
        max_attempts,
        effective_concurrency,
        effective_timeout,
        effective_breaker,
        transport,
    ) = _configured_run_settings(
        inputs.config,
        concurrency=concurrency,
        timeout_seconds=timeout_seconds,
        circuit_breaker_threshold=circuit_breaker_threshold,
    )
    run_dir = inputs.runs_root / run_id
    if not run_dir.parent.resolve().is_relative_to(inputs.project_root):
        raise RunContractError("run directory escapes the project root")

    manifest_bytes: bytes
    existing: Mapping[str, Any] | None = None
    launcher_runtime: Mapping[str, str] | None = None
    if resume:
        existing, manifest_bytes = _load_manifest(run_dir)
        recorded_contract = _required_mapping(
            existing,
            "contract",
            location="run manifest",
        )
        binary = _recorded_binary_identity(
            _required_mapping(
                recorded_contract,
                "codex_binary",
                location="run manifest.contract",
            )
        )
        launcher_runtime = _recorded_launcher_runtime(
            _required_mapping(
                recorded_contract,
                "launcher_runtime",
                location="run manifest.contract",
            )
        )
        if launcher_runtime != _launcher_runtime_contract():
            raise RunContractError(
                "current Python launcher runtime differs from run manifest"
            )
    else:
        binary = _binary_identity(executable)
    contract = _make_contract(
        inputs,
        selected_cases=selected_cases,
        expected_case_count=expected_case_count,
        selection_limit=limit,
        binary=binary,
        max_attempts=max_attempts,
        concurrency=effective_concurrency,
        timeout_seconds=effective_timeout,
        circuit_breaker_threshold=effective_breaker,
        transport=transport,
        launcher_runtime=launcher_runtime,
    )
    if resume:
        assert existing is not None
        created_at = _validate_created_at(existing.get("created_at"))
        expected_manifest = _make_manifest(
            run_id=run_id,
            mode=mode,
            dataset_version=_required_string(
                inputs.config, "dataset_version", location="benchmark config"
            ),
            created_at=created_at,
            contract=contract,
        )
        if existing != expected_manifest:
            raise RunContractError(
                "run manifest contract drift; resume refused before execution"
            )
    else:
        manifest = _make_manifest(
            run_id=run_id,
            mode=mode,
            dataset_version=_required_string(
                inputs.config, "dataset_version", location="benchmark config"
            ),
            created_at=_utc_now(),
            contract=contract,
        )
        _write_manifest_new(
            run_dir,
            manifest,
            response_schema_bytes=inputs.response_schema_bytes,
            codex_executable_bytes=binary.executable_bytes,
        )
        _, manifest_bytes = _load_manifest(run_dir)

    with _run_owned_lock(run_dir, run_id):
        return _execute_locked_invocation(
            inputs=inputs,
            run_dir=run_dir,
            run_id=run_id,
            manifest_bytes=manifest_bytes,
            contract=contract,
            mode=mode,
            selected_cases=selected_cases,
            resume=resume,
            limit=limit,
            infrastructure_gate=infrastructure_gate,
            executable=executable,
            max_attempts=max_attempts,
            effective_concurrency=effective_concurrency,
            effective_timeout=effective_timeout,
            effective_breaker=effective_breaker,
            runner_factory=runner_factory,
            invocation_argv=invocation_argv,
        )


def _execute_locked_invocation(
    *,
    inputs: _RunInputs,
    run_dir: Path,
    run_id: str,
    manifest_bytes: bytes,
    contract: Mapping[str, Any],
    mode: str,
    selected_cases: tuple[PromptCase, ...],
    resume: bool,
    limit: int | None,
    infrastructure_gate: bool,
    executable: str | Path,
    max_attempts: int,
    effective_concurrency: int,
    effective_timeout: float,
    effective_breaker: int,
    runner_factory: Callable[..., _Runner] | None,
    invocation_argv: Sequence[str] | None,
) -> RunValidationSummary:
    frozen_schema_path = _validate_frozen_response_schema(run_dir, contract)
    frozen_codex_path = _validate_frozen_codex_executable(run_dir, contract)

    if resume:
        _recover_staged_record_publications(run_dir)
        _recover_staged_invocation_publication(run_dir)
        _validate_invocations(
            run_dir=run_dir,
            run_id=run_id,
            manifest_bytes=manifest_bytes,
            config_bytes=inputs.config_bytes,
            manifest_contract=contract,
            require_closed=True,
            seal_trailing_open=True,
            recover_trailing_empty=True,
        )
        progress = _inspect_terminals(
            run_dir=run_dir,
            run_id=run_id,
            mode=mode,
            selected_cases=selected_cases,
            manifest_contract=contract,
            require_complete=False,
        )
        if progress.is_complete:
            if (
                mode == "infrastructure_gate"
                and progress.success_count != progress.selected_case_count
            ):
                raise RunContractError(
                    "infrastructure gate failed: every selected case must have a "
                    "successful terminal"
                )
            return _inspect_terminals(
                run_dir=run_dir,
                run_id=run_id,
                mode=mode,
                selected_cases=selected_cases,
                manifest_contract=contract,
                require_complete=True,
                config_bytes=inputs.config_bytes,
                require_operational_quiescence=True,
            )

    if invocation_argv is None:
        generated_argv = [
            "python3",
            "-m",
            "cofactor_bench.cli",
            "run",
            "--config",
            str(inputs.config_path),
            "--run-id",
            run_id,
        ]
        if resume:
            generated_argv.append("--resume")
        generated_argv.extend(("--concurrency", str(effective_concurrency)))
        generated_argv.extend(("--timeout-seconds", str(effective_timeout)))
        generated_argv.extend(
            ("--circuit-breaker-threshold", str(effective_breaker))
        )
        if limit is not None:
            generated_argv.extend(("--limit", str(limit)))
        if infrastructure_gate:
            generated_argv.append("--infrastructure-gate")
        generated_argv.extend(("--codex-executable", str(executable)))
        invocation_argv = tuple(generated_argv)
    invocation_directory, invocation_common = _create_invocation(
        run_dir=run_dir,
        run_id=run_id,
        manifest_bytes=manifest_bytes,
        inputs=inputs,
        argv=invocation_argv,
        resume=resume,
        limit=limit,
        infrastructure_gate=infrastructure_gate,
        contract=contract,
    )

    try:
        factory = CodexExecRunner if runner_factory is None else runner_factory
        model_settings = _contract_model_settings(contract)
        transport = _required_mapping(
            contract, "transport", location="run manifest.contract"
        )
        runner = factory(
            run_dir=run_dir,
            executable=frozen_codex_path,
            schema_path=frozen_schema_path,
            max_attempts=max_attempts,
            timeout_seconds=effective_timeout,
            circuit_breaker_threshold=effective_breaker,
            model_settings=model_settings,
            credential_environment_name=(
                "DEEPSEEK_API_KEY"
                if transport.get("kind") == "deepseek_official_api"
                else None
            ),
        )
        runner.run_cases(
            selected_cases,
            resume=resume,
            concurrency=effective_concurrency,
        )
        _validate_frozen_codex_executable(run_dir, contract)
        summary = _inspect_terminals(
            run_dir=run_dir,
            run_id=run_id,
            mode=mode,
            selected_cases=selected_cases,
            manifest_contract=contract,
            require_complete=True,
        )
        if (
            mode == "infrastructure_gate"
            and summary.success_count != summary.selected_case_count
        ):
            raise RunContractError(
                "infrastructure gate failed: every selected case must have a "
                "successful terminal"
            )
    except BaseException as error:
        _close_invocation(
            invocation_directory,
            invocation_common,
            status="exception",
            error_type=type(error).__name__,
        )
        raise
    _close_invocation(
        invocation_directory,
        invocation_common,
        status="success",
        error_type=None,
    )
    return _inspect_terminals(
        run_dir=run_dir,
        run_id=run_id,
        mode=mode,
        selected_cases=selected_cases,
        manifest_contract=contract,
        require_complete=True,
        config_bytes=inputs.config_bytes,
        require_operational_quiescence=True,
    )


def _strict_terminal_payload(path: Path) -> Mapping[str, Any]:
    payload = _decode_json_object(
        _read_bytes(path, location="terminal record"),
        location=f"terminal record {path}",
    )
    fields = frozenset(payload)
    if fields != _TERMINAL_FIELDS:
        raise RunContractError(
            f"terminal record fields differ from contract: "
            f"missing={sorted(_TERMINAL_FIELDS - fields)}, "
            f"extra={sorted(fields - _TERMINAL_FIELDS)}"
        )
    return payload


def _sha256_file_bytes(path: Path, *, location: str) -> str:
    return _sha256(_read_bytes(path, location=location))


def _validate_attempt_ledger(
    *,
    case_dir: Path,
    case: PromptCase,
    manifest_contract: Mapping[str, Any],
    terminal_payload: Mapping[str, Any] | None,
) -> int:
    """Validate immutable attempts sufficiently for offline reporting."""

    attempts_root = case_dir / "attempts"
    if not attempts_root.exists():
        if terminal_payload is not None:
            raise RunContractError(
                f"terminal {case.sample_id} has no complete attempt ledger"
            )
        return 0
    if attempts_root.is_symlink() or not attempts_root.is_dir():
        raise RunContractError(f"attempts path for {case.sample_id} is unsafe")

    numbered: list[tuple[int, Path]] = []
    for entry in attempts_root.iterdir():
        suffix = entry.name.removeprefix("attempt-")
        if (
            entry.is_symlink()
            or not entry.is_dir()
            or not entry.name.startswith("attempt-")
            or not suffix.isdigit()
        ):
            raise RunContractError(
                f"unexpected attempt ledger entry {entry.name!r} for {case.sample_id}"
            )
        number = int(suffix)
        if entry.name != f"attempt-{number:04d}" or number < 1:
            raise RunContractError(
                f"invalid attempt directory {entry.name!r} for {case.sample_id}"
            )
        numbered.append((number, entry))
    numbered.sort()
    if [number for number, _ in numbered] != list(range(1, len(numbered) + 1)):
        raise RunContractError(f"attempt ledger gap for {case.sample_id}")

    model = _required_mapping(
        manifest_contract, "model", location="run manifest.contract"
    )
    transport = _required_mapping(
        manifest_contract, "transport", location="run manifest.contract"
    )
    max_attempts = transport.get("max_attempts")
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or not 1 <= max_attempts <= MAX_ATTEMPTS
    ):
        raise RunContractError("run manifest max_attempts is invalid")
    if len(numbered) > max_attempts:
        raise RunContractError(
            f"attempt ledger {case.sample_id} exceeds max_attempts"
        )
    binary = _required_mapping(
        manifest_contract, "codex_binary", location="run manifest.contract"
    )
    run_dir = case_dir.parent.parent
    if binary.get("frozen_path") != FROZEN_CODEX_EXECUTABLE_NAME:
        raise RunContractError("run manifest frozen Codex path is invalid")
    executable = str(run_dir / FROZEN_CODEX_EXECUTABLE_NAME)
    schema_path = _validate_frozen_response_schema(run_dir, manifest_contract)
    expected_prompt_sha256 = _sha256(render_prompt(case).encode("utf-8"))
    attempt_error_codes: list[str | None] = []
    attempt_payloads: list[Mapping[str, Any]] = []
    for number, directory in numbered:
        observed_files: set[str] = set()
        for entry in directory.iterdir():
            if entry.is_symlink() or not entry.is_file():
                raise RunContractError(
                    f"unsafe attempt artifact {entry.name!r} for {case.sample_id}"
                )
            observed_files.add(entry.name)
        missing_files = _ATTEMPT_REQUIRED_FILES - observed_files
        extra_files = observed_files - _ATTEMPT_ALLOWED_FILES
        if missing_files:
            raise RunContractError(
                f"partial attempt {number} for {case.sample_id}; "
                f"missing={sorted(missing_files)}"
            )
        if extra_files:
            raise RunContractError(
                f"unexpected attempt files for {case.sample_id}: {sorted(extra_files)}"
            )
        attempt = _decode_json_object(
            _read_bytes(directory / "attempt.json", location="attempt record"),
            location=f"attempt record {case.sample_id}/{number}",
        )
        fields = frozenset(attempt)
        if fields != _ATTEMPT_FIELDS:
            raise RunContractError(
                f"attempt record fields differ for {case.sample_id}/{number}; "
                f"missing={sorted(_ATTEMPT_FIELDS - fields)}, "
                f"extra={sorted(fields - _ATTEMPT_FIELDS)}"
            )
        expected_identity = {
            "schema_version": "cofactor9.1.attempt.v1",
            "sample_id": case.sample_id,
            "attempt_number": number,
            "model": model.get("name"),
            "reasoning_effort": model.get("reasoning_effort"),
            "service_tier": model.get("service_tier"),
            "environment_policy": "fixed-allowlist",
        }
        for key, expected in expected_identity.items():
            if attempt.get(key) != expected:
                raise RunContractError(
                    f"attempt {case.sample_id}/{number} field {key!r} differs"
                )
        _validate_timestamp(
            attempt.get("started_at"),
            location=f"attempt {case.sample_id}/{number} started_at",
        )
        _validate_timestamp(
            attempt.get("completed_at"),
            location=f"attempt {case.sample_id}/{number} completed_at",
        )
        duration = attempt.get("duration_seconds")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or float(duration) < 0
        ):
            raise RunContractError(
                f"attempt {case.sample_id}/{number} duration_seconds is invalid"
            )
        argv = attempt.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(item, str) for item in argv)
        ):
            raise RunContractError(
                f"attempt {case.sample_id}/{number} argv is invalid"
            )
        returncode = attempt.get("returncode")
        if returncode is not None and (
            isinstance(returncode, bool) or not isinstance(returncode, int)
        ):
            raise RunContractError(
                f"attempt {case.sample_id}/{number} returncode is invalid"
            )
        if not isinstance(attempt.get("timed_out"), bool):
            raise RunContractError(
                f"attempt {case.sample_id}/{number} timed_out is invalid"
            )
        redactions = attempt.get("redaction_count")
        if (
            isinstance(redactions, bool)
            or not isinstance(redactions, int)
            or redactions < 0
        ):
            raise RunContractError(
                f"attempt {case.sample_id}/{number} redaction_count is invalid"
            )
        usage = attempt.get("usage")
        if not isinstance(usage, Mapping) or any(
            not isinstance(key, str)
            or not key
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for key, value in usage.items()
        ):
            raise RunContractError(
                f"attempt {case.sample_id}/{number} usage is invalid"
            )
        retry_disposition = attempt.get("retry_disposition")
        if retry_disposition not in {"none", "retryable", "nonretryable"}:
            raise RunContractError(
                f"attempt {case.sample_id}/{number} retry disposition is invalid"
            )
        error_code_value = attempt.get("error_code")
        error_message = attempt.get("error_message")
        if error_code_value is None:
            if error_message is not None or retry_disposition != "none":
                raise RunContractError(
                    f"attempt {case.sample_id}/{number} success/error fields conflict"
                )
            error_code: str | None = None
        else:
            if (
                not isinstance(error_code_value, str)
                or _ERROR_CODE.fullmatch(error_code_value) is None
                or not isinstance(error_message, str)
                or not error_message
                or retry_disposition == "none"
            ):
                raise RunContractError(
                    f"attempt {case.sample_id}/{number} error fields are invalid"
                )
            error_code = error_code_value
            if error_code in _KNOWN_INCIDENT_CODES:
                raise RunContractError(
                    f"attempt {case.sample_id}/{number} uses a transport-only error code"
                )
        attempt_error_codes.append(error_code)
        attempt_payloads.append(attempt)

        if error_code == "INTERRUPTED_ATTEMPT":
            raise RunContractError(
                f"attempt {case.sample_id}/{number} uses a transport-only error code"
            )
        if argv.count("-C") != 1:
            raise RunContractError(
                f"attempt {case.sample_id}/{number} argv differs from contract"
            )
        working_index = argv.index("-C") + 1
        if working_index >= len(argv):
            raise RunContractError(
                f"attempt {case.sample_id}/{number} argv differs from contract"
            )
        working_directory = Path(argv[working_index])
        if (
            not working_directory.is_absolute()
            or working_directory.parent != Path("/private/tmp")
            or not working_directory.name.startswith("cofactor9.1-attempt-")
        ):
            raise RunContractError(
                f"attempt {case.sample_id}/{number} working directory is invalid"
            )
        expected_argv = build_codex_argv(
            executable=executable,
            schema_path=schema_path,
            working_directory=working_directory,
            model_settings=_contract_model_settings(manifest_contract),
        )
        if argv != expected_argv:
            raise RunContractError(
                f"attempt {case.sample_id}/{number} argv differs from contract"
            )
        if attempt.get("prompt_sha256") != expected_prompt_sha256:
            raise RunContractError(
                f"attempt {case.sample_id}/{number} prompt differs from public case"
            )
        if error_code is None:
            if returncode != 0 or attempt.get("timed_out") is not False:
                raise RunContractError(
                    f"attempt {case.sample_id}/{number} success process fields conflict"
                )
        else:
            timed_out = attempt.get("timed_out")
            if error_code == "TIMEOUT" and timed_out is not True:
                raise RunContractError(
                    f"attempt {case.sample_id}/{number} timeout fields conflict"
                )
            nonretryable_codes = {
                "AUTH_ERROR",
                "PROCESS_START_ERROR",
                "TOOL_POLLUTION",
                "UNKNOWN_EVENT",
            }
            expected_disposition = (
                "nonretryable"
                if error_code in nonretryable_codes
                else "retryable"
            )
            if retry_disposition != expected_disposition:
                raise RunContractError(
                    f"attempt {case.sample_id}/{number} retry disposition conflicts "
                    "with error code"
                )
        for field, filename in (
            ("prompt_sha256", "prompt.txt"),
            ("stdout_sha256", "stdout.jsonl"),
            ("stderr_sha256", "stderr.txt"),
        ):
            recorded = attempt.get(field)
            observed = _sha256_file_bytes(
                directory / filename,
                location=f"attempt {filename}",
            )
            if recorded != observed:
                display = field.removesuffix("_sha256").replace("_", " ")
                raise RunContractError(
                    f"attempt {case.sample_id}/{number} {display} SHA-256 mismatch"
                )
        try:
            raw_stdout = _read_bytes(
                directory / "stdout.jsonl",
                location="attempt raw stdout",
            ).decode("utf-8")
            raw_stderr = _read_bytes(
                directory / "stderr.txt",
                location="attempt raw stderr",
            ).decode("utf-8")
        except UnicodeDecodeError as error:
            raise RunContractError(
                f"attempt {case.sample_id}/{number} transport output is not UTF-8"
            ) from error
        replay = replay_codex_attempt(
            case=case,
            argv=argv,
            stdout=raw_stdout,
            stderr=raw_stderr,
            returncode=returncode,
            timed_out=attempt["timed_out"],
            duration_seconds=float(duration),
        )
        if (
            replay.error_code != error_code
            or replay.error_message != error_message
            or replay.retry_disposition != retry_disposition
            or replay.thread_id != attempt.get("thread_id")
            or dict(replay.usage) != dict(usage)
        ):
            raise RunContractError(
                f"attempt {case.sample_id}/{number} raw transport outcome differs "
                "from attempt record"
            )
        prediction_path = directory / "prediction.json"
        if replay.prediction is None:
            if prediction_path.exists():
                raise RunContractError(
                    f"failed attempt {case.sample_id}/{number} has a prediction"
                )
        else:
            if not prediction_path.is_file():
                raise RunContractError(
                    f"successful attempt {case.sample_id}/{number} lacks prediction"
                )
            if _decode_json_object(
                _read_bytes(prediction_path, location="attempt prediction"),
                location=f"attempt prediction {case.sample_id}/{number}",
            ) != replay.prediction.to_dict():
                raise RunContractError(
                    f"attempt {case.sample_id}/{number} replayed prediction differs"
                )
        if terminal_payload is not None and attempt.get("prompt_sha256") != terminal_payload.get(
            "prompt_sha256"
        ):
            raise RunContractError(
                f"attempt {case.sample_id}/{number} prompt differs from terminal"
            )

    if any(error_code is None for error_code in attempt_error_codes[:-1]):
        raise RunContractError(
            f"attempt ledger {case.sample_id} continued after a successful attempt"
        )
    if any(
        payload.get("retry_disposition") != "retryable"
        for payload in attempt_payloads[:-1]
    ):
        raise RunContractError(
            f"attempt ledger {case.sample_id} continued after a nonretryable failure"
        )

    if terminal_payload is not None:
        attempt_count = terminal_payload.get("attempt_count")
        if attempt_count != len(numbered):
            raise RunContractError(
                f"terminal {case.sample_id} attempt_count differs from complete "
                "attempt ledger"
            )
        status = terminal_payload.get("status")
        final_prediction = numbered[-1][1] / "prediction.json" if numbered else None
        if status == "success":
            if (
                terminal_payload.get("error_code") is not None
                or terminal_payload.get("error_message") is not None
                or not attempt_error_codes
                or attempt_error_codes[-1] is not None
            ):
                raise RunContractError(
                    f"successful terminal {case.sample_id} conflicts with final attempt"
                )
            if final_prediction is None or not final_prediction.is_file():
                raise RunContractError(
                    f"successful terminal {case.sample_id} lacks final prediction"
                )
            prediction_payload = _decode_json_object(
                _read_bytes(final_prediction, location="saved prediction"),
                location=f"saved prediction {case.sample_id}",
            )
            if prediction_payload != terminal_payload.get("prediction"):
                raise RunContractError(
                    f"saved prediction for {case.sample_id} differs from terminal"
                )
            try:
                raw_output = parse_codex_stdout(
                    _read_bytes(
                        numbered[-1][1] / "stdout.jsonl",
                        location="successful raw stdout",
                    ).decode("utf-8")
                )
                replayed = parse_prediction_json(
                    raw_output.message_text,
                    expected_sample_id=case.sample_id,
                    allowed_labels=case.allowed_labels,
                )
            except Exception as error:
                raise RunContractError(
                    f"successful terminal {case.sample_id} raw stdout cannot be replayed: {error}"
                ) from error
            final_attempt = attempt_payloads[-1]
            if (
                replayed.to_dict() != prediction_payload
                or raw_output.thread_id != final_attempt.get("thread_id")
                or raw_output.usage != final_attempt.get("usage")
            ):
                raise RunContractError(
                    f"successful terminal {case.sample_id} raw stdout prediction "
                    "differs from published prediction or attempt metadata"
                )
        elif status == "terminal_error":
            terminal_error_code = terminal_payload.get("error_code")
            terminal_error_message = terminal_payload.get("error_message")
            if (
                terminal_payload.get("prediction") is not None
                or not isinstance(terminal_error_code, str)
                or _ERROR_CODE.fullmatch(terminal_error_code) is None
                or not isinstance(terminal_error_message, str)
                or not terminal_error_message
                or not attempt_error_codes
                or attempt_error_codes[-1] != terminal_error_code
                or attempt_payloads[-1].get("error_message")
                != terminal_error_message
            ):
                raise RunContractError(
                    f"error terminal {case.sample_id} differs from final attempt"
                )
            if (
                attempt_payloads[-1].get("retry_disposition") == "retryable"
                and len(numbered) != max_attempts
            ):
                raise RunContractError(
                    f"error terminal {case.sample_id} closed before retry budget"
                )
            if any(
                (directory / "prediction.json").exists()
                for _, directory in numbered
            ):
                raise RunContractError(
                    f"terminal error {case.sample_id} unexpectedly has a prediction file"
                )
        else:
            raise RunContractError(
                f"terminal {case.sample_id} has unsupported status {status!r}"
            )
    return len(numbered)


def _validate_transport_incidents(
    *,
    run_dir: Path,
    selected_cases: tuple[PromptCase, ...],
    manifest_contract: Mapping[str, Any],
) -> Mapping[str, tuple[LedgerBundle, ...]]:
    root = run_dir / "transport-incidents"
    if not root.exists():
        return MappingProxyType({})
    if root.is_symlink() or not root.is_dir():
        raise RunContractError("transport incident root is unsafe")
    cases_by_id = {case.sample_id: case for case in selected_cases}
    binary = _required_mapping(
        manifest_contract,
        "codex_binary",
        location="run manifest.contract",
    )
    if binary.get("frozen_path") != FROZEN_CODEX_EXECUTABLE_NAME:
        raise RunContractError("run manifest frozen Codex path is invalid")
    executable = str(run_dir / FROZEN_CODEX_EXECUTABLE_NAME)
    schema_path = _validate_frozen_response_schema(run_dir, manifest_contract)
    model_settings = _contract_model_settings(manifest_contract)
    result: dict[str, tuple[LedgerBundle, ...]] = {}
    for case_root in root.iterdir():
        if case_root.is_symlink() or not case_root.is_dir():
            raise RunContractError(
                f"unexpected transport incident entry {case_root.name!r}"
            )
        case = cases_by_id.get(case_root.name)
        if case is None:
            raise RunContractError(
                f"transport incident has unknown sample {case_root.name!r}"
            )
        numbered: list[tuple[int, Path]] = []
        for directory in case_root.iterdir():
            suffix = directory.name.removeprefix("incident-")
            if (
                directory.is_symlink()
                or not directory.is_dir()
                or not directory.name.startswith("incident-")
                or not suffix.isdigit()
            ):
                raise RunContractError(
                    f"unexpected incident ledger entry {directory.name!r}"
                )
            number = int(suffix)
            if number < 1 or directory.name != f"incident-{number:04d}":
                raise RunContractError(
                    f"invalid incident ledger entry {directory.name!r}"
                )
            numbered.append((number, directory))
        numbered.sort()
        if [number for number, _ in numbered] != list(range(1, len(numbered) + 1)):
            raise RunContractError(f"incident ledger gap for {case.sample_id}")
        bundles: list[LedgerBundle] = []
        for number, directory in numbered:
            observed: set[str] = set()
            for entry in directory.iterdir():
                if entry.is_symlink() or not entry.is_file():
                    raise RunContractError(
                        f"incident {case.sample_id}/{number} has unsafe artifact"
                    )
                observed.add(entry.name)
            missing = _INCIDENT_REQUIRED_FILES - observed
            extra = observed - _INCIDENT_ALLOWED_FILES
            if missing or extra:
                raise RunContractError(
                    f"incident artifact set differs for {case.sample_id}/{number}; "
                    f"missing={sorted(missing)}, extra={sorted(extra)}"
                )
            record_bytes = _read_bytes(
                directory / "incident.json",
                location="transport incident record",
            )
            prompt_bytes = _read_bytes(
                directory / "prompt.txt",
                location="transport incident prompt",
            )
            stdout_bytes = _read_bytes(
                directory / "stdout.jsonl",
                location="transport incident stdout",
            )
            stderr_bytes = _read_bytes(
                directory / "stderr.txt",
                location="transport incident stderr",
            )
            prediction_path = directory / "prediction.json"
            prediction_bytes = (
                _read_bytes(prediction_path, location="transport incident prediction")
                if prediction_path.is_file()
                else None
            )
            record = _decode_json_object(
                record_bytes,
                location=f"transport incident {case.sample_id}/{number}",
            )
            if frozenset(record) != _INCIDENT_FIELDS:
                raise RunContractError(
                    f"incident {case.sample_id}/{number} fields differ from contract"
                )
            expected_identity = {
                "schema_version": TRANSPORT_INCIDENT_SCHEMA_VERSION,
                "sample_id": case.sample_id,
                "incident_number": number,
                "model": model_settings.model,
                "reasoning_effort": model_settings.reasoning_effort,
                "service_tier": model_settings.service_tier,
                "environment_policy": "fixed-allowlist",
                "prompt_sha256": _sha256(prompt_bytes),
                "stdout_sha256": _sha256(stdout_bytes),
                "stderr_sha256": _sha256(stderr_bytes),
                "prediction_sha256": (
                    _sha256(prediction_bytes) if prediction_bytes is not None else None
                ),
            }
            for key, expected in expected_identity.items():
                if record.get(key) != expected:
                    raise RunContractError(
                        f"incident {case.sample_id}/{number} field {key!r} differs"
                    )
            if prompt_bytes != render_prompt(case).encode("utf-8"):
                raise RunContractError(
                    f"incident {case.sample_id}/{number} prompt differs from public case"
                )
            started = _validate_timestamp(
                record.get("started_at"),
                location=f"incident {case.sample_id}/{number} started_at",
            )
            completed = _validate_timestamp(
                record.get("completed_at"),
                location=f"incident {case.sample_id}/{number} completed_at",
            )
            if completed < started:
                raise RunContractError(
                    f"incident {case.sample_id}/{number} completion precedes start"
                )
            duration = record.get("duration_seconds")
            if (
                isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or not math.isfinite(float(duration))
                or float(duration) < 0
            ):
                raise RunContractError(
                    f"incident {case.sample_id}/{number} duration is invalid"
                )
            tentative = record.get("tentative_attempt_number")
            if (
                isinstance(tentative, bool)
                or not isinstance(tentative, int)
                or not 1 <= tentative <= MAX_ATTEMPTS
            ):
                raise RunContractError(
                    f"incident {case.sample_id}/{number} tentative attempt is invalid"
                )
            returncode = record.get("returncode")
            if returncode is not None and (
                isinstance(returncode, bool) or not isinstance(returncode, int)
            ):
                raise RunContractError(
                    f"incident {case.sample_id}/{number} returncode is invalid"
                )
            timed_out = record.get("timed_out")
            if not isinstance(timed_out, bool):
                raise RunContractError(
                    f"incident {case.sample_id}/{number} timed_out is invalid"
                )
            cancelled = record.get("cancelled")
            start_error = record.get("start_error")
            error_code = record.get("error_code")
            error_message = record.get("error_message")
            disposition = record.get("retry_disposition")
            if (
                error_code not in _KNOWN_INCIDENT_CODES
                or not isinstance(error_message, str)
                or not error_message
                or disposition not in {"retryable", "nonretryable"}
            ):
                raise RunContractError(
                    f"incident {case.sample_id}/{number} error fields are invalid"
                )
            usage = record.get("usage")
            if not isinstance(usage, Mapping) or any(
                not isinstance(key, str)
                or not key
                or isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for key, value in usage.items()
            ):
                raise RunContractError(
                    f"incident {case.sample_id}/{number} usage is invalid"
                )
            redactions = record.get("redaction_count")
            if (
                isinstance(redactions, bool)
                or not isinstance(redactions, int)
                or redactions < 0
            ):
                raise RunContractError(
                    f"incident {case.sample_id}/{number} redaction_count is invalid"
                )
            argv = record.get("argv")
            if (
                not isinstance(argv, list)
                or not argv
                or any(not isinstance(item, str) or not item for item in argv)
            ):
                raise RunContractError(
                    f"incident {case.sample_id}/{number} argv is invalid"
                )
            if error_code == "INTERRUPTED_ATTEMPT":
                expected_argv = [executable, "<interrupted-before-capture>"]
                if (
                    argv != expected_argv
                    or returncode is not None
                    or timed_out
                    or cancelled is not None
                    or start_error is not None
                    or disposition != "retryable"
                    or record.get("thread_id") is not None
                    or dict(usage)
                    or float(duration) != 0.0
                ):
                    raise RunContractError(
                        f"incident {case.sample_id}/{number} interruption fields conflict"
                    )
            else:
                if not isinstance(cancelled, bool):
                    raise RunContractError(
                        f"incident {case.sample_id}/{number} cancelled is invalid"
                    )
                if error_code == "PROCESS_START_ERROR":
                    if start_error != error_message:
                        raise RunContractError(
                            f"incident {case.sample_id}/{number} start_error differs"
                        )
                elif start_error is not None:
                    raise RunContractError(
                        f"incident {case.sample_id}/{number} has unexpected start_error"
                    )
                if (error_code == "RUN_CANCELLED") != cancelled:
                    raise RunContractError(
                        f"incident {case.sample_id}/{number} cancellation fields conflict"
                    )
                if argv.count("-C") != 1:
                    raise RunContractError(
                        f"incident {case.sample_id}/{number} argv differs from contract"
                    )
                working_index = argv.index("-C") + 1
                if working_index >= len(argv):
                    raise RunContractError(
                        f"incident {case.sample_id}/{number} argv differs from contract"
                    )
                working_directory = Path(argv[working_index])
                if (
                    not working_directory.is_absolute()
                    or working_directory.parent != Path("/private/tmp")
                    or not working_directory.name.startswith("cofactor9.1-attempt-")
                ):
                    raise RunContractError(
                        f"incident {case.sample_id}/{number} working directory is invalid"
                    )
                expected_argv = build_codex_argv(
                    executable=executable,
                    schema_path=schema_path,
                    working_directory=working_directory,
                    model_settings=model_settings,
                )
                if argv != expected_argv:
                    raise RunContractError(
                        f"incident {case.sample_id}/{number} argv differs from contract"
                    )
                try:
                    stdout = stdout_bytes.decode("utf-8")
                    stderr = stderr_bytes.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise RunContractError(
                        f"incident {case.sample_id}/{number} output is not UTF-8"
                    ) from error
                replay = replay_codex_attempt(
                    case=case,
                    argv=argv,
                    stdout=stdout,
                    stderr=stderr,
                    returncode=returncode,
                    timed_out=timed_out,
                    cancelled=cancelled,
                    start_error=start_error,
                    duration_seconds=float(duration),
                )
                if (
                    replay.error_code != error_code
                    or replay.error_message != error_message
                    or replay.retry_disposition != disposition
                    or replay.thread_id != record.get("thread_id")
                    or dict(replay.usage) != dict(usage)
                ):
                    raise RunContractError(
                        f"incident {case.sample_id}/{number} raw transport outcome "
                        "differs from incident record"
                    )
            if prediction_bytes is not None:
                prediction_payload = _decode_json_object(
                    prediction_bytes,
                    location=f"incident {case.sample_id}/{number} prediction",
                )
                try:
                    parse_prediction_json(
                        json.dumps(prediction_payload),
                        expected_sample_id=case.sample_id,
                        allowed_labels=case.allowed_labels,
                    )
                except Exception as error:
                    raise RunContractError(
                        f"incident {case.sample_id}/{number} prediction is invalid"
                    ) from error
            bundles.append(
                LedgerBundle(
                    number=number,
                    record_bytes=record_bytes,
                    prompt_bytes=prompt_bytes,
                    stdout_bytes=stdout_bytes,
                    stderr_bytes=stderr_bytes,
                    prediction_bytes=prediction_bytes,
                )
            )
        result[case.sample_id] = tuple(bundles)
    return MappingProxyType(result)


def _validate_run_tree(
    *,
    run_dir: Path,
    require_operational_quiescence: bool,
) -> None:
    for entry in run_dir.iterdir():
        if entry.name == ".DS_Store":
            if entry.is_symlink() or not entry.is_file():
                raise RunContractError("platform metadata artifact is unsafe")
            continue
        if entry.name not in _RUN_TOP_LEVEL_ALLOWED:
            raise RunContractError(f"unexpected run artifact {entry.name!r}")
        if entry.is_symlink():
            raise RunContractError(f"run artifact {entry.name!r} may not be a symlink")
    for name in (
        "manifest.json",
        FROZEN_RESPONSE_SCHEMA_NAME,
        FROZEN_CODEX_EXECUTABLE_NAME,
        "metrics.json",
        "report.md",
    ):
        path = run_dir / name
        if path.exists() and not path.is_file():
            raise RunContractError(f"run artifact {name!r} must be a real file")
    if not require_operational_quiescence:
        return
    for name in (".inflight", ".active"):
        root = run_dir / name
        if not root.exists():
            continue
        if not root.is_dir():
            raise RunContractError(f"operational {name} path is unsafe")
        if any(root.iterdir()):
            display = name.removeprefix(".")
            raise RunContractError(f"{display} operational directory must be empty")
    staging = run_dir / ".staging"
    if staging.exists():
        if not staging.is_dir():
            raise RunContractError("staging path is unsafe")
        for entry in staging.iterdir():
            allowed = entry.name in {
                ".directory-publish.lock",
                ".invocation-publish.lock",
                ".run-invocation.lock",
            }
            if entry.is_symlink() or not entry.is_file() or not allowed:
                raise RunContractError(
                    f"unexpected staging artifact {entry.name!r}"
                )


def _collect_ledger_bytes(
    *,
    run_dir: Path,
    selected_cases: tuple[PromptCase, ...],
    incident_bundles: Mapping[str, tuple[LedgerBundle, ...]],
    invocation_bundles: tuple[InvocationBundle, ...],
) -> tuple[
    Mapping[str, bytes],
    Mapping[str, tuple[LedgerBundle, ...]],
    str,
    int,
    float,
    Mapping[str, int],
]:
    terminal_records: dict[str, bytes] = {}
    attempt_bundles: dict[str, tuple[LedgerBundle, ...]] = {}
    framed: dict[str, bytes] = {}
    usage_totals: Counter[str] = Counter()
    duration_total = 0.0
    attempt_count = 0
    for case in selected_cases:
        case_dir = run_dir / "cases" / case.sample_id
        if case_dir.exists():
            allowed_case_entries = {"terminal.json", "attempts"}
            for entry in case_dir.iterdir():
                if entry.name not in allowed_case_entries:
                    raise RunContractError(
                        f"unexpected case ledger artifact {entry.name!r}"
                    )
        terminal_path = case_dir / "terminal.json"
        if terminal_path.is_file():
            terminal_bytes = _read_bytes(terminal_path, location="terminal record")
            terminal_records[case.sample_id] = terminal_bytes
            framed[f"terminal/{case.sample_id}"] = terminal_bytes
        attempts_root = case_dir / "attempts"
        if not attempts_root.exists():
            continue
        numbered: list[tuple[int, Path]] = []
        for directory in attempts_root.iterdir():
            suffix = directory.name.removeprefix("attempt-")
            if not suffix.isdigit():
                raise RunContractError(
                    f"unexpected attempt ledger entry {directory.name!r}"
                )
            numbered.append((int(suffix), directory))
        numbered.sort()
        bundles: list[LedgerBundle] = []
        for number, directory in numbered:
            record_bytes = _read_bytes(directory / "attempt.json", location="attempt record")
            prompt_bytes = _read_bytes(directory / "prompt.txt", location="attempt prompt")
            stdout_bytes = _read_bytes(directory / "stdout.jsonl", location="attempt stdout")
            stderr_bytes = _read_bytes(directory / "stderr.txt", location="attempt stderr")
            prediction_path = directory / "prediction.json"
            prediction_bytes = (
                _read_bytes(prediction_path, location="attempt prediction")
                if prediction_path.is_file()
                else None
            )
            bundle = LedgerBundle(
                number=number,
                record_bytes=record_bytes,
                prompt_bytes=prompt_bytes,
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
                prediction_bytes=prediction_bytes,
            )
            bundles.append(bundle)
            prefix = f"attempt/{case.sample_id}/{number:04d}"
            for filename, payload in (
                ("attempt.json", record_bytes),
                ("prompt.txt", prompt_bytes),
                ("stdout.jsonl", stdout_bytes),
                ("stderr.txt", stderr_bytes),
            ):
                framed[f"{prefix}/{filename}"] = payload
            if prediction_bytes is not None:
                framed[f"{prefix}/prediction.json"] = prediction_bytes
            record = _decode_json_object(record_bytes, location="attempt record")
            attempt_count += 1
            duration_total += float(record["duration_seconds"])
            for key, value in record["usage"].items():
                usage_totals[key] += value
        attempt_bundles[case.sample_id] = tuple(bundles)
    for sample_id, bundles in incident_bundles.items():
        for bundle in bundles:
            prefix = f"incident/{sample_id}/{bundle.number:04d}"
            for filename, payload in (
                ("incident.json", bundle.record_bytes),
                ("prompt.txt", bundle.prompt_bytes),
                ("stdout.jsonl", bundle.stdout_bytes),
                ("stderr.txt", bundle.stderr_bytes),
            ):
                framed[f"{prefix}/{filename}"] = payload
            if bundle.prediction_bytes is not None:
                framed[f"{prefix}/prediction.json"] = bundle.prediction_bytes
            record = _decode_json_object(bundle.record_bytes, location="incident record")
            duration_total += float(record["duration_seconds"])
            for key, value in record["usage"].items():
                usage_totals[key] += value
    for bundle in invocation_bundles:
        prefix = f"invocation/{bundle.number:04d}"
        framed[f"{prefix}/start.json"] = bundle.start_bytes
        framed[f"{prefix}/end.json"] = bundle.end_bytes
    return (
        MappingProxyType(terminal_records),
        MappingProxyType(attempt_bundles),
        _framed_hash("cofactor9.1.ledger-composite.v1", framed),
        attempt_count,
        round(duration_total, 6),
        MappingProxyType(dict(sorted(usage_totals.items()))),
    )


def _inspect_terminals(
    *,
    run_dir: Path,
    run_id: str,
    mode: str,
    selected_cases: tuple[PromptCase, ...],
    manifest_contract: Mapping[str, Any],
    require_complete: bool,
    config_bytes: bytes | None = None,
    require_operational_quiescence: bool = False,
) -> RunValidationSummary:
    _validate_run_tree(
        run_dir=run_dir,
        require_operational_quiescence=require_operational_quiescence,
    )
    cases_root = run_dir / "cases"
    expected_by_id = {case.sample_id: case for case in selected_cases}
    observed_directories: set[str] = set()
    if cases_root.exists():
        if cases_root.is_symlink() or not cases_root.is_dir():
            raise RunContractError("run cases path must be a real directory")
        for entry in cases_root.iterdir():
            if entry.is_symlink() or not entry.is_dir():
                raise RunContractError(f"unexpected case ledger entry {entry.name!r}")
            if entry.name not in expected_by_id:
                raise RunContractError(f"unexpected case directory {entry.name!r}")
            observed_directories.add(entry.name)

    model = _required_mapping(
        manifest_contract, "model", location="run manifest.contract"
    )
    terminal_count = 0
    success_count = 0
    terminal_error_count = 0
    missing: list[str] = []
    for case in selected_cases:
        case_dir = cases_root / case.sample_id
        terminal_path = cases_root / case.sample_id / "terminal.json"
        if not terminal_path.exists():
            _validate_attempt_ledger(
                case_dir=case_dir,
                case=case,
                manifest_contract=manifest_contract,
                terminal_payload=None,
            )
            missing.append(case.sample_id)
            continue
        if terminal_path.is_symlink() or not terminal_path.is_file():
            raise RunContractError(
                f"terminal record for {case.sample_id} must be a real file"
            )
        payload = _strict_terminal_payload(terminal_path)
        _validate_attempt_ledger(
            case_dir=case_dir,
            case=case,
            manifest_contract=manifest_contract,
            terminal_payload=payload,
        )
        expected_metadata = {
            "schema_version": TERMINAL_SCHEMA_VERSION,
            "sample_id": case.sample_id,
            "model": model.get("name"),
            "reasoning_effort": model.get("reasoning_effort"),
            "service_tier": model.get("service_tier"),
            "prompt_version": model.get("prompt_version"),
            "catalog_version": case.catalog_version,
        }
        for key, expected in expected_metadata.items():
            if payload.get(key) != expected:
                raise RunContractError(
                    f"terminal {case.sample_id} field {key!r} differs from manifest"
                )
        _validate_timestamp(
            payload.get("completed_at"),
            location=f"terminal {case.sample_id} completed_at",
        )
        if payload.get("prompt_sha256") != _sha256(
            render_prompt(case).encode("utf-8")
        ):
            raise RunContractError(
                f"terminal {case.sample_id} prompt differs from public case"
            )
        try:
            terminal = _load_terminal(
                terminal_path,
                case,
                model_settings=_contract_model_settings(manifest_contract),
            )
        except Exception as error:
            raise RunContractError(
                f"terminal {case.sample_id} is unreadable: {error}"
            ) from error
        terminal_count += 1
        if terminal.status == "success":
            success_count += 1
        else:
            terminal_error_count += 1

    if require_complete and missing:
        preview = ", ".join(missing[:3])
        raise RunContractError(
            f"missing terminal records: {len(missing)} (first: {preview})"
        )
    manifest_bytes = _read_bytes(run_dir / "manifest.json", location="run manifest")
    incident_bundles = _validate_transport_incidents(
        run_dir=run_dir,
        selected_cases=selected_cases,
        manifest_contract=manifest_contract,
    )
    invocation_bundles: tuple[InvocationBundle, ...] = ()
    if config_bytes is not None:
        invocation_bundles = _validate_invocations(
            run_dir=run_dir,
            run_id=run_id,
            manifest_bytes=manifest_bytes,
            config_bytes=config_bytes,
            manifest_contract=manifest_contract,
            require_closed=True,
        )
        if require_operational_quiescence and not invocation_bundles:
            raise RunContractError("formal run has no closed invocation ledger")
    (
        _,
        _,
        ledger_composite,
        attempt_count,
        total_duration,
        usage,
    ) = _collect_ledger_bytes(
        run_dir=run_dir,
        selected_cases=selected_cases,
        incident_bundles=incident_bundles,
        invocation_bundles=invocation_bundles,
    )
    incident_entries: dict[str, bytes] = {}
    incident_codes: Counter[str] = Counter()
    for sample_id, bundles in incident_bundles.items():
        for bundle in bundles:
            prefix = f"incident/{sample_id}/{bundle.number:04d}"
            incident_entries[f"{prefix}/incident.json"] = bundle.record_bytes
            incident_entries[f"{prefix}/prompt.txt"] = bundle.prompt_bytes
            incident_entries[f"{prefix}/stdout.jsonl"] = bundle.stdout_bytes
            incident_entries[f"{prefix}/stderr.txt"] = bundle.stderr_bytes
            if bundle.prediction_bytes is not None:
                incident_entries[f"{prefix}/prediction.json"] = bundle.prediction_bytes
            record = _decode_json_object(bundle.record_bytes, location="incident record")
            incident_codes[str(record["error_code"])] += 1
    invocation_entries: dict[str, bytes] = {}
    for bundle in invocation_bundles:
        invocation_entries[
            f"invocation/{bundle.number:04d}/start.json"
        ] = bundle.start_bytes
        invocation_entries[
            f"invocation/{bundle.number:04d}/end.json"
        ] = bundle.end_bytes
    return RunValidationSummary(
        run_id=run_id,
        mode=mode,
        selected_case_count=len(selected_cases),
        terminal_count=terminal_count,
        success_count=success_count,
        terminal_error_count=terminal_error_count,
        missing_terminal_count=len(missing),
        manifest_sha256=_sha256(manifest_bytes),
        attempt_count=attempt_count,
        incident_count=sum(len(value) for value in incident_bundles.values()),
        invocation_count=len(invocation_bundles),
        total_duration_seconds=total_duration,
        usage=usage,
        incident_error_code_counts=MappingProxyType(dict(sorted(incident_codes.items()))),
        incident_composite_sha256=_framed_hash(
            "cofactor9.1.incident-composite.v1",
            incident_entries,
        ),
        invocation_composite_sha256=_framed_hash(
            "cofactor9.1.invocation-composite.v1",
            invocation_entries,
        ),
        ledger_composite_sha256=ledger_composite,
    )


def inspect_run_progress_from_config(
    *,
    config_path: str | Path,
    run_id: str,
    expected_case_count: int = EXPECTED_CASE_COUNT,
) -> RunValidationSummary:
    """Read current terminal progress without launching or resuming any case."""

    _validate_run_id(run_id)
    inputs = _load_inputs(config_path)
    run_dir = inputs.runs_root / run_id
    manifest, _ = _load_manifest(run_dir)
    if manifest.get("run_id") != run_id:
        raise RunContractError("run manifest run_id differs from directory")
    contract = _required_mapping(manifest, "contract", location="run manifest")
    _validate_frozen_response_schema(run_dir, contract)
    _validate_frozen_codex_executable(run_dir, contract)
    cases_contract = _required_mapping(
        contract, "cases", location="run manifest.contract"
    )
    selected_count = cases_contract.get("selected_count")
    if isinstance(selected_count, bool) or not isinstance(selected_count, int):
        raise RunContractError("run manifest selected_count is invalid")
    selected_cases = inputs.cases[:selected_count]
    if cases_contract.get("selected_sample_ids_sha256") != _selected_id_sha256(
        selected_cases
    ):
        raise RunContractError("run manifest case selection hash is invalid")
    if cases_contract.get("expected_formal_count") != expected_case_count:
        raise RunContractError("run manifest expected formal count differs")
    return _inspect_terminals(
        run_dir=run_dir,
        run_id=run_id,
        mode=str(manifest.get("mode")),
        selected_cases=selected_cases,
        manifest_contract=contract,
        require_complete=False,
        config_bytes=inputs.config_bytes,
    )


def validate_run_from_config(
    *,
    config_path: str | Path,
    run_id: str,
    expected_case_count: int = EXPECTED_CASE_COUNT,
    executable: str | Path | None = None,
    case_validator: Callable[[str | Path], object] = validate_cases_from_config,
) -> RunValidationSummary:
    """Validate a formal run against current frozen inputs without model calls."""

    _validate_run_id(run_id)
    inputs = _load_inputs(config_path)
    _validate_fixed_model_contract(inputs.config)
    _validate_case_artifacts(case_validator, inputs.config_path)
    if len(inputs.cases) != expected_case_count:
        raise RunContractError(
            f"formal validation requires exactly {expected_case_count} public cases"
        )
    run_dir = inputs.runs_root / run_id
    manifest, _ = _load_manifest(run_dir)
    if manifest.get("run_id") != run_id:
        raise RunContractError("run manifest run_id differs from directory")
    if manifest.get("dataset_version") != inputs.config.get("dataset_version"):
        raise RunContractError("run manifest dataset_version differs from config")
    if manifest.get("mode") != "formal":
        raise RunContractError("formal run validation rejects infrastructure gates")
    contract = _required_mapping(manifest, "contract", location="run manifest")
    _validate_frozen_response_schema(run_dir, contract)
    _validate_frozen_codex_executable(run_dir, contract)
    cases_contract = _required_mapping(
        contract, "cases", location="run manifest.contract"
    )
    if cases_contract.get("selection_limit") is not None:
        raise RunContractError("formal run manifest may not contain a case limit")
    if cases_contract.get("total_count") != expected_case_count:
        raise RunContractError("formal run manifest total count differs")
    if cases_contract.get("selected_count") != expected_case_count:
        raise RunContractError("formal run manifest selected count differs")

    transport_contract = _required_mapping(
        contract, "transport", location="run manifest.contract"
    )
    execution_contract = _required_mapping(
        contract, "execution", location="run manifest.contract"
    )
    binary_contract = _required_mapping(
        contract, "codex_binary", location="run manifest.contract"
    )
    binary = _recorded_binary_identity(binary_contract)
    runtime_contract = _required_mapping(
        contract,
        "launcher_runtime",
        location="run manifest.contract",
    )
    recorded_runtime = _recorded_launcher_runtime(runtime_contract)
    if executable is not None:
        current_binary = _binary_identity(executable)
        if binary_contract != _codex_binary_contract(current_binary):
            raise RunContractError("current Codex binary differs from run manifest")
    max_attempts = transport_contract.get("max_attempts")
    concurrency = execution_contract.get("concurrency")
    timeout = transport_contract.get("timeout_seconds")
    breaker = transport_contract.get("circuit_breaker_threshold")
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or max_attempts != MAX_ATTEMPTS
        or isinstance(concurrency, bool)
        or not isinstance(concurrency, int)
        or not 1 <= concurrency <= MAX_CONCURRENCY
        or isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or not 0 < float(timeout) < 86_400
        or isinstance(breaker, bool)
        or not isinstance(breaker, int)
        or breaker != DEFAULT_CIRCUIT_BREAKER_THRESHOLD
    ):
        raise RunContractError("run manifest execution settings are malformed")
    (
        configured_max_attempts,
        configured_concurrency,
        configured_timeout,
        configured_breaker,
        configured_transport,
    ) = _configured_run_settings(
        inputs.config,
        concurrency=None,
        timeout_seconds=None,
        circuit_breaker_threshold=None,
    )
    if (
        max_attempts,
        concurrency,
        float(timeout),
        breaker,
    ) != (
        configured_max_attempts,
        configured_concurrency,
        configured_timeout,
        configured_breaker,
    ):
        raise RunContractError("run manifest execution settings differ from config")
    expected_contract = _make_contract(
        inputs,
        selected_cases=inputs.cases,
        expected_case_count=expected_case_count,
        selection_limit=None,
        binary=binary,
        max_attempts=configured_max_attempts,
        concurrency=configured_concurrency,
        timeout_seconds=configured_timeout,
        circuit_breaker_threshold=configured_breaker,
        transport=configured_transport,
        launcher_runtime=recorded_runtime,
    )
    if contract != expected_contract:
        raise RunContractError("run manifest contract differs from current artifacts")
    return _inspect_terminals(
        run_dir=run_dir,
        run_id=run_id,
        mode="formal",
        selected_cases=inputs.cases,
        manifest_contract=contract,
        require_complete=True,
        config_bytes=inputs.config_bytes,
        require_operational_quiescence=True,
    )


def verified_run_snapshot_from_config(
    *,
    config_path: str | Path,
    run_id: str,
    expected_case_count: int = EXPECTED_CASE_COUNT,
    executable: str | Path | None = None,
    case_validator: Callable[[str | Path], object] = validate_cases_from_config,
) -> VerifiedRunSnapshot:
    """Freeze exactly the bytes accepted by formal validation for scoring."""

    validation = validate_run_from_config(
        config_path=config_path,
        run_id=run_id,
        expected_case_count=expected_case_count,
        executable=executable,
        case_validator=case_validator,
    )
    inputs = _load_inputs(config_path)
    run_dir = inputs.runs_root / run_id
    manifest, manifest_bytes = _load_manifest(run_dir)
    contract = _required_mapping(manifest, "contract", location="run manifest")
    artifact_contract = _required_mapping(
        contract,
        "evaluation_artifacts",
        location="run manifest.contract",
    )
    if set(artifact_contract) != set(_EVALUATION_ARTIFACT_KEYS):
        raise RunContractError("run manifest evaluation artifact closure differs")
    artifact_bytes = {
        name: bytes(inputs.artifact_bytes[name])
        for name in _EVALUATION_ARTIFACT_KEYS
    }
    artifact_sha256 = {name: _sha256(value) for name, value in artifact_bytes.items()}
    for name in _EVALUATION_ARTIFACT_KEYS:
        descriptor = artifact_contract.get(name)
        if not isinstance(descriptor, Mapping) or descriptor.get("sha256") != artifact_sha256[name]:
            raise RunContractError(f"snapshot artifact {name} differs from run manifest")
    implementation_contract = _required_mapping(
        contract,
        "evaluation_implementations",
        location="run manifest.contract",
    )
    if set(implementation_contract) != set(_EVALUATION_IMPLEMENTATION_KEYS):
        raise RunContractError("evaluation implementation closure differs")
    implementation_sha256: dict[str, str] = {}
    for name in _EVALUATION_IMPLEMENTATION_KEYS:
        descriptor = implementation_contract.get(name)
        if not isinstance(descriptor, Mapping):
            raise RunContractError(f"evaluation implementation {name} is malformed")
        sha = descriptor.get("sha256")
        if not isinstance(sha, str) or _SHA256.fullmatch(sha) is None:
            raise RunContractError(f"evaluation implementation {name} hash is malformed")
        implementation_sha256[name] = sha
    incident_bundles = _validate_transport_incidents(
        run_dir=run_dir,
        selected_cases=inputs.cases,
        manifest_contract=contract,
    )
    invocation_bundles = _validate_invocations(
        run_dir=run_dir,
        run_id=run_id,
        manifest_bytes=manifest_bytes,
        config_bytes=inputs.config_bytes,
        manifest_contract=contract,
        require_closed=True,
    )
    (
        terminal_records,
        attempt_bundles,
        ledger_composite,
        _,
        _,
        _,
    ) = _collect_ledger_bytes(
        run_dir=run_dir,
        selected_cases=inputs.cases,
        incident_bundles=incident_bundles,
        invocation_bundles=invocation_bundles,
    )
    if ledger_composite != validation.ledger_composite_sha256:
        raise RunContractError("run ledger changed while snapshot was being frozen")
    post_validation = _inspect_terminals(
        run_dir=run_dir,
        run_id=run_id,
        mode="formal",
        selected_cases=inputs.cases,
        manifest_contract=contract,
        require_complete=True,
        config_bytes=inputs.config_bytes,
        require_operational_quiescence=True,
    )
    if post_validation.ledger_composite_sha256 != ledger_composite:
        raise RunContractError("run ledger changed during snapshot verification")
    provenance_entries: dict[str, bytes] = {
        "manifest/manifest.json": manifest_bytes,
        "ledger/composite.sha256": ledger_composite.encode("ascii"),
    }
    provenance_entries.update(
        {f"artifact/{name}": value for name, value in artifact_bytes.items()}
    )
    provenance_entries.update(
        {
            f"implementation/{name}.sha256": value.encode("ascii")
            for name, value in implementation_sha256.items()
        }
    )
    provenance_composite = _framed_hash(
        "cofactor9.1.scoring-provenance.v1",
        provenance_entries,
    )
    if _read_bytes(run_dir / "manifest.json", location="run manifest recheck") != manifest_bytes:
        raise RunContractError("run manifest changed while snapshot was being frozen")
    for name, path in inputs.artifact_paths.items():
        if _read_bytes(path, location=f"artifact {name} recheck") != artifact_bytes[name]:
            raise RunContractError(
                f"evaluation artifact {name} changed while snapshot was being frozen"
            )
    return VerifiedRunSnapshot(
        run_id=run_id,
        run_dir=run_dir,
        validation=validation,
        manifest_bytes=manifest_bytes,
        manifest_sha256=_sha256(manifest_bytes),
        evaluation_implementation_sha256=MappingProxyType(
            implementation_sha256
        ),
        artifact_bytes=MappingProxyType(artifact_bytes),
        artifact_sha256=MappingProxyType(artifact_sha256),
        terminal_records=terminal_records,
        attempt_bundles=attempt_bundles,
        incident_bundles=incident_bundles,
        invocation_bundles=invocation_bundles,
        ledger_composite_sha256=ledger_composite,
        provenance_composite_sha256=provenance_composite,
    )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "RUN_MANIFEST_SCHEMA_VERSION",
    "InvocationBundle",
    "LedgerBundle",
    "RunContractError",
    "RunValidationSummary",
    "VerifiedRunSnapshot",
    "execute_run_from_config",
    "inspect_run_progress_from_config",
    "validate_run_from_config",
    "verified_run_snapshot_from_config",
]
