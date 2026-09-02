# Specification: Cofactor9.1 benchmark

## Objective

Construct a reproducible benchmark from the frozen UniProt 2026_02 query

```text
reviewed:true AND ec:* AND fragment:false AND length:[50 TO 1100]
AND cc_cofactor_chebi_exp:*
```

and run a sequence-only `gpt-5.6-sol` baseline with maximum reasoning effort
and fast/priority service when the available runtime supports it.

The benchmark must preserve all 5,337 accessions with at least one cofactor
object carrying `ECO:0000269`. It must not misrepresent the population as a
flat, mutually exclusive 104-way classification dataset.

## Scientific views

### 0. SourceCandidates-7008

An audit table for every record returned by the frozen query. Exactly 1,671
rows must explain why they did not enter Master-5337; no source candidate is
allowed to disappear from the audit trail.

### 1. Master-5337

An immutable accession-level inventory containing every eligible record,
source provenance, sequence hash, structured cofactor blocks, evidence links,
scope, notes for curator audit, ontology ancestry and reason codes.

### 2. Full-Structured

The primary benchmark view. Its target is a block formula:

```text
AND(block_1, block_2, ...)
block_i = OR(label_1, label_2, ...)
```

The `molecule` field conditionally scopes a block to a chain or isoform. The
original ChEBI term remains the exact target; ancestors are stored separately.

### 3. Core-Provisional

A conservative derived view for conventional exact classification. A record is
eligible only if it has one experimental label and one label across all
cofactor annotations, uses a leaf target term, matches the supplied sequence
scope, has no unknown `X`, has no unresolved note-semantic risk, and is the
representative of a non-conflicting exact-sequence group. Representative
selection happens after the accession-local quality predicates: among eligible
members of one exact-sequence entity, choose the lexicographically first
accession. Thus an ineligible accession can never suppress a clean identical
sequence merely because its accession sorts first. `U` is retained as valid
selenocysteine.

`single_clean` is the structural checkpoint before these extra exclusions:
3,971 accessions, 3,942 exact-sequence entities and 64 represented labels. The
final Core count must not be claimed until note adjudication is complete. Any
automated overnight view is named `core_provisional` and records its triage
rule version.

### 4. Ambiguity-Challenge

Contains alternatives, multiple required cofactors, activity/condition scope,
coarse ontology terms, molecule scope, annotation contradictions and exact
sequence label conflicts. These are biologically informative cases, not trash.

## Evidence contract

- Label eligibility is determined at the individual cofactor object.
- `ECO:0000269` with `source=PubMed` is accepted.
- `ECO:0000269` with `source=Reference` is accepted only when the referenced
  UniProt citation resolves in the entry.
- Other evidence codes remain in the audit object but do not create an
  experimental gold label.
- Notes can add risk flags but cannot silently create a gold label.

Evidence status is an exclusive enum:

- `ALL_DIRECT_PUBMED`: 5,210 entries.
- `MIXED_DIRECT_AND_REFERENCE`: 3 entries.
- `ALL_REFERENCE_ONLY`: 124 entries.

Across the snapshot there are 6,981 experimental label occurrences and 6,974
unique accession--experimental-ChEBI pairs. Occurrences and all their evidence
must be preserved; deduplicated labels are a derived projection only.

## Formula contract

Block formulas across all 5,337 entries form four mutually exclusive shapes:

- `SINGLE`: 4,178 entries.
- `PURE_OR`: 647 entries.
- `PURE_AND`: 449 entries.
- `MIXED_AND_OR`: 63 entries.

The separate label-cardinality checkpoint remains 4,179 entries with one unique
experimental ChEBI ID; one of those entries contains two equal singleton blocks
and therefore has a `PURE_AND`, not `SINGLE`, formula. For scoring, canonicalize
deterministically by deduplicating labels only inside each block, then sorting
labels and blocks. Equal blocks, subset/superset blocks and their multiplicity
must remain intact: separate UniProt blocks are jointly annotated facts and may
not be absorbed. The frozen snapshot yields 5,911 non-empty experimental blocks.
Six accessions contain a label repeated across blocks, spanning eight overlapping
block pairs. Their gold formulas remain in Master/Full/Challenge, but the current
set-valued response cannot represent block role or multiplicity, so they receive
zero weight in every formal ranking and form a separate diagnostic slice.

