# Cofactor9.1 project rules

## Purpose

Build and evaluate a reproducible protein-cofactor annotation benchmark from a
frozen UniProt release. The immutable source snapshot and audit trail are the
authority; conversation summaries are not.

## Stack and commands

- Python 3.14+, standard library first.
- Tests: `python3 -m unittest discover -s tests -v`
- Build: `python3 -m cofactor_bench.cli build --config config/benchmark.json`
- Validate: `python3 -m cofactor_bench.cli validate --config config/benchmark.json`
- Run: `python3 -m cofactor_bench.cli run --config config/benchmark.json`
- Score: `python3 -m cofactor_bench.cli score --config config/benchmark.json`

## Data invariants

- Never mutate a frozen raw snapshot.
- A cofactor is experimentally supported only when the cofactor object itself
  carries `ECO:0000269`; evidence on a note or entry is not label evidence.
- Accept a PubMed evidence source or a resolvable UniProt `Reference` source.
- Preserve UniProt cofactor blocks: labels within a block are alternatives
  (`OR`), while separate blocks are jointly annotated (`AND`).
- Preserve molecule/isoform/chain scope.
- Preserve original ChEBI IDs and ontology ancestry; never silently flatten a
  coarse term to a descendant.
- Keep every one of the 5,337 accessions in the master view. Filtering creates
  derived views and reason codes; it never silently deletes source records.
- The sequence-only model input must never contain accession, organism, EC,
  UniProt note, evidence identifier, PMID, or gold label.

## Engineering conventions

- Use typed dataclasses and pure functions for parsing and scoring.
- JSONL output is deterministic: UTF-8, stable key order, one record per line.
- Every behavior change starts with a failing `unittest`.
- Generated runs are resumable and append-only. Never overwrite a completed
  prediction without an explicit new run ID.
- Never print or persist API keys, cookies, tokens, or other secrets.
- Agents share the workspace. Do not revert or overwrite another agent's work;
  coordinate file ownership before editing.

## Boundaries

- Always: record release, query, hashes, commands, timestamps, model and prompt
  versions; run tests after each implementation slice.
- Ask first: destructive deletion, publishing, external messaging, or changing
  the scientific task definition.
- Never: expose gold metadata to the sequence-only runner, invent missing
  biological labels, treat `U` as illegal, or choose one side of a duplicate
  label conflict without an adjudication record.
