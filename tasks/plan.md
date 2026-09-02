# Implementation plan: Cofactor9.1 benchmark

## Overview

Build the benchmark in risk-first vertical slices: first prove frozen-data
parsing and invariants, then construct deterministic views, then prove scoring
on fixtures, then connect an authenticated resumable model transport, run the
complete view and render the result report.

## Architecture decisions

- Python standard library keeps the pipeline portable and audit-friendly.
- JSONL and content hashes make every stage inspectable and resumable.
- Master data is accession-level; scoring also groups exact sequence hashes.
- Block formulas and ontology ancestry are preserved rather than flattened.
- The runner is an adapter boundary so transport changes cannot affect science.

## Dependency graph

```text
frozen raw JSON + ChEBI ontology
        -> parser + manifest verification
        -> master JSONL
        -> reason codes + derived views
        -> validation report
        -> prompt cases
        -> model runner ledger
        -> scorer
        -> result report
```

## Phase 1: Foundation

- Task 1: Freeze source artifacts and manifest.
- Task 2: Define tested domain schema and evidence/block parser.
- Task 3: Build Master-5337 and validate invariant counts.

### Checkpoint: Foundation

- Frozen hashes verify and parser tests pass.
- Master contains exactly 5,337 accessions and 104 experimental labels.

## Phase 2: Views and audit

- Task 4: Add conservative note, scope and sequence reason codes.
- Task 5: Add ChEBI ancestry and exact-sequence grouping.
- Task 6: Generate Full, Core and Challenge view files plus audit report.

### Checkpoint: Views

- Every master row belongs to documented views with explicit reasons.
- Independent counts reconcile; no source row disappears.

## Phase 3: Evaluation engine

- Task 7: Define leakage-safe case and response schemas.
- Task 8: Implement block-aware, hierarchy-aware and Core metrics using TDD.
- Task 9: Implement append-only resumable run ledger and transport adapter.

### Checkpoint: Evaluation engine

- Fixture predictions produce hand-calculated metrics.
- Interrupted fake runs resume without duplicate calls.
- Prompt-leak validator rejects accession, note, evidence and gold leakage.

## Phase 4: Real run and report

- Task 10: Verify authenticated `gpt-5.6-sol` max/fast transport with a real
  one-case infrastructure gate. This is not a pilot or a reported result.
- Task 11: Run the complete configured benchmark view with retries and resume.
- Task 12: Score saved responses and write machine/human result reports.

### Checkpoint: Complete

- All tests and frozen-data validation pass.
- Every requested case has a terminal ledger record.
- Metrics reproduce from saved responses with no model call.
- Run manifest records exact model, prompt, settings, commands and hashes.

## Parallel ownership

- Data worker: parser, manifest, views and their tests.
- Evaluation worker: schemas, scorer and scorer tests.
- Runtime worker: transport probe, runner ledger and smoke/full-run operations.
- Root agent: contracts, integration, conflict resolution and final audit.

Workers share the workspace and must not edit files outside their ownership.

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Model transport unavailable | Full run cannot start | Probe local authenticated CLI/API first; keep adapter and case artifacts ready |
| Full max-reasoning run is long | Overnight completion risk | Append-only resume, bounded concurrency, immediate retries and progress ledger |
| Prompt leaks gold metadata | Invalid result | Generate opaque cases and run a deny-list/leakage validator before calls |
| Flat scoring corrupts multi-label semantics | Invalid science | Preserve block AND/OR and separately score compatible vs complete annotation |
| Note rules over-delete valid biology | Biased core | Flags create manual queue; they never delete Master-5337 |
| Ontology drift | Irreproducible hierarchy | Freeze ChEBI ontology hash and version in manifest |
| Parallel agent edits collide | Lost work | Explicit file ownership and root integration |

## Open questions resolved by safe defaults

- The full structured view is primary; Core is a conventional secondary view.
- Exact sequence entities receive equal macro weight while accessions remain in
  Master-5337.
- Low-confidence or malformed model output is retried, then retained as an
  explicit terminal failure rather than silently dropped.
