"""Minimal ChEBI is-a graph analysis used by Cofactor9.1."""

from __future__ import annotations

from collections import defaultdict, deque
import re
from typing import Any, Iterable


_CHEBI_IRI = re.compile(r"http://purl\.obolibrary\.org/obo/CHEBI_(\d+)$")


def _to_chebi(identifier: str) -> str | None:
    match = _CHEBI_IRI.fullmatch(identifier)
    return f"CHEBI:{match.group(1)}" if match else None


def analyze_target_ancestry(
    graph: dict[str, Any], target_ids: Iterable[str]
) -> dict[str, Any]:
    """Find shortest target-to-target ancestor paths in a ChEBI graph."""

    targets = set(target_ids)
    node_ids = {
        chebi
        for node in graph.get("nodes", [])
        if (chebi := _to_chebi(str(node.get("id", "")))) is not None
    }
    parents: dict[str, set[str]] = defaultdict(set)
    for edge in graph.get("edges", []):
        if edge.get("pred") != "is_a":
            continue
        child = _to_chebi(str(edge.get("sub", "")))
        parent = _to_chebi(str(edge.get("obj", "")))
        if child and parent:
            parents[child].add(parent)

    pairs: list[dict[str, Any]] = []
    for specific in sorted(targets):
        queue = deque([(specific, 0)])
        visited = {specific}
        while queue:
            current, distance = queue.popleft()
            for parent in sorted(parents.get(current, ())):
                if parent in visited:
                    continue
                visited.add(parent)
                next_distance = distance + 1
                if parent in targets:
                    pairs.append(
                        {
                            "specific": specific,
                            "ancestor": parent,
                            "distance": next_distance,
                        }
                    )
                queue.append((parent, next_distance))

    pairs.sort(key=lambda item: (item["specific"], item["ancestor"]))
    return {
        "pairs": pairs,
        "ancestor_targets": sorted({item["ancestor"] for item in pairs}),
        "terms_in_overlap": sorted(
            {item["specific"] for item in pairs}
            | {item["ancestor"] for item in pairs}
        ),
        "missing_target_nodes": sorted(targets - node_ids),
    }
