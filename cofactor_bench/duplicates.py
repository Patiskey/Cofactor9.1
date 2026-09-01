"""Exact-sequence grouping with conflict-safe representative selection."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable


def _formula_key(formula: Iterable[Iterable[str]]) -> tuple[tuple[str, ...], ...]:
    blocks = {tuple(sorted(set(block))) for block in formula}
    return tuple(sorted(blocks))


def analyze_exact_sequence_groups(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Annotate exact sequence groups without resolving label conflicts."""

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[record["sequence"]["sha256"]].append(record)

    by_accession: dict[str, dict[str, Any]] = {}
    duplicate_groups = duplicate_entries = conflict_groups = conflict_entries = 0
    for sequence_hash, members in sorted(groups.items()):
        accessions = sorted(member["entry"]["accession"] for member in members)
        representative = accessions[0]
        duplicate = len(members) > 1
        formulas = {
            _formula_key(member["derived"]["gold_formula"]) for member in members
        }
        conflict = duplicate and len(formulas) > 1
        if duplicate:
            duplicate_groups += 1
            duplicate_entries += len(members)
        if conflict:
            conflict_groups += 1
            conflict_entries += len(members)

        status = (
            "DUPLICATE_CONFLICT"
            if conflict
            else "DUPLICATE_CONSISTENT"
            if duplicate
            else "UNIQUE"
        )
        for accession in accessions:
            reasons: list[str] = []
            if duplicate:
                reasons.append("EXACT_SEQUENCE_DUPLICATE")
            if conflict:
                reasons.append("EXACT_SEQUENCE_LABEL_CONFLICT")
            by_accession[accession] = {
                "sequence_entity_id": sequence_hash,
                "status": status,
                "members": accessions,
                "representative_accession": representative,
                "is_representative": accession == representative,
                "reason_codes": reasons,
            }

    return {
        "by_accession": by_accession,
        "summary": {
            "sequence_entities": len(groups),
            "duplicate_groups": duplicate_groups,
            "duplicate_entries": duplicate_entries,
            "conflict_groups": conflict_groups,
            "conflict_entries": conflict_entries,
        },
    }
