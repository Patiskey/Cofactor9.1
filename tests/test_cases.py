from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import unittest

from cofactor_bench.cases import (
    CASE_ARTIFACT_SCHEMA_VERSION,
    EXPECTED_CASE_COUNT,
    PRIVATE_MAPPING_MODE,
    PRIVATE_MAPPING_PURPOSE,
    PRIVATE_MAPPING_SCHEMA_VERSION,
    CaseArtifactError,
    build_case_artifacts,
    build_cases_from_config,
    case_artifact_paths,
    derive_case_artifacts,
    load_prompt_cases,
    validate_case_artifacts,
)


PROJECT_ROOT = Path(__file__).parents[1]
FULL_STRUCTURED = PROJECT_ROOT / "data" / "derived" / "full_structured.jsonl"
LABEL_CATALOG = PROJECT_ROOT / "data" / "derived" / "label_catalog.json"


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _jsonl_bytes(values: list[dict[str, object]]) -> bytes:
    return b"".join(_json_bytes(value) for value in values)


def _fixture_catalog() -> dict[str, object]:
    labels = [
        {
            "chebi_id": f"CHEBI:{index}",
            "name": f"frozen cofactor {index}",
            "uniprot_display_name": f"UniProt name {index}",
            "master_accession_count": 999 - index,
            "frequency_band": "head",
        }
        for index in range(1, 105)
    ]
    return {
        "schema_version": "cofactor9.1.label-catalog.v1",
        "dataset_version": "Cofactor9.1",
        "catalog_version": "cofactor9.1.allowed-labels.v1",
        "rule_version": "cofactor9.1.views.v2",
        "input_hashes": {"master_sha256": "0" * 64},
        "labels": labels,
        "summary": {
            "label_count": 104,
            "frequency_band_counts": {"head": 104, "mid": 0, "tail": 0},
        },
        "chebi_name_audit": {"target_label_count": 104},
        "uniprot_display_name_audit": {"target_label_count": 104},
    }


def _fixture_rows(count: int = 3) -> list[dict[str, object]]:
    all_labels = [f"CHEBI:{index}" for index in range(1, 105)]
    rows: list[dict[str, object]] = []
    for index in range(count):
        sequence = "M" + ("A" * (index + 1))
        rows.append(
            {
                "schema_version": "cofactor9.1.view-record.v1",
                "dataset_version": "Cofactor9.1",
                "derivation": {"rule_version": "cofactor9.1.views.v2"},
                "entry": {"accession": f"P{index + 1:05d}"},
                "sequence": {
                    "value": sequence,
                    "sha256": hashlib.sha256(sequence.encode("ascii")).hexdigest(),
                },
                "derived": {
                    "experimental_label_ids": all_labels if index == 0 else [
                        all_labels[index % len(all_labels)]
                    ],
                },
            }
        )
    return rows


def _fixture_inputs(count: int = 3) -> tuple[bytes, bytes]:
    return _jsonl_bytes(_fixture_rows(count)), _json_bytes(_fixture_catalog())


class FrozenCaseDerivationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.full_bytes = FULL_STRUCTURED.read_bytes()
        cls.catalog_bytes = LABEL_CATALOG.read_bytes()
        cls.artifacts = derive_case_artifacts(
            cls.full_bytes,
            cls.catalog_bytes,
        )

    def test_frozen_inputs_produce_exactly_5337_unique_opaque_mappings(self) -> None:
        artifacts = self.artifacts
        sample_ids = [case.sample_id for case in artifacts.cases]
        accessions = [mapping.accession for mapping in artifacts.private_mappings]

        self.assertEqual(EXPECTED_CASE_COUNT, 5337)
        self.assertEqual(len(artifacts.cases), EXPECTED_CASE_COUNT)
        self.assertEqual(len(artifacts.private_mappings), EXPECTED_CASE_COUNT)
        self.assertEqual(len(set(sample_ids)), EXPECTED_CASE_COUNT)
        self.assertEqual(len(set(accessions)), EXPECTED_CASE_COUNT)
        self.assertTrue(
            all(re.fullmatch(r"sample_[0-9a-f]{32}", value) for value in sample_ids)
        )
        self.assertTrue(
            all(
                mapping.accession.casefold() not in mapping.sample_id.casefold()
                for mapping in artifacts.private_mappings
            )
        )
        self.assertNotEqual(accessions, sorted(accessions))

        source_by_accession: dict[str, tuple[str, str]] = {}
        for line in self.full_bytes.splitlines():
            row = json.loads(line)
            source_by_accession[row["entry"]["accession"]] = (
                row["sequence"]["value"],
                row["sequence"]["sha256"],
            )
        case_by_id = {case.sample_id: case for case in artifacts.cases}
        for mapping in artifacts.private_mappings:
            source_sequence, source_sha256 = source_by_accession[mapping.accession]
            self.assertEqual(mapping.sequence_sha256, source_sha256)
            self.assertEqual(case_by_id[mapping.sample_id].sequence, source_sequence)

    def test_catalog_is_exact_gold_union_and_projection_is_leak_free(self) -> None:
        catalog_payload = json.loads(self.catalog_bytes)
        expected_terms = [
            {"chebi_id": item["chebi_id"], "name": item["name"]}
            for item in catalog_payload["labels"]
        ]
        expected_ids = {term["chebi_id"] for term in expected_terms}
        gold_union: set[str] = set()
        for line in self.full_bytes.splitlines():
            gold_union.update(
                json.loads(line)["derived"]["experimental_label_ids"]
            )

        self.assertEqual(len(expected_terms), 104)
        self.assertEqual(expected_ids, gold_union)
        self.assertEqual(
            set(self.artifacts.manifest["counts"]),
            {
                "catalog_terms",
                "prompt_cases",
                "private_mappings",
                "unique_accessions",
                "unique_sample_ids",
            },
        )
        for case in self.artifacts.cases:
            payload = case.to_payload()
            self.assertEqual(set(payload), {"sample_id", "sequence", "label_catalog"})
            self.assertEqual(set(payload["label_catalog"]), {"version", "terms"})
            self.assertEqual(payload["label_catalog"]["terms"], expected_terms)
            self.assertTrue(
                all(set(term) == {"chebi_id", "name"} for term in expected_terms)
            )


