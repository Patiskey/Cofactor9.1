import unittest


from cofactor_bench.duplicates import analyze_exact_sequence_groups


def record(accession, sequence_hash, formula):
    return {
        "entry": {"accession": accession},
        "sequence": {"sha256": sequence_hash},
        "derived": {"gold_formula": formula},
    }


class ExactSequenceGroupTests(unittest.TestCase):
    def test_consistent_duplicate_uses_lexicographic_representative(self):
        records = [
            record("P2", "same", [["CHEBI:1"]]),
            record("P1", "same", [["CHEBI:1"]]),
            record("P3", "unique", [["CHEBI:2"]]),
        ]

        result = analyze_exact_sequence_groups(records)

        self.assertEqual(result["summary"]["duplicate_groups"], 1)
        self.assertEqual(result["by_accession"]["P1"]["status"], "DUPLICATE_CONSISTENT")
        self.assertEqual(result["by_accession"]["P2"]["representative_accession"], "P1")
        self.assertTrue(result["by_accession"]["P1"]["is_representative"])
        self.assertFalse(result["by_accession"]["P2"]["is_representative"])

    def test_conflicting_duplicate_marks_the_entire_group(self):
        records = [
            record("A", "same", [["CHEBI:1"]]),
            record("B", "same", [["CHEBI:2"]]),
        ]

        result = analyze_exact_sequence_groups(records)

        self.assertEqual(result["summary"]["conflict_groups"], 1)
        self.assertEqual(result["summary"]["conflict_entries"], 2)
        self.assertEqual(result["by_accession"]["A"]["status"], "DUPLICATE_CONFLICT")
        self.assertIn(
            "EXACT_SEQUENCE_LABEL_CONFLICT",
            result["by_accession"]["B"]["reason_codes"],
        )

    def test_formula_order_does_not_create_a_false_conflict(self):
        records = [
            record("A", "same", [["CHEBI:2"], ["CHEBI:1", "CHEBI:3"]]),
            record("B", "same", [["CHEBI:3", "CHEBI:1"], ["CHEBI:2"]]),
        ]

        result = analyze_exact_sequence_groups(records)

        self.assertEqual(result["summary"]["conflict_groups"], 0)


if __name__ == "__main__":
    unittest.main()
