from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
import hashlib
import re
from typing import Any

from .model import (
    AlphabetStatus,
    CofactorBlock,
    Evidence,
    EvidenceStatus,
    FormulaShape,
    LabelOccurrence,
    NoteText,
    ParsedUniProtEntry,
)


EXPERIMENTAL_EVIDENCE_CODE = "ECO:0000269"
_REFERENCE_ID_PATTERN = re.compile(r"^Ref\.(\d+)$")
_CHEBI_ID_PATTERN = re.compile(r"^CHEBI:(\d+)$")
_STANDARD_AMINO_ACIDS = frozenset("ACDEFGHIKLMNPQRSTVWY")


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _chebi_sort_key(label: str) -> tuple[int, int, str]:
    match = _CHEBI_ID_PATTERN.fullmatch(label)
    if match is not None:
        return (0, int(match.group(1)), label)
    return (1, 0, label)


def canonicalize_formula(
    blocks: Iterable[Iterable[str]],
) -> tuple[tuple[str, ...], ...]:
    """Canonicalize a CNF block formula without changing raw occurrences."""

    unique_blocks = {
        frozenset(label for label in block if label)
        for block in blocks
    }
    unique_blocks.discard(frozenset())
    absorbed = {
        block
        for block in unique_blocks
        if any(other < block for other in unique_blocks)
    }
    retained = unique_blocks - absorbed
    ordered = [tuple(sorted(block, key=_chebi_sort_key)) for block in retained]
    return tuple(
        sorted(
            ordered,
            key=lambda block: tuple(_chebi_sort_key(label) for label in block),
        )
    )


