from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
import platform
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock
from types import SimpleNamespace

from cofactor_bench import cli
from cofactor_bench import run as run_module
from cofactor_bench.prediction import Prediction
from cofactor_bench.prompt import CatalogTerm, create_prompt_case, render_prompt
from cofactor_bench.run import (
    LedgerBundle,
    RunContractError,
    VerifiedRunSnapshot,
    execute_run_from_config,
    inspect_run_progress_from_config,
    validate_run_from_config,
    verified_run_snapshot_from_config,
)
from cofactor_bench.runner import (
    DISABLED_FEATURES,
    RunAborted,
    TerminalResult,
    build_codex_argv,
    replay_codex_attempt,
)


PROJECT_ROOT = Path(__file__).parents[1]


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _catalog() -> tuple[CatalogTerm, ...]:
    return tuple(
        CatalogTerm(f"CHEBI:{index}", f"frozen cofactor {index}")
        for index in range(1, 105)
    )


class _RecordingRunner:
    instances: list["_RecordingRunner"] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.calls: list[tuple[tuple[str, ...], bool, int]] = []
        self.run_dir = Path(str(kwargs["run_dir"]))
        type(self).instances.append(self)

    def run_cases(
        self,
        cases: tuple[object, ...],
        *,
        resume: bool,
        concurrency: int,
    ) -> tuple[TerminalResult, ...]:
        sample_ids = tuple(case.sample_id for case in cases)
        self.calls.append((sample_ids, resume, concurrency))
        results: list[TerminalResult] = []
        for case in cases:
            prediction = Prediction(
                schema_version="cofactor9.1.response.v2",
                sample_id=case.sample_id,
                status="predict",
                predicted_cofactors=("CHEBI:1",),
                primary_guess="CHEBI:1",
                confidence_complete=0.75,
            )
            result = TerminalResult(
                sample_id=case.sample_id,
                status="success",
                attempt_count=1,
                prediction=prediction,
            )
            terminal = {
                "schema_version": "cofactor9.1.terminal.v1",
                "sample_id": case.sample_id,
                "status": "success",
                "attempt_count": 1,
                "prediction": prediction.to_dict(),
                "error_code": None,
                "error_message": None,
                "completed_at": "2026-09-02T00:00:00Z",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "max",
                "service_tier": "fast",
                "prompt_version": "cofactor9.1.sequence-only.named-catalog.v2",
                "catalog_version": case.catalog_version,
                "prompt_sha256": hashlib.sha256(
                    render_prompt(case).encode("utf-8")
                ).hexdigest(),
            }
            terminal_path = (
                self.run_dir / "cases" / case.sample_id / "terminal.json"
            )
            terminal_path.parent.mkdir(parents=True, exist_ok=True)
            attempt_dir = terminal_path.parent / "attempts" / "attempt-0001"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            prompt_bytes = render_prompt(case).encode("utf-8")
            stdout_bytes = b"".join(
                _canonical_bytes(event)
                for event in (
                    {"type": "thread.started", "thread_id": "fake-thread"},
                    {"type": "turn.started"},
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "message-1",
                            "type": "agent_message",
                            "text": json.dumps(
                                prediction.to_dict(),
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        },
                    },
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                )
            )
            stderr_bytes = b""
            (attempt_dir / "prompt.txt").write_bytes(prompt_bytes)
            (attempt_dir / "stdout.jsonl").write_bytes(stdout_bytes)
            (attempt_dir / "stderr.txt").write_bytes(stderr_bytes)
            (attempt_dir / "prediction.json").write_bytes(
                _canonical_bytes(prediction.to_dict())
            )
            (attempt_dir / "attempt.json").write_bytes(
                _canonical_bytes(
                    {
                        "schema_version": "cofactor9.1.attempt.v1",
                        "sample_id": case.sample_id,
                        "attempt_number": 1,
                        "started_at": "2026-09-02T00:00:00Z",
                        "completed_at": "2026-09-02T00:00:01Z",
                        "duration_seconds": 1.0,
                        "argv": build_codex_argv(
                            executable=self.kwargs["executable"],
                            schema_path=Path(self.kwargs["schema_path"]),
                            working_directory=Path(
                                "/private/tmp/cofactor9.1-attempt-test"
                            ),
                        ),
                        "model": "gpt-5.6-sol",
                        "reasoning_effort": "max",
                        "service_tier": "fast",
                        "returncode": 0,
                        "timed_out": False,
                        "error_code": None,
                        "error_message": None,
                        "retry_disposition": "none",
                        "thread_id": "fake-thread",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                        "prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
                        "stdout_sha256": hashlib.sha256(stdout_bytes).hexdigest(),
                        "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
                        "redaction_count": 0,
                        "environment_policy": "fixed-allowlist",
                    }
                )
            )
            terminal_path.write_bytes(_canonical_bytes(terminal))
            results.append(result)
        return tuple(results)


class RunOrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        _RecordingRunner.instances.clear()
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "project"
        (self.root / "config").mkdir(parents=True)
        (self.root / "data" / "derived").mkdir(parents=True)
        (self.root / "schemas").mkdir()
        (self.root / "schemas" / "model-response.schema.json").write_bytes(
            (PROJECT_ROOT / "schemas" / "model-response.schema.json").read_bytes()
        )
        self.binary = self.root / "fake-codex"
        feature_output = "".join(
            f"printf '{feature} stable false\\n'\n"
            for feature in DISABLED_FEATURES
        )
        self.binary.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then\n"
            "  printf 'codex-cli 0.152.0\\n'\n"
            "else\n"
            f"{feature_output}"
            "fi\n",
            encoding="utf-8",
        )
        self.binary.chmod(0o700)
        self.cases = tuple(
            create_prompt_case(
                sequence="M" + ("A" * index),
                catalog_terms=_catalog(),
                catalog_version="cofactor9.1.allowed-labels.v1",
            )
            for index in range(1, 4)
        )
        cases_bytes = b"".join(
            _canonical_bytes(case.to_payload()) for case in self.cases
        )
        self.cases_path = self.root / "data" / "derived" / "cases.jsonl"
        self.cases_path.write_bytes(cases_bytes)
        accessions = tuple(f"P{index:05d}" for index in range(1, 4))
        full_bytes = b"".join(
            _canonical_bytes(
                {
                    "schema_version": "cofactor9.1.view-record.v1",
                    "dataset_version": "Cofactor9.1",
                    "derivation": {"rule_version": "cofactor9.1.views.v3"},
                    "entry": {"accession": accession},
                }
            )
            for accession in accessions
        )
        core_bytes = full_bytes
        catalog_bytes = _canonical_bytes(
            {
                "schema_version": "cofactor9.1.label-catalog.v1",
                "dataset_version": "Cofactor9.1",
                "catalog_version": "cofactor9.1.allowed-labels.v1",
                "labels": [
                    {"chebi_id": term.chebi_id, "name": term.name}
                    for term in _catalog()
                ],
            }
        )
        ontology_bytes = _canonical_bytes(
            {
                "schema_version": "cofactor9.1.ontology-audit.v1",
                "dataset_version": "Cofactor9.1",
                "pairs": [],
            }
        )
        cluster_bytes = b"".join(
            _canonical_bytes(
                {
                    "schema_version": "cofactor9.1.homology-cluster-record.v1",
                    "accession": accession,
                    "cluster_id": f"cluster-{index}",
                    "representative_accession": accession,
                    "member_count": 1,
                }
            )
            for index, accession in enumerate(accessions, start=1)
        )
        self.artifact_bytes = {
            "full_structured": full_bytes,
            "core_provisional": core_bytes,
            "label_catalog": catalog_bytes,
            "ontology_audit": ontology_bytes,
            "homology_clusters": cluster_bytes,
        }
        for name, value in self.artifact_bytes.items():
            suffix = ".json" if name in {"label_catalog", "ontology_audit"} else ".jsonl"
            (self.root / "data" / "derived" / f"{name}{suffix}").write_bytes(value)
        private_bytes = b"".join(
            _canonical_bytes(
                {
                    "schema_version": "cofactor9.1.case-map.private.v1",
                    "sample_id": case.sample_id,
                    "accession": accession,
                }
            )
            for case, accession in zip(self.cases, accessions, strict=True)
        )
        self.private_map_path = (
            self.root / "data" / "derived" / "cases.private-map.jsonl"
        )
        self.private_map_path.write_bytes(private_bytes)
        self.private_map_path.chmod(0o600)
        self.case_manifest_path = (
            self.root / "data" / "derived" / "cases.manifest.json"
        )
        self.case_manifest_path.write_bytes(
            _canonical_bytes(
                {
                    "schema_version": "cofactor9.1.case-artifacts.v1",
                    "dataset_version": "Cofactor9.1",
                    "prompt_version": (
                        "cofactor9.1.sequence-only.named-catalog.v2"
                    ),
                    "catalog_version": "cofactor9.1.allowed-labels.v1",
                    "view_rule_version": "cofactor9.1.views.v3",
                    "public_case_schema_version": "cofactor9.1.prompt-cases.v1",
                    "private_mapping": {
                        "schema_version": "cofactor9.1.case-map.private.v1",
                        "visibility": "private",
                        "file_mode": "0600",
                        "purpose": "scoring-and-audit-only; never expose to the model",
                    },
                    "input_sha256": {
                        "full_structured": hashlib.sha256(full_bytes).hexdigest(),
                        "label_catalog": hashlib.sha256(catalog_bytes).hexdigest(),
                    },
                    "counts": {
                        "prompt_cases": 3,
                        "private_mappings": 3,
                        "unique_sample_ids": 3,
                        "unique_accessions": 3,
                        "catalog_terms": 104,
                    },
                    "output_sha256": {
                        "prompt_cases": hashlib.sha256(cases_bytes).hexdigest(),
                        "private_mapping": hashlib.sha256(private_bytes).hexdigest(),
                    },
                }
            )
        )
        self.cluster_manifest_path = (
            self.root / "data" / "derived" / "homology_clusters.manifest.json"
        )
        self.cluster_manifest_path.write_bytes(
            _canonical_bytes(
                {
                    "schema_version": "cofactor9.1.homology-cluster-manifest.v1",
                    "dataset_version": "Cofactor9.1",
                    "view_rule_version": "cofactor9.1.views.v3",
                    "cluster_record_schema_version": (
                        "cofactor9.1.homology-cluster-record.v1"
                    ),
                    "input_sha256": {
                        "full_structured": hashlib.sha256(full_bytes).hexdigest(),
                    },
                    "output_sha256": {
                        "homology_clusters": hashlib.sha256(cluster_bytes).hexdigest(),
                    },
                    "counts": {
                        "input_records": 3,
                        "output_records": 3,
                        "unique_accessions": 3,
                        "cluster_count": 3,
                    },
                }
            )
        )
        self.view_audit_path = self.root / "data" / "derived" / "view_audit.json"
        self.view_audit_path.write_bytes(
            _canonical_bytes(
                {
                    "schema_version": "cofactor9.1.view-audit.v1",
                    "dataset_version": "Cofactor9.1",
                    "rule_version": "cofactor9.1.views.v3",
                    "output_artifact_sha256": {
                        name: hashlib.sha256(value).hexdigest()
                        for name, value in self.artifact_bytes.items()
                        if name
                        in {
                            "full_structured",
                            "core_provisional",
                            "label_catalog",
                            "ontology_audit",
                        }
                    },
                    "summary": {
                        "full_structured_accessions": 3,
                        "core_provisional_accessions": 3,
                        "label_catalog": {"label_count": 104},
                        "ontology": {"ancestor_pair_count": 0},
                    },
                }
            )
        )
        self.config_path = self.root / "config" / "benchmark.json"
        self.config = {
            "schema_version": "cofactor9.1.config.v1",
            "dataset_version": "Cofactor9.1",
            "paths": {
                "cases": "data/derived/cases.jsonl",
                "full_structured": "data/derived/full_structured.jsonl",
                "core_provisional": "data/derived/core_provisional.jsonl",
                "label_catalog": "data/derived/label_catalog.json",
                "homology_clusters": "data/derived/homology_clusters.jsonl",
                "homology_clusters_manifest": (
                    "data/derived/homology_clusters.manifest.json"
                ),
                "runs": "runs",
            },
            "model": {
                "name": "gpt-5.6-sol",
                "reasoning_effort": "max",
                "service_tier": "fast",
                "prompt_version": (
                    "cofactor9.1.sequence-only.named-catalog.v2"
                ),
                "response_schema_version": "cofactor9.1.response.v2",
            },
            "run": {
                "transport": "codex_cli_chatgpt_oauth",
                "max_attempts": 3,
                "concurrency": 2,
                "timeout_seconds": 600,
                "circuit_breaker_threshold": 1,
            },
        }
        self._write_config()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _write_config(self) -> None:
        self.config_path.write_bytes(_canonical_bytes(self.config))

    @staticmethod
    def _case_validator(_: str | Path) -> object:
        return object()

    def _execute(self, **overrides: object):
        options: dict[str, object] = {
            "config_path": self.config_path,
            "run_id": "full-gpt56sol-max-fast-v1",
            "concurrency": 2,
            "expected_case_count": 3,
            "executable": self.binary,
            "runner_factory": _RecordingRunner,
            "case_validator": self._case_validator,
        }
        options.update(overrides)
        return execute_run_from_config(**options)

    def _validate(self, **overrides: object):
        options: dict[str, object] = {
            "config_path": self.config_path,
            "run_id": "full-gpt56sol-max-fast-v1",
            "expected_case_count": 3,
            "executable": self.binary,
            "case_validator": self._case_validator,
        }
        options.update(overrides)
        return validate_run_from_config(**options)

    def _write_incident(
        self,
        *,
        run_id: str,
        case_index: int = 0,
        error_code: str = "CAPACITY_ERROR",
        stderr_bytes: bytes = b"service capacity exceeded",
    ) -> Path:
        case = self.cases[case_index]
        incident_dir = (
            self.root
            / "runs"
            / run_id
            / "transport-incidents"
            / case.sample_id
            / "incident-0001"
        )
        incident_dir.mkdir(parents=True)
        prompt_bytes = render_prompt(case).encode("utf-8")
        stdout_bytes = b""
        record = {
            "schema_version": "cofactor9.1.transport-incident.v1",
            "sample_id": case.sample_id,
            "incident_number": 1,
            "tentative_attempt_number": 1,
            "started_at": "2026-09-02T00:00:00Z",
            "completed_at": "2026-09-02T00:00:02Z",
            "duration_seconds": 2.5,
            "argv": build_codex_argv(
                executable=(
                    self.root / "runs" / run_id / "codex-executable"
                ).resolve(),
                schema_path=(
                    self.root / "runs" / run_id / "response-schema.json"
                ),
                working_directory=Path(
                    "/private/tmp/cofactor9.1-attempt-incident-test"
                ),
            ),
            "model": "gpt-5.6-sol",
            "reasoning_effort": "max",
            "service_tier": "fast",
            "returncode": 1,
            "timed_out": False,
            "cancelled": False,
            "start_error": None,
            "error_code": error_code,
            "error_message": "Codex exited with status 1",
            "retry_disposition": "retryable",
            "thread_id": None,
            "usage": {},
            "prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
            "stdout_sha256": hashlib.sha256(stdout_bytes).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
            "prediction_sha256": None,
            "redaction_count": 0,
            "environment_policy": "fixed-allowlist",
        }
        (incident_dir / "prompt.txt").write_bytes(prompt_bytes)
        (incident_dir / "stdout.jsonl").write_bytes(stdout_bytes)
        (incident_dir / "stderr.txt").write_bytes(stderr_bytes)
        (incident_dir / "incident.json").write_bytes(_canonical_bytes(record))
        return incident_dir

    def test_new_formal_run_freezes_full_contract_and_runs_every_case(self) -> None:
        summary = self._execute()

        self.assertEqual(summary.selected_case_count, 3)
        self.assertEqual(summary.terminal_count, 3)
        self.assertEqual(summary.success_count, 3)
        runner = _RecordingRunner.instances[-1]
        self.assertEqual(
            runner.calls,
            [(tuple(case.sample_id for case in self.cases), False, 2)],
        )
        manifest_path = self.root / "runs" / summary.run_id / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        contract = manifest["contract"]
        self.assertEqual(manifest["mode"], "formal")
        self.assertEqual(contract["model"]["name"], "gpt-5.6-sol")
        self.assertEqual(contract["model"]["reasoning_effort"], "max")
        self.assertEqual(contract["model"]["service_tier"], "fast")
        self.assertEqual(contract["execution"]["concurrency"], 2)
        self.assertEqual(contract["cases"]["selected_count"], 3)
        self.assertEqual(
            contract["cases"]["sha256"],
            hashlib.sha256(self.cases_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            contract["cases"]["manifest_sha256"],
            hashlib.sha256(self.case_manifest_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            contract["config"]["sha256"],
            hashlib.sha256(self.config_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            contract["response_schema"]["sha256"],
            hashlib.sha256(
                (self.root / "schemas" / "model-response.schema.json").read_bytes()
            ).hexdigest(),
        )
        frozen_schema = manifest_path.parent / "response-schema.json"
        self.assertTrue(frozen_schema.is_file())
        self.assertEqual(
            frozen_schema.read_bytes(),
            self.root.joinpath(
                "schemas", "model-response.schema.json"
            ).read_bytes(),
        )
        self.assertEqual(stat.S_IMODE(frozen_schema.stat().st_mode), 0o444)
        self.assertEqual(
            contract["response_schema"]["frozen_path"], "response-schema.json"
        )
        self.assertEqual(contract["response_schema"]["file_mode"], "0444")
        self.assertEqual(
            Path(runner.kwargs["schema_path"]).resolve(), frozen_schema.resolve()
        )
        self.assertEqual(contract["codex_binary"]["version"], "codex-cli 0.152.0")
        self.assertEqual(
            contract["codex_binary"]["sha256"],
            hashlib.sha256(self.binary.read_bytes()).hexdigest(),
        )
        frozen_codex = manifest_path.parent / "codex-executable"
        self.assertEqual(frozen_codex.read_bytes(), self.binary.read_bytes())
        self.assertEqual(stat.S_IMODE(frozen_codex.stat().st_mode), 0o555)
        self.assertEqual(
            Path(runner.kwargs["executable"]).resolve(),
            frozen_codex.resolve(),
        )
        runtime = contract["launcher_runtime"]
        self.assertEqual(runtime["kind"], "python")
        self.assertEqual(runtime["resolved_path"], str(Path(sys.executable).resolve()))
        self.assertRegex(runtime["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            contract["transport"]["disabled_features"],
            list(DISABLED_FEATURES),
        )
        self.assertEqual(
            contract["transport"]["disabled_feature_preflight"],
            {
                "command": "native features list with exact --disable overrides",
                "observed_states": {
                    feature: False for feature in DISABLED_FEATURES
                },
                "all_user_tool_features_false": True,
                "non_exposed_backend_exceptions": [],
            },
        )
        for key in (
            "prompt_implementation",
            "prediction_implementation",
            "case_implementation",
            "orchestration_implementation",
        ):
            self.assertRegex(contract[key]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            set(contract["evaluation_implementations"]),
            {"reporting", "scoring", "metrics", "prediction"},
        )
        self.assertEqual(
            set(contract["evaluation_artifacts"]),
            {
                "public_cases",
                "case_manifest",
                "private_mapping",
                "full_structured",
                "core_provisional",
                "label_catalog",
                "ontology_audit",
                "view_audit",
                "homology_clusters",
                "homology_clusters_manifest",
            },
        )
        private_contract = contract["evaluation_artifacts"]["private_mapping"]
        self.assertEqual(private_contract["visibility"], "private-hash-only")
        self.assertNotIn("content", private_contract)
        self.assertEqual(private_contract["record_count"], 3)

    def test_new_run_refuses_any_preexisting_run_directory(self) -> None:
        (self.root / "runs" / "full-gpt56sol-max-fast-v1").mkdir(parents=True)

        with self.assertRaisesRegex(RunContractError, "already exists"):
            self._execute()

        self.assertEqual(_RecordingRunner.instances, [])

    def test_resume_requires_byte_identical_contract_before_runner_starts(self) -> None:
        self._execute()
        before = self._validate()
        run_dir = self.root / "runs" / "full-gpt56sol-max-fast-v1"
        before_files = {
            path.relative_to(run_dir).as_posix(): path.read_bytes()
            for path in run_dir.rglob("*")
            if path.is_file()
        }
        _RecordingRunner.instances.clear()

        resumed = self._execute(resume=True)
        self.assertEqual(resumed.terminal_count, 3)
        self.assertEqual(_RecordingRunner.instances, [])
        after_files = {
            path.relative_to(run_dir).as_posix(): path.read_bytes()
            for path in run_dir.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after_files, before_files)
        self.assertEqual(resumed.ledger_composite_sha256, before.ledger_composite_sha256)
        self.assertEqual(
            resumed.invocation_composite_sha256,
            before.invocation_composite_sha256,
        )

        _RecordingRunner.instances.clear()
        with self.assertRaisesRegex(RunContractError, "must equal benchmark config"):
            self._execute(resume=True, concurrency=3)
        self.assertEqual(_RecordingRunner.instances, [])

    def test_resume_rejects_config_case_and_schema_drift(self) -> None:
        self._execute()
        mutations = (
            (self.config_path, self.config_path.read_bytes() + b" \n"),
            (self.cases_path, self.cases_path.read_bytes() + b"\n"),
            (
                self.root / "data" / "derived" / "full_structured.jsonl",
                self.artifact_bytes["full_structured"] + b"\n",
            ),
            (
                self.root / "data" / "derived" / "ontology_audit.json",
                self.artifact_bytes["ontology_audit"] + b"\n",
            ),
            (
                self.root / "schemas" / "model-response.schema.json",
                (self.root / "schemas" / "model-response.schema.json").read_bytes()
                + b"\n",
            ),
        )
        for path, changed in mutations:
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(changed)
                with self.assertRaises(RunContractError):
                    self._execute(resume=True)
                path.write_bytes(original)

    def test_resume_uses_frozen_codex_after_source_upgrade_or_removal(self) -> None:
        self._execute(run_id="frozen-codex-resume-v1")
        run_dir = self.root / "runs" / "frozen-codex-resume-v1"
        frozen = run_dir / "codex-executable"
        frozen_bytes = frozen.read_bytes()

        self.binary.write_bytes(b"#!/bin/sh\nprintf 'codex-cli 9.999.0\\n'\n")
        self.binary.chmod(0o700)
        upgraded = self._execute(run_id="frozen-codex-resume-v1", resume=True)
        self.assertTrue(upgraded.is_complete)
        self.assertEqual(frozen.read_bytes(), frozen_bytes)
        self.assertEqual(
            Path(_RecordingRunner.instances[-1].kwargs["executable"]).resolve(),
            frozen.resolve(),
        )

        self.binary.unlink()
        removed = self._execute(run_id="frozen-codex-resume-v1", resume=True)
        self.assertTrue(removed.is_complete)
        self.assertEqual(frozen.read_bytes(), frozen_bytes)

    def test_resume_and_validation_reject_frozen_codex_tampering(self) -> None:
        self._execute(run_id="tampered-frozen-codex-v1")
        frozen = (
            self.root / "runs" / "tampered-frozen-codex-v1" / "codex-executable"
        )
        frozen.chmod(0o700)
        frozen.write_bytes(frozen.read_bytes() + b"tampered")
        frozen.chmod(0o555)

        with self.assertRaisesRegex(RunContractError, "Codex executable SHA-256"):
            self._validate(run_id="tampered-frozen-codex-v1")
        _RecordingRunner.instances.clear()
        with self.assertRaisesRegex(RunContractError, "Codex executable SHA-256"):
            self._execute(run_id="tampered-frozen-codex-v1", resume=True)
        self.assertEqual(_RecordingRunner.instances, [])

    def test_resume_rejects_validation_implementation_drift(self) -> None:
        self._execute()
        from cofactor_bench import run as run_module

        original = run_module._module_sha256

        def changed(module_file, *, name):
            if name == "prediction":
                return "f" * 64
            return original(module_file, name=name)

        with mock.patch("cofactor_bench.run._module_sha256", side_effect=changed):
            with self.assertRaisesRegex(RunContractError, "manifest contract drift"):
                self._execute(resume=True)

    def test_resume_checks_launcher_runtime_but_offline_validation_uses_provenance(self) -> None:
        self._execute(run_id="launcher-runtime-v1")
        recorded = run_module._launcher_runtime_contract()
        changed = {**recorded, "sha256": "f" * 64}

        with mock.patch(
            "cofactor_bench.run._launcher_runtime_contract",
            return_value=changed,
        ):
            with self.assertRaisesRegex(
                RunContractError,
                "Python launcher runtime differs",
            ):
                self._execute(run_id="launcher-runtime-v1", resume=True)
            validated = validate_run_from_config(
                config_path=self.config_path,
                run_id="launcher-runtime-v1",
                expected_case_count=3,
                executable=None,
                case_validator=self._case_validator,
            )

        self.assertTrue(validated.is_complete)

    def test_limit_is_only_available_for_explicit_infrastructure_gate(self) -> None:
        with self.assertRaisesRegex(RunContractError, "infrastructure gate"):
            self._execute(limit=1)

        summary = self._execute(
            run_id="transport-gate-v1",
            limit=1,
            infrastructure_gate=True,
        )
        self.assertEqual(summary.mode, "infrastructure_gate")
        self.assertEqual(summary.selected_case_count, 1)
        manifest = json.loads(
            (self.root / "runs" / "transport-gate-v1" / "manifest.json").read_text()
        )
        self.assertEqual(manifest["mode"], "infrastructure_gate")
        self.assertEqual(manifest["contract"]["cases"]["selected_count"], 1)

    def test_formal_validation_requires_every_unique_readable_terminal(self) -> None:
        self._execute()
        validated = self._validate()
        self.assertTrue(validated.is_complete)
        self.assertEqual(validated.terminal_count, 3)

        terminal = (
            self.root
            / "runs"
            / "full-gpt56sol-max-fast-v1"
            / "cases"
            / self.cases[1].sample_id
            / "terminal.json"
        )
        terminal.unlink()
        with self.assertRaisesRegex(RunContractError, "missing terminal"):
            self._validate()

    def test_offline_validation_does_not_need_the_historical_codex_binary(self) -> None:
        self._execute(run_id="offline-score-v1")
        self.binary.unlink()

        summary = validate_run_from_config(
            config_path=self.config_path,
            run_id="offline-score-v1",
            expected_case_count=3,
            executable=None,
            case_validator=self._case_validator,
        )

        self.assertTrue(summary.is_complete)

    def test_validation_rejects_duplicate_json_fields_and_unexpected_case_dirs(self) -> None:
        self._execute()
        terminal = (
            self.root
            / "runs"
            / "full-gpt56sol-max-fast-v1"
            / "cases"
            / self.cases[0].sample_id
            / "terminal.json"
        )
        terminal.write_text(
            '{"sample_id":"x","sample_id":"y"}\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RunContractError, "duplicate"):
            self._validate()

        self._execute(run_id="second-formal")
        extra = self.root / "runs" / "second-formal" / "cases" / "sample_extra"
        extra.mkdir()
        with self.assertRaisesRegex(RunContractError, "unexpected case"):
            self._validate(run_id="second-formal")

    def test_partial_or_hash_drifted_attempt_fails_validation_and_resume(self) -> None:
        self._execute(run_id="partial-attempt-v1")
        attempt_dir = (
            self.root
            / "runs"
            / "partial-attempt-v1"
            / "cases"
            / self.cases[0].sample_id
            / "attempts"
            / "attempt-0001"
        )
        (attempt_dir / "attempt.json").unlink()

        with self.assertRaisesRegex(RunContractError, "partial attempt"):
            self._validate(run_id="partial-attempt-v1")
        _RecordingRunner.instances.clear()
        with self.assertRaisesRegex(RunContractError, "partial attempt"):
            self._execute(run_id="partial-attempt-v1", resume=True)
        self.assertEqual(_RecordingRunner.instances, [])

        self._execute(run_id="hash-drift-v1")
        stdout_path = (
            self.root
            / "runs"
            / "hash-drift-v1"
            / "cases"
            / self.cases[0].sample_id
            / "attempts"
            / "attempt-0001"
            / "stdout.jsonl"
        )
        stdout_path.write_bytes(b"tampered\n")
        with self.assertRaisesRegex(RunContractError, "stdout SHA-256"):
            self._validate(run_id="hash-drift-v1")

    def test_resume_allows_runner_owned_inflight_recovery_state(self) -> None:
        self._execute(run_id="recoverable-partial-v1")
        case_dir = (
            self.root
            / "runs"
            / "recoverable-partial-v1"
            / "cases"
            / self.cases[0].sample_id
        )
        (case_dir / "terminal.json").unlink()
        shutil.rmtree(case_dir / "attempts" / "attempt-0001")
        inflight = (
            self.root
            / "runs"
            / "recoverable-partial-v1"
            / ".inflight"
            / self.cases[0].sample_id
        )
        inflight.mkdir(parents=True)
        (inflight / "prompt.txt").write_bytes(
            render_prompt(self.cases[0]).encode("utf-8")
        )
        staging = self.root / "runs" / "recoverable-partial-v1" / ".staging"
        runner_publication = staging / "publish-case-runner-owned.tmp"
        launcher_publication = staging / "publish-launch-runner-owned.tmp"
        runner_publication.write_bytes(b"runner-owned")
        launcher_publication.write_bytes(b"launcher-owned")

        class RecoveringRunner(_RecordingRunner):
            def run_cases(self, cases, *, resume, concurrency):
                self.assert_runner_publications_exist()
                shutil.rmtree(self.run_dir / ".inflight" / cases[0].sample_id)
                runner_publication.unlink()
                launcher_publication.unlink()
                return super().run_cases(
                    cases,
                    resume=resume,
                    concurrency=concurrency,
                )

            @staticmethod
            def assert_runner_publications_exist():
                if not runner_publication.is_file() or not launcher_publication.is_file():
                    raise AssertionError("foreign staging namespace was consumed")

        resumed = self._execute(
            run_id="recoverable-partial-v1",
            resume=True,
            runner_factory=RecoveringRunner,
        )
        self.assertTrue(resumed.is_complete)
        self.assertTrue((case_dir / "terminal.json").is_file())

    def test_infrastructure_gate_requires_every_selected_case_to_succeed(self) -> None:
        class ErrorRunner(_RecordingRunner):
            def run_cases(self, cases, *, resume, concurrency):
                super().run_cases(cases, resume=resume, concurrency=concurrency)
                results = []
                for case in cases:
                    case_dir = self.run_dir / "cases" / case.sample_id
                    terminal_path = case_dir / "terminal.json"
                    terminal = json.loads(terminal_path.read_text())
                    attempt_path = case_dir / "attempts" / "attempt-0001" / "attempt.json"
                    attempt = json.loads(attempt_path.read_text())
                    stdout = b"".join(
                        _canonical_bytes(event)
                        for event in (
                            {"type": "thread.started", "thread_id": "fake-thread"},
                            {"type": "turn.started"},
                            {
                                "type": "item.completed",
                                "item": {
                                    "id": "tool-1",
                                    "type": "command_execution",
                                },
                            },
                        )
                    )
                    stderr = b""
                    replay = replay_codex_attempt(
                        case=case,
                        argv=attempt["argv"],
                        stdout=stdout.decode(),
                        stderr="",
                        returncode=0,
                        timed_out=False,
                    )
                    attempt.update(
                        returncode=0,
                        error_code=replay.error_code,
                        error_message=replay.error_message,
                        retry_disposition=replay.retry_disposition,
                        thread_id=replay.thread_id,
                        usage=dict(replay.usage),
                    )
                    (attempt_path.parent / "stdout.jsonl").write_bytes(stdout)
                    (attempt_path.parent / "stderr.txt").write_bytes(stderr)
                    attempt["stdout_sha256"] = hashlib.sha256(stdout).hexdigest()
                    attempt["stderr_sha256"] = hashlib.sha256(stderr).hexdigest()
                    attempt_path.write_bytes(_canonical_bytes(attempt))
                    (attempt_path.parent / "prediction.json").unlink()
                    terminal.update(
                        status="terminal_error",
                        prediction=None,
                        error_code=replay.error_code,
                        error_message=replay.error_message,
                    )
                    terminal_path.write_bytes(_canonical_bytes(terminal))
                    results.append(
                        TerminalResult(
                            sample_id=case.sample_id,
                            status="terminal_error",
                            attempt_count=1,
                            error_code=replay.error_code,
                            error_message=replay.error_message,
                        )
                    )
                return tuple(results)

        with self.assertRaisesRegex(RunContractError, "infrastructure gate failed"):
            self._execute(
                run_id="failed-gate-v1",
                limit=2,
                infrastructure_gate=True,
                runner_factory=ErrorRunner,
            )

        progress = inspect_run_progress_from_config(
            config_path=self.config_path,
            run_id="failed-gate-v1",
            expected_case_count=3,
        )
        self.assertTrue(progress.is_complete)
        self.assertEqual(progress.success_count, 0)
        self.assertEqual(progress.terminal_error_count, 2)

    def test_terminal_error_is_reclassified_from_raw_process_capture(self) -> None:
        class MismatchRunner(_RecordingRunner):
            def run_cases(self, cases, *, resume, concurrency):
                super().run_cases(cases, resume=resume, concurrency=concurrency)
                case = cases[0]
                case_dir = self.run_dir / "cases" / case.sample_id
                attempt_path = case_dir / "attempts" / "attempt-0001" / "attempt.json"
                attempt = json.loads(attempt_path.read_text())
                stderr = b"authentication failed: invalid api key"
                stdout = b""
                (attempt_path.parent / "stdout.jsonl").write_bytes(stdout)
                (attempt_path.parent / "stderr.txt").write_bytes(stderr)
                (attempt_path.parent / "prediction.json").unlink()
                attempt.update(
                    returncode=1,
                    error_code="PROCESS_EXIT",
                    error_message="Codex exited with status 1",
                    retry_disposition="retryable",
                    thread_id=None,
                    usage={},
                    stdout_sha256=hashlib.sha256(stdout).hexdigest(),
                    stderr_sha256=hashlib.sha256(stderr).hexdigest(),
                )
                attempt_path.write_bytes(_canonical_bytes(attempt))
                terminal_path = case_dir / "terminal.json"
                terminal = json.loads(terminal_path.read_text())
                terminal.update(
                    status="terminal_error",
                    prediction=None,
                    error_code="PROCESS_EXIT",
                    error_message="Codex exited with status 1",
                )
                terminal_path.write_bytes(_canonical_bytes(terminal))
                return tuple()

        with self.assertRaisesRegex(RunContractError, "raw transport outcome"):
            self._execute(run_id="raw-error-mismatch", runner_factory=MismatchRunner)

    def test_frozen_response_schema_copy_is_required_and_immutable(self) -> None:
        self._execute(run_id="schema-copy-v1")
        frozen = self.root / "runs" / "schema-copy-v1" / "response-schema.json"
        frozen.chmod(0o600)
        frozen.write_bytes(frozen.read_bytes() + b"\n")

        with self.assertRaisesRegex(RunContractError, "frozen response schema"):
            self._validate(run_id="schema-copy-v1")
        _RecordingRunner.instances.clear()
        with self.assertRaisesRegex(RunContractError, "frozen response schema"):
            self._execute(run_id="schema-copy-v1", resume=True)
        self.assertEqual(_RecordingRunner.instances, [])

    def test_terminal_attempt_count_must_match_complete_attempt_ledger(self) -> None:
        self._execute(run_id="attempt-count-v1")
        terminal_path = (
            self.root
            / "runs"
            / "attempt-count-v1"
            / "cases"
            / self.cases[0].sample_id
            / "terminal.json"
        )
        terminal = json.loads(terminal_path.read_text())
        terminal["attempt_count"] = 2
        terminal_path.write_bytes(_canonical_bytes(terminal))

        with self.assertRaisesRegex(RunContractError, "attempt_count"):
            self._validate(run_id="attempt-count-v1")

    def test_terminal_outcome_must_match_the_final_attempt(self) -> None:
        self._execute(run_id="outcome-mismatch-v1")
        case_dir = (
            self.root
            / "runs"
            / "outcome-mismatch-v1"
            / "cases"
            / self.cases[0].sample_id
        )
        terminal_path = case_dir / "terminal.json"
        terminal = json.loads(terminal_path.read_text())
        terminal.update(
            status="terminal_error",
            prediction=None,
            error_code="TIMEOUT",
            error_message="timed out",
        )
        terminal_path.write_bytes(_canonical_bytes(terminal))
        (case_dir / "attempts" / "attempt-0001" / "prediction.json").unlink()

        with self.assertRaisesRegex(RunContractError, "lacks prediction"):
            self._validate(run_id="outcome-mismatch-v1")

    def test_attempt_argv_and_raw_success_stdout_are_replayed(self) -> None:
        self._execute(run_id="attempt-replay-v1")
        attempt_dir = (
            self.root
            / "runs"
            / "attempt-replay-v1"
            / "cases"
            / self.cases[0].sample_id
            / "attempts"
            / "attempt-0001"
        )
        attempt_path = attempt_dir / "attempt.json"
        attempt = json.loads(attempt_path.read_text())
        attempt["argv"].remove("shell_tool")
        attempt_path.write_bytes(_canonical_bytes(attempt))
        with self.assertRaisesRegex(RunContractError, "argv differs"):
            self._validate(run_id="attempt-replay-v1")

        self._execute(run_id="stdout-replay-v1")
        attempt_dir = (
            self.root
            / "runs"
            / "stdout-replay-v1"
            / "cases"
            / self.cases[0].sample_id
            / "attempts"
            / "attempt-0001"
        )
        stdout_path = attempt_dir / "stdout.jsonl"
        changed = stdout_path.read_text().replace("CHEBI:1", "CHEBI:2")
        stdout_path.write_text(changed)
        attempt_path = attempt_dir / "attempt.json"
        attempt = json.loads(attempt_path.read_text())
        attempt["stdout_sha256"] = hashlib.sha256(changed.encode()).hexdigest()
        attempt_path.write_bytes(_canonical_bytes(attempt))
        with self.assertRaisesRegex(RunContractError, "replayed prediction"):
            self._validate(run_id="stdout-replay-v1")

    def test_attempt_count_cannot_exceed_manifest_max_attempts(self) -> None:
        self._execute(run_id="too-many-attempts-v1")
        case_dir = (
            self.root
            / "runs"
            / "too-many-attempts-v1"
            / "cases"
            / self.cases[0].sample_id
        )
        first = case_dir / "attempts" / "attempt-0001"
        for number in (2, 3, 4):
            copied = case_dir / "attempts" / f"attempt-{number:04d}"
            shutil.copytree(first, copied)
            attempt = json.loads((copied / "attempt.json").read_text())
            attempt["attempt_number"] = number
            (copied / "attempt.json").write_bytes(_canonical_bytes(attempt))
        terminal_path = case_dir / "terminal.json"
        terminal = json.loads(terminal_path.read_text())
        terminal["attempt_count"] = 4
        terminal_path.write_bytes(_canonical_bytes(terminal))

        with self.assertRaisesRegex(RunContractError, "exceeds max_attempts"):
            self._validate(run_id="too-many-attempts-v1")

    def test_model_contract_mismatch_is_rejected_before_creating_run(self) -> None:
        self.config["model"]["reasoning_effort"] = "high"
        self._write_config()

        with self.assertRaisesRegex(RunContractError, "reasoning_effort"):
            self._execute(run_id="invalid-contract")

        self.assertFalse(self.root.joinpath("runs", "invalid-contract").exists())

    def test_effective_timeout_breaker_and_concurrency_are_frozen_and_forwarded(self) -> None:
        self.config["run"].update(concurrency=3, timeout_seconds=321)
        self._write_config()
        self._execute(
            run_id="settings-v1",
            concurrency=3,
            timeout_seconds=321,
            circuit_breaker_threshold=1,
        )

        runner = _RecordingRunner.instances[-1]
        self.assertEqual(runner.kwargs["timeout_seconds"], 321.0)
        self.assertEqual(runner.kwargs["circuit_breaker_threshold"], 1)
        self.assertEqual(runner.calls[0][2], 3)
        manifest = json.loads(
            (self.root / "runs" / "settings-v1" / "manifest.json").read_text()
        )
        self.assertEqual(
            manifest["contract"]["transport"]["timeout_seconds"], 321.0
        )
        self.assertEqual(
            manifest["contract"]["transport"]["circuit_breaker_threshold"], 1
        )
        self.assertEqual(manifest["contract"]["execution"]["concurrency"], 3)

    def test_run_settings_must_equal_config_and_breaker_is_exactly_one(self) -> None:
        for breaker in (0, 2, True, 1.0):
            with self.subTest(breaker=breaker):
                self.config["run"]["circuit_breaker_threshold"] = breaker
                self._write_config()
                with self.assertRaisesRegex(
                    RunContractError, "circuit_breaker_threshold must be exactly 1"
                ):
                    self._execute(run_id=f"bad-breaker-{str(breaker).lower()}")
        self.config["run"]["circuit_breaker_threshold"] = 1
        self.config["run"]["max_attempts"] = 2
        self._write_config()
        with self.assertRaisesRegex(RunContractError, "max_attempts must be exactly 3"):
            self._execute(run_id="bad-attempt-budget")

        self.config["run"]["max_attempts"] = 3
        self._write_config()
        with self.assertRaisesRegex(RunContractError, "must equal benchmark config"):
            self._execute(run_id="override-drift", concurrency=3)

    def test_aborted_run_preserves_manifest_and_reports_resumable_progress(self) -> None:
        class AbortingRunner(_RecordingRunner):
            def run_cases(self, cases, *, resume, concurrency):
                completed = super().run_cases(
                    tuple(cases[:1]), resume=resume, concurrency=concurrency
                )
                raise RunAborted(
                    error_code="CAPACITY_ERROR",
                    threshold=1,
                    completed_results=completed,
                    failures=(),
                    pending_sample_ids=tuple(case.sample_id for case in cases[1:]),
                )

        with self.assertRaises(RunAborted):
            self._execute(run_id="aborted-v1", runner_factory=AbortingRunner)

        manifest = self.root / "runs" / "aborted-v1" / "manifest.json"
        self.assertTrue(manifest.is_file())
        progress = inspect_run_progress_from_config(
            config_path=self.config_path,
            run_id="aborted-v1",
            expected_case_count=3,
        )
        self.assertEqual(progress.terminal_count, 1)
        self.assertEqual(progress.missing_terminal_count, 2)
        self.assertFalse(progress.is_complete)

        invocation_dir = self.root / "runs" / "aborted-v1" / "invocations"
        entries = sorted(invocation_dir.iterdir())
        self.assertEqual([entry.name for entry in entries], ["invocation-0001"])
        end = json.loads((entries[0] / "end.json").read_text())
        self.assertEqual(end["status"], "exception")
        self.assertEqual(end["error_type"], "RunAborted")

    def test_invocation_ledger_is_append_only_contiguous_and_closed(self) -> None:
        self._execute(run_id="invocations-v1")
        self._execute(run_id="invocations-v1", resume=True)
        root = self.root / "runs" / "invocations-v1" / "invocations"
        self.assertEqual(
            sorted(entry.name for entry in root.iterdir()),
            ["invocation-0001"],
        )
        manifest_bytes = (
            self.root / "runs" / "invocations-v1" / "manifest.json"
        ).read_bytes()
        for number in (1,):
            directory = root / f"invocation-{number:04d}"
            self.assertEqual(
                {path.name for path in directory.iterdir()},
                {"start.json", "end.json"},
            )
            start = json.loads((directory / "start.json").read_text())
            end = json.loads((directory / "end.json").read_text())
            self.assertEqual(start["invocation_number"], number)
            self.assertEqual(end["invocation_number"], number)
            self.assertEqual(
                start["run_manifest_sha256"], hashlib.sha256(manifest_bytes).hexdigest()
            )
            self.assertEqual(end["status"], "success")
        summary = self._validate(run_id="invocations-v1")
        self.assertEqual(summary.invocation_count, 1)
        self.assertRegex(summary.invocation_composite_sha256, r"^[0-9a-f]{64}$")

    def test_invocation_start_is_staged_before_atomic_publication(self) -> None:
        original_publish = run_module._write_bytes_exclusive_direct

        def fail_staged_start(path: Path, value: bytes) -> None:
            if path.name == "start.json":
                raise OSError("simulated host loss before invocation publication")
            original_publish(path, value)

        with mock.patch.object(
            run_module,
            "_write_bytes_exclusive_direct",
            side_effect=fail_staged_start,
        ), self.assertRaisesRegex(OSError, "simulated host loss"):
            self._execute(run_id="atomic-invocation-v1")

        invocation_root = self.root / "runs" / "atomic-invocation-v1" / "invocations"
        self.assertFalse(
            invocation_root.exists() and any(invocation_root.iterdir()),
            "a failed start publication must not expose an empty final invocation",
        )

        summary = self._execute(run_id="atomic-invocation-v1", resume=True)
        self.assertTrue(summary.is_complete)
        self.assertEqual(
            sorted(path.name for path in invocation_root.iterdir()),
            ["invocation-0001"],
        )

    def test_resume_recovers_all_private_invocation_staging_crash_shapes(self) -> None:
        shapes = ("empty", "temporary-only", "start-and-temporary")
        for shape in shapes:
            with self.subTest(shape=shape):
                run_id = f"staged-{shape}-v1"
                self._execute(run_id=run_id)
                run_dir = self.root / "runs" / run_id
                invocation_root = run_dir / "invocations"
                start = json.loads(
                    (invocation_root / "invocation-0001" / "start.json").read_text()
                )
                start["invocation_number"] = 2
                start["resume"] = True
                start["argv"] = [*start["argv"], "--resume"]
                staged = run_dir / ".staging" / f".invocation-{shape}.tmp"
                staged.mkdir()
                if shape in {"temporary-only", "start-and-temporary"}:
                    (staged / ".start.json.123.deadbeef.tmp").write_bytes(
                        _canonical_bytes(start)
                    )
                if shape == "start-and-temporary":
                    (staged / "start.json").write_bytes(_canonical_bytes(start))

                summary = self._execute(run_id=run_id, resume=True)

                self.assertFalse(staged.exists())
                expected_count = 1 if shape == "empty" else 2
                self.assertEqual(summary.invocation_count, expected_count)
                if shape != "empty":
                    recovered = invocation_root / "invocation-0002"
                    recovered_end = json.loads((recovered / "end.json").read_text())
                    self.assertEqual(recovered_end["status"], "host_interrupted")

    def test_resume_recovers_end_publication_temp_before_or_after_link(self) -> None:
        for final_already_linked in (False, True):
            with self.subTest(final_already_linked=final_already_linked):
                run_id = f"end-publication-{int(final_already_linked)}-v1"
                self._execute(run_id=run_id)
                run_dir = self.root / "runs" / run_id
                invocation = run_dir / "invocations" / "invocation-0001"
                end_path = invocation / "end.json"
                end_bytes = end_path.read_bytes()
                if not final_already_linked:
                    end_path.unlink()
                staged = (
                    run_dir
                    / ".staging"
                    / "publish-invocation-0001-end.json-123-deadbeef.tmp"
                )
                staged.write_bytes(end_bytes)

                summary = self._execute(run_id=run_id, resume=True)

                self.assertFalse(staged.exists())
                self.assertEqual(end_path.read_bytes(), end_bytes)
                self.assertEqual(summary.invocation_count, 1)

    def test_resume_discards_partial_end_temp_and_seals_start_only(self) -> None:
        run_id = "partial-end-publication-v1"
        self._execute(run_id=run_id)
        run_dir = self.root / "runs" / run_id
        invocation = run_dir / "invocations" / "invocation-0001"
        (invocation / "end.json").unlink()
        staged = (
            run_dir
            / ".staging"
            / "publish-invocation-0001-end.json-123-deadbeef.tmp"
        )
        staged.write_bytes(b'{"schema_version":"truncated')

        summary = self._execute(run_id=run_id, resume=True)

        self.assertFalse(staged.exists())
        recovered_end = json.loads((invocation / "end.json").read_text())
        self.assertEqual(recovered_end["status"], "host_interrupted")
        self.assertEqual(recovered_end["error_type"], "HostInterruption")
        self.assertEqual(summary.invocation_count, 1)

    def test_resume_audits_only_a_safe_trailing_empty_invocation(self) -> None:
        self._execute(run_id="legacy-empty-invocation-v1")
        invocation_root = (
            self.root / "runs" / "legacy-empty-invocation-v1" / "invocations"
        )
        empty = invocation_root / "invocation-0002"
        empty.mkdir()
        latest_ledger_mtime = max(
            path.stat().st_mtime_ns
            for path in (self.root / "runs" / "legacy-empty-invocation-v1").rglob("*")
            if path != empty
        )
        os_time = latest_ledger_mtime + 1_000_000
        os.utime(empty, ns=(os_time, os_time))

        summary = self._execute(run_id="legacy-empty-invocation-v1", resume=True)
        self.assertTrue(summary.is_complete)
        self.assertEqual(summary.invocation_count, 2)
        recovered_start = json.loads((empty / "start.json").read_text())
        recovered_end = json.loads((empty / "end.json").read_text())
        self.assertEqual(recovered_start["status"], "recovered_empty_allocation")
        self.assertEqual(
            recovered_start["argv"],
            ["<unavailable-before-atomic-start-publication>"],
        )
        self.assertEqual(recovered_end["status"], "host_interrupted")
        self.assertEqual(
            recovered_end["error_type"],
            "HostInterruptionBeforeStartPublication",
        )

        self._execute(run_id="unsafe-empty-invocation-v1")
        unsafe_run = self.root / "runs" / "unsafe-empty-invocation-v1"
        unsafe_empty = unsafe_run / "invocations" / "invocation-0002"
        unsafe_empty.mkdir()
        unsafe_terminal = unsafe_run / "cases" / self.cases[0].sample_id / "terminal.json"
        allocation_time = unsafe_empty.stat().st_mtime_ns
        os.utime(
            unsafe_terminal,
            ns=(allocation_time + 1_000_000, allocation_time + 1_000_000),
        )
        with self.assertRaisesRegex(
            RunContractError,
            "model ledger changed after empty invocation allocation",
        ):
            self._execute(run_id="unsafe-empty-invocation-v1", resume=True)
        self.assertEqual(tuple(unsafe_empty.iterdir()), ())

    def test_resume_closes_a_trailing_start_only_invocation(self) -> None:
        self._execute(run_id="start-only-invocation-v1")
        invocation_root = (
            self.root / "runs" / "start-only-invocation-v1" / "invocations"
        )
        first = invocation_root / "invocation-0001"
        (first / "end.json").unlink()

        summary = self._execute(run_id="start-only-invocation-v1", resume=True)

        self.assertEqual(summary.invocation_count, 1)
        end = json.loads((first / "end.json").read_text())
        self.assertEqual(end["status"], "host_interrupted")
        self.assertEqual(end["error_type"], "HostInterruption")

    def test_run_lifecycle_lock_rejects_a_concurrent_resume(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        first_error: list[BaseException] = []

        class BlockingRunner(_RecordingRunner):
            def run_cases(self, cases, *, resume, concurrency):
                entered.set()
                if not release.wait(timeout=10):
                    raise AssertionError("test did not release the blocking runner")
                return super().run_cases(
                    cases,
                    resume=resume,
                    concurrency=concurrency,
                )

        def first_run() -> None:
            try:
                self._execute(
                    run_id="lifecycle-lock-v1",
                    runner_factory=BlockingRunner,
                )
            except BaseException as error:
                first_error.append(error)

        worker = threading.Thread(target=first_run, daemon=True)
        worker.start()
        self.assertTrue(entered.wait(timeout=10), "first runner never started")
        first_invocation = (
            self.root
            / "runs"
            / "lifecycle-lock-v1"
            / "invocations"
            / "invocation-0001"
        )
        self.assertTrue((first_invocation / "start.json").is_file())
        self.assertFalse((first_invocation / "end.json").exists())
        instance_count = len(_RecordingRunner.instances)

        try:
            with self.assertRaisesRegex(RunContractError, "active invocation"):
                self._execute(run_id="lifecycle-lock-v1", resume=True)
            self.assertEqual(len(_RecordingRunner.instances), instance_count)
            self.assertFalse((first_invocation / "end.json").exists())
        finally:
            release.set()
            worker.join(timeout=10)

        self.assertFalse(worker.is_alive())
        self.assertEqual(first_error, [])
        self.assertTrue((first_invocation / "end.json").is_file())
        final = self._execute(run_id="lifecycle-lock-v1", resume=True)
        self.assertTrue(final.is_complete)
        self.assertEqual(final.invocation_count, 1)

    def test_node_wrapper_freezes_and_executes_the_native_codex_binary(self) -> None:
        target_by_platform = {
            ("Darwin", "arm64"): ("@openai/codex-darwin-arm64", "aarch64-apple-darwin"),
            ("Darwin", "x86_64"): ("@openai/codex-darwin-x64", "x86_64-apple-darwin"),
            ("Linux", "aarch64"): ("@openai/codex-linux-arm64", "aarch64-unknown-linux-musl"),
            ("Linux", "x86_64"): ("@openai/codex-linux-x64", "x86_64-unknown-linux-musl"),
        }
        platform_key = (platform.system(), platform.machine().lower())
        if platform_key not in target_by_platform:
            self.skipTest(f"unsupported wrapper test platform {platform_key!r}")
        package_name, target = target_by_platform[platform_key]
        package_root = self.root / "node_modules" / "@openai" / "codex"
        wrapper = package_root / "bin" / "codex.js"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text(
            "#!/usr/bin/env node\n"
            "const PLATFORM_PACKAGE_BY_TARGET = {};\n"
            "const targetTriple = 'test';\n"
            "const binaryPath = findCodexExecutable();\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
        native = (
            package_root
            / "node_modules"
            / "@openai"
            / package_name.removeprefix("@openai/")
            / "vendor"
            / target
            / "bin"
            / "codex"
        )
        native.parent.mkdir(parents=True)
        native_bytes = (
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then\n"
            "  printf 'codex-cli 0.152.0\\n'\n"
            "else\n"
            + "".join(
                f"printf '{feature} stable false\\n'\n"
                for feature in DISABLED_FEATURES
            )
            + "fi\n"
        ).encode("utf-8")
        native.write_bytes(native_bytes)
        native.chmod(0o700)

        summary = self._execute(run_id="node-wrapper-v1", executable=wrapper)

        self.assertTrue(summary.is_complete)
        run_dir = self.root / "runs" / "node-wrapper-v1"
        contract = json.loads((run_dir / "manifest.json").read_text())["contract"]
        binary = contract["codex_binary"]
        self.assertEqual(binary["launcher_kind"], "openai-node-wrapper")
        self.assertEqual(binary["launcher_path"], str(wrapper.resolve()))
        self.assertEqual(binary["resolved_path"], str(native.resolve()))
        self.assertEqual(binary["sha256"], hashlib.sha256(native_bytes).hexdigest())
        frozen = run_dir / binary["frozen_path"]
        self.assertEqual(frozen.read_bytes(), native_bytes)
        self.assertEqual(stat.S_IMODE(frozen.stat().st_mode), 0o555)
        self.assertEqual(
            Path(_RecordingRunner.instances[-1].kwargs["executable"]).resolve(),
            frozen.resolve(),
        )

    def test_installed_default_codex_wrapper_resolves_to_its_native_binary(self) -> None:
        located = shutil.which("codex")
        if located is None:
            self.skipTest("default codex executable is not installed")
        launcher = Path(located).resolve()
        first_line = launcher.read_bytes().splitlines()[:1]
        if not first_line or b"node" not in first_line[0]:
            self.skipTest("installed codex is already a direct native executable")

        identity = run_module._binary_identity("codex")

        self.assertEqual(identity.launcher_kind, "openai-node-wrapper")
        self.assertEqual(identity.launcher_path, launcher)
        self.assertNotEqual(identity.resolved_path, launcher)
        self.assertGreater(identity.resolved_path.stat().st_size, launcher.stat().st_size)
        self.assertEqual(
            identity.sha256,
            hashlib.sha256(identity.resolved_path.read_bytes()).hexdigest(),
        )

    def test_incident_ledger_and_run_tree_are_strictly_validated(self) -> None:
        self._execute(run_id="incident-v1")
        incident_dir = self._write_incident(run_id="incident-v1")
        summary = self._validate(run_id="incident-v1")
        self.assertEqual(summary.incident_count, 1)
        self.assertEqual(summary.incident_error_code_counts, {"CAPACITY_ERROR": 1})
        self.assertEqual(summary.total_duration_seconds, 5.5)
        self.assertEqual(summary.usage, {"input_tokens": 3, "output_tokens": 3})
        self.assertRegex(summary.incident_composite_sha256, r"^[0-9a-f]{64}$")

        (incident_dir / "unexpected.txt").write_text("x")
        with self.assertRaisesRegex(RunContractError, "incident artifact set"):
            self._validate(run_id="incident-v1")
        (incident_dir / "unexpected.txt").unlink()

        unknown = self.root / "runs" / "incident-v1" / "unknown-ledger"
        unknown.mkdir()
        with self.assertRaisesRegex(RunContractError, "unexpected run artifact"):
            self._validate(run_id="incident-v1")
        unknown.rmdir()

        inflight = self.root / "runs" / "incident-v1" / ".inflight" / "stray"
        inflight.mkdir(parents=True)
        with self.assertRaisesRegex(RunContractError, "inflight.*must be empty"):
            self._validate(run_id="incident-v1")

    def test_incident_identity_and_raw_classification_are_replayed(self) -> None:
        self._execute(run_id="incident-replay-v1")
        self._write_incident(
            run_id="incident-replay-v1",
            error_code="TRANSPORT_ERROR",
            stderr_bytes=b"authentication failed: invalid api key",
        )
        with self.assertRaisesRegex(RunContractError, "raw transport outcome"):
            self._validate(run_id="incident-replay-v1")

    def test_verified_snapshot_contains_immutable_bundles_and_provenance(self) -> None:
        self._execute(run_id="snapshot-v1")
        self._write_incident(run_id="snapshot-v1")
        snapshot = verified_run_snapshot_from_config(
            config_path=self.config_path,
            run_id="snapshot-v1",
            expected_case_count=3,
            executable=self.binary,
            case_validator=self._case_validator,
        )
        self.assertIsInstance(snapshot, VerifiedRunSnapshot)
        self.assertEqual(snapshot.run_id, "snapshot-v1")
        self.assertEqual(
            snapshot.artifact_bytes["full_structured"],
            self.artifact_bytes["full_structured"],
        )
        self.assertIsInstance(
            snapshot.attempt_bundles[self.cases[0].sample_id][0], LedgerBundle
        )
        self.assertEqual(
            snapshot.incident_bundles[self.cases[0].sample_id][0].number, 1
        )
        self.assertRegex(snapshot.provenance_composite_sha256, r"^[0-9a-f]{64}$")

        full_path = self.root / "data" / "derived" / "full_structured.jsonl"
        full_path.write_bytes(b"changed after snapshot\n")
        self.assertEqual(
            snapshot.artifact_bytes["full_structured"],
            self.artifact_bytes["full_structured"],
        )
        with self.assertRaises(RunContractError):
            self._validate(run_id="snapshot-v1")

    def test_unsafe_run_ids_are_rejected_without_allocating_directories(self) -> None:
        for run_id in ("../escape", "/absolute", "a/b", "..", "", "a" * 129):
            with self.subTest(run_id=run_id):
                with self.assertRaises(RunContractError):
                    self._execute(run_id=run_id)
        self.assertFalse((self.root / "runs").exists())


class CommandLineTests(unittest.TestCase):
    def _score_fixture(
        self,
        directory: str,
        *,
        run_id: str = "formal-v1",
    ) -> tuple[Path, Path, SimpleNamespace, mock.Mock, bytes, bytes]:
        root = Path(directory).resolve()
        config_path = root / "config" / "benchmark.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_bytes(
            _canonical_bytes(
                {
                    "schema_version": "cofactor9.1.config.v1",
                    "paths": {"runs": "runs"},
                }
            )
        )
        run_dir = root / "runs" / run_id
        (run_dir / ".staging").mkdir(parents=True)
        validation = mock.Mock()
        validation.to_dict.return_value = {"run_id": run_id}
        snapshot = SimpleNamespace(
            run_id=run_id,
            run_dir=run_dir,
            validation=validation,
            manifest_sha256="1" * 64,
            ledger_composite_sha256="2" * 64,
            provenance_composite_sha256="3" * 64,
            artifact_sha256={"public_cases": "4" * 64},
            evaluation_implementation_sha256={"reporting": "5" * 64},
        )
        report = mock.Mock()
        report.to_dict.return_value = {"headline": {"score": 1.0}}
        return (
            config_path,
            run_dir,
            snapshot,
            report,
            b'{"report":true}\n',
            b"# report\n",
        )

    @staticmethod
    def _score_staging_path(run_dir: Path, name: str, value: bytes) -> Path:
        digest = hashlib.sha256(value).hexdigest()
        return run_dir / ".staging" / f"score-{name}-{digest}.tmp"

    def _run_mock_score(
        self,
        *,
        config_path: Path,
        run_dir: Path,
        snapshot: SimpleNamespace,
        report: mock.Mock,
        metrics_bytes: bytes,
        markdown_bytes: bytes,
    ) -> dict[str, object]:
        def reject_unrecovered_score_staging(**_: object) -> SimpleNamespace:
            residues = tuple((run_dir / ".staging").glob("score-*.tmp"))
            if residues:
                raise RunContractError("score staging was not recovered")
            return snapshot

        with mock.patch(
            "cofactor_bench.cli.verified_run_snapshot_from_config",
            side_effect=reject_unrecovered_score_staging,
        ), mock.patch(
            "cofactor_bench.reporting.score_run", return_value=report
        ), mock.patch(
            "cofactor_bench.reporting.report_json_bytes",
            return_value=metrics_bytes,
        ), mock.patch(
            "cofactor_bench.reporting.render_markdown",
            return_value=markdown_bytes.decode("utf-8"),
        ):
            return cli._score_command(config_path, run_id=snapshot.run_id)

    def test_score_outputs_publish_durably_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.json"
            cli._publish_exact(path, b'{"ok":true}\n')
            cli._publish_exact(path, b'{"ok":true}\n')
            self.assertEqual(path.read_bytes(), b'{"ok":true}\n')
            self.assertEqual(tuple(Path(directory).glob("*.tmp")), ())
            with self.assertRaisesRegex(RunContractError, "different bytes"):
                cli._publish_exact(path, b'{"ok":false}\n')

    def test_score_recovers_metrics_and_report_temp_only_before_snapshot(self) -> None:
        for target_name in ("metrics.json", "report.md"):
            with self.subTest(target_name=target_name), tempfile.TemporaryDirectory() as directory:
                (
                    config_path,
                    run_dir,
                    snapshot,
                    report,
                    metrics_bytes,
                    markdown_bytes,
                ) = self._score_fixture(directory)
                payload = (
                    metrics_bytes if target_name == "metrics.json" else markdown_bytes
                )
                temporary = self._score_staging_path(
                    run_dir, target_name, payload
                )
                temporary.write_bytes(payload)

                self._run_mock_score(
                    config_path=config_path,
                    run_dir=run_dir,
                    snapshot=snapshot,
                    report=report,
                    metrics_bytes=metrics_bytes,
                    markdown_bytes=markdown_bytes,
                )

                self.assertEqual(
                    (run_dir / "metrics.json").read_bytes(), metrics_bytes
                )
                self.assertEqual((run_dir / "report.md").read_bytes(), markdown_bytes)
                self.assertFalse(temporary.exists())

    def test_score_recovers_final_plus_temp_without_touching_other_namespaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (
                config_path,
                run_dir,
                snapshot,
                report,
                metrics_bytes,
                markdown_bytes,
            ) = self._score_fixture(directory)
            score_temporaries: list[Path] = []
            for target_name, payload in (
                ("metrics.json", metrics_bytes),
                ("report.md", markdown_bytes),
            ):
                (run_dir / target_name).write_bytes(payload)
                temporary = self._score_staging_path(
                    run_dir, target_name, payload
                )
                temporary.write_bytes(payload)
                score_temporaries.append(temporary)
            runner_temporary = run_dir / ".staging" / "publish-attempt-1.tmp"
            launcher_temporary = run_dir / ".staging" / "publish-launch-1.tmp"
            runner_temporary.write_bytes(b"runner-owned")
            launcher_temporary.write_bytes(b"launcher-owned")

            self._run_mock_score(
                config_path=config_path,
                run_dir=run_dir,
                snapshot=snapshot,
                report=report,
                metrics_bytes=metrics_bytes,
                markdown_bytes=markdown_bytes,
            )

            self.assertTrue(all(not path.exists() for path in score_temporaries))
            self.assertEqual(runner_temporary.read_bytes(), b"runner-owned")
            self.assertEqual(launcher_temporary.read_bytes(), b"launcher-owned")

    def test_score_completes_pair_when_metrics_exists_and_report_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (
                config_path,
                run_dir,
                snapshot,
                report,
                metrics_bytes,
                markdown_bytes,
            ) = self._score_fixture(directory)
            (run_dir / "metrics.json").write_bytes(metrics_bytes)

            self._run_mock_score(
                config_path=config_path,
                run_dir=run_dir,
                snapshot=snapshot,
                report=report,
                metrics_bytes=metrics_bytes,
                markdown_bytes=markdown_bytes,
            )

            self.assertEqual((run_dir / "metrics.json").read_bytes(), metrics_bytes)
            self.assertEqual((run_dir / "report.md").read_bytes(), markdown_bytes)
            self.assertEqual(
                tuple((run_dir / ".staging").glob("score-*.tmp")), ()
            )

    def test_concurrent_score_fails_closed_before_second_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (
                config_path,
                run_dir,
                snapshot,
                report,
                metrics_bytes,
                markdown_bytes,
            ) = self._score_fixture(directory)
            entered = threading.Event()
            release = threading.Event()
            first_errors: list[BaseException] = []
            score_calls = 0
            score_calls_lock = threading.Lock()

            def score_effect(**_: object) -> mock.Mock:
                nonlocal score_calls
                with score_calls_lock:
                    score_calls += 1
                    current_call = score_calls
                if current_call == 1:
                    entered.set()
                    if not release.wait(timeout=5):
                        raise AssertionError("test did not release first score")
                return report

            def first_score() -> None:
                try:
                    cli._score_command(config_path, run_id="formal-v1")
                except BaseException as error:  # pragma: no cover - asserted below.
                    first_errors.append(error)

            with mock.patch(
                "cofactor_bench.cli.verified_run_snapshot_from_config",
                return_value=snapshot,
            ) as freeze, mock.patch(
                "cofactor_bench.reporting.score_run", side_effect=score_effect
            ), mock.patch(
                "cofactor_bench.reporting.report_json_bytes",
                return_value=metrics_bytes,
            ), mock.patch(
                "cofactor_bench.reporting.render_markdown",
                return_value=markdown_bytes.decode("utf-8"),
            ):
                thread = threading.Thread(target=first_score)
                thread.start()
                self.assertTrue(entered.wait(timeout=5))
                try:
                    with self.assertRaisesRegex(
                        RunContractError, "active|publication"
                    ):
                        cli._score_command(config_path, run_id="formal-v1")
                finally:
                    release.set()
                    thread.join(timeout=5)

            self.assertFalse(thread.is_alive())
            self.assertEqual(first_errors, [])
            self.assertEqual(score_calls, 1)
            self.assertEqual(freeze.call_count, 1)
            self.assertEqual((run_dir / "metrics.json").read_bytes(), metrics_bytes)
            self.assertEqual((run_dir / "report.md").read_bytes(), markdown_bytes)

    def test_score_consumes_one_verified_snapshot_without_live_artifact_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            (
                config_path,
                run_dir,
                snapshot,
                report,
                _,
                _,
            ) = self._score_fixture(directory)
            with mock.patch(
                "cofactor_bench.cli.verified_run_snapshot_from_config",
                return_value=snapshot,
            ) as freeze, mock.patch(
                "cofactor_bench.reporting.score_run", return_value=report
            ) as score, mock.patch(
                "cofactor_bench.reporting.report_json_bytes",
                return_value=b'{"report":true}\n',
            ), mock.patch(
                "cofactor_bench.reporting.render_markdown",
                return_value="# report\n",
            ):
                result = cli._score_command(config_path, run_id="formal-v1")

            freeze.assert_called_once_with(
                config_path=config_path,
                run_id="formal-v1",
                executable=None,
            )
            score.assert_called_once_with(
                verified_run_snapshot=snapshot,
                formal=True,
                expected_case_count=5_337,
            )
            self.assertEqual(
                result["provenance"]["provenance_composite_sha256"], "3" * 64
            )
            self.assertEqual(
                (run_dir / "metrics.json").read_bytes(), b'{"report":true}\n'
            )

    def test_run_command_forwards_explicit_frozen_settings(self) -> None:
        summary = mock.Mock()
        summary.to_dict.return_value = {"run_id": "formal-v1", "is_complete": True}
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch(
            "cofactor_bench.cli.execute_run_from_config", return_value=summary
        ) as execute, redirect_stdout(stdout), redirect_stderr(stderr):
            status = cli.main(
                [
                    "run",
                    "--config",
                    "config/benchmark.json",
                    "--run-id",
                    "formal-v1",
                    "--resume",
                    "--concurrency",
                    "16",
                    "--timeout-seconds",
                    "600",
                    "--circuit-breaker-threshold",
                    "1",
                ]
            )

        self.assertEqual(status, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue())["run_id"], "formal-v1")
        execute.assert_called_once_with(
            config_path="config/benchmark.json",
            run_id="formal-v1",
            resume=True,
            concurrency=16,
            timeout_seconds=600.0,
            circuit_breaker_threshold=1,
            limit=None,
            infrastructure_gate=False,
            executable="codex",
            invocation_argv=(
                sys.executable,
                "-m",
                "cofactor_bench.cli",
                "run",
                "--config",
                "config/benchmark.json",
                "--run-id",
                "formal-v1",
                "--resume",
                "--concurrency",
                "16",
                "--timeout-seconds",
                "600",
                "--circuit-breaker-threshold",
                "1",
            ),
        )

    def test_cli_rejects_any_breaker_other_than_one(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
            cli.main(
                [
                    "run",
                    "--run-id",
                    "formal-v1",
                    "--circuit-breaker-threshold",
                    "2",
                ]
            )
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("invalid choice", stderr.getvalue())

    def test_interrupted_run_returns_nonzero_with_resumable_progress(self) -> None:
        interruption = RunAborted(
            error_code="CAPACITY_ERROR",
            threshold=1,
            completed_results=(),
            failures=(),
            pending_sample_ids=("sample_" + "1" * 32,),
        )
        progress = mock.Mock()
        progress.to_dict.return_value = {
            "terminal_count": 2,
            "missing_terminal_count": 5335,
            "is_complete": False,
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch(
            "cofactor_bench.cli.execute_run_from_config", side_effect=interruption
        ), mock.patch(
            "cofactor_bench.cli.inspect_run_progress_from_config",
            return_value=progress,
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            status = cli.main(["run", "--run-id", "formal-v1"])

        self.assertEqual(status, 3)
        self.assertEqual(stdout.getvalue(), "")
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["status"], "run_interrupted")
        self.assertEqual(payload["pending_case_count"], 1)
        self.assertEqual(payload["progress"]["terminal_count"], 2)

    def test_validate_run_requires_explicit_run_id(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
            status = cli.main(["validate", "--stage", "run"])
        self.assertEqual(status, 1)
        self.assertIn("requires --run-id", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