class CaseValidationTests(unittest.TestCase):
    def test_catalog_must_equal_the_full_experimental_gold_union(self) -> None:
        full_bytes, _ = _fixture_inputs()
        catalog = _fixture_catalog()
        catalog["labels"][-1] = {
            **catalog["labels"][-1],
            "chebi_id": "CHEBI:105",
        }

        with self.assertRaisesRegex(CaseArtifactError, "gold union"):
            derive_case_artifacts(
                full_bytes,
                _json_bytes(catalog),
                expected_case_count=3,
            )

    def test_sequence_hash_and_duplicate_accessions_are_rejected(self) -> None:
        rows = _fixture_rows()
        rows[0]["sequence"]["sha256"] = "f" * 64
        with self.assertRaisesRegex(CaseArtifactError, "sequence SHA256"):
            derive_case_artifacts(
                _jsonl_bytes(rows),
                _json_bytes(_fixture_catalog()),
                expected_case_count=3,
            )

        rows = _fixture_rows()
        rows[1]["entry"]["accession"] = rows[0]["entry"]["accession"]
        with self.assertRaisesRegex(CaseArtifactError, "Duplicate accession"):
            derive_case_artifacts(
                _jsonl_bytes(rows),
                _json_bytes(_fixture_catalog()),
                expected_case_count=3,
            )

    def test_catalog_and_full_records_must_use_the_same_view_rule_version(self) -> None:
        rows = _fixture_rows()
        rows[1]["derivation"]["rule_version"] = "cofactor9.1.views.v1"

        with self.assertRaisesRegex(CaseArtifactError, "rule_version"):
            derive_case_artifacts(
                _jsonl_bytes(rows),
                _json_bytes(_fixture_catalog()),
                expected_case_count=3,
            )

    def test_input_order_does_not_change_public_cases_or_private_mapping(self) -> None:
        rows = _fixture_rows()
        catalog_bytes = _json_bytes(_fixture_catalog())
        first = derive_case_artifacts(
            _jsonl_bytes(rows),
            catalog_bytes,
            expected_case_count=3,
        )
        second = derive_case_artifacts(
            _jsonl_bytes(list(reversed(rows))),
            catalog_bytes,
            expected_case_count=3,
        )

        self.assertEqual(first.cases_jsonl, second.cases_jsonl)
        self.assertEqual(first.private_mapping_jsonl, second.private_mapping_jsonl)
        self.assertNotEqual(
            first.manifest["input_sha256"]["full_structured"],
            second.manifest["input_sha256"]["full_structured"],
        )

    def test_unique_sequence_accession_rename_does_not_change_public_cases(self) -> None:
        rows = _fixture_rows()
        catalog_bytes = _json_bytes(_fixture_catalog())
        original = derive_case_artifacts(
            _jsonl_bytes(rows),
            catalog_bytes,
            expected_case_count=3,
        )
        rows[1]["entry"]["accession"] = "Q99999"
        renamed = derive_case_artifacts(
            _jsonl_bytes(rows),
            catalog_bytes,
            expected_case_count=3,
        )

        self.assertEqual(original.cases_jsonl, renamed.cases_jsonl)
        self.assertEqual(
            [case.sample_id for case in original.cases],
            [case.sample_id for case in renamed.cases],
        )
        self.assertNotEqual(
            original.private_mapping_jsonl,
            renamed.private_mapping_jsonl,
        )

    def test_exact_sequence_accessions_do_not_affect_public_cases(self) -> None:
        rows = _fixture_rows()
        rows[1]["sequence"] = dict(rows[0]["sequence"])
        catalog_bytes = _json_bytes(_fixture_catalog())
        original = derive_case_artifacts(
            _jsonl_bytes(rows),
            catalog_bytes,
            expected_case_count=3,
        )

        renamed_rows = json.loads(json.dumps(rows))
        renamed_rows[0]["entry"]["accession"] = "Q99998"
        renamed = derive_case_artifacts(
            _jsonl_bytes(renamed_rows),
            catalog_bytes,
            expected_case_count=3,
        )
        swapped_rows = json.loads(json.dumps(rows))
        swapped_rows[0]["entry"]["accession"], swapped_rows[1]["entry"][
            "accession"
        ] = (
            swapped_rows[1]["entry"]["accession"],
            swapped_rows[0]["entry"]["accession"],
        )
        swapped = derive_case_artifacts(
            _jsonl_bytes(swapped_rows),
            catalog_bytes,
            expected_case_count=3,
        )

        self.assertEqual(original.cases_jsonl, renamed.cases_jsonl)
        self.assertEqual(original.cases_jsonl, swapped.cases_jsonl)
        self.assertNotEqual(
            original.private_mapping_jsonl,
            renamed.private_mapping_jsonl,
        )

    def test_manifest_declares_sequence_only_sha256_id_derivation(self) -> None:
        full_bytes, catalog_bytes = _fixture_inputs()
        artifacts = derive_case_artifacts(
            full_bytes,
            catalog_bytes,
            expected_case_count=3,
        )

        self.assertEqual(
            artifacts.manifest["id_derivation"],
            {
                "algorithm": "SHA-256",
                "digest_hex_characters": 32,
                "domain": "cofactor9.1.sequence-case-id.v2",
                "inputs": [
                    "sequence_sha256",
                    "exact_sequence_duplicate_ordinal",
                ],
                "ordinal_base": 0,
                "version": "cofactor9.1.sequence-case-id.v2",
            },
        )
        derivation_json = json.dumps(
            artifacts.manifest["id_derivation"],
            sort_keys=True,
        ).casefold()
        self.assertNotIn("hmac", derivation_json)
        self.assertNotIn("accession", derivation_json)

    def test_strict_loader_rejects_a_leaked_accession_field(self) -> None:
        full_bytes, catalog_bytes = _fixture_inputs()
        artifacts = derive_case_artifacts(
            full_bytes,
            catalog_bytes,
            expected_case_count=3,
        )
        payload = artifacts.cases[0].to_payload()
        payload["accession"] = "P00001"

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.jsonl"
            path.write_bytes(_json_bytes(payload))
            with self.assertRaisesRegex(CaseArtifactError, "fields"):
                load_prompt_cases(path)


