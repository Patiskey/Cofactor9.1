# Evaluation worker log

- Completed: `2026-09-02T01:43:52+08:00`
- Ownership: `cofactor_bench/scoring.py`, `cofactor_bench/prediction.py`,
  `tests/test_scoring.py`, `tests/test_prediction.py`, and this log only.
- Runtime: Python 3.14.6, standard library only.
- Git: no commit created, as requested by the root agent.

## Implemented contract

- Strict response schema version `cofactor9.1.response.v1` with the closed field
  set `schema_version`, `sample_id`, `status`, `best_guess`, and
  `confidence_complete`.
- Exact sample-ID binding, `predict|abstain`, nonempty unique canonical ChEBI
  guesses, caller-supplied optional label-count ceiling, catalog OOV rejection,
  finite `[0, 1]` confidence, duplicate JSON-key rejection, and no response
  repair.
- Frozen typed `Prediction`; abstentions retain their nonempty `best_guess`.
- Gold CNF scoring with one requirement slot per OR block and deterministic
  maximum-cardinality one-to-one matching.
- Record exactness and aggregate exact micro precision/recall/F1.
- Maximum-weight hierarchy matching, weight `1 / (1 + distance)`, plus exact,
  under-specific, and over-specific match counts.
- Deterministic conversion from ontology `specific/ancestor/distance` pair
  records to descendant-to-ancestor distance maps.
- Core-Single confusion matrix, accuracy, per-class metrics, macro-F1, and
  balanced accuracy.
- Primary structured and Core scores include every abstention; coverage and
  selective accuracy are reported separately.

## TDD evidence

### Prediction RED

Command:

```text
python3 -m unittest tests.test_prediction -v
```

Observed output before implementation:

```text
ModuleNotFoundError: No module named 'cofactor_bench.prediction'
Ran 1 test in 0.000s
FAILED (errors=1)
```

### Prediction GREEN

Command:

```text
python3 -m unittest tests.test_prediction -v
```

Observed output:

```text
Ran 14 tests in 0.001s
OK
```

### Scoring RED

Command:

```text
python3 -m unittest tests.test_scoring -v
```

Observed output before implementation:

```text
ModuleNotFoundError: No module named 'cofactor_bench.scoring'
Ran 1 test in 0.000s
FAILED (errors=1)
```

### Scoring GREEN

Command:

```text
python3 -m unittest tests.test_scoring -v
```

Observed output after the initial scoring implementation:

```text
Ran 16 tests in 0.001s
OK
```

### Ontology conversion RED

Command:

```text
python3 -m unittest tests.test_scoring.AncestryConversionTests -v
```

Observed output before the conversion helper existed:

```text
ImportError: cannot import name 'ancestor_distances_from_pairs' from 'cofactor_bench.scoring'
Ran 1 test in 0.000s
FAILED (errors=1)
```

### Ontology conversion and final scoring GREEN

Commands:

```text
python3 -m unittest tests.test_scoring.AncestryConversionTests -v
python3 -m unittest tests.test_scoring -v
```

Observed output:

```text
Ran 2 tests in 0.000s
OK
Ran 18 tests in 0.001s
OK
```

## Additional verification

The polynomial matcher was cross-checked against exhaustive enumeration on
2,500 seeded random matrices after orienting the Hungarian algorithm with the
smaller bipartite side as rows:

```text
randomized Hungarian cross-check: 2,500 matrices OK
```

An early full-suite probe, while other workers still had missing parser/build
modules and a runner stub, reported `FAILED (errors=11)`. No evaluator test
failed. After those shared files stabilized, the required full-suite command
was rerun:

```text
python3 -m unittest discover -s tests -v
```

Final observed output:

```text
Ran 69 tests in 8.074s
OK
```

## Review notes

- Correctness: hand-calculated OR, AND, overlapping-block, hierarchy, aggregate,
  abstention, and Core fixtures pass.
- Security: model output is treated only as data; strict JSON/schema validation
  rejects malformed, duplicate-key, extra-field, OOV, nonfinite, and mismatched
  responses. It is never evaluated or repaired.
- Performance: weighted matching is polynomial and transposes imbalanced
  prediction/block matrices so the smaller side drives augmentation.
- Dependencies/secrets: no dependency was added and no credential-bearing code
  or output was introduced.
