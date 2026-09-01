"""Pure, deterministic scoring for canonical cofactor CNF targets."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import math
from typing import Literal


Status = Literal["predict", "abstain"]
Relation = Literal["exact", "under_specific", "over_specific"]
AncestorDistances = Mapping[str, Mapping[str, int]]


@dataclass(frozen=True, slots=True)
class RecordScore:
    """Exact and hierarchy-aware scores for one structured record."""

    tp: int
    fp: int
    fn: int
    precision: float
    recall: float
    f1: float
    exact: bool
    hierarchy_tp: float
    hierarchy_fp: float
    hierarchy_fn: float
    hierarchy_precision: float
    hierarchy_recall: float
    hierarchy_f1: float
    hierarchy_exact: int
    under_specific: int
    over_specific: int
    status: Status


@dataclass(frozen=True, slots=True)
class StructuredMetrics:
    """Micro, record-exact, hierarchy, and abstention aggregates."""

    record_count: int
    record_exact_count: int
    record_exact_accuracy: float
    tp: int
    fp: int
    fn: int
    micro_precision: float
    micro_recall: float
    micro_f1: float
    hierarchy_tp: float
    hierarchy_fp: float
    hierarchy_fn: float
    hierarchy_micro_precision: float
    hierarchy_micro_recall: float
    hierarchy_micro_f1: float
    hierarchy_exact: int
    under_specific: int
    over_specific: int
    abstention_count: int
    coverage: float
    selective_record_exact_accuracy: float


@dataclass(frozen=True, slots=True)
class CoreRecord:
    """One Core-Single gold label paired with a validated best-guess set."""

    sample_id: str
    gold_label: str
    best_guess: tuple[str, ...]
    status: Status = "predict"


@dataclass(frozen=True, slots=True)
class ClassMetrics:
    label: str
    support: int
    tp: int
    fp: int
    fn: int
    precision: float
    recall: float
    f1: float


@dataclass(frozen=True, slots=True)
class CoreMetrics:
    """Conventional multiclass metrics for the conservative Core-Single view."""

    record_count: int
    labels: tuple[str, ...]
    confusion_matrix: tuple[tuple[int, ...], ...]
    per_class: tuple[ClassMetrics, ...]
    accuracy: float
    macro_f1: float
    balanced_accuracy: float
    abstention_count: int
    coverage: float
    selective_accuracy: float

    @property
    def confusion(self) -> dict[str, dict[str, int]]:
        """Return an actual-row/predicted-column confusion mapping."""

        return {
            actual: {
                predicted: self.confusion_matrix[row_index][column_index]
                for column_index, predicted in enumerate(self.labels)
            }
            for row_index, actual in enumerate(self.labels)
        }


@dataclass(frozen=True, slots=True)
class _Candidate:
    gold_label: str
    weight: float
    relation: Relation


def ancestor_distances_from_pairs(
    pairs: Iterable[object],
) -> dict[str, dict[str, int]]:
    """Convert audited ontology pair records to the scorer's lookup form."""

    expected_fields = frozenset({"specific", "ancestor", "distance"})
    entries: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str]] = set()
    for index, pair in enumerate(pairs):
        if not isinstance(pair, Mapping):
            raise ValueError(f"ontology pair {index} must be an object")
        if frozenset(pair.keys()) != expected_fields:
            raise ValueError(
                f"ontology pair {index} fields must be {sorted(expected_fields)}"
            )
        specific = pair["specific"]
        ancestor = pair["ancestor"]
        distance = pair["distance"]
        if not isinstance(specific, str) or not specific:
            raise ValueError(f"ontology pair {index} has an invalid specific label")
        if not isinstance(ancestor, str) or not ancestor:
            raise ValueError(f"ontology pair {index} has an invalid ancestor label")
        if specific == ancestor:
            raise ValueError(f"ontology pair {index} must connect distinct labels")
        _validate_distance(specific, ancestor, distance)
        edge = (specific, ancestor)
        if edge in seen:
            raise ValueError(f"ontology pairs contain duplicate edge {edge!r}")
        seen.add(edge)
        entries.append((specific, ancestor, distance))

    distances: dict[str, dict[str, int]] = {}
    for specific, ancestor, distance in sorted(entries):
        distances.setdefault(specific, {})[ancestor] = distance
    return distances


