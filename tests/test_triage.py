import unittest


from cofactor_bench.triage import classify_alphabet, triage_record


class AlphabetClassificationTests(unittest.TestCase):
    def test_selenocysteine_is_valid_but_informationally_flagged(self):
        result = classify_alphabet("ACUG")

        self.assertEqual(result["status"], "HAS_U")
        self.assertEqual(result["nonstandard_symbols"], ["U"])
        self.assertEqual(result["reason_codes"], ["SELENOCYSTEINE_U"])

    def test_unknown_residue_is_exclusion_relevant(self):
        result = classify_alphabet("ACXG")

        self.assertEqual(result["status"], "HAS_X")
        self.assertEqual(result["reason_codes"], ["UNKNOWN_RESIDUE_X"])

    def test_other_noncanonical_symbol_is_invalid(self):
        result = classify_alphabet("ACBG")

        self.assertEqual(result["status"], "INVALID")
        self.assertEqual(result["nonstandard_symbols"], ["B"])


class ConservativeTriageTests(unittest.TestCase):
    def test_plain_matching_binding_note_does_not_require_review(self):
        result = triage_record(
            label_id="CHEBI:18420",
            note="Binds 1 Mg(2+) ion per subunit.",
        )

        self.assertEqual(result["adjudication_status"], "NOT_REQUIRED")
        self.assertEqual(result["reason_codes"], [])

    def test_weak_alternative_is_pending_not_auto_excluded(self):
        result = triage_record(
            label_id="CHEBI:18420",
            note="Can also use Mn(2+) with lower efficiency.",
        )

        self.assertEqual(result["adjudication_status"], "PENDING")
        self.assertIn("NOTE_OTHER_COFACTOR_MENTION", result["reason_codes"])
        self.assertIn("NOTE_ALTERNATIVE_OR_COMPARISON", result["reason_codes"])
        self.assertIn("NOTE_PREFERENCE_OR_PARTIAL_ACTIVITY", result["reason_codes"])

    def test_negated_alternative_still_enters_manual_queue(self):
        result = triage_record(
            label_id="CHEBI:29105",
            note="Cannot use Mn(2+) as a cofactor.",
        )

        self.assertEqual(result["adjudication_status"], "PENDING")
        self.assertIn("NOTE_NEGATION_OR_INHIBITION", result["reason_codes"])

    def test_ancestor_and_molecule_scope_are_flagged_even_without_note(self):
        result = triage_record(
            label_id="CHEBI:24875",
            note="",
            molecule="Isoform 2",
            is_ancestor_target=True,
        )

        self.assertEqual(result["adjudication_status"], "PENDING")
        self.assertIn("ONTOLOGY_ANCESTOR_TARGET", result["reason_codes"])
        self.assertIn("MOLECULE_SCOPE", result["reason_codes"])


if __name__ == "__main__":
    unittest.main()
