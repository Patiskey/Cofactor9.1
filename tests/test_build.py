from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cofactor_bench.build import (
    build_from_config,
    build_snapshot,
    verify_foundation_generation,
    verify_source_manifest,
)
from cofactor_bench.model import EvidenceStatus, FormulaShape


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FROZEN_UNIPROT = PROJECT_ROOT / "data/raw/uniprotkb_cofactor_query_2026_02.json.gz"
FROZEN_CHEBI = PROJECT_ROOT / "data/raw/chebi_lite_2026-08-14.json.gz"
SOURCE_CONFIG = PROJECT_ROOT / "config/benchmark.json"


def _cofactor(chebi_id: str, evidence: dict[str, str]) -> dict[str, object]:
    return {
        "name": chebi_id,
        "cofactorCrossReference": {"database": "ChEBI", "id": chebi_id},
        "evidences": [evidence],
    }


def _synthetic_payload() -> dict[str, object]:
    return {
        "results": [
            {
                "primaryAccession": "B00002",
                "uniProtkbId": "REJECT_TEST",
                "entryAudit": {"entryVersion": 1, "sequenceVersion": 1},
                "comments": [
                    {
                        "commentType": "COFACTOR",
                        "cofactors": [
                            _cofactor(
                                "CHEBI:3",
                                {
                                    "evidenceCode": "ECO:0000255",
                                    "source": "HAMAP-Rule",
                                    "id": "MF_00003",
                                },
                            )
                        ],
                        "note": {
                            "texts": [
                                {
                                    "value": "Note evidence is not label evidence.",
                                    "evidences": [
                                        {
                                            "evidenceCode": "ECO:0000269",
                                            "source": "PubMed",
                                            "id": "30000000",
                                        }
                                    ],
                                }
                            ]
                        },
                    }
                ],
                "sequence": {"value": "MAAA", "length": 4, "crc64": "B"},
            },
            {
                "primaryAccession": "A00001",
                "uniProtkbId": "DIRECT_TEST",
                "entryAudit": {"entryVersion": 2, "sequenceVersion": 1},
                "comments": [
                    {
                        "commentType": "COFACTOR",
                        "molecule": "Isoform 1",
                        "cofactors": [
                            _cofactor(
                                "CHEBI:1",
                                {
                                    "evidenceCode": "ECO:0000269",
                                    "source": "PubMed",
                                    "id": "10000000",
                                },
                            )
                        ],
                    }
                ],
                "sequence": {"value": "MUA", "length": 3, "crc64": "A"},
            },
            {
                "primaryAccession": "C00003",
                "uniProtkbId": "REFERENCE_TEST",
                "entryAudit": {"entryVersion": 3, "sequenceVersion": 1},
                "references": [
                    {
                        "referenceNumber": 1,
                        "citation": {
                            "id": "CI-REFERENCE",
                            "citationType": "submission",
                            "title": "Resolved local reference",
                        },
                    }
                ],
                "comments": [
                    {
                        "commentType": "COFACTOR",
                        "cofactors": [
                            _cofactor(
                                "CHEBI:2",
                                {
                                    "evidenceCode": "ECO:0000269",
                                    "source": "Reference",
                                    "id": "Ref.1",
                                },
                            )
                        ],
                    }
                ],
                "sequence": {"value": "MXX", "length": 3, "crc64": "C"},
            },
        ]
    }