def score_record(
    gold_blocks: Iterable[Iterable[str]],
    best_guess: Iterable[str],
    *,
    ancestor_distances: AncestorDistances | None = None,
    status: Status = "predict",
) -> RecordScore:
    """Score one CNF target against a flat, set-valued prediction.

    Each gold OR block is one requirement slot. Prediction labels and slots are
    paired one-to-one, so listing two accepted alternatives for one OR block
    earns one true positive and one false positive.
    """

    blocks = _normalize_blocks(gold_blocks)
    predictions = _normalize_predictions(best_guess)
    _validate_status(status)

    exact_candidates = _candidate_matrix(blocks, predictions, None)
    exact_pairs = _maximum_weight_pairs(
        [[candidate.weight for candidate in row] for row in exact_candidates]
    )
    tp = len(exact_pairs)
    fp = len(predictions) - tp
    fn = len(blocks) - tp
    precision, recall, f1 = _precision_recall_f1(tp, fp, fn)

    hierarchy_candidates = _candidate_matrix(
        blocks,
        predictions,
        ancestor_distances,
    )
    hierarchy_pairs = _maximum_weight_pairs(
        [[candidate.weight for candidate in row] for row in hierarchy_candidates]
    )
    matched_candidates = [
        hierarchy_candidates[prediction_index][block_index]
        for prediction_index, block_index in hierarchy_pairs
    ]
    hierarchy_tp = math.fsum(candidate.weight for candidate in matched_candidates)
    hierarchy_fp = float(len(predictions)) - hierarchy_tp
    hierarchy_fn = float(len(blocks)) - hierarchy_tp
    hierarchy_precision, hierarchy_recall, hierarchy_f1 = _precision_recall_f1(
        hierarchy_tp,
        hierarchy_fp,
        hierarchy_fn,
    )

    return RecordScore(
        tp=tp,
        fp=fp,
        fn=fn,
        precision=precision,
        recall=recall,
        f1=f1,
        exact=(tp == len(blocks) and fp == 0),
        hierarchy_tp=hierarchy_tp,
        hierarchy_fp=hierarchy_fp,
        hierarchy_fn=hierarchy_fn,
        hierarchy_precision=hierarchy_precision,
        hierarchy_recall=hierarchy_recall,
        hierarchy_f1=hierarchy_f1,
        hierarchy_exact=sum(
            candidate.relation == "exact" for candidate in matched_candidates
        ),
        under_specific=sum(
            candidate.relation == "under_specific"
            for candidate in matched_candidates
        ),
        over_specific=sum(
            candidate.relation == "over_specific" for candidate in matched_candidates
        ),
        status=status,
    )


def aggregate_record_scores(scores: Iterable[RecordScore]) -> StructuredMetrics:
    """Aggregate records without dropping abstentions from primary metrics."""

    values = tuple(scores)
    record_count = len(values)
    exact_count = sum(score.exact for score in values)
    tp = sum(score.tp for score in values)
    fp = sum(score.fp for score in values)
    fn = sum(score.fn for score in values)
    micro_precision, micro_recall, micro_f1 = _precision_recall_f1(tp, fp, fn)

    hierarchy_tp = math.fsum(score.hierarchy_tp for score in values)
    hierarchy_fp = math.fsum(score.hierarchy_fp for score in values)
    hierarchy_fn = math.fsum(score.hierarchy_fn for score in values)
    hierarchy_precision, hierarchy_recall, hierarchy_f1 = _precision_recall_f1(
        hierarchy_tp,
        hierarchy_fp,
        hierarchy_fn,
    )

    predicted = tuple(score for score in values if score.status == "predict")
    abstention_count = record_count - len(predicted)
    return StructuredMetrics(
        record_count=record_count,
        record_exact_count=exact_count,
        record_exact_accuracy=_ratio(exact_count, record_count),
        tp=tp,
        fp=fp,
        fn=fn,
        micro_precision=micro_precision,
        micro_recall=micro_recall,
        micro_f1=micro_f1,
        hierarchy_tp=hierarchy_tp,
        hierarchy_fp=hierarchy_fp,
        hierarchy_fn=hierarchy_fn,
        hierarchy_micro_precision=hierarchy_precision,
        hierarchy_micro_recall=hierarchy_recall,
        hierarchy_micro_f1=hierarchy_f1,
        hierarchy_exact=sum(score.hierarchy_exact for score in values),
        under_specific=sum(score.under_specific for score in values),
        over_specific=sum(score.over_specific for score in values),
        abstention_count=abstention_count,
        coverage=_ratio(len(predicted), record_count),
        selective_record_exact_accuracy=_ratio(
            sum(score.exact for score in predicted),
            len(predicted),
        ),
    )


