from __future__ import annotations

import unittest

from cofactor_bench.model import EvidenceStatus, FormulaShape
from cofactor_bench.parser import canonicalize_formula, parse_uniprot_entry


def synthetic_uniprot_entry() -> dict[str, object]:
    return {
        "entryType": "UniProtKB reviewed (Swiss-Prot)",
        "primaryAccession": "P00001",
        "uniProtkbId": "SYNTH_TEST",
        "entryAudit": {
            "entryVersion": 7,
            "sequenceVersion": 2,
            "lastAnnotationUpdateDate": "2026-01-02",
        },
        "organism": {"scientificName": "Synthetic organism", "taxonId": 999999},
        "proteinDescription": {
            "recommendedName": {
                "fullName": {"value": "Synthetic enzyme"},
                "ecNumbers": [{"value": "1.2.3.4"}],
            }
        },
        "references": [
            {
                "referenceNumber": 2,
                "citation": {
                    "id": "22222222",
                    "citationType": "journal article",
                    "title": "Synthetic direct experiment",
                    "citationCrossReferences": [
                        {"database": "PubMed", "id": "22222222"}
                    ],
                },
                "referencePositions": ["COFACTOR"],
            },
            {
                "referenceNumber": 7,
                "citation": {
                    "id": "CI-SYNTHETIC",
                    "citationType": "submission",
                    "title": "Synthetic submission",
                },
                "referencePositions": ["COFACTOR"],
            },
        ],
        "comments": [
            {
                "commentType": "COFACTOR",
                "cofactors": [
                    {
                        "name": "cofactor A",
                        "cofactorCrossReference": {
                            "database": "ChEBI",
                            "id": "CHEBI:100",
                        },
                        "evidences": [
                            {
                                "evidenceCode": "ECO:0000269",
                                "source": "PubMed",
                                "id": "11111111",
                            },
                            {
                                "evidenceCode": "ECO:0000255",
                                "source": "HAMAP-Rule",
                                "id": "MF_00001",
                            },
                        ],
                    },
                    {
                        "name": "cofactor A, second occurrence",
                        "cofactorCrossReference": {
                            "database": "ChEBI",
                            "id": "CHEBI:100",
                        },
                        "evidences": [
                            {
                                "evidenceCode": "ECO:0000269",
                                "source": "Reference",
                                "id": "Ref.2",
                            }
                        ],
                    },
                    {
                        "name": "cofactor B",
                        "cofactorCrossReference": {
                            "database": "ChEBI",
                            "id": "CHEBI:200",
                        },
                        "evidences": [
                            {
                                "evidenceCode": "ECO:0000269",
                                "source": "Reference",
                                "id": "Ref.2",
                            }
                        ],
                    },
                    {
                        "name": "unresolved reference",
                        "cofactorCrossReference": {
                            "database": "ChEBI",
                            "id": "CHEBI:999",
                        },
                        "evidences": [
                            {
                                "evidenceCode": "ECO:0000269",
                                "source": "Reference",
                                "id": "Ref.99",
                            }
                        ],
                    },
                ],
                "note": {
                    "texts": [
                        {
                            "value": "A note is audit context, never label evidence.",
                            "evidences": [
                                {
                                    "evidenceCode": "ECO:0000269",
                                    "source": "PubMed",
                                    "id": "33333333",
                                }
                            ],
                        }
                    ]
                },
            },
            {
                "commentType": "COFACTOR",
                "molecule": "Isoform 1",
                "cofactors": [
                    {
                        "name": "cofactor C",
                        "cofactorCrossReference": {
                            "database": "ChEBI",
                            "id": "CHEBI:300",
                        },
                        "evidences": [
                            {
                                "evidenceCode": "ECO:0000269",
                                "source": "Reference",
                                "id": "Ref.7",
                            }
                        ],
                    },
                    {
                        "name": "note-only candidate",
                        "cofactorCrossReference": {
                            "database": "ChEBI",
                            "id": "CHEBI:400",
                        },
                        "evidences": [
                            {
                                "evidenceCode": "ECO:0000250",
                                "source": "UniProtKB",
                                "id": "P99999",
                            }
                        ],
                    },
                ],
                "note": {
                    "texts": [
                        {
                            "value": "The note itself has experimental evidence.",
                            "evidences": [
                                {
                                    "evidenceCode": "ECO:0000269",
                                    "source": "PubMed",
                                    "id": "44444444",
                                }
                            ],
                        }
                    ]
                },
            },
        ],
        "sequence": {
            "value": "ACDUX",
            "length": 5,
            "crc64": "SYNTHETIC",
        },
    }


