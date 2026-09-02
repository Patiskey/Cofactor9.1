"""Deterministic, leakage-safe prompt case artifacts.

The public artifact is deliberately only a JSONL stream of validated
``PromptCase`` payloads.  Accession linkage lives in a separate owner-only
artifact whose declared purpose forbids model exposure.  A deterministic
manifest binds both artifacts to the exact frozen input bytes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any

from .prompt import (
    CATALOG_SIZE,
    PROMPT_VERSION,
    CatalogTerm,
    PromptCase,
    PromptValidationError,
)


EXPECTED_CASE_COUNT = 5_337
CASE_ARTIFACT_SCHEMA_VERSION = "cofactor9.1.case-artifacts.v1"
PUBLIC_CASE_SCHEMA_VERSION = "cofactor9.1.prompt-cases.v1"
PRIVATE_MAPPING_SCHEMA_VERSION = "cofactor9.1.case-map.private.v1"
PRIVATE_MAPPING_PURPOSE = "scoring-and-audit-only; never expose to the model"
PRIVATE_MAPPING_MODE = 0o600

_VIEW_RECORD_SCHEMA_VERSION = "cofactor9.1.view-record.v1"
_LABEL_CATALOG_SCHEMA_VERSION = "cofactor9.1.label-catalog.v1"
_ID_DERIVATION_VERSION = "cofactor9.1.sequence-case-id.v2"
_ID_DERIVATION_DOMAIN = _ID_DERIVATION_VERSION.encode("ascii")
_ACCESSION = re.compile(r"[A-Z0-9]{6,10}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SAMPLE_ID = re.compile(r"sample_[0-9a-f]{32}\Z")


class CaseArtifactError(ValueError):
    """Raised when case inputs or immutable artifacts violate the contract."""


@dataclass(frozen=True, slots=True)
class CaseArtifactPaths:
    """The public, private and manifest paths for one case artifact set."""

    cases: Path
    private_mapping: Path
    manifest: Path


@dataclass(frozen=True, slots=True)
class PrivateCaseMapping:
    """One private accession link, never suitable for model input."""

    sample_id: str
    accession: str
    sequence_sha256: str

    def __post_init__(self) -> None:
        if _SAMPLE_ID.fullmatch(self.sample_id) is None:
            raise CaseArtifactError("Private mapping has an invalid sample_id")
        if _ACCESSION.fullmatch(self.accession) is None:
            raise CaseArtifactError("Private mapping has an invalid accession")
        if _SHA256.fullmatch(self.sequence_sha256) is None:
            raise CaseArtifactError("Private mapping has an invalid sequence SHA256")

    def to_dict(self) -> dict[str, str]:
        return {
            "schema_version": PRIVATE_MAPPING_SCHEMA_VERSION,
            "visibility": "private",
            "purpose": PRIVATE_MAPPING_PURPOSE,
            "sample_id": self.sample_id,
            "accession": self.accession,
            "sequence_sha256": self.sequence_sha256,
        }


@dataclass(frozen=True, slots=True)
class CaseArtifacts:
    """In-memory deterministic prompt cases and their canonical bytes."""

    cases: tuple[PromptCase, ...]
    private_mappings: tuple[PrivateCaseMapping, ...]
    manifest: dict[str, Any]
    cases_jsonl: bytes
    private_mapping_jsonl: bytes
    manifest_json: bytes


@dataclass(frozen=True, slots=True)
class CaseBuildSummary:
    """Compact integrity summary for a built or validated artifact set."""

    case_count: int
    catalog_term_count: int
    unique_sample_id_count: int
    unique_accession_count: int
    input_sha256: dict[str, str]
    output_sha256: dict[str, str]
    cases_path: Path
    private_mapping_path: Path
    manifest_path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_count": self.case_count,
            "catalog_term_count": self.catalog_term_count,
            "unique_sample_id_count": self.unique_sample_id_count,
            "unique_accession_count": self.unique_accession_count,
            "input_sha256": dict(self.input_sha256),
            "output_sha256": dict(self.output_sha256),
            "paths": {
                "cases": str(self.cases_path),
                "private_mapping": str(self.private_mapping_path),
                "manifest": str(self.manifest_path),
            },
        }


@dataclass(frozen=True, slots=True)
class _SourceCase:
    accession: str
    sequence: str
    sequence_sha256: str
    experimental_label_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _SourceOccurrence:
    source: _SourceCase
    duplicate_ordinal: int


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _jsonl_bytes(values: tuple[Mapping[str, Any], ...]) -> bytes:
    return b"".join(_json_bytes(value) for value in values)


def _reject_json_constant(value: str) -> None:
    raise CaseArtifactError(f"Non-finite JSON number {value!r} is not allowed")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CaseArtifactError(f"Duplicate JSON field {key!r} is not allowed")
        result[key] = value
    return result


def _decode_json(value: bytes, *, location: str) -> Any:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CaseArtifactError(f"{location} is not valid UTF-8") from error
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except CaseArtifactError:
        raise
    except json.JSONDecodeError as error:
        raise CaseArtifactError(f"{location} is not valid JSON: {error.msg}") from error


def _decode_jsonl(value: bytes, *, location: str) -> list[dict[str, Any]]:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CaseArtifactError(f"{location} is not valid UTF-8") from error
    if not text:
        raise CaseArtifactError(f"{location} is empty")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            raise CaseArtifactError(
                f"{location} line {line_number} is blank; "
                "blank JSONL lines are forbidden"
            )
        try:
            decoded = json.loads(
                line,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except CaseArtifactError:
            raise
        except json.JSONDecodeError as error:
            raise CaseArtifactError(
                f"{location} line {line_number} is not valid JSON: {error.msg}"
            ) from error
        if not isinstance(decoded, dict):
            raise CaseArtifactError(
                f"{location} line {line_number} must contain a JSON object"
            )
        records.append(decoded)
    return records


def _required_mapping(
    container: Mapping[str, Any],
    key: str,
    *,
    location: str,
) -> Mapping[str, Any]:
    value = container.get(key)
    if not isinstance(value, Mapping):
        raise CaseArtifactError(f"{location}.{key} must be a JSON object")
    return value


def _required_string(
    container: Mapping[str, Any],
    key: str,
    *,
    location: str,
) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value:
        raise CaseArtifactError(f"{location}.{key} must be a nonempty string")
    return value


def _parse_catalog(
    value: bytes,
) -> tuple[str, str, str, tuple[CatalogTerm, ...]]:
    decoded = _decode_json(value, location="label catalog")
    if not isinstance(decoded, Mapping):
        raise CaseArtifactError("label catalog must contain a JSON object")
    if decoded.get("schema_version") != _LABEL_CATALOG_SCHEMA_VERSION:
        raise CaseArtifactError("label catalog schema_version is unsupported")
    dataset_version = _required_string(
        decoded,
        "dataset_version",
        location="label catalog",
    )
    catalog_version = _required_string(
        decoded,
        "catalog_version",
        location="label catalog",
    )
    rule_version = _required_string(
        decoded,
        "rule_version",
        location="label catalog",
    )
    labels = decoded.get("labels")
    if not isinstance(labels, list):
        raise CaseArtifactError("label catalog.labels must be a JSON list")
    if len(labels) != CATALOG_SIZE:
        raise CaseArtifactError(
            f"label catalog must contain exactly {CATALOG_SIZE} labels"
        )
    terms: list[CatalogTerm] = []
    for index, item in enumerate(labels):
        if not isinstance(item, Mapping):
            raise CaseArtifactError(f"label catalog label {index} must be an object")
        try:
            terms.append(
                CatalogTerm(
                    _required_string(
                        item,
                        "chebi_id",
                        location=f"label catalog label {index}",
                    ),
                    _required_string(
                        item,
                        "name",
                        location=f"label catalog label {index}",
                    ),
                )
            )
        except PromptValidationError as error:
            raise CaseArtifactError(
                f"label catalog label {index} is invalid: {error}"
            ) from error

    summary = _required_mapping(decoded, "summary", location="label catalog")
    if summary.get("label_count") != len(terms):
        raise CaseArtifactError(
            "label catalog.summary.label_count differs from labels"
        )
    try:
        PromptCase(
            sample_id="sample_00000000000000000000000000000000",
            sequence="M",
            catalog_version=catalog_version,
            catalog_terms=tuple(terms),
        )
    except PromptValidationError as error:
        raise CaseArtifactError(f"label catalog is invalid: {error}") from error
    return dataset_version, catalog_version, rule_version, tuple(terms)


def _parse_source_cases(
    value: bytes,
    *,
    expected_case_count: int,
    expected_dataset_version: str,
    expected_rule_version: str,
) -> tuple[_SourceCase, ...]:
    records = _decode_jsonl(value, location="Full-Structured input")
    if len(records) != expected_case_count:
        raise CaseArtifactError(
            "Full-Structured input must contain exactly "
            f"{expected_case_count} records; observed {len(records)}"
        )

    source_cases: list[_SourceCase] = []
    accessions: set[str] = set()
    for index, record in enumerate(records):
        location = f"Full-Structured record {index + 1}"
        if record.get("schema_version") != _VIEW_RECORD_SCHEMA_VERSION:
            raise CaseArtifactError(f"{location} schema_version is unsupported")
        if record.get("dataset_version") != expected_dataset_version:
            raise CaseArtifactError(
                f"{location} dataset_version differs from the label catalog"
            )
        derivation = _required_mapping(record, "derivation", location=location)
        if derivation.get("rule_version") != expected_rule_version:
            raise CaseArtifactError(
                f"{location}.derivation.rule_version differs from the label catalog"
            )
        entry = _required_mapping(record, "entry", location=location)
        accession = _required_string(entry, "accession", location=f"{location}.entry")
        if _ACCESSION.fullmatch(accession) is None:
            raise CaseArtifactError(f"{location} has an invalid accession")
        if accession in accessions:
            raise CaseArtifactError(f"Duplicate accession {accession!r}")
        accessions.add(accession)

        sequence = _required_mapping(record, "sequence", location=location)
        sequence_value = _required_string(
            sequence,
            "value",
            location=f"{location}.sequence",
        )
        sequence_sha256 = _required_string(
            sequence,
            "sha256",
            location=f"{location}.sequence",
        )
        if _SHA256.fullmatch(sequence_sha256) is None:
            raise CaseArtifactError(f"{location} has an invalid sequence SHA256")
        try:
            observed_sequence_sha256 = hashlib.sha256(
                sequence_value.encode("ascii")
            ).hexdigest()
        except UnicodeEncodeError as error:
            raise CaseArtifactError(
                f"{location} sequence must contain only ASCII amino-acid symbols"
            ) from error
        if not secrets.compare_digest(observed_sequence_sha256, sequence_sha256):
            raise CaseArtifactError(f"{location} sequence SHA256 does not match value")

        derived = _required_mapping(record, "derived", location=location)
        raw_label_ids = derived.get("experimental_label_ids")
        if not isinstance(raw_label_ids, list) or not raw_label_ids:
            raise CaseArtifactError(
                f"{location}.derived.experimental_label_ids must be a nonempty list"
            )
        if any(
            not isinstance(label_id, str) or not label_id
            for label_id in raw_label_ids
        ):
            raise CaseArtifactError(
                f"{location}.derived.experimental_label_ids contains an invalid ID"
            )
        if len(raw_label_ids) != len(set(raw_label_ids)):
            raise CaseArtifactError(
                f"{location}.derived.experimental_label_ids contains duplicates"
            )
        source_cases.append(
            _SourceCase(
                accession=accession,
                sequence=sequence_value,
                sequence_sha256=sequence_sha256,
                experimental_label_ids=tuple(raw_label_ids),
            )
        )
    return tuple(source_cases)


def _opaque_sample_id(sequence_sha256: str, duplicate_ordinal: int) -> str:
    message = (
        _ID_DERIVATION_DOMAIN
        + b"\0"
        + sequence_sha256.encode("ascii")
        + b"\0"
        + str(duplicate_ordinal).encode("ascii")
    )
    token = hashlib.sha256(message).hexdigest()[:32]
    return f"sample_{token}"


def _ordered_occurrences(
    sources: tuple[_SourceCase, ...],
) -> tuple[_SourceOccurrence, ...]:
    exact_sequence_groups: dict[tuple[str, str], list[_SourceCase]] = {}
    for source in sources:
        sequence_identity = (source.sequence_sha256, source.sequence)
        exact_sequence_groups.setdefault(sequence_identity, []).append(source)

    occurrences: list[_SourceOccurrence] = []
    for sequence_identity in sorted(exact_sequence_groups):
        group = sorted(
            exact_sequence_groups[sequence_identity],
            key=lambda source: source.accession,
        )
        occurrences.extend(
            _SourceOccurrence(source=source, duplicate_ordinal=ordinal)
            for ordinal, source in enumerate(group)
        )
    return tuple(occurrences)


def _validate_expected_count(expected_case_count: object) -> int:
    if (
        isinstance(expected_case_count, bool)
        or not isinstance(expected_case_count, int)
        or expected_case_count < 1
    ):
        raise CaseArtifactError("expected_case_count must be a positive integer")
    return expected_case_count


def derive_case_artifacts(
    full_structured_bytes: bytes,
    label_catalog_bytes: bytes,
    *,
    expected_case_count: int = EXPECTED_CASE_COUNT,
) -> CaseArtifacts:
    """Purely derive canonical public/private case artifacts from frozen bytes."""

    if not isinstance(full_structured_bytes, bytes):
        raise TypeError("full_structured_bytes must be bytes")
    if not isinstance(label_catalog_bytes, bytes):
        raise TypeError("label_catalog_bytes must be bytes")
    expected_count = _validate_expected_count(expected_case_count)
    (
        dataset_version,
        catalog_version,
        view_rule_version,
        catalog_terms,
    ) = _parse_catalog(label_catalog_bytes)
    sources = _parse_source_cases(
        full_structured_bytes,
        expected_case_count=expected_count,
        expected_dataset_version=dataset_version,
        expected_rule_version=view_rule_version,
    )
    catalog_ids = {term.chebi_id for term in catalog_terms}
    gold_union = {
        label_id for source in sources for label_id in source.experimental_label_ids
    }
    if catalog_ids != gold_union:
        missing_from_catalog = sorted(gold_union - catalog_ids)
        absent_from_gold = sorted(catalog_ids - gold_union)
        raise CaseArtifactError(
            "label catalog identifiers differ from the Full experimental gold union; "
            f"missing_from_catalog={missing_from_catalog}, "
            f"absent_from_gold={absent_from_gold}"
        )

    ordered_occurrences = _ordered_occurrences(sources)
    cases: list[PromptCase] = []
    private_mappings: list[PrivateCaseMapping] = []
    seen_sample_ids: set[str] = set()
    for occurrence in ordered_occurrences:
        source = occurrence.source
        sample_id = _opaque_sample_id(
            source.sequence_sha256,
            occurrence.duplicate_ordinal,
        )
        if sample_id in seen_sample_ids:
            raise CaseArtifactError(
                "Deterministic sample_id collision; refusing ambiguous mapping"
            )
        seen_sample_ids.add(sample_id)
        try:
            case = PromptCase(
                sample_id=sample_id,
                sequence=source.sequence,
                catalog_version=catalog_version,
                catalog_terms=catalog_terms,
            )
        except PromptValidationError as error:
            raise CaseArtifactError(
                "Source sequence for private accession "
                f"{source.accession!r} is invalid: "
                f"{error}"
            ) from error
        cases.append(case)
        private_mappings.append(
            PrivateCaseMapping(
                sample_id=sample_id,
                accession=source.accession,
                sequence_sha256=source.sequence_sha256,
            )
        )

    cases_tuple = tuple(cases)
    mappings_tuple = tuple(private_mappings)
    cases_jsonl = _jsonl_bytes(tuple(case.to_payload() for case in cases_tuple))
    private_mapping_jsonl = _jsonl_bytes(
        tuple(mapping.to_dict() for mapping in mappings_tuple)
    )
    input_sha256 = {
        "full_structured": _sha256_bytes(full_structured_bytes),
        "label_catalog": _sha256_bytes(label_catalog_bytes),
    }
    output_sha256 = {
        "prompt_cases": _sha256_bytes(cases_jsonl),
        "private_mapping": _sha256_bytes(private_mapping_jsonl),
    }
    manifest: dict[str, Any] = {
        "schema_version": CASE_ARTIFACT_SCHEMA_VERSION,
        "dataset_version": dataset_version,
        "prompt_version": PROMPT_VERSION,
        "public_case_schema_version": PUBLIC_CASE_SCHEMA_VERSION,
        "catalog_version": catalog_version,
        "view_rule_version": view_rule_version,
        "id_derivation": {
            "algorithm": "SHA-256",
            "digest_hex_characters": 32,
            "domain": _ID_DERIVATION_VERSION,
            "inputs": [
                "sequence_sha256",
                "exact_sequence_duplicate_ordinal",
            ],
            "ordinal_base": 0,
            "version": _ID_DERIVATION_VERSION,
        },
        "private_mapping": {
            "schema_version": PRIVATE_MAPPING_SCHEMA_VERSION,
            "visibility": "private",
            "purpose": PRIVATE_MAPPING_PURPOSE,
            "file_mode": "0600",
        },
        "input_sha256": input_sha256,
        "output_sha256": output_sha256,
        "counts": {
            "prompt_cases": len(cases_tuple),
            "private_mappings": len(mappings_tuple),
            "unique_sample_ids": len(seen_sample_ids),
            "unique_accessions": len({item.accession for item in mappings_tuple}),
            "catalog_terms": len(catalog_terms),
        },
    }
    return CaseArtifacts(
        cases=cases_tuple,
        private_mappings=mappings_tuple,
        manifest=manifest,
        cases_jsonl=cases_jsonl,
        private_mapping_jsonl=private_mapping_jsonl,
        manifest_json=_json_bytes(manifest),
    )


def case_artifact_paths(cases_path: str | Path) -> CaseArtifactPaths:
    """Derive explicit private-map and manifest paths beside ``cases.jsonl``."""

    public_path = Path(cases_path)
    if public_path.suffix != ".jsonl":
        raise CaseArtifactError("Public cases path must end with .jsonl")
    stem = public_path.stem
    return CaseArtifactPaths(
        cases=public_path,
        private_mapping=public_path.with_name(f"{stem}.private-map.jsonl"),
        manifest=public_path.with_name(f"{stem}.manifest.json"),
    )


def load_prompt_cases(path: str | Path) -> tuple[PromptCase, ...]:
    """Strictly load public cases; any metadata field is a hard failure."""

    source = Path(path)
    try:
        records = _decode_jsonl(source.read_bytes(), location="prompt cases")
    except OSError as error:
        raise CaseArtifactError(
            f"Cannot read prompt cases {source}: {error}"
        ) from error
    cases: list[PromptCase] = []
    seen_sample_ids: set[str] = set()
    shared_catalog: tuple[str, tuple[CatalogTerm, ...]] | None = None
    for index, record in enumerate(records):
        try:
            case = PromptCase.from_payload(record)
        except PromptValidationError as error:
            raise CaseArtifactError(f"Prompt case line {index + 1}: {error}") from error
        if case.sample_id in seen_sample_ids:
            raise CaseArtifactError(f"Duplicate prompt sample_id {case.sample_id!r}")
        seen_sample_ids.add(case.sample_id)
        catalog = (case.catalog_version, case.catalog_terms)
        if shared_catalog is None:
            shared_catalog = catalog
        elif catalog != shared_catalog:
            raise CaseArtifactError(
                "Every prompt case must expose the identical catalog"
            )
        cases.append(case)
    return tuple(cases)


def _resolved_paths(
    *,
    full_structured_path: str | Path,
    label_catalog_path: str | Path,
    cases_path: str | Path,
    private_mapping_path: str | Path,
    manifest_path: str | Path,
) -> tuple[Path, Path, CaseArtifactPaths]:
    full = Path(full_structured_path).resolve()
    catalog = Path(label_catalog_path).resolve()
    outputs = CaseArtifactPaths(
        cases=Path(cases_path).resolve(),
        private_mapping=Path(private_mapping_path).resolve(),
        manifest=Path(manifest_path).resolve(),
    )
    all_paths = {
        full,
        catalog,
        outputs.cases,
        outputs.private_mapping,
        outputs.manifest,
    }
    if len(all_paths) != 5:
        raise CaseArtifactError("Case input and output paths must all be distinct")
    return full, catalog, outputs


def _prepare_from_paths(
    full_path: Path,
    catalog_path: Path,
    *,
    expected_case_count: int,
) -> CaseArtifacts:
    try:
        full_bytes = full_path.read_bytes()
        catalog_bytes = catalog_path.read_bytes()
    except OSError as error:
        raise CaseArtifactError(f"Cannot read frozen case input: {error}") from error
    return derive_case_artifacts(
        full_bytes,
        catalog_bytes,
        expected_case_count=expected_case_count,
    )


def _target_payloads(
    artifacts: CaseArtifacts,
    paths: CaseArtifactPaths,
) -> dict[Path, tuple[bytes, int, str]]:
    return {
        paths.cases: (artifacts.cases_jsonl, 0o644, "prompt cases"),
        paths.private_mapping: (
            artifacts.private_mapping_jsonl,
            PRIVATE_MAPPING_MODE,
            "private mapping",
        ),
        paths.manifest: (artifacts.manifest_json, 0o644, "manifest"),
    }


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _verify_regular_file(path: Path, *, description: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise CaseArtifactError(f"Cannot inspect {description}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise CaseArtifactError(f"{description} must be a regular non-symlink file")
    return metadata


def _verify_existing(
    artifacts: CaseArtifacts,
    paths: CaseArtifactPaths,
) -> None:
    payloads = _target_payloads(artifacts, paths)
    for path, (expected, _, description) in payloads.items():
        metadata = _verify_regular_file(path, description=description)
        if path == paths.private_mapping:
            observed_mode = stat.S_IMODE(metadata.st_mode)
            if observed_mode != PRIVATE_MAPPING_MODE:
                raise CaseArtifactError(
                    "Private mapping permissions must be exactly 0600; "
                    f"observed {observed_mode:04o}"
                )
        try:
            observed = path.read_bytes()
        except OSError as error:
            raise CaseArtifactError(f"Cannot read {description}: {error}") from error
        if not secrets.compare_digest(observed, expected):
            raise CaseArtifactError(
                f"Existing {description} content/SHA256 differs from "
                "deterministic bytes"
            )


def _exclusive_write(path: Path, payload: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, mode)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as error:
        raise CaseArtifactError(
            f"Output appeared concurrently at {path}; refusing to overwrite"
        ) from error
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _publish_new(artifacts: CaseArtifacts, paths: CaseArtifactPaths) -> None:
    created: list[Path] = []
    try:
        for path, (payload, mode, _) in _target_payloads(artifacts, paths).items():
            _exclusive_write(path, payload, mode=mode)
            created.append(path)
        _verify_existing(artifacts, paths)
    except BaseException:
        for path in reversed(created):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _summary(artifacts: CaseArtifacts, paths: CaseArtifactPaths) -> CaseBuildSummary:
    counts = artifacts.manifest["counts"]
    return CaseBuildSummary(
        case_count=counts["prompt_cases"],
        catalog_term_count=counts["catalog_terms"],
        unique_sample_id_count=counts["unique_sample_ids"],
        unique_accession_count=counts["unique_accessions"],
        input_sha256=dict(artifacts.manifest["input_sha256"]),
        output_sha256={
            **dict(artifacts.manifest["output_sha256"]),
            "manifest": _sha256_bytes(artifacts.manifest_json),
        },
        cases_path=paths.cases,
        private_mapping_path=paths.private_mapping,
        manifest_path=paths.manifest,
    )


def build_case_artifacts(
    *,
    full_structured_path: str | Path,
    label_catalog_path: str | Path,
    cases_path: str | Path,
    private_mapping_path: str | Path,
    manifest_path: str | Path,
    expected_case_count: int = EXPECTED_CASE_COUNT,
) -> CaseBuildSummary:
    """Create immutable case artifacts, or verify an identical prior build.

    No existing byte is ever replaced.  A complete identical set is an
    idempotent success; any partial or different set fails closed.
    """

    expected_count = _validate_expected_count(expected_case_count)
    full, catalog, paths = _resolved_paths(
        full_structured_path=full_structured_path,
        label_catalog_path=label_catalog_path,
        cases_path=cases_path,
        private_mapping_path=private_mapping_path,
        manifest_path=manifest_path,
    )
    artifacts = _prepare_from_paths(
        full,
        catalog,
        expected_case_count=expected_count,
    )
    existence = {
        path: _path_lexists(path)
        for path in (paths.cases, paths.private_mapping, paths.manifest)
    }
    if any(existence.values()):
        if not all(existence.values()):
            present = sorted(str(path) for path, exists in existence.items() if exists)
            missing = sorted(
                str(path) for path, exists in existence.items() if not exists
            )
            raise CaseArtifactError(
                "Incomplete existing case artifact set; refusing to overwrite "
                "or repair; "
                f"present={present}, missing={missing}"
            )
        try:
            _verify_existing(artifacts, paths)
        except CaseArtifactError as error:
            raise CaseArtifactError(
                f"Existing case artifact set differs; refusing to overwrite: {error}"
            ) from error
        return _summary(artifacts, paths)

    _publish_new(artifacts, paths)
    return _summary(artifacts, paths)


def validate_case_artifacts(
    *,
    full_structured_path: str | Path,
    label_catalog_path: str | Path,
    cases_path: str | Path,
    private_mapping_path: str | Path,
    manifest_path: str | Path,
    expected_case_count: int = EXPECTED_CASE_COUNT,
) -> CaseBuildSummary:
    """Recompute and byte-verify an existing immutable artifact set."""

    expected_count = _validate_expected_count(expected_case_count)
    full, catalog, paths = _resolved_paths(
        full_structured_path=full_structured_path,
        label_catalog_path=label_catalog_path,
        cases_path=cases_path,
        private_mapping_path=private_mapping_path,
        manifest_path=manifest_path,
    )
    artifacts = _prepare_from_paths(
        full,
        catalog,
        expected_case_count=expected_count,
    )
    missing = [
        str(path)
        for path in (paths.cases, paths.private_mapping, paths.manifest)
        if not _path_lexists(path)
    ]
    if missing:
        raise CaseArtifactError("Missing case artifacts: " + ", ".join(missing))
    _verify_existing(artifacts, paths)
    loaded = load_prompt_cases(paths.cases)
    if len(loaded) != expected_count:
        raise CaseArtifactError(
            f"Prompt case count differs from expected {expected_count}"
        )
    return _summary(artifacts, paths)


def _config_case_paths(config_path: str | Path) -> tuple[Path, Path, CaseArtifactPaths]:
    config_file = Path(config_path).resolve()
    try:
        decoded = _decode_json(config_file.read_bytes(), location="benchmark config")
    except OSError as error:
        raise CaseArtifactError(f"Cannot read benchmark config: {error}") from error
    if not isinstance(decoded, Mapping):
        raise CaseArtifactError("Benchmark config must contain a JSON object")
    if decoded.get("schema_version") != "cofactor9.1.config.v1":
        raise CaseArtifactError("Benchmark config schema_version is unsupported")
    model = _required_mapping(decoded, "model", location="benchmark config")
    if model.get("prompt_version") != PROMPT_VERSION:
        raise CaseArtifactError(
            "Benchmark config prompt_version differs from the prompt implementation"
        )
    paths = _required_mapping(decoded, "paths", location="benchmark config")
    root = config_file.parent.parent.resolve()

    def resolve_configured(key: str) -> Path:
        configured = _required_string(paths, key, location="benchmark config.paths")
        path = Path(configured)
        if path.is_absolute():
            raise CaseArtifactError("Configured project paths must be relative")
        resolved = (root / path).resolve()
        if not resolved.is_relative_to(root):
            raise CaseArtifactError(
                f"Configured path escapes project root: {configured!r}"
            )
        return resolved

    public_paths = case_artifact_paths(resolve_configured("cases"))
    return (
        resolve_configured("full_structured"),
        resolve_configured("label_catalog"),
        public_paths,
    )


def build_cases_from_config(config_path: str | Path) -> CaseBuildSummary:
    """Build the frozen 5,337-case set using ``config/benchmark.json`` paths."""

    full, catalog, outputs = _config_case_paths(config_path)
    return build_case_artifacts(
        full_structured_path=full,
        label_catalog_path=catalog,
        cases_path=outputs.cases,
        private_mapping_path=outputs.private_mapping,
        manifest_path=outputs.manifest,
    )


def validate_cases_from_config(config_path: str | Path) -> CaseBuildSummary:
    """Validate the configured frozen 5,337-case artifact set."""

    full, catalog, outputs = _config_case_paths(config_path)
    return validate_case_artifacts(
        full_structured_path=full,
        label_catalog_path=catalog,
        cases_path=outputs.cases,
        private_mapping_path=outputs.private_mapping,
        manifest_path=outputs.manifest,
    )


__all__ = [
    "CASE_ARTIFACT_SCHEMA_VERSION",
    "EXPECTED_CASE_COUNT",
    "PRIVATE_MAPPING_MODE",
    "PRIVATE_MAPPING_PURPOSE",
    "PRIVATE_MAPPING_SCHEMA_VERSION",
    "PUBLIC_CASE_SCHEMA_VERSION",
    "CaseArtifactError",
    "CaseArtifactPaths",
    "CaseArtifacts",
    "CaseBuildSummary",
    "PrivateCaseMapping",
    "build_case_artifacts",
    "build_cases_from_config",
    "case_artifact_paths",
    "derive_case_artifacts",
    "load_prompt_cases",
    "validate_case_artifacts",
    "validate_cases_from_config",
]