def score_core_single(
    records: Iterable[CoreRecord],
    *,
    labels: Iterable[str] | None = None,
) -> CoreMetrics:
    """Compute deterministic multiclass metrics for Core-Single records."""

    values = tuple(records)
    seen_sample_ids: set[str] = set()
    for record in values:
        if not record.sample_id or record.sample_id in seen_sample_ids:
            raise ValueError("Core-Single sample_id values must be nonempty and unique")
        seen_sample_ids.add(record.sample_id)
        if len(record.best_guess) != 1:
            raise ValueError("Core-Single records require exactly one best_guess")
        _validate_status(record.status)

    observed = {
        label
        for record in values
        for label in (record.gold_label, record.best_guess[0])
    }
    if labels is None:
        ordered_labels = tuple(sorted(observed))
    else:
        supplied_labels = tuple(labels)
        if any(not isinstance(label, str) or not label for label in supplied_labels):
            raise ValueError("Core-Single labels must be nonempty strings")
        if len(set(supplied_labels)) != len(supplied_labels):
            raise ValueError("Core-Single labels must be unique")
        ordered_labels = tuple(sorted(supplied_labels))
        missing = observed - set(ordered_labels)
        if missing:
            raise ValueError(f"Core-Single labels omit observed values: {sorted(missing)}")

    label_index = {label: index for index, label in enumerate(ordered_labels)}
    mutable_confusion = [
        [0 for _ in ordered_labels]
        for _ in ordered_labels
    ]
    for record in values:
        mutable_confusion[label_index[record.gold_label]][
            label_index[record.best_guess[0]]
        ] += 1
    confusion = tuple(tuple(row) for row in mutable_confusion)

    class_metrics: list[ClassMetrics] = []
    for index, label in enumerate(ordered_labels):
        tp = confusion[index][index]
        support = sum(confusion[index])
        fn = support - tp
        fp = sum(confusion[row][index] for row in range(len(ordered_labels))) - tp
        precision, recall, f1 = _precision_recall_f1(tp, fp, fn)
        class_metrics.append(
            ClassMetrics(
                label=label,
                support=support,
                tp=tp,
                fp=fp,
                fn=fn,
                precision=precision,
                recall=recall,
                f1=f1,
            )
        )

    correct = sum(confusion[index][index] for index in range(len(ordered_labels)))
    supported = [metric for metric in class_metrics if metric.support > 0]
    predicted_records = tuple(
        record for record in values if record.status == "predict"
    )
    selective_correct = sum(
        record.gold_label == record.best_guess[0] for record in predicted_records
    )
    abstention_count = len(values) - len(predicted_records)
    return CoreMetrics(
        record_count=len(values),
        labels=ordered_labels,
        confusion_matrix=confusion,
        per_class=tuple(class_metrics),
        accuracy=_ratio(correct, len(values)),
        macro_f1=_ratio(
            math.fsum(metric.f1 for metric in class_metrics),
            len(class_metrics),
        ),
        balanced_accuracy=_ratio(
            math.fsum(metric.recall for metric in supported),
            len(supported),
        ),
        abstention_count=abstention_count,
        coverage=_ratio(len(predicted_records), len(values)),
        selective_accuracy=_ratio(selective_correct, len(predicted_records)),
    )


def _normalize_blocks(
    gold_blocks: Iterable[Iterable[str]],
) -> tuple[frozenset[str], ...]:
    if isinstance(gold_blocks, (str, bytes)):
        raise ValueError("gold_blocks must contain nonempty OR blocks")
    normalized: list[frozenset[str]] = []
    for block_index, block in enumerate(gold_blocks):
        if isinstance(block, (str, bytes)):
            raise ValueError(f"gold block {block_index} must be an iterable of labels")
        labels = tuple(block)
        if not labels:
            raise ValueError(f"gold block {block_index} must not be empty")
        if any(not isinstance(label, str) or not label for label in labels):
            raise ValueError(f"gold block {block_index} contains an invalid label")
        if len(set(labels)) != len(labels):
            raise ValueError(f"gold block {block_index} contains duplicate labels")
        normalized.append(frozenset(labels))
    if not normalized:
        raise ValueError("gold_blocks must contain at least one OR block")
    return tuple(normalized)


def _normalize_predictions(best_guess: Iterable[str]) -> tuple[str, ...]:
    if isinstance(best_guess, (str, bytes)):
        raise ValueError("best_guess must be an iterable of labels")
    labels = tuple(best_guess)
    if any(not isinstance(label, str) or not label for label in labels):
        raise ValueError("best_guess contains an invalid label")
    if len(set(labels)) != len(labels):
        raise ValueError("best_guess contains duplicate labels")
    return tuple(sorted(labels))


def _validate_status(status: str) -> None:
    if status not in {"predict", "abstain"}:
        raise ValueError("status must be 'predict' or 'abstain'")


def _candidate_matrix(
    blocks: Sequence[frozenset[str]],
    predictions: Sequence[str],
    ancestor_distances: AncestorDistances | None,
) -> list[list[_Candidate]]:
    return [
        [
            _best_candidate(prediction, block, ancestor_distances)
            for block in blocks
        ]
        for prediction in predictions
    ]