class UniProtParserTests(unittest.TestCase):
    def test_duplicate_or_citationless_reference_numbers_do_not_resolve(self) -> None:
        for variant in ("duplicate", "citationless"):
            with self.subTest(variant=variant):
                entry = synthetic_uniprot_entry()
                references = entry["references"]
                if variant == "duplicate":
                    references.append(
                        {
                            "referenceNumber": 2,
                            "citation": {
                                "id": "CONFLICTING-CITATION",
                                "citationType": "submission",
                            },
                        }
                    )
                else:
                    references[0] = {"referenceNumber": 2}

                parsed = parse_uniprot_entry(entry, raw_record_index=12)
                occurrence = parsed.cofactor_blocks[0].label_occurrences[1]
                evidence = occurrence.evidences[0]

                self.assertFalse(evidence.accepted_for_experimental)
                self.assertEqual(evidence.resolution_status, "UNRESOLVED_REFERENCE")
                self.assertIsNone(evidence.resolved_reference)

    def test_preserves_blocks_occurrences_evidence_notes_and_scope(self) -> None:
        parsed = parse_uniprot_entry(synthetic_uniprot_entry(), raw_record_index=12)

        self.assertEqual(parsed.accession, "P00001")
        self.assertEqual(len(parsed.cofactor_blocks), 2)
        self.assertEqual(parsed.cofactor_blocks[0].source_ordinal, 1)
        self.assertIsNone(parsed.cofactor_blocks[0].molecule)
        self.assertEqual(parsed.cofactor_blocks[1].molecule, "Isoform 1")
        self.assertEqual(len(parsed.cofactor_blocks[0].label_occurrences), 4)
        self.assertEqual(len(parsed.cofactor_blocks[0].notes), 1)

        first = parsed.cofactor_blocks[0].label_occurrences[0]
        duplicate = parsed.cofactor_blocks[0].label_occurrences[1]
        self.assertEqual(first.chebi_id, duplicate.chebi_id)
        self.assertNotEqual(first.occurrence_id, duplicate.occurrence_id)
        self.assertEqual(len(first.evidences), 2)
        self.assertEqual(first.evidences[0].source_id, "11111111")
        self.assertEqual(first.evidences[1].source_id, "MF_00001")

    def test_filters_evidence_at_label_occurrence_and_resolves_ref_number(self) -> None:
        parsed = parse_uniprot_entry(synthetic_uniprot_entry(), raw_record_index=12)
        occurrences = [
            occurrence
            for block in parsed.cofactor_blocks
            for occurrence in block.label_occurrences
        ]

        self.assertEqual([item.experimental for item in occurrences], [True, True, True, False, True, False])
        resolved = occurrences[1].evidences[0]
        self.assertTrue(resolved.accepted_for_experimental)
        self.assertEqual(resolved.reference_number, 2)
        self.assertEqual(resolved.resolved_reference["citation"]["id"], "22222222")

        unresolved = occurrences[3].evidences[0]
        self.assertFalse(unresolved.accepted_for_experimental)
        self.assertEqual(unresolved.resolution_status, "UNRESOLVED_REFERENCE")
        self.assertIsNone(unresolved.resolved_reference)

        # ECO:0000269 on either note must not promote CHEBI:400.
        self.assertNotIn("CHEBI:400", parsed.experimental_label_ids)
        self.assertIn("CHEBI:400", parsed.all_cofactor_label_ids)

    def test_builds_and_or_formula_without_discarding_duplicate_occurrences(self) -> None:
        parsed = parse_uniprot_entry(synthetic_uniprot_entry(), raw_record_index=12)

        self.assertEqual(parsed.experimental_occurrence_count, 4)
        self.assertEqual(
            parsed.experimental_label_ids,
            ("CHEBI:100", "CHEBI:200", "CHEBI:300"),
        )
        self.assertEqual(
            parsed.gold_formula,
            (("CHEBI:100", "CHEBI:200"), ("CHEBI:300",)),
        )
        self.assertEqual(parsed.formula_shape, FormulaShape.MIXED_AND_OR)
        self.assertEqual(parsed.evidence_status, EvidenceStatus.MIXED_DIRECT_AND_REFERENCE)

    def test_canonical_formula_deduplicates_and_applies_cnf_absorption(self) -> None:
        formula = canonicalize_formula(
            [
                ["CHEBI:10", "CHEBI:2", "CHEBI:2"],
                ["CHEBI:2", "CHEBI:10"],
                ["CHEBI:2"],
                ["CHEBI:3"],
            ]
        )

        self.assertEqual(formula, (("CHEBI:2",), ("CHEBI:3",)))


if __name__ == "__main__":
    unittest.main()