## Exact duplicate contract

- `sequence_entity_id = sha256(sequence)`.
- All accession rows remain in Master-5337.
- Same-sequence, same-formula accessions share one sequence entity and one
  scoring weight.
- A sequence hash with different gold formulas is marked
  `EXACT_SEQUENCE_LABEL_CONFLICT`; the entire group requires adjudication.
- Near homologs are retained and receive cluster IDs for cluster-weighted
  reporting. They are not blindly deleted.

## Reason-code vocabulary

- `EXTRA_NONEXPERIMENTAL_LABEL`
- `PURE_OR_FORMULA`
- `PURE_AND_FORMULA`
- `MIXED_AND_OR_FORMULA`
- `OVERLAPPING_BLOCK_LABEL`
- `REFERENCE_ONLY_EXPERIMENTAL_EVIDENCE`
- `PARTIAL_DIRECT_PUBMED_EVIDENCE`
- `DUPLICATE_EXPERIMENTAL_LABEL_OCCURRENCE`
- `NOTE_OTHER_COFACTOR_MENTION`
- `NOTE_ALTERNATIVE_OR_COMPARISON`
- `NOTE_PREFERENCE_OR_PARTIAL_ACTIVITY`
- `NOTE_CONDITIONAL_OR_ACTIVITY_SCOPE`
- `NOTE_UNCERTAINTY`
- `NOTE_NEGATION_OR_INHIBITION`
- `NOTE_STATE_CHANGE_OR_HISTORY`
- `NOTE_GENERIC_LABEL_SPECIFIC_MENTIONS`
- `ONTOLOGY_ANCESTOR_TARGET`
- `MOLECULE_SCOPE`
- `UNKNOWN_RESIDUE_X`
- `SELENOCYSTEINE_U`
- `EXACT_SEQUENCE_DUPLICATE`
- `EXACT_SEQUENCE_LABEL_CONFLICT`
- `UNRESOLVED_REFERENCE_EVIDENCE`
- `ANNOTATION_CONTRADICTION`

Reason codes are additive and auditable. They are not implicit deletion rules.
Formula shape, evidence status, alphabet status and exact-sequence status are
also stored as exclusive enums. A note rule can only set adjudication to
`PENDING`; it can never auto-exclude a record. Core membership is an explicit
predicate, never the absence of reason codes.

## Master field contract

Each master row stores, at minimum:

- Source release/query/artifact hash and raw record index.
- Accession, UniProt ID, entry/sequence versions, update date, organism/taxon
  and EC numbers.
- Sequence value, length, CRC64, SHA-256, alphabet enum and nonstandard symbols.
- Every raw cofactor block with ordinal, molecule, notes and every label
  occurrence with its ordinal, ChEBI/name and complete evidence list.
- Resolved citation metadata for accepted `Reference` evidence.
- Experimental/all-label projections, canonical gold formula, formula shape,
  ontology ancestry, sequence entity/group status, memberships and reasons.
- An adjudication object with status, decision, reviewer, rationale, evidence
  occurrence IDs and rule version.

## Model input and response contract

Each request receives only a sequence-derived opaque sample ID, the amino-acid
sequence and the same frozen 104-term catalog of `{chebi_id, name}` objects.
The ID is a domain-separated SHA-256 projection of the sequence SHA-256 and its
stable ordinal within an exact-sequence group; neither its token nor public case
ordering is derived from accession. Thus it adds no metadata beyond the supplied
sequence while remaining unique for all 5,337 accession-level calls.
`name` is exactly the preferred label in the frozen ChEBI 254 ontology; the
prompt projection never contains accession counts, frequency bands, UniProt
display names or any case-specific subset. The catalog is sorted by numeric
ChEBI ID, never by frequency, and its order carries no likelihood signal.

The task is specifically to predict UniProt-style `COFACTOR` annotations, not
ordinary substrates, products or non-cofactor ligands. The model returns an
unordered joint assertion set: one label for every jointly required `AND`
block, and only its most likely label for an interchangeable `OR` block. The
list is not top-k alternatives. It must return strict JSON:

