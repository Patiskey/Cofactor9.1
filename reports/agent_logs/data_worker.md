# Data foundation worker log

Date: 2026-09-02 (Asia/Shanghai)

Scope: Tasks 1–3 only. Implemented source freezing/verification, typed UniProt
cofactor parsing, deterministic SourceCandidates-7008 and Master-5337 builds.
Ontology ancestry, note triage and exact-sequence grouping were intentionally
left to their later task slices.

## Frozen source verification

| Artifact | Compressed SHA-256 | Bytes | Decompressed SHA-256 | Decompressed bytes |
|---|---:|---:|---:|---:|
| `uniprotkb_cofactor_query_2026_02.json.gz` | `366b6d5924e8138f5e2e6af11bc9f638ef0b37e97123cb9f89d4a759c59612ea` | 34,274,616 | `4b02445b9254579b4a38493f41a4859e131cf9d1ee257665ec1ae1f8710fee43` | 211,025,065 |
| `uniprotkb_cofactor_query_2026_02.headers.txt` | `9baf74dc0186e7791e24d301da7075fd4bfc1bf2c9b564e1d600d37289dafd7e` | 868 | — | — |
| `chebi_lite_2026-08-14.json.gz` | `d4102c38436128a0fc434dcd250e3d86cf6b36e9315e9c13444b5bf37ab47afe` | 9,490,617 | `d14526badabba6959c9d5facacb8a97d84b006347fae84cb5d7ca30a91ccd131` | 186,860,803 |

The UniProt response header reports release `2026_02`, release date
`10-June-2026`, and API deployment date `29-July-2026`. The ChEBI graph embeds
ontology version `254`, version IRI
`http://purl.obolibrary.org/obo/chebi/254/chebi_lite.owl`, and raw ontology date
`14:08:2026 10:24`.

Commands and observed results:

```text
$ shasum -a 256 data/raw/uniprotkb_cofactor_query_2026_02.json.gz data/raw/uniprotkb_cofactor_query_2026_02.headers.txt data/raw/chebi_lite_2026-08-14.json.gz
366b6d5924e8138f5e2e6af11bc9f638ef0b37e97123cb9f89d4a759c59612ea  data/raw/uniprotkb_cofactor_query_2026_02.json.gz
9baf74dc0186e7791e24d301da7075fd4bfc1bf2c9b564e1d600d37289dafd7e  data/raw/uniprotkb_cofactor_query_2026_02.headers.txt
d4102c38436128a0fc434dcd250e3d86cf6b36e9315e9c13444b5bf37ab47afe  data/raw/chebi_lite_2026-08-14.json.gz

$ python3 -m unittest tests.test_build.SourceManifestTests -v
test_manifest_verifies_compressed_and_decompressed_artifacts (...) ... ok
Ran 1 test in 0.290s
OK
```

## TDD record

The implementation began with synthetic fixtures and actual failing runs.

```text
$ python3 -m unittest tests.test_parser -v
exit_code=1
ImportError: Failed to import test module: test_parser
ModuleNotFoundError: No module named 'cofactor_bench.model'
Ran 1 test in 0.000s
FAILED (errors=1)

$ python3 -m unittest tests.test_build -v
exit_code=1
ImportError: Failed to import test module: test_build
ModuleNotFoundError: No module named 'cofactor_bench.build'
Ran 1 test in 0.000s
FAILED (errors=1)

$ python3 -m unittest tests.test_build.SyntheticBuildTests.test_build_from_config_resolves_paths_from_project_root -v
exit_code=1
ImportError: cannot import name 'build_from_config' from 'cofactor_bench.build'
Ran 1 test in 0.000s
FAILED (errors=1)

$ python3 -m unittest tests.test_build.SourceManifestTests -v
exit_code=1
ImportError: cannot import name 'verify_source_manifest' from 'cofactor_bench.build'
Ran 1 test in 0.000s
FAILED (errors=1)

$ python3 -m unittest tests.test_build.SyntheticBuildTests.test_build_from_config_resolves_paths_from_project_root -v
exit_code=1
AssertionError: ValueError not raised
Ran 1 test in 0.003s
FAILED (failures=1)
```

The last RED exposed that `expected_foundation` was recorded but not enforced.
The fix validates every configured invariant before atomically replacing either
JSONL output.

Focused GREEN results:

```text
$ python3 -m unittest tests.test_parser -v
Ran 4 tests in 0.001s
OK

$ python3 -m unittest tests.test_build.SyntheticBuildTests -v
Ran 2 tests in 0.004s
OK

$ python3 -m unittest tests.test_build.FrozenSnapshotBuildTests -v
Ran 1 test in 2.659s
OK

$ python3 -m unittest tests.test_build.SourceManifestTests -v
Ran 1 test in 0.290s
OK
```

The synthetic parser fixture proves that duplicate label occurrences remain
separate, each complete evidence list is retained, `Ref.N` resolves against
`references[].referenceNumber`, note evidence cannot promote a label, molecule
scope survives, and only the derived formula is deduplicated/canonicalized.

## Frozen build

Command:

