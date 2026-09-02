# Cofactor9.1 data and view audit

- Dataset version: `Cofactor9.1`
- Rule version: `cofactor9.1.views.v2`
- Full-Structured accessions: 5337
- Single-Clean accessions: 3971
- Single-Clean exact-sequence entities: 3942
- Single-Clean labels: 64
- Core-Provisional accessions: 3233
- Ambiguity-Challenge accessions: 2082

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

- `ambiguity_challenge`: `37e3976751c24c4b18854fcc5ce000a8f6d4d408f0ca5e76640ec011012fdc35`
- `core_provisional`: `a9e05ce403d654eabf0ac4ba7c0fdd5152957f322e9f2f4b88d91a4dbe9a0e19`
- `full_structured`: `ed27e0c5d3538656b06b3c21cde0bc9831e6917161006dd92f7737d0d8472824`
- `label_catalog`: `62a0418549a6811459de3f24972696d259bacd41e68bc7886a8a603dd77c01fd`
- `ontology_audit`: `e0abffc4137856d5ea00411a84cb3e0226c8a7a3ec7fa0ff9d000716ccd84526`
- `single_clean`: `2a4122637a60e40b681fd9694881855058c09f13f9189bf32210cc17894a69dc`
- `view_audit`: `fba8f40f6a8a3b30166632ae51d7ff051826a6d6f4dce14f770ff4c16b9695d7`

## Input SHA-256

- `chebi_artifact_sha256`: `d4102c38436128a0fc434dcd250e3d86cf6b36e9315e9c13444b5bf37ab47afe`
- `chebi_decompressed_content_sha256`: `d14526badabba6959c9d5facacb8a97d84b006347fae84cb5d7ca30a91ccd131`
- `master_sha256`: `6305ff8cef0d0cdc52b5debe8ec99c12d0ed8377600dc1e6c46746cc21092a8c`