class SyntheticBuildTests(unittest.TestCase):
    def test_build_from_config_rejects_paths_outside_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            (project_root / "config").mkdir()
            config_path = project_root / "config/benchmark.json"
            config_path.write_text(
                json.dumps(
                    {
                        "schema_version": "cofactor9.1.config.v1",
                        "dataset_version": "Cofactor9.1-test",
                        "paths": {
                            "uniprot_raw": "data/source.json.gz",
                            "master": "../escaped-master.jsonl",
                            "source_candidates": "data/source_candidates.jsonl",
                            "source_manifest": "data/source-manifest.json",
                            "foundation_generation": "data/foundation-generation.json",
                        },
                        "source": {"uniprot": {}},
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch(
                "cofactor_bench.build.verify_source_manifest",
                return_value={
                    "uniprot_source_metadata": {
                        "release": "test",
                        "query": "test:true",
                        "artifact_sha256": "0" * 64,
                    }
                },
            ):
                with self.assertRaisesRegex(ValueError, "escapes project root"):
                    build_from_config(config_path)

    def test_rejects_input_output_aliases_before_opening_any_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            raw_path = root / "source.json.gz"
            with gzip.open(raw_path, "wt", encoding="utf-8") as handle:
                json.dump(_synthetic_payload(), handle)
            metadata = {
                "release": "test_release",
                "query": "synthetic:true",
                "artifact_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            }

            with self.assertRaisesRegex(ValueError, "distinct"):
                build_snapshot(
                    raw_path=raw_path,
                    master_path=raw_path,
                    source_candidates_path=root / "source_candidates.jsonl",
                    dataset_version="Cofactor9.1-test",
                    source_metadata=metadata,
                )
            self.assertTrue(raw_path.read_bytes().startswith(b"\x1f\x8b"))

            shared_output = root / "shared.jsonl"
            with self.assertRaisesRegex(ValueError, "distinct"):
                build_snapshot(
                    raw_path=raw_path,
                    master_path=shared_output,
                    source_candidates_path=shared_output,
                    dataset_version="Cofactor9.1-test",
                    source_metadata=metadata,
                )
            self.assertFalse(shared_output.exists())

    def test_requires_a_valid_source_hash_and_rejects_unknown_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            raw_path = root / "source.json.gz"
            with gzip.open(raw_path, "wt", encoding="utf-8") as handle:
                json.dump(_synthetic_payload(), handle)
            base = {"release": "test_release", "query": "synthetic:true"}

            for metadata in (
                base,
                {**base, "artifact_sha256": "not-a-sha256"},
                {
                    **base,
                    "artifact_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                    "api_token": "must-never-enter-master",
                },
            ):
                with self.subTest(metadata=metadata):
                    with self.assertRaises(ValueError):
                        build_snapshot(
                            raw_path=raw_path,
                            master_path=root / "master.jsonl",
                            source_candidates_path=root / "source_candidates.jsonl",
                            dataset_version="Cofactor9.1-test",
                            source_metadata=metadata,
                        )

    def test_output_pair_rolls_back_if_second_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            raw_path = root / "source.json.gz"
            master_path = root / "master.jsonl"
            audit_path = root / "source_candidates.jsonl"
            with gzip.open(raw_path, "wt", encoding="utf-8") as handle:
                json.dump(_synthetic_payload(), handle)
            master_path.write_text("old-master\n", encoding="utf-8")
            audit_path.write_text("old-audit\n", encoding="utf-8")
            real_replace = __import__("os").replace
            replace_count = 0

            def fail_second_replace(source: object, target: object) -> None:
                nonlocal replace_count
                replace_count += 1
                if replace_count == 2:
                    raise OSError("injected second replace failure")
                real_replace(source, target)

            with mock.patch(
                "cofactor_bench.build.os.replace",
                side_effect=fail_second_replace,
            ):
                with self.assertRaisesRegex(OSError, "injected"):
                    build_snapshot(
                        raw_path=raw_path,
                        master_path=master_path,
                        source_candidates_path=audit_path,
                        dataset_version="Cofactor9.1-test",
                        source_metadata={
                            "release": "test_release",
                            "query": "synthetic:true",
                            "artifact_sha256": hashlib.sha256(
                                raw_path.read_bytes()
                            ).hexdigest(),
                        },
                    )

            self.assertEqual(master_path.read_text(), "old-master\n")
            self.assertEqual(audit_path.read_text(), "old-audit\n")

    def test_build_from_config_resolves_paths_from_project_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            (project_root / "config").mkdir()
            (project_root / "data").mkdir()
            raw_path = project_root / "data/source.json.gz"
            with gzip.open(raw_path, "wt", encoding="utf-8") as handle:
                json.dump(_synthetic_payload(), handle)
            source_sha256 = hashlib.sha256(raw_path.read_bytes()).hexdigest()
            config_path = project_root / "config/benchmark.json"
            config_payload = {
                "schema_version": "cofactor9.1.config.v1",
                "dataset_version": "Cofactor9.1-test",
                "paths": {
                    "uniprot_raw": "data/source.json.gz",
                    "master": "data/master.jsonl",
                    "source_candidates": "data/source_candidates.jsonl",
                    "source_manifest": "data/source-manifest.json",
                    "foundation_generation": "data/foundation-generation.json",
                },
                "source": {
                    "uniprot": {
                        "release": "test_release",
                        "query": "synthetic:true",
                        "artifact_sha256": source_sha256,
                    }
                },
            }
            config_path.write_text(
                json.dumps(config_payload),
                encoding="utf-8",
            )

            verified = {
                "uniprot_source_metadata": {
                    "release": "test_release",
                    "query": "synthetic:true",
                    "artifact_sha256": source_sha256,
                }
            }
            with mock.patch(
                "cofactor_bench.build.verify_source_manifest",
                return_value=verified,
            ):
                summary = build_from_config(config_path)

            self.assertEqual(summary.master_record_count, 2)
            self.assertTrue((project_root / "data/master.jsonl").exists())
            self.assertTrue((project_root / "data/source_candidates.jsonl").exists())
            self.assertTrue((project_root / "data/foundation-generation.json").exists())
            verified_generation = verify_foundation_generation(config_path)
            self.assertEqual(
                verified_generation["artifacts"]["master"]["record_count"],
                2,
            )
            self.assertEqual(
                verified_generation["formula_rule_version"],
                "cofactor9.1.formula.v2",
            )
            with (project_root / "data/master.jsonl").open("a") as handle:
                handle.write("{}\n")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                verify_foundation_generation(config_path)

            config_payload["expected_foundation"] = {"master_record_count": 999}
            config_path.write_text(json.dumps(config_payload), encoding="utf-8")
            with mock.patch(
                "cofactor_bench.build.verify_source_manifest",
                return_value=verified,
            ):
                with self.assertRaisesRegex(ValueError, "master_record_count"):
                    build_from_config(config_path)

    def test_writes_complete_deterministic_master_and_source_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            raw_path = root / "source.json.gz"
            master_path = root / "master.jsonl"
            audit_path = root / "source_candidates.jsonl"
            with gzip.open(raw_path, "wt", encoding="utf-8") as handle:
                json.dump(_synthetic_payload(), handle)

            source_sha256 = hashlib.sha256(raw_path.read_bytes()).hexdigest()
            summary = build_snapshot(
                raw_path=raw_path,
                master_path=master_path,
                source_candidates_path=audit_path,
                dataset_version="Cofactor9.1-test",
                source_metadata={
                    "release": "test_release",
                    "query": "synthetic:true",
                    "artifact_sha256": source_sha256,
                },
            )

            self.assertEqual(summary.source_record_count, 3)
            self.assertEqual(summary.master_record_count, 2)
            self.assertEqual(summary.ineligible_record_count, 1)
            self.assertEqual(summary.experimental_label_count, 2)
            self.assertEqual(
                summary.evidence_status_counts,
                {
                    EvidenceStatus.ALL_DIRECT_PUBMED.value: 1,
                    EvidenceStatus.ALL_REFERENCE_ONLY.value: 1,
                },
            )

            master_rows = [json.loads(line) for line in master_path.read_text().splitlines()]
            self.assertEqual([row["entry"]["accession"] for row in master_rows], ["A00001", "C00003"])
            self.assertEqual(
                set(master_rows[0]),
                {
                    "schema_version",
                    "dataset_version",
                    "source",
                    "entry",
                    "sequence",
                    "cofactor_blocks",
                    "derived",
                    "adjudication",
                },
            )
            self.assertEqual(master_rows[0]["cofactor_blocks"][0]["molecule"], "Isoform 1")
            self.assertEqual(master_rows[0]["derived"]["experimental_label_ids"], ["CHEBI:1"])
            self.assertEqual(
                master_rows[0]["sequence"]["sha256"],
                hashlib.sha256(b"MUA").hexdigest(),
            )

            audit_rows = [json.loads(line) for line in audit_path.read_text().splitlines()]
            self.assertEqual([row["source"]["raw_record_index"] for row in audit_rows], [0, 1, 2])
            self.assertFalse(audit_rows[0]["eligible_for_master"])
            self.assertIn(
                "NO_ACCEPTED_EXPERIMENTAL_COFACTOR",
                audit_rows[0]["eligibility_reasons"],
            )

            first_master_bytes = master_path.read_bytes()
            first_audit_bytes = audit_path.read_bytes()
            build_snapshot(
                raw_path=raw_path,
                master_path=master_path,
                source_candidates_path=audit_path,
                dataset_version="Cofactor9.1-test",
                source_metadata={
                    "release": "test_release",
                    "query": "synthetic:true",
                    "artifact_sha256": source_sha256,
                },
            )
            self.assertEqual(master_path.read_bytes(), first_master_bytes)
            self.assertEqual(audit_path.read_bytes(), first_audit_bytes)

    def test_counts_cross_block_overlap_without_losing_formula_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            raw_path = root / "source.json.gz"
            payload = _synthetic_payload()
            direct_entry = payload["results"][1]
            direct_entry["comments"].append(
                {
                    "commentType": "COFACTOR",
                    "cofactors": [
                        _cofactor(
                            "CHEBI:1",
                            {
                                "evidenceCode": "ECO:0000269",
                                "source": "PubMed",
                                "id": "10000001",
                            },
                        )
                    ],
                }
            )
            with gzip.open(raw_path, "wt", encoding="utf-8") as handle:
                json.dump(payload, handle)
            master_path = root / "master.jsonl"
            summary = build_snapshot(
                raw_path=raw_path,
                master_path=master_path,
                source_candidates_path=root / "source_candidates.jsonl",
                dataset_version="Cofactor9.1-test",
                source_metadata={
                    "release": "test_release",
                    "query": "synthetic:true",
                    "artifact_sha256": hashlib.sha256(
                        raw_path.read_bytes()
                    ).hexdigest(),
                },
            )
            rows = [json.loads(line) for line in master_path.read_text().splitlines()]

        self.assertEqual(summary.canonical_block_count, 3)
        self.assertEqual(summary.canonical_block_label_overlap_count, 1)
        self.assertEqual(summary.canonical_block_label_overlap_accession_count, 1)
        direct = next(row for row in rows if row["entry"]["accession"] == "A00001")
        self.assertEqual(
            direct["derived"]["gold_formula"],
            [["CHEBI:1"], ["CHEBI:1"]],
        )
        self.assertEqual(
            direct["derived"]["reason_codes"],
            ["OVERLAPPING_BLOCK_LABEL"],
        )
        self.assertEqual(
            {
                label
                for block in direct["derived"]["gold_formula"]
                for label in block
            },
            set(direct["derived"]["experimental_label_ids"]),
        )


@unittest.skipUnless(
    FROZEN_UNIPROT.exists() and FROZEN_CHEBI.exists() and SOURCE_CONFIG.exists(),
    "frozen source artifacts are unavailable",
)
class SourceManifestTests(unittest.TestCase):
    def test_manifest_verifies_compressed_and_decompressed_artifacts(self) -> None:
        verification = verify_source_manifest(SOURCE_CONFIG)

        self.assertEqual(verification["uniprot_release"], "2026_02")
        self.assertEqual(verification["chebi_version"], "254")
        self.assertEqual(verification["verified_artifact_count"], 3)
        self.assertEqual(
            verification["artifact_sha256"]["uniprot_json"],
            "366b6d5924e8138f5e2e6af11bc9f638ef0b37e97123cb9f89d4a759c59612ea",
        )
        self.assertEqual(
            verification["decompressed_content_sha256"]["chebi_lite_json"],
            "d14526badabba6959c9d5facacb8a97d84b006347fae84cb5d7ca30a91ccd131",
        )


@unittest.skipUnless(FROZEN_UNIPROT.exists(), "frozen UniProt snapshot is unavailable")
class FrozenSnapshotBuildTests(unittest.TestCase):
    def test_frozen_snapshot_reproduces_foundation_invariants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            summary = build_snapshot(
                raw_path=FROZEN_UNIPROT,
                master_path=root / "master.jsonl",
                source_candidates_path=root / "source_candidates.jsonl",
                dataset_version="Cofactor9.1",
                source_metadata={
                    "release": "2026_02",
                    "query": (
                        "reviewed:true AND ec:* AND fragment:false AND "
                        "length:[50 TO 1100] AND cc_cofactor_chebi_exp:*"
                    ),
                    "artifact_sha256": (
                        "366b6d5924e8138f5e2e6af11bc9f638"
                        "ef0b37e97123cb9f89d4a759c59612ea"
                    ),
                },
            )
            union_mismatches = []
            overlap_accessions = []
            overlap_reason_accessions = []
            with (root / "master.jsonl").open(encoding="utf-8") as handle:
                for line in handle:
                    row = json.loads(line)
                    derived = row["derived"]
                    formula_union = {
                        label
                        for block in derived["gold_formula"]
                        for label in block
                    }
                    if formula_union != set(derived["experimental_label_ids"]):
                        union_mismatches.append(row["entry"]["accession"])
                    blocks = [set(block) for block in derived["gold_formula"]]
                    if any(
                        left & right
                        for index, left in enumerate(blocks)
                        for right in blocks[index + 1 :]
                    ):
                        overlap_accessions.append(row["entry"]["accession"])
                    if "OVERLAPPING_BLOCK_LABEL" in derived["reason_codes"]:
                        overlap_reason_accessions.append(row["entry"]["accession"])

        self.assertEqual(summary.source_record_count, 7008)
        self.assertEqual(summary.master_record_count, 5337)
        self.assertEqual(summary.unique_master_accession_count, 5337)
        self.assertEqual(summary.ineligible_record_count, 1671)
        self.assertEqual(summary.experimental_label_count, 104)
        self.assertEqual(
            summary.evidence_status_counts,
            {
                EvidenceStatus.ALL_DIRECT_PUBMED.value: 5210,
                EvidenceStatus.MIXED_DIRECT_AND_REFERENCE.value: 3,
                EvidenceStatus.ALL_REFERENCE_ONLY.value: 124,
            },
        )
        self.assertEqual(
            summary.experimental_label_cardinality_counts,
            {1: 4179, 2: 838, 3: 208, 4: 75, 5: 31, 6: 3, 7: 2, 8: 1},
        )
        self.assertEqual(
            summary.all_label_cardinality_counts,
            {1: 3971, 2: 974, 3: 252, 4: 89, 5: 44, 6: 4, 7: 2, 8: 1},
        )
        self.assertEqual(
            summary.formula_shape_counts,
            {
                FormulaShape.SINGLE.value: 4178,
                FormulaShape.PURE_OR.value: 647,
                FormulaShape.PURE_AND.value: 449,
                FormulaShape.MIXED_AND_OR.value: 63,
            },
        )
        self.assertEqual(summary.experimental_occurrence_count, 6981)
        self.assertEqual(summary.unique_accession_experimental_label_count, 6974)
        self.assertEqual(summary.duplicate_experimental_occurrence_count, 7)
        self.assertEqual(summary.canonical_block_count, 5911)
        self.assertEqual(summary.canonical_block_label_overlap_count, 8)
        self.assertEqual(summary.canonical_block_label_overlap_accession_count, 6)
        self.assertEqual(union_mismatches, [])
        self.assertEqual(
            overlap_accessions,
            ["P0ABJ9", "Q57580", "Q6AYK3", "Q8NFF5", "Q9LNJ9", "Q9SIY3"],
        )
        self.assertEqual(overlap_reason_accessions, overlap_accessions)
        self.assertEqual(summary.sequence_with_u_count, 11)
        self.assertEqual(summary.sequence_with_x_count, 8)
        self.assertEqual(summary.missing_sequence_count, 0)
        self.assertEqual(summary.sequence_length_mismatch_count, 0)
        self.assertEqual(summary.missing_experimental_chebi_id_count, 0)


if __name__ == "__main__":
    unittest.main()
