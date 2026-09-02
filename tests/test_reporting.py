from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
import unittest

from cofactor_bench.cases import (
    PRIVATE_MAPPING_PURPOSE,
    PRIVATE_MAPPING_SCHEMA_VERSION,
)
from cofactor_bench.prompt import PromptCase, render_prompt
from cofactor_bench.run import LedgerBundle, RunValidationSummary, VerifiedRunSnapshot
from cofactor_bench.reporting import (
    ReportingError,
    render_markdown,
    report_json_bytes,
    score_run,
)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _jsonl_bytes(values: list[dict[str, object]]) -> bytes:
    return b"".join(_json_bytes(value) for value in values)


def _catalog() -> dict[str, object]:
    labels = []
    for number in range(1, 105):
        count = 100 if number == 1 else 10 if number == 2 else 1
        band = "head" if count >= 100 else "mid" if count >= 10 else "tail"
        labels.append(
            {
                "chebi_id": f"CHEBI:{number}",
                "name": f"cofactor {number}",
                "uniprot_display_name": f"Cofactor {number}",
                "master_accession_count": count,
                "frequency_band": band,
            }
        )
    return {
        "schema_version": "cofactor9.1.label-catalog.v1",
        "dataset_version": "Cofactor9.1",
        "catalog_version": "cofactor9.1.allowed-labels.v1",
        "rule_version": "cofactor9.1.views.v3",
        "input_hashes": {"master_sha256": "0" * 64},
        "labels": labels,
        "summary": {
            "label_count": 104,
            "frequency_band_counts": {"head": 1, "mid": 1, "tail": 102},
        },
    }


def _sample_id(number: int) -> str:
    return f"sample_{number:032x}"


def _sequence_sha256(sequence: str) -> str:
    return hashlib.sha256(sequence.encode("ascii")).hexdigest()


def _public_case(number: int, sequence: str) -> dict[str, object]:
    catalog = _catalog()
    return {
        "sample_id": _sample_id(number),
        "sequence": sequence,
        "label_catalog": {
            "version": catalog["catalog_version"],
            "terms": [
                {"chebi_id": item["chebi_id"], "name": item["name"]}
                for item in catalog["labels"]
            ],
        },
    }


def _mapping(number: int, accession: str, sequence: str) -> dict[str, object]:
    return {
        "schema_version": PRIVATE_MAPPING_SCHEMA_VERSION,
        "visibility": "private",
        "purpose": PRIVATE_MAPPING_PURPOSE,
        "sample_id": _sample_id(number),
        "accession": accession,
        "sequence_sha256": _sequence_sha256(sequence),
    }


def _full_record(
    accession: str,
    sequence: str,
    blocks: list[list[str]],
    *,
    members: list[str] | None = None,
    conflict: bool = False,
    core: bool = False,
) -> dict[str, object]:
    sequence_hash = _sequence_sha256(sequence)
    group_members = members or [accession]
    labels_seen: set[str] = set()
    overlaps = False
    for block in blocks:
        overlaps = overlaps or bool(labels_seen.intersection(block))
        labels_seen.update(block)
    return {
        "schema_version": "cofactor9.1.view-record.v1",
        "dataset_version": "Cofactor9.1",
        "derivation": {
            "rule_version": "cofactor9.1.views.v3",
            "input_hashes": {"master_sha256": "0" * 64},
        },
        "entry": {"accession": accession},
        "sequence": {
            "value": sequence,
            "sha256": sequence_hash,
        },
        "derived": {
            "gold_formula": blocks,
            "reason_codes": (["OVERLAPPING_BLOCK_LABEL"] if overlaps else []),
            "experimental_label_ids": sorted(
                {label for block in blocks for label in block}
            ),
            "exact_sequence": {
                "sequence_entity_id": sequence_hash,
                "status": (
                    "DUPLICATE_CONFLICT"
                    if conflict
                    else "DUPLICATE_CONSISTENT"
                    if len(group_members) > 1
                    else "UNIQUE"
                ),
                "members": group_members,
            },
            "view_membership": {
                "full_structured": {"included": True},
                "core_provisional": {"included": core},
            },
        },
    }


def _terminal(
    case: dict[str, object],
    predicted: list[str] | None,
    *,
    primary: str | None = None,
    confidence: float = 0.9,
    error_code: str | None = None,
) -> dict[str, object]:
    prompt_case = PromptCase.from_payload(case)
    status = "terminal_error" if predicted is None else "success"
    prediction = None
    if predicted is not None:
        prediction = {
            "schema_version": "cofactor9.1.response.v2",
            "sample_id": case["sample_id"],
            "status": "predict" if confidence >= 0.5 else "abstain",
            "predicted_cofactors": predicted,
            "primary_guess": primary or predicted[0],
            "confidence_complete": confidence,
        }
    return {
        "schema_version": "cofactor9.1.terminal.v1",
        "sample_id": case["sample_id"],
        "status": status,
        "attempt_count": 1,
        "prediction": prediction,
        "error_code": error_code if predicted is None else None,
        "error_message": "audited model failure" if predicted is None else None,
        "completed_at": "2026-09-02T00:00:00Z",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "max",
        "service_tier": "fast",
        "prompt_version": "cofactor9.1.sequence-only.named-catalog.v2",
        "catalog_version": case["label_catalog"]["version"],
        "prompt_sha256": hashlib.sha256(
            render_prompt(prompt_case).encode("utf-8")
        ).hexdigest(),
    }


def _attempt(
    case: dict[str, object],
    number: int,
    *,
    duration: float,
    input_tokens: int,
    error_code: str | None = None,
) -> dict[str, object]:
    prompt_hash = hashlib.sha256(
        render_prompt(PromptCase.from_payload(case)).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "cofactor9.1.attempt.v1",
        "sample_id": case["sample_id"],
        "attempt_number": number,
        "started_at": "2026-09-02T00:00:00Z",
        "completed_at": "2026-09-02T00:00:01Z",
        "duration_seconds": duration,
        "argv": ["codex", "exec"],
        "model": "gpt-5.6-sol",
        "reasoning_effort": "max",
        "service_tier": "fast",
        "returncode": 0,
        "timed_out": False,
        "error_code": error_code,
        "error_message": "failure" if error_code else None,
        "retry_disposition": "retryable" if error_code else "none",
        "thread_id": None if error_code else f"thread-{number}",
        "usage": (
            {}
            if error_code
            else {
                "input_tokens": input_tokens,
                "cached_input_tokens": 0,
                "output_tokens": 2,
                "reasoning_output_tokens": 1,
            }
        ),
        "prompt_sha256": prompt_hash,
        "stdout_sha256": hashlib.sha256(b"").hexdigest(),
        "stderr_sha256": hashlib.sha256(b"").hexdigest(),
        "redaction_count": 0,
        "environment_policy": "fixed-allowlist",
    }


