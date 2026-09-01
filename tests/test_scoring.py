import unittest

from cofactor_bench.scoring import (
    CoreRecord,
    aggregate_record_scores,
    ancestor_distances_from_pairs,
    score_core_single,
    score_record,
)


A = "CHEBI:100"
B = "CHEBI:200"
C = "CHEBI:300"
D = "CHEBI:400"


class AncestryConversionTests(unittest.TestCase):
    def test_converts_ontology_pairs_to_deterministic_distance_map(self) -> None:
        pairs = [
            {"specific": C, "ancestor": A, "distance": 2},
            {"specific": B, "ancestor": A, "distance": 1},
            {"specific": C, "ancestor": B, "distance": 1},
        ]

        distances = ancestor_distances_from_pairs(reversed(pairs))

        self.assertEqual(distances, {B: {A: 1}, C: {A: 2, B: 1}})
        self.assertEqual(list(distances), [B, C])
        self.assertEqual(list(distances[C]), [A, B])

    def test_rejects_malformed_or_duplicate_ontology_pairs(self) -> None:
        invalid_pair_sets = (
            [{"specific": C, "ancestor": B}],
            [{"specific": C, "ancestor": B, "distance": 0}],
            [{"specific": C, "ancestor": C, "distance": 1}],
            [
                {"specific": C, "ancestor": B, "distance": 1},
                {"specific": C, "ancestor": B, "distance": 1},
            ],
        )
        for pairs in invalid_pair_sets:
            with self.subTest(pairs=pairs):
                with self.assertRaises(ValueError):
                    ancestor_distances_from_pairs(pairs)