def _best_candidate(
    prediction: str,
    block: frozenset[str],
    ancestor_distances: AncestorDistances | None,
) -> _Candidate:
    candidates: list[_Candidate] = []
    for gold_label in sorted(block):
        if prediction == gold_label:
            candidates.append(_Candidate(gold_label, 1.0, "exact"))
            continue
        if ancestor_distances is None:
            continue

        under_distance = ancestor_distances.get(gold_label, {}).get(prediction)
        if under_distance is not None:
            _validate_distance(gold_label, prediction, under_distance)
            candidates.append(
                _Candidate(
                    gold_label,
                    1.0 / (1.0 + under_distance),
                    "under_specific",
                )
            )

        over_distance = ancestor_distances.get(prediction, {}).get(gold_label)
        if over_distance is not None:
            _validate_distance(prediction, gold_label, over_distance)
            candidates.append(
                _Candidate(
                    gold_label,
                    1.0 / (1.0 + over_distance),
                    "over_specific",
                )
            )

    if not candidates:
        return _Candidate("", 0.0, "exact")
    relation_order = {"exact": 0, "under_specific": 1, "over_specific": 2}
    return min(
        candidates,
        key=lambda candidate: (
            -candidate.weight,
            relation_order[candidate.relation],
            candidate.gold_label,
        ),
    )


def _validate_distance(descendant: str, ancestor: str, distance: object) -> None:
    if isinstance(distance, bool) or not isinstance(distance, int) or distance < 1:
        raise ValueError(
            f"ancestor distance {descendant!r}->{ancestor!r} must be a positive integer"
        )


def _maximum_weight_pairs(weights: Sequence[Sequence[float]]) -> tuple[tuple[int, int], ...]:
    """Return a deterministic maximum-weight one-to-one bipartite matching."""

    prediction_count = len(weights)
    if prediction_count == 0:
        return ()
    block_count = len(weights[0])
    if any(len(row) != block_count for row in weights):
        raise ValueError("weight matrix must be rectangular")
    transposed = prediction_count > block_count
    oriented_weights = (
        [
            [weights[prediction][block] for prediction in range(prediction_count)]
            for block in range(block_count)
        ]
        if transposed
        else [list(row) for row in weights]
    )
    row_count = len(oriented_weights)
    if row_count == 0:
        return ()
    column_count = len(oriented_weights[0])
    costs = [[-weight for weight in row] for row in oriented_weights]

    # Hungarian algorithm for row_count <= column_count.
    row_potential = [0.0] * (row_count + 1)
    column_potential = [0.0] * (column_count + 1)
    row_for_column = [0] * (column_count + 1)
    previous_column = [0] * (column_count + 1)
    for row in range(1, row_count + 1):
        row_for_column[0] = row
        minimum = [math.inf] * (column_count + 1)
        used = [False] * (column_count + 1)
        column = 0
        while True:
            used[column] = True
            active_row = row_for_column[column]
            delta = math.inf
            next_column = 0
            for candidate_column in range(1, column_count + 1):
                if used[candidate_column]:
                    continue
                reduced_cost = (
                    costs[active_row - 1][candidate_column - 1]
                    - row_potential[active_row]
                    - column_potential[candidate_column]
                )
                if reduced_cost < minimum[candidate_column]:
                    minimum[candidate_column] = reduced_cost
                    previous_column[candidate_column] = column
                if minimum[candidate_column] < delta:
                    delta = minimum[candidate_column]
                    next_column = candidate_column
            for candidate_column in range(column_count + 1):
                if used[candidate_column]:
                    row_potential[row_for_column[candidate_column]] += delta
                    column_potential[candidate_column] -= delta
                else:
                    minimum[candidate_column] -= delta
            column = next_column
            if row_for_column[column] == 0:
                break
        while True:
            prior = previous_column[column]
            row_for_column[column] = row_for_column[prior]
            column = prior
            if column == 0:
                break

    oriented_pairs = [
        (row_for_column[column] - 1, column - 1)
        for column in range(1, column_count + 1)
        if row_for_column[column] != 0
        and oriented_weights[row_for_column[column] - 1][column - 1] > 0.0
    ]
    pairs = (
        [(column, row) for row, column in oriented_pairs]
        if transposed
        else oriented_pairs
    )
    return tuple(sorted(pairs))


def _precision_recall_f1(
    tp: float,
    fp: float,
    fn: float,
) -> tuple[float, float, float]:
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    f1 = _ratio(2.0 * precision * recall, precision + recall)
    return precision, recall, f1


def _ratio(numerator: float, denominator: float) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


__all__ = [
    "AncestorDistances",
    "ClassMetrics",
    "CoreMetrics",
    "CoreRecord",
    "RecordScore",
    "StructuredMetrics",
    "aggregate_record_scores",
    "ancestor_distances_from_pairs",
    "score_core_single",
    "score_record",
]