```json
{
  "schema_version": "cofactor9.1.response.v2",
  "sample_id": "sample_00000000000000000000000000000000",
  "status": "predict",
  "predicted_cofactors": ["CHEBI:18420"],
  "primary_guess": "CHEBI:18420",
  "confidence_complete": 0.75
}
```

No accession, organism, EC number, database note, literature ID or gold label
may enter the model prompt. Raw responses, parsed responses, retries, latency,
token usage and errors are stored under an immutable run ID.

`confidence_complete` is the model's probability that the entire joint set is
record-exact, with no missing or extra cofactor. `status` is `predict` iff this
value is at least the preregistered threshold 0.5, otherwise `abstain`. Even an
abstention must include a non-empty joint set and a `primary_guess` that belongs
to it; primary scores still evaluate the prediction. `primary_guess` exists
only for conventional top-1 Core-Provisional evaluation and never changes structured
set scoring. Malformed JSON, an out-of-vocabulary label, a duplicate label, an
unexpected field, threshold-inconsistent status or a mismatched sample ID is an
explicit prediction error and is never repaired by another LLM.

The authenticated transport is Codex CLI 0.152.0 using ChatGPT OAuth. A new
run resolves the installed Node launcher to the native executable, copies the
217,812,496-byte native binary into the run, records its SHA-256 and mode, and
executes only that read-only `0555` copy. Resume therefore does not depend on
the external Codex installation. The manifest also freezes the Python launcher
runtime path/version/hash and the exact observed feature states.

Every attempt runs in a fresh empty `/private/tmp` directory with user
config/rules ignored and read-only sandboxing. Shell, web, browser, MCP, app,
plugin, image, hook, goal, multi-agent, sleep, tool-suggestion, host-code and
in-app-browser surfaces are disabled and preflighted against the frozen native
binary. Codex 0.152.0 reports `unified_exec=true` as an internal backend while
`shell_tool=false`; the manifest records this exception instead of falsely
claiming every internal feature is false. Any exposed tool event or unknown
event invalidates the attempt.

Before a request can start, the complete prompt and empty stdout/stderr files
are atomically published. A durable supervisor writes and fsyncs launch
identity before opening its execution gate, binds model stdio directly to
those files, and writes a completion witness containing return code, timeout,
cancellation and start-error state. If the parent process dies, resume first
checks both process groups and raw-replays a durable completion; it does not
discard an already completed response and pay for an automatic duplicate.
The irreducible machine-loss boundary is recorded explicitly as
`INTERRUPTED_ATTEMPT / remote outcome unknown` before a later retry.

The run manifest freezes all evaluation artifacts and implementation hashes.
Run-level invocations, attempts, incidents and terminals are append-only and
validated from raw bytes. A lifecycle lock prevents concurrent run/resume or
run/score mutation. Once all 5,337 terminals exist, `--resume` becomes a
strict, byte-preserving validation no-op; retrying a terminal error requires a
new run ID. Score stages both deterministic outputs under the same lifecycle
lock and publishes `metrics.json` plus `report.md` with crash-recoverable,
no-clobber semantics.

## Scoring contract

Report at minimum:

- Full-Structured block coverage and label precision/recall/F1.
- Exact ChEBI and hierarchy-aware label scores.
- Core-Provisional accuracy, macro-F1 and balanced accuracy.
- Head/mid/tail results using marginal accession frequency in Master-5337:
  Head >=100, Mid 10--99, Tail <10 (14/20/70 labels).
- Accession-micro, label-macro and exact-sequence-entity macro results.
- Abstention coverage and selective accuracy.
- Parse failure and terminal error rates.

The scorer builds a prediction-to-gold-block bipartite graph and takes a maximum
cardinality one-to-one matching. Thus gold `[{A,B}]` with prediction `[A]` is
exact, `[A,B]` has one false positive, and gold `[{A},{B}]` requires both. It
reports record exact and block precision/recall/F1. Hierarchy-aware scoring is
diagnostic only: an ancestor/descendant at distance `d` receives `1/(1+d)`,
while strict ChEBI exact remains the headline ranking.