class StructuredRecordScoringTests(unittest.TestCase):
    def test_one_prediction_satisfies_an_or_block_exactly(self) -> None:
        result = score_record([{A, B}], [A])

        self.assertEqual((result.tp, result.fp, result.fn), (1, 0, 0))
        self.assertEqual(
            (result.precision, result.recall, result.f1),
            (1.0, 1.0, 1.0),
        )
        self.assertTrue(result.exact)

    def test_predicting_both_or_alternatives_adds_one_false_positive(self) -> None:
        result = score_record([{A, B}], [A, B])

        self.assertEqual((result.tp, result.fp, result.fn), (1, 1, 0))
        self.assertEqual(result.precision, 0.5)
        self.assertEqual(result.recall, 1.0)
        self.assertFalse(result.exact)

    def test_separate_and_blocks_each_require_a_prediction(self) -> None:
        result = score_record([{A}, {B}], [A, B])

        self.assertEqual((result.tp, result.fp, result.fn), (2, 0, 0))
        self.assertTrue(result.exact)

    def test_matching_is_maximum_cardinality_not_first_hit_greedy(self) -> None:
        # A can satisfy either block but B can only satisfy the first block.
        result = score_record([{A, B}, {A}], [A, B])

        self.assertEqual((result.tp, result.fp, result.fn), (2, 0, 0))
        self.assertTrue(result.exact)

    def test_best_guess_is_scored_as_a_set_and_rejects_duplicates(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate"):
            score_record([{A}], [A, A])

    def test_status_does_not_change_primary_record_score(self) -> None:
        predicted = score_record([{A}], [A], status="predict")
        abstained = score_record([{A}], [A], status="abstain")

        self.assertEqual(
            (abstained.tp, abstained.fp, abstained.fn, abstained.exact),
            (predicted.tp, predicted.fp, predicted.fn, predicted.exact),
        )


class HierarchyAwareScoringTests(unittest.TestCase):
    def setUp(self) -> None:
        # Mapping direction is descendant -> {ancestor: shortest distance}.
        self.ancestry = {
            C: {B: 1, A: 2},
            B: {A: 1},
        }

    def test_exact_hierarchy_match_has_full_weight(self) -> None:
        result = score_record([{C}], [C], ancestor_distances=self.ancestry)

        self.assertEqual(result.hierarchy_tp, 1.0)
        self.assertEqual(result.hierarchy_exact, 1)
        self.assertEqual(result.under_specific, 0)
        self.assertEqual(result.over_specific, 0)

    def test_predicted_ancestor_is_recorded_as_under_specific(self) -> None:
        result = score_record([{C}], [B], ancestor_distances=self.ancestry)

        self.assertEqual(result.hierarchy_tp, 0.5)
        self.assertEqual(result.hierarchy_fp, 0.5)
        self.assertEqual(result.hierarchy_fn, 0.5)
        self.assertEqual(result.hierarchy_f1, 0.5)
        self.assertEqual(result.under_specific, 1)
        self.assertEqual(result.over_specific, 0)

    def test_predicted_descendant_is_recorded_as_over_specific(self) -> None:
        result = score_record([{A}], [C], ancestor_distances=self.ancestry)

        self.assertAlmostEqual(result.hierarchy_tp, 1.0 / 3.0)
        self.assertEqual(result.under_specific, 0)
        self.assertEqual(result.over_specific, 1)

    def test_unrelated_labels_receive_no_hierarchy_credit(self) -> None:
        result = score_record([{C}], [D], ancestor_distances=self.ancestry)

        self.assertEqual(result.hierarchy_tp, 0.0)
        self.assertEqual(result.hierarchy_fp, 1.0)
        self.assertEqual(result.hierarchy_fn, 1.0)

    def test_hierarchy_uses_maximum_weight_one_to_one_assignment(self) -> None:
        # B exactly satisfies the first block; A can then softly satisfy C.
        result = score_record([{B}, {C}], [A, B], ancestor_distances=self.ancestry)

        self.assertEqual(result.hierarchy_tp, 1.0 + (1.0 / 3.0))
        self.assertEqual(result.hierarchy_exact, 1)
        self.assertEqual(result.under_specific, 1)


class StructuredAggregateTests(unittest.TestCase):
    def test_micro_and_record_exact_metrics_use_all_records(self) -> None:
        scores = [
            score_record([{A, B}], [A], status="abstain"),
            score_record([{A}, {B}], [A, C], status="predict"),
        ]

        metrics = aggregate_record_scores(scores)

        self.assertEqual(metrics.record_count, 2)
        self.assertEqual(metrics.record_exact_count, 1)
        self.assertEqual(metrics.record_exact_accuracy, 0.5)
        self.assertEqual((metrics.tp, metrics.fp, metrics.fn), (2, 1, 1))
        self.assertAlmostEqual(metrics.micro_precision, 2.0 / 3.0)
        self.assertAlmostEqual(metrics.micro_recall, 2.0 / 3.0)
        self.assertAlmostEqual(metrics.micro_f1, 2.0 / 3.0)
        self.assertEqual(metrics.abstention_count, 1)
        self.assertEqual(metrics.coverage, 0.5)
        self.assertEqual(metrics.selective_record_exact_accuracy, 0.0)

    def test_hierarchy_micro_metrics_and_specificity_counts_aggregate(self) -> None:
        ancestry = {C: {B: 1, A: 2}, B: {A: 1}}
        scores = [
            score_record([{C}], [B], ancestor_distances=ancestry),
            score_record([{A}], [C], ancestor_distances=ancestry),
        ]

        metrics = aggregate_record_scores(scores)

        self.assertAlmostEqual(metrics.hierarchy_tp, 0.5 + (1.0 / 3.0))
        self.assertAlmostEqual(metrics.hierarchy_micro_precision, (0.5 + 1 / 3) / 2)
        self.assertAlmostEqual(metrics.hierarchy_micro_recall, (0.5 + 1 / 3) / 2)
        self.assertEqual(metrics.under_specific, 1)
        self.assertEqual(metrics.over_specific, 1)


class CoreSingleScoringTests(unittest.TestCase):
    def test_confusion_macro_f1_and_balanced_accuracy(self) -> None:
        records = [
            CoreRecord("s1", A, (A,), "predict"),
            CoreRecord("s2", A, (B,), "predict"),
            CoreRecord("s3", B, (B,), "predict"),
            CoreRecord("s4", C, (B,), "abstain"),
        ]

        metrics = score_core_single(records, labels=[C, B, A])

        self.assertEqual(metrics.labels, (A, B, C))
        self.assertEqual(
            metrics.confusion,
            {
                A: {A: 1, B: 1, C: 0},
                B: {A: 0, B: 1, C: 0},
                C: {A: 0, B: 1, C: 0},
            },
        )
        self.assertEqual(metrics.accuracy, 0.5)
        self.assertAlmostEqual(metrics.macro_f1, 7.0 / 18.0)
        self.assertEqual(metrics.balanced_accuracy, 0.5)
        self.assertEqual(metrics.abstention_count, 1)
        self.assertEqual(metrics.coverage, 0.75)
        self.assertAlmostEqual(metrics.selective_accuracy, 2.0 / 3.0)

    def test_core_primary_accuracy_ignores_abstention_status(self) -> None:
        metrics = score_core_single(
            [CoreRecord("s1", A, (A,), "abstain")],
            labels=[A],
        )

        self.assertEqual(metrics.accuracy, 1.0)
        self.assertEqual(metrics.coverage, 0.0)
        self.assertEqual(metrics.selective_accuracy, 0.0)

    def test_core_requires_exactly_one_best_guess(self) -> None:
        for best_guess in ((), (A, B)):
            with self.subTest(best_guess=best_guess):
                with self.assertRaisesRegex(ValueError, "exactly one"):
                    score_core_single(
                        [CoreRecord("s1", A, best_guess, "predict")],
                        labels=[A, B],
                    )


if __name__ == "__main__":
    unittest.main()