def _reference_index(entry: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    references: dict[int, Mapping[str, Any]] = {}
    seen_numbers: set[int] = set()
    for raw_reference in entry.get("references", ()):
        if not isinstance(raw_reference, Mapping):
            continue
        number = _optional_int(raw_reference.get("referenceNumber"))
        if number is None:
            continue
        if number in seen_numbers:
            references.pop(number, None)
            continue
        seen_numbers.add(number)
        if isinstance(raw_reference.get("citation"), Mapping):
            references[number] = raw_reference
    return references


def _reference_number(source_id: str | None) -> int | None:
    if source_id is None:
        return None
    match = _REFERENCE_ID_PATTERN.fullmatch(source_id)
    return int(match.group(1)) if match is not None else None


def _parse_evidence(
    raw_evidence: object,
    source_ordinal: int,
    references: Mapping[int, Mapping[str, Any]],
) -> Evidence:
    raw = raw_evidence if isinstance(raw_evidence, Mapping) else {}
    evidence_code = _optional_string(raw.get("evidenceCode"))
    source = _optional_string(raw.get("source"))
    source_id = _optional_string(raw.get("id"))
    reference_number = _reference_number(source_id) if source == "Reference" else None
    resolved_reference = (
        references.get(reference_number) if reference_number is not None else None
    )

    accepted = False
    if evidence_code != EXPERIMENTAL_EVIDENCE_CODE:
        resolution_status = "NOT_EXPERIMENTAL"
    elif source == "PubMed":
        accepted = True
        resolution_status = "DIRECT_PUBMED"
    elif source == "Reference" and resolved_reference is not None:
        accepted = True
        resolution_status = "RESOLVED_REFERENCE"
    elif source == "Reference":
        resolution_status = "UNRESOLVED_REFERENCE"
    else:
        resolution_status = "UNSUPPORTED_SOURCE"

    return Evidence(
        source_ordinal=source_ordinal,
        evidence_code=evidence_code,
        source=source,
        source_id=source_id,
        accepted_for_experimental=accepted,
        resolution_status=resolution_status,
        reference_number=reference_number,
        resolved_reference=(
            deepcopy(dict(resolved_reference)) if resolved_reference is not None else None
        ),
    )


def _parse_evidences(
    raw_evidences: object,
    references: Mapping[int, Mapping[str, Any]],
) -> tuple[Evidence, ...]:
    if not isinstance(raw_evidences, Sequence) or isinstance(raw_evidences, str):
        return ()
    return tuple(
        _parse_evidence(raw, ordinal, references)
        for ordinal, raw in enumerate(raw_evidences, start=1)
    )


def _parse_notes(
    raw_note: object,
    references: Mapping[int, Mapping[str, Any]],
) -> tuple[NoteText, ...]:
    if not isinstance(raw_note, Mapping):
        return ()
    raw_texts = raw_note.get("texts", ())
    if not isinstance(raw_texts, Sequence) or isinstance(raw_texts, str):
        return ()
    notes: list[NoteText] = []
    for ordinal, raw_text in enumerate(raw_texts, start=1):
        if not isinstance(raw_text, Mapping):
            continue
        notes.append(
            NoteText(
                source_ordinal=ordinal,
                value=_optional_string(raw_text.get("value")) or "",
                evidences=_parse_evidences(raw_text.get("evidences", ()), references),
            )
        )
    return tuple(notes)


def _parse_block(
    raw_block: Mapping[str, Any],
    *,
    accession: str,
    block_ordinal: int,
    references: Mapping[int, Mapping[str, Any]],
) -> CofactorBlock:
    raw_cofactors = raw_block.get("cofactors", ())
    if not isinstance(raw_cofactors, Sequence) or isinstance(raw_cofactors, str):
        raw_cofactors = ()
    occurrences: list[LabelOccurrence] = []
    for occurrence_ordinal, raw_cofactor in enumerate(raw_cofactors, start=1):
        cofactor = raw_cofactor if isinstance(raw_cofactor, Mapping) else {}
        cross_reference = cofactor.get("cofactorCrossReference", {})
        cross_reference = (
            cross_reference if isinstance(cross_reference, Mapping) else {}
        )
        database = _optional_string(cross_reference.get("database"))
        cross_reference_id = _optional_string(cross_reference.get("id"))
        chebi_id = cross_reference_id if database == "ChEBI" else None
        evidences = _parse_evidences(cofactor.get("evidences", ()), references)
        experimental = chebi_id is not None and any(
            evidence.accepted_for_experimental for evidence in evidences
        )
        occurrences.append(
            LabelOccurrence(
                occurrence_id=(
                    f"{accession or 'missing-accession'}:"
                    f"block-{block_ordinal}:occurrence-{occurrence_ordinal}"
                ),
                source_ordinal=occurrence_ordinal,
                name=_optional_string(cofactor.get("name")),
                cross_reference_database=database,
                chebi_id=chebi_id,
                evidences=evidences,
                experimental=experimental,
            )
        )
    return CofactorBlock(
        source_ordinal=block_ordinal,
        molecule=_optional_string(raw_block.get("molecule")),
        notes=_parse_notes(raw_block.get("note"), references),
        label_occurrences=tuple(occurrences),
    )


def _extract_ec_numbers(protein_description: object) -> tuple[str, ...]:
    values: set[str] = set()

    def visit(node: object) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if key == "ecNumbers" and isinstance(value, Sequence):
                    for raw_ec in value:
                        if isinstance(raw_ec, Mapping):
                            ec_number = _optional_string(raw_ec.get("value"))
                            if ec_number:
                                values.add(ec_number)
                else:
                    visit(value)
        elif isinstance(node, Sequence) and not isinstance(node, str):
            for item in node:
                visit(item)

    visit(protein_description)
    return tuple(sorted(values))


def _alphabet(sequence: str | None) -> tuple[AlphabetStatus, tuple[str, ...]]:
    if sequence is None:
        return (AlphabetStatus.MISSING, ())
    nonstandard = tuple(sorted(set(sequence) - _STANDARD_AMINO_ACIDS))
    symbols = set(nonstandard)
    if symbols - {"U", "X"}:
        status = AlphabetStatus.OTHER_NONSTANDARD
    elif symbols == {"U", "X"}:
        status = AlphabetStatus.SELENOCYSTEINE_U_AND_UNKNOWN_X
    elif symbols == {"U"}:
        status = AlphabetStatus.SELENOCYSTEINE_U
    elif symbols == {"X"}:
        status = AlphabetStatus.UNKNOWN_X
    else:
        status = AlphabetStatus.STANDARD
    return (status, nonstandard)


def _formula_shape(
    experimental_label_ids: tuple[str, ...],
    raw_experimental_blocks: Sequence[Sequence[str]],
) -> FormulaShape:
    if not experimental_label_ids:
        return FormulaShape.NONE
    if len(experimental_label_ids) == 1:
        return FormulaShape.SINGLE
    active_blocks = [set(block) for block in raw_experimental_blocks if block]
    if len(active_blocks) == 1:
        return FormulaShape.PURE_OR
    if all(len(block) == 1 for block in active_blocks):
        return FormulaShape.PURE_AND
    return FormulaShape.MIXED_AND_OR


def _evidence_status(
    occurrences: Sequence[LabelOccurrence],
) -> EvidenceStatus | None:
    experimental = [item for item in occurrences if item.experimental]
    if not experimental:
        return None
    direct_count = sum(item.has_direct_pubmed for item in experimental)
    if direct_count == len(experimental):
        return EvidenceStatus.ALL_DIRECT_PUBMED
    if direct_count == 0:
        return EvidenceStatus.ALL_REFERENCE_ONLY
    return EvidenceStatus.MIXED_DIRECT_AND_REFERENCE


def parse_uniprot_entry(
    entry: Mapping[str, Any],
    *,
    raw_record_index: int,
) -> ParsedUniProtEntry:
    """Parse one UniProt JSON entry without flattening source block semantics."""

    accession = _optional_string(entry.get("primaryAccession")) or ""
    references = _reference_index(entry)
    blocks: list[CofactorBlock] = []
    raw_comments = entry.get("comments", ())
    if isinstance(raw_comments, Sequence) and not isinstance(raw_comments, str):
        for raw_comment in raw_comments:
            if not isinstance(raw_comment, Mapping):
                continue
            if raw_comment.get("commentType") != "COFACTOR":
                continue
            blocks.append(
                _parse_block(
                    raw_comment,
                    accession=accession,
                    block_ordinal=len(blocks) + 1,
                    references=references,
                )
            )

    occurrences = [
        occurrence for block in blocks for occurrence in block.label_occurrences
    ]
    experimental_occurrences = [item for item in occurrences if item.experimental]
    experimental_label_ids = tuple(
        sorted(
            {item.chebi_id for item in experimental_occurrences if item.chebi_id},
            key=_chebi_sort_key,
        )
    )
    all_cofactor_label_ids = tuple(
        sorted(
            {item.chebi_id for item in occurrences if item.chebi_id},
            key=_chebi_sort_key,
        )
    )
    raw_experimental_blocks = [
        [
            occurrence.chebi_id
            for occurrence in block.label_occurrences
            if occurrence.experimental and occurrence.chebi_id is not None
        ]
        for block in blocks
    ]
    gold_formula = canonicalize_formula(raw_experimental_blocks)

    sequence_object = entry.get("sequence", {})
    sequence_object = sequence_object if isinstance(sequence_object, Mapping) else {}
    sequence_value = _optional_string(sequence_object.get("value"))
    alphabet_status, nonstandard_symbols = _alphabet(sequence_value)
    audit = entry.get("entryAudit", {})
    audit = audit if isinstance(audit, Mapping) else {}
    organism = entry.get("organism", {})
    organism = organism if isinstance(organism, Mapping) else {}

    return ParsedUniProtEntry(
        raw_record_index=raw_record_index,
        accession=accession,
        uniprot_id=_optional_string(entry.get("uniProtkbId")),
        entry_type=_optional_string(entry.get("entryType")),
        entry_version=_optional_int(audit.get("entryVersion")),
        sequence_version=_optional_int(audit.get("sequenceVersion")),
        last_annotation_update_date=_optional_string(
            audit.get("lastAnnotationUpdateDate")
        ),
        organism_scientific_name=_optional_string(organism.get("scientificName")),
        organism_taxon_id=_optional_int(organism.get("taxonId")),
        ec_numbers=_extract_ec_numbers(entry.get("proteinDescription", {})),
        sequence_value=sequence_value,
        reported_sequence_length=_optional_int(sequence_object.get("length")),
        sequence_crc64=_optional_string(sequence_object.get("crc64")),
        sequence_sha256=(
            hashlib.sha256(sequence_value.encode("ascii")).hexdigest()
            if sequence_value is not None
            else None
        ),
        alphabet_status=alphabet_status,
        nonstandard_symbols=nonstandard_symbols,
        cofactor_blocks=tuple(blocks),
        experimental_label_ids=experimental_label_ids,
        all_cofactor_label_ids=all_cofactor_label_ids,
        gold_formula=gold_formula,
        formula_shape=_formula_shape(
            experimental_label_ids,
            raw_experimental_blocks,
        ),
        evidence_status=_evidence_status(occurrences),
        experimental_occurrence_count=len(experimental_occurrences),
    )
