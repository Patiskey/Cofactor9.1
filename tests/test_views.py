from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cofactor_bench.views import (
    _resolve_path,
    _validate_frozen,
    build_views,
    build_views_from_config,
    derive_view_artifacts,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FROZEN_MASTER = PROJECT_ROOT / "data/derived/master.jsonl"
FROZEN_CHEBI = PROJECT_ROOT / "data/raw/chebi_lite_2026-08-14.json.gz"


def _occurrence(
    chebi_id: str,
    name: str,
    *,
    experimental: bool,
) -> dict[str, object]:
    return {
        "source_ordinal": 0,
        "chebi_id": chebi_id,
        "name": name,
        "experimental": experimental,
        "evidences": [],
    }


def _record(
    accession: str,
    sequence: str,
    labels: list[tuple[str, str, bool]],
    *,
    formula: list[list[str]],
    formula_shape: str = "SINGLE",
    note: str = "",
    molecule: str | None = None,
    existing_reasons: list[str] | None = None,
) -> dict[str, object]:
    experimental_ids = sorted(
        {chebi_id for chebi_id, _name, experimental in labels if experimental}
    )
    all_ids = sorted({chebi_id for chebi_id, _name, _experimental in labels})
    return {
        "schema_version": "cofactor9.1.master.v1",
        "dataset_version": "Cofactor9.1-test",
        "source": {"raw_record_index": 0},
        "entry": {"accession": accession},
        "sequence": {
            "value": sequence,
            "length": len(sequence),
            "sha256": hashlib.sha256(sequence.encode("ascii")).hexdigest(),
            "alphabet_status": "STANDARD",
            "nonstandard_symbols": [],
        },
        "cofactor_blocks": [
            {
                "source_ordinal": 0,
                "molecule": molecule,
                "notes": ([{"value": note, "evidences": []}] if note else []),
                "label_occurrences": [
                    _occurrence(chebi_id, name, experimental=experimental)
                    for chebi_id, name, experimental in labels
                ],
            }
        ],
        "derived": {
            "experimental_label_ids": experimental_ids,
            "all_cofactor_label_ids": all_ids,
            "gold_formula": formula,
            "formula_shape": formula_shape,
            "evidence_status": "ALL_DIRECT_PUBMED",
            "experimental_occurrence_count": sum(
                experimental for _chebi_id, _name, experimental in labels
            ),
            "reason_codes": list(existing_reasons or []),
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


def _synthetic_records() -> list[dict[str, object]]:
    return [
        _record(
            "A00001",
            "ACU",
            [("CHEBI:2", "magnesium", True)],
            formula=[["CHEBI:2"]],
            existing_reasons=["EXISTING_AUDIT_FLAG"],
        ),
        _record(
            "B00002",
            "ACU",
            [("CHEBI:2", "magnesium", True)],
            formula=[["CHEBI:2"]],
            note="Can also use zinc with lower efficiency.",
        ),
        _record(
            "C00003",
            "ACX",
            [("CHEBI:1", "metal ion", True)],
            formula=[["CHEBI:1"]],
            molecule="Isoform 2",
        ),
        _record(
            "D00004",
            "AAAA",
            [
                ("CHEBI:3", "zinc ion", True),
                ("CHEBI:2", "magnesium", False),
            ],
            formula=[["CHEBI:3"]],
        ),
        _record(
            "E00005",
            "CCCC",
            [("CHEBI:2", "magnesium", True)],
            formula=[["CHEBI:2"]],
        ),
        _record(
            "F00006",
            "CCCC",
            [("CHEBI:3", "zinc ion", True)],
            formula=[["CHEBI:3"]],
        ),
        _record(
            "G00007",
            "GGGG",
            [
                ("CHEBI:2", "magnesium", True),
                ("CHEBI:3", "zinc ion", True),
            ],
            formula=[["CHEBI:2", "CHEBI:3"]],
            formula_shape="PURE_OR",
        ),
    ]


def _synthetic_graph() -> dict[str, object]:
    prefix = "http://purl.obolibrary.org/obo/CHEBI_"
    return {
        "nodes": [
            {"id": f"{prefix}1", "lbl": "metal ion"},
            {"id": f"{prefix}2", "lbl": "magnesium(2+)"},
            {"id": f"{prefix}3", "lbl": "zinc(2+)"},
        ],
        "edges": [
            {
                "sub": f"{prefix}2",
                "pred": "is_a",
                "obj": f"{prefix}1",
            }
        ],
    }


class SyntheticViewTests(unittest.TestCase):
    def test_config_build_verifies_the_published_foundation_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_directory = root / "config"
            config_directory.mkdir()
            config_path = config_directory / "benchmark.json"
            paths = {
                "master": "data/derived/master.jsonl",
                "chebi_raw": "data/raw/chebi.json.gz",
                "foundation_generation": "data/manifests/foundation.json",
                "source_candidates": "data/derived/source.jsonl",
                "full_structured": "data/derived/full.jsonl",
                "single_clean": "data/derived/single.jsonl",
                "core_provisional": "data/derived/core.jsonl",
                "ambiguity_challenge": "data/derived/challenge.jsonl",
                "label_catalog": "data/derived/catalog.json",
                "reports": "reports",
            }
            config_path.write_text(
                json.dumps(
                    {
                        "dataset_version": "Cofactor9.1-test",
                        "paths": paths,
                        "source": {
                            "chebi": {
                                "artifact_sha256": "b" * 64,
                                "decompressed_content_sha256": "c" * 64,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            sentinel = object()
            with (
                patch(
                    "cofactor_bench.build.verify_foundation_generation",
                    return_value={
                        "artifacts": {"master": {"sha256": "a" * 64}}
                    },
                ) as verify,
                patch("cofactor_bench.views.build_views", return_value=sentinel) as build,
            ):
                observed = build_views_from_config(config_path)

            self.assertIs(observed, sentinel)
            verify.assert_called_once_with(config_path.resolve())
            self.assertEqual(
                build.call_args.kwargs["expected_input_hashes"],
                {
                    "master_sha256": "a" * 64,
                    "chebi_artifact_sha256": "b" * 64,
                    "chebi_decompressed_content_sha256": "c" * 64,
                },
            )

    def test_configured_view_paths_cannot_escape_the_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.assertEqual(
                _resolve_path(root, "data/derived/full.jsonl"),
                (root / "data/derived/full.jsonl").resolve(),
            )
            for configured in ("../outside.jsonl", str(root / "absolute.jsonl")):
                with self.subTest(configured=configured):
                    with self.assertRaisesRegex(ValueError, "project"):
                        _resolve_path(root, configured)

    def test_derives_additive_reasons_and_explicit_view_membership(self) -> None:
        artifacts = derive_view_artifacts(
            _synthetic_records(),
            _synthetic_graph(),
            dataset_version="Cofactor9.1-test",
            input_hashes={"master_sha256": "a" * 64, "chebi_sha256": "b" * 64},
        )

        self.assertEqual(len(artifacts.full_structured), 7)
        self.assertEqual(
            [row["entry"]["accession"] for row in artifacts.single_clean],
            ["A00001", "B00002", "C00003", "E00005", "F00006"],
        )
        self.assertEqual(
            [row["entry"]["accession"] for row in artifacts.core_provisional],
            ["A00001"],
        )
        self.assertEqual(
            [row["entry"]["accession"] for row in artifacts.ambiguity_challenge],
            ["B00002", "C00003", "D00004", "E00005", "F00006", "G00007"],
        )

        by_accession = {
            row["entry"]["accession"]: row for row in artifacts.full_structured
        }
        retained_u = by_accession["A00001"]
        self.assertIn("EXISTING_AUDIT_FLAG", retained_u["derived"]["reason_codes"])
        self.assertIn("SELENOCYSTEINE_U", retained_u["derived"]["reason_codes"])
        self.assertTrue(
            retained_u["derived"]["view_membership"]["core_provisional"]["included"]
        )

        scoped_ancestor = by_accession["C00003"]
        self.assertEqual(scoped_ancestor["derived"]["note_triage"]["status"], "NOT_REQUIRED")
        self.assertIn("MOLECULE_SCOPE", scoped_ancestor["derived"]["reason_codes"])
        self.assertIn(
            "ONTOLOGY_ANCESTOR_TARGET",
            scoped_ancestor["derived"]["reason_codes"],
        )
        self.assertIn("UNKNOWN_RESIDUE_X", scoped_ancestor["derived"]["reason_codes"])

        note_risk = by_accession["B00002"]
        self.assertEqual(note_risk["adjudication"]["status"], "PENDING")
        self.assertIsNone(note_risk["adjudication"]["decision"])
        self.assertIn(
            "NOTE_ALTERNATIVE_OR_COMPARISON",
            note_risk["derived"]["reason_codes"],
        )
        self.assertFalse(
            note_risk["derived"]["view_membership"]["core_provisional"]["included"]
        )

        conflict = by_accession["E00005"]
        self.assertEqual(
            conflict["derived"]["exact_sequence"]["status"],
            "DUPLICATE_CONFLICT",
        )
        self.assertIn(
            "EXACT_SEQUENCE_LABEL_CONFLICT",
            conflict["derived"]["view_membership"]["ambiguity_challenge"][
                "reason_codes"
            ],
        )
        self.assertEqual(conflict["adjudication"]["status"], "PENDING")
        self.assertEqual(by_accession["F00006"]["adjudication"]["status"], "PENDING")

    def test_marks_cross_block_overlap_without_dropping_the_record(self) -> None:
        overlapping = _record(
            "H00008",
            "MMMM",
            [
                ("CHEBI:2", "magnesium", True),
                ("CHEBI:3", "zinc ion", True),
            ],
            formula=[["CHEBI:2"], ["CHEBI:2", "CHEBI:3"]],
            formula_shape="MIXED_AND_OR",
        )

        artifacts = derive_view_artifacts(
            [overlapping],
            _synthetic_graph(),
            dataset_version="Cofactor9.1-test",
            input_hashes={"master_sha256": "a" * 64, "chebi_sha256": "b" * 64},
        )

        self.assertEqual(len(artifacts.full_structured), 1)
        self.assertEqual(len(artifacts.ambiguity_challenge), 1)
        row = artifacts.full_structured[0]
        self.assertIn(
            "OVERLAPPING_BLOCK_LABEL",
            row["derived"]["reason_codes"],
        )
        self.assertIn(
            "OVERLAPPING_BLOCK_LABEL",
            row["derived"]["view_membership"]["ambiguity_challenge"][
                "reason_codes"
            ],
        )
        self.assertEqual(
            artifacts.view_audit["summary"]["formula"],
            {
                "canonical_block_count": 2,
                "canonical_block_label_overlap_count": 1,
                "overlapping_block_label_accession_count": 1,
            },
        )

    def test_core_selects_a_representative_after_entry_quality_filtering(self) -> None:
        dirty_first = _record(
            "A00001",
            "MMMM",
            [
                ("CHEBI:2", "magnesium", True),
                ("CHEBI:3", "zinc ion", False),
            ],
            formula=[["CHEBI:2"]],
        )
        clean_second = _record(
            "Z00002",
            "MMMM",
            [("CHEBI:2", "magnesium", True)],
            formula=[["CHEBI:2"]],
        )

        artifacts = derive_view_artifacts(
            [dirty_first, clean_second],
            _synthetic_graph(),
            dataset_version="Cofactor9.1-test",
            input_hashes={"master_sha256": "a" * 64, "chebi_sha256": "b" * 64},
        )

        self.assertEqual(
            [row["entry"]["accession"] for row in artifacts.core_provisional],
            ["Z00002"],
        )
        by_accession = {
            row["entry"]["accession"]: row for row in artifacts.full_structured
        }
        dirty_exact = by_accession["A00001"]["derived"]["exact_sequence"]
        clean_exact = by_accession["Z00002"]["derived"]["exact_sequence"]
        self.assertTrue(dirty_exact["is_representative"])
        self.assertFalse(dirty_exact["is_core_representative"])
        self.assertEqual(
            dirty_exact["core_representative_accession"],
            "Z00002",
        )
        self.assertFalse(clean_exact["is_representative"])
        self.assertTrue(clean_exact["is_core_representative"])

    def test_label_catalog_uses_validated_chebi_names_and_audits_uniprot_names(self) -> None:
        artifacts = derive_view_artifacts(
            _synthetic_records(),
            _synthetic_graph(),
            dataset_version="Cofactor9.1-test",
            input_hashes={"master_sha256": "a" * 64, "chebi_sha256": "b" * 64},
        )

        labels = artifacts.label_catalog["labels"]
        self.assertEqual(
            [item["chebi_id"] for item in labels],
            ["CHEBI:1", "CHEBI:2", "CHEBI:3"],
        )
        self.assertEqual(labels[1]["name"], "magnesium(2+)")
        self.assertEqual(labels[1]["uniprot_display_name"], "magnesium")
        self.assertEqual(labels[1]["master_accession_count"], 4)
        self.assertEqual(labels[1]["frequency_band"], "tail")
        self.assertEqual(
            artifacts.label_catalog["uniprot_display_name_audit"]["nonunique_ids"],
            [],
        )
        self.assertEqual(
            artifacts.label_catalog["chebi_name_audit"],
            {
                "target_label_count": 3,
                "ids_with_exactly_one_named_active_node": 3,
                "missing_node_ids": [],
                "duplicate_node_ids": [],
                "missing_label_ids": [],
                "deprecated_ids": [],
                "duplicate_preferred_labels": [],
            },
        )

        inconsistent = _synthetic_records()
        extra = deepcopy(inconsistent[0])
        extra["entry"]["accession"] = "H00008"
        extra["sequence"]["value"] = "HHHH"
        extra["sequence"]["sha256"] = hashlib.sha256(b"HHHH").hexdigest()
        extra["cofactor_blocks"][0]["label_occurrences"][0]["name"] = "Mg ion"
        inconsistent.append(extra)
        with self.assertRaisesRegex(ValueError, "non-unique UniProt cofactor names"):
            derive_view_artifacts(
                inconsistent,
                _synthetic_graph(),
                dataset_version="Cofactor9.1-test",
                input_hashes={
                    "master_sha256": "a" * 64,
                    "chebi_sha256": "b" * 64,
                },
            )

        deprecated_graph = _synthetic_graph()
        deprecated_graph["nodes"][1]["meta"] = {"deprecated": True}
        with self.assertRaisesRegex(ValueError, "deprecated ChEBI target nodes"):
            derive_view_artifacts(
                _synthetic_records(),
                deprecated_graph,
                dataset_version="Cofactor9.1-test",
                input_hashes={
                    "master_sha256": "a" * 64,
                    "chebi_sha256": "b" * 64,
                },
            )

        duplicate_graph = _synthetic_graph()
        duplicate_graph["nodes"].append(
            {
                "id": "http://purl.obolibrary.org/obo/CHEBI_2",
                "lbl": "duplicate magnesium",
            }
        )
        with self.assertRaisesRegex(ValueError, "duplicate ChEBI target nodes"):
            derive_view_artifacts(
                _synthetic_records(),
                duplicate_graph,
                dataset_version="Cofactor9.1-test",
                input_hashes={
                    "master_sha256": "a" * 64,
                    "chebi_sha256": "b" * 64,
                },
            )

        missing_graph = _synthetic_graph()
        missing_graph["nodes"] = missing_graph["nodes"][:-1]
        with self.assertRaisesRegex(ValueError, "missing ChEBI target nodes"):
            derive_view_artifacts(
                _synthetic_records(),
                missing_graph,
                dataset_version="Cofactor9.1-test",
                input_hashes={
                    "master_sha256": "a" * 64,
                    "chebi_sha256": "b" * 64,
                },
            )

        unnamed_graph = _synthetic_graph()
        del unnamed_graph["nodes"][2]["lbl"]
        with self.assertRaisesRegex(ValueError, "without preferred labels"):
            derive_view_artifacts(
                _synthetic_records(),
                unnamed_graph,
                dataset_version="Cofactor9.1-test",
                input_hashes={
                    "master_sha256": "a" * 64,
                    "chebi_sha256": "b" * 64,
                },
            )

        duplicate_name_graph = _synthetic_graph()
        duplicate_name_graph["nodes"][2]["lbl"] = "magnesium(2+)"
        with self.assertRaisesRegex(ValueError, "duplicate ChEBI preferred labels"):
            derive_view_artifacts(
                _synthetic_records(),
                duplicate_name_graph,
                dataset_version="Cofactor9.1-test",
                input_hashes={
                    "master_sha256": "a" * 64,
                    "chebi_sha256": "b" * 64,
                },
            )

    def test_build_writes_deterministic_atomic_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            master_path = root / "master.jsonl"
            master_path.write_text(
                "".join(
                    json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                    for row in reversed(_synthetic_records())
                ),
                encoding="utf-8",
            )
            chebi_path = root / "chebi.json.gz"
            with gzip.open(chebi_path, "wt", encoding="utf-8") as handle:
                json.dump({"graphs": [_synthetic_graph()]}, handle)

            paths = {
                "full_structured": root / "full_structured.jsonl",
                "single_clean": root / "single_clean.jsonl",
                "core_provisional": root / "core_provisional.jsonl",
                "ambiguity_challenge": root / "ambiguity_challenge.jsonl",
                "label_catalog": root / "label_catalog.json",
                "ontology_audit": root / "ontology_audit.json",
                "view_audit": root / "view_audit.json",
                "report": root / "data_audit.md",
            }
            first = build_views(
                master_path=master_path,
                chebi_path=chebi_path,
                output_paths=paths,
                dataset_version="Cofactor9.1-test",
            )
            first_bytes = {name: path.read_bytes() for name, path in paths.items()}
            second = build_views(
                master_path=master_path,
                chebi_path=chebi_path,
                output_paths=paths,
                dataset_version="Cofactor9.1-test",
            )

            self.assertEqual(first.output_sha256, second.output_sha256)
            self.assertEqual(
                first_bytes,
                {name: path.read_bytes() for name, path in paths.items()},
            )
            self.assertEqual(first.full_structured_accessions, 7)
            self.assertEqual(first.single_clean_accessions, 5)
            self.assertEqual(first.core_provisional_accessions, 1)
            self.assertFalse(any(root.glob(".*.tmp")))
            first_row = json.loads(paths["full_structured"].read_text().splitlines()[0])
            self.assertEqual(first_row["entry"]["accession"], "A00001")
            self.assertEqual(first_row["schema_version"], "cofactor9.1.view-record.v1")
            self.assertEqual(first_row["derivation"]["rule_version"], "cofactor9.1.views.v3")
            self.assertEqual(
                set(first_row["derivation"]["input_hashes"]),
                {"chebi_artifact_sha256", "chebi_decompressed_content_sha256", "master_sha256"},
            )
            stable_bytes = {
                name: path.read_bytes() for name, path in paths.items()
            }
            with self.assertRaisesRegex(ValueError, "chebi_artifact_sha256"):
                build_views(
                    master_path=master_path,
                    chebi_path=chebi_path,
                    output_paths=paths,
                    dataset_version="Cofactor9.1-test",
                    expected_input_hashes={
                        "chebi_artifact_sha256": "0" * 64,
                    },
                )
            self.assertEqual(
                {name: path.read_bytes() for name, path in paths.items()},
                stable_bytes,
            )

    def test_coordinated_outputs_roll_back_if_replacement_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            master_path = root / "master.jsonl"
            master_path.write_text(
                "".join(
                    json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                    for row in _synthetic_records()
                ),
                encoding="utf-8",
            )
            chebi_path = root / "chebi.json.gz"
            with gzip.open(chebi_path, "wt", encoding="utf-8") as handle:
                json.dump({"graphs": [_synthetic_graph()]}, handle)
            paths = {
                "full_structured": root / "full_structured.jsonl",
                "single_clean": root / "single_clean.jsonl",
                "core_provisional": root / "core_provisional.jsonl",
                "ambiguity_challenge": root / "ambiguity_challenge.jsonl",
                "label_catalog": root / "label_catalog.json",
                "ontology_audit": root / "ontology_audit.json",
                "view_audit": root / "view_audit.json",
                "report": root / "data_audit.md",
            }
            original = {
                name: f"old-{name}\n".encode("utf-8") for name in paths
            }
            for name, path in paths.items():
                path.write_bytes(original[name])

            real_replace = os.replace
            replacement_calls = 0

            def fail_second_replacement(source: object, target: object) -> None:
                nonlocal replacement_calls
                replacement_calls += 1
                if replacement_calls == 2:
                    raise OSError("synthetic replacement failure")
                real_replace(source, target)

            with patch("cofactor_bench.views.os.replace", fail_second_replacement):
                with self.assertRaisesRegex(OSError, "synthetic replacement failure"):
                    build_views(
                        master_path=master_path,
                        chebi_path=chebi_path,
                        output_paths=paths,
                        dataset_version="Cofactor9.1-test",
                    )

            self.assertEqual(
                {name: path.read_bytes() for name, path in paths.items()},
                original,
            )
            self.assertFalse(any(root.glob(".*.tmp")))
            self.assertFalse(any(root.glob(".*.rollback")))

    def test_rejects_frozen_input_output_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            master_path = root / "master.jsonl"
            master_bytes = "".join(
                json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                for row in _synthetic_records()
            ).encode("utf-8")
            master_path.write_bytes(master_bytes)
            chebi_path = root / "chebi.json.gz"
            with gzip.open(chebi_path, "wt", encoding="utf-8") as handle:
                json.dump({"graphs": [_synthetic_graph()]}, handle)
            paths = {
                "full_structured": master_path,
                "single_clean": root / "single_clean.jsonl",
                "core_provisional": root / "core_provisional.jsonl",
                "ambiguity_challenge": root / "ambiguity_challenge.jsonl",
                "label_catalog": root / "label_catalog.json",
                "ontology_audit": root / "ontology_audit.json",
                "view_audit": root / "view_audit.json",
                "report": root / "data_audit.md",
            }

            with self.assertRaisesRegex(ValueError, "inputs and outputs must be distinct"):
                build_views(
                    master_path=master_path,
                    chebi_path=chebi_path,
                    output_paths=paths,
                    dataset_version="Cofactor9.1-test",
                )

            self.assertEqual(master_path.read_bytes(), master_bytes)
            self.assertFalse(paths["single_clean"].exists())


@unittest.skipUnless(
    FROZEN_MASTER.exists() and FROZEN_CHEBI.exists(),
    "frozen Master or ChEBI artifact is unavailable",
)
class FrozenViewTests(unittest.TestCase):
    def test_frozen_views_reproduce_all_checkpoints_and_hash_stability(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = {
                "full_structured": root / "full_structured.jsonl",
                "single_clean": root / "single_clean.jsonl",
                "core_provisional": root / "core_provisional.jsonl",
                "ambiguity_challenge": root / "ambiguity_challenge.jsonl",
                "label_catalog": root / "label_catalog.json",
                "ontology_audit": root / "ontology_audit.json",
                "view_audit": root / "view_audit.json",
                "report": root / "data_audit.md",
            }
            first = build_views(
                master_path=FROZEN_MASTER,
                chebi_path=FROZEN_CHEBI,
                output_paths=paths,
                dataset_version="Cofactor9.1",
            )
            second = build_views(
                master_path=FROZEN_MASTER,
                chebi_path=FROZEN_CHEBI,
                output_paths=paths,
                dataset_version="Cofactor9.1",
            )

            self.assertEqual(first.output_sha256, second.output_sha256)
            self.assertEqual(first.full_structured_accessions, 5337)
            self.assertEqual(first.single_clean_accessions, 3971)
            self.assertEqual(first.single_clean_sequence_entities, 3942)
            self.assertEqual(first.single_clean_labels, 64)
            self.assertEqual(first.core_provisional_accessions, 3233)
            self.assertEqual(first.ambiguity_challenge_accessions, 2082)
            self.assertEqual(first.ontology_ancestor_pairs, 59)
            self.assertEqual(first.ontology_overlap_terms, 44)
            self.assertEqual(first.ontology_ancestor_targets, 11)
            self.assertEqual(first.ontology_missing_targets, 0)
            self.assertEqual(first.sequence_entities, 5295)
            self.assertEqual(first.duplicate_groups, 39)
            self.assertEqual(first.duplicate_entries, 81)
            self.assertEqual(first.conflict_groups, 6)
            self.assertEqual(first.conflict_entries, 12)
            self.assertEqual(first.canonical_block_count, 5911)
            self.assertEqual(first.canonical_block_label_overlap_count, 8)
            self.assertEqual(
                first.overlapping_block_label_accession_count,
                6,
            )
            self.assertEqual(first.note_pending_single_clean, 477)
            self.assertEqual(first.label_count, 104)
            self.assertEqual(
                first.frequency_band_counts,
                {"head": 14, "mid": 20, "tail": 70},
            )

            audit = json.loads(paths["view_audit"].read_text())
            diagnostics = audit["diagnostic_counts"]
            core_audit = audit["core_predicate_audit"]
            overlap_accessions = [
                "P0ABJ9",
                "Q57580",
                "Q6AYK3",
                "Q8NFF5",
                "Q9LNJ9",
                "Q9SIY3",
            ]
            self.assertEqual(
                audit["formula_audit"]["overlapping_block_label_accessions"],
                overlap_accessions,
            )
            self.assertEqual(
                audit["reason_code_accession_counts"][
                    "OVERLAPPING_BLOCK_LABEL"
                ],
                6,
            )
            self.assertEqual(
                audit["challenge_reason_accession_counts"][
                    "OVERLAPPING_BLOCK_LABEL"
                ],
                6,
            )
            full_overlap_accessions = [
                row["entry"]["accession"]
                for row in (
                    json.loads(line)
                    for line in paths["full_structured"].read_text().splitlines()
                )
                if "OVERLAPPING_BLOCK_LABEL" in row["derived"]["reason_codes"]
            ]
            challenge_accessions = {
                json.loads(line)["entry"]["accession"]
                for line in paths["ambiguity_challenge"].read_text().splitlines()
            }
            self.assertEqual(full_overlap_accessions, overlap_accessions)
            self.assertTrue(set(overlap_accessions).issubset(challenge_accessions))
            self.assertGreater(core_audit["sequence_with_selenocysteine_u"], 0)
            self.assertEqual(
                core_audit["violation_counts"],
                {
                    "not_single_clean": 0,
                    "ontology_ancestor_target": 0,
                    "unknown_residue_x": 0,
                    "molecule_scope": 0,
                    "exact_sequence_conflict": 0,
                    "not_core_sequence_representative": 0,
                    "note_triage_pending": 0,
                },
            )
            self.assertEqual(
                diagnostics["experimental_single_experimental_block_notes_pending"],
                538,
            )
            self.assertEqual(
                diagnostics["experimental_single_all_block_notes_pending"],
                560,
            )
            self.assertEqual(
                diagnostics[
                    "experimental_single_experimental_block_notes_or_molecule_scope_pending"
                ],
                542,
            )
            self.assertEqual(diagnostics["historical_note_pending_target"], 541)
            self.assertFalse(diagnostics["historical_note_pending_target_reproduced"])

            with self.assertRaisesRegex(ValueError, "core_provisional_accessions"):
                _validate_frozen(
                    replace(first, core_provisional_accessions=0),
                    diagnostics,
                )
            with self.assertRaisesRegex(
                ValueError,
                "ambiguity_challenge_accessions",
            ):
                _validate_frozen(
                    replace(first, ambiguity_challenge_accessions=0),
                    diagnostics,
                )


if __name__ == "__main__":
    unittest.main()
