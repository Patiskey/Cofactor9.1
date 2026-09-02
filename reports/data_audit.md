# Cofactor9.1 data and view audit

- Dataset version: `Cofactor9.1`
- Rule version: `cofactor9.1.views.v3`
- Full-Structured accessions: 5337
- Single-Clean accessions: 3971
- Single-Clean exact-sequence entities: 3942
- Single-Clean labels: 64
- Core-Provisional accessions: 3233
- Ambiguity-Challenge accessions: 2082

## Gold formula checkpoints

- Preserved non-empty cofactor blocks: 5911
- Overlapping block pairs: 8
- Accessions with a label repeated across blocks: 6

## Ontology checkpoints

- Target ancestor pairs: 59
- Terms in overlap: 44
- Ancestor targets: 11
- Missing targets: 0

## Exact-sequence checkpoints

- Sequence entities: 5295
- Duplicate groups / entries: 39 / 81
- Conflict groups / entries: 6 / 12

## Label catalog checkpoints

- Labels: 104
- Frequency bands (head / mid / tail): 14 / 20 / 70
- ChEBI target nodes with exactly one active preferred label: 104 / 104
- Missing / duplicate / unnamed / deprecated ChEBI targets: 0 / 0 / 0 / 0
- Duplicate ChEBI preferred labels across target IDs: 0
- Targets with one unique UniProt display name: 104 / 104

## Core-Provisional predicate audit

- Retained sequences containing selenocysteine `U`: 7
- Predicate violations among retained rows: 0

## Conservative note triage

- Single-Clean, all cofactor notes, PENDING accessions: 477
- Experimental-single accessions: 4179
- Experimental-single, experimental-block notes, PENDING: 538
- Experimental-single, all-block notes, PENDING: 560
- Experimental-single, experimental-block note or molecule scope, PENDING: 542
- Historical count 541 is not reproduced by the frozen inputs and current rules; it is retained as an audit anomaly, not an acceptance invariant.

## Anomalies

- `HISTORICAL_NOTE_PENDING_COUNT_NOT_REPRODUCED`: historical=541, observed=477. Audit the historical counting rule; do not tune regexes or hard-code membership to force the count.

## Artifact SHA-256

- `ambiguity_challenge`: `e773520fe6df1a2c710dba2e890a8b2eb83398bb2ee593b2af6219745af05278`
- `core_provisional`: `bed3532d0e4ad332e125df90a02b2b6bba4239f342743367c547cf39545d56bd`
- `full_structured`: `57cf6b5c74de55306b9bbc623514aabdfed6919f291e92ec8ddcfc21599a44fa`
- `label_catalog`: `22f79b263ed3b027da2bcc8cc4c475a38a56393cb5dd6a6b7590b1ac979bc234`
- `ontology_audit`: `4cb9414d0e4a59168e2739378d66a2b2f9a9a2909650bf852124b713c27f41c3`
- `single_clean`: `195a59d1a1bd945bdbcdb9cc0045fdd0387716887737cfc15936944485d936d1`
- `view_audit`: `540f98cfc353c8929924362382e470e8537f73e742b20d17bd43ccf5a1c7f43a`

## Input SHA-256

- `chebi_artifact_sha256`: `d4102c38436128a0fc434dcd250e3d86cf6b36e9315e9c13444b5bf37ab47afe`
- `chebi_decompressed_content_sha256`: `d14526badabba6959c9d5facacb8a97d84b006347fae84cb5d7ca30a91ccd131`
- `master_sha256`: `6ff828f4d93175ea48bce228b2ee5921118ed358a69e9e8dcf071d82193d7f4f`
