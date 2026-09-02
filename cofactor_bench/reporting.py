"""Strict offline result assembly for the frozen Cofactor9.1 benchmark.

This module never calls a model.  It closes the public sample IDs back to the
private accession map, revalidates every saved terminal response, and derives
all reported numbers from immutable bytes.  Formal reports fail closed on an
incomplete ledger; explicitly partial reports are permanently marked as
diagnostic-only.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import re
import statistics
from typing import Any, Protocol

from .cases import PRIVATE_MAPPING_PURPOSE, PRIVATE_MAPPING_SCHEMA_VERSION
from .deepseek_adapter import (
    API_ENDPOINT as DEEPSEEK_API_ENDPOINT,
    MAX_OUTPUT_TOKENS as DEEPSEEK_MAX_OUTPUT_TOKENS,
    MODEL as DEEPSEEK_MODEL,
    REASONING_EFFORT as DEEPSEEK_REASONING_EFFORT,
    SERVICE_TIER as DEEPSEEK_SERVICE_TIER,
    THINKING_TYPE as DEEPSEEK_THINKING_TYPE,
)
from .metrics import CalibrationObservation, score_calibration
from .prediction import Prediction, PredictionValidationError, validate_prediction
from .prompt import CATALOG_SIZE, PROMPT_VERSION, CatalogTerm, PromptCase, render_prompt
from .runner import MODEL, REASONING_EFFORT, SERVICE_TIER
from .scoring import RecordScore, ancestor_distances_from_pairs, score_record


RESULT_SCHEMA_VERSION = "cofactor9.1.results.v1"
TERMINAL_SCHEMA_VERSION = "cofactor9.1.terminal.v1"
ATTEMPT_SCHEMA_VERSION = "cofactor9.1.attempt.v1"
TRANSPORT_INCIDENT_SCHEMA_VERSION = "cofactor9.1.transport-incident.v1"
RUN_MANIFEST_SCHEMA_VERSION = "cofactor9.1.run-manifest.v1"
CLUSTER_RECORD_SCHEMA_VERSION = "cofactor9.1.homology-cluster-record.v1"
CLUSTER_ID_VERSION = "cofactor9.1.homology-cluster-id.v1"
CLUSTER_IDENTITY_PERCENT = 90
CLUSTER_MUTUAL_COVERAGE_PERCENT = 80

_VIEW_SCHEMA_VERSION = "cofactor9.1.view-record.v1"
_CATALOG_SCHEMA_VERSION = "cofactor9.1.label-catalog.v1"
_ONTOLOGY_SCHEMA_VERSION = "cofactor9.1.ontology-audit.v1"
_DATASET_VERSION = "Cofactor9.1"
_VIEW_RULE_VERSION = "cofactor9.1.views.v3"
_FORMULA_RULE_VERSION = "cofactor9.1.formula.v2"
_ACCESSION = re.compile(r"[A-Z0-9]{6,10}\Z")
_SAMPLE_ID = re.compile(r"sample_[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CLUSTER_ID = re.compile(r"cluster_[0-9a-f]{64}\Z")
_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_PARSE_ERROR_CODES = frozenset({"INVALID_PREDICTION", "CASE_MISMATCH"})
_EXACT_SEQUENCE_STATUSES = frozenset(
    {"UNIQUE", "DUPLICATE_CONSISTENT", "DUPLICATE_CONFLICT"}
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
_INCIDENT_FIELDS = _ATTEMPT_FIELDS - {"attempt_number"} | {
    "incident_number",
    "tentative_attempt_number",
    "prediction_sha256",
    "cancelled",
    "start_error",
}
_INCIDENT_ERROR_CODES = frozenset(
    {
        "AUTH_ERROR",
        "CAPACITY_ERROR",
        "PROCESS_START_ERROR",
        "TRANSPORT_ERROR",
        "RUN_CANCELLED",
        "INTERRUPTED_ATTEMPT",
    }
)
_PRIVATE_FIELDS = frozenset(
    {
        "schema_version",
        "visibility",
        "purpose",
        "sample_id",
        "accession",
        "sequence_sha256",
    }
)
_CLUSTER_FIELDS = frozenset(
    {
        "schema_version",
        "accession",
        "cluster_id",
        "representative_accession",
        "member_count",
    }
)
_EVALUATION_ARTIFACT_KEYS = frozenset(
    {
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
    }
)
_EVALUATION_IMPLEMENTATION_KEYS = frozenset(
    {"reporting", "scoring", "metrics", "prediction"}
)


class ReportingError(ValueError):
    """Raised when an offline input cannot support an auditable report."""


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    """A validated, deterministic machine-readable result document."""

    _value: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return deepcopy(self._value)


class LedgerBundleLike(Protocol):
    """Byte-only attempt/incident bundle supplied by the run validator."""

    number: int
    record_bytes: bytes
    prompt_bytes: bytes
    stdout_bytes: bytes
    stderr_bytes: bytes
    prediction_bytes: bytes | None


class InvocationBundleLike(Protocol):
    """Byte-only invocation bundle supplied by the run validator."""

    number: int
    start_bytes: bytes
    end_bytes: bytes


class VerifiedRunSnapshotLike(Protocol):
    """Structural contract consumed without importing or rereading ``run.py``."""

    run_id: str
    validation: object
    manifest_bytes: bytes
    manifest_sha256: str
    evaluation_implementation_sha256: Mapping[str, str]
    artifact_bytes: Mapping[str, bytes]
    artifact_sha256: Mapping[str, str]
    terminal_records: Mapping[str, bytes]
    attempt_bundles: Mapping[str, Sequence[LedgerBundleLike]]
    incident_bundles: Mapping[str, Sequence[LedgerBundleLike]]
    invocation_bundles: Sequence[InvocationBundleLike]
    ledger_composite_sha256: str
    provenance_composite_sha256: str


@dataclass(frozen=True, slots=True)
class _RawBundle:
    number: int
    record_bytes: bytes
    prompt_bytes: bytes
    stdout_bytes: bytes
    stderr_bytes: bytes
    prediction_bytes: bytes | None


@dataclass(frozen=True, slots=True)
class _RawInvocation:
    number: int
    start_bytes: bytes
    end_bytes: bytes


@dataclass(frozen=True, slots=True)
class _VerifiedInputs:
    run_id: str
    manifest: dict[str, Any]
    manifest_bytes: bytes
    manifest_sha256: str
    contract: dict[str, Any]
    versions: dict[str, str]
    implementation_sha256: dict[str, str]
    artifact_bytes: dict[str, bytes]
    artifact_sha256: dict[str, str]
    terminal_records: dict[str, bytes]
    attempt_bundles: dict[str, tuple[_RawBundle, ...]]
    incident_bundles: dict[str, tuple[_RawBundle, ...]]
    invocation_bundles: tuple[_RawInvocation, ...]
    ledger_composite_sha256: str
    provenance_composite_sha256: str


@dataclass(frozen=True, slots=True)
class _Catalog:
    version: str
    terms: tuple[CatalogTerm, ...]
    bands: dict[str, str]

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(term.chebi_id for term in self.terms)


@dataclass(frozen=True, slots=True)
class _PrivateLink:
    sample_id: str
    accession: str
    sequence_sha256: str


@dataclass(frozen=True, slots=True)
class _GoldRecord:
    accession: str
    sequence: str
    sequence_sha256: str
    sequence_entity_id: str
    exact_sequence_status: str
    exact_sequence_members: tuple[str, ...]
    gold_blocks: tuple[tuple[str, ...], ...]
    overlapping_block_pair_count: int
    core_included: bool

    @property
    def conflict(self) -> bool:
        return self.exact_sequence_status == "DUPLICATE_CONFLICT"

    @property
    def overlapping_blocks(self) -> bool:
        return self.overlapping_block_pair_count > 0

    @property
    def ranking_excluded(self) -> bool:
        return self.conflict or self.overlapping_blocks


@dataclass(frozen=True, slots=True)
class _Terminal:
    sample_id: str
    status: str
    attempt_count: int
    prediction: Prediction | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class _Attempt:
    sample_id: str
    attempt_number: int
    duration_seconds: float
    error_code: str | None
    usage: dict[str, int]


@dataclass(frozen=True, slots=True)
class _Incident:
    sample_id: str
    incident_number: int
    tentative_attempt_number: int
    duration_seconds: float
    error_code: str
    usage: dict[str, int]


@dataclass(frozen=True, slots=True)
class _ScoredRecord:
    gold: _GoldRecord
    terminal: _Terminal
    score: RecordScore
    weight: float


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _reject_constant(value: str) -> None:
    raise ReportingError(f"Non-finite JSON number {value!r} is forbidden")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReportingError(f"Duplicate JSON field {key!r} is forbidden")
        result[key] = value
    return result


def _decode_json(value: bytes, *, location: str) -> Any:
    if not isinstance(value, bytes):
        raise TypeError(f"{location} must be bytes")
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReportingError(f"{location} is not valid UTF-8") from error
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except ReportingError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise ReportingError(f"{location} is not valid JSON: {error}") from error


def _decode_jsonl(value: bytes, *, location: str) -> list[dict[str, Any]]:
    if not isinstance(value, bytes):
        raise TypeError(f"{location} must be bytes")
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReportingError(f"{location} is not valid UTF-8") from error
    if not text:
        raise ReportingError(f"{location} is empty")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            raise ReportingError(f"{location} line {line_number} is blank")
        decoded = _decode_json(line.encode("utf-8"), location=f"{location} line {line_number}")
        if not isinstance(decoded, dict):
            raise ReportingError(f"{location} line {line_number} must be an object")
        records.append(decoded)
    return records


def _exact_fields(value: Mapping[str, Any], fields: frozenset[str], *, location: str) -> None:
    actual = frozenset(value)
    if actual != fields:
        raise ReportingError(
            f"{location} fields differ; missing={sorted(fields - actual)}, "
            f"extra={sorted(str(item) for item in actual - fields)}"
        )


def _mapping(value: object, *, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReportingError(f"{location} must be an object")
    return value


def _string(value: object, *, location: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise ReportingError(f"{location} must be a nonempty control-free string")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise ReportingError(f"{location} has an invalid format")
    return value


def _positive_int(value: object, *, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ReportingError(f"{location} must be a positive integer")
    return value


def _nonnegative_int(value: object, *, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReportingError(f"{location} must be a nonnegative integer")
    return value


def _finite_nonnegative(value: object, *, location: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ReportingError(f"{location} must be a finite nonnegative number")
    return float(value)


def _timestamp(value: object, *, location: str) -> str:
    text = _string(value, location=location)
    if not text.endswith("Z"):
        raise ReportingError(f"{location} must be an explicit UTC timestamp")
    try:
        datetime.fromisoformat(text.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise ReportingError(f"{location} is not a valid timestamp") from error
    return text


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _safe_run_id(value: object, *, location: str = "run_id") -> str:
    run_id = _string(value, location=location)
    if (
        len(run_id) > 128
        or run_id in {".", ".."}
        or "/" in run_id
        or "\\" in run_id
    ):
        raise ReportingError(f"{location} must be a safe single path component")
    return run_id


def _copy_bytes_mapping(
    value: object,
    *,
    location: str,
    exact_keys: frozenset[str] | None = None,
) -> dict[str, bytes]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{location} must be a mapping")
    try:
        items = list(value.items())
    except RuntimeError as error:
        raise ReportingError(f"{location} changed while snapshotting") from error
    copied: dict[str, bytes] = {}
    for key, payload in items:
        if not isinstance(key, str) or not key:
            raise ReportingError(f"{location} keys must be nonempty strings")
        if not isinstance(payload, bytes):
            raise TypeError(f"{location}.{key} must be bytes")
        copied[key] = payload
    if exact_keys is not None and frozenset(copied) != exact_keys:
        raise ReportingError(
            f"{location} keys differ; missing={sorted(exact_keys - frozenset(copied))}, "
            f"extra={sorted(frozenset(copied) - exact_keys)}"
        )
    return copied


def _copy_sha256_mapping(
    value: object,
    *,
    location: str,
    exact_keys: frozenset[str],
) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{location} must be a mapping")
    copied = dict(value)
    if frozenset(copied) != exact_keys:
        raise ReportingError(
            f"{location} keys differ; missing={sorted(exact_keys - frozenset(copied))}, "
            f"extra={sorted(frozenset(copied) - exact_keys)}"
        )
    for key, digest in copied.items():
        _string(digest, location=f"{location}.{key}", pattern=_SHA256)
    return copied


def _copy_bundle_mapping(
    value: object,
    *,
    location: str,
) -> dict[str, tuple[_RawBundle, ...]]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{location} must be a mapping")
    result: dict[str, tuple[_RawBundle, ...]] = {}
    for sample_id, raw_bundles in list(value.items()):
        _string(sample_id, location=f"{location} key", pattern=_SAMPLE_ID)
        if isinstance(raw_bundles, (str, bytes)) or not isinstance(
            raw_bundles, Sequence
        ):
            raise ReportingError(f"{location}.{sample_id} must be a sequence")
        bundles: list[_RawBundle] = []
        for expected_number, raw in enumerate(tuple(raw_bundles), start=1):
            number = getattr(raw, "number", None)
            if number != expected_number:
                raise ReportingError(
                    f"{location}.{sample_id} bundle numbering is not contiguous"
                )
            fields: dict[str, bytes | None] = {}
            for field in (
                "record_bytes",
                "prompt_bytes",
                "stdout_bytes",
                "stderr_bytes",
                "prediction_bytes",
            ):
                payload = getattr(raw, field, None)
                if field == "prediction_bytes" and payload is None:
                    fields[field] = None
                    continue
                if not isinstance(payload, bytes):
                    raise TypeError(
                        f"{location}.{sample_id}/{number}.{field} must be bytes"
                    )
                fields[field] = payload
            bundles.append(
                _RawBundle(
                    number=number,
                    record_bytes=fields["record_bytes"],  # type: ignore[arg-type]
                    prompt_bytes=fields["prompt_bytes"],  # type: ignore[arg-type]
                    stdout_bytes=fields["stdout_bytes"],  # type: ignore[arg-type]
                    stderr_bytes=fields["stderr_bytes"],  # type: ignore[arg-type]
                    prediction_bytes=fields["prediction_bytes"],
                )
            )
        if not bundles:
            raise ReportingError(f"{location}.{sample_id} must not be empty")
        result[sample_id] = tuple(bundles)
    return result


def _copy_invocations(value: object) -> tuple[_RawInvocation, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("invocation_bundles must be a sequence")
    result: list[_RawInvocation] = []
    for expected_number, raw in enumerate(tuple(value), start=1):
        number = getattr(raw, "number", None)
        start_bytes = getattr(raw, "start_bytes", None)
        end_bytes = getattr(raw, "end_bytes", None)
        if number != expected_number:
            raise ReportingError("invocation bundle numbering is not contiguous")
        if not isinstance(start_bytes, bytes) or not isinstance(end_bytes, bytes):
            raise TypeError("invocation bundle values must be bytes")
        result.append(_RawInvocation(number, start_bytes, end_bytes))
    return tuple(result)


def _framed_sha256(domain: str, frames: Sequence[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    digest.update(domain.encode("ascii") + b"\0")
    for key, payload in sorted(frames):
        digest.update(key.encode("utf-8") + b"\0")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _bundle_frames(
    kind: str,
    bundles: Mapping[str, Sequence[_RawBundle]],
) -> list[tuple[str, bytes]]:
    frames: list[tuple[str, bytes]] = []
    for sample_id, sample_bundles in bundles.items():
        for bundle in sample_bundles:
            prefix = f"{kind}/{sample_id}/{bundle.number:04d}"
            record_name = "attempt.json" if kind == "attempt" else "incident.json"
            frames.extend(
                (
                    (f"{prefix}/{record_name}", bundle.record_bytes),
                    (f"{prefix}/prompt.txt", bundle.prompt_bytes),
                    (f"{prefix}/stdout.jsonl", bundle.stdout_bytes),
                    (f"{prefix}/stderr.txt", bundle.stderr_bytes),
                )
            )
            if bundle.prediction_bytes is not None:
                frames.append(
                    (f"{prefix}/prediction.json", bundle.prediction_bytes)
                )
    return frames


def _snapshot_ledger_sha256(
    terminal_records: Mapping[str, bytes],
    attempt_bundles: Mapping[str, Sequence[_RawBundle]],
    incident_bundles: Mapping[str, Sequence[_RawBundle]],
    invocation_bundles: Sequence[_RawInvocation],
) -> str:
    frames = [
        (f"terminal/{sample_id}", payload)
        for sample_id, payload in terminal_records.items()
    ]
    frames.extend(_bundle_frames("attempt", attempt_bundles))
    frames.extend(_bundle_frames("incident", incident_bundles))
    for bundle in invocation_bundles:
        frames.extend(
            (
                (f"invocation/{bundle.number:04d}/start.json", bundle.start_bytes),
                (f"invocation/{bundle.number:04d}/end.json", bundle.end_bytes),
            )
        )
    return _framed_sha256("cofactor9.1.ledger-composite.v1", frames)


def _snapshot_provenance_sha256(
    manifest_bytes: bytes,
    artifact_bytes: Mapping[str, bytes],
    implementation_sha256: Mapping[str, str],
    ledger_composite_sha256: str,
) -> str:
    frames = [("manifest/manifest.json", manifest_bytes)]
    frames.extend(
        (f"artifact/{name}", payload)
        for name, payload in artifact_bytes.items()
    )
    frames.extend(
        (f"implementation/{name}.sha256", digest.encode("ascii"))
        for name, digest in implementation_sha256.items()
    )
    frames.append(
        ("ledger/composite.sha256", ledger_composite_sha256.encode("ascii"))
    )
    return _framed_sha256("cofactor9.1.scoring-provenance.v1", frames)


def _incident_composite_sha256(
    bundles: Mapping[str, Sequence[_RawBundle]],
) -> str:
    return _framed_sha256(
        "cofactor9.1.incident-composite.v1",
        _bundle_frames("incident", bundles),
    )


def _validate_artifact_manifests(
    artifacts: Mapping[str, bytes],
    hashes: Mapping[str, str],
    *,
    expected_case_count: int,
) -> None:
    case_manifest = _mapping(
        _decode_json(artifacts["case_manifest"], location="case manifest"),
        location="case manifest",
    )
    if (
        case_manifest.get("schema_version") != "cofactor9.1.case-artifacts.v1"
        or case_manifest.get("dataset_version") != _DATASET_VERSION
        or case_manifest.get("view_rule_version") != _VIEW_RULE_VERSION
        or case_manifest.get("prompt_version") != PROMPT_VERSION
    ):
        raise ReportingError("case manifest version contract differs")
    case_output_hashes = _mapping(
        case_manifest.get("output_sha256"), location="case manifest output_sha256"
    )
    if (
        case_output_hashes.get("prompt_cases") != hashes["public_cases"]
        or case_output_hashes.get("private_mapping") != hashes["private_mapping"]
    ):
        raise ReportingError("case manifest output SHA256 differs from snapshot")
    case_input_hashes = _mapping(
        case_manifest.get("input_sha256"), location="case manifest input_sha256"
    )
    if (
        case_input_hashes.get("full_structured") != hashes["full_structured"]
        or case_input_hashes.get("label_catalog") != hashes["label_catalog"]
    ):
        raise ReportingError("case manifest input SHA256 differs from snapshot")
    counts = _mapping(case_manifest.get("counts"), location="case manifest counts")
    for key in (
        "prompt_cases",
        "private_mappings",
        "unique_sample_ids",
        "unique_accessions",
    ):
        if counts.get(key) != expected_case_count:
            raise ReportingError(f"case manifest {key} differs from expected count")

    view_audit = _mapping(
        _decode_json(artifacts["view_audit"], location="view audit"),
        location="view audit",
    )
    if (
        view_audit.get("schema_version") != "cofactor9.1.view-audit.v1"
        or view_audit.get("dataset_version") != _DATASET_VERSION
        or view_audit.get("rule_version") != _VIEW_RULE_VERSION
    ):
        raise ReportingError("view audit version contract differs")
    view_outputs = _mapping(
        view_audit.get("output_artifact_sha256"),
        location="view audit output_artifact_sha256",
    )
    for name in (
        "full_structured",
        "core_provisional",
        "label_catalog",
        "ontology_audit",
    ):
        if view_outputs.get(name) != hashes[name]:
            raise ReportingError(f"view audit {name} SHA256 differs from snapshot")

    cluster_manifest = _mapping(
        _decode_json(
            artifacts["homology_clusters_manifest"],
            location="homology cluster manifest",
        ),
        location="homology cluster manifest",
    )
    if (
        cluster_manifest.get("schema_version")
        != "cofactor9.1.homology-cluster-manifest.v1"
        or cluster_manifest.get("dataset_version") != _DATASET_VERSION
        or cluster_manifest.get("view_rule_version") != _VIEW_RULE_VERSION
        or cluster_manifest.get("cluster_record_schema_version")
        != CLUSTER_RECORD_SCHEMA_VERSION
    ):
        raise ReportingError("homology cluster manifest version contract differs")
    cluster_inputs = _mapping(
        cluster_manifest.get("input_sha256"),
        location="homology cluster manifest input_sha256",
    )
    cluster_outputs = _mapping(
        cluster_manifest.get("output_sha256"),
        location="homology cluster manifest output_sha256",
    )
    if cluster_inputs.get("full_structured") != hashes["full_structured"]:
        raise ReportingError("homology cluster manifest Full SHA256 differs")
    if cluster_outputs.get("homology_clusters") != hashes["homology_clusters"]:
        raise ReportingError("homology cluster manifest output SHA256 differs")
    cluster_counts = _mapping(
        cluster_manifest.get("counts"), location="homology cluster manifest counts"
    )
    for key in ("input_records", "output_records", "unique_accessions"):
        if cluster_counts.get(key) != expected_case_count:
            raise ReportingError(
                f"homology cluster manifest {key} differs from expected count"
            )


def _validate_verified_snapshot(
    snapshot: VerifiedRunSnapshotLike,
    *,
    expected_case_count: int,
    formal: bool,
) -> _VerifiedInputs:
    if snapshot is None:
        raise TypeError("verified_run_snapshot is required")
    try:
        run_id = _safe_run_id(snapshot.run_id, location="snapshot run_id")
        manifest_bytes = snapshot.manifest_bytes
        declared_manifest_hash = snapshot.manifest_sha256
        raw_artifact_bytes = snapshot.artifact_bytes
        raw_artifact_hashes = snapshot.artifact_sha256
        raw_implementation_hashes = snapshot.evaluation_implementation_sha256
        raw_terminals = snapshot.terminal_records
        raw_attempts = snapshot.attempt_bundles
        raw_incidents = snapshot.incident_bundles
        raw_invocations = snapshot.invocation_bundles
        declared_ledger_hash = snapshot.ledger_composite_sha256
        declared_provenance_hash = snapshot.provenance_composite_sha256
        validation = snapshot.validation
    except AttributeError as error:
        raise TypeError("verified_run_snapshot does not satisfy its contract") from error
    if not isinstance(manifest_bytes, bytes):
        raise TypeError("snapshot manifest_bytes must be bytes")
    _string(
        declared_manifest_hash,
        location="snapshot manifest SHA256",
        pattern=_SHA256,
    )
    if _sha256(manifest_bytes) != declared_manifest_hash:
        raise ReportingError("snapshot manifest SHA256 differs from manifest bytes")

    artifact_bytes = _copy_bytes_mapping(
        raw_artifact_bytes,
        location="snapshot artifact_bytes",
        exact_keys=_EVALUATION_ARTIFACT_KEYS,
    )
    artifact_hashes = _copy_sha256_mapping(
        raw_artifact_hashes,
        location="snapshot artifact_sha256",
        exact_keys=_EVALUATION_ARTIFACT_KEYS,
    )
    for name, payload in artifact_bytes.items():
        if _sha256(payload) != artifact_hashes[name]:
            raise ReportingError(f"artifact {name} SHA256 differs from snapshot")
    implementation_hashes = _copy_sha256_mapping(
        raw_implementation_hashes,
        location="snapshot evaluation implementation SHA256",
        exact_keys=_EVALUATION_IMPLEMENTATION_KEYS,
    )
    terminal_records = _copy_bytes_mapping(
        raw_terminals, location="snapshot terminal_records"
    )
    attempt_bundles = _copy_bundle_mapping(
        raw_attempts, location="snapshot attempt_bundles"
    )
    incident_bundles = _copy_bundle_mapping(
        raw_incidents, location="snapshot incident_bundles"
    )
    invocation_bundles = _copy_invocations(raw_invocations)

    manifest = _mapping(
        _decode_json(manifest_bytes, location="verified run manifest"),
        location="verified run manifest",
    )
    if manifest.get("schema_version") != RUN_MANIFEST_SCHEMA_VERSION:
        raise ReportingError("verified run manifest schema_version is unsupported")
    if manifest.get("run_id") != run_id:
        raise ReportingError("verified run manifest run_id differs from snapshot")
    if manifest.get("dataset_version") != _DATASET_VERSION:
        raise ReportingError("verified run manifest dataset_version differs")
    _timestamp(manifest.get("created_at"), location="run manifest created_at")
    mode = manifest.get("mode")
    if mode not in {"formal", "infrastructure_gate"}:
        raise ReportingError("verified run manifest mode is unsupported")
    if formal and mode != "formal":
        raise ReportingError("formal reporting requires a formal run manifest")
    contract = dict(_mapping(manifest.get("contract"), location="run manifest contract"))
    contract_hash = _string(
        manifest.get("contract_sha256"),
        location="run manifest contract_sha256",
        pattern=_SHA256,
    )
    if contract_hash != _sha256(_canonical_json_bytes(contract)):
        raise ReportingError("run manifest contract SHA256 differs")

    model = _mapping(contract.get("model"), location="run manifest model")
    binary = _mapping(
        contract.get("codex_binary"), location="run manifest codex_binary"
    )
    _string(binary.get("sha256"), location="Codex binary SHA256", pattern=_SHA256)
    _string(binary.get("version"), location="Codex binary version")
    transport = _mapping(
        contract.get("transport"), location="run manifest transport"
    )
    transport_kind = transport.get("kind")
    if transport_kind == "codex_cli_chatgpt_oauth":
        expected_runtime = (MODEL, REASONING_EFFORT, SERVICE_TIER)
    elif transport_kind == "deepseek_official_api":
        expected_runtime = (
            DEEPSEEK_MODEL,
            DEEPSEEK_REASONING_EFFORT,
            DEEPSEEK_SERVICE_TIER,
        )
        expected_transport = {
            "provider": "deepseek-official",
            "endpoint": DEEPSEEK_API_ENDPOINT,
            "max_output_tokens": DEEPSEEK_MAX_OUTPUT_TOKENS,
            "thinking": {"type": DEEPSEEK_THINKING_TYPE},
            "response_format": {"type": "json_object"},
            "credential_environment_name": "DEEPSEEK_API_KEY",
            "internal_http_retries": 0,
        }
        if any(
            transport.get(key) != value
            for key, value in expected_transport.items()
        ):
            raise ReportingError(
                "run manifest DeepSeek transport differs from scorer contract"
            )
    else:
        raise ReportingError("run manifest transport is unsupported")
    expected_model = {
        "name": expected_runtime[0],
        "reasoning_effort": expected_runtime[1],
        "service_tier": expected_runtime[2],
        "prompt_version": PROMPT_VERSION,
        "response_schema_version": "cofactor9.1.response.v2",
    }
    if any(model.get(key) != value for key, value in expected_model.items()):
        raise ReportingError("run manifest model settings differ from scorer contract")
    max_attempts = _positive_int(
        transport.get("max_attempts"), location="run manifest max_attempts"
    )

    cases_contract = _mapping(
        contract.get("cases"), location="run manifest cases"
    )
    if (
        cases_contract.get("sha256") != artifact_hashes["public_cases"]
        or cases_contract.get("manifest_sha256") != artifact_hashes["case_manifest"]
        or cases_contract.get("selected_count") != expected_case_count
        or cases_contract.get("expected_formal_count") != expected_case_count
    ):
        raise ReportingError("run manifest cases contract differs from snapshot")

    version_mapping = _mapping(
        contract.get("evaluation_versions"),
        location="run manifest evaluation_versions",
    )
    expected_versions = {
        "dataset_version": _DATASET_VERSION,
        "view_rule_version": _VIEW_RULE_VERSION,
        "formula_rule_version": _FORMULA_RULE_VERSION,
        "prompt_version": PROMPT_VERSION,
        "catalog_version": str(cases_contract.get("catalog_version")),
        "response_schema_version": "cofactor9.1.response.v2",
    }
    if set(version_mapping) != set(expected_versions) or any(
        version_mapping.get(key) != value for key, value in expected_versions.items()
    ):
        raise ReportingError("run manifest evaluation_versions differ from scorer contract")
    versions = {key: str(value) for key, value in version_mapping.items()}

    manifest_implementations = _mapping(
        contract.get("evaluation_implementations"),
        location="run manifest evaluation_implementations",
    )
    if frozenset(manifest_implementations) != _EVALUATION_IMPLEMENTATION_KEYS:
        raise ReportingError("run manifest evaluation implementation keys differ")
    for name, digest in implementation_hashes.items():
        entry = _mapping(
            manifest_implementations.get(name),
            location=f"run manifest implementation {name}",
        )
        if entry.get("sha256") != digest:
            raise ReportingError(
                f"evaluation implementation {name} differs from run manifest"
            )

    manifest_artifacts = _mapping(
        contract.get("evaluation_artifacts"),
        location="run manifest evaluation_artifacts",
    )
    if frozenset(manifest_artifacts) != _EVALUATION_ARTIFACT_KEYS:
        raise ReportingError("run manifest evaluation artifact keys differ")
    for name, digest in artifact_hashes.items():
        entry = _mapping(
            manifest_artifacts.get(name),
            location=f"run manifest artifact {name}",
        )
        if entry.get("sha256") != digest:
            raise ReportingError(f"evaluation artifact {name} differs from run manifest")
    private_entry = _mapping(
        manifest_artifacts.get("private_mapping"),
        location="run manifest private mapping artifact",
    )
    if private_entry.get("visibility") != "private-hash-only" or "content" in private_entry:
        raise ReportingError("run manifest private mapping disclosure policy differs")

    _validate_artifact_manifests(
        artifact_bytes,
        artifact_hashes,
        expected_case_count=expected_case_count,
    )

    for sample_id, bundles in attempt_bundles.items():
        if len(bundles) > max_attempts:
            raise ReportingError(
                f"attempt ledger {sample_id} exceeds manifest max_attempts"
            )
    ledger_hash = _string(
        declared_ledger_hash,
        location="snapshot ledger composite SHA256",
        pattern=_SHA256,
    )
    observed_ledger_hash = _snapshot_ledger_sha256(
        terminal_records,
        attempt_bundles,
        incident_bundles,
        invocation_bundles,
    )
    if ledger_hash != observed_ledger_hash:
        raise ReportingError("snapshot ledger composite SHA256 differs from byte bundles")
    provenance_hash = _string(
        declared_provenance_hash,
        location="snapshot provenance composite SHA256",
        pattern=_SHA256,
    )
    if provenance_hash != _snapshot_provenance_sha256(
        manifest_bytes,
        artifact_bytes,
        implementation_hashes,
        ledger_hash,
    ):
        raise ReportingError(
            "snapshot provenance composite SHA256 differs from frozen inputs"
        )

    validation_checks = {
        "run_id": run_id,
        "manifest_sha256": declared_manifest_hash,
        "selected_case_count": expected_case_count,
        "terminal_count": len(terminal_records),
        "missing_terminal_count": expected_case_count - len(terminal_records),
        "attempt_count": sum(len(items) for items in attempt_bundles.values()),
        "incident_count": sum(len(items) for items in incident_bundles.values()),
        "invocation_count": len(invocation_bundles),
        "incident_composite_sha256": _incident_composite_sha256(
            incident_bundles
        ),
        "ledger_composite_sha256": ledger_hash,
    }
    for field, expected in validation_checks.items():
        if getattr(validation, field, None) != expected:
            raise ReportingError(
                f"snapshot validation.{field} differs from immutable bytes"
            )

    return _VerifiedInputs(
        run_id=run_id,
        manifest=dict(manifest),
        manifest_bytes=manifest_bytes,
        manifest_sha256=declared_manifest_hash,
        contract=contract,
        versions=versions,
        implementation_sha256=implementation_hashes,
        artifact_bytes=artifact_bytes,
        artifact_sha256=artifact_hashes,
        terminal_records=terminal_records,
        attempt_bundles=attempt_bundles,
        incident_bundles=incident_bundles,
        invocation_bundles=invocation_bundles,
        ledger_composite_sha256=ledger_hash,
        provenance_composite_sha256=provenance_hash,
    )


def _ratio(numerator: float, denominator: float) -> float:
    return float(numerator / denominator) if denominator else 0.0


def _f1(precision: float, recall: float) -> float:
    return 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0


def _parse_catalog(value: bytes) -> _Catalog:
    decoded = _decode_json(value, location="label catalog")
    document = _mapping(decoded, location="label catalog")
    if document.get("schema_version") != _CATALOG_SCHEMA_VERSION:
        raise ReportingError("label catalog schema_version is unsupported")
    if document.get("dataset_version") != _DATASET_VERSION:
        raise ReportingError("label catalog dataset_version is unsupported")
    if document.get("rule_version") != _VIEW_RULE_VERSION:
        raise ReportingError("label catalog rule_version is unsupported")
    version = _string(document.get("catalog_version"), location="catalog_version")
    labels = document.get("labels")
    if not isinstance(labels, list) or len(labels) != CATALOG_SIZE:
        raise ReportingError(f"label catalog must contain exactly {CATALOG_SIZE} labels")
    terms: list[CatalogTerm] = []
    bands: dict[str, str] = {}
    counts = Counter()
    for index, raw in enumerate(labels):
        item = _mapping(raw, location=f"label catalog item {index}")
        chebi_id = _string(item.get("chebi_id"), location=f"catalog label {index}.chebi_id")
        name = _string(item.get("name"), location=f"catalog label {index}.name")
        band = item.get("frequency_band")
        count = item.get("master_accession_count")
        if band not in {"head", "mid", "tail"}:
            raise ReportingError(f"catalog label {chebi_id} has an invalid frequency band")
        accession_count = _positive_int(count, location=f"catalog label {chebi_id} count")
        expected_band = "head" if accession_count >= 100 else "mid" if accession_count >= 10 else "tail"
        if band != expected_band:
            raise ReportingError(f"catalog label {chebi_id} frequency band contradicts count")
        try:
            term = CatalogTerm(chebi_id, name)
        except ValueError as error:
            raise ReportingError(f"catalog label {index} is invalid: {error}") from error
        terms.append(term)
        bands[chebi_id] = band
        counts[band] += 1
    try:
        PromptCase(
            sample_id="sample_00000000000000000000000000000000",
            sequence="M",
            catalog_version=version,
            catalog_terms=tuple(terms),
        )
    except ValueError as error:
        raise ReportingError(f"label catalog ordering/content is invalid: {error}") from error
    summary = _mapping(document.get("summary"), location="label catalog.summary")
    if summary.get("label_count") != CATALOG_SIZE:
        raise ReportingError("label catalog summary label_count is inconsistent")
    stated_bands = _mapping(summary.get("frequency_band_counts"), location="catalog band counts")
    if dict(stated_bands) != {band: counts[band] for band in ("head", "mid", "tail")}:
        raise ReportingError("label catalog summary frequency bands are inconsistent")
    return _Catalog(version, tuple(terms), bands)


def _parse_public_cases(value: bytes, catalog: _Catalog, expected_count: int) -> tuple[PromptCase, ...]:
    records = _decode_jsonl(value, location="public prompt cases")
    if len(records) != expected_count:
        raise ReportingError(
            f"public prompt cases require {expected_count} records; observed {len(records)}"
        )
    cases: list[PromptCase] = []
    seen: set[str] = set()
    for index, raw in enumerate(records):
        try:
            case = PromptCase.from_payload(raw)
        except ValueError as error:
            raise ReportingError(f"public case {index + 1} is invalid: {error}") from error
        if case.sample_id in seen:
            raise ReportingError(f"duplicate public sample_id {case.sample_id!r}")
        seen.add(case.sample_id)
        if case.catalog_version != catalog.version or case.catalog_terms != catalog.terms:
            raise ReportingError("public case catalog differs from frozen label catalog")
        cases.append(case)
    return tuple(cases)


def _parse_private_links(value: bytes, cases: Sequence[PromptCase], expected_count: int) -> tuple[_PrivateLink, ...]:
    records = _decode_jsonl(value, location="private sample mapping")
    if len(records) != expected_count:
        raise ReportingError(
            f"private sample mapping requires {expected_count} records; observed {len(records)}"
        )
    by_case = {case.sample_id: case for case in cases}
    links: list[_PrivateLink] = []
    sample_ids: set[str] = set()
    accessions: set[str] = set()
    for index, raw in enumerate(records):
        _exact_fields(raw, _PRIVATE_FIELDS, location=f"private mapping {index + 1}")
        if raw.get("schema_version") != PRIVATE_MAPPING_SCHEMA_VERSION:
            raise ReportingError("private mapping schema_version is unsupported")
        if raw.get("visibility") != "private" or raw.get("purpose") != PRIVATE_MAPPING_PURPOSE:
            raise ReportingError("private mapping visibility/purpose contract is invalid")
        sample_id = _string(raw.get("sample_id"), location="private sample_id", pattern=_SAMPLE_ID)
        accession = _string(raw.get("accession"), location="private accession", pattern=_ACCESSION)
        sequence_hash = _string(raw.get("sequence_sha256"), location="private sequence SHA256", pattern=_SHA256)
        if sample_id in sample_ids or accession in accessions:
            raise ReportingError("private mapping sample IDs and accessions must be unique")
        sample_ids.add(sample_id)
        accessions.add(accession)
        case = by_case.get(sample_id)
        if case is None:
            raise ReportingError(f"private mapping has unknown sample_id {sample_id!r}")
        observed_hash = hashlib.sha256(case.sequence.encode("ascii")).hexdigest()
        if sequence_hash != observed_hash:
            raise ReportingError(f"private mapping sequence SHA256 differs for {sample_id}")
        links.append(_PrivateLink(sample_id, accession, sequence_hash))
    if sample_ids != set(by_case):
        raise ReportingError("private mapping does not close exactly over public cases")
    return tuple(links)


def _parse_gold_formula(
    value: object,
    *,
    location: str,
    allowed_labels: frozenset[str],
) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list) or not value:
        raise ReportingError(f"{location} must be a nonempty list of OR blocks")
    blocks: list[tuple[str, ...]] = []
    for block_index, raw_block in enumerate(value):
        if not isinstance(raw_block, list) or not raw_block:
            raise ReportingError(f"{location} block {block_index} must be nonempty")
        if any(not isinstance(label, str) or label not in allowed_labels for label in raw_block):
            raise ReportingError(f"{location} block {block_index} contains an unknown label")
        labels = tuple(raw_block)
        numeric_labels = tuple(
            sorted(set(labels), key=lambda label: int(label.split(":", 1)[1]))
        )
        if labels != numeric_labels:
            raise ReportingError(f"{location} block {block_index} is not canonical")
        blocks.append(labels)
    canonical = tuple(
        sorted(
            blocks,
            key=lambda block: tuple(
                int(label.split(":", 1)[1]) for label in block
            ),
        )
    )
    if tuple(blocks) != canonical:
        raise ReportingError(f"{location} blocks are not canonically sorted")
    return canonical


def _formula_overlap_pair_count(blocks: Sequence[Sequence[str]]) -> int:
    label_sets = [set(block) for block in blocks]
    return sum(
        bool(left & right)
        for index, left in enumerate(label_sets)
        for right in label_sets[index + 1 :]
    )


def _parse_gold_records(
    value: bytes,
    catalog: _Catalog,
    *,
    location: str,
    expected_count: int | None,
) -> tuple[_GoldRecord, ...]:
    records = _decode_jsonl(value, location=location)
    if expected_count is not None and len(records) != expected_count:
        raise ReportingError(
            f"{location} requires {expected_count} records; observed {len(records)}"
        )
    allowed = frozenset(catalog.labels)
    result: list[_GoldRecord] = []
    accessions: set[str] = set()
    for index, raw in enumerate(records):
        item_location = f"{location} record {index + 1}"
        if raw.get("schema_version") != _VIEW_SCHEMA_VERSION:
            raise ReportingError(f"{item_location} schema_version is unsupported")
        if raw.get("dataset_version") != _DATASET_VERSION:
            raise ReportingError(f"{item_location} dataset_version is unsupported")
        derivation = _mapping(raw.get("derivation"), location=f"{item_location}.derivation")
        if derivation.get("rule_version") != _VIEW_RULE_VERSION:
            raise ReportingError(f"{item_location} rule_version is unsupported")
        entry = _mapping(raw.get("entry"), location=f"{item_location}.entry")
        accession = _string(entry.get("accession"), location=f"{item_location} accession", pattern=_ACCESSION)
        if accession in accessions:
            raise ReportingError(f"{location} contains duplicate accession {accession!r}")
        accessions.add(accession)
        sequence = _mapping(raw.get("sequence"), location=f"{item_location}.sequence")
        sequence_value = _string(sequence.get("value"), location=f"{item_location} sequence")
        try:
            observed_sha = hashlib.sha256(sequence_value.encode("ascii")).hexdigest()
        except UnicodeEncodeError as error:
            raise ReportingError(f"{item_location} sequence must be ASCII") from error
        sequence_sha = _string(
            sequence.get("sha256"),
            location=f"{item_location} sequence SHA256",
            pattern=_SHA256,
        )
        if observed_sha != sequence_sha:
            raise ReportingError(f"{item_location} sequence SHA256 differs from value")
        derived = _mapping(raw.get("derived"), location=f"{item_location}.derived")
        blocks = _parse_gold_formula(
            derived.get("gold_formula"),
            location=f"{item_location}.gold_formula",
            allowed_labels=allowed,
        )
        experimental = derived.get("experimental_label_ids")
        if (
            not isinstance(experimental, list)
            or any(not isinstance(label, str) or label not in allowed for label in experimental)
            or experimental
            != sorted(
                set(experimental),
                key=lambda label: int(label.split(":", 1)[1]),
            )
        ):
            raise ReportingError(
                f"{item_location} experimental labels are not a canonical catalog projection"
            )
        formula_labels = {label for block in blocks for label in block}
        if formula_labels != set(experimental):
            raise ReportingError(
                f"{item_location} gold formula label union differs from "
                "experimental labels"
            )
        overlap_pair_count = _formula_overlap_pair_count(blocks)
        reason_codes = derived.get("reason_codes")
        if (
            not isinstance(reason_codes, list)
            or any(not isinstance(reason, str) for reason in reason_codes)
        ):
            raise ReportingError(f"{item_location} reason_codes are invalid")
        has_overlap_reason = "OVERLAPPING_BLOCK_LABEL" in reason_codes
        if has_overlap_reason != bool(overlap_pair_count):
            raise ReportingError(
                f"{item_location} OVERLAPPING_BLOCK_LABEL reason contradicts "
                "gold_formula"
            )
        exact = _mapping(derived.get("exact_sequence"), location=f"{item_location}.exact_sequence")
        entity_id = _string(
            exact.get("sequence_entity_id"),
            location=f"{item_location} sequence_entity_id",
            pattern=_SHA256,
        )
        if entity_id != sequence_sha:
            raise ReportingError(f"{item_location} sequence_entity_id differs from sequence SHA256")
        status = exact.get("status")
        if status not in _EXACT_SEQUENCE_STATUSES:
            raise ReportingError(f"{item_location} exact-sequence status is invalid")
        raw_members = exact.get("members")
        if (
            not isinstance(raw_members, list)
            or not raw_members
            or any(not isinstance(member, str) or _ACCESSION.fullmatch(member) is None for member in raw_members)
            or raw_members != sorted(set(raw_members))
        ):
            raise ReportingError(f"{item_location} exact-sequence members are invalid")
        memberships = _mapping(derived.get("view_membership"), location=f"{item_location}.view_membership")
        full_membership = _mapping(memberships.get("full_structured"), location=f"{item_location}.full membership")
        if full_membership.get("included") is not True:
            raise ReportingError(f"{item_location} is not marked Full-Structured")
        core_membership = _mapping(memberships.get("core_provisional"), location=f"{item_location}.core membership")
        if not isinstance(core_membership.get("included"), bool):
            raise ReportingError(f"{item_location} core membership flag is invalid")
        result.append(
            _GoldRecord(
                accession=accession,
                sequence=sequence_value,
                sequence_sha256=sequence_sha,
                sequence_entity_id=entity_id,
                exact_sequence_status=str(status),
                exact_sequence_members=tuple(raw_members),
                gold_blocks=blocks,
                overlapping_block_pair_count=overlap_pair_count,
                core_included=bool(core_membership.get("included")),
            )
        )
    return tuple(result)


def _validate_exact_sequence_groups(records: Sequence[_GoldRecord]) -> tuple[int, int]:
    by_entity: dict[str, list[_GoldRecord]] = defaultdict(list)
    for record in records:
        by_entity[record.sequence_entity_id].append(record)
    conflict_groups = 0
    conflict_accessions = 0
    for entity_id, members in by_entity.items():
        accessions = tuple(sorted(record.accession for record in members))
        formulas = {record.gold_blocks for record in members}
        expected_status = (
            "UNIQUE"
            if len(members) == 1
            else "DUPLICATE_CONSISTENT"
            if len(formulas) == 1
            else "DUPLICATE_CONFLICT"
        )
        for record in members:
            if record.exact_sequence_members != accessions:
                raise ReportingError(
                    f"exact-sequence members differ from observed entity {entity_id}"
                )
            if record.exact_sequence_status != expected_status:
                raise ReportingError(
                    f"exact-sequence status contradicts formulas for entity {entity_id}"
                )
        if expected_status == "DUPLICATE_CONFLICT":
            conflict_groups += 1
            conflict_accessions += len(members)
    return conflict_groups, conflict_accessions


def _validate_full_mapping(
    records: Sequence[_GoldRecord],
    links: Sequence[_PrivateLink],
) -> dict[str, _PrivateLink]:
    by_accession = {link.accession: link for link in links}
    if set(by_accession) != {record.accession for record in records}:
        raise ReportingError("private mapping accessions do not close exactly over Full-Structured")
    for record in records:
        link = by_accession[record.accession]
        if link.sequence_sha256 != record.sequence_sha256:
            raise ReportingError(
                f"private/public/Full sequence SHA256 differs for accession {record.accession}"
            )
    return by_accession


def _validate_core_records(
    core_records: Sequence[_GoldRecord],
    full_records: Sequence[_GoldRecord],
    *,
    frozen: bool,
) -> None:
    full = {record.accession: record for record in full_records}
    if frozen and len(core_records) != 3_233:
        raise ReportingError(f"frozen Core-Provisional requires 3233 records; observed {len(core_records)}")
    for record in core_records:
        source = full.get(record.accession)
        if source is None:
            raise ReportingError(f"Core accession {record.accession} is absent from Full-Structured")
        if not source.core_included or not record.core_included:
            raise ReportingError(f"Core accession {record.accession} lacks explicit membership")
        if (
            record.sequence_sha256 != source.sequence_sha256
            or record.gold_blocks != source.gold_blocks
            or record.sequence_entity_id != source.sequence_entity_id
        ):
            raise ReportingError(f"Core accession {record.accession} differs from Full-Structured")
        if len(record.gold_blocks) != 1 or len(record.gold_blocks[0]) != 1:
            raise ReportingError(f"Core accession {record.accession} is not single-label")
        if record.conflict:
            raise ReportingError(f"Core accession {record.accession} is an exact-sequence conflict")
        if record.overlapping_blocks:
            raise ReportingError(
                f"Core accession {record.accession} has overlapping gold blocks"
            )


def _parse_ontology(value: bytes, catalog: _Catalog, *, frozen: bool) -> dict[str, dict[str, int]]:
    decoded = _decode_json(value, location="ontology audit")
    document = _mapping(decoded, location="ontology audit")
    if document.get("schema_version") != _ONTOLOGY_SCHEMA_VERSION:
        raise ReportingError("ontology audit schema_version is unsupported")
    if document.get("dataset_version") != _DATASET_VERSION or document.get("rule_version") != _VIEW_RULE_VERSION:
        raise ReportingError("ontology audit dataset/rule version is unsupported")
    if document.get("target_label_count") != CATALOG_SIZE:
        raise ReportingError("ontology audit target_label_count is inconsistent")
    pairs = document.get("pairs")
    if not isinstance(pairs, list):
        raise ReportingError("ontology audit pairs must be a list")
    if frozen and len(pairs) != 59:
        raise ReportingError(f"frozen ontology audit requires 59 pairs; observed {len(pairs)}")
    allowed = frozenset(catalog.labels)
    for pair in pairs:
        item = _mapping(pair, location="ontology pair")
        if item.get("specific") not in allowed or item.get("ancestor") not in allowed:
            raise ReportingError("ontology pair contains a label outside the frozen catalog")
    try:
        return ancestor_distances_from_pairs(pairs)
    except (TypeError, ValueError) as error:
        raise ReportingError(f"ontology audit pairs are invalid: {error}") from error


def _expected_cluster_id(members: Sequence[str]) -> str:
    digest = hashlib.sha256()
    digest.update(CLUSTER_ID_VERSION.encode("ascii") + b"\0")
    for accession in sorted(members):
        digest.update(accession.encode("ascii") + b"\0")
    return f"cluster_{digest.hexdigest()}"


def _parse_clusters(value: bytes, full_records: Sequence[_GoldRecord]) -> dict[str, str]:
    rows = _decode_jsonl(value, location="homology clusters")
    full_accessions = {record.accession for record in full_records}
    if len(rows) != len(full_accessions):
        raise ReportingError(
            f"homology clusters require {len(full_accessions)} rows; observed {len(rows)}"
        )
    observed_order: list[str] = []
    by_accession: dict[str, str] = {}
    declarations: dict[str, tuple[str, int]] = {}
    members_by_cluster: dict[str, list[str]] = defaultdict(list)
    for index, raw in enumerate(rows):
        _exact_fields(raw, _CLUSTER_FIELDS, location=f"homology cluster row {index + 1}")
        if raw.get("schema_version") != CLUSTER_RECORD_SCHEMA_VERSION:
            raise ReportingError("homology cluster schema_version is unsupported")
        accession = _string(raw.get("accession"), location="cluster accession", pattern=_ACCESSION)
        cluster_id = _string(raw.get("cluster_id"), location="cluster_id", pattern=_CLUSTER_ID)
        representative = _string(
            raw.get("representative_accession"),
            location="cluster representative_accession",
            pattern=_ACCESSION,
        )
        member_count = _positive_int(raw.get("member_count"), location="cluster member_count")
        if accession in by_accession:
            raise ReportingError(f"homology clusters duplicate accession {accession!r}")
        observed_order.append(accession)
        by_accession[accession] = cluster_id
        members_by_cluster[cluster_id].append(accession)
        declaration = (representative, member_count)
        prior = declarations.setdefault(cluster_id, declaration)
        if prior != declaration:
            differing = (
                "representative_accession"
                if prior[0] != representative
                else "member_count"
            )
            raise ReportingError(
                f"homology cluster {cluster_id} has inconsistent {differing} declarations"
            )
    if observed_order != sorted(observed_order):
        raise ReportingError("homology cluster rows must be sorted by accession")
    if set(by_accession) != full_accessions:
        raise ReportingError("homology clusters do not close exactly over Full-Structured")
    for cluster_id, members in members_by_cluster.items():
        representative, member_count = declarations[cluster_id]
        if member_count != len(members):
            raise ReportingError(
                f"homology cluster_id {cluster_id} member_count is inconsistent"
            )
        if representative != min(members):
            raise ReportingError(f"homology cluster {cluster_id} representative is not lexical minimum")
        if cluster_id != _expected_cluster_id(members):
            raise ReportingError(f"homology cluster_id is not derived from its complete member set")
    entity_clusters: dict[str, set[str]] = defaultdict(set)
    by_record = {record.accession: record for record in full_records}
    for accession, cluster_id in by_accession.items():
        entity_clusters[by_record[accession].sequence_entity_id].add(cluster_id)
    if any(len(cluster_ids) != 1 for cluster_ids in entity_clusters.values()):
        raise ReportingError("one exact-sequence entity is split across homology clusters")
    return by_accession


def _parse_terminal_records(
    values: Mapping[str, bytes],
    cases: Sequence[PromptCase],
    catalog: _Catalog,
    model_contract: Mapping[str, Any],
) -> dict[str, _Terminal]:
    if not isinstance(values, Mapping):
        raise TypeError("terminal_records must be a mapping of sample_id to bytes")
    by_sample = {case.sample_id: case for case in cases}
    unknown = set(values) - set(by_sample)
    if unknown:
        raise ReportingError(f"terminal ledger contains unknown sample IDs: {sorted(unknown)[:3]}")
    terminals: dict[str, _Terminal] = {}
    for sample_id in sorted(values):
        if not isinstance(sample_id, str) or _SAMPLE_ID.fullmatch(sample_id) is None:
            raise ReportingError("terminal ledger keys must be valid sample IDs")
        decoded = _decode_json(values[sample_id], location=f"terminal {sample_id}")
        raw = _mapping(decoded, location=f"terminal {sample_id}")
        _exact_fields(raw, _TERMINAL_FIELDS, location=f"terminal {sample_id}")
        if raw.get("schema_version") != TERMINAL_SCHEMA_VERSION:
            raise ReportingError(f"terminal {sample_id} schema_version is unsupported")
        if raw.get("sample_id") != sample_id:
            raise ReportingError(f"terminal {sample_id} sample ID differs from ledger key")
        attempt_count = _positive_int(raw.get("attempt_count"), location=f"terminal {sample_id} attempt_count")
        _timestamp(raw.get("completed_at"), location=f"terminal {sample_id} completed_at")
        if (
            raw.get("model") != model_contract.get("name")
            or raw.get("reasoning_effort")
            != model_contract.get("reasoning_effort")
            or raw.get("service_tier") != model_contract.get("service_tier")
            or raw.get("prompt_version") != PROMPT_VERSION
            or raw.get("catalog_version") != catalog.version
        ):
            raise ReportingError(f"terminal {sample_id} model/prompt settings differ from contract")
        case = by_sample[sample_id]
        expected_prompt_hash = hashlib.sha256(render_prompt(case).encode("utf-8")).hexdigest()
        if raw.get("prompt_sha256") != expected_prompt_hash:
            raise ReportingError(f"terminal {sample_id} prompt SHA256 differs from public case")
        status = raw.get("status")
        if status == "success":
            if raw.get("error_code") is not None or raw.get("error_message") is not None:
                raise ReportingError(f"successful terminal {sample_id} carries an error")
            try:
                prediction = validate_prediction(
                    raw.get("prediction"),
                    expected_sample_id=sample_id,
                    allowed_labels=catalog.labels,
                )
            except PredictionValidationError as error:
                raise ReportingError(f"terminal {sample_id} prediction is invalid: {error}") from error
            terminals[sample_id] = _Terminal(sample_id, status, attempt_count, prediction, None)
            continue
        if status == "terminal_error":
            if raw.get("prediction") is not None:
                raise ReportingError(f"error terminal {sample_id} must not contain a prediction")
            error_code = _string(raw.get("error_code"), location=f"terminal {sample_id} error_code", pattern=_ERROR_CODE)
            _string(raw.get("error_message"), location=f"terminal {sample_id} error_message")
            terminals[sample_id] = _Terminal(sample_id, status, attempt_count, None, error_code)
            continue
        raise ReportingError(f"terminal {sample_id} status is unsupported")
    return terminals


def _parse_usage(value: object, *, location: str) -> dict[str, int]:
    usage = _mapping(value, location=location)
    parsed: dict[str, int] = {}
    for key, raw_value in usage.items():
        if not isinstance(key, str) or not key or any(ord(char) < 32 for char in key):
            raise ReportingError(f"{location} has an invalid usage key")
        parsed[key] = _nonnegative_int(raw_value, location=f"{location}.{key}")
    return parsed


def _validate_bundle_file_hashes(
    raw: Mapping[str, Any],
    bundle: _RawBundle,
    *,
    location: str,
) -> None:
    for field, payload in (
        ("prompt_sha256", bundle.prompt_bytes),
        ("stdout_sha256", bundle.stdout_bytes),
        ("stderr_sha256", bundle.stderr_bytes),
    ):
        if raw.get(field) != _sha256(payload):
            raise ReportingError(f"{location} {field} differs from bundle bytes")


def _validate_attempt_bundles(
    bundles: Mapping[str, Sequence[_RawBundle]],
    terminals: Mapping[str, _Terminal],
    cases: Sequence[PromptCase],
) -> None:
    cases_by_sample = {case.sample_id: case for case in cases}
    if set(bundles) != set(terminals):
        raise ReportingError(
            "attempt ledger must close exactly over available terminal records"
        )
    for sample_id, sample_bundles in bundles.items():
        case = cases_by_sample[sample_id]
        expected_prompt = render_prompt(case).encode("utf-8")
        terminal = terminals[sample_id]
        for bundle in sample_bundles:
            location = f"attempt bundle {sample_id}/{bundle.number}"
            if bundle.prompt_bytes != expected_prompt:
                raise ReportingError(f"{location} prompt differs from public case")
            raw = _mapping(
                _decode_json(bundle.record_bytes, location=location),
                location=location,
            )
            _validate_bundle_file_hashes(raw, bundle, location=location)
            if bundle.number != len(sample_bundles):
                if bundle.prediction_bytes is not None:
                    raise ReportingError(f"{location} has a non-final prediction")
                continue
            if terminal.status == "success":
                if bundle.prediction_bytes is None:
                    raise ReportingError(f"{location} lacks the final prediction bytes")
                prediction = _mapping(
                    _decode_json(
                        bundle.prediction_bytes,
                        location=f"{location} prediction",
                    ),
                    location=f"{location} prediction",
                )
                if terminal.prediction is None or prediction != terminal.prediction.to_dict():
                    raise ReportingError(
                        f"{location} prediction differs from terminal prediction"
                    )
            elif bundle.prediction_bytes is not None:
                raise ReportingError(f"{location} error attempt has prediction bytes")


def _parse_incident_bundles(
    bundles: Mapping[str, Sequence[_RawBundle]],
    cases: Sequence[PromptCase],
    *,
    max_attempts: int,
    model_contract: Mapping[str, Any],
) -> dict[str, tuple[_Incident, ...]]:
    cases_by_sample = {case.sample_id: case for case in cases}
    unknown = set(bundles) - set(cases_by_sample)
    if unknown:
        raise ReportingError(
            f"transport incident ledger contains unknown sample IDs: {sorted(unknown)[:3]}"
        )
    result: dict[str, tuple[_Incident, ...]] = {}
    for sample_id, sample_bundles in bundles.items():
        expected_prompt = render_prompt(cases_by_sample[sample_id]).encode("utf-8")
        incidents: list[_Incident] = []
        for bundle in sample_bundles:
            location = f"transport incident {sample_id}/{bundle.number}"
            raw = _mapping(
                _decode_json(bundle.record_bytes, location=location),
                location=location,
            )
            _exact_fields(raw, _INCIDENT_FIELDS, location=location)
            if raw.get("schema_version") != TRANSPORT_INCIDENT_SCHEMA_VERSION:
                raise ReportingError(f"{location} schema_version is unsupported")
            if (
                raw.get("sample_id") != sample_id
                or raw.get("incident_number") != bundle.number
            ):
                raise ReportingError(f"{location} identity/order is invalid")
            tentative_attempt = _positive_int(
                raw.get("tentative_attempt_number"),
                location=f"{location} tentative_attempt_number",
            )
            if tentative_attempt > max_attempts:
                raise ReportingError(
                    f"{location} tentative_attempt_number exceeds max_attempts"
                )
            started_at = _timestamp(
                raw.get("started_at"), location=f"{location} started_at"
            )
            completed_at = _timestamp(
                raw.get("completed_at"), location=f"{location} completed_at"
            )
            completed_time = datetime.fromisoformat(
                completed_at.removesuffix("Z") + "+00:00"
            )
            started_time = datetime.fromisoformat(
                started_at.removesuffix("Z") + "+00:00"
            )
            if completed_time < started_time:
                raise ReportingError(f"{location} completion precedes its start")
            duration = _finite_nonnegative(
                raw.get("duration_seconds"), location=f"{location} duration"
            )
            argv = raw.get("argv")
            if (
                not isinstance(argv, list)
                or not argv
                or any(not isinstance(item, str) for item in argv)
            ):
                raise ReportingError(f"{location} argv is invalid")
            if (
                raw.get("model") != model_contract.get("name")
                or raw.get("reasoning_effort")
                != model_contract.get("reasoning_effort")
                or raw.get("service_tier") != model_contract.get("service_tier")
                or raw.get("environment_policy") != "fixed-allowlist"
            ):
                raise ReportingError(f"{location} runtime contract differs")
            returncode = raw.get("returncode")
            if returncode is not None and (
                isinstance(returncode, bool) or not isinstance(returncode, int)
            ):
                raise ReportingError(f"{location} returncode is invalid")
            if raw.get("timed_out") is not False:
                raise ReportingError(f"{location} cannot be a timed-out attempt")
            cancelled = raw.get("cancelled")
            start_error = raw.get("start_error")
            error_code = _string(
                raw.get("error_code"),
                location=f"{location} error_code",
                pattern=_ERROR_CODE,
            )
            if error_code not in _INCIDENT_ERROR_CODES:
                raise ReportingError(f"{location} error_code is not an incident code")
            error_message = _string(
                raw.get("error_message"), location=f"{location} error_message"
            )
            expected_disposition = {
                "AUTH_ERROR": "nonretryable",
                "CAPACITY_ERROR": "retryable",
                "TRANSPORT_ERROR": "retryable",
                "PROCESS_START_ERROR": "nonretryable",
                "RUN_CANCELLED": "retryable",
                "INTERRUPTED_ATTEMPT": "retryable",
            }[error_code]
            if raw.get("retry_disposition") != expected_disposition:
                raise ReportingError(
                    f"{location} retry_disposition conflicts with error_code"
                )
            if raw.get("thread_id") is not None:
                raise ReportingError(f"{location} cannot carry a completed thread")
            usage = _parse_usage(raw.get("usage"), location=f"{location}.usage")
            if usage:
                raise ReportingError(f"{location} cannot carry completed model usage")
            _nonnegative_int(
                raw.get("redaction_count"), location=f"{location} redaction_count"
            )
            if bundle.prompt_bytes != expected_prompt:
                raise ReportingError(f"{location} prompt differs from public case")
            _validate_bundle_file_hashes(raw, bundle, location=location)
            prediction_sha256 = raw.get("prediction_sha256")
            if bundle.prediction_bytes is None:
                if prediction_sha256 is not None:
                    raise ReportingError(
                        f"{location} prediction SHA256 exists without prediction bytes"
                    )
            elif prediction_sha256 != _sha256(bundle.prediction_bytes):
                raise ReportingError(
                    f"{location} prediction_sha256 differs from bundle bytes"
                )
            if error_code == "INTERRUPTED_ATTEMPT":
                if (
                    not isinstance(argv, list)
                    or len(argv) != 2
                    or argv[1]
                    not in {
                        "<interrupted-before-capture>",
                        "<outcome-unknown-after-host-interruption>",
                    }
                    or returncode is not None
                    or cancelled is not None
                    or start_error is not None
                    or duration != 0.0
                ):
                    raise ReportingError(
                        f"{location} interrupted process fields conflict"
                    )
            elif error_code == "RUN_CANCELLED":
                if cancelled is not True or start_error is not None:
                    raise ReportingError(
                        f"{location} cancelled process fields conflict"
                    )
            elif error_code == "PROCESS_START_ERROR":
                if (
                    cancelled is not False
                    or returncode is not None
                    or not isinstance(start_error, str)
                    or not start_error
                    or start_error != error_message
                ):
                    raise ReportingError(
                        f"{location} process-start fields conflict"
                    )
            elif cancelled is not False or start_error is not None:
                raise ReportingError(f"{location} process fields conflict")
            if (
                bundle.prediction_bytes is not None
                and error_code != "INTERRUPTED_ATTEMPT"
            ):
                raise ReportingError(
                    f"{location} captured incident unexpectedly carries a prediction"
                )
            incidents.append(
                _Incident(
                    sample_id=sample_id,
                    incident_number=bundle.number,
                    tentative_attempt_number=tentative_attempt,
                    duration_seconds=duration,
                    error_code=error_code,
                    usage=usage,
                )
            )
        result[sample_id] = tuple(incidents)
    return result


def _parse_attempt_records(
    values: Mapping[str, Sequence[bytes]] | None,
    terminals: Mapping[str, _Terminal],
    cases: Sequence[PromptCase],
    model_contract: Mapping[str, Any],
) -> dict[str, tuple[_Attempt, ...]]:
    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise TypeError("attempt_records must be a mapping of sample_id to byte sequences")
    if set(values) != set(terminals):
        raise ReportingError("attempt ledger must close exactly over available terminal records")
    cases_by_sample = {case.sample_id: case for case in cases}
    parsed: dict[str, tuple[_Attempt, ...]] = {}
    for sample_id in sorted(values):
        raw_records = values[sample_id]
        if isinstance(raw_records, (str, bytes)) or not isinstance(raw_records, Sequence):
            raise ReportingError(f"attempt ledger {sample_id} must be a sequence of JSON bytes")
        terminal = terminals[sample_id]
        if len(raw_records) != terminal.attempt_count:
            raise ReportingError(
                f"attempt ledger {sample_id} count differs from terminal attempt_count"
            )
        attempts: list[_Attempt] = []
        expected_prompt_hash = hashlib.sha256(
            render_prompt(cases_by_sample[sample_id]).encode("utf-8")
        ).hexdigest()
        for offset, raw_bytes in enumerate(raw_records, start=1):
            decoded = _decode_json(raw_bytes, location=f"attempt {sample_id}/{offset}")
            raw = _mapping(decoded, location=f"attempt {sample_id}/{offset}")
            _exact_fields(raw, _ATTEMPT_FIELDS, location=f"attempt {sample_id}/{offset}")
            if raw.get("schema_version") != ATTEMPT_SCHEMA_VERSION:
                raise ReportingError(f"attempt {sample_id}/{offset} schema_version is unsupported")
            if raw.get("sample_id") != sample_id or raw.get("attempt_number") != offset:
                raise ReportingError(f"attempt {sample_id}/{offset} identity/order is invalid")
            _timestamp(raw.get("started_at"), location=f"attempt {sample_id}/{offset} started_at")
            _timestamp(raw.get("completed_at"), location=f"attempt {sample_id}/{offset} completed_at")
            duration = _finite_nonnegative(raw.get("duration_seconds"), location=f"attempt {sample_id}/{offset} duration")
            argv = raw.get("argv")
            if not isinstance(argv, list) or not argv or any(not isinstance(item, str) for item in argv):
                raise ReportingError(f"attempt {sample_id}/{offset} argv is invalid")
            if (
                raw.get("model") != model_contract.get("name")
                or raw.get("reasoning_effort")
                != model_contract.get("reasoning_effort")
                or raw.get("service_tier") != model_contract.get("service_tier")
                or raw.get("environment_policy") != "fixed-allowlist"
                or raw.get("prompt_sha256") != expected_prompt_hash
            ):
                raise ReportingError(f"attempt {sample_id}/{offset} runtime contract differs")
            returncode = raw.get("returncode")
            if returncode is not None and (isinstance(returncode, bool) or not isinstance(returncode, int)):
                raise ReportingError(f"attempt {sample_id}/{offset} returncode is invalid")
            if not isinstance(raw.get("timed_out"), bool):
                raise ReportingError(f"attempt {sample_id}/{offset} timed_out is invalid")
            for sha_field in ("stdout_sha256", "stderr_sha256"):
                _string(raw.get(sha_field), location=f"attempt {sample_id}/{offset} {sha_field}", pattern=_SHA256)
            _nonnegative_int(raw.get("redaction_count"), location=f"attempt {sample_id}/{offset} redaction_count")
            retry_disposition = raw.get("retry_disposition")
            if retry_disposition not in {"none", "retryable", "nonretryable"}:
                raise ReportingError(f"attempt {sample_id}/{offset} retry_disposition is invalid")
            error_code_value = raw.get("error_code")
            error_message = raw.get("error_message")
            if error_code_value is None:
                if error_message is not None or retry_disposition != "none":
                    raise ReportingError(f"attempt {sample_id}/{offset} success/error fields conflict")
                error_code = None
            else:
                error_code = _string(error_code_value, location=f"attempt {sample_id}/{offset} error_code", pattern=_ERROR_CODE)
                _string(error_message, location=f"attempt {sample_id}/{offset} error_message")
                if retry_disposition == "none":
                    raise ReportingError(f"attempt {sample_id}/{offset} error lacks retry disposition")
            thread_id = raw.get("thread_id")
            if thread_id is not None:
                _string(thread_id, location=f"attempt {sample_id}/{offset} thread_id")
            usage = _parse_usage(raw.get("usage"), location=f"attempt {sample_id}/{offset}.usage")
            attempts.append(_Attempt(sample_id, offset, duration, error_code, usage))
        if any(item.error_code is None for item in attempts[:-1]):
            raise ReportingError(f"attempt ledger {sample_id} continued after a successful attempt")
        final = attempts[-1]
        if terminal.status == "success" and final.error_code is not None:
            raise ReportingError(f"successful terminal {sample_id} has a failed final attempt")
        if terminal.status == "terminal_error" and final.error_code != terminal.error_code:
            raise ReportingError(f"error terminal {sample_id} differs from final attempt")
        parsed[sample_id] = tuple(attempts)
    return parsed


def _ledger_hash(values: Mapping[str, bytes], *, domain: str) -> str:
    digest = hashlib.sha256()
    digest.update(domain.encode("ascii") + b"\0")
    for sample_id in sorted(values):
        payload = values[sample_id]
        if not isinstance(payload, bytes):
            raise TypeError(f"{domain} values must be bytes")
        digest.update(sample_id.encode("ascii") + b"\0")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _attempt_ledger_hash(values: Mapping[str, Sequence[bytes]] | None) -> str | None:
    if values is None:
        return None
    flattened: dict[str, bytes] = {}
    for sample_id, records in values.items():
        if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
            raise TypeError("attempt_records values must be byte sequences")
        for index, payload in enumerate(records, start=1):
            flattened[f"{sample_id}/attempt-{index:04d}"] = payload
    return _ledger_hash(flattened, domain="cofactor9.1.attempt-ledger.v1")


def _score_gold(
    record: _GoldRecord,
    terminal: _Terminal,
    ancestors: Mapping[str, Mapping[str, int]],
) -> RecordScore:
    prediction = terminal.prediction
    return score_record(
        record.gold_blocks,
        prediction.predicted_cofactors if prediction is not None else (),
        ancestor_distances=ancestors,
        status=prediction.status if prediction is not None else "abstain",
    )


def _weighted_structured(
    records: Sequence[_GoldRecord],
    terminals_by_accession: Mapping[str, _Terminal],
    weights: Mapping[str, float],
    ancestors: Mapping[str, Mapping[str, int]],
) -> tuple[dict[str, Any], tuple[_ScoredRecord, ...]]:
    scored: list[_ScoredRecord] = []
    for record in records:
        terminal = terminals_by_accession.get(record.accession)
        if terminal is None:
            continue
        weight = float(weights.get(record.accession, 0.0))
        if not math.isfinite(weight) or weight < 0.0:
            raise ReportingError(f"invalid scoring weight for {record.accession}")
        scored.append(_ScoredRecord(record, terminal, _score_gold(record, terminal, ancestors), weight))
    effective_weight = math.fsum(item.weight for item in scored)
    exact_weight = math.fsum(item.weight * float(item.score.exact) for item in scored)
    tp = math.fsum(item.weight * item.score.tp for item in scored)
    fp = math.fsum(item.weight * item.score.fp for item in scored)
    fn = math.fsum(item.weight * item.score.fn for item in scored)
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    hierarchy_tp = math.fsum(item.weight * item.score.hierarchy_tp for item in scored)
    hierarchy_fp = math.fsum(item.weight * item.score.hierarchy_fp for item in scored)
    hierarchy_fn = math.fsum(item.weight * item.score.hierarchy_fn for item in scored)
    hierarchy_precision = _ratio(hierarchy_tp, hierarchy_tp + hierarchy_fp)
    hierarchy_recall = _ratio(hierarchy_tp, hierarchy_tp + hierarchy_fn)
    predict_weight = math.fsum(
        item.weight
        for item in scored
        if item.terminal.prediction is not None and item.terminal.prediction.status == "predict"
    )
    abstain_weight = math.fsum(
        item.weight
        for item in scored
        if item.terminal.prediction is not None and item.terminal.prediction.status == "abstain"
    )
    error_weight = math.fsum(
        item.weight for item in scored if item.terminal.status == "terminal_error"
    )
    selective_exact = math.fsum(
        item.weight * float(item.score.exact)
        for item in scored
        if item.terminal.prediction is not None and item.terminal.prediction.status == "predict"
    )
    metrics: dict[str, Any] = {
        "record_count": len(scored),
        "effective_weight": effective_weight,
        "record_exact_weight": exact_weight,
        "record_exact_accuracy": _ratio(exact_weight, effective_weight),
        "weighted_tp": tp,
        "weighted_fp": fp,
        "weighted_fn": fn,
        "block_micro_precision": precision,
        "block_micro_recall": recall,
        "block_micro_f1": _f1(precision, recall),
        "block_coverage": recall,
        "hierarchy_weighted_tp": hierarchy_tp,
        "hierarchy_weighted_fp": hierarchy_fp,
        "hierarchy_weighted_fn": hierarchy_fn,
        "hierarchy_block_micro_precision": hierarchy_precision,
        "hierarchy_block_micro_recall": hierarchy_recall,
        "hierarchy_block_micro_f1": _f1(hierarchy_precision, hierarchy_recall),
        "hierarchy_exact_match_count": sum(
            item.score.hierarchy_exact for item in scored if item.weight > 0.0
        ),
        "hierarchy_under_specific_count": sum(
            item.score.under_specific for item in scored if item.weight > 0.0
        ),
        "hierarchy_over_specific_count": sum(
            item.score.over_specific for item in scored if item.weight > 0.0
        ),
        "predict_status_weight": predict_weight,
        "abstain_status_weight": abstain_weight,
        "terminal_error_weight": error_weight,
        "coverage": _ratio(predict_weight, effective_weight),
        "selective_record_exact_accuracy": _ratio(selective_exact, predict_weight),
    }
    return metrics, tuple(scored)


def _accession_weights(records: Sequence[_GoldRecord]) -> dict[str, float]:
    return {
        record.accession: 0.0 if record.ranking_excluded else 1.0
        for record in records
    }


def _diagnostic_unit_weights(
    records: Sequence[_GoldRecord],
) -> dict[str, float]:
    return {record.accession: 1.0 for record in records}


def _entity_weights(
    records: Sequence[_GoldRecord],
    terminals_by_accession: Mapping[str, _Terminal],
) -> tuple[dict[str, float], int]:
    groups: dict[str, list[_GoldRecord]] = defaultdict(list)
    for record in records:
        if record.accession in terminals_by_accession and not record.ranking_excluded:
            groups[record.sequence_entity_id].append(record)
    weights: dict[str, float] = {record.accession: 0.0 for record in records}
    for members in groups.values():
        per_accession = 1.0 / len(members)
        for record in members:
            weights[record.accession] = per_accession
    return weights, len(groups)


def _cluster_weights(
    records: Sequence[_GoldRecord],
    terminals_by_accession: Mapping[str, _Terminal],
    cluster_by_accession: Mapping[str, str],
) -> tuple[dict[str, float], int]:
    by_cluster_entity: dict[str, dict[str, list[_GoldRecord]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for record in records:
        if record.accession in terminals_by_accession and not record.ranking_excluded:
            by_cluster_entity[cluster_by_accession[record.accession]][
                record.sequence_entity_id
            ].append(record)
    weights: dict[str, float] = {record.accession: 0.0 for record in records}
    for entities in by_cluster_entity.values():
        entity_weight = 1.0 / len(entities)
        for members in entities.values():
            per_accession = entity_weight / len(members)
            for record in members:
                weights[record.accession] = per_accession
    return weights, len(by_cluster_entity)


def _exact_matching(
    blocks: Sequence[Sequence[str]], predictions: Sequence[str]
) -> tuple[tuple[int, int], ...]:
    """Return one deterministic maximum-cardinality exact block matching."""

    ordered_predictions = tuple(sorted(enumerate(predictions), key=lambda item: (item[1], item[0])))
    block_to_prediction: dict[int, int] = {}

    def augment(prediction_index: int, label: str, seen_blocks: set[int]) -> bool:
        for block_index, block in enumerate(blocks):
            if label not in block or block_index in seen_blocks:
                continue
            seen_blocks.add(block_index)
            prior = block_to_prediction.get(block_index)
            if prior is None:
                block_to_prediction[block_index] = prediction_index
                return True
            prior_label = predictions[prior]
            if augment(prior, prior_label, seen_blocks):
                block_to_prediction[block_index] = prediction_index
                return True
        return False

    for prediction_index, label in ordered_predictions:
        augment(prediction_index, label, set())
    return tuple(sorted((prediction_index, block_index) for block_index, prediction_index in block_to_prediction.items()))


def _label_macro(
    scored: Sequence[_ScoredRecord],
    labels: Sequence[str],
    *,
    selected_labels: frozenset[str] | None = None,
) -> dict[str, Any]:
    tp = {label: 0.0 for label in labels}
    fp = {label: 0.0 for label in labels}
    fn = {label: 0.0 for label in labels}
    for item in scored:
        if item.weight <= 0.0:
            continue
        predictions = (
            tuple(item.terminal.prediction.predicted_cofactors)
            if item.terminal.prediction is not None
            else ()
        )
        pairs = _exact_matching(item.gold.gold_blocks, predictions)
        matched_predictions = {prediction_index for prediction_index, _ in pairs}
        matched_blocks = {block_index for _, block_index in pairs}
        for prediction_index, _ in pairs:
            tp[predictions[prediction_index]] += item.weight
        for prediction_index, label in enumerate(predictions):
            if prediction_index not in matched_predictions:
                fp[label] += item.weight
        for block_index, block in enumerate(item.gold.gold_blocks):
            if block_index in matched_blocks:
                continue
            fractional = item.weight / len(block)
            for label in block:
                fn[label] += fractional
    selected = tuple(
        label for label in labels if selected_labels is None or label in selected_labels
    )
    per_label: list[dict[str, Any]] = []
    for label in selected:
        precision = _ratio(tp[label], tp[label] + fp[label])
        recall = _ratio(tp[label], tp[label] + fn[label])
        per_label.append(
            {
                "label": label,
                "weighted_tp": tp[label],
                "weighted_fp": fp[label],
                "weighted_fn": fn[label],
                "precision": precision,
                "recall": recall,
                "f1": _f1(precision, recall),
            }
        )
    count = len(per_label)
    return {
        "policy": "matched-block attribution; unmatched OR-block FN is divided equally among alternatives",
        "label_count": count,
        "macro_precision": _ratio(math.fsum(item["precision"] for item in per_label), count),
        "macro_recall": _ratio(math.fsum(item["recall"] for item in per_label), count),
        "macro_f1": _ratio(math.fsum(item["f1"] for item in per_label), count),
        "per_label": per_label,
    }


def _slice_structured_by_band(
    records: Sequence[_GoldRecord],
    terminals_by_accession: Mapping[str, _Terminal],
    weights: Mapping[str, float],
    ancestors: Mapping[str, Mapping[str, int]],
    catalog: _Catalog,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for band in ("head", "mid", "tail"):
        selected = [
            record
            for record in records
            if any(catalog.bands[label] == band for block in record.gold_blocks for label in block)
        ]
        metrics, _ = _weighted_structured(selected, terminals_by_accession, weights, ancestors)
        metrics["slice_policy"] = "record contains at least one gold alternative in this band; slices may overlap"
        result[band] = metrics
    return result


def _core_metrics(
    records: Sequence[_GoldRecord],
    terminals_by_accession: Mapping[str, _Terminal],
    *,
    labels: Sequence[str] | None = None,
) -> dict[str, Any]:
    selected = [record for record in records if record.accession in terminals_by_accession]
    gold_labels = tuple(sorted(labels or {record.gold_blocks[0][0] for record in selected}))
    stats = {
        label: {"support": 0, "tp": 0, "fp": 0, "fn": 0}
        for label in gold_labels
    }
    correct = 0
    predicted_count = 0
    predict_status_count = 0
    abstain_count = 0
    terminal_error_count = 0
    selective_correct = 0
    confusion: Counter[tuple[str, str]] = Counter()
    for record in selected:
        gold = record.gold_blocks[0][0]
        terminal = terminals_by_accession[record.accession]
        stats[gold]["support"] += 1
        prediction = terminal.prediction
        if prediction is None:
            terminal_error_count += 1
            stats[gold]["fn"] += 1
            continue
        predicted_count += 1
        primary = prediction.primary_guess
        confusion[(gold, primary)] += 1
        is_correct = primary == gold
        correct += int(is_correct)
        stats[gold]["tp"] += int(is_correct)
        stats[gold]["fn"] += int(not is_correct)
        if not is_correct and primary in stats:
            stats[primary]["fp"] += 1
        if prediction.status == "predict":
            predict_status_count += 1
            selective_correct += int(is_correct)
        else:
            abstain_count += 1
    per_class: list[dict[str, Any]] = []
    for label in gold_labels:
        item = stats[label]
        precision = _ratio(item["tp"], item["tp"] + item["fp"])
        recall = _ratio(item["tp"], item["support"])
        per_class.append(
            {
                "label": label,
                **item,
                "precision": precision,
                "recall": recall,
                "f1": _f1(precision, recall),
            }
        )
    label_count = len(per_class)
    return {
        "record_count": len(selected),
        "represented_label_count": label_count,
        "accuracy": _ratio(correct, len(selected)),
        "macro_f1": _ratio(math.fsum(item["f1"] for item in per_class), label_count),
        "balanced_accuracy": _ratio(math.fsum(item["recall"] for item in per_class), label_count),
        "validated_prediction_count": predicted_count,
        "terminal_error_count": terminal_error_count,
        "predict_status_count": predict_status_count,
        "abstention_count": abstain_count,
        "coverage": _ratio(predict_status_count, len(selected)),
        "selective_accuracy": _ratio(selective_correct, predict_status_count),
        "per_class": per_class,
        "confusion_nonzero": [
            {"gold": gold, "predicted": predicted, "count": count}
            for (gold, predicted), count in sorted(confusion.items())
        ],
    }


def _core_by_band(
    records: Sequence[_GoldRecord],
    terminals_by_accession: Mapping[str, _Terminal],
    catalog: _Catalog,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for band in ("head", "mid", "tail"):
        selected = [record for record in records if catalog.bands[record.gold_blocks[0][0]] == band]
        result[band] = _core_metrics(selected, terminals_by_accession)
    return result


def _calibration_dict(
    scored: Sequence[_ScoredRecord],
    *,
    bin_count: int,
) -> dict[str, Any]:
    observations: list[CalibrationObservation] = []
    terminal_errors = 0
    zero_weight_conflict_predictions = 0
    zero_weight_overlap_predictions = 0
    zero_weight_ranking_excluded_predictions = 0
    for item in scored:
        prediction = item.terminal.prediction
        if prediction is None:
            terminal_errors += 1
            continue
        if item.weight == 0.0:
            zero_weight_conflict_predictions += item.gold.conflict
            zero_weight_overlap_predictions += item.gold.overlapping_blocks
            zero_weight_ranking_excluded_predictions += item.gold.ranking_excluded
        observations.append(
            CalibrationObservation(
                sample_id=item.terminal.sample_id,
                confidence=prediction.confidence_complete,
                correct=item.score.exact,
                weight=item.weight,
            )
        )
    metrics = score_calibration(observations, bin_count=bin_count)
    return {
        "observation_count": metrics.observation_count,
        "total_weight": metrics.total_weight,
        "brier_score": metrics.brier_score,
        "expected_calibration_error": metrics.expected_calibration_error,
        "aurc": metrics.aurc,
        "excluded_terminal_error_count": terminal_errors,
        "zero_weight_conflict_prediction_count": zero_weight_conflict_predictions,
        "zero_weight_overlap_prediction_count": zero_weight_overlap_predictions,
        "zero_weight_ranking_excluded_prediction_count": (
            zero_weight_ranking_excluded_predictions
        ),
        "bins": [
            {
                "lower": item.lower,
                "upper": item.upper,
                "observation_count": item.observation_count,
                "weight": item.weight,
                "mean_confidence": item.mean_confidence,
                "accuracy": item.accuracy,
                "absolute_gap": item.absolute_gap,
            }
            for item in metrics.bins
        ],
        "risk_coverage_curve": [
            {
                "confidence_threshold": item.confidence_threshold,
                "coverage": item.coverage,
                "risk": item.risk,
            }
            for item in metrics.risk_coverage_curve
        ],
    }


def _terminal_summary(
    terminals: Mapping[str, _Terminal],
    attempts: Mapping[str, Sequence[_Attempt]],
    *,
    expected_count: int,
) -> dict[str, Any]:
    success = [item for item in terminals.values() if item.status == "success"]
    errors = [item for item in terminals.values() if item.status == "terminal_error"]
    predict = [item for item in success if item.prediction is not None and item.prediction.status == "predict"]
    abstain = [item for item in success if item.prediction is not None and item.prediction.status == "abstain"]
    error_counts = Counter(item.error_code for item in errors)
    parse_failures = sum(error_counts[code] for code in _PARSE_ERROR_CODES)
    all_attempts = [item for values in attempts.values() for item in values]
    attempt_parse_failures = sum(item.error_code in _PARSE_ERROR_CODES for item in all_attempts)
    return {
        "expected_case_count": expected_count,
        "terminal_count": len(terminals),
        "missing_terminal_count": expected_count - len(terminals),
        "terminal_completion_rate": _ratio(len(terminals), expected_count),
        "success_count": len(success),
        "successful_prediction_rate": _ratio(len(success), expected_count),
        "terminal_error_count": len(errors),
        "terminal_error_rate": _ratio(len(errors), expected_count),
        "prediction_parse_failure_count": parse_failures,
        "prediction_parse_failure_rate": _ratio(parse_failures, expected_count),
        "model_predict_count": len(predict),
        "model_abstention_count": len(abstain),
        "model_status_coverage": _ratio(len(predict), expected_count),
        "error_code_counts": {
            str(code): count for code, count in sorted(error_counts.items())
        },
        "attempt_count": len(all_attempts),
        "attempt_prediction_parse_failure_count": attempt_parse_failures,
        "attempt_prediction_parse_failure_rate": _ratio(
            attempt_parse_failures, len(all_attempts)
        ),
    }


def _nearest_rank(values: Sequence[float], proportion: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(proportion * len(ordered)) - 1)
    return float(ordered[index])


def _attempt_resource_summary(attempts: Sequence[_Attempt]) -> dict[str, Any]:
    durations = [item.duration_seconds for item in attempts]
    usage: Counter[str] = Counter()
    for attempt in attempts:
        usage.update(attempt.usage)
    return {
        "attempt_count": len(attempts),
        "total_duration_seconds": math.fsum(durations),
        "mean_duration_seconds": statistics.fmean(durations) if durations else 0.0,
        "median_duration_seconds": float(statistics.median(durations)) if durations else 0.0,
        "p95_duration_seconds": _nearest_rank(durations, 0.95),
        "max_duration_seconds": max(durations, default=0.0),
        "usage_totals": {key: usage[key] for key in sorted(usage)},
    }


def _resource_summary(
    attempts: Mapping[str, Sequence[_Attempt]], terminals: Mapping[str, _Terminal]
) -> dict[str, Any]:
    if not attempts:
        model_attempts = _attempt_resource_summary(())
        return {
            "available": False,
            "all_attempts": model_attempts,
            "model_attempts": dict(model_attempts),
            "successful_final_attempts": _attempt_resource_summary(()),
            "per_case_total_latency": {
                "case_count": 0,
                "total_duration_seconds": 0.0,
                "mean_duration_seconds": 0.0,
                "median_duration_seconds": 0.0,
                "p95_duration_seconds": 0.0,
                "max_duration_seconds": 0.0,
            },
            "retried_case_count": 0,
        }
    all_attempts = [item for values in attempts.values() for item in values]
    successful_final = [
        values[-1]
        for sample_id, values in attempts.items()
        if terminals[sample_id].status == "success"
    ]
    case_durations = [math.fsum(item.duration_seconds for item in values) for values in attempts.values()]
    model_attempts = _attempt_resource_summary(all_attempts)
    return {
        "available": True,
        "all_attempts": model_attempts,
        "model_attempts": dict(model_attempts),
        "successful_final_attempts": _attempt_resource_summary(successful_final),
        "per_case_total_latency": {
            "case_count": len(case_durations),
            "total_duration_seconds": math.fsum(case_durations),
            "mean_duration_seconds": statistics.fmean(case_durations),
            "median_duration_seconds": float(statistics.median(case_durations)),
            "p95_duration_seconds": _nearest_rank(case_durations, 0.95),
            "max_duration_seconds": max(case_durations),
        },
        "retried_case_count": sum(len(values) > 1 for values in attempts.values()),
    }


def _incident_resource_summary(
    incidents: Mapping[str, Sequence[_Incident]],
    bundles: Mapping[str, Sequence[_RawBundle]],
) -> dict[str, Any]:
    all_incidents = [item for values in incidents.values() for item in values]
    durations = [item.duration_seconds for item in all_incidents]
    error_counts = Counter(item.error_code for item in all_incidents)
    usage: Counter[str] = Counter()
    for incident in all_incidents:
        usage.update(incident.usage)
    return {
        "incident_count": len(all_incidents),
        "affected_sample_count": len(incidents),
        "affected_sample_ids": sorted(incidents),
        "error_code_counts": {
            code: error_counts[code] for code in sorted(error_counts)
        },
        "total_duration_seconds": math.fsum(durations),
        "mean_duration_seconds": (
            statistics.fmean(durations) if durations else 0.0
        ),
        "p95_duration_seconds": _nearest_rank(durations, 0.95),
        "usage_totals": {key: usage[key] for key in sorted(usage)},
        "ledger_sha256": _incident_composite_sha256(bundles),
        "composite_sha256": _incident_composite_sha256(bundles),
        "excluded_from_model_attempt_budget": True,
    }


def _conflict_group_details(
    records: Sequence[_GoldRecord],
    terminals_by_accession: Mapping[str, _Terminal],
    links_by_accession: Mapping[str, _PrivateLink],
) -> list[dict[str, Any]]:
    groups: dict[str, list[_GoldRecord]] = defaultdict(list)
    for record in records:
        if record.conflict:
            groups[record.sequence_entity_id].append(record)
    details: list[dict[str, Any]] = []
    for entity_id in sorted(groups):
        members: list[dict[str, Any]] = []
        for record in sorted(groups[entity_id], key=lambda item: item.accession):
            terminal = terminals_by_accession.get(record.accession)
            prediction = terminal.prediction if terminal is not None else None
            members.append(
                {
                    "accession": record.accession,
                    "sample_id": links_by_accession[record.accession].sample_id,
                    "gold_formula": [list(block) for block in record.gold_blocks],
                    "terminal_status": terminal.status if terminal is not None else "missing",
                    "predicted_cofactors": (
                        list(prediction.predicted_cofactors) if prediction is not None else None
                    ),
                    "primary_guess": prediction.primary_guess if prediction is not None else None,
                }
            )
        details.append(
            {
                "sequence_entity_id": entity_id,
                "accession_count": len(members),
                "members": members,
            }
        )
    return details


def _overlap_record_details(
    records: Sequence[_GoldRecord],
    terminals_by_accession: Mapping[str, _Terminal],
    links_by_accession: Mapping[str, _PrivateLink],
) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for record in sorted(records, key=lambda item: item.accession):
        if not record.overlapping_blocks:
            continue
        terminal = terminals_by_accession.get(record.accession)
        prediction = terminal.prediction if terminal is not None else None
        details.append(
            {
                "accession": record.accession,
                "sample_id": links_by_accession[record.accession].sample_id,
                "gold_formula": [list(block) for block in record.gold_blocks],
                "terminal_status": (
                    terminal.status if terminal is not None else "missing"
                ),
                "predicted_cofactors": (
                    list(prediction.predicted_cofactors)
                    if prediction is not None
                    else None
                ),
                "primary_guess": (
                    prediction.primary_guess if prediction is not None else None
                ),
                "ranking_weight": 0.0,
                "reason_code": "OVERLAPPING_BLOCK_LABEL",
            }
        )
    return details


def score_run(
    *,
    verified_run_snapshot: VerifiedRunSnapshotLike,
    formal: bool = True,
    expected_case_count: int = 5_337,
    calibration_bin_count: int = 10,
) -> BenchmarkReport:
    """Score one validator-owned, byte-stable run snapshot without live reads.

    ``formal=False`` permits an incomplete terminal ledger for infrastructure
    diagnosis.  That mode is permanently marked ``diagnostic_only`` and cannot
    be confused with a completed benchmark result.
    """

    expected_count = _positive_int(expected_case_count, location="expected_case_count")
    if not isinstance(formal, bool):
        raise TypeError("formal must be boolean")
    if (
        not isinstance(calibration_bin_count, int)
        or isinstance(calibration_bin_count, bool)
        or calibration_bin_count < 1
    ):
        raise ReportingError("calibration_bin_count must be a positive integer")
    verified = _validate_verified_snapshot(
        verified_run_snapshot,
        expected_case_count=expected_count,
        formal=formal,
    )
    run_name = verified.run_id
    max_attempts = _positive_int(
        _mapping(
            verified.contract.get("transport"),
            location="run manifest transport",
        ).get("max_attempts"),
        location="run manifest max_attempts",
    )
    public_cases_bytes = verified.artifact_bytes["public_cases"]
    private_mapping_bytes = verified.artifact_bytes["private_mapping"]
    full_structured_bytes = verified.artifact_bytes["full_structured"]
    core_provisional_bytes = verified.artifact_bytes["core_provisional"]
    label_catalog_bytes = verified.artifact_bytes["label_catalog"]
    ontology_audit_bytes = verified.artifact_bytes["ontology_audit"]
    cluster_bytes = verified.artifact_bytes["homology_clusters"]
    terminal_records = verified.terminal_records
    attempt_records = {
        sample_id: tuple(bundle.record_bytes for bundle in bundles)
        for sample_id, bundles in verified.attempt_bundles.items()
    }
    frozen = expected_count == 5_337
    catalog = _parse_catalog(label_catalog_bytes)
    if frozen:
        observed_bands = Counter(catalog.bands.values())
        if dict(observed_bands) != {"tail": 70, "mid": 20, "head": 14}:
            raise ReportingError("frozen label catalog must contain 14/20/70 head/mid/tail labels")
    cases = _parse_public_cases(public_cases_bytes, catalog, expected_count)
    links = _parse_private_links(private_mapping_bytes, cases, expected_count)
    full_records = _parse_gold_records(
        full_structured_bytes,
        catalog,
        location="Full-Structured",
        expected_count=expected_count,
    )
    core_records = _parse_gold_records(
        core_provisional_bytes,
        catalog,
        location="Core-Provisional",
        expected_count=None,
    )
    conflict_group_count, conflict_accession_count = _validate_exact_sequence_groups(full_records)
    if frozen and (conflict_group_count, conflict_accession_count) != (6, 12):
        raise ReportingError(
            "frozen exact-sequence conflicts must contain 6 groups and 12 accessions"
        )
    overlap_records = [record for record in full_records if record.overlapping_blocks]
    overlap_pair_count = sum(
        record.overlapping_block_pair_count for record in overlap_records
    )
    if frozen and (len(overlap_records), overlap_pair_count) != (6, 8):
        raise ReportingError(
            "frozen overlapping gold formulas must contain 6 accessions and "
            "8 overlapping block pairs"
        )
    conflict_accessions = {
        record.accession for record in full_records if record.conflict
    }
    overlap_accessions = {record.accession for record in overlap_records}
    ranking_excluded_accessions = conflict_accessions | overlap_accessions
    links_by_accession = _validate_full_mapping(full_records, links)
    _validate_core_records(core_records, full_records, frozen=frozen)
    ancestors = _parse_ontology(ontology_audit_bytes, catalog, frozen=frozen)

    cluster_by_accession: dict[str, str] | None = None
    if cluster_bytes is not None:
        cluster_by_accession = _parse_clusters(cluster_bytes, full_records)
        if frozen and len(set(cluster_by_accession.values())) != 5_066:
            raise ReportingError("frozen homology artifact must contain 5066 clusters")
    ranking_eligible_entity_count = len(
        {
            record.sequence_entity_id
            for record in full_records
            if not record.ranking_excluded
        }
    )
    ranking_eligible_cluster_count = (
        len(
            {
                cluster_by_accession[record.accession]
                for record in full_records
                if not record.ranking_excluded
            }
        )
        if cluster_by_accession is not None
        else None
    )

    model_contract = _mapping(
        verified.contract.get("model"), location="run manifest model"
    )
    terminals = _parse_terminal_records(
        terminal_records,
        cases,
        catalog,
        model_contract,
    )
    if formal and len(terminals) != expected_count:
        raise ReportingError(
            f"Formal run requires {expected_count} terminal records; observed {len(terminals)}"
        )
    _validate_attempt_bundles(verified.attempt_bundles, terminals, cases)
    parsed_attempts = _parse_attempt_records(
        attempt_records,
        terminals,
        cases,
        model_contract,
    )
    if formal and not parsed_attempts:
        raise ReportingError("formal run requires a complete attempt ledger for usage/latency")
    parsed_incidents = _parse_incident_bundles(
        verified.incident_bundles,
        cases,
        max_attempts=max_attempts,
        model_contract=model_contract,
    )

    link_by_sample = {link.sample_id: link for link in links}
    terminals_by_accession = {
        link_by_sample[sample_id].accession: terminal
        for sample_id, terminal in terminals.items()
    }
    accession_metrics, accession_scored = _weighted_structured(
        full_records,
        terminals_by_accession,
        _accession_weights(full_records),
        ancestors,
    )
    accession_metrics["weighting"] = (
        "accession_micro; exact-sequence conflicts and overlapping gold blocks zero"
    )

    primary_weights, primary_entity_count = _entity_weights(
        full_records, terminals_by_accession
    )
    primary_metrics, primary_scored = _weighted_structured(
        full_records,
        terminals_by_accession,
        primary_weights,
        ancestors,
    )
    primary_metrics.update(
        {
            "weighting": "exact_sequence_entity_macro",
            "entity_count": primary_entity_count,
            "ranking_exclusion_policy": (
                "DUPLICATE_CONFLICT and OVERLAPPING_BLOCK_LABEL accessions "
                "receive zero weight in every formal ranking"
            ),
        }
    )

    conflict_records = [record for record in full_records if record.conflict]
    conflict_metrics, _ = _weighted_structured(
        conflict_records,
        terminals_by_accession,
        _diagnostic_unit_weights(conflict_records),
        ancestors,
    )
    conflict_slice = {
        "group_count": conflict_group_count,
        "accession_count": conflict_accession_count,
        "primary_effective_weight": 0.0,
        "metrics": conflict_metrics,
        "groups": _conflict_group_details(
            full_records, terminals_by_accession, links_by_accession
        ),
    }

    overlap_slice = {
        "reason_code": "OVERLAPPING_BLOCK_LABEL",
        "reason": (
            "The unique-label-set response cannot encode a label satisfying "
            "multiple independent UniProt cofactor blocks."
        ),
        "accession_count": len(overlap_records),
        "overlapping_block_pair_count": overlap_pair_count,
        "ranking_effective_weight": 0.0,
        "records": _overlap_record_details(
            full_records,
            terminals_by_accession,
            links_by_accession,
        ),
    }

    label_macro = _label_macro(primary_scored, catalog.labels)
    label_macro["weighting"] = (
        "exact_sequence_entity_macro; conflicts and overlapping gold blocks zero"
    )
    label_macro_by_band = {
        band: _label_macro(
            primary_scored,
            catalog.labels,
            selected_labels=frozenset(
                label for label in catalog.labels if catalog.bands[label] == band
            ),
        )
        for band in ("head", "mid", "tail")
    }

    cluster_section: dict[str, Any]
    cluster_scored: tuple[_ScoredRecord, ...] = ()
    if cluster_by_accession is None:
        cluster_section = {
            "available": False,
            "cluster_count": 0,
            "effective_primary_cluster_count": 0,
            "metrics": None,
        }
    else:
        cluster_weights, effective_clusters = _cluster_weights(
            full_records, terminals_by_accession, cluster_by_accession
        )
        cluster_metrics, cluster_scored = _weighted_structured(
            full_records,
            terminals_by_accession,
            cluster_weights,
            ancestors,
        )
        cluster_metrics["weighting"] = (
            "cluster macro; exact-sequence entities equal within cluster; "
            "accessions equal within entity; conflicts and overlapping gold "
            "blocks zero"
        )
        cluster_section = {
            "available": True,
            "method": {
                "tool": "DIAMOND",
                "identity_percent": CLUSTER_IDENTITY_PERCENT,
                "mutual_coverage_percent": CLUSTER_MUTUAL_COVERAGE_PERCENT,
                "cluster_id_version": CLUSTER_ID_VERSION,
            },
            "cluster_count": len(set(cluster_by_accession.values())),
            "effective_primary_cluster_count": effective_clusters,
            "metrics": cluster_metrics,
        }

    core = _core_metrics(core_records, terminals_by_accession)
    core["source_view_record_count"] = len(core_records)
    core["ranking_excluded_record_count"] = sum(
        record.ranking_excluded for record in core_records
    )
    core["primary_field"] = "primary_guess"
    core["abstentions_still_scored"] = True
    core["by_frequency_band"] = _core_by_band(
        core_records, terminals_by_accession, catalog
    )

    calibration = {
        "target": "record_exact",
        "confidence_field": "confidence_complete",
        "primary": _calibration_dict(primary_scored, bin_count=calibration_bin_count),
        "accession_diagnostic": _calibration_dict(
            accession_scored, bin_count=calibration_bin_count
        ),
    }
    if cluster_scored:
        calibration["homology_cluster_diagnostic"] = _calibration_dict(
            cluster_scored, bin_count=calibration_bin_count
        )

    terminal_summary = _terminal_summary(
        terminals, parsed_attempts, expected_count=expected_count
    )
    resources = _resource_summary(parsed_attempts, terminals)
    incident_resources = _incident_resource_summary(
        parsed_incidents, verified.incident_bundles
    )
    resources["transport_incidents"] = incident_resources
    resources["model_attempt_budget_max_per_sample"] = max_attempts

    headline_keys = (
        "record_count",
        "effective_weight",
        "record_exact_accuracy",
        "block_micro_precision",
        "block_micro_recall",
        "block_micro_f1",
        "block_coverage",
        "hierarchy_block_micro_precision",
        "hierarchy_block_micro_recall",
        "hierarchy_block_micro_f1",
        "coverage",
        "selective_record_exact_accuracy",
    )
    headline = {
        "view": "Full-Structured",
        "weighting": "exact_sequence_entity_macro",
        "strict_chebi_is_ranking_metric": True,
        **{key: primary_metrics[key] for key in headline_keys},
    }

    input_hashes: dict[str, str] = {
        **verified.artifact_sha256,
        "run_manifest": verified.manifest_sha256,
        "terminal_ledger": _ledger_hash(
            terminal_records, domain="cofactor9.1.terminal-ledger.v1"
        ),
        "attempt_ledger": _attempt_ledger_hash(attempt_records)
        or _ledger_hash({}, domain="cofactor9.1.attempt-ledger.v1"),
        "incident_ledger": incident_resources["ledger_sha256"],
        "ledger_composite": verified.ledger_composite_sha256,
        "provenance_composite": verified.provenance_composite_sha256,
    }
    eligible_accession_count = len(full_records) - len(ranking_excluded_accessions)
    eligible_terminal_records = [
        record
        for record in full_records
        if not record.ranking_excluded and record.accession in terminals_by_accession
    ]
    eligible_success_count = sum(
        terminals_by_accession[record.accession].status == "success"
        for record in eligible_terminal_records
    )
    eligible_terminal_error_count = sum(
        terminals_by_accession[record.accession].status == "terminal_error"
        for record in eligible_terminal_records
    )
    binary_contract = _mapping(
        verified.contract.get("codex_binary"), location="run manifest codex_binary"
    )
    provenance = {
        "run_manifest": {
            "schema_version": verified.manifest["schema_version"],
            "sha256": verified.manifest_sha256,
            "contract_sha256": verified.manifest["contract_sha256"],
        },
        **verified.versions,
        "model": {
            "name": model_contract["name"],
            "reasoning_effort": model_contract["reasoning_effort"],
            "service_tier": model_contract["service_tier"],
            "binary_requested": binary_contract.get("requested"),
            "binary_resolved_path": binary_contract.get("resolved_path"),
            "binary_sha256": binary_contract["sha256"],
            "binary_version": binary_contract["version"],
        },
        "evaluation_implementation_sha256": dict(
            verified.implementation_sha256
        ),
        "scoring_implementation_sha256": verified.implementation_sha256[
            "reporting"
        ],
        "evaluation_artifact_sha256": dict(verified.artifact_sha256),
        "ledger_composite_sha256": verified.ledger_composite_sha256,
        "provenance_composite_sha256": verified.provenance_composite_sha256,
    }
    denominators = {
        "expected_accession_count": expected_count,
        "ranking_eligible_accession_count": eligible_accession_count,
        "terminal_count": len(terminals),
        "success_count": terminal_summary["success_count"],
        "terminal_error_count": terminal_summary["terminal_error_count"],
        "pending_count": expected_count - len(terminals),
        "incident_count": incident_resources["incident_count"],
        "incident_affected_sample_count": incident_resources[
            "affected_sample_count"
        ],
        "ranking_eligible_terminal_count": len(eligible_terminal_records),
        "ranking_eligible_success_count": eligible_success_count,
        "ranking_eligible_terminal_error_count": eligible_terminal_error_count,
        "ranking_eligible_pending_count": (
            eligible_accession_count - len(eligible_terminal_records)
        ),
    }
    value: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "dataset_version": _DATASET_VERSION,
        "run_id": run_name,
        "mode": "formal" if formal else "partial_diagnostic",
        "diagnostic_only": not formal,
        "model": {
            "name": model_contract["name"],
            "reasoning_effort": model_contract["reasoning_effort"],
            "service_tier": model_contract["service_tier"],
            "prompt_version": model_contract["prompt_version"],
            "response_schema_version": model_contract[
                "response_schema_version"
            ],
        },
        "provenance": provenance,
        "input_sha256": input_hashes,
        "outcome_denominators": denominators,
        "terminal_ledger": terminal_summary,
        "headline": headline,
        "full_structured": {
            "source_view_record_count": len(full_records),
            "gold_formula_audit": {
                "preserved_block_count": sum(
                    len(record.gold_blocks) for record in full_records
                ),
                "overlapping_block_pair_count": overlap_pair_count,
                "overlapping_block_label_accession_count": len(
                    overlap_accessions
                ),
                "label_union_matches_experimental_labels": True,
            },
            "ranking_exclusions": {
                "zero_weight_accession_count": len(ranking_excluded_accessions),
                "ranking_eligible_accession_count": eligible_accession_count,
                "exact_sequence_conflict_accession_count": len(
                    conflict_accessions
                ),
                "overlapping_block_label_accession_count": len(
                    overlap_accessions
                ),
                "intersection_accession_count": len(
                    conflict_accessions & overlap_accessions
                ),
                "ranking_eligible_exact_sequence_entity_count": (
                    ranking_eligible_entity_count
                ),
                "ranking_eligible_homology_cluster_count": (
                    ranking_eligible_cluster_count
                ),
            },
            "primary": primary_metrics,
            "accession_weighted": accession_metrics,
            "label_macro": label_macro,
            "label_macro_by_frequency_band": label_macro_by_band,
            "by_gold_frequency_band": _slice_structured_by_band(
                full_records,
                terminals_by_accession,
                primary_weights,
                ancestors,
                catalog,
            ),
        },
        "homology_cluster_macro": cluster_section,
        "conflict_slice": conflict_slice,
        "unrepresentable_overlap_slice": overlap_slice,
        "core_single": core,
        "calibration": calibration,
        "resources": resources,
    }
    return BenchmarkReport(value)


def _report_value(report: BenchmarkReport | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(report, BenchmarkReport):
        return report.to_dict()
    if isinstance(report, Mapping):
        return deepcopy(dict(report))
    raise TypeError("report must be a BenchmarkReport or mapping")


def report_json_bytes(report: BenchmarkReport | Mapping[str, Any]) -> bytes:
    """Render canonical UTF-8 JSON with stable ordering and no nonfinite values."""

    return (
        json.dumps(
            _report_value(report),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def render_markdown(report: BenchmarkReport | Mapping[str, Any]) -> str:
    """Render a compact human report from the exact machine result."""

    value = _report_value(report)
    if value.get("schema_version") != RESULT_SCHEMA_VERSION:
        raise ReportingError("cannot render an unsupported result schema")
    headline = _mapping(value.get("headline"), location="result headline")
    terminals = _mapping(value.get("terminal_ledger"), location="terminal summary")
    core = _mapping(value.get("core_single"), location="Core summary")
    conflicts = _mapping(value.get("conflict_slice"), location="conflict summary")
    overlaps = _mapping(
        value.get("unrepresentable_overlap_slice"),
        location="overlap summary",
    )
    clusters = _mapping(value.get("homology_cluster_macro"), location="cluster summary")
    calibration = _mapping(value.get("calibration"), location="calibration summary")
    primary_calibration = _mapping(calibration.get("primary"), location="primary calibration")
    resources = _mapping(value.get("resources"), location="resource summary")
    model_attempts = _mapping(
        resources.get("model_attempts"), location="model attempt resources"
    )
    incidents = _mapping(
        resources.get("transport_incidents"),
        location="transport incident resources",
    )
    provenance = _mapping(value.get("provenance"), location="result provenance")
    run_manifest = _mapping(
        provenance.get("run_manifest"), location="run manifest provenance"
    )
    denominators = _mapping(
        value.get("outcome_denominators"), location="outcome denominators"
    )

    lines = [
        "# Cofactor9.1 result report",
        "",
        f"- Run ID: `{value['run_id']}`",
        f"- Mode: `{value['mode']}`",
        f"- Model: `{value['model']['name']}` / `{value['model']['reasoning_effort']}` / `{value['model']['service_tier']}`",
        f"- Manifest SHA-256: `{run_manifest['sha256']}`",
        f"- Ledger composite SHA-256: `{provenance['ledger_composite_sha256']}`",
    ]
    if value.get("diagnostic_only") is True:
        lines.extend(
            [
                "",
                "> **Diagnostic-only partial run.** This is not a completed benchmark result and must not be reported as one.",
            ]
        )
    lines.extend(
        [
            "",
            "## Full-Structured primary",
            "",
            "Primary weighting gives every representable, non-conflicting exact-sequence entity total weight one. Exact-sequence conflicts and overlapping-block formulas receive zero weight in every formal ranking and remain in audited slices below.",
            "",
            f"- Scored records / effective entities: {headline['record_count']} / {headline['effective_weight']:.6f}",
            f"- Strict record-exact accuracy: {headline['record_exact_accuracy']:.6f}",
            f"- Strict block precision / recall / F1: {headline['block_micro_precision']:.6f} / {headline['block_micro_recall']:.6f} / {headline['block_micro_f1']:.6f}",
            f"- Hierarchy-aware block precision / recall / F1 (diagnostic): {headline['hierarchy_block_micro_precision']:.6f} / {headline['hierarchy_block_micro_recall']:.6f} / {headline['hierarchy_block_micro_f1']:.6f}",
            f"- Predict-status coverage / selective exact accuracy: {headline['coverage']:.6f} / {headline['selective_record_exact_accuracy']:.6f}",
            "",
            "## Core-Single",
            "",
            f"- Records / represented labels: {core['record_count']} / {core['represented_label_count']}",
            f"- Accuracy / macro-F1 / balanced accuracy: {core['accuracy']:.6f} / {core['macro_f1']:.6f} / {core['balanced_accuracy']:.6f}",
            "",
            "## Homology cluster macro",
            "",
            f"- Available: {str(clusters['available']).lower()}",
            f"- Clusters / effective primary clusters: {clusters['cluster_count']} / {clusters['effective_primary_cluster_count']}",
            "",
            "## Conflict slice",
            "",
            f"- Groups / accessions: {conflicts['group_count']} / {conflicts['accession_count']}",
            f"- Primary effective weight: {conflicts['primary_effective_weight']:.1f}",
            "",
            "## Unrepresentable overlap slice",
            "",
            f"- Accessions / overlapping block pairs: {overlaps['accession_count']} / {overlaps['overlapping_block_pair_count']}",
            f"- Formal ranking effective weight: {overlaps['ranking_effective_weight']:.1f}",
            f"- Reason: {overlaps['reason']}",
            "",
            "## Ledger integrity",
            "",
            f"- Terminals / expected / missing: {terminals['terminal_count']} / {terminals['expected_case_count']} / {terminals['missing_terminal_count']}",
            f"- Success / terminal errors / parse failures: {terminals['success_count']} / {terminals['terminal_error_count']} / {terminals['prediction_parse_failure_count']}",
            f"- Ranking-eligible / eligible pending: {denominators['ranking_eligible_accession_count']} / {denominators['ranking_eligible_pending_count']}",
            "",
            "## Calibration and resources",
            "",
            f"- Record-exact Brier / ECE / AURC: {primary_calibration['brier_score']:.6f} / {primary_calibration['expected_calibration_error']:.6f} / {primary_calibration['aurc']:.6f}",
            f"- Model attempts / total duration seconds: {model_attempts['attempt_count']} / {model_attempts['total_duration_seconds']:.6f}",
            f"- Transport incidents / affected samples / duration seconds: {incidents['incident_count']} / {incidents['affected_sample_count']} / {incidents['total_duration_seconds']:.6f}",
            "- Transport incidents are excluded from the model-attempt budget.",
            "",
            "## Verified provenance",
            "",
            f"- Run manifest schema / hash: `{run_manifest['schema_version']}` / `{run_manifest['sha256']}`",
            f"- Dataset / view / formula: `{provenance['dataset_version']}` / `{provenance['view_rule_version']}` / `{provenance['formula_rule_version']}`",
            f"- Scoring implementation SHA-256: `{provenance['scoring_implementation_sha256']}`",
            f"- Provenance composite SHA-256: `{provenance['provenance_composite_sha256']}`",
            "",
            "Strict ChEBI metrics are the ranking result. Hierarchy-aware metrics are diagnostic only.",
            "",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "BenchmarkReport",
    "LedgerBundleLike",
    "ReportingError",
    "VerifiedRunSnapshotLike",
    "render_markdown",
    "report_json_bytes",
    "score_run",
]
