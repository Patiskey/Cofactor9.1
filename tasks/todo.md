# Cofactor9.1 task ledger

- [ ] Task 1: Freeze source artifacts and manifest
  - Acceptance: UniProt and ChEBI files have verified hashes, releases, query,
    timestamps and source URLs.
  - Verify: `python3 -m cofactor_bench.cli validate --stage raw`
  - Files: `config/benchmark.json`, `data/manifests/source.json`

- [ ] Task 2: Implement evidence/block parser with TDD
  - Acceptance: label-level ECO filtering, Reference resolution and AND/OR
    blocks pass synthetic tests.
  - Verify: `python3 -m unittest tests.test_parser -v`
  - Files: `cofactor_bench/model.py`, `cofactor_bench/parser.py`,
    `tests/test_parser.py`

- [ ] Task 3: Build and validate Master-5337
  - Acceptance: 5,337 unique accessions and 104 experimental labels; no missing
    sequence or ChEBI ID.
  - Verify: `python3 -m cofactor_bench.cli build` then `... validate --stage master`
  - Files: `cofactor_bench/build.py`, `tests/test_build.py`, generated data

- [ ] Task 4: Implement conservative reason codes
  - Acceptance: note, scope, U/X and mixed-evidence fixtures receive expected
    additive codes.
  - Verify: `python3 -m unittest tests.test_triage -v`
  - Files: `cofactor_bench/triage.py`, `tests/test_triage.py`

- [ ] Task 5: Implement ontology and exact-sequence grouping
  - Acceptance: ancestor relations and six conflicting exact-sequence groups
    reproduce from frozen data.
  - Verify: `python3 -m unittest tests.test_ontology tests.test_duplicates -v`
  - Files: ontology/duplicate modules and tests

- [ ] Task 6: Generate Full/Core/Challenge and audit report
  - Acceptance: all master rows reconcile across documented views and reasons.
  - Verify: `python3 -m cofactor_bench.cli validate --stage views`
  - Files: `cofactor_bench/views.py`, view tests, generated reports

- [ ] Task 7: Implement leakage-safe prompt/response schemas
  - Acceptance: prompt contains only opaque ID, sequence and allowed catalog;
    invalid JSON and leaked gold fail tests.
  - Verify: `python3 -m unittest tests.test_prompt -v`
  - Files: `cofactor_bench/prompt.py`, `tests/test_prompt.py`

- [ ] Task 8: Implement scorer with hand-calculated fixtures
  - Acceptance: compatible/complete block metrics, hierarchy, Core metrics and
    abstention match fixture expectations.
  - Verify: `python3 -m unittest tests.test_score -v`
  - Files: `cofactor_bench/score.py`, `tests/test_score.py`

- [ ] Task 9: Implement resumable runner ledger
  - Acceptance: interrupted fake run resumes exactly once per pending case and
    preserves raw attempts.
  - Verify: `python3 -m unittest tests.test_runner -v`
  - Files: `cofactor_bench/runner.py`, `tests/test_runner.py`

- [ ] Task 10: Pass a real one-case transport gate
  - Acceptance: exact `gpt-5.6-sol` model/settings recorded; one parseable
    terminal response using a real sequence and the full named catalog; no
    prompt leakage. This is infrastructure validation, not a pilot result.
  - Verify: inspect run manifest and `... validate --stage run`

- [ ] Task 11: Complete the configured full run
  - Acceptance: one terminal ledger record per configured benchmark case, with
    retries/errors fully audited.
  - Verify: `python3 -m cofactor_bench.cli validate --stage run`

- [ ] Task 12: Produce reproducible metrics and report
  - Acceptance: JSON metrics and Markdown report regenerate from saved
    predictions without model calls.
  - Verify: `python3 -m cofactor_bench.cli score` and full test suite