```text
$ python3 -c 'import json; from cofactor_bench.build import build_from_config; print(json.dumps(build_from_config("config/benchmark.json").to_dict(), ensure_ascii=False, sort_keys=True, indent=2))'
exit_code=0
wall_time_seconds=3.48759125
```

Observed summary:

```json
{
  "source_record_count": 7008,
  "master_record_count": 5337,
  "unique_master_accession_count": 5337,
  "ineligible_record_count": 1671,
  "experimental_label_count": 104,
  "evidence_status_counts": {
    "ALL_DIRECT_PUBMED": 5210,
    "MIXED_DIRECT_AND_REFERENCE": 3,
    "ALL_REFERENCE_ONLY": 124
  },
  "experimental_label_cardinality_counts": {
    "1": 4179,
    "2": 838,
    "3": 208,
    "4": 75,
    "5": 31,
    "6": 3,
    "7": 2,
    "8": 1
  },
  "all_label_cardinality_counts": {
    "1": 3971,
    "2": 974,
    "3": 252,
    "4": 89,
    "5": 44,
    "6": 4,
    "7": 2,
    "8": 1
  },
  "formula_shape_counts": {
    "SINGLE": 4179,
    "PURE_OR": 647,
    "PURE_AND": 448,
    "MIXED_AND_OR": 63
  },
  "experimental_occurrence_count": 6981,
  "unique_accession_experimental_label_count": 6974,
  "duplicate_experimental_occurrence_count": 7,
  "canonical_block_count": 5904,
  "canonical_block_label_overlap_count": 0,
  "sequence_with_u_count": 11,
  "sequence_with_x_count": 8,
  "missing_sequence_count": 0,
  "sequence_length_mismatch_count": 0,
  "missing_experimental_chebi_id_count": 0
}
```

Source audit reason reconciliation:

```text
HAS_ACCEPTED_EXPERIMENTAL_COFACTOR       5337
NO_ACCEPTED_EXPERIMENTAL_COFACTOR        1671
```

Generated artifact verification:

```text
$ wc -l data/derived/master.jsonl data/derived/source_candidates.jsonl
    5337 data/derived/master.jsonl
    7008 data/derived/source_candidates.jsonl
   12345 total

$ shasum -a 256 data/derived/master.jsonl data/derived/source_candidates.jsonl
6305ff8cef0d0cdc52b5debe8ec99c12d0ed8377600dc1e6c46746cc21092a8c  data/derived/master.jsonl
ae5565fb93c2a6729cbd480b16fc372a047ca94a7072573f4a630a5741476314  data/derived/source_candidates.jsonl
```

A second config-driven frozen build produced the same two hashes.

## Final verification

```text
$ python3 -m json.tool config/benchmark.json >/dev/null
exit_code=0

$ python3 -m json.tool data/manifests/source.json >/dev/null
exit_code=0

$ python3 -m unittest discover -s tests -v
Ran 74 tests in 8.292s
OK
```

Code review covered correctness, readability, architecture, security and
performance. One required finding (`expected_foundation` was initially inert)
was fixed under a failing test. The final implementation uses only the Python
standard library, treats source/config JSON as untrusted at parsing boundaries,
contains no credentials, and builds the 211 MB decompressed UniProt snapshot in
about 3.5 seconds on this host. No git commit was made, as instructed.

## Tasks 4–6 continuation: derived views

Scope was limited to the new view builder/tests, generated view/audit artifacts,
this log, and `reports/data_audit.md`. Existing triage, ontology, duplicate,
prompt, runner, scoring, config, parser, model and CLI files were not edited.

### TDD record

The first synthetic fixture run was RED because the production module did not
exist:

```text
$ python3 -m unittest tests.test_views -v
exit_code=1
ModuleNotFoundError: No module named 'cofactor_bench.views'
Ran 1 test in 0.000s
FAILED (errors=1)
```

The synthetic fixtures cover additive reasons, high-recall note triage,
ontology ancestors, molecule scope, U/X handling, exact-sequence consistent and
conflicting groups, deterministic representative selection, explicit view
membership, numeric ChEBI sorting, ChEBI target node validity, UniProt display
name uniqueness and deterministic output. A second RED slice tightened the
atomicity boundary:

```text
$ python3 -m unittest \
    tests.test_views.SyntheticViewTests.test_coordinated_outputs_roll_back_if_replacement_fails \
    tests.test_views.SyntheticViewTests.test_rejects_frozen_input_output_aliases -v
test_coordinated_outputs_roll_back_if_replacement_fails (...) ... FAIL
test_rejects_frozen_input_output_aliases (...) ... FAIL
Ran 2 tests in 0.009s
FAILED (failures=2)
```

The GREEN implementation stages all eight outputs, backs up an existing output
set, rolls the whole set back if any replacement fails, and rejects aliases with
either frozen input before opening an output.

Freezing the ChEBI input at the view boundary also started RED:

```text
$ python3 -m unittest tests.test_views.SyntheticViewTests.test_build_writes_deterministic_atomic_artifacts -v
TypeError: build_views() got an unexpected keyword argument 'expected_input_hashes'
Ran 1 test in 0.008s
FAILED (errors=1)
```

