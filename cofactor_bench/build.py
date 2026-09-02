from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, TextIO

from .model import ParsedUniProtEntry
from .parser import EXPERIMENTAL_EVIDENCE_CODE, parse_uniprot_entry


MASTER_SCHEMA_VERSION = "cofactor9.1.master.v1"
SOURCE_CANDIDATE_SCHEMA_VERSION = "cofactor9.1.source-candidate.v1"
FORMULA_RULE_VERSION = "cofactor9.1.formula.v2"

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_METADATA_FIELDS = frozenset(
    {
        "release",
        "release_date",
        "api_deployment_date",
        "query",
        "url",
        "retrieved_at",
        "artifact_sha256",
        "decompressed_content_sha256",
        "artifact_bytes",
        "decompressed_content_bytes",
    }
)
_SOURCE_METADATA_REQUIRED = frozenset({"release", "query", "artifact_sha256"})


@dataclass(frozen=True)
class BuildSummary:
    source_record_count: int
    master_record_count: int
    unique_master_accession_count: int
    ineligible_record_count: int
    experimental_label_count: int
    evidence_status_counts: dict[str, int]
    experimental_label_cardinality_counts: dict[int, int]
    all_label_cardinality_counts: dict[int, int]
    formula_shape_counts: dict[str, int]
    experimental_occurrence_count: int
    unique_accession_experimental_label_count: int
    duplicate_experimental_occurrence_count: int
    canonical_block_count: int
    canonical_block_label_overlap_count: int
    canonical_block_label_overlap_accession_count: int
    sequence_with_u_count: int
    sequence_with_x_count: int
    missing_sequence_count: int
    sequence_length_mismatch_count: int
    missing_experimental_chebi_id_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_record_count": self.source_record_count,
            "master_record_count": self.master_record_count,
            "unique_master_accession_count": self.unique_master_accession_count,
            "ineligible_record_count": self.ineligible_record_count,
            "experimental_label_count": self.experimental_label_count,
            "evidence_status_counts": self.evidence_status_counts,
            "experimental_label_cardinality_counts": {
                str(key): value
                for key, value in self.experimental_label_cardinality_counts.items()
            },
            "all_label_cardinality_counts": {
                str(key): value for key, value in self.all_label_cardinality_counts.items()
            },
            "formula_shape_counts": self.formula_shape_counts,
            "experimental_occurrence_count": self.experimental_occurrence_count,
            "unique_accession_experimental_label_count": (
                self.unique_accession_experimental_label_count
            ),
            "duplicate_experimental_occurrence_count": (
                self.duplicate_experimental_occurrence_count
            ),
            "canonical_block_count": self.canonical_block_count,
            "canonical_block_label_overlap_count": (
                self.canonical_block_label_overlap_count
            ),
            "canonical_block_label_overlap_accession_count": (
                self.canonical_block_label_overlap_accession_count
            ),
            "sequence_with_u_count": self.sequence_with_u_count,
            "sequence_with_x_count": self.sequence_with_x_count,
            "missing_sequence_count": self.missing_sequence_count,
            "sequence_length_mismatch_count": self.sequence_length_mismatch_count,
            "missing_experimental_chebi_id_count": (
                self.missing_experimental_chebi_id_count
            ),
        }


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_and_size(handle: Any) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
        size += len(chunk)
    return (digest.hexdigest(), size)


def _load_json_object(path: Path, description: str) -> Mapping[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, Mapping):
        raise ValueError(f"{description} must contain a JSON object")
    return value