def _incident(
    case: dict[str, object],
    number: int,
    *,
    duration: float = 2.5,
    input_tokens: int = 0,
    error_code: str = "CAPACITY_ERROR",
) -> dict[str, object]:
    prompt_hash = hashlib.sha256(
        render_prompt(PromptCase.from_payload(case)).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "cofactor9.1.transport-incident.v1",
        "sample_id": case["sample_id"],
        "incident_number": number,
        "tentative_attempt_number": 1,
        "started_at": "2026-09-02T00:00:00Z",
        "completed_at": "2026-09-02T00:00:03Z",
        "duration_seconds": duration,
        "argv": ["codex", "exec"],
        "model": "gpt-5.6-sol",
        "reasoning_effort": "max",
        "service_tier": "fast",
        "returncode": 1,
        "timed_out": False,
        "cancelled": False,
        "start_error": None,
        "error_code": error_code,
        "error_message": "audited transport incident",
        "retry_disposition": "retryable",
        "thread_id": None,
        "usage": ({"input_tokens": input_tokens} if input_tokens else {}),
        "prompt_sha256": prompt_hash,
        "stdout_sha256": hashlib.sha256(b"").hexdigest(),
        "stderr_sha256": hashlib.sha256(b"").hexdigest(),
        "prediction_sha256": None,
        "redaction_count": 0,
        "environment_policy": "fixed-allowlist",
    }


def _cluster_id(accessions: list[str]) -> str:
    digest = hashlib.sha256()
    digest.update(b"cofactor9.1.homology-cluster-id.v1\0")
    for accession in sorted(accessions):
        digest.update(accession.encode("ascii"))
        digest.update(b"\0")
    return f"cluster_{digest.hexdigest()}"