`build_views_from_config` now requires and verifies both the compressed ChEBI
artifact hash and decompressed-content hash before deriving or replacing any
view.

```text
$ python3 -m unittest tests.test_views -v
Ran 6 tests in 5.657s
OK
```

### Frozen view reconciliation

The frozen Master and ChEBI graph reproduced:

| Checkpoint | Observed |
|---|---:|
| Full-Structured accessions | 5,337 |
| Single-Clean accessions | 3,971 |
| Single-Clean sequence entities | 3,942 |
| Single-Clean labels | 64 |
| Core-Provisional accessions | 3,232 (pre-independent-review build) |
| Ambiguity-Challenge accessions | 2,082 |
| Ontology ancestor pairs / overlap terms / ancestor targets / missing | 59 / 44 / 11 / 0 |
| Exact-sequence entities | 5,295 |
| Duplicate groups / entries | 39 / 81 |
| Conflict groups / entries | 6 / 12 |
| Label frequency bands (head / mid / tail) | 14 / 20 / 70 |

All 104 ChEBI targets have exactly one frozen release-254 node, a nonempty
preferred label, no deprecated flag, and no preferred-label collision across
target IDs. Catalog `name` is that ChEBI preferred
label; `uniprot_display_name` separately records the unique UniProt occurrence
name (104/104 unique). Marginal accession frequency and band remain audit
metadata and are not a model-input projection.

Core-Provisional contains no X, molecule scope, ancestor target, sequence
conflict, nonrepresentative exact-sequence row, or note-PENDING row. Seven Core
rows contain `U`, proving selenocysteine is retained when all other predicates
pass. No human exclusion decision is synthesized; regex findings only set note
triage to `PENDING`.

### Historical 541 diagnostic

The requested historical note queue count 541 is not reproducible from the
frozen inputs and current `triage_record` rules. It was not hard-coded or forced.
The exact audited projections are:

```text
single_clean=3971, all cofactor notes PENDING=477
experimental-single=4179, experimental-block notes PENDING=538
experimental-single=4179, all-block notes PENDING=560
experimental-single=4179, experimental-block note OR molecule scope PENDING=542
```

`data/derived/view_audit.json` retains 541 as
`HISTORICAL_NOTE_PENDING_COUNT_NOT_REPRODUCED`, with these diagnostic counts and
the instruction not to tune regexes merely to force the old number.

### Deterministic build and hashes

Command (run twice, with an equality assertion over every output hash):

```text
$ python3 -c 'from cofactor_bench.views import build_views_from_config; first=build_views_from_config("config/benchmark.json"); second=build_views_from_config("config/benchmark.json"); assert first.output_sha256 == second.output_sha256'
exit_code=0
```

Final artifact hashes:

```text
fd18a09b3e21bc16685e5994d2a2997216120692e1f25eee2db96ae83426e5b1  data/derived/full_structured.jsonl
b6b93c1af214670e319d2a1e9d8e642f5cc190e9e9b86785022b60f665b256ec  data/derived/single_clean.jsonl
8b1b397a783ca2d61e9fdedd9d8dbeacb2d458fd6a41cddb5cd948edc2ea1ed1  data/derived/core_provisional.jsonl
8e8f4905b5334386e71264ff95feee863b95a3387734315b3145b707eb361f92  data/derived/ambiguity_challenge.jsonl
ad326a715155960af1328538f2c441598a99a924d58e2cdadc52700692ba2fa3  data/derived/label_catalog.json
784b7ca5faa7c430402690144fe03d191758bd85e77995dc2e01b79533c80104  data/derived/ontology_audit.json
7ad61287ee8ae8bed6f652281afb8fc1130177504d1669b5a90c2a65ae30cf8f  data/derived/view_audit.json
2bf640b9bd1cd4a4c3c22c417864eae0005483e6e2c479d72c0fbbf11907e7c6  reports/data_audit.md
```

Final repository regression:

```text
$ python3 -m unittest discover -s tests -v
Ran 91 tests in 15.626s
OK
```

No warnings or failures were emitted. No git commit was made.

After that stable run, concurrent named-prompt work changed prompt/prediction
tests and implementation in separate files. A later full discovery therefore
reported three out-of-scope failures while all six view tests still passed:
one `CatalogTerm` construction-timing mismatch, one response-schema version
mismatch, and one runner escaped-child timeout attempt-count mismatch. The root
agent was given the exact failures; no out-of-scope file was changed here.

## Root integration correction after independent review

An independent read-only review found that the v1 Core representative was
chosen before accession-local quality filtering. In the real P62136/P62139
pair, that ordering discarded the entire otherwise eligible sequence entity.
Rule `cofactor9.1.views.v2` now chooses the lexicographically first accession
only among members that already pass all non-representative Core predicates.
The corrected Core-Provisional count is 3,233. The frozen validator now locks
both Core=3,233 and Challenge=2,082. All 12 records in the six exact-sequence
conflict groups are also explicitly `PENDING` adjudication.

The v2 hashes superseding the pre-review list above are recorded in
`data/derived/view_audit.json` and `reports/data_audit.md`; the pre-review hashes
remain here solely as an audit trail of the worker handoff.
