"""Deterministic benchmark views and audits derived from Master-5337."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

from .duplicates import analyze_exact_sequence_groups
from .ontology import analyze_target_ancestry
from .triage import classify_alphabet, triage_record


VIEW_RECORD_SCHEMA_VERSION = "cofactor9.1.view-record.v1"
LABEL_CATALOG_SCHEMA_VERSION = "cofactor9.1.label-catalog.v1"
ONTOLOGY_AUDIT_SCHEMA_VERSION = "cofactor9.1.ontology-audit.v1"
VIEW_AUDIT_SCHEMA_VERSION = "cofactor9.1.view-audit.v1"
RULE_VERSION = "cofactor9.1.views.v3"
CATALOG_VERSION = "cofactor9.1.allowed-labels.v1"

_CHEBI_ID = re.compile(r"CHEBI:([1-9][0-9]*)\Z")
_CHEBI_IRI = re.compile(r"http://purl\.obolibrary\.org/obo/CHEBI_(\d+)\Z")
_VIEW_NAMES = (
    "full_structured",
    "single_clean",
    "core_provisional",
    "ambiguity_challenge",
)
_REQUIRED_OUTPUTS = frozenset(
    {
        *_VIEW_NAMES,
        "label_catalog",
        "ontology_audit",
        "view_audit",
        "report",
    }
)

_FORMULA_REASON = {
    "PURE_OR": "PURE_OR_FORMULA",
    "PURE_AND": "PURE_AND_FORMULA",
    "MIXED_AND_OR": "MIXED_AND_OR_FORMULA",
}
_NOTE_REASON_PREFIX = "NOTE_"
_CHALLENGE_REASON_CODES = frozenset(
    {
        "EXTRA_NONEXPERIMENTAL_LABEL",
        "PURE_OR_FORMULA",
        "PURE_AND_FORMULA",
        "MIXED_AND_OR_FORMULA",
        "OVERLAPPING_BLOCK_LABEL",
        "DUPLICATE_EXPERIMENTAL_LABEL_OCCURRENCE",
        "ONTOLOGY_ANCESTOR_TARGET",
        "MOLECULE_SCOPE",
        "UNKNOWN_RESIDUE_X",
        "EXACT_SEQUENCE_LABEL_CONFLICT",
        "UNRESOLVED_REFERENCE_EVIDENCE",
        "ANNOTATION_CONTRADICTION",
    }
)

_FROZEN_EXPECTED = {
    "full_structured_accessions": 5337,
    "single_clean_accessions": 3971,
    "single_clean_sequence_entities": 3942,
    "single_clean_labels": 64,
    "core_provisional_accessions": 3233,
    "ambiguity_challenge_accessions": 2082,
    "ontology_ancestor_pairs": 59,
    "ontology_overlap_terms": 44,
    "ontology_ancestor_targets": 11,
    "ontology_missing_targets": 0,
    "sequence_entities": 5295,
    "duplicate_groups": 39,
    "duplicate_entries": 81,
    "conflict_groups": 6,
    "conflict_entries": 12,
    "canonical_block_count": 5911,
    "canonical_block_label_overlap_count": 8,
    "overlapping_block_label_accession_count": 6,
    "note_pending_single_clean": 477,
    "label_count": 104,
    "frequency_band_counts": {"head": 14, "mid": 20, "tail": 70},
}
_FROZEN_DIAGNOSTICS = {
    "experimental_single_accessions": 4179,
    "experimental_single_experimental_block_notes_pending": 538,
    "experimental_single_all_block_notes_pending": 560,
    "experimental_single_experimental_block_notes_or_molecule_scope_pending": 542,
}


@dataclass(frozen=True, slots=True)
class DerivedViewArtifacts:
    """In-memory, deterministic products of the view derivation rules."""

    full_structured: tuple[dict[str, Any], ...]
    single_clean: tuple[dict[str, Any], ...]
    core_provisional: tuple[dict[str, Any], ...]
    ambiguity_challenge: tuple[dict[str, Any], ...]
    label_catalog: dict[str, Any]
    ontology_audit: dict[str, Any]
    view_audit: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ViewBuildSummary:
    """Compact verification summary returned after all outputs are published."""

    full_structured_accessions: int
    single_clean_accessions: int
    single_clean_sequence_entities: int
    single_clean_labels: int
    core_provisional_accessions: int
    ambiguity_challenge_accessions: int
    ontology_ancestor_pairs: int
    ontology_overlap_terms: int
    ontology_ancestor_targets: int
    ontology_missing_targets: int
    sequence_entities: int
    duplicate_groups: int
    duplicate_entries: int
    conflict_groups: int
    conflict_entries: int
    canonical_block_count: int
    canonical_block_label_overlap_count: int
    overlapping_block_label_accession_count: int
    note_pending_single_clean: int
    label_count: int
    frequency_band_counts: dict[str, int]
    output_sha256: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "full_structured_accessions": self.full_structured_accessions,
            "single_clean_accessions": self.single_clean_accessions,
            "single_clean_sequence_entities": self.single_clean_sequence_entities,
            "single_clean_labels": self.single_clean_labels,
            "core_provisional_accessions": self.core_provisional_accessions,
            "ambiguity_challenge_accessions": (
                self.ambiguity_challenge_accessions
            ),
            "ontology_ancestor_pairs": self.ontology_ancestor_pairs,
            "ontology_overlap_terms": self.ontology_overlap_terms,
            "ontology_ancestor_targets": self.ontology_ancestor_targets,
            "ontology_missing_targets": self.ontology_missing_targets,
            "sequence_entities": self.sequence_entities,
            "duplicate_groups": self.duplicate_groups,
            "duplicate_entries": self.duplicate_entries,
            "conflict_groups": self.conflict_groups,
            "conflict_entries": self.conflict_entries,
            "canonical_block_count": self.canonical_block_count,
            "canonical_block_label_overlap_count": (
                self.canonical_block_label_overlap_count
            ),
            "overlapping_block_label_accession_count": (
                self.overlapping_block_label_accession_count
            ),
            "note_pending_single_clean": self.note_pending_single_clean,
            "label_count": self.label_count,
            "frequency_band_counts": self.frequency_band_counts,
            "output_sha256": self.output_sha256,
        }


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decompressed_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    opener = gzip.open if path.suffix == ".gz" else Path.open
    with opener(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _numeric_chebi_key(chebi_id: str) -> int:
    match = _CHEBI_ID.fullmatch(chebi_id)
    if match is None:
        raise ValueError(f"Invalid ChEBI identifier {chebi_id!r}")
    return int(match.group(1))


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


def _jsonl_bytes(records: Sequence[Mapping[str, Any]]) -> bytes:
    return "".join(
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for record in records
    ).encode("utf-8")


def _load_master(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(
                    f"Master line {line_number} must contain a JSON object"
                )
            records.append(value)
    return records


def _load_chebi_graph(path: Path) -> dict[str, Any]:
    opener = gzip.open if path.suffix == ".gz" else Path.open
    with opener(path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, Mapping):
        raise ValueError("ChEBI artifact must contain a JSON object")
    graphs = payload.get("graphs")
    if (
        not isinstance(graphs, Sequence)
        or isinstance(graphs, (str, bytes))
        or len(graphs) != 1
        or not isinstance(graphs[0], dict)
    ):
        raise ValueError("ChEBI artifact must contain exactly one graph")
    return graphs[0]


def _entry_accession(record: Mapping[str, Any]) -> str:
    entry = record.get("entry")
    if not isinstance(entry, Mapping):
        raise ValueError("Every Master record must contain an entry object")
    accession = entry.get("accession")
    if not isinstance(accession, str) or not accession:
        raise ValueError("Every Master record must contain an accession")
    return accession


def _derived(record: Mapping[str, Any]) -> Mapping[str, Any]:
    value = record.get("derived")
    if not isinstance(value, Mapping):
        raise ValueError(
            f"Master record {_entry_accession(record)!r} lacks derived data"
        )
    return value


def _label_ids(record: Mapping[str, Any], field: str) -> tuple[str, ...]:
    value = _derived(record).get(field)
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or not all(isinstance(item, str) for item in value)
    ):
        raise ValueError(
            f"Master record {_entry_accession(record)!r} has invalid {field}"
        )
    return tuple(value)


def _gold_formula(record: Mapping[str, Any]) -> tuple[tuple[str, ...], ...]:
    value = _derived(record).get("gold_formula")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(
            f"Master record {_entry_accession(record)!r} has invalid gold_formula"
        )
    formula: list[tuple[str, ...]] = []
    for block in value:
        if (
            not isinstance(block, Sequence)
            or isinstance(block, (str, bytes))
            or not block
            or not all(isinstance(label, str) for label in block)
        ):
            raise ValueError(
                f"Master record {_entry_accession(record)!r} has invalid gold_formula"
            )
        labels = tuple(block)
        if len(labels) != len(set(labels)):
            raise ValueError(
                f"Master record {_entry_accession(record)!r} repeats a label "
                "within a gold_formula block"
            )
        formula.append(labels)
    formula_union = {label for block in formula for label in block}
    if formula_union != set(_label_ids(record, "experimental_label_ids")):
        raise ValueError(
            f"Master record {_entry_accession(record)!r} gold_formula label union "
            "differs from experimental_label_ids"
        )
    return tuple(formula)


def _formula_overlap_count(formula: Sequence[Sequence[str]]) -> int:
    blocks = [set(block) for block in formula]
    return sum(
        bool(left & right)
        for index, left in enumerate(blocks)
        for right in blocks[index + 1 :]
    )


def _blocks(record: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    value = record.get("cofactor_blocks")
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or not all(isinstance(item, Mapping) for item in value)
    ):
        raise ValueError(
            f"Master record {_entry_accession(record)!r} has invalid cofactor blocks"
        )
    return value


def _experimental_blocks(
    record: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        block
        for block in _blocks(record)
        if any(
            occurrence.get("experimental") is True
            for occurrence in block.get("label_occurrences", [])
            if isinstance(occurrence, Mapping)
        )
    )


def _notes_from_blocks(blocks: Iterable[Mapping[str, Any]]) -> str:
    values: list[str] = []
    for block in blocks:
        notes = block.get("notes", [])
        if not isinstance(notes, Sequence) or isinstance(notes, (str, bytes)):
            continue
        for note in notes:
            if not isinstance(note, Mapping):
                continue
            value = note.get("value")
            if isinstance(value, str) and value:
                values.append(value)
    return "\n".join(values)


def _molecules_from_blocks(blocks: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    return tuple(
        molecule
        for block in blocks
        if isinstance((molecule := block.get("molecule")), str) and molecule
    )


def _note_triage(label_ids: Sequence[str], note: str) -> dict[str, Any]:
    reasons: set[str] = set()
    for label_id in label_ids:
        result = triage_record(label_id=label_id, note=note)
        reasons.update(result["reason_codes"])
    ordered = sorted(reasons)
    return {
        "status": "PENDING" if ordered else "NOT_REQUIRED",
        "reason_codes": ordered,
        "rule_version": RULE_VERSION,
    }


def _has_unresolved_reference(record: Mapping[str, Any]) -> bool:
    return any(
        evidence.get("resolution_status") == "UNRESOLVED_REFERENCE"
        for block in _blocks(record)
        for occurrence in block.get("label_occurrences", [])
        if isinstance(occurrence, Mapping)
        for evidence in occurrence.get("evidences", [])
        if isinstance(evidence, Mapping)
    )


def _ontology_target_names(
    graph: Mapping[str, Any],
    target_ids: Sequence[str],
) -> tuple[dict[str, str], dict[str, Any]]:
    target_set = set(target_ids)
    target_nodes: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    nodes = graph.get("nodes", [])
    if not isinstance(nodes, Sequence) or isinstance(nodes, (str, bytes)):
        raise ValueError("ChEBI graph nodes must be an array")
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        identifier = node.get("id")
        if not isinstance(identifier, str):
            continue
        match = _CHEBI_IRI.fullmatch(identifier)
        if match is None:
            continue
        chebi_id = f"CHEBI:{match.group(1)}"
        if chebi_id in target_set:
            target_nodes[chebi_id].append(node)

    missing_nodes = [chebi_id for chebi_id in target_ids if not target_nodes[chebi_id]]
    duplicate_nodes = [
        chebi_id for chebi_id in target_ids if len(target_nodes[chebi_id]) > 1
    ]
    missing_labels = [
        chebi_id
        for chebi_id in target_ids
        if len(target_nodes[chebi_id]) == 1
        and (
            not isinstance(target_nodes[chebi_id][0].get("lbl"), str)
            or not target_nodes[chebi_id][0].get("lbl")
        )
    ]
    deprecated = [
        chebi_id
        for chebi_id in target_ids
        if any(
            isinstance(node.get("meta"), Mapping)
            and node["meta"].get("deprecated") is True
            for node in target_nodes[chebi_id]
        )
    ]
    if missing_nodes:
        raise ValueError("Found missing ChEBI target nodes: " + ", ".join(missing_nodes))
    if duplicate_nodes:
        raise ValueError(
            "Found duplicate ChEBI target nodes: " + ", ".join(duplicate_nodes)
        )
    if missing_labels:
        raise ValueError(
            "Found ChEBI target nodes without preferred labels: "
            + ", ".join(missing_labels)
        )
    if deprecated:
        raise ValueError(
            "Found deprecated ChEBI target nodes: " + ", ".join(deprecated)
        )

    labels = {
        chebi_id: str(target_nodes[chebi_id][0]["lbl"])
        for chebi_id in target_ids
    }
    ids_by_label: dict[str, list[str]] = defaultdict(list)
    for chebi_id, label in labels.items():
        ids_by_label[label].append(chebi_id)
    duplicate_preferred_labels = [
        {"name": label, "chebi_ids": sorted(chebi_ids, key=_numeric_chebi_key)}
        for label, chebi_ids in sorted(ids_by_label.items())
        if len(chebi_ids) > 1
    ]
    if duplicate_preferred_labels:
        raise ValueError(
            "Found duplicate ChEBI preferred labels: "
            + "; ".join(
                f"{item['name']}={item['chebi_ids']!r}"
                for item in duplicate_preferred_labels
            )
        )
    audit = {
        "target_label_count": len(target_ids),
        "ids_with_exactly_one_named_active_node": len(target_ids),
        "missing_node_ids": missing_nodes,
        "duplicate_node_ids": duplicate_nodes,
        "missing_label_ids": missing_labels,
        "deprecated_ids": deprecated,
        "duplicate_preferred_labels": duplicate_preferred_labels,
    }
    return labels, audit


def _frequency_band(count: int) -> str:
    if count >= 100:
        return "head"
    if count >= 10:
        return "mid"
    return "tail"


def _build_label_catalog(
    records: Sequence[Mapping[str, Any]],
    *,
    ontology_labels: Mapping[str, str],
    chebi_name_audit: Mapping[str, Any],
    dataset_version: str,
    input_hashes: Mapping[str, str],
) -> dict[str, Any]:
    accession_counts: Counter[str] = Counter()
    names: dict[str, set[str]] = defaultdict(set)
    for record in records:
        accession_counts.update(set(_label_ids(record, "experimental_label_ids")))
        for block in _blocks(record):
            for occurrence in block.get("label_occurrences", []):
                if not isinstance(occurrence, Mapping):
                    continue
                if occurrence.get("experimental") is not True:
                    continue
                chebi_id = occurrence.get("chebi_id")
                name = occurrence.get("name")
                if isinstance(chebi_id, str) and isinstance(name, str) and name:
                    names[chebi_id].add(name)

    target_ids = sorted(accession_counts, key=_numeric_chebi_key)
    missing_names = [chebi_id for chebi_id in target_ids if not names[chebi_id]]
    nonunique = [chebi_id for chebi_id in target_ids if len(names[chebi_id]) != 1]
    if missing_names:
        raise ValueError(
            "Missing UniProt cofactor names for: " + ", ".join(missing_names)
        )
    if nonunique:
        details = "; ".join(
            f"{chebi_id}={sorted(names[chebi_id])!r}" for chebi_id in nonunique
        )
        raise ValueError("Found non-unique UniProt cofactor names: " + details)

    labels = [
        {
            "chebi_id": chebi_id,
            "name": ontology_labels[chebi_id],
            "uniprot_display_name": next(iter(names[chebi_id])),
            "master_accession_count": accession_counts[chebi_id],
            "frequency_band": _frequency_band(accession_counts[chebi_id]),
        }
        for chebi_id in target_ids
    ]
    band_counts = Counter(item["frequency_band"] for item in labels)
    return {
        "schema_version": LABEL_CATALOG_SCHEMA_VERSION,
        "dataset_version": dataset_version,
        "catalog_version": CATALOG_VERSION,
        "rule_version": RULE_VERSION,
        "input_hashes": dict(sorted(input_hashes.items())),
        "labels": labels,
        "summary": {
            "label_count": len(labels),
            "frequency_band_counts": {
                band: band_counts.get(band, 0) for band in ("head", "mid", "tail")
            },
        },
        "chebi_name_audit": dict(chebi_name_audit),
        "uniprot_display_name_audit": {
            "target_label_count": len(target_ids),
            "ids_with_exactly_one_uniprot_name": sum(
                len(names[chebi_id]) == 1 for chebi_id in target_ids
            ),
            "missing_name_ids": missing_names,
            "nonunique_ids": nonunique,
        },
    }


def _diagnostic_counts(
    records: Sequence[Mapping[str, Any]],
    *,
    single_clean_all_notes_pending: int,
) -> dict[str, Any]:
    experimental_single = [
        record
        for record in records
        if len(_label_ids(record, "experimental_label_ids")) == 1
    ]
    experimental_notes_pending = 0
    all_notes_pending = 0
    experimental_notes_or_scope_pending = 0
    for record in experimental_single:
        label_ids = _label_ids(record, "experimental_label_ids")
        experimental_blocks = _experimental_blocks(record)
        experimental_result = _note_triage(
            label_ids,
            _notes_from_blocks(experimental_blocks),
        )
        all_result = _note_triage(label_ids, _notes_from_blocks(_blocks(record)))
        experimental_pending = experimental_result["status"] == "PENDING"
        experimental_notes_pending += experimental_pending
        all_notes_pending += all_result["status"] == "PENDING"
        experimental_notes_or_scope_pending += experimental_pending or bool(
            _molecules_from_blocks(experimental_blocks)
        )

    return {
        "experimental_single_accessions": len(experimental_single),
        "experimental_single_experimental_block_notes_pending": (
            experimental_notes_pending
        ),
        "experimental_single_all_block_notes_pending": all_notes_pending,
        "experimental_single_experimental_block_notes_or_molecule_scope_pending": (
            experimental_notes_or_scope_pending
        ),
        "historical_note_pending_target": 541,
        "historical_note_pending_target_reproduced": (
            single_clean_all_notes_pending == 541
            or experimental_notes_pending == 541
            or all_notes_pending == 541
            or experimental_notes_or_scope_pending == 541
        ),
        "historical_note_pending_target_status": (
            "REPRODUCED_WITH_CURRENT_FROZEN_INPUTS_AND_RULES"
            if 541
            in {
                single_clean_all_notes_pending,
                experimental_notes_pending,
                all_notes_pending,
                experimental_notes_or_scope_pending,
            }
            else "NOT_REPRODUCED_WITH_CURRENT_FROZEN_INPUTS_AND_RULES"
        ),
    }


def _single_membership_reasons(
    *,
    experimental_ids: Sequence[str],
    all_ids: Sequence[str],
    formula_shape: str,
) -> list[str]:
    reasons: set[str] = set()
    if len(experimental_ids) != 1:
        formula_reason = _FORMULA_REASON.get(formula_shape)
        if formula_reason:
            reasons.add(formula_reason)
    if len(all_ids) != 1 or set(all_ids) != set(experimental_ids):
        reasons.add("EXTRA_NONEXPERIMENTAL_LABEL")
    return sorted(reasons)


def _core_membership_reasons(
    reasons: Sequence[str],
    *,
    single_clean: bool,
    exact_sequence: Mapping[str, Any],
    note_triage: Mapping[str, Any],
) -> list[str]:
    selected: set[str] = set()
    if not single_clean:
        selected.update(
            reason
            for reason in reasons
            if reason in _FORMULA_REASON.values()
            or reason == "EXTRA_NONEXPERIMENTAL_LABEL"
        )
    selected.update(
        reason
        for reason in reasons
        if reason
        in {
            "ONTOLOGY_ANCESTOR_TARGET",
            "UNKNOWN_RESIDUE_X",
            "MOLECULE_SCOPE",
            "EXACT_SEQUENCE_LABEL_CONFLICT",
        }
    )
    if (
        exact_sequence.get("status") == "DUPLICATE_CONSISTENT"
        and not exact_sequence.get("is_core_representative")
    ):
        selected.add("EXACT_SEQUENCE_DUPLICATE")
    selected.update(note_triage.get("reason_codes", []))
    return sorted(selected)


def _core_entry_criteria(
    *,
    experimental_ids: Sequence[str],
    all_ids: Sequence[str],
    is_ancestor_target: bool,
    reasons: set[str],
    molecules: Sequence[str],
    exact_sequence: Mapping[str, Any],
    note_triage: Mapping[str, Any],
) -> dict[str, bool]:
    """Return accession-local Core predicates before entity representative choice."""

    return {
        "single_clean": len(experimental_ids) == 1 and len(all_ids) == 1,
        "not_ontology_ancestor_target": not is_ancestor_target,
        "no_unknown_residue_x": "UNKNOWN_RESIDUE_X" not in reasons,
        "no_molecule_scope": not molecules,
        "nonconflicting_exact_sequence": (
            exact_sequence["status"] != "DUPLICATE_CONFLICT"
        ),
        "note_triage_not_required": note_triage["status"] == "NOT_REQUIRED",
    }


def _core_representatives(
    records: Sequence[Mapping[str, Any]],
    *,
    ancestor_targets: set[str],
    by_accession: Mapping[str, Mapping[str, Any]],
) -> dict[str, str]:
    """Choose one accession per sequence entity only after quality filtering."""

    eligible: dict[str, list[str]] = defaultdict(list)
    for record in sorted(records, key=_entry_accession):
        accession = _entry_accession(record)
        experimental_ids = _label_ids(record, "experimental_label_ids")
        all_ids = _label_ids(record, "all_cofactor_label_ids")
        sequence = record.get("sequence")
        if not isinstance(sequence, Mapping) or not isinstance(
            sequence.get("value"), str
        ):
            raise ValueError(f"Record {accession!r} lacks a sequence")
        reasons = set(classify_alphabet(sequence["value"])["reason_codes"])
        molecules = _molecules_from_blocks(_blocks(record))
        exact_sequence = by_accession[accession]
        note_triage = _note_triage(
            experimental_ids,
            _notes_from_blocks(_blocks(record)),
        )
        criteria = _core_entry_criteria(
            experimental_ids=experimental_ids,
            all_ids=all_ids,
            is_ancestor_target=bool(set(experimental_ids) & ancestor_targets),
            reasons=reasons,
            molecules=molecules,
            exact_sequence=exact_sequence,
            note_triage=note_triage,
        )
        if all(criteria.values()):
            entity_id = exact_sequence.get("sequence_entity_id")
            if not isinstance(entity_id, str) or not entity_id:
                raise ValueError(
                    f"Record {accession!r} lacks a sequence_entity_id"
                )
            eligible[entity_id].append(accession)
    return {
        entity_id: min(accessions)
        for entity_id, accessions in eligible.items()
    }


def _challenge_reasons(reasons: Iterable[str]) -> list[str]:
    return sorted(
        reason
        for reason in set(reasons)
        if reason in _CHALLENGE_REASON_CODES
        or reason.startswith(_NOTE_REASON_PREFIX)
    )


def _enrich_records(
    records: Sequence[Mapping[str, Any]],
    *,
    ancestry: Mapping[str, Any],
    duplicates: Mapping[str, Any],
    dataset_version: str,
    input_hashes: Mapping[str, str],
) -> tuple[
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
]:
    ancestor_targets = set(ancestry["ancestor_targets"])
    pairs_by_specific: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pair in ancestry["pairs"]:
        pairs_by_specific[pair["specific"]].append(dict(pair))

    full: list[dict[str, Any]] = []
    single_rows: list[dict[str, Any]] = []
    core_rows: list[dict[str, Any]] = []
    challenge_rows: list[dict[str, Any]] = []
    by_accession = duplicates["by_accession"]
    core_representatives = _core_representatives(
        records,
        ancestor_targets=ancestor_targets,
        by_accession=by_accession,
    )
    for original in sorted(records, key=_entry_accession):
        record = deepcopy(original)
        accession = _entry_accession(record)
        experimental_ids = _label_ids(record, "experimental_label_ids")
        all_ids = _label_ids(record, "all_cofactor_label_ids")
        derived = record["derived"]
        formula_shape = derived.get("formula_shape")
        if not isinstance(formula_shape, str):
            raise ValueError(f"Record {accession!r} lacks formula_shape")
        formula = _gold_formula(record)

        reasons = set(derived.get("reason_codes", []))
        formula_reason = _FORMULA_REASON.get(formula_shape)
        if formula_reason:
            reasons.add(formula_reason)
        if _formula_overlap_count(formula):
            reasons.add("OVERLAPPING_BLOCK_LABEL")
        if set(all_ids) != set(experimental_ids):
            reasons.add("EXTRA_NONEXPERIMENTAL_LABEL")
        evidence_status = derived.get("evidence_status")
        if evidence_status == "ALL_REFERENCE_ONLY":
            reasons.add("REFERENCE_ONLY_EXPERIMENTAL_EVIDENCE")
        elif evidence_status == "MIXED_DIRECT_AND_REFERENCE":
            reasons.add("PARTIAL_DIRECT_PUBMED_EVIDENCE")
        occurrence_count = derived.get("experimental_occurrence_count")
        if isinstance(occurrence_count, int) and occurrence_count > len(
            experimental_ids
        ):
            reasons.add("DUPLICATE_EXPERIMENTAL_LABEL_OCCURRENCE")
        if _has_unresolved_reference(record):
            reasons.add("UNRESOLVED_REFERENCE_EVIDENCE")

        sequence = record.get("sequence")
        if not isinstance(sequence, dict) or not isinstance(
            sequence.get("value"), str
        ):
            raise ValueError(f"Record {accession!r} lacks a sequence")
        alphabet = classify_alphabet(sequence["value"])
        sequence["alphabet_status"] = alphabet["status"]
        sequence["nonstandard_symbols"] = alphabet["nonstandard_symbols"]
        reasons.update(alphabet["reason_codes"])

        molecules = _molecules_from_blocks(_blocks(record))
        if molecules:
            reasons.add("MOLECULE_SCOPE")
        is_ancestor_target = bool(set(experimental_ids) & ancestor_targets)
        if is_ancestor_target:
            reasons.add("ONTOLOGY_ANCESTOR_TARGET")

        exact_sequence = deepcopy(by_accession[accession])
        entity_id = exact_sequence.get("sequence_entity_id")
        core_representative = core_representatives.get(entity_id)
        exact_sequence["core_representative_accession"] = core_representative
        exact_sequence["is_core_representative"] = (
            core_representative == accession
        )
        exact_sequence["core_representative_policy"] = (
            "LEXICOGRAPHIC_ACCESSION_AFTER_ENTRY_QUALITY_FILTERING"
        )
        reasons.update(exact_sequence["reason_codes"])
        note_triage = _note_triage(
            experimental_ids,
            _notes_from_blocks(_blocks(record)),
        )
        reasons.update(note_triage["reason_codes"])
        ordered_reasons = sorted(reasons)

        is_single_clean = len(experimental_ids) == 1 and len(all_ids) == 1
        core_criteria = _core_entry_criteria(
            experimental_ids=experimental_ids,
            all_ids=all_ids,
            is_ancestor_target=is_ancestor_target,
            reasons=reasons,
            molecules=molecules,
            exact_sequence=exact_sequence,
            note_triage=note_triage,
        )
        core_criteria["core_sequence_representative"] = bool(
            exact_sequence["is_core_representative"]
        )
        is_core = all(core_criteria.values())
        challenge_reasons = _challenge_reasons(ordered_reasons)
        is_challenge = bool(challenge_reasons)

        derived["reason_codes"] = ordered_reasons
        derived["ontology"] = {
            "is_ancestor_target": is_ancestor_target,
            "target_ancestry": {
                label_id: pairs_by_specific.get(label_id, [])
                for label_id in sorted(experimental_ids, key=_numeric_chebi_key)
            },
        }
        derived["exact_sequence"] = exact_sequence
        derived["note_triage"] = note_triage
        derived["view_membership"] = {
            "full_structured": {
                "included": True,
                "reason_codes": [],
            },
            "single_clean": {
                "included": is_single_clean,
                "criteria": {
                    "one_experimental_label": len(experimental_ids) == 1,
                    "one_all_cofactor_label": len(all_ids) == 1,
                },
                "reason_codes": _single_membership_reasons(
                    experimental_ids=experimental_ids,
                    all_ids=all_ids,
                    formula_shape=formula_shape,
                ),
            },
            "core_provisional": {
                "included": is_core,
                "criteria": core_criteria,
                "reason_codes": _core_membership_reasons(
                    ordered_reasons,
                    single_clean=is_single_clean,
                    exact_sequence=exact_sequence,
                    note_triage=note_triage,
                ),
            },
            "ambiguity_challenge": {
                "included": is_challenge,
                "reason_codes": challenge_reasons,
            },
        }
        memberships = set(derived.get("memberships", []))
        memberships.update({"MASTER_5337", "FULL_STRUCTURED"})
        if is_single_clean:
            memberships.add("SINGLE_CLEAN")
        if is_core:
            memberships.add("CORE_PROVISIONAL")
        if is_challenge:
            memberships.add("AMBIGUITY_CHALLENGE")
        derived["memberships"] = sorted(memberships)

        record["schema_version"] = VIEW_RECORD_SCHEMA_VERSION
        record["dataset_version"] = dataset_version
        record["derivation"] = {
            "rule_version": RULE_VERSION,
            "input_hashes": dict(sorted(input_hashes.items())),
        }
        adjudication = record.get("adjudication")
        if not isinstance(adjudication, dict):
            adjudication = {}
            record["adjudication"] = adjudication
        adjudication_reasons = set(note_triage["reason_codes"])
        if exact_sequence["status"] == "DUPLICATE_CONFLICT":
            adjudication_reasons.add("EXACT_SEQUENCE_LABEL_CONFLICT")
        if adjudication.get("status") in {None, "UNASSESSED", "NOT_REQUIRED"}:
            adjudication["status"] = (
                "PENDING" if adjudication_reasons else "NOT_REQUIRED"
            )
        adjudication["reason_codes"] = sorted(adjudication_reasons)
        adjudication["rule_version"] = RULE_VERSION
        adjudication.setdefault("decision", None)
        adjudication.setdefault("reviewer", None)
        adjudication.setdefault("rationale", None)
        adjudication.setdefault("evidence_occurrence_ids", [])

        full.append(record)
        if is_single_clean:
            single_rows.append(record)
        if is_core:
            core_rows.append(record)
        if is_challenge:
            challenge_rows.append(record)

    return (
        tuple(full),
        tuple(single_rows),
        tuple(core_rows),
        tuple(challenge_rows),
    )


def derive_view_artifacts(
    records: Iterable[Mapping[str, Any]],
    chebi_graph: Mapping[str, Any],
    *,
    dataset_version: str,
    input_hashes: Mapping[str, str],
) -> DerivedViewArtifacts:
    """Purely derive all benchmark views and audits from Master records."""

    ordered_records = sorted(list(records), key=_entry_accession)
    accessions = [_entry_accession(record) for record in ordered_records]
    if len(accessions) != len(set(accessions)):
        duplicates = sorted(
            accession
            for accession, count in Counter(accessions).items()
            if count > 1
        )
        raise ValueError("Duplicate Master accessions: " + ", ".join(duplicates))

    formulas = {
        _entry_accession(record): _gold_formula(record)
        for record in ordered_records
    }
    formula_overlap_counts = {
        accession: _formula_overlap_count(formula)
        for accession, formula in formulas.items()
    }
    formula_audit = {
        "canonical_block_count": sum(len(formula) for formula in formulas.values()),
        "canonical_block_label_overlap_count": sum(
            formula_overlap_counts.values()
        ),
        "overlapping_block_label_accession_count": sum(
            bool(count) for count in formula_overlap_counts.values()
        ),
        "overlapping_block_label_accessions": sorted(
            accession
            for accession, count in formula_overlap_counts.items()
            if count
        ),
    }

    target_ids = sorted(
        {
            label_id
            for record in ordered_records
            for label_id in _label_ids(record, "experimental_label_ids")
        },
        key=_numeric_chebi_key,
    )
    ancestry = analyze_target_ancestry(dict(chebi_graph), target_ids)
    exact_sequences = analyze_exact_sequence_groups(ordered_records)
    ontology_labels, chebi_name_audit = _ontology_target_names(
        chebi_graph,
        target_ids,
    )
    label_catalog = _build_label_catalog(
        ordered_records,
        ontology_labels=ontology_labels,
        chebi_name_audit=chebi_name_audit,
        dataset_version=dataset_version,
        input_hashes=input_hashes,
    )
    (
        full,
        single_rows,
        core_rows,
        challenge_rows,
    ) = _enrich_records(
        ordered_records,
        ancestry=ancestry,
        duplicates=exact_sequences,
        dataset_version=dataset_version,
        input_hashes=input_hashes,
    )

    ontology_audit = {
        "schema_version": ONTOLOGY_AUDIT_SCHEMA_VERSION,
        "dataset_version": dataset_version,
        "rule_version": RULE_VERSION,
        "input_hashes": dict(sorted(input_hashes.items())),
        "target_label_count": len(target_ids),
        "pairs": ancestry["pairs"],
        "ancestor_targets": ancestry["ancestor_targets"],
        "terms_in_overlap": ancestry["terms_in_overlap"],
        "missing_target_nodes": ancestry["missing_target_nodes"],
        "target_node_audit": chebi_name_audit,
        "summary": {
            "ancestor_pair_count": len(ancestry["pairs"]),
            "ancestor_target_count": len(ancestry["ancestor_targets"]),
            "overlap_term_count": len(ancestry["terms_in_overlap"]),
            "missing_target_count": len(ancestry["missing_target_nodes"]),
        },
    }

    reason_counts = Counter(
        reason
        for record in full
        for reason in record["derived"]["reason_codes"]
    )
    challenge_reason_counts = Counter(
        reason
        for record in challenge_rows
        for reason in record["derived"]["view_membership"][
            "ambiguity_challenge"
        ]["reason_codes"]
    )
    core_failed_criteria = Counter(
        criterion
        for record in full
        for criterion, passed in record["derived"]["view_membership"][
            "core_provisional"
        ]["criteria"].items()
        if not passed
    )
    single_label_ids = {
        record["derived"]["experimental_label_ids"][0]
        for record in single_rows
    }
    note_pending_single_clean = sum(
        record["derived"]["note_triage"]["status"] == "PENDING"
        for record in single_rows
    )
    diagnostics = _diagnostic_counts(
        ordered_records,
        single_clean_all_notes_pending=note_pending_single_clean,
    )
    core_predicate_audit = {
        "sequence_with_selenocysteine_u": sum(
            "U" in record["sequence"]["value"] for record in core_rows
        ),
        "violation_counts": {
            "not_single_clean": sum(
                not record["derived"]["view_membership"]["single_clean"][
                    "included"
                ]
                for record in core_rows
            ),
            "ontology_ancestor_target": sum(
                record["derived"]["ontology"]["is_ancestor_target"]
                for record in core_rows
            ),
            "unknown_residue_x": sum(
                "UNKNOWN_RESIDUE_X" in record["derived"]["reason_codes"]
                for record in core_rows
            ),
            "molecule_scope": sum(
                "MOLECULE_SCOPE" in record["derived"]["reason_codes"]
                for record in core_rows
            ),
            "exact_sequence_conflict": sum(
                record["derived"]["exact_sequence"]["status"]
                == "DUPLICATE_CONFLICT"
                for record in core_rows
            ),
            "not_core_sequence_representative": sum(
                not record["derived"]["exact_sequence"][
                    "is_core_representative"
                ]
                for record in core_rows
            ),
            "note_triage_pending": sum(
                record["derived"]["note_triage"]["status"] == "PENDING"
                for record in core_rows
            ),
        },
    }
    view_audit = {
        "schema_version": VIEW_AUDIT_SCHEMA_VERSION,
        "dataset_version": dataset_version,
        "rule_version": RULE_VERSION,
        "input_hashes": dict(sorted(input_hashes.items())),
        "summary": {
            "full_structured_accessions": len(full),
            "single_clean_accessions": len(single_rows),
            "single_clean_sequence_entities": len(
                {record["sequence"]["sha256"] for record in single_rows}
            ),
            "single_clean_labels": len(single_label_ids),
            "core_provisional_accessions": len(core_rows),
            "ambiguity_challenge_accessions": len(challenge_rows),
            "note_pending_single_clean": note_pending_single_clean,
            "formula": {
                key: formula_audit[key]
                for key in (
                    "canonical_block_count",
                    "canonical_block_label_overlap_count",
                    "overlapping_block_label_accession_count",
                )
            },
            "ontology": ontology_audit["summary"],
            "exact_sequence": exact_sequences["summary"],
            "label_catalog": label_catalog["summary"],
        },
        "diagnostic_counts": diagnostics,
        "formula_audit": formula_audit,
        "core_predicate_audit": core_predicate_audit,
        "reason_code_accession_counts": dict(sorted(reason_counts.items())),
        "challenge_reason_accession_counts": dict(
            sorted(challenge_reason_counts.items())
        ),
        "core_failed_criterion_counts": dict(sorted(core_failed_criteria.items())),
        "anomalies": (
            []
            if diagnostics["historical_note_pending_target_reproduced"]
            else [
                {
                    "code": "HISTORICAL_NOTE_PENDING_COUNT_NOT_REPRODUCED",
                    "historical_count": 541,
                    "observed_single_clean_all_block_notes_pending": (
                        note_pending_single_clean
                    ),
                    "disposition": (
                        "Audit the historical counting rule; do not tune regexes or "
                        "hard-code membership to force the count."
                    ),
                }
            ]
        ),
    }

    return DerivedViewArtifacts(
        full_structured=full,
        single_clean=single_rows,
        core_provisional=core_rows,
        ambiguity_challenge=challenge_rows,
        label_catalog=label_catalog,
        ontology_audit=ontology_audit,
        view_audit=view_audit,
    )


def _summary_from_artifacts(
    artifacts: DerivedViewArtifacts,
    output_sha256: Mapping[str, str],
) -> ViewBuildSummary:
    summary = artifacts.view_audit["summary"]
    ontology = summary["ontology"]
    exact = summary["exact_sequence"]
    catalog = summary["label_catalog"]
    formula = summary["formula"]
    return ViewBuildSummary(
        full_structured_accessions=summary["full_structured_accessions"],
        single_clean_accessions=summary["single_clean_accessions"],
        single_clean_sequence_entities=summary["single_clean_sequence_entities"],
        single_clean_labels=summary["single_clean_labels"],
        core_provisional_accessions=summary["core_provisional_accessions"],
        ambiguity_challenge_accessions=summary[
            "ambiguity_challenge_accessions"
        ],
        ontology_ancestor_pairs=ontology["ancestor_pair_count"],
        ontology_overlap_terms=ontology["overlap_term_count"],
        ontology_ancestor_targets=ontology["ancestor_target_count"],
        ontology_missing_targets=ontology["missing_target_count"],
        sequence_entities=exact["sequence_entities"],
        duplicate_groups=exact["duplicate_groups"],
        duplicate_entries=exact["duplicate_entries"],
        conflict_groups=exact["conflict_groups"],
        conflict_entries=exact["conflict_entries"],
        canonical_block_count=formula["canonical_block_count"],
        canonical_block_label_overlap_count=formula[
            "canonical_block_label_overlap_count"
        ],
        overlapping_block_label_accession_count=formula[
            "overlapping_block_label_accession_count"
        ],
        note_pending_single_clean=summary["note_pending_single_clean"],
        label_count=catalog["label_count"],
        frequency_band_counts=dict(catalog["frequency_band_counts"]),
        output_sha256=dict(sorted(output_sha256.items())),
    )


def _validate_frozen(summary: ViewBuildSummary, diagnostics: Mapping[str, Any]) -> None:
    observed = summary.to_dict()
    mismatches = [
        f"{field}: expected {expected!r}, observed {observed[field]!r}"
        for field, expected in _FROZEN_EXPECTED.items()
        if observed[field] != expected
    ]
    mismatches.extend(
        f"diagnostic {field}: expected {expected!r}, observed {diagnostics[field]!r}"
        for field, expected in _FROZEN_DIAGNOSTICS.items()
        if diagnostics[field] != expected
    )
    if mismatches:
        raise ValueError("Frozen view invariant mismatch: " + "; ".join(mismatches))


def _render_report(
    artifacts: DerivedViewArtifacts,
    *,
    output_hashes: Mapping[str, str],
) -> str:
    summary = artifacts.view_audit["summary"]
    ontology = summary["ontology"]
    exact = summary["exact_sequence"]
    catalog = summary["label_catalog"]
    formula = summary["formula"]
    chebi_names = artifacts.label_catalog["chebi_name_audit"]
    uniprot_names = artifacts.label_catalog["uniprot_display_name_audit"]
    diagnostics = artifacts.view_audit["diagnostic_counts"]
    core_audit = artifacts.view_audit["core_predicate_audit"]
    anomalies = artifacts.view_audit["anomalies"]
    hash_lines = "\n".join(
        f"- `{name}`: `{digest}`" for name, digest in sorted(output_hashes.items())
    )
    input_hash_lines = "\n".join(
        f"- `{name}`: `{digest}`"
        for name, digest in sorted(artifacts.view_audit["input_hashes"].items())
    )
    anomaly_lines = (
        "\n".join(
            f"- `{item['code']}`: historical={item['historical_count']}, "
            f"observed={item['observed_single_clean_all_block_notes_pending']}. "
            f"{item['disposition']}"
            for item in anomalies
        )
        if anomalies
        else "- None."
    )
    historical_note_line = (
        "- Historical count 541 is reproduced by a documented diagnostic "
        "projection.\n"
        if diagnostics["historical_note_pending_target_reproduced"]
        else (
            "- Historical count 541 is not reproduced by the frozen inputs and "
            "current rules; it is retained as an audit anomaly, not an acceptance "
            "invariant.\n"
        )
    )
    return (
        "# Cofactor9.1 data and view audit\n\n"
        f"- Dataset version: `{artifacts.view_audit['dataset_version']}`\n"
        f"- Rule version: `{RULE_VERSION}`\n"
        f"- Full-Structured accessions: {summary['full_structured_accessions']}\n"
        f"- Single-Clean accessions: {summary['single_clean_accessions']}\n"
        f"- Single-Clean exact-sequence entities: "
        f"{summary['single_clean_sequence_entities']}\n"
        f"- Single-Clean labels: {summary['single_clean_labels']}\n"
        f"- Core-Provisional accessions: {summary['core_provisional_accessions']}\n"
        f"- Ambiguity-Challenge accessions: "
        f"{summary['ambiguity_challenge_accessions']}\n\n"
        "## Gold formula checkpoints\n\n"
        f"- Preserved non-empty cofactor blocks: "
        f"{formula['canonical_block_count']}\n"
        f"- Overlapping block pairs: "
        f"{formula['canonical_block_label_overlap_count']}\n"
        f"- Accessions with a label repeated across blocks: "
        f"{formula['overlapping_block_label_accession_count']}\n\n"
        "## Ontology checkpoints\n\n"
        f"- Target ancestor pairs: {ontology['ancestor_pair_count']}\n"
        f"- Terms in overlap: {ontology['overlap_term_count']}\n"
        f"- Ancestor targets: {ontology['ancestor_target_count']}\n"
        f"- Missing targets: {ontology['missing_target_count']}\n\n"
        "## Exact-sequence checkpoints\n\n"
        f"- Sequence entities: {exact['sequence_entities']}\n"
        f"- Duplicate groups / entries: {exact['duplicate_groups']} / "
        f"{exact['duplicate_entries']}\n"
        f"- Conflict groups / entries: {exact['conflict_groups']} / "
        f"{exact['conflict_entries']}\n\n"
        "## Label catalog checkpoints\n\n"
        f"- Labels: {catalog['label_count']}\n"
        f"- Frequency bands (head / mid / tail): "
        f"{catalog['frequency_band_counts']['head']} / "
        f"{catalog['frequency_band_counts']['mid']} / "
        f"{catalog['frequency_band_counts']['tail']}\n"
        f"- ChEBI target nodes with exactly one active preferred label: "
        f"{chebi_names['ids_with_exactly_one_named_active_node']} / "
        f"{chebi_names['target_label_count']}\n"
        f"- Missing / duplicate / unnamed / deprecated ChEBI targets: "
        f"{len(chebi_names['missing_node_ids'])} / "
        f"{len(chebi_names['duplicate_node_ids'])} / "
        f"{len(chebi_names['missing_label_ids'])} / "
        f"{len(chebi_names['deprecated_ids'])}\n"
        f"- Duplicate ChEBI preferred labels across target IDs: "
        f"{len(chebi_names['duplicate_preferred_labels'])}\n"
        f"- Targets with one unique UniProt display name: "
        f"{uniprot_names['ids_with_exactly_one_uniprot_name']} / "
        f"{uniprot_names['target_label_count']}\n\n"
        "## Core-Provisional predicate audit\n\n"
        f"- Retained sequences containing selenocysteine `U`: "
        f"{core_audit['sequence_with_selenocysteine_u']}\n"
        "- Predicate violations among retained rows: "
        f"{sum(core_audit['violation_counts'].values())}\n\n"
        "## Conservative note triage\n\n"
        f"- Single-Clean, all cofactor notes, PENDING accessions: "
        f"{summary['note_pending_single_clean']}\n"
        f"- Experimental-single accessions: "
        f"{diagnostics['experimental_single_accessions']}\n"
        f"- Experimental-single, experimental-block notes, PENDING: "
        f"{diagnostics['experimental_single_experimental_block_notes_pending']}\n"
        f"- Experimental-single, all-block notes, PENDING: "
        f"{diagnostics['experimental_single_all_block_notes_pending']}\n"
        f"- Experimental-single, experimental-block note or molecule scope, "
        f"PENDING: "
        f"{diagnostics['experimental_single_experimental_block_notes_or_molecule_scope_pending']}\n"
        f"{historical_note_line}\n"
        "## Anomalies\n\n"
        f"{anomaly_lines}\n\n"
        "## Artifact SHA-256\n\n"
        f"{hash_lines}\n\n"
        "## Input SHA-256\n\n"
        f"{input_hash_lines}\n"
    )


def _atomic_write_many(payloads: Mapping[Path, bytes]) -> None:
    if len(payloads) != len(set(payloads)):
        raise ValueError("Output paths must be unique")
    temporary_paths: dict[Path, Path] = {}
    backup_paths: dict[Path, Path | None] = {}
    try:
        for target, payload in payloads.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=target.parent,
                delete=False,
            )
            temporary = Path(handle.name)
            temporary_paths[target] = temporary
            try:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                handle.close()
        for target in payloads:
            if not target.exists():
                backup_paths[target] = None
                continue
            descriptor, backup_name = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".rollback",
                dir=target.parent,
            )
            os.close(descriptor)
            backup = Path(backup_name)
            try:
                shutil.copy2(target, backup)
            except BaseException:
                backup.unlink(missing_ok=True)
                raise
            backup_paths[target] = backup
        for target, temporary in temporary_paths.items():
            os.replace(temporary, target)
    except BaseException:
        rollback_errors: list[BaseException] = []
        for target, backup in backup_paths.items():
            try:
                if backup is None:
                    target.unlink(missing_ok=True)
                else:
                    os.replace(backup, target)
            except BaseException as error:
                rollback_errors.append(error)
        for temporary in temporary_paths.values():
            temporary.unlink(missing_ok=True)
        if rollback_errors:
            raise RuntimeError(
                "View artifact replacement and rollback both failed"
            ) from rollback_errors[0]
        raise
    finally:
        for temporary in temporary_paths.values():
            temporary.unlink(missing_ok=True)
        for backup in backup_paths.values():
            if backup is not None:
                backup.unlink(missing_ok=True)


def build_views(
    *,
    master_path: str | Path,
    chebi_path: str | Path,
    output_paths: Mapping[str, str | Path],
    dataset_version: str,
    expected_input_hashes: Mapping[str, str] | None = None,
) -> ViewBuildSummary:
    """Build every view and audit from frozen inputs with atomic file writes."""

    missing_outputs = sorted(_REQUIRED_OUTPUTS - set(output_paths))
    if missing_outputs:
        raise ValueError("Missing view output paths: " + ", ".join(missing_outputs))
    unexpected_outputs = sorted(set(output_paths) - _REQUIRED_OUTPUTS)
    if unexpected_outputs:
        raise ValueError(
            "Unexpected view output paths: " + ", ".join(unexpected_outputs)
        )
    targets = {
        name: Path(output_paths[name]).resolve()
        for name in sorted(_REQUIRED_OUTPUTS)
    }
    if len(set(targets.values())) != len(targets):
        raise ValueError("View output paths must be unique")

    master = Path(master_path).resolve()
    chebi = Path(chebi_path).resolve()
    if len({master, chebi, *targets.values()}) != len(targets) + 2:
        raise ValueError("Frozen inputs and outputs must be distinct paths")
    input_hashes = {
        "master_sha256": _sha256_file(master),
        "chebi_artifact_sha256": _sha256_file(chebi),
        "chebi_decompressed_content_sha256": _decompressed_sha256(chebi),
    }
    if expected_input_hashes is not None:
        unknown_hashes = sorted(set(expected_input_hashes) - set(input_hashes))
        if unknown_hashes:
            raise ValueError(
                "Unknown expected input hashes: " + ", ".join(unknown_hashes)
            )
        mismatches = [
            f"{name}: expected {expected!r}, observed {input_hashes[name]!r}"
            for name, expected in expected_input_hashes.items()
            if input_hashes[name] != expected
        ]
        if mismatches:
            raise ValueError("Frozen view input hash mismatch: " + "; ".join(mismatches))
    artifacts = derive_view_artifacts(
        _load_master(master),
        _load_chebi_graph(chebi),
        dataset_version=dataset_version,
        input_hashes=input_hashes,
    )

    payloads_by_name = {
        "full_structured": _jsonl_bytes(artifacts.full_structured),
        "single_clean": _jsonl_bytes(artifacts.single_clean),
        "core_provisional": _jsonl_bytes(artifacts.core_provisional),
        "ambiguity_challenge": _jsonl_bytes(artifacts.ambiguity_challenge),
        "label_catalog": _json_bytes(artifacts.label_catalog),
        "ontology_audit": _json_bytes(artifacts.ontology_audit),
    }
    artifact_hashes = {
        name: _sha256_bytes(payload) for name, payload in payloads_by_name.items()
    }
    view_audit = deepcopy(artifacts.view_audit)
    view_audit["output_artifact_sha256"] = dict(sorted(artifact_hashes.items()))
    payloads_by_name["view_audit"] = _json_bytes(view_audit)
    artifact_hashes["view_audit"] = _sha256_bytes(payloads_by_name["view_audit"])
    payloads_by_name["report"] = _render_report(
        artifacts,
        output_hashes=artifact_hashes,
    ).encode("utf-8")
    artifact_hashes["report"] = _sha256_bytes(payloads_by_name["report"])

    preliminary_summary = _summary_from_artifacts(artifacts, artifact_hashes)
    if dataset_version == "Cofactor9.1":
        _validate_frozen(
            preliminary_summary,
            artifacts.view_audit["diagnostic_counts"],
        )

    _atomic_write_many(
        {targets[name]: payload for name, payload in payloads_by_name.items()}
    )
    observed_hashes = {
        name: _sha256_file(targets[name]) for name in sorted(targets)
    }
    if observed_hashes != dict(sorted(artifact_hashes.items())):
        raise RuntimeError("Published view artifact hashes differ from staged bytes")
    return _summary_from_artifacts(artifacts, observed_hashes)


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


def _resolve_path(project_root: Path, configured: str) -> Path:
    path = Path(configured)
    if path.is_absolute():
        raise ValueError("Configured project paths must be relative")
    root = project_root.resolve()
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"Configured path escapes project root: {configured!r}")
    return resolved


def build_views_from_config(config_path: str | Path) -> ViewBuildSummary:
    """Build views using the existing paths in ``config/benchmark.json``."""

    config_file = Path(config_path).resolve()
    with config_file.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, Mapping):
        raise ValueError("Benchmark configuration must contain a JSON object")
    project_root = config_file.parent.parent
    paths = _required_mapping(config, "paths")
    source = _required_mapping(config, "source")
    chebi_source = _required_mapping(source, "chebi")
    dataset_version = _required_string(config, "dataset_version")
    from cofactor_bench.build import verify_foundation_generation

    foundation_generation = verify_foundation_generation(config_file)
    foundation_artifacts = _required_mapping(
        foundation_generation,
        "artifacts",
    )
    foundation_master = _required_mapping(foundation_artifacts, "master")
    master_path = _resolve_path(project_root, _required_string(paths, "master"))
    derived_directory = master_path.parent
    report_directory = _resolve_path(
        project_root,
        _required_string(paths, "reports"),
    )
    output_paths = {
        name: _resolve_path(project_root, _required_string(paths, name))
        for name in _VIEW_NAMES
    }
    output_paths.update(
        {
            "label_catalog": _resolve_path(
                project_root,
                _required_string(paths, "label_catalog"),
            ),
            "ontology_audit": derived_directory / "ontology_audit.json",
            "view_audit": derived_directory / "view_audit.json",
            "report": report_directory / "data_audit.md",
        }
    )
    return build_views(
        master_path=master_path,
        chebi_path=_resolve_path(
            project_root,
            _required_string(paths, "chebi_raw"),
        ),
        output_paths=output_paths,
        dataset_version=dataset_version,
        expected_input_hashes={
            "master_sha256": _required_string(
                foundation_master,
                "sha256",
            ),
            "chebi_artifact_sha256": _required_string(
                chebi_source,
                "artifact_sha256",
            ),
            "chebi_decompressed_content_sha256": _required_string(
                chebi_source,
                "decompressed_content_sha256",
            ),
        },
    )


__all__ = [
    "CATALOG_VERSION",
    "DerivedViewArtifacts",
    "RULE_VERSION",
    "ViewBuildSummary",
    "build_views",
    "build_views_from_config",
    "derive_view_artifacts",
]