def _load_snapshot(path: Path) -> Sequence[Mapping[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else Path.open
    with opener(path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, Mapping):
        raise ValueError("UniProt snapshot must contain a JSON object")
    results = payload.get("results")
    if not isinstance(results, Sequence) or isinstance(results, str):
        raise ValueError("UniProt snapshot must contain a results array")
    if not all(isinstance(result, Mapping) for result in results):
        raise ValueError("Every UniProt result must be a JSON object")
    return results


def verify_source_manifest(config_path: str | Path) -> dict[str, Any]:
    """Verify frozen source bytes and cross-check recorded release metadata."""

    config_file = Path(config_path).resolve()
    project_root = config_file.parent.parent
    config = _load_json_object(config_file, "Benchmark configuration")
    if config.get("schema_version") != "cofactor9.1.config.v1":
        raise ValueError("Benchmark configuration schema_version is unsupported")
    dataset_version = _required_string(config, "dataset_version")
    paths = _required_mapping(config, "paths")
    source = _required_mapping(config, "source")
    uniprot_config = _required_mapping(source, "uniprot")
    chebi_config = _required_mapping(source, "chebi")
    manifest_path = _resolve_project_path(
        project_root,
        _required_string(paths, "source_manifest"),
    )
    manifest = _load_json_object(manifest_path, "Source manifest")
    if manifest.get("schema_version") != "cofactor9.1.source-manifest.v1":
        raise ValueError("Source manifest schema_version is unsupported")
    if manifest.get("dataset_version") != dataset_version:
        raise ValueError("Source manifest dataset_version differs from configuration")
    artifacts = _required_mapping(manifest, "artifacts")
    expected_path_keys = {
        "uniprot_json": "uniprot_raw",
        "uniprot_headers": "uniprot_headers",
        "chebi_lite_json": "chebi_raw",
    }
    artifact_sha256: dict[str, str] = {}
    decompressed_sha256: dict[str, str] = {}

    for artifact_name, config_path_key in expected_path_keys.items():
        artifact = _required_mapping(artifacts, artifact_name)
        recorded_path = _required_string(artifact, "path")
        configured_path = _required_string(paths, config_path_key)
        if recorded_path != configured_path:
            raise ValueError(
                f"Manifest path for {artifact_name} differs from configuration"
            )
        if not _required_string(artifact, "source_url").startswith("https://"):
            raise ValueError(f"Manifest source URL for {artifact_name} must use HTTPS")
        _required_string(artifact, "retrieved_at")
        path = _resolve_project_path(project_root, recorded_path)
        observed_sha256 = sha256_file(path)
        expected_sha256 = _required_string(artifact, "sha256")
        if observed_sha256 != expected_sha256:
            raise ValueError(
                f"SHA-256 mismatch for {artifact_name}: expected {expected_sha256}, "
                f"observed {observed_sha256}"
            )
        observed_bytes = path.stat().st_size
        expected_bytes = artifact.get("bytes")
        if observed_bytes != expected_bytes:
            raise ValueError(
                f"Byte-size mismatch for {artifact_name}: expected {expected_bytes}, "
                f"observed {observed_bytes}"
            )
        artifact_sha256[artifact_name] = observed_sha256

        expected_content_sha256 = artifact.get("decompressed_content_sha256")
        if expected_content_sha256 is not None:
            with gzip.open(path, "rb") as handle:
                observed_content_sha256, observed_content_bytes = _hash_and_size(handle)
            if observed_content_sha256 != expected_content_sha256:
                raise ValueError(
                    f"Decompressed SHA-256 mismatch for {artifact_name}: expected "
                    f"{expected_content_sha256}, observed {observed_content_sha256}"
                )
            expected_content_bytes = artifact.get("decompressed_content_bytes")
            if observed_content_bytes != expected_content_bytes:
                raise ValueError(
                    f"Decompressed byte-size mismatch for {artifact_name}: expected "
                    f"{expected_content_bytes}, observed {observed_content_bytes}"
                )
            decompressed_sha256[artifact_name] = observed_content_sha256

    uniprot_artifact = _required_mapping(artifacts, "uniprot_json")
    chebi_artifact = _required_mapping(artifacts, "chebi_lite_json")
    uniprot_release = _required_string(uniprot_artifact, "release")
    chebi_version = _required_string(chebi_artifact, "ontology_version")
    if uniprot_release != _required_string(uniprot_config, "release"):
        raise ValueError("UniProt release differs between config and source manifest")
    if _required_string(uniprot_artifact, "query") != _required_string(
        uniprot_config, "query"
    ):
        raise ValueError("UniProt query differs between config and source manifest")
    if chebi_version != _required_string(chebi_config, "version"):
        raise ValueError("ChEBI version differs between config and source manifest")

    aligned_fields = (
        (uniprot_artifact, uniprot_config, "source_url", "url"),
        (uniprot_artifact, uniprot_config, "retrieved_at", "retrieved_at"),
        (uniprot_artifact, uniprot_config, "sha256", "artifact_sha256"),
        (uniprot_artifact, uniprot_config, "bytes", "artifact_bytes"),
        (
            uniprot_artifact,
            uniprot_config,
            "decompressed_content_sha256",
            "decompressed_content_sha256",
        ),
        (
            uniprot_artifact,
            uniprot_config,
            "decompressed_content_bytes",
            "decompressed_content_bytes",
        ),
        (chebi_artifact, chebi_config, "source_url", "url"),
        (chebi_artifact, chebi_config, "retrieved_at", "retrieved_at"),
        (chebi_artifact, chebi_config, "sha256", "artifact_sha256"),
        (chebi_artifact, chebi_config, "bytes", "artifact_bytes"),
        (
            chebi_artifact,
            chebi_config,
            "decompressed_content_sha256",
            "decompressed_content_sha256",
        ),
        (
            chebi_artifact,
            chebi_config,
            "decompressed_content_bytes",
            "decompressed_content_bytes",
        ),
    )
    for artifact, configured, artifact_key, config_key in aligned_fields:
        if artifact.get(artifact_key) != configured.get(config_key):
            raise ValueError(
                f"Source field {artifact_key!r} differs between config and manifest"
            )

    expected_chebi_values = (
        "ChEBI Ontology",
        _required_string(chebi_artifact, "ontology_date_raw"),
        chebi_version,
        _required_string(chebi_artifact, "version_iri"),
    )
    chebi_path = _resolve_project_path(
        project_root,
        _required_string(paths, "chebi_raw"),
    )
    with gzip.open(chebi_path, "rt", encoding="utf-8") as handle:
        chebi_header = handle.read(16384)
    if not all(value in chebi_header for value in expected_chebi_values):
        raise ValueError("ChEBI artifact header does not match recorded ontology metadata")

    header_path = _resolve_project_path(
        project_root,
        _required_string(paths, "uniprot_headers"),
    )
    header_lines = header_path.read_text(encoding="utf-8").splitlines()
    header_values = {
        key.strip().lower(): value.strip()
        for line in header_lines
        if ":" in line
        for key, value in [line.split(":", 1)]
    }
    if header_values.get("x-uniprot-release") != uniprot_release:
        raise ValueError("UniProt response header does not match frozen release")
    for header_key, config_key in (
        ("x-uniprot-release-date", "release_date"),
        ("x-api-deployment-date", "api_deployment_date"),
    ):
        raw_date = header_values.get(header_key)
        try:
            normalized_date = datetime.strptime(
                raw_date or "",
                "%d-%B-%Y",
            ).date().isoformat()
        except ValueError as error:
            raise ValueError(f"UniProt response header {header_key} is invalid") from error
        if normalized_date != _required_string(uniprot_config, config_key):
            raise ValueError(
                f"UniProt response header {header_key} differs from configuration"
            )

    return {
        "verified_artifact_count": len(expected_path_keys),
        "artifact_sha256": artifact_sha256,
        "decompressed_content_sha256": decompressed_sha256,
        "uniprot_release": uniprot_release,
        "chebi_version": chebi_version,
        "uniprot_source_metadata": {
            "release": uniprot_release,
            "release_date": _required_string(uniprot_config, "release_date"),
            "api_deployment_date": _required_string(
                uniprot_config,
                "api_deployment_date",
            ),
            "query": _required_string(uniprot_artifact, "query"),
            "url": _required_string(uniprot_artifact, "source_url"),
            "retrieved_at": _required_string(uniprot_artifact, "retrieved_at"),
            "artifact_sha256": _required_string(uniprot_artifact, "sha256"),
            "decompressed_content_sha256": _required_string(
                uniprot_artifact,
                "decompressed_content_sha256",
            ),
            "artifact_bytes": uniprot_artifact["bytes"],
            "decompressed_content_bytes": uniprot_artifact[
                "decompressed_content_bytes"
            ],
        },
    }


def _source_object(
    source_metadata: Mapping[str, Any],
    raw_record_index: int,
) -> dict[str, Any]:
    return {**source_metadata, "raw_record_index": raw_record_index}


def _entry_object(parsed: ParsedUniProtEntry) -> dict[str, Any]:
    return {
        "accession": parsed.accession,
        "uniprot_id": parsed.uniprot_id,
        "entry_type": parsed.entry_type,
        "entry_version": parsed.entry_version,
        "sequence_version": parsed.sequence_version,
        "last_annotation_update_date": parsed.last_annotation_update_date,
        "organism": {
            "scientific_name": parsed.organism_scientific_name,
            "taxon_id": parsed.organism_taxon_id,
        },
        "ec_numbers": list(parsed.ec_numbers),
    }


def _sequence_object(parsed: ParsedUniProtEntry) -> dict[str, Any]:
    actual_length = (
        len(parsed.sequence_value) if parsed.sequence_value is not None else None
    )
    return {
        "value": parsed.sequence_value,
        "length": actual_length,
        "reported_length": parsed.reported_sequence_length,
        "length_matches_reported": (
            actual_length == parsed.reported_sequence_length
            if actual_length is not None and parsed.reported_sequence_length is not None
            else None
        ),
        "crc64": parsed.sequence_crc64,
        "sha256": parsed.sequence_sha256,
        "alphabet_status": parsed.alphabet_status.value,
        "nonstandard_symbols": list(parsed.nonstandard_symbols),
    }


def _master_row(
    parsed: ParsedUniProtEntry,
    *,
    dataset_version: str,
    source_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": MASTER_SCHEMA_VERSION,
        "dataset_version": dataset_version,
        "source": _source_object(source_metadata, parsed.raw_record_index),
        "entry": _entry_object(parsed),
        "sequence": _sequence_object(parsed),
        "cofactor_blocks": [block.to_dict() for block in parsed.cofactor_blocks],
        "derived": {
            "experimental_label_ids": list(parsed.experimental_label_ids),
            "all_cofactor_label_ids": list(parsed.all_cofactor_label_ids),
            "gold_formula": [list(block) for block in parsed.gold_formula],
            "formula_shape": parsed.formula_shape.value,
            "evidence_status": (
                parsed.evidence_status.value if parsed.evidence_status else None
            ),
            "experimental_occurrence_count": parsed.experimental_occurrence_count,
            "canonical_block_count": len(parsed.gold_formula),
            "reason_codes": (
                ["OVERLAPPING_BLOCK_LABEL"]
                if _canonical_overlap_count(parsed)
                else []
            ),
            "memberships": ["MASTER_5337"],
        },
        "adjudication": {
            "status": "UNASSESSED",
            "decision": None,
            "reviewer": None,
            "rationale": None,
            "evidence_occurrence_ids": [],
            "rule_version": None,
        },
    }


def _eligibility_reasons(parsed: ParsedUniProtEntry) -> list[str]:
    if parsed.eligible_for_master:
        return ["HAS_ACCEPTED_EXPERIMENTAL_COFACTOR"]
    reasons = ["NO_ACCEPTED_EXPERIMENTAL_COFACTOR"]
    evidences = [
        evidence
        for block in parsed.cofactor_blocks
        for occurrence in block.label_occurrences
        for evidence in occurrence.evidences
    ]
    if any(
        evidence.evidence_code == EXPERIMENTAL_EVIDENCE_CODE
        and evidence.resolution_status == "UNRESOLVED_REFERENCE"
        for evidence in evidences
    ):
        reasons.append("UNRESOLVED_REFERENCE_EVIDENCE")
    return reasons


def _source_candidate_row(
    parsed: ParsedUniProtEntry,
    *,
    dataset_version: str,
    source_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SOURCE_CANDIDATE_SCHEMA_VERSION,
        "dataset_version": dataset_version,
        "source": _source_object(source_metadata, parsed.raw_record_index),
        "entry": {
            "accession": parsed.accession,
            "uniprot_id": parsed.uniprot_id,
        },
        "eligible_for_master": parsed.eligible_for_master,
        "eligibility_reasons": _eligibility_reasons(parsed),
        "experimental_label_ids": list(parsed.experimental_label_ids),
        "all_cofactor_label_ids": list(parsed.all_cofactor_label_ids),
        "experimental_occurrence_count": parsed.experimental_occurrence_count,
    }


def _write_json_line(handle: TextIO, value: Mapping[str, Any]) -> None:
    handle.write(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    handle.write("\n")


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    temporary, handle = _temporary_output(path)
    try:
        _write_json_line(handle, value)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(temporary, path)
    except BaseException:
        handle.close()
        temporary.unlink(missing_ok=True)
        raise


def _temporary_output(target: Path) -> tuple[Path, TextIO]:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
        delete=False,
    )
    return (Path(handle.name), handle)


def _canonical_overlap_count(parsed: ParsedUniProtEntry) -> int:
    blocks = [set(block) for block in parsed.gold_formula]
    return sum(
        bool(left & right)
        for left_index, left in enumerate(blocks)
        for right in blocks[left_index + 1 :]
    )


def _accepted_evidence_without_chebi_count(parsed: ParsedUniProtEntry) -> int:
    return sum(
        1
        for block in parsed.cofactor_blocks
        for occurrence in block.label_occurrences
        if occurrence.chebi_id is None
        and any(
            evidence.accepted_for_experimental for evidence in occurrence.evidences
        )
    )


def _validate_expected_foundation(
    summary: BuildSummary,
    expected: Mapping[str, Any],
) -> None:
    observed = summary.to_dict()
    mismatches = [
        f"{key}: expected {expected_value!r}, observed {observed.get(key)!r}"
        for key, expected_value in expected.items()
        if key not in observed or observed[key] != expected_value
    ]
    if mismatches:
        raise ValueError("Foundation invariant mismatch: " + "; ".join(mismatches))


def _normalize_source_metadata(source_metadata: Mapping[str, Any]) -> dict[str, Any]:
    actual = frozenset(source_metadata)
    missing = sorted(_SOURCE_METADATA_REQUIRED - actual)
    unknown = sorted(str(key) for key in actual - _SOURCE_METADATA_FIELDS)
    if missing or unknown:
        raise ValueError(
            "Source provenance fields do not match the allowlist; "
            f"missing={missing}, unknown={unknown}"
        )
    normalized = dict(source_metadata)
    for key in _SOURCE_METADATA_REQUIRED:
        value = normalized[key]
        if not isinstance(value, str) or not value:
            raise ValueError(f"Source provenance field {key!r} must be nonempty")
    for key in (
        "release_date",
        "api_deployment_date",
        "url",
        "retrieved_at",
        "decompressed_content_sha256",
    ):
        value = normalized.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError(f"Source provenance field {key!r} must be a string")
    for key in ("artifact_bytes", "decompressed_content_bytes"):
        value = normalized.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ValueError(
                f"Source provenance field {key!r} must be a nonnegative integer"
            )
    for key in ("artifact_sha256", "decompressed_content_sha256"):
        value = normalized.get(key)
        if value is not None and _SHA256.fullmatch(value) is None:
            raise ValueError(f"Source provenance field {key!r} is not SHA-256")
    if "url" in normalized and not normalized["url"].startswith("https://"):
        raise ValueError("Source provenance URL must use HTTPS")
    return normalized


def _backup_file(target: Path) -> Path | None:
    if not target.exists():
        return None
    descriptor, name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".rollback",
        dir=target.parent,
    )
    os.close(descriptor)
    backup = Path(name)
    shutil.copy2(target, backup)
    return backup


def _replace_output_pair(
    master_temporary: Path,
    master_target: Path,
    source_temporary: Path,
    source_target: Path,
) -> None:
    """Replace a coordinated output pair and restore both on ordinary failure."""

    backups = {
        master_target: _backup_file(master_target),
        source_target: _backup_file(source_target),
    }
    try:
        os.replace(master_temporary, master_target)
        os.replace(source_temporary, source_target)
    except BaseException:
        rollback_errors: list[BaseException] = []
        for target, backup in backups.items():
            try:
                if backup is None:
                    target.unlink(missing_ok=True)
                else:
                    os.replace(backup, target)
            except BaseException as error:
                rollback_errors.append(error)
        if rollback_errors:
            raise RuntimeError(
                "Foundation output replacement and rollback both failed"
            ) from rollback_errors[0]
        raise
    finally:
        for backup in backups.values():
            if backup is not None:
                backup.unlink(missing_ok=True)


def build_snapshot(
    *,
    raw_path: str | Path,
    master_path: str | Path,
    source_candidates_path: str | Path,
    dataset_version: str,
    source_metadata: Mapping[str, Any],
    expected_foundation: Mapping[str, Any] | None = None,
    generation_manifest_path: str | Path | None = None,
) -> BuildSummary:
    """Build deterministic source-audit and Master JSONL artifacts."""

    raw = Path(raw_path).resolve()
    master_target = Path(master_path).resolve()
    source_target = Path(source_candidates_path).resolve()
    if len({raw, master_target, source_target}) != 3:
        raise ValueError("Raw input and both output paths must be distinct")
    provenance = _normalize_source_metadata(source_metadata)
    expected_sha256 = provenance["artifact_sha256"]
    actual_sha256 = sha256_file(raw)
    if expected_sha256 != actual_sha256:
        raise ValueError(
            f"UniProt artifact SHA-256 mismatch: expected {expected_sha256}, "
            f"observed {actual_sha256}"
        )

    results = _load_snapshot(raw)
    master_temporary, master_handle = _temporary_output(master_target)
    source_temporary, source_handle = _temporary_output(source_target)

    master_count = 0
    accessions: set[str] = set()
    experimental_labels: set[str] = set()
    evidence_status_counts: Counter[str] = Counter()
    experimental_cardinality_counts: Counter[int] = Counter()
    all_cardinality_counts: Counter[int] = Counter()
    formula_shape_counts: Counter[str] = Counter()
    experimental_occurrence_count = 0
    unique_accession_label_count = 0
    canonical_block_count = 0
    canonical_overlap_count = 0
    canonical_overlap_accession_count = 0
    sequence_with_u_count = 0
    sequence_with_x_count = 0
    missing_sequence_count = 0
    sequence_length_mismatch_count = 0
    missing_experimental_chebi_id_count = 0

    try:
        for raw_record_index, raw_entry in enumerate(results):
            parsed = parse_uniprot_entry(
                raw_entry,
                raw_record_index=raw_record_index,
            )
            _write_json_line(
                source_handle,
                _source_candidate_row(
                    parsed,
                    dataset_version=dataset_version,
                    source_metadata=provenance,
                ),
            )
            if not parsed.eligible_for_master:
                continue

            formula_label_union = {
                label for block in parsed.gold_formula for label in block
            }
            if formula_label_union != set(parsed.experimental_label_ids):
                raise ValueError(
                    "Gold formula label union differs from experimental labels for "
                    f"{parsed.accession}"
                )

            _write_json_line(
                master_handle,
                _master_row(
                    parsed,
                    dataset_version=dataset_version,
                    source_metadata=provenance,
                ),
            )
            master_count += 1
            accessions.add(parsed.accession)
            experimental_labels.update(parsed.experimental_label_ids)
            if parsed.evidence_status is not None:
                evidence_status_counts[parsed.evidence_status.value] += 1
            experimental_cardinality_counts[len(parsed.experimental_label_ids)] += 1
            all_cardinality_counts[len(parsed.all_cofactor_label_ids)] += 1
            formula_shape_counts[parsed.formula_shape.value] += 1
            experimental_occurrence_count += parsed.experimental_occurrence_count
            unique_accession_label_count += len(parsed.experimental_label_ids)
            canonical_block_count += len(parsed.gold_formula)
            accession_overlap_count = _canonical_overlap_count(parsed)
            canonical_overlap_count += accession_overlap_count
            canonical_overlap_accession_count += bool(accession_overlap_count)
            sequence = parsed.sequence_value
            sequence_with_u_count += bool(sequence and "U" in sequence)
            sequence_with_x_count += bool(sequence and "X" in sequence)
            missing_sequence_count += sequence is None
            if sequence is not None:
                sequence_length_mismatch_count += (
                    parsed.reported_sequence_length is None
                    or len(sequence) != parsed.reported_sequence_length
                )
            missing_experimental_chebi_id_count += (
                _accepted_evidence_without_chebi_count(parsed)
            )

        summary = BuildSummary(
            source_record_count=len(results),
            master_record_count=master_count,
            unique_master_accession_count=len(accessions),
            ineligible_record_count=len(results) - master_count,
            experimental_label_count=len(experimental_labels),
            evidence_status_counts=dict(sorted(evidence_status_counts.items())),
            experimental_label_cardinality_counts=dict(
                sorted(experimental_cardinality_counts.items())
            ),
            all_label_cardinality_counts=dict(sorted(all_cardinality_counts.items())),
            formula_shape_counts=dict(sorted(formula_shape_counts.items())),
            experimental_occurrence_count=experimental_occurrence_count,
            unique_accession_experimental_label_count=unique_accession_label_count,
            duplicate_experimental_occurrence_count=(
                experimental_occurrence_count - unique_accession_label_count
            ),
            canonical_block_count=canonical_block_count,
            canonical_block_label_overlap_count=canonical_overlap_count,
            canonical_block_label_overlap_accession_count=(
                canonical_overlap_accession_count
            ),
            sequence_with_u_count=sequence_with_u_count,
            sequence_with_x_count=sequence_with_x_count,
            missing_sequence_count=missing_sequence_count,
            sequence_length_mismatch_count=sequence_length_mismatch_count,
            missing_experimental_chebi_id_count=missing_experimental_chebi_id_count,
        )
        if expected_foundation is not None:
            _validate_expected_foundation(summary, expected_foundation)

        master_handle.flush()
        source_handle.flush()
        os.fsync(master_handle.fileno())
        os.fsync(source_handle.fileno())
        master_handle.close()
        source_handle.close()
        _replace_output_pair(
            master_temporary,
            master_target,
            source_temporary,
            source_target,
        )
        if generation_manifest_path is not None:
            _write_json_atomic(
                Path(generation_manifest_path).resolve(),
                {
                    "schema_version": "cofactor9.1.foundation-generation.v1",
                    "dataset_version": dataset_version,
                    "formula_rule_version": FORMULA_RULE_VERSION,
                    "source_artifact_sha256": expected_sha256,
                    "master": {
                        "sha256": sha256_file(master_target),
                        "bytes": master_target.stat().st_size,
                        "record_count": summary.master_record_count,
                    },
                    "source_candidates": {
                        "sha256": sha256_file(source_target),
                        "bytes": source_target.stat().st_size,
                        "record_count": summary.source_record_count,
                    },
                },
            )
    except BaseException:
        master_handle.close()
        source_handle.close()
        master_temporary.unlink(missing_ok=True)
        source_temporary.unlink(missing_ok=True)
        raise

    return summary


def _required_mapping(container: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = container.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"Configuration field {key!r} must be an object")
    return value


def _required_string(container: Mapping[str, Any], key: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Configuration field {key!r} must be a non-empty string")
    return value


def _resolve_project_path(project_root: Path, configured_path: str) -> Path:
    path = Path(configured_path)
    if path.is_absolute():
        raise ValueError("Configured project paths must be relative")
    root = project_root.resolve()
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"Configured path escapes project root: {configured_path!r}")
    return resolved


def build_from_config(config_path: str | Path) -> BuildSummary:
    """Build the foundation artifacts described by ``config/benchmark.json``."""

    config_file = Path(config_path).resolve()
    with config_file.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, Mapping):
        raise ValueError("Benchmark configuration must contain a JSON object")

    project_root = config_file.parent.parent
    paths = _required_mapping(config, "paths")
    source = _required_mapping(config, "source")
    uniprot = _required_mapping(source, "uniprot")
    dataset_version = _required_string(config, "dataset_version")
    expected_foundation = config.get("expected_foundation")
    if expected_foundation is not None and not isinstance(
        expected_foundation, Mapping
    ):
        raise ValueError("Configuration field 'expected_foundation' must be an object")

    verification = verify_source_manifest(config_file)

    return build_snapshot(
        raw_path=_resolve_project_path(
            project_root,
            _required_string(paths, "uniprot_raw"),
        ),
        master_path=_resolve_project_path(
            project_root,
            _required_string(paths, "master"),
        ),
        source_candidates_path=_resolve_project_path(
            project_root,
            _required_string(paths, "source_candidates"),
        ),
        dataset_version=dataset_version,
        source_metadata=verification["uniprot_source_metadata"],
        expected_foundation=expected_foundation,
        generation_manifest_path=_resolve_project_path(
            project_root,
            _required_string(paths, "foundation_generation"),
        ),
    )


