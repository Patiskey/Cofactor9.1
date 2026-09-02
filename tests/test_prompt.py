import json
from pathlib import Path
import re
import unittest

from cofactor_bench.prompt import (
    CATALOG_SIZE,
    PROMPT_VERSION,
    CatalogTerm,
    PromptCase,
    PromptValidationError,
    create_prompt_case,
    render_prompt,
    validate_prompt_payload,
)


def label_catalog() -> tuple[CatalogTerm, ...]:
    return tuple(
        CatalogTerm(f"CHEBI:{index}", f"frozen cofactor {index}")
        for index in range(1, 105)
    )


class PromptCaseTests(unittest.TestCase):
    def test_create_prompt_case_uses_a_random_opaque_sample_id(self) -> None:
        first = create_prompt_case(
            sequence="MSEQUENCEU",
            catalog_terms=label_catalog(),
            catalog_version="uniprot-2026_02-chebi-v1",
        )
        second = create_prompt_case(
            sequence="MSEQUENCEU",
            catalog_terms=label_catalog(),
            catalog_version="uniprot-2026_02-chebi-v1",
        )

        self.assertRegex(first.sample_id, r"\Asample_[0-9a-f]{32}\Z")
        self.assertNotEqual(first.sample_id, second.sample_id)
        self.assertEqual(CATALOG_SIZE, 104)

    def test_payload_is_a_closed_sequence_only_contract(self) -> None:
        case = create_prompt_case(
            sequence="MSEQUENCEUX",
            catalog_terms=label_catalog(),
            catalog_version="uniprot-2026_02-chebi-v1",
        )

        payload = case.to_payload()

        self.assertEqual(set(payload), {"sample_id", "sequence", "label_catalog"})
        self.assertEqual(
            set(payload["label_catalog"]),
            {"version", "terms"},
        )
        self.assertEqual(len(payload["label_catalog"]["terms"]), 104)
        self.assertEqual(
            payload["label_catalog"]["terms"][0],
            {"chebi_id": "CHEBI:1", "name": "frozen cofactor 1"},
        )
        self.assertNotIn("accession", json.dumps(payload).lower())
        self.assertNotIn("organism", json.dumps(payload).lower())
        self.assertNotIn("pmid", json.dumps(payload).lower())
        self.assertNotIn("gold", json.dumps(payload).lower())

    def test_validate_prompt_payload_rejects_metadata_and_gold_fields(self) -> None:
        case = create_prompt_case(
            sequence="MSEQUENCE",
            catalog_terms=label_catalog(),
            catalog_version="uniprot-2026_02-chebi-v1",
        )
        base = case.to_payload()
        forbidden_fields = (
            "accession",
            "organism",
            "ec",
            "note",
            "evidence",
            "pmid",
            "gold_label",
        )

        for field in forbidden_fields:
            with self.subTest(field=field):
                payload = dict(base)
                payload[field] = "forbidden-sentinel"
                with self.assertRaisesRegex(PromptValidationError, "fields"):
                    validate_prompt_payload(payload)

    def test_catalog_and_sequence_are_validated_without_silent_repair(self) -> None:
        labels = label_catalog()
        invalid_inputs = (
            {"sequence": "M SEQUENCE", "catalog_terms": labels},
            {"sequence": "msequence", "catalog_terms": labels},
            {"sequence": "MSEQUENCE", "catalog_terms": labels[:-1]},
            {
                "sequence": "MSEQUENCE",
                "catalog_terms": labels[:-1] + (labels[-2],),
            },
            {
                "sequence": "MSEQUENCE",
                "catalog_terms": labels[:-1] + ("CHEBI:0001",),
            },
            {
                "sequence": "MSEQUENCE",
                "catalog_terms": tuple(reversed(labels)),
            },
        )

        for values in invalid_inputs:
            with self.subTest(values=values):
                with self.assertRaises(PromptValidationError):
                    create_prompt_case(
                        catalog_version="uniprot-2026_02-chebi-v1",
                        **values,
                    )

    def test_render_prompt_contains_one_canonical_case_payload(self) -> None:
        case = create_prompt_case(
            sequence="MSEQUENCEUX",
            catalog_terms=label_catalog(),
            catalog_version="uniprot-2026_02-chebi-v1",
        )

        prompt = render_prompt(case)
        match = re.search(
            r"BEGIN_CASE_JSON\n(?P<payload>[^\n]+)\nEND_CASE_JSON",
            prompt,
        )

        self.assertIsNotNone(match)
        decoded = json.loads(match.group("payload"))
        self.assertEqual(decoded, case.to_payload())
        self.assertEqual(prompt.count(case.sequence), 1)
        self.assertIn(PROMPT_VERSION, prompt)
        self.assertIn("not a ranked top-k list", prompt)
        self.assertIn("simultaneously assert", prompt)
        self.assertIn("record-exact", prompt)
        self.assertIn("UniProt-style COFACTOR", prompt)

    def test_prompt_case_round_trips_the_exact_closed_payload(self) -> None:
        case = create_prompt_case(
            sequence="MSEQUENCEU",
            catalog_terms=label_catalog(),
            catalog_version="uniprot-2026_02-chebi-v1",
        )

        restored = PromptCase.from_payload(
            json.loads(json.dumps(case.to_payload()))
        )

        self.assertEqual(restored, case)


class ModelResponseSchemaTests(unittest.TestCase):
    def test_response_schema_is_strict_and_matches_prediction_contract(self) -> None:
        schema_path = (
            Path(__file__).parents[1] / "schemas" / "model-response.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["required"]),
            {
                "schema_version",
                "sample_id",
                "status",
                "predicted_cofactors",
                "primary_guess",
                "confidence_complete",
            },
        )
        self.assertEqual(
            schema["properties"]["schema_version"]["const"],
            "cofactor9.1.response.v2",
        )
        self.assertEqual(
            schema["properties"]["schema_version"]["type"],
            "string",
        )
        self.assertEqual(
            schema["properties"]["status"]["enum"],
            ["predict", "abstain"],
        )
        self.assertEqual(
            schema["properties"]["predicted_cofactors"]["minItems"],
            1,
        )
        self.assertNotIn(
            "uniqueItems",
            schema["properties"]["predicted_cofactors"],
        )


if __name__ == "__main__":
    unittest.main()
