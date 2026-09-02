from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class EvidenceStatus(str, Enum):
    ALL_DIRECT_PUBMED = "ALL_DIRECT_PUBMED"
    MIXED_DIRECT_AND_REFERENCE = "MIXED_DIRECT_AND_REFERENCE"
    ALL_REFERENCE_ONLY = "ALL_REFERENCE_ONLY"


class FormulaShape(str, Enum):
    NONE = "NONE"
    SINGLE = "SINGLE"
    PURE_OR = "PURE_OR"
    PURE_AND = "PURE_AND"
    MIXED_AND_OR = "MIXED_AND_OR"


class AlphabetStatus(str, Enum):
    MISSING = "MISSING"
    STANDARD = "STANDARD"
    SELENOCYSTEINE_U = "SELENOCYSTEINE_U"
    UNKNOWN_X = "UNKNOWN_X"
    SELENOCYSTEINE_U_AND_UNKNOWN_X = "SELENOCYSTEINE_U_AND_UNKNOWN_X"
    OTHER_NONSTANDARD = "OTHER_NONSTANDARD"


@dataclass(frozen=True)
class Evidence:
    source_ordinal: int
    evidence_code: str | None
    source: str | None
    source_id: str | None
    accepted_for_experimental: bool
    resolution_status: str
    reference_number: int | None = None
    resolved_reference: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_ordinal": self.source_ordinal,
            "evidence_code": self.evidence_code,
            "source": self.source,
            "source_id": self.source_id,
            "accepted_for_experimental": self.accepted_for_experimental,
            "resolution_status": self.resolution_status,
            "reference_number": self.reference_number,
            "resolved_reference": self.resolved_reference,
        }


@dataclass(frozen=True)
class NoteText:
    source_ordinal: int
    value: str
    evidences: tuple[Evidence, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_ordinal": self.source_ordinal,
            "value": self.value,
            "evidences": [evidence.to_dict() for evidence in self.evidences],
        }


@dataclass(frozen=True)
class LabelOccurrence:
    occurrence_id: str
    source_ordinal: int
    name: str | None
    cross_reference_database: str | None
    chebi_id: str | None
    evidences: tuple[Evidence, ...]
    experimental: bool

    @property
    def has_direct_pubmed(self) -> bool:
        return any(
            evidence.accepted_for_experimental and evidence.source == "PubMed"
            for evidence in self.evidences
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "occurrence_id": self.occurrence_id,
            "source_ordinal": self.source_ordinal,
            "name": self.name,
            "cross_reference_database": self.cross_reference_database,
            "chebi_id": self.chebi_id,
            "evidences": [evidence.to_dict() for evidence in self.evidences],
            "experimental": self.experimental,
        }


@dataclass(frozen=True)
class CofactorBlock:
    source_ordinal: int
    molecule: str | None
    notes: tuple[NoteText, ...]
    label_occurrences: tuple[LabelOccurrence, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_ordinal": self.source_ordinal,
            "molecule": self.molecule,
            "notes": [note.to_dict() for note in self.notes],
            "label_occurrences": [
                occurrence.to_dict() for occurrence in self.label_occurrences
            ],
        }


@dataclass(frozen=True)
class ParsedUniProtEntry:
    raw_record_index: int
    accession: str
    uniprot_id: str | None
    entry_type: str | None
    entry_version: int | None
    sequence_version: int | None
    last_annotation_update_date: str | None
    organism_scientific_name: str | None
    organism_taxon_id: int | None
    ec_numbers: tuple[str, ...]
    sequence_value: str | None
    reported_sequence_length: int | None
    sequence_crc64: str | None
    sequence_sha256: str | None
    alphabet_status: AlphabetStatus
    nonstandard_symbols: tuple[str, ...]
    cofactor_blocks: tuple[CofactorBlock, ...]
    experimental_label_ids: tuple[str, ...]
    all_cofactor_label_ids: tuple[str, ...]
    gold_formula: tuple[tuple[str, ...], ...]
    formula_shape: FormulaShape
    evidence_status: EvidenceStatus | None
    experimental_occurrence_count: int

    @property
    def eligible_for_master(self) -> bool:
        return self.experimental_occurrence_count > 0
