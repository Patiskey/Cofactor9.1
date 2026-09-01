"""Deterministic confidence and selective-risk metrics."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable


@dataclass(frozen=True, slots=True)
class CalibrationObservation:
    sample_id: str
    confidence: float
    correct: bool
    weight: float = 1.0


@dataclass(frozen=True, slots=True)
class CalibrationBin:
    lower: float
    upper: float
    observation_count: int
    weight: float
    mean_confidence: float
    accuracy: float
    absolute_gap: float


@dataclass(frozen=True, slots=True)
class RiskCoveragePoint:
    confidence_threshold: float
    coverage: float
    risk: float


@dataclass(frozen=True, slots=True)
class CalibrationMetrics:
    observation_count: int
    total_weight: float
    brier_score: float
    expected_calibration_error: float
    aurc: float
    bins: tuple[CalibrationBin, ...]
    risk_coverage_curve: tuple[RiskCoveragePoint, ...]


def _validate_observations(
    observations: Iterable[CalibrationObservation],
) -> tuple[CalibrationObservation, ...]:
    values = tuple(observations)
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, CalibrationObservation):
            raise TypeError("observations must be CalibrationObservation values")
        if not item.sample_id or item.sample_id in seen:
            raise ValueError("sample_id values must be nonempty and unique")
        seen.add(item.sample_id)
        if (
            isinstance(item.confidence, bool)
            or not isinstance(item.confidence, (int, float))
            or not math.isfinite(item.confidence)
            or not 0.0 <= item.confidence <= 1.0
        ):
            raise ValueError("confidence must be a finite number in [0, 1]")
        if not isinstance(item.correct, bool):
            raise ValueError("correct must be boolean")
        if (
            isinstance(item.weight, bool)
            or not isinstance(item.weight, (int, float))
            or not math.isfinite(item.weight)
            or item.weight < 0.0
        ):
            raise ValueError("weight must be a finite nonnegative number")
    return tuple(item for item in values if item.weight > 0.0)


def score_calibration(
    observations: Iterable[CalibrationObservation],
    *,
    bin_count: int = 10,
) -> CalibrationMetrics:
    """Score weighted calibration and a tie-invariant threshold AURC.

    Confidence ties enter the retained set together. AURC is the right-step
    integral of selective risk over the resulting threshold coverages.
    """

    if isinstance(bin_count, bool) or not isinstance(bin_count, int) or bin_count < 1:
        raise ValueError("bin_count must be a positive integer")
    values = _validate_observations(observations)
    total_weight = math.fsum(float(item.weight) for item in values)
    if total_weight == 0.0:
        return CalibrationMetrics(0, 0.0, 0.0, 0.0, 0.0, (), ())

    brier = math.fsum(
        float(item.weight) * (float(item.confidence) - float(item.correct)) ** 2
        for item in values
    ) / total_weight

    buckets: list[list[CalibrationObservation]] = [[] for _ in range(bin_count)]
    for item in values:
        index = min(int(float(item.confidence) * bin_count), bin_count - 1)
        buckets[index].append(item)
    bins: list[CalibrationBin] = []
    ece = 0.0
    for index, bucket in enumerate(buckets):
        if not bucket:
            continue
        weight = math.fsum(float(item.weight) for item in bucket)
        mean_confidence = math.fsum(
            float(item.weight) * float(item.confidence) for item in bucket
        ) / weight
        accuracy = math.fsum(
            float(item.weight) * float(item.correct) for item in bucket
        ) / weight
        gap = abs(mean_confidence - accuracy)
        ece += (weight / total_weight) * gap
        bins.append(
            CalibrationBin(
                lower=index / bin_count,
                upper=(index + 1) / bin_count,
                observation_count=len(bucket),
                weight=weight,
                mean_confidence=mean_confidence,
                accuracy=accuracy,
                absolute_gap=gap,
            )
        )

    by_confidence: dict[float, list[CalibrationObservation]] = {}
    for item in values:
        by_confidence.setdefault(float(item.confidence), []).append(item)
    retained_weight = 0.0
    retained_error_weight = 0.0
    prior_coverage = 0.0
    aurc = 0.0
    curve: list[RiskCoveragePoint] = []
    for threshold in sorted(by_confidence, reverse=True):
        group = by_confidence[threshold]
        retained_weight += math.fsum(float(item.weight) for item in group)
        retained_error_weight += math.fsum(
            float(item.weight) * float(not item.correct) for item in group
        )
        coverage = retained_weight / total_weight
        risk = retained_error_weight / retained_weight
        aurc += (coverage - prior_coverage) * risk
        prior_coverage = coverage
        curve.append(RiskCoveragePoint(threshold, coverage, risk))

    return CalibrationMetrics(
        observation_count=len(values),
        total_weight=total_weight,
        brier_score=brier,
        expected_calibration_error=ece,
        aurc=aurc,
        bins=tuple(bins),
        risk_coverage_curve=tuple(curve),
    )


__all__ = [
    "CalibrationBin",
    "CalibrationMetrics",
    "CalibrationObservation",
    "RiskCoveragePoint",
    "score_calibration",
]
