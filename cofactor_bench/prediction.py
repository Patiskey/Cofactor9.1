"""Strict parsing and semantic validation for saved model predictions."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
import json
import math
import re
from typing import Literal


SCHEMA_VERSION = "cofactor9.1.response.v2"
ABSTENTION_THRESHOLD = 0.5

_FIELDS = frozenset(
    {
        "schema_version",
        "sample_id",
        "status",
        "predicted_cofactors",
        "primary_guess",
        "confidence_complete",
    }
)
_STATUSES = frozenset({"predict", "abstain"})
_CHEBI_ID = re.compile(r"CHEBI:[1-9][0-9]*\Z")


class PredictionValidationError(ValueError):
    """Raised when a response is not exactly valid prediction JSON."""


@dataclass(frozen=True, slots=True)
class Prediction:
    """A validated response value safe for downstream scoring."""

    schema_version: str
    sample_id: str
    status: Literal["predict", "abstain"]
    predicted_cofactors: tuple[str, ...]
    primary_guess: str
    confidence_complete: float

    def to_dict(self) -> dict[str, object]:
        """Return the canonical JSON-safe representation."""

        return {
            "schema_version": self.schema_version,
            "sample_id": self.sample_id,
            "status": self.status,
            "predicted_cofactors": list(self.predicted_cofactors),
            "primary_guess": self.primary_guess,
            "confidence_complete": self.confidence_complete,
        }


def validate_prediction(
    payload: object,
    *,
    expected_sample_id: str,
    allowed_labels: Collection[str],
    max_labels: int | None = None,
) -> Prediction:
    """Validate a decoded response without coercion, normalization, or repair."""

    if not isinstance(payload, Mapping):
        raise PredictionValidationError("prediction must be a JSON object")

    actual_fields = frozenset(payload.keys())
    if actual_fields != _FIELDS:
        missing = sorted(_FIELDS - actual_fields)
        extra = sorted(str(field) for field in actual_fields - _FIELDS)
        raise PredictionValidationError(
            f"prediction fields do not match schema; missing={missing}, extra={extra}"
        )

    if payload["schema_version"] != SCHEMA_VERSION:
        raise PredictionValidationError(
            f"schema_version must be {SCHEMA_VERSION!r}"
        )

    sample_id = payload["sample_id"]
    if not isinstance(sample_id, str) or not sample_id:
        raise PredictionValidationError("sample_id must be a nonempty string")
    if sample_id != expected_sample_id:
        raise PredictionValidationError(
            f"sample_id {sample_id!r} does not match {expected_sample_id!r}"
        )

    status = payload["status"]
    if not isinstance(status, str) or status not in _STATUSES:
        raise PredictionValidationError("status must be 'predict' or 'abstain'")

    predicted_value = payload["predicted_cofactors"]
    if not isinstance(predicted_value, list) or not predicted_value:
        raise PredictionValidationError(
            "predicted_cofactors must be a nonempty JSON list"
        )
    if max_labels is not None:
        if isinstance(max_labels, bool) or not isinstance(max_labels, int) or max_labels < 1:
            raise ValueError("max_labels must be a positive integer or None")
        if len(predicted_value) > max_labels:
            raise PredictionValidationError(
                f"predicted_cofactors exceeds configured max_labels={max_labels}"
            )

    labels: list[str] = []
    seen: set[str] = set()
    allowed = frozenset(allowed_labels)
    for index, label in enumerate(predicted_value):
        if not isinstance(label, str) or _CHEBI_ID.fullmatch(label) is None:
            raise PredictionValidationError(
                f"predicted_cofactors[{index}] must be a canonical CHEBI identifier"
            )
        if label in seen:
            raise PredictionValidationError(
                f"predicted_cofactors contains duplicate label {label!r}"
            )
        if label not in allowed:
            raise PredictionValidationError(
                f"predicted_cofactors contains out-of-vocabulary label {label!r}"
            )
        seen.add(label)
        labels.append(label)

    primary_guess = payload["primary_guess"]
    if (
        not isinstance(primary_guess, str)
        or _CHEBI_ID.fullmatch(primary_guess) is None
        or primary_guess not in allowed
        or primary_guess not in seen
    ):
        raise PredictionValidationError(
            "primary_guess must be an allowed member of predicted_cofactors"
        )

    confidence = payload["confidence_complete"]
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0.0 <= confidence <= 1.0
    ):
        raise PredictionValidationError(
            "confidence_complete must be a finite number from 0 through 1"
        )
    expected_status = (
        "predict" if confidence >= ABSTENTION_THRESHOLD else "abstain"
    )
    if status != expected_status:
        raise PredictionValidationError(
            "status is inconsistent with the preregistered confidence threshold"
        )

    return Prediction(
        schema_version=SCHEMA_VERSION,
        sample_id=sample_id,
        status=status,
        predicted_cofactors=tuple(labels),
        primary_guess=primary_guess,
        confidence_complete=float(confidence),
    )


def parse_prediction_json(
    raw: str,
    *,
    expected_sample_id: str,
    allowed_labels: Collection[str],
    max_labels: int | None = None,
) -> Prediction:
    """Decode one complete JSON document and validate it exactly as received."""

    if not isinstance(raw, str):
        raise PredictionValidationError("raw prediction must be a JSON string")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise PredictionValidationError(
                    f"duplicate JSON object key {key!r}"
                )
            result[key] = value
        return result

    def reject_nonfinite_number(value: str) -> object:
        raise PredictionValidationError(
            f"non-finite JSON number {value!r} is not permitted"
        )

    try:
        payload = json.loads(
            raw,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_nonfinite_number,
        )
    except PredictionValidationError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise PredictionValidationError(f"malformed prediction JSON: {error}") from error

    return validate_prediction(
        payload,
        expected_sample_id=expected_sample_id,
        allowed_labels=allowed_labels,
        max_labels=max_labels,
    )


__all__ = [
    "ABSTENTION_THRESHOLD",
    "SCHEMA_VERSION",
    "Prediction",
    "PredictionValidationError",
    "parse_prediction_json",
    "validate_prediction",
]