Main structured metrics always score `predicted_cofactors` regardless of
abstention; Core-Provisional top-1 metrics use `primary_guess`. Separately report
coverage, selective risk/AURC, record-exact Brier score and calibration. Exact
sequence-consistent groups receive total weight one. The six conflicting
sequence groups (12 accessions) and six cross-block-overlap accessions are
disjoint; all 18 receive zero weight in accession, entity, label, cluster,
frequency, hierarchy and calibration rankings and are reported in explicit
diagnostic slices while remaining in raw results and model calls. This leaves
5,319 rank-eligible accessions, 5,283 exact-sequence entities and 5,056 homology
clusters. Core-Provisional contains no excluded record.

## Project structure

```text
AGENTS.md                    project rules
config/benchmark.json        frozen paths and run settings
docs/spec.md                 scientific and engineering contract
tasks/                       implementation plan and ledger
cofactor_bench/              parser, views, validation, runner, scorer
tests/                       unit and integration tests
data/raw/                    immutable source snapshots (not committed)
data/manifests/              hashes, releases and queries
data/derived/                deterministic master and view JSONL files
runs/<run_id>/               prompts, raw responses, predictions and metrics
reports/                     human-readable audit and result reports
```

## Commands

```bash
python3 -m unittest discover -s tests -v
python3 -m cofactor_bench.cli build --config config/benchmark.json
python3 -m cofactor_bench.cli validate --config config/benchmark.json
python3 -m cofactor_bench.cli run --config config/benchmark.json --run-id <gate-id> --infrastructure-gate --limit 1
python3 -m cofactor_bench.cli run --config config/benchmark.json --run-id <formal-id>
python3 -m cofactor_bench.cli run --config config/benchmark.json --run-id <formal-id> --resume
python3 -m cofactor_bench.cli validate --config config/benchmark.json --stage run --run-id <formal-id>
python3 -m cofactor_bench.cli score --config config/benchmark.json --run-id <formal-id>
```

The explicitly named infrastructure gate verifies transport only. It is never
reported as a pilot or final benchmark result; the formal run defaults to all
5,337 cases and may not use `--limit`. The formal runner itself executes its
first selected case as a same-run transport probe and expands to concurrency
16 only after that case succeeds; this is not a separate pilot or subset.

## Testing strategy

- Small unit tests: evidence scoping, block formula construction, note triage,
  ontology ancestry, duplicate grouping and scoring.
- Medium integration tests: synthetic UniProt payload to deterministic JSONL;
  response parsing and resumable run ledger.
- End-to-end validation: frozen snapshot produces expected invariant counts,
  all outputs pass schema checks, and scoring reproduces from saved responses.

Tests use Python's standard-library `unittest` unless a dependency is justified
and recorded.

## Boundaries

- Always: preserve raw inputs; hash every artifact; separate gold from prompts;
  use deterministic ordering and fixed seeds; retain exclusion reason codes.
- Ask first: change the biological question, publish artifacts, or incur an
  unbounded external cost not implied by the full-run request.
- Never: mutate raw data, expose secrets, hide failures, silently normalize a
  ChEBI label, or report a partial smoke run as the full result.

## Success criteria

1. The snapshot manifest verifies UniProt release `2026_02`, 7,008 input
   records, 5,337 experimental candidates and 104 experimental ChEBI labels.
2. Master-5337 contains exactly 5,337 unique accessions and preserves every
   source block, label evidence and scope.
3. Derived-view counts and every exclusion/flag are reproducible from source.
4. Exact duplicate and ontology-overlap invariants match independent audits.
5. No gold metadata appears in any model prompt artifact.
6. Unit, integration and frozen-data validation tests pass.
7. The `gpt-5.6-sol` run is resumable and produces one terminal, parseable
   record per benchmark case, or an explicit audited failure after retries.
8. Saved predictions reproduce the final machine-readable metrics and a human
   report without another model call.
9. Frozen assertions include 4,179 entries with one unique experimental label,
   3,971 single-clean entries, 4,178/647/449/63 formula shapes, 5,911 preserved
   blocks, six overlap accessions/eight overlapping block pairs, 39 duplicate
   sequence groups with six conflicts, and 59 ChEBI ancestor pairs involving 44
   terms.

## Approved assumptions

The user explicitly approved continuing the proposed design and autonomous
overnight execution. Therefore Master-5337 + Full-Structured primary +
Core-Provisional and Ambiguity-Challenge derived views are treated as approved.

The exact model transport is intentionally adapter-based until the local
authenticated runtime is verified. Transport choice may not change prompts,
case selection or scoring semantics.