class CaseArtifactPublicationTests(unittest.TestCase):
    def _paths(self, directory: str) -> tuple[Path, Path, Path]:
        cases_path = Path(directory) / "cases.jsonl"
        paths = case_artifact_paths(cases_path)
        return paths.cases, paths.private_mapping, paths.manifest

    def _build(self, directory: str):
        full_bytes, catalog_bytes = _fixture_inputs()
        full_path = Path(directory) / "full.jsonl"
        catalog_path = Path(directory) / "catalog.json"
        full_path.write_bytes(full_bytes)
        catalog_path.write_bytes(catalog_bytes)
        cases_path, private_path, manifest_path = self._paths(directory)
        summary = build_case_artifacts(
            full_structured_path=full_path,
            label_catalog_path=catalog_path,
            cases_path=cases_path,
            private_mapping_path=private_path,
            manifest_path=manifest_path,
            expected_case_count=3,
        )
        return (
            full_path,
            catalog_path,
            cases_path,
            private_path,
            manifest_path,
            summary,
        )

    def test_build_is_byte_stable_idempotent_and_manifested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (
                full_path,
                catalog_path,
                cases_path,
                private_path,
                manifest_path,
                first,
            ) = self._build(directory)
            original_bytes = {
                path: path.read_bytes()
                for path in (cases_path, private_path, manifest_path)
            }
            for path in original_bytes:
                os.utime(path, ns=(1_000_000_000, 1_000_000_000))

            second = build_case_artifacts(
                full_structured_path=full_path,
                label_catalog_path=catalog_path,
                cases_path=cases_path,
                private_mapping_path=private_path,
                manifest_path=manifest_path,
                expected_case_count=3,
            )
            validated = validate_case_artifacts(
                full_structured_path=full_path,
                label_catalog_path=catalog_path,
                cases_path=cases_path,
                private_mapping_path=private_path,
                manifest_path=manifest_path,
                expected_case_count=3,
            )

            self.assertEqual(first, second)
            self.assertEqual(second, validated)
            self.assertEqual(
                original_bytes,
                {path: path.read_bytes() for path in original_bytes},
            )
            self.assertTrue(
                all(path.stat().st_mtime_ns == 1_000_000_000 for path in original_bytes)
            )
            manifest = json.loads(manifest_path.read_bytes())
            self.assertEqual(
                manifest["schema_version"], CASE_ARTIFACT_SCHEMA_VERSION
            )
            self.assertEqual(manifest["counts"]["prompt_cases"], 3)
            self.assertEqual(
                set(manifest["input_sha256"]),
                {"full_structured", "label_catalog"},
            )
            self.assertEqual(
                set(manifest["output_sha256"]),
                {"prompt_cases", "private_mapping"},
            )
            self.assertEqual(
                manifest["output_sha256"]["prompt_cases"],
                hashlib.sha256(cases_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                manifest["output_sha256"]["private_mapping"],
                hashlib.sha256(private_path.read_bytes()).hexdigest(),
            )

    def test_private_mapping_is_explicitly_private_and_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            *_, private_path, manifest_path, _ = self._build(directory)
            private_rows = [
                json.loads(line) for line in private_path.read_text().splitlines()
            ]
            manifest = json.loads(manifest_path.read_bytes())

            self.assertEqual(
                stat.S_IMODE(private_path.stat().st_mode), PRIVATE_MAPPING_MODE
            )
            self.assertEqual(
                manifest["private_mapping"],
                {
                    "file_mode": "0600",
                    "purpose": PRIVATE_MAPPING_PURPOSE,
                    "schema_version": PRIVATE_MAPPING_SCHEMA_VERSION,
                    "visibility": "private",
                },
            )
            self.assertTrue(
                all(
                    set(row)
                    == {
                        "accession",
                        "purpose",
                        "sample_id",
                        "schema_version",
                        "sequence_sha256",
                        "visibility",
                    }
                    for row in private_rows
                )
            )
            self.assertTrue(all(row["visibility"] == "private" for row in private_rows))
            self.assertTrue(
                all(row["purpose"] == PRIVATE_MAPPING_PURPOSE for row in private_rows)
            )

            private_path.chmod(0o644)
            with self.assertRaisesRegex(CaseArtifactError, "0600"):
                validate_case_artifacts(
                    full_structured_path=Path(directory) / "full.jsonl",
                    label_catalog_path=Path(directory) / "catalog.json",
                    cases_path=Path(directory) / "cases.jsonl",
                    private_mapping_path=private_path,
                    manifest_path=manifest_path,
                    expected_case_count=3,
                )

    def test_tampering_is_detected_and_build_never_overwrites_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (
                full_path,
                catalog_path,
                cases_path,
                private_path,
                manifest_path,
                _,
            ) = self._build(directory)
            lines = cases_path.read_text().splitlines()
            leaked = json.loads(lines[0])
            leaked["accession"] = "P00001"
            tampered = _json_bytes(leaked) + b"\n".join(
                line.encode("utf-8") for line in lines[1:]
            ) + b"\n"
            cases_path.write_bytes(tampered)

            with self.assertRaises(CaseArtifactError):
                validate_case_artifacts(
                    full_structured_path=full_path,
                    label_catalog_path=catalog_path,
                    cases_path=cases_path,
                    private_mapping_path=private_path,
                    manifest_path=manifest_path,
                    expected_case_count=3,
                )
            with self.assertRaisesRegex(CaseArtifactError, "refusing to overwrite"):
                build_case_artifacts(
                    full_structured_path=full_path,
                    label_catalog_path=catalog_path,
                    cases_path=cases_path,
                    private_mapping_path=private_path,
                    manifest_path=manifest_path,
                    expected_case_count=3,
                )
            self.assertEqual(cases_path.read_bytes(), tampered)

    def test_changed_input_metadata_invalidates_the_recorded_input_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (
                full_path,
                catalog_path,
                cases_path,
                private_path,
                manifest_path,
                _,
            ) = self._build(directory)
            catalog = json.loads(catalog_path.read_bytes())
            catalog["labels"][0]["frequency_band"] = "tail"
            catalog_path.write_bytes(_json_bytes(catalog))

            with self.assertRaisesRegex(CaseArtifactError, "manifest"):
                validate_case_artifacts(
                    full_structured_path=full_path,
                    label_catalog_path=catalog_path,
                    cases_path=cases_path,
                    private_mapping_path=private_path,
                    manifest_path=manifest_path,
                    expected_case_count=3,
                )


class ConfigCaseTests(unittest.TestCase):
    def test_configured_case_paths_must_be_relative_and_stay_in_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            config_path = root / "config" / "benchmark.json"
            base = {
                "schema_version": "cofactor9.1.config.v1",
                "dataset_version": "Cofactor9.1",
                "paths": {
                    "full_structured": "data/derived/full_structured.jsonl",
                    "label_catalog": "data/derived/label_catalog.json",
                    "cases": "data/derived/cases.jsonl",
                },
                "model": {
                    "prompt_version": "cofactor9.1.sequence-only.named-catalog.v2"
                },
            }

            for configured in ("../outside.jsonl", str(root / "absolute.jsonl")):
                with self.subTest(configured=configured):
                    payload = json.loads(json.dumps(base))
                    payload["paths"]["full_structured"] = configured
                    config_path.write_bytes(_json_bytes(payload))
                    with self.assertRaisesRegex(CaseArtifactError, "project"):
                        build_cases_from_config(config_path)


if __name__ == "__main__":
    unittest.main()