def _cluster_rows(groups: list[list[str]]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for members in groups:
        cluster_id = _cluster_id(members)
        representative = min(members)
        for accession in members:
            rows.append(
                {
                    "schema_version": "cofactor9.1.homology-cluster-record.v1",
                    "accession": accession,
                    "cluster_id": cluster_id,
                    "representative_accession": representative,
                    "member_count": len(members),
                }
            )
    return sorted(rows, key=lambda row: str(row["accession"]))


def _framed_sha256(domain: str, frames: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    digest.update(domain.encode("ascii") + b"\0")
    for key, payload in sorted(frames):
        digest.update(key.encode("utf-8") + b"\0")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _bundle_frames(
    kind: str,
    bundles: dict[str, tuple[LedgerBundle, ...]],
) -> list[tuple[str, bytes]]:
    frames: list[tuple[str, bytes]] = []
    for sample_id, sample_bundles in bundles.items():
        for bundle in sample_bundles:
            prefix = f"{kind}/{sample_id}/{bundle.number:04d}"
            record_name = "attempt.json" if kind == "attempt" else "incident.json"
            frames.extend(
                [
                    (f"{prefix}/{record_name}", bundle.record_bytes),
                    (f"{prefix}/prompt.txt", bundle.prompt_bytes),
                    (f"{prefix}/stdout.jsonl", bundle.stdout_bytes),
                    (f"{prefix}/stderr.txt", bundle.stderr_bytes),
                ]
            )
            if bundle.prediction_bytes is not None:
                frames.append(
                    (f"{prefix}/prediction.json", bundle.prediction_bytes)
                )
    return frames


def _snapshot_ledger_sha256(
    terminals: dict[str, bytes],
    attempts: dict[str, tuple[LedgerBundle, ...]],
    incidents: dict[str, tuple[LedgerBundle, ...]],
) -> str:
    frames = [(f"terminal/{key}", value) for key, value in terminals.items()]
    frames.extend(_bundle_frames("attempt", attempts))
    frames.extend(_bundle_frames("incident", incidents))
    return _framed_sha256("cofactor9.1.ledger-composite.v1", frames)


def _snapshot_provenance_sha256(
    manifest_bytes: bytes,
    artifact_bytes: dict[str, bytes],
    implementation_sha256: dict[str, str],
    ledger_composite_sha256: str,
) -> str:
    frames = [("manifest/manifest.json", manifest_bytes)]
    frames.extend(
        (f"artifact/{key}", value) for key, value in artifact_bytes.items()
    )
    frames.extend(
        (f"implementation/{key}.sha256", value.encode("ascii"))
        for key, value in implementation_sha256.items()
    )
    frames.append(
        ("ledger/composite.sha256", ledger_composite_sha256.encode("ascii"))
    )
    return _framed_sha256("cofactor9.1.scoring-provenance.v1", frames)


class ReportingFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.sequences = ["MAAA", "MAAA", "MCCC", "MCCC", "MGGG"]
        self.accessions = ["A00001", "A00002", "A00003", "A00004", "A00005"]
        self.public_cases = [
            _public_case(index, sequence)
            for index, sequence in enumerate(self.sequences, start=1)
        ]
        self.private_mappings = [
            _mapping(index, accession, sequence)
            for index, (accession, sequence) in enumerate(
                zip(self.accessions, self.sequences, strict=True), start=1
            )
        ]
        duplicate_members = ["A00001", "A00002"]
        conflict_members = ["A00003", "A00004"]
        self.full_records = [
            _full_record(
                "A00001",
                "MAAA",
                [["CHEBI:1"]],
                members=duplicate_members,
                core=True,
            ),
            _full_record(
                "A00002",
                "MAAA",
                [["CHEBI:1"]],
                members=duplicate_members,
            ),
            _full_record(
                "A00003",
                "MCCC",
                [["CHEBI:2"]],
                members=conflict_members,
                conflict=True,
            ),
            _full_record(
                "A00004",
                "MCCC",
                [["CHEBI:3"]],
                members=conflict_members,
                conflict=True,
            ),
            _full_record("A00005", "MGGG", [["CHEBI:2", "CHEBI:3"]]),
        ]
        predictions = [
            (["CHEBI:1"], 0.9),
            (["CHEBI:2"], 0.8),
            (["CHEBI:2"], 0.8),
            (["CHEBI:3"], 0.4),
            (["CHEBI:3"], 0.7),
        ]
        self.terminals = {
            case["sample_id"]: _json_bytes(_terminal(case, labels, confidence=confidence))
            for case, (labels, confidence) in zip(
                self.public_cases, predictions, strict=True
            )
        }
        self.attempts = {
            case["sample_id"]: (
                _json_bytes(
                    _attempt(
                        case,
                        1,
                        duration=float(index),
                        input_tokens=index * 10,
                    )
                ),
            )
            for index, case in enumerate(self.public_cases, start=1)
        }
        self.clusters = _cluster_rows(
            [["A00001", "A00002", "A00005"], ["A00003", "A00004"]]
        )
        self.incidents: dict[str, tuple[bytes, ...]] = {}

    def _artifact_bytes(
        self,
        *,
        mappings: list[dict[str, object]],
        clusters: list[dict[str, object]],
        public_cases: list[dict[str, object]] | None = None,
        full_records: list[dict[str, object]] | None = None,
        ontology_pairs: list[dict[str, object]] | None = None,
    ) -> dict[str, bytes]:
        cases = public_cases or self.public_cases
        full = full_records or self.full_records
        ontology = {
            "schema_version": "cofactor9.1.ontology-audit.v1",
            "dataset_version": "Cofactor9.1",
            "rule_version": "cofactor9.1.views.v3",
            "target_label_count": 104,
            "pairs": (
                ontology_pairs
                if ontology_pairs is not None
                else [
                    {"specific": "CHEBI:2", "ancestor": "CHEBI:1", "distance": 1},
                    {"specific": "CHEBI:3", "ancestor": "CHEBI:2", "distance": 1},
                ]
            ),
        }
        public_bytes = _jsonl_bytes(cases)
        private_bytes = _jsonl_bytes(mappings)
        full_bytes = _jsonl_bytes(full)
        core_bytes = _jsonl_bytes(
            [record for record in full if record["derived"]["view_membership"]["core_provisional"]["included"]]
        )
        catalog_bytes = _json_bytes(_catalog())
        ontology_bytes = _json_bytes(ontology)
        cluster_bytes = _jsonl_bytes(clusters)
        case_manifest = {
            "schema_version": "cofactor9.1.case-artifacts.v1",
            "dataset_version": "Cofactor9.1",
            "view_rule_version": "cofactor9.1.views.v3",
            "prompt_version": "cofactor9.1.sequence-only.named-catalog.v2",
            "catalog_version": "cofactor9.1.allowed-labels.v1",
            "public_case_schema_version": "cofactor9.1.prompt-cases.v1",
            "private_mapping": {
                "schema_version": PRIVATE_MAPPING_SCHEMA_VERSION,
                "visibility": "private",
                "file_mode": "0600",
                "purpose": PRIVATE_MAPPING_PURPOSE,
            },
            "input_sha256": {
                "full_structured": hashlib.sha256(full_bytes).hexdigest(),
                "label_catalog": hashlib.sha256(catalog_bytes).hexdigest(),
            },
            "counts": {
                "prompt_cases": len(cases),
                "private_mappings": len(mappings),
                "unique_sample_ids": len(cases),
                "unique_accessions": len(mappings),
                "catalog_terms": 104,
            },
            "output_sha256": {
                "prompt_cases": hashlib.sha256(public_bytes).hexdigest(),
                "private_mapping": hashlib.sha256(private_bytes).hexdigest(),
            },
        }
        view_audit = {
            "schema_version": "cofactor9.1.view-audit.v1",
            "dataset_version": "Cofactor9.1",
            "rule_version": "cofactor9.1.views.v3",
            "output_artifact_sha256": {
                "full_structured": hashlib.sha256(full_bytes).hexdigest(),
                "core_provisional": hashlib.sha256(core_bytes).hexdigest(),
                "label_catalog": hashlib.sha256(catalog_bytes).hexdigest(),
                "ontology_audit": hashlib.sha256(ontology_bytes).hexdigest(),
            },
            "summary": {
                "full_structured_accessions": len(full),
                "core_provisional_accessions": sum(
                    record["derived"]["view_membership"]["core_provisional"]["included"]
                    for record in full
                ),
            },
        }
        cluster_manifest = {
            "schema_version": "cofactor9.1.homology-cluster-manifest.v1",
            "dataset_version": "Cofactor9.1",
            "view_rule_version": "cofactor9.1.views.v3",
            "cluster_record_schema_version": "cofactor9.1.homology-cluster-record.v1",
            "algorithm": {
                "name": "DIAMOND cluster",
                "identity_percent": 90,
                "mutual_coverage_percent": 80,
                "cluster_id_version": "cofactor9.1.homology-cluster-id.v1",
            },
            "input_sha256": {
                "full_structured": hashlib.sha256(full_bytes).hexdigest(),
            },
            "output_sha256": {
                "homology_clusters": hashlib.sha256(cluster_bytes).hexdigest(),
            },
            "counts": {
                "input_records": len(full),
                "output_records": len(clusters),
                "unique_accessions": len(clusters),
                "cluster_count": len({row["cluster_id"] for row in clusters}),
            },
        }
        return {
            "public_cases": public_bytes,
            "case_manifest": _json_bytes(case_manifest),
            "private_mapping": private_bytes,
            "full_structured": full_bytes,
            "core_provisional": core_bytes,
            "label_catalog": catalog_bytes,
            "ontology_audit": ontology_bytes,
            "view_audit": _json_bytes(view_audit),
            "homology_clusters": cluster_bytes,
            "homology_clusters_manifest": _json_bytes(cluster_manifest),
        }

    @staticmethod
    def _ledger_bundles(
        records: dict[str, tuple[bytes, ...]],
        cases: list[dict[str, object]],
        terminals: dict[str, bytes],
    ) -> dict[str, tuple[LedgerBundle, ...]]:
        cases_by_sample = {str(case["sample_id"]): case for case in cases}
        result: dict[str, tuple[LedgerBundle, ...]] = {}
        for sample_id, values in records.items():
            case = cases_by_sample[sample_id]
            prompt_bytes = render_prompt(PromptCase.from_payload(case)).encode("utf-8")
            terminal = json.loads(terminals[sample_id]) if sample_id in terminals else None
            bundles = []
            for number, record_bytes in enumerate(values, start=1):
                prediction_bytes = None
                if terminal is not None and number == len(values) and terminal["prediction"] is not None:
                    prediction_bytes = _json_bytes(terminal["prediction"])
                bundles.append(
                    LedgerBundle(
                        number=number,
                        record_bytes=record_bytes,
                        prompt_bytes=prompt_bytes,
                        stdout_bytes=b"",
                        stderr_bytes=b"",
                        prediction_bytes=prediction_bytes,
                    )
                )
            result[sample_id] = tuple(bundles)
        return result

    @staticmethod
    def _incident_bundles(
        records: dict[str, tuple[bytes, ...]],
        cases: list[dict[str, object]],
    ) -> dict[str, tuple[LedgerBundle, ...]]:
        cases_by_sample = {str(case["sample_id"]): case for case in cases}
        result: dict[str, tuple[LedgerBundle, ...]] = {}
        for sample_id, values in records.items():
            prompt_bytes = render_prompt(
                PromptCase.from_payload(cases_by_sample[sample_id])
            ).encode("utf-8")
            result[sample_id] = tuple(
                LedgerBundle(
                    number=number,
                    record_bytes=record_bytes,
                    prompt_bytes=prompt_bytes,
                    stdout_bytes=b"",
                    stderr_bytes=b"",
                )
                for number, record_bytes in enumerate(values, start=1)
            )
        return result

    def snapshot(
        self,
        *,
        run_id: str = "fixture-run",
        terminal_records: dict[str, bytes] | None = None,
        attempt_records: dict[str, tuple[bytes, ...]] | None = None,
        incident_records: dict[str, tuple[bytes, ...]] | None = None,
        mappings: list[dict[str, object]] | None = None,
        clusters: list[dict[str, object]] | None = None,
        public_cases: list[dict[str, object]] | None = None,
        full_records: list[dict[str, object]] | None = None,
        ontology_pairs: list[dict[str, object]] | None = None,
        artifact_bytes: dict[str, bytes] | None = None,
    ) -> VerifiedRunSnapshot:
        cases = public_cases or self.public_cases
        terminals = self.terminals if terminal_records is None else terminal_records
        attempts = self.attempts if attempt_records is None else attempt_records
        incidents = self.incidents if incident_records is None else incident_records
        artifacts = (
            dict(artifact_bytes)
            if artifact_bytes is not None
            else self._artifact_bytes(
                mappings=mappings or self.private_mappings,
                clusters=clusters or self.clusters,
                public_cases=cases,
                full_records=full_records,
                ontology_pairs=ontology_pairs,
            )
        )
        artifact_hashes = {
            name: hashlib.sha256(payload).hexdigest()
            for name, payload in artifacts.items()
        }
        implementation_hashes = {
            "reporting": "a" * 64,
            "scoring": "b" * 64,
            "metrics": "c" * 64,
            "prediction": "d" * 64,
        }
        evaluation_artifacts = {
            name: {"sha256": sha256}
            for name, sha256 in artifact_hashes.items()
        }
        evaluation_artifacts["private_mapping"]["visibility"] = "private-hash-only"
        contract = {
            "cases": {
                "sha256": artifact_hashes["public_cases"],
                "manifest_sha256": artifact_hashes["case_manifest"],
                "manifest_schema_version": "cofactor9.1.case-artifacts.v1",
                "catalog_version": "cofactor9.1.allowed-labels.v1",
                "total_count": len(cases),
                "expected_formal_count": len(cases),
                "selected_count": len(cases),
            },
            "model": {
                "name": "gpt-5.6-sol",
                "reasoning_effort": "max",
                "service_tier": "fast",
                "prompt_version": "cofactor9.1.sequence-only.named-catalog.v2",
                "response_schema_version": "cofactor9.1.response.v2",
            },
            "codex_binary": {
                "requested": "codex",
                "resolved_path": "/fixture/codex",
                "sha256": "e" * 64,
                "version": "codex-cli 0.152.0",
            },
            "transport": {
                "kind": "codex_cli_chatgpt_oauth",
                "max_attempts": 3,
                "timeout_seconds": 600.0,
                "circuit_breaker_threshold": 1,
            },
            "evaluation_versions": {
                "dataset_version": "Cofactor9.1",
                "view_rule_version": "cofactor9.1.views.v3",
                "formula_rule_version": "cofactor9.1.formula.v2",
                "prompt_version": "cofactor9.1.sequence-only.named-catalog.v2",
                "catalog_version": "cofactor9.1.allowed-labels.v1",
                "response_schema_version": "cofactor9.1.response.v2",
            },
            "evaluation_implementations": {
                name: {"sha256": sha256}
                for name, sha256 in implementation_hashes.items()
            },
            "evaluation_artifacts": evaluation_artifacts,
        }
        manifest = {
            "schema_version": "cofactor9.1.run-manifest.v1",
            "run_id": run_id,
            "dataset_version": "Cofactor9.1",
            "mode": "formal",
            "created_at": "2026-09-02T00:00:00Z",
            "contract_sha256": hashlib.sha256(_json_bytes(contract)).hexdigest(),
            "contract": contract,
        }
        manifest_bytes = _json_bytes(manifest)
        attempt_bundles = self._ledger_bundles(attempts, cases, terminals)
        incident_bundles = self._incident_bundles(incidents, cases)
        ledger_hash = _snapshot_ledger_sha256(
            terminals, attempt_bundles, incident_bundles
        )
        validation = RunValidationSummary(
            run_id=run_id,
            mode="formal",
            selected_case_count=len(cases),
            terminal_count=len(terminals),
            success_count=sum(
                json.loads(payload)["status"] == "success"
                for payload in terminals.values()
            ),
            terminal_error_count=sum(
                json.loads(payload)["status"] == "terminal_error"
                for payload in terminals.values()
            ),
            missing_terminal_count=len(cases) - len(terminals),
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            attempt_count=sum(len(values) for values in attempts.values()),
            incident_count=sum(len(values) for values in incidents.values()),
            incident_composite_sha256=_framed_sha256(
                "cofactor9.1.incident-composite.v1",
                _bundle_frames("incident", incident_bundles),
            ),
            ledger_composite_sha256=ledger_hash,
        )
        return VerifiedRunSnapshot(
            run_id=run_id,
            run_dir=Path("/fixture/run"),
            validation=validation,
            manifest_bytes=manifest_bytes,
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            evaluation_implementation_sha256=MappingProxyType(
                implementation_hashes
            ),
            artifact_bytes=MappingProxyType(artifacts),
            artifact_sha256=MappingProxyType(artifact_hashes),
            terminal_records=MappingProxyType(terminals),
            attempt_bundles=MappingProxyType(attempt_bundles),
            incident_bundles=MappingProxyType(incident_bundles),
            invocation_bundles=(),
            ledger_composite_sha256=ledger_hash,
            provenance_composite_sha256=_snapshot_provenance_sha256(
                manifest_bytes, artifacts, implementation_hashes, ledger_hash
            ),
        )

    def score(
        self,
        *,
        formal: bool = True,
        terminal_records: dict[str, bytes] | None = None,
        attempt_records: dict[str, tuple[bytes, ...]] | None = None,
        mappings: list[dict[str, object]] | None = None,
        clusters: list[dict[str, object]] | None = None,
    ):
        return score_run(
            verified_run_snapshot=self.snapshot(
                terminal_records=terminal_records,
                attempt_records=attempt_records,
                mappings=mappings,
                clusters=clusters,
            ),
            formal=formal,
            expected_case_count=5,
            calibration_bin_count=2,
        )


class ScoreRunTests(ReportingFixture):
    def test_scores_primary_raw_conflict_cluster_core_and_resources(self) -> None:
        report = self.score()
        value = report.to_dict()

        self.assertEqual(value["schema_version"], "cofactor9.1.results.v1")
        self.assertFalse(value["diagnostic_only"])
        self.assertEqual(value["headline"]["weighting"], "exact_sequence_entity_macro")
        self.assertAlmostEqual(value["headline"]["record_exact_accuracy"], 0.75)
        self.assertAlmostEqual(
            value["full_structured"]["accession_weighted"][
                "record_exact_accuracy"
            ],
            2.0 / 3.0,
        )
        self.assertEqual(
            value["full_structured"]["accession_weighted"]["effective_weight"],
            3.0,
        )
        self.assertEqual(
            value["full_structured"]["ranking_exclusions"],
            {
                "zero_weight_accession_count": 2,
                "ranking_eligible_accession_count": 3,
                "exact_sequence_conflict_accession_count": 2,
                "overlapping_block_label_accession_count": 0,
                "intersection_accession_count": 0,
                "ranking_eligible_exact_sequence_entity_count": 2,
                "ranking_eligible_homology_cluster_count": 1,
            },
        )
        self.assertAlmostEqual(value["headline"]["block_micro_f1"], 0.75)
        self.assertIn("hierarchy_block_micro_f1", value["headline"])
        self.assertGreater(
            value["headline"]["hierarchy_block_micro_f1"],
            value["headline"]["block_micro_f1"],
        )
        self.assertEqual(
            value["full_structured"]["label_macro"]["label_count"], 104
        )
        self.assertIn(
            "macro_f1", value["full_structured"]["label_macro"]
        )
        self.assertEqual(
            value["full_structured"]["label_macro_by_frequency_band"]["head"][
                "label_count"
            ],
            1,
        )
        self.assertEqual(value["full_structured"]["primary"]["entity_count"], 2)
        self.assertEqual(value["conflict_slice"]["group_count"], 1)
        self.assertEqual(value["conflict_slice"]["accession_count"], 2)
        self.assertEqual(value["conflict_slice"]["primary_effective_weight"], 0.0)
        self.assertEqual(value["conflict_slice"]["metrics"]["record_exact_accuracy"], 1.0)
        self.assertEqual(value["homology_cluster_macro"]["cluster_count"], 2)
        self.assertEqual(
            value["homology_cluster_macro"]["effective_primary_cluster_count"], 1
        )
        self.assertAlmostEqual(
            value["homology_cluster_macro"]["metrics"]["record_exact_accuracy"],
            0.75,
        )
        self.assertEqual(value["core_single"]["record_count"], 1)
        self.assertEqual(value["core_single"]["accuracy"], 1.0)
        self.assertEqual(value["core_single"]["by_frequency_band"]["head"]["accuracy"], 1.0)
        self.assertEqual(value["terminal_ledger"]["success_count"], 5)
        self.assertEqual(value["terminal_ledger"]["model_abstention_count"], 1)
        self.assertEqual(value["resources"]["all_attempts"]["attempt_count"], 5)
        self.assertEqual(value["resources"]["all_attempts"]["total_duration_seconds"], 15.0)
        self.assertEqual(
            value["resources"]["all_attempts"]["usage_totals"]["input_tokens"],
            150,
        )
        self.assertAlmostEqual(value["calibration"]["primary"]["brier_score"], 0.2075)
        self.assertIn("head", value["full_structured"]["by_gold_frequency_band"])
        self.assertIn("tail", value["full_structured"]["by_gold_frequency_band"])

    def test_output_bytes_and_markdown_are_stable_and_labeled(self) -> None:
        report = self.score()
        first = report_json_bytes(report)
        second = report_json_bytes(report)
        self.assertEqual(first, second)
        self.assertEqual(json.loads(first), report.to_dict())
        markdown = render_markdown(report)
        self.assertIn("# Cofactor9.1 result report", markdown)
        self.assertIn("Full-Structured primary", markdown)
        self.assertIn("Conflict slice", markdown)
        self.assertNotIn("Diagnostic-only partial run", markdown)
        self.assertIn("Verified provenance", markdown)
        self.assertIn("Transport incidents", markdown)

    def test_provenance_binds_manifest_artifacts_implementations_and_ledgers(self) -> None:
        snapshot = self.snapshot()
        value = score_run(
            verified_run_snapshot=snapshot,
            formal=True,
            expected_case_count=5,
        ).to_dict()

        provenance = value["provenance"]
        self.assertEqual(provenance["run_manifest"]["sha256"], snapshot.manifest_sha256)
        self.assertEqual(
            provenance["run_manifest"]["schema_version"],
            "cofactor9.1.run-manifest.v1",
        )
        self.assertEqual(provenance["dataset_version"], "Cofactor9.1")
        self.assertEqual(provenance["view_rule_version"], "cofactor9.1.views.v3")
        self.assertEqual(
            provenance["formula_rule_version"], "cofactor9.1.formula.v2"
        )
        self.assertEqual(
            provenance["scoring_implementation_sha256"], "a" * 64
        )
        self.assertEqual(
            provenance["evaluation_artifact_sha256"],
            dict(snapshot.artifact_sha256),
        )
        self.assertEqual(
            provenance["ledger_composite_sha256"],
            snapshot.ledger_composite_sha256,
        )
        self.assertEqual(
            provenance["provenance_composite_sha256"],
            snapshot.provenance_composite_sha256,
        )
        self.assertEqual(provenance["model"]["binary_sha256"], "e" * 64)

    def test_transport_incidents_are_a_separate_non_attempt_resource(self) -> None:
        sample_id = str(self.public_cases[0]["sample_id"])
        incident_records = {
            sample_id: (
                _json_bytes(
                    _incident(
                        self.public_cases[0],
                        1,
                        duration=2.5,
                    )
                ),
            )
        }
        value = score_run(
            verified_run_snapshot=self.snapshot(incident_records=incident_records),
            formal=True,
            expected_case_count=5,
        ).to_dict()

        self.assertEqual(value["resources"]["model_attempts"]["attempt_count"], 5)
        incidents = value["resources"]["transport_incidents"]
        self.assertEqual(incidents["incident_count"], 1)
        self.assertEqual(incidents["affected_sample_count"], 1)
        self.assertEqual(incidents["affected_sample_ids"], [sample_id])
        self.assertEqual(incidents["error_code_counts"], {"CAPACITY_ERROR": 1})
        self.assertEqual(incidents["total_duration_seconds"], 2.5)
        self.assertEqual(incidents["usage_totals"], {})
        self.assertTrue(incidents["excluded_from_model_attempt_budget"])
        denominators = value["outcome_denominators"]
        self.assertEqual(denominators["expected_accession_count"], 5)
        self.assertEqual(denominators["ranking_eligible_accession_count"], 3)
        self.assertEqual(denominators["success_count"], 5)
        self.assertEqual(denominators["terminal_error_count"], 0)
        self.assertEqual(denominators["pending_count"], 0)
        self.assertEqual(denominators["incident_affected_sample_count"], 1)

    def test_transport_incident_process_fields_are_semantically_closed(self) -> None:
        sample_id = str(self.public_cases[0]["sample_id"])
        baseline = _incident(self.public_cases[0], 1)
        mutations = {
            "capacity_marked_cancelled": {"cancelled": True},
            "capacity_with_start_error": {"start_error": "forged"},
            "capacity_marked_timed_out": {"timed_out": True},
            "capacity_with_usage": {"usage": {"input_tokens": 7}},
        }
        for name, change in mutations.items():
            with self.subTest(name=name):
                record = {**baseline, **change}
                incident_records = {
                    sample_id: (_json_bytes(record),)
                }
                with self.assertRaises(ReportingError):
                    score_run(
                        verified_run_snapshot=self.snapshot(
                            incident_records=incident_records
                        ),
                        formal=True,
                        expected_case_count=5,
                    )

    def test_overlapping_blocks_have_zero_weight_in_every_ranking(self) -> None:
        baseline = self.score().to_dict()
        overlap_case = _public_case(6, "MTTT")
        overlap_accession = "A00006"
        public_cases = [*self.public_cases, overlap_case]
        mappings = [
            *self.private_mappings,
            _mapping(6, overlap_accession, "MTTT"),
        ]
        full_records = [
            *self.full_records,
            _full_record(
                overlap_accession,
                "MTTT",
                [["CHEBI:2"], ["CHEBI:2"]],
            ),
        ]
        terminals = {
            **self.terminals,
            overlap_case["sample_id"]: _json_bytes(
                _terminal(overlap_case, ["CHEBI:1"], confidence=0.99)
            ),
        }
        attempts = {
            **self.attempts,
            overlap_case["sample_id"]: (
                _json_bytes(
                    _attempt(
                        overlap_case,
                        1,
                        duration=6.0,
                        input_tokens=60,
                    )
                ),
            ),
        }
        clusters = _cluster_rows(
            [
                ["A00001", "A00002", "A00005"],
                ["A00003", "A00004"],
                [overlap_accession],
            ]
        )
        report = score_run(
            verified_run_snapshot=self.snapshot(
                run_id="overlap-fixture",
                terminal_records=terminals,
                attempt_records=attempts,
                mappings=mappings,
                clusters=clusters,
                public_cases=public_cases,
                full_records=full_records,
            ),
            formal=True,
            expected_case_count=6,
            calibration_bin_count=2,
        ).to_dict()

        overlap = report["unrepresentable_overlap_slice"]
        self.assertEqual(overlap["accession_count"], 1)
        self.assertEqual(overlap["overlapping_block_pair_count"], 1)
        self.assertEqual(overlap["ranking_effective_weight"], 0.0)
        self.assertEqual(
            overlap["records"],
            [
                {
                    "accession": overlap_accession,
                    "sample_id": overlap_case["sample_id"],
                    "gold_formula": [["CHEBI:2"], ["CHEBI:2"]],
                    "terminal_status": "success",
                    "predicted_cofactors": ["CHEBI:1"],
                    "primary_guess": "CHEBI:1",
                    "ranking_weight": 0.0,
                    "reason_code": "OVERLAPPING_BLOCK_LABEL",
                }
            ],
        )
        for section in ("primary", "accession_weighted"):
            self.assertEqual(
                report["full_structured"][section]["effective_weight"],
                baseline["full_structured"][section]["effective_weight"],
            )
            self.assertEqual(
                report["full_structured"][section]["record_exact_accuracy"],
                baseline["full_structured"][section]["record_exact_accuracy"],
            )
            self.assertEqual(
                report["full_structured"][section][
                    "hierarchy_under_specific_count"
                ],
                baseline["full_structured"][section][
                    "hierarchy_under_specific_count"
                ],
            )
        self.assertEqual(
            report["full_structured"]["label_macro"],
            baseline["full_structured"]["label_macro"],
        )
        self.assertEqual(
            report["homology_cluster_macro"]["effective_primary_cluster_count"],
            baseline["homology_cluster_macro"]["effective_primary_cluster_count"],
        )
        self.assertEqual(
            report["homology_cluster_macro"]["metrics"]["record_exact_accuracy"],
            baseline["homology_cluster_macro"]["metrics"]["record_exact_accuracy"],
        )
        for section in ("primary", "accession_diagnostic"):
            self.assertEqual(
                report["calibration"][section]["total_weight"],
                baseline["calibration"][section]["total_weight"],
            )
            self.assertEqual(
                report["calibration"][section]["brier_score"],
                baseline["calibration"][section]["brier_score"],
            )
            self.assertEqual(
                report["calibration"][section][
                    "zero_weight_overlap_prediction_count"
                ],
                1,
            )

    def test_partial_mode_is_explicitly_diagnostic(self) -> None:
        partial = dict(list(self.terminals.items())[:2])
        partial_attempts = {
            sample_id: self.attempts[sample_id] for sample_id in partial
        }
        report = score_run(
            verified_run_snapshot=self.snapshot(
                run_id="partial-fixture",
                terminal_records=partial,
                attempt_records=partial_attempts,
                ontology_pairs=[],
            ),
            formal=False,
            expected_case_count=5,
        )
        value = report.to_dict()
        self.assertTrue(value["diagnostic_only"])
        self.assertEqual(value["terminal_ledger"]["missing_terminal_count"], 3)
        self.assertIn("Diagnostic-only partial run", render_markdown(report))

    def test_formal_mode_requires_every_terminal_and_attempt(self) -> None:
        partial = dict(list(self.terminals.items())[:4])
        with self.assertRaisesRegex(ReportingError, "Formal run requires"):
            self.score(terminal_records=partial)

        attempts = dict(self.attempts)
        attempts.pop(self.public_cases[0]["sample_id"])
        with self.assertRaisesRegex(ReportingError, "attempt ledger"):
            score_run(
                verified_run_snapshot=self.snapshot(
                    terminal_records=self.terminals,
                    attempt_records=attempts,
                    ontology_pairs=[],
                ),
                formal=True,
                expected_case_count=5,
            )

    def test_terminal_errors_are_counted_not_dropped(self) -> None:
        terminals = dict(self.terminals)
        last_case = self.public_cases[-1]
        terminals[last_case["sample_id"]] = _json_bytes(
            _terminal(last_case, None, error_code="INVALID_PREDICTION")
        )
        attempts = dict(self.attempts)
        attempts[last_case["sample_id"]] = (
            _json_bytes(
                _attempt(
                    last_case,
                    1,
                    duration=5.0,
                    input_tokens=0,
                    error_code="INVALID_PREDICTION",
                )
            ),
        )
        report = score_run(
            verified_run_snapshot=self.snapshot(
                run_id="error-fixture",
                terminal_records=terminals,
                attempt_records=attempts,
                ontology_pairs=[],
            ),
            formal=True,
            expected_case_count=5,
        ).to_dict()
        self.assertEqual(report["terminal_ledger"]["terminal_error_count"], 1)
        self.assertEqual(report["terminal_ledger"]["prediction_parse_failure_count"], 1)
        self.assertEqual(report["terminal_ledger"]["prediction_parse_failure_rate"], 0.2)
        self.assertEqual(report["full_structured"]["accession_weighted"]["record_count"], 5)
        self.assertEqual(report["calibration"]["primary"]["excluded_terminal_error_count"], 1)

    def test_core_terminal_error_stays_in_accuracy_denominator(self) -> None:
        terminals = dict(self.terminals)
        first_case = self.public_cases[0]
        terminals[first_case["sample_id"]] = _json_bytes(
            _terminal(first_case, None, error_code="INVALID_PREDICTION")
        )
        attempts = dict(self.attempts)
        attempts[first_case["sample_id"]] = (
            _json_bytes(
                _attempt(
                    first_case,
                    1,
                    duration=1.0,
                    input_tokens=0,
                    error_code="INVALID_PREDICTION",
                )
            ),
        )

        report = self.score(
            terminal_records=terminals,
            attempt_records=attempts,
        ).to_dict()

        self.assertEqual(report["core_single"]["record_count"], 1)
        self.assertEqual(report["core_single"]["terminal_error_count"], 1)
        self.assertEqual(report["core_single"]["accuracy"], 0.0)
        self.assertEqual(report["core_single"]["macro_f1"], 0.0)


class ReportingValidationTests(ReportingFixture):
    def test_verified_snapshot_fails_closed_on_manifest_artifact_and_ledger_drift(self) -> None:
        snapshot = self.snapshot()
        with self.assertRaisesRegex(ReportingError, "manifest SHA256"):
            score_run(
                verified_run_snapshot=replace(
                    snapshot, manifest_sha256="f" * 64
                ),
                expected_case_count=5,
            )

        changed_artifacts = dict(snapshot.artifact_bytes)
        changed_artifacts["private_mapping"] += b"\n"
        with self.assertRaisesRegex(ReportingError, "private_mapping.*SHA256"):
            score_run(
                verified_run_snapshot=replace(
                    snapshot,
                    artifact_bytes=MappingProxyType(changed_artifacts),
                ),
                expected_case_count=5,
            )

        with self.assertRaisesRegex(ReportingError, "ledger composite SHA256"):
            score_run(
                verified_run_snapshot=replace(
                    snapshot, ledger_composite_sha256="0" * 64
                ),
                expected_case_count=5,
            )

    def test_verified_snapshot_rejects_implementation_or_manifest_identity_drift(self) -> None:
        snapshot = self.snapshot()
        implementations = dict(snapshot.evaluation_implementation_sha256)
        implementations["reporting"] = "f" * 64
        with self.assertRaisesRegex(ReportingError, "implementation.*reporting"):
            score_run(
                verified_run_snapshot=replace(
                    snapshot,
                    evaluation_implementation_sha256=MappingProxyType(
                        implementations
                    ),
                ),
                expected_case_count=5,
            )

        manifest = json.loads(snapshot.manifest_bytes)
        manifest["run_id"] = "different-run"
        manifest["contract_sha256"] = hashlib.sha256(
            _json_bytes(manifest["contract"])
        ).hexdigest()
        manifest_bytes = _json_bytes(manifest)
        with self.assertRaisesRegex(ReportingError, "run_id"):
            score_run(
                verified_run_snapshot=replace(
                    snapshot,
                    manifest_bytes=manifest_bytes,
                    manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                ),
                expected_case_count=5,
            )

    def test_mapping_sequence_hash_must_close_public_and_full_data(self) -> None:
        mappings = [dict(item) for item in self.private_mappings]
        mappings[0]["sequence_sha256"] = "f" * 64
        with self.assertRaisesRegex(ReportingError, "sequence SHA256"):
            self.score(mappings=mappings)

    def test_terminal_response_v2_is_strictly_revalidated(self) -> None:
        terminals = dict(self.terminals)
        sample_id = self.public_cases[0]["sample_id"]
        malformed = json.loads(terminals[sample_id])
        malformed["prediction"]["status"] = "abstain"
        terminals[sample_id] = _json_bytes(malformed)
        with self.assertRaisesRegex(ReportingError, "prediction"):
            self.score(terminal_records=terminals)

    def test_homology_cluster_membership_and_id_are_strict(self) -> None:
        clusters = [dict(row) for row in self.clusters]
        clusters[0]["member_count"] = 99
        with self.assertRaisesRegex(ReportingError, "member_count"):
            self.score(clusters=clusters)

        clusters = [dict(row) for row in self.clusters]
        clusters[0]["cluster_id"] = "cluster_" + "f" * 64
        with self.assertRaisesRegex(ReportingError, "cluster_id"):
            self.score(clusters=clusters)


class FrozenReportingIntegrationTests(unittest.TestCase):
    def test_real_5337_artifacts_close_before_any_model_call(self) -> None:
        root = Path(__file__).resolve().parent.parent
        required = {
            "public": root / "data/derived/cases.v3.jsonl",
            "case_manifest": root / "data/derived/cases.v3.manifest.json",
            "private": root / "data/derived/cases.v3.private-map.jsonl",
            "full": root / "data/derived/full_structured.jsonl",
            "core": root / "data/derived/core_provisional.jsonl",
            "catalog": root / "data/derived/label_catalog.json",
            "ontology": root / "data/derived/ontology_audit.json",
            "view_audit": root / "data/derived/view_audit.json",
            "clusters": root / "data/derived/homology_clusters.v3.jsonl",
            "cluster_manifest": root / "data/derived/homology_clusters.v3.manifest.json",
        }
        missing = [str(path) for path in required.values() if not path.is_file()]
        if missing:
            self.skipTest("frozen derived artifacts are unavailable: " + ", ".join(missing))

        artifact_bytes = {
            "public_cases": required["public"].read_bytes(),
            "case_manifest": required["case_manifest"].read_bytes(),
            "private_mapping": required["private"].read_bytes(),
            "full_structured": required["full"].read_bytes(),
            "core_provisional": required["core"].read_bytes(),
            "label_catalog": required["catalog"].read_bytes(),
            "ontology_audit": required["ontology"].read_bytes(),
            "view_audit": required["view_audit"].read_bytes(),
            "homology_clusters": required["clusters"].read_bytes(),
            "homology_clusters_manifest": required["cluster_manifest"].read_bytes(),
        }
        public_cases = [
            json.loads(line)
            for line in artifact_bytes["public_cases"].decode("utf-8").splitlines()
        ]
        fixture = ReportingFixture(methodName="runTest")
        fixture.setUp()
        snapshot = fixture.snapshot(
            run_id="frozen-artifact-contract-test",
            terminal_records={},
            attempt_records={},
            incident_records={},
            public_cases=public_cases,
            artifact_bytes=artifact_bytes,
        )
        report = score_run(
            verified_run_snapshot=snapshot,
            formal=False,
            expected_case_count=5_337,
        ).to_dict()

        self.assertTrue(report["diagnostic_only"])
        self.assertEqual(report["full_structured"]["source_view_record_count"], 5_337)
        self.assertEqual(report["core_single"]["source_view_record_count"], 3_233)
        ranking_exclusions = report["full_structured"]["ranking_exclusions"]
        self.assertEqual(
            {
                key: ranking_exclusions[key]
                for key in (
                    "zero_weight_accession_count",
                    "exact_sequence_conflict_accession_count",
                    "overlapping_block_label_accession_count",
                    "intersection_accession_count",
                )
            },
            {
                "zero_weight_accession_count": 18,
                "exact_sequence_conflict_accession_count": 12,
                "overlapping_block_label_accession_count": 6,
                "intersection_accession_count": 0,
            },
        )
        self.assertEqual(
            ranking_exclusions[
                "ranking_eligible_exact_sequence_entity_count"
            ],
            5_283,
        )
        self.assertEqual(
            ranking_exclusions["ranking_eligible_accession_count"],
            5_319,
        )
        self.assertEqual(
            ranking_exclusions["ranking_eligible_homology_cluster_count"],
            5_056,
        )
        self.assertEqual(
            report["core_single"]["ranking_excluded_record_count"],
            0,
        )
        self.assertEqual(report["conflict_slice"]["group_count"], 6)
        self.assertEqual(report["conflict_slice"]["accession_count"], 12)
        self.assertEqual(
            report["full_structured"]["gold_formula_audit"],
            {
                "preserved_block_count": 5_911,
                "overlapping_block_pair_count": 8,
                "overlapping_block_label_accession_count": 6,
                "label_union_matches_experimental_labels": True,
            },
        )
        self.assertEqual(
            report["unrepresentable_overlap_slice"]["accession_count"],
            6,
        )
        self.assertEqual(
            report["unrepresentable_overlap_slice"][
                "overlapping_block_pair_count"
            ],
            8,
        )
        self.assertTrue(
            all(
                record["ranking_weight"] == 0.0
                for record in report["unrepresentable_overlap_slice"]["records"]
            )
        )
        self.assertEqual(report["homology_cluster_macro"]["cluster_count"], 5_066)
        self.assertEqual(
            report["input_sha256"]["homology_clusters"],
            "330cf792ffe6b2c8888a1cc3bf6b2865c420f90e42d78ccd47fff3897cc91671",
        )
