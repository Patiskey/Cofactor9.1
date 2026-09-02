# Authoritative sources

This project implements source-specific behavior only from the following
official documentation and freezes the retrieved data independently by hash.

## UniProt

- Evidence representation and `PubMed` / `Reference` sources:
  https://www.uniprot.org/help/evidences
- Evidence-code table:
  https://www.uniprot.org/help/evidence_table
- Cofactor block semantics, alternative cofactors and notes:
  https://www.uniprot.org/help/cofactor
- Frozen query endpoint:
  https://rest.uniprot.org/uniprotkb/search?query=reviewed%3Atrue%20AND%20ec%3A%2A%20AND%20fragment%3Afalse%20AND%20length%3A%5B50%20TO%201100%5D%20AND%20cc_cofactor_chebi_exp%3A%2A&format=json

## ChEBI

- Official downloads:
  https://www.ebi.ac.uk/chebi/downloads
- Ontology explanation:
  https://www.ebi.ac.uk/training/online/courses/chebi-quick-tour/the-chebi-ontology/
- Frozen ontology directory:
  https://ftp.ebi.ac.uk/pub/databases/chebi/ontology/

## Codex model transport

- GPT-5.6 Sol model and reasoning levels:
  https://developers.openai.com/api/docs/models/gpt-5.6-sol
- Fast/priority processing:
  https://developers.openai.com/api/docs/guides/fast-mode
- Codex non-interactive mode:
  https://learn.chatgpt.com/docs/non-interactive-mode
- Codex configuration reference:
  https://learn.chatgpt.com/docs/config-file/config-reference

The Codex CLI adapter is a constrained fallback for the current authenticated
environment. A direct Responses API request with no tools is scientifically
cleaner and remains the preferred transport when a separate API credential is
available. Transport changes may not alter prompt, cases or scoring.
