import json
import unittest

from cofactor_bench.prediction import (
    ABSTENTION_THRESHOLD,
    SCHEMA_VERSION,
    Prediction,
    PredictionValidationError,
    parse_prediction_json,
    validate_prediction,
)


ALLOWED_LABELS = {
    "CHEBI:18420",
    "CHEBI:29105",
    "CHEBI:57692",
    "CHEBI:597326",
    "CHEBI:999999",
}


def valid_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "sample_id": "sample_000001",
        "status": "predict",
        "predicted_cofactors": ["CHEBI:18420"],
        "primary_guess": "CHEBI:18420",
        "confidence_complete": 0.75,
    }
    payload.update(changes)
    return payload


class PredictionValidationTests(unittest.TestCase):
    def test_validate_prediction_returns_frozen_typed_value(self) -> None:
        prediction = validate_prediction(
            valid_payload(),
            expected_sample_id="sample_000001",
            allowed_labels=ALLOWED_LABELS,
        )

        self.assertEqual(
            prediction,
            Prediction(
                schema_version=SCHEMA_VERSION,
                sample_id="sample_000001",
                status="predict",
                predicted_cofactors=("CHEBI:18420",),
                primary_guess="CHEBI:18420",
                confidence_complete=0.75,
            ),
        )
        with self.assertRaises((AttributeError, TypeError)):
            prediction.status = "abstain"  # type: ignore[misc]

    def test_abstain_still_requires_and_preserves_joint_prediction(self) -> None:
        prediction = validate_prediction(
            valid_payload(
                status="abstain",
                predicted_cofactors=["CHEBI:57692", "CHEBI:29105"],
                primary_guess="CHEBI:57692",
                confidence_complete=0.25,
            ),
            expected_sample_id="sample_000001",
            allowed_labels=ALLOWED_LABELS,
        )

        self.assertEqual(prediction.status, "abstain")
        self.assertEqual(
            prediction.predicted_cofactors,
            ("CHEBI:57692", "CHEBI:29105"),
        )

    def test_schema_version_is_fixed(self) -> None:
        with self.assertRaisesRegex(
            PredictionValidationError, "schema_version"
        ):
            validate_prediction(
                valid_payload(schema_version="cofactor9.1.response.v1"),
                expected_sample_id="sample_000001",
                allowed_labels=ALLOWED_LABELS,
            )

    def test_sample_id_must_match_requested_sample(self) -> None:
        with self.assertRaisesRegex(PredictionValidationError, "sample_id"):
            validate_prediction(
                valid_payload(sample_id="sample_999999"),
                expected_sample_id="sample_000001",
                allowed_labels=ALLOWED_LABELS,
            )

    def test_only_predict_and_abstain_statuses_are_valid(self) -> None:
        with self.assertRaisesRegex(PredictionValidationError, "status"):
            validate_prediction(
                valid_payload(status="error"),
                expected_sample_id="sample_000001",
                allowed_labels=ALLOWED_LABELS,
            )

    def test_predicted_cofactors_must_be_a_nonempty_json_list(self) -> None:
        for predicted_cofactors in ([], "CHEBI:18420", None):
            with self.subTest(predicted_cofactors=predicted_cofactors):
                with self.assertRaisesRegex(
                    PredictionValidationError, "predicted_cofactors"
                ):
                    validate_prediction(
                        valid_payload(predicted_cofactors=predicted_cofactors),
                        expected_sample_id="sample_000001",
                        allowed_labels=ALLOWED_LABELS,
                    )

    def test_predicted_cofactors_reject_duplicates_malformed_and_oov(self) -> None:
        invalid_guesses = (
            ["CHEBI:18420", "CHEBI:18420"],
            ["CHEBI:abc"],
            ["CHEBI:00001"],
            [18420],
            ["CHEBI:123456"],
        )
        for predicted_cofactors in invalid_guesses:
            with self.subTest(predicted_cofactors=predicted_cofactors):
                with self.assertRaisesRegex(
                    PredictionValidationError, "predicted_cofactors"
                ):
                    validate_prediction(
                        valid_payload(predicted_cofactors=predicted_cofactors),
                        expected_sample_id="sample_000001",
                        allowed_labels=ALLOWED_LABELS,
                    )

    def test_predicted_cofactor_limit_is_supplied_by_the_caller(self) -> None:
        five_labels = sorted(ALLOWED_LABELS)
        prediction = validate_prediction(
            valid_payload(
                predicted_cofactors=five_labels,
                primary_guess=five_labels[0],
            ),
            expected_sample_id="sample_000001",
            allowed_labels=ALLOWED_LABELS,
            max_labels=5,
        )
        self.assertEqual(len(prediction.predicted_cofactors), 5)

        with self.assertRaisesRegex(PredictionValidationError, "max_labels"):
            validate_prediction(
                valid_payload(
                    predicted_cofactors=five_labels,
                    primary_guess=five_labels[0],
                ),
                expected_sample_id="sample_000001",
                allowed_labels=ALLOWED_LABELS,
                max_labels=4,
            )

    def test_confidence_complete_must_be_a_finite_number_in_unit_interval(self) -> None:
        invalid_values = (-0.01, 1.01, True, "0.5", float("nan"), float("inf"))
        for confidence in invalid_values:
            with self.subTest(confidence=confidence):
                with self.assertRaisesRegex(
                    PredictionValidationError, "confidence_complete"
                ):
                    validate_prediction(
                        valid_payload(confidence_complete=confidence),
                        expected_sample_id="sample_000001",
                        allowed_labels=ALLOWED_LABELS,
                    )

        for confidence in (0, 1, 0.5):
            with self.subTest(valid_confidence=confidence):
                prediction = validate_prediction(
                    valid_payload(
                        confidence_complete=confidence,
                        status="predict" if confidence >= 0.5 else "abstain",
                    ),
                    expected_sample_id="sample_000001",
                    allowed_labels=ALLOWED_LABELS,
                )
                self.assertEqual(prediction.confidence_complete, float(confidence))

    def test_primary_guess_must_be_an_allowed_member_of_joint_set(self) -> None:
        invalid = (
            valid_payload(primary_guess="CHEBI:57692"),
            valid_payload(primary_guess="CHEBI:123456"),
            valid_payload(primary_guess=18420),
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(PredictionValidationError, "primary_guess"):
                    validate_prediction(
                        payload,
                        expected_sample_id="sample_000001",
                        allowed_labels=ALLOWED_LABELS,
                    )

    def test_status_is_tied_to_preregistered_exact_set_threshold(self) -> None:
        self.assertEqual(ABSTENTION_THRESHOLD, 0.5)
        inconsistent = (
            valid_payload(status="abstain", confidence_complete=0.5),
            valid_payload(status="predict", confidence_complete=0.49),
        )
        for payload in inconsistent:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(PredictionValidationError, "status"):
                    validate_prediction(
                        payload,
                        expected_sample_id="sample_000001",
                        allowed_labels=ALLOWED_LABELS,
                    )

    def test_missing_and_extra_fields_are_rejected(self) -> None:
        missing = valid_payload()
        del missing["confidence_complete"]
        extra = valid_payload(rationale="not part of the persisted schema")

        for payload in (missing, extra):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(PredictionValidationError, "fields"):
                    validate_prediction(
                        payload,
                        expected_sample_id="sample_000001",
                        allowed_labels=ALLOWED_LABELS,
                    )

    def test_non_object_payload_is_rejected(self) -> None:
        with self.assertRaisesRegex(PredictionValidationError, "object"):
            validate_prediction(
                [valid_payload()],
                expected_sample_id="sample_000001",
                allowed_labels=ALLOWED_LABELS,
            )


class PredictionJsonParsingTests(unittest.TestCase):
    def test_parse_prediction_json_validates_without_repair(self) -> None:
        raw = json.dumps(valid_payload(), sort_keys=True)

        prediction = parse_prediction_json(
            raw,
            expected_sample_id="sample_000001",
            allowed_labels=ALLOWED_LABELS,
        )

        self.assertEqual(prediction.predicted_cofactors, ("CHEBI:18420",))

    def test_malformed_or_wrapped_json_fails_instead_of_being_repaired(self) -> None:
        raw_valid = json.dumps(valid_payload())
        malformed_responses = (
            raw_valid[:-1],
            f"```json\n{raw_valid}\n```",
            f"Here is the answer: {raw_valid}",
            f"{raw_valid}\ntrailing prose",
            raw_valid.replace("0.75", "NaN"),
        )
        for raw in malformed_responses:
            with self.subTest(raw=raw):
                with self.assertRaises(PredictionValidationError):
                    parse_prediction_json(
                        raw,
                        expected_sample_id="sample_000001",
                        allowed_labels=ALLOWED_LABELS,
                    )

    def test_duplicate_json_object_keys_are_rejected(self) -> None:
        raw = (
            '{"schema_version":"cofactor9.1.response.v2",'
            '"sample_id":"sample_000001",'
            '"sample_id":"sample_000002",'
            '"status":"predict",'
            '"predicted_cofactors":["CHEBI:18420"],'
            '"primary_guess":"CHEBI:18420",'
            '"confidence_complete":0.75}'
        )

        with self.assertRaisesRegex(PredictionValidationError, "duplicate"):
            parse_prediction_json(
                raw,
                expected_sample_id="sample_000001",
                allowed_labels=ALLOWED_LABELS,
            )

if __name__ == "__main__":
    unittest.main()