def verify_foundation_generation(config_path: str | Path) -> dict[str, Any]:
    """Reject a mixed or tampered Master/SourceCandidates output generation."""

    config_file = Path(config_path).resolve()
    config = _load_json_object(config_file, "Benchmark configuration")
    project_root = config_file.parent.parent
    paths = _required_mapping(config, "paths")
    generation_path = _resolve_project_path(
        project_root,
        _required_string(paths, "foundation_generation"),
    )
    generation = _load_json_object(generation_path, "Foundation generation manifest")
    if generation.get("schema_version") != "cofactor9.1.foundation-generation.v1":
        raise ValueError("Foundation generation schema_version is unsupported")
    if generation.get("dataset_version") != config.get("dataset_version"):
        raise ValueError("Foundation generation dataset_version is inconsistent")
    if generation.get("formula_rule_version") != FORMULA_RULE_VERSION:
        raise ValueError("Foundation generation formula_rule_version is inconsistent")

    observed: dict[str, dict[str, Any]] = {}
    for generation_key, config_key in (
        ("master", "master"),
        ("source_candidates", "source_candidates"),
    ):
        artifact = _required_mapping(generation, generation_key)
        path = _resolve_project_path(
            project_root,
            _required_string(paths, config_key),
        )
        digest = sha256_file(path)
        size = path.stat().st_size
        with path.open("rb") as handle:
            line_count = sum(1 for _ in handle)
        if digest != artifact.get("sha256"):
            raise ValueError(f"Foundation {generation_key} SHA-256 mismatch")
        if size != artifact.get("bytes"):
            raise ValueError(f"Foundation {generation_key} byte-size mismatch")
        if line_count != artifact.get("record_count"):
            raise ValueError(f"Foundation {generation_key} record-count mismatch")
        observed[generation_key] = {
            "sha256": digest,
            "bytes": size,
            "record_count": line_count,
        }
    return {
        "schema_version": generation["schema_version"],
        "dataset_version": generation["dataset_version"],
        "formula_rule_version": generation["formula_rule_version"],
        "artifacts": observed,
    }
