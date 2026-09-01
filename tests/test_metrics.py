import unittest

from cofactor_bench.metrics import (
    CalibrationObservation,
    score_calibration,
)


class CalibrationMetricTests(unittest.TestCase):
    def test_brier_ece_and_threshold_aurc_match_hand_calculation(self) -> None:
        observations = [
            CalibrationObservation("a", confidence=0.9, correct=True),
            CalibrationObservation("b", confidence=0.8, correct=False),
            CalibrationObservation("c", confidence=0.2, correct=True),
            CalibrationObservation("d", confidence=0.1, correct=False),
        ]

        metrics = score_calibration(observations, bin_count=2)

        self.assertAlmostEqual(metrics.brier_score, 0.325)
        self.assertAlmostEqual(metrics.expected_calibration_error, 0.35)
        # Risks after each distinct threshold are 0, 1/2, 1/3, 1/2.
        self.assertAlmostEqual(
            metrics.aurc,
            (0.0 + 0.5 + (1.0 / 3.0) + 0.5) / 4.0,
        )
        self.assertEqual(
            [(item.coverage, item.risk) for item in metrics.risk_coverage_curve],
            [(0.25, 0.0), (0.5, 0.5), (0.75, 1 / 3), (1.0, 0.5)],
        )

    def test_tied_confidences_are_grouped_and_order_invariant(self) -> None:
        first = [
            CalibrationObservation("z", confidence=0.8, correct=False),
            CalibrationObservation("a", confidence=0.8, correct=True),
            CalibrationObservation("m", confidence=0.2, correct=True),
        ]

        left = score_calibration(first, bin_count=5)
        right = score_calibration(reversed(first), bin_count=5)

        self.assertEqual(left, right)
        self.assertEqual(len(left.risk_coverage_curve), 2)
        self.assertAlmostEqual(left.aurc, (2 / 3) * 0.5 + (1 / 3) * (1 / 3))

    def test_zero_weight_observations_do_not_change_primary_metrics(self) -> None:
        base = [CalibrationObservation("a", confidence=0.9, correct=True)]
        with_conflict = base + [
            CalibrationObservation(
                "conflict", confidence=0.0, correct=False, weight=0.0
            )
        ]

        self.assertEqual(
            score_calibration(base),
            score_calibration(with_conflict),
        )

    def test_rejects_duplicate_ids_invalid_confidence_weights_and_bins(self) -> None:
        invalid_sets = (
            [
                CalibrationObservation("a", 0.5, True),
                CalibrationObservation("a", 0.4, False),
            ],
            [CalibrationObservation("a", -0.1, True)],
            [CalibrationObservation("a", 1.1, True)],
            [CalibrationObservation("a", 0.5, True, weight=-1.0)],
        )
        for observations in invalid_sets:
            with self.subTest(observations=observations):
                with self.assertRaises(ValueError):
                    score_calibration(observations)

        with self.assertRaises(ValueError):
            score_calibration(
                [CalibrationObservation("a", 0.5, True)],
                bin_count=0,
            )


if __name__ == "__main__":
    unittest.main()
