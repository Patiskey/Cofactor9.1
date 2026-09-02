from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import cofactor_bench.runner as runner_module
from cofactor_bench.prompt import (
    CatalogTerm,
    PromptCase,
    create_prompt_case,
    render_prompt,
)
from cofactor_bench.runner import (
    AttemptReplayResult,
    CodexExecRunner,
    CompletedCaseError,
    DISABLED_FEATURES,
    MAX_CONCURRENCY,
    RunAborted,
    RunCancelled,
    RunCasesError,
    RunnerError,
    SystematicFailure,
    build_codex_argv,
    replay_codex_attempt,
)


FAKE_CODEX = r'''#!/usr/bin/env python3
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

executable = Path(__file__)
executable.with_suffix(".process-started").write_text(str(os.getpid()))
scenario_path = executable.with_suffix(".scenario.json")
calls_path = executable.with_suffix(".calls.jsonl")
scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
prompt = sys.stdin.read()
start = "BEGIN_CASE_JSON\n"
end = "\nEND_CASE_JSON"
payload = json.loads(prompt.split(start, 1)[1].split(end, 1)[0])
sample_id = payload["sample_id"]

forbidden_markers = ("KEY", "TOKEN", "SECRET", "PASSWORD")
with calls_path.open("a+", encoding="utf-8") as handle:
    fcntl.flock(handle, fcntl.LOCK_EX)
    handle.seek(0)
    call_number = len(handle.read().splitlines()) + 1
    kinds = scenario.get("kinds", ["success"])
    kind = scenario.get("kind_by_sample", {}).get(
        sample_id,
        kinds[min(call_number - 1, len(kinds) - 1)],
    )
    observed = {
        "args": sys.argv[1:],
        "cwd": os.getcwd(),
        "entries_before": sorted(os.listdir(".")),
        "prompt": prompt,
        "sample_id": sample_id,
        "sensitive_env_names": sorted(
            name for name in os.environ
            if any(marker in name.upper() for marker in forbidden_markers)
        ),
    }
    handle.seek(0, os.SEEK_END)
    handle.write(json.dumps(observed, sort_keys=True) + "\n")
    handle.flush()
    fcntl.flock(handle, fcntl.LOCK_UN)

activity_path = executable.with_suffix(".activity.json")
if scenario.get("track_activity"):
    with activity_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        raw_state = handle.read()
        state = json.loads(raw_state) if raw_state else {"active": 0, "max_active": 0}
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(state, sort_keys=True))
        handle.flush()
        fcntl.flock(handle, fcntl.LOCK_UN)

delay = scenario.get("delay_seconds_by_sample", {}).get(sample_id, 0)
if delay:
    time.sleep(delay)

prediction = {
    "schema_version": "cofactor9.1.response.v2",
    "sample_id": sample_id,
    "status": "predict",
    "predicted_cofactors": [payload["label_catalog"]["terms"][0]["chebi_id"]],
    "primary_guess": payload["label_catalog"]["terms"][0]["chebi_id"],
    "confidence_complete": 0.75,
}
if kind == "mismatch":
    prediction["sample_id"] = "sample_00000000000000000000000000000000"

events = [
    {"type": "thread.started", "thread_id": "fake-thread"},
    {"type": "turn.started"},
]
if kind in {"tool", "tool_exit"}:
    events.append({
        "type": "item.completed",
        "item": {"id": "item-tool", "type": "command_execution", "command": "pwd"},
    })
elif kind == "unknown":
    events.append({"type": "future.event", "payload": {}})
elif kind == "timeout":
    marker = executable.with_suffix(".child-terminated")
    ready = executable.with_suffix(".child-ready")
    child_code = (
        "import pathlib,signal,time;"
        f"p=pathlib.Path({str(marker)!r});"
        f"r=pathlib.Path({str(ready)!r});"
        "signal.signal(signal.SIGTERM,lambda *_:(p.write_text('terminated'),exit(0)));"
        "r.write_text('ready');"
        "time.sleep(60)"
    )
    subprocess.Popen([sys.executable, "-c", child_code])
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    time.sleep(60)
elif kind == "escaped_pipe":
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        start_new_session=True,
    )
    executable.with_suffix(".escaped-pid").write_text(str(child.pid))
    time.sleep(60)
elif kind == "lingering_descendant":
    marker = executable.with_suffix(".lingering-terminated")
    ready = executable.with_suffix(".lingering-ready")
    child_code = (
        "import pathlib,signal,time;"
        f"p=pathlib.Path({str(marker)!r});"
        f"r=pathlib.Path({str(ready)!r});"
        "signal.signal(signal.SIGTERM,lambda *_:(p.write_text('terminated'),exit(0)));"
        "r.write_text('ready');"
        "time.sleep(0.75);"
        "print('late-descendant-pollution',flush=True)"
    )
    subprocess.Popen([sys.executable, "-c", child_code])
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    events.append({
        "type": "item.completed",
        "item": {
            "id": "item-message",
            "type": "agent_message",
            "text": json.dumps(prediction, sort_keys=True),
        },
    })
elif kind == "auth":
    print("authentication failed: unauthorized API credential", file=sys.stderr)
    raise SystemExit(1)
elif kind == "capacity":
    print("service overloaded: rate limit 429", file=sys.stderr)
    raise SystemExit(1)
elif kind == "transport":
    print("network connection reset by peer", file=sys.stderr)
    raise SystemExit(1)
elif kind == "truncated":
    for event in events:
        print(json.dumps(event), flush=True)
    sys.stdout.write('{"type":"item.completed","item":')
    sys.stdout.flush()
    raise SystemExit(0)
else:
    events.append({
        "type": "item.completed",
        "item": {
            "id": "item-message",
            "type": "agent_message",
            "text": json.dumps(prediction, sort_keys=True),
        },
    })

usage = {"input_tokens": 11, "cached_input_tokens": 0, "output_tokens": 7}
if kind == "nonfinite":
    usage["input_tokens"] = float("nan")
events.append({
    "type": "turn.completed",
    "usage": usage,
})
for event in events:
    print(json.dumps(event, sort_keys=True), flush=True)
if scenario.get("track_activity"):
    with activity_path.open("r+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        state = json.loads(handle.read())
        state["active"] -= 1
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(state, sort_keys=True))
        handle.flush()
        fcntl.flock(handle, fcntl.LOCK_UN)
if kind == "tool_exit":
    raise SystemExit(7)
'''


def label_catalog() -> tuple[CatalogTerm, ...]:
    return tuple(
        CatalogTerm(f"CHEBI:{index}", f"frozen cofactor {index}")
        for index in range(1, 105)
    )


class CodexExecRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.fake_codex = self.base / "fake-codex"
        self.fake_codex.write_text(FAKE_CODEX, encoding="utf-8")
        self.fake_codex.chmod(0o700)
        self.run_dir = self.base / "run"
        self.schema_path = (
            Path(__file__).parents[1] / "schemas" / "model-response.schema.json"
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def make_case(self, sequence: str = "MSEQUENCEUX") -> PromptCase:
        return create_prompt_case(
            sequence=sequence,
            catalog_terms=label_catalog(),
            catalog_version="uniprot-2026_02-chebi-v1",
        )

    def set_scenario(self, *kinds: str) -> None:
        self.fake_codex.with_suffix(".scenario.json").write_text(
            json.dumps({"kinds": list(kinds)}),
            encoding="utf-8",
        )

    def set_case_scenarios(
        self,
        cases: tuple[PromptCase, ...],
        kinds: tuple[str, ...],
        *,
        delays: tuple[float, ...] | None = None,
        track_activity: bool = False,
    ) -> None:
        scenario: dict[str, object] = {
            "kind_by_sample": {
                case.sample_id: kind for case, kind in zip(cases, kinds, strict=True)
            },
            "track_activity": track_activity,
        }
        if delays is not None:
            scenario["delay_seconds_by_sample"] = {
                case.sample_id: delay
                for case, delay in zip(cases, delays, strict=True)
            }
        self.fake_codex.with_suffix(".scenario.json").write_text(
            json.dumps(scenario),
            encoding="utf-8",
        )

    def calls(self) -> list[dict[str, object]]:
        path = self.fake_codex.with_suffix(".calls.jsonl")
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def make_runner(
        self,
        *,
        max_attempts: int = 3,
        timeout_seconds: float = 10.0,
        circuit_breaker_threshold: int = 1,
    ) -> CodexExecRunner:
        return CodexExecRunner(
            run_dir=self.run_dir,
            executable=self.fake_codex,
            schema_path=self.schema_path,
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
            circuit_breaker_threshold=circuit_breaker_threshold,
        )

    def case_directory(self, case: PromptCase) -> Path:
        return self.run_dir / "cases" / case.sample_id

    def test_success_uses_fixed_hardened_argv_and_persists_two_layer_result(self) -> None:
        case = self.make_case()
        self.set_scenario("success")

        result = self.make_runner().run_case(case)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.attempt_count, 1)
        self.assertEqual(result.prediction.sample_id, case.sample_id)
        call = self.calls()[0]
        expected_argv = build_codex_argv(
            executable=self.fake_codex,
            schema_path=self.schema_path,
            working_directory=Path(call["cwd"]),
        )
        self.assertEqual([str(self.fake_codex), *call["args"]], expected_argv)
        self.assertEqual(call["prompt"], render_prompt(case))
        self.assertEqual(call["entries_before"], [])
        self.assertEqual(call["sensitive_env_names"], [])
        self.assertEqual(Path(call["cwd"]).parent, Path("/private/tmp"))
        self.assertFalse(Path(call["cwd"]).exists())

        case_directory = self.case_directory(case)
        terminal = json.loads(
            (case_directory / "terminal.json").read_text(encoding="utf-8")
        )
        self.assertEqual(terminal["status"], "success")
        self.assertEqual(terminal["prediction"]["sample_id"], case.sample_id)
        attempt = case_directory / "attempts" / "attempt-0001"
        self.assertTrue((attempt / "prompt.txt").is_file())
        self.assertTrue((attempt / "stdout.jsonl").is_file())
        self.assertTrue((attempt / "attempt.json").is_file())

    def test_public_replay_reuses_runtime_classification_without_process_execution(
        self,
    ) -> None:
        success_case = self.make_case("MREPLAYSUCCESS")
        failure_case = self.make_case("MREPLAYFAILURE")
        runner = self.make_runner(max_attempts=1)
        self.set_scenario("success")
        success = runner.run_case(success_case)
        self.set_scenario("mismatch")
        failure = runner.run_case(failure_case)

        def replay(case: PromptCase) -> AttemptReplayResult:
            attempt = self.case_directory(case) / "attempts" / "attempt-0001"
            record = json.loads((attempt / "attempt.json").read_text())
            return replay_codex_attempt(
                case=case,
                argv=record["argv"],
                stdout=(attempt / "stdout.jsonl").read_text(),
                stderr=(attempt / "stderr.txt").read_text(),
                returncode=record["returncode"],
                timed_out=record["timed_out"],
                cancelled=False,
                start_error=None,
                duration_seconds=record["duration_seconds"],
            )

        calls_before_replay = len(self.calls())
        with mock.patch.object(
            runner_module.subprocess,
            "Popen",
            side_effect=AssertionError("replay must never execute a process"),
        ):
            replayed_success = replay(success_case)
            replayed_failure = replay(failure_case)

        self.assertIsInstance(replayed_success, AttemptReplayResult)
        self.assertEqual(replayed_success.prediction, success.prediction)
        self.assertEqual(replayed_success.error_code, None)
        self.assertEqual(replayed_success.retry_disposition, "none")
        self.assertEqual(replayed_success.thread_id, "fake-thread")
        self.assertEqual(replayed_success.usage["input_tokens"], 11)
        self.assertIsNone(replayed_failure.prediction)
        self.assertEqual(replayed_failure.error_code, failure.error_code)
        self.assertEqual(replayed_failure.error_message, failure.error_message)
        self.assertEqual(replayed_failure.retry_disposition, "retryable")
        self.assertIsNone(replayed_failure.thread_id)
        self.assertEqual(replayed_failure.usage, {})
        self.assertEqual(len(self.calls()), calls_before_replay)

    def test_fixed_command_disables_every_audited_tool_surface(self) -> None:
        argv = build_codex_argv(
            executable=self.fake_codex,
            schema_path=self.schema_path,
            working_directory=Path("/private/tmp/fresh-case"),
        )

        disabled = [
            argv[index + 1]
            for index, value in enumerate(argv[:-1])
            if value == "--disable"
        ]
        self.assertEqual(tuple(disabled), DISABLED_FEATURES)
        self.assertNotIn("web_search_cached", disabled)
        self.assertNotIn("web_search_request", disabled)
        self.assertIn("sleep_tool", disabled)
        self.assertIn("tool_suggest", disabled)
        self.assertIn("code_mode_host", disabled)
        self.assertIn("in_app_browser", disabled)
        self.assertIn("--ignore-user-config", argv)
        self.assertIn("--ignore-rules", argv)
        self.assertIn("--strict-config", argv)
        self.assertIn("--ephemeral", argv)
        self.assertIn("--skip-git-repo-check", argv)
        self.assertIn("read-only", argv)
        self.assertIn('model_reasoning_effort="max"', argv)
        self.assertIn('service_tier="fast"', argv)
        self.assertIn('approval_policy="never"', argv)
        self.assertIn('web_search="disabled"', argv)
        self.assertEqual(argv[-1], "-")

    def test_tool_event_is_nonretryable_pollution(self) -> None:
        case = self.make_case()
        self.set_scenario("tool", "success")

        result = self.make_runner().run_case(case)

        self.assertEqual(result.status, "terminal_error")
        self.assertEqual(result.error_code, "TOOL_POLLUTION")
        self.assertEqual(result.attempt_count, 1)
        self.assertEqual(len(self.calls()), 1)

    def test_tool_pollution_takes_priority_over_nonzero_process_exit(self) -> None:
        case = self.make_case()
        self.set_scenario("tool_exit", "success")

        result = self.make_runner().run_case(case)

        self.assertEqual(result.status, "terminal_error")
        self.assertEqual(result.error_code, "TOOL_POLLUTION")
        self.assertEqual(result.attempt_count, 1)
        self.assertEqual(len(self.calls()), 1)

    def test_unknown_outer_event_is_nonretryable_pollution(self) -> None:
        case = self.make_case()
        self.set_scenario("unknown", "success")

        result = self.make_runner().run_case(case)

        self.assertEqual(result.status, "terminal_error")
        self.assertEqual(result.error_code, "UNKNOWN_EVENT")
        self.assertEqual(result.attempt_count, 1)

    def test_truncated_stdout_is_preserved_then_retried(self) -> None:
        case = self.make_case()
        self.set_scenario("truncated", "success")

        result = self.make_runner().run_case(case)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.attempt_count, 2)
        attempts = self.case_directory(case) / "attempts"
        first = json.loads(
            (attempts / "attempt-0001" / "attempt.json").read_text()
        )
        self.assertEqual(first["error_code"], "TRUNCATED_STDOUT")
        self.assertEqual(first["retry_disposition"], "retryable")
        self.assertTrue(
            (attempts / "attempt-0001" / "stdout.jsonl").read_text().endswith(
                '"item":'
            )
        )
        self.assertEqual(len(self.calls()), 2)

    def test_nonstandard_nonfinite_outer_json_is_rejected(self) -> None:
        case = self.make_case()
        self.set_scenario("nonfinite")

        result = self.make_runner().run_case(case)

        self.assertEqual(result.status, "terminal_error")
        self.assertEqual(result.error_code, "INVALID_EVENT_STREAM")
        self.assertEqual(result.attempt_count, 3)

    def test_case_mismatch_retries_no_more_than_three_attempts(self) -> None:
        case = self.make_case()
        self.set_scenario("mismatch")

        result = self.make_runner().run_case(case)

        self.assertEqual(result.status, "terminal_error")
        self.assertEqual(result.error_code, "CASE_MISMATCH")
        self.assertEqual(result.attempt_count, 3)
        self.assertEqual(len(self.calls()), 3)

    def test_resume_skips_success_and_does_not_overwrite_terminal(self) -> None:
        case = self.make_case()
        self.set_scenario("success")
        runner = self.make_runner()
        first = runner.run_case(case)
        terminal_path = self.case_directory(case) / "terminal.json"
        original_terminal = terminal_path.read_bytes()

        resumed = runner.run_case(case, resume=True)

        self.assertEqual(resumed, first)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(terminal_path.read_bytes(), original_terminal)
        with self.assertRaises(CompletedCaseError):
            runner.run_case(case, resume=False)

    def test_resume_rejects_a_different_prompt_with_the_same_sample_id(self) -> None:
        case = self.make_case()
        self.set_scenario("success")
        runner = self.make_runner()
        runner.run_case(case)
        mismatched_case = PromptCase(
            sample_id=case.sample_id,
            sequence="MDIFFERENTSEQUENCE",
            catalog_version=case.catalog_version,
            catalog_terms=case.catalog_terms,
        )

        with self.assertRaisesRegex(RunnerError, "prompt"):
            runner.run_case(mismatched_case, resume=True)

        self.assertEqual(len(self.calls()), 1)

    def test_resume_seals_legacy_prompt_only_attempt_then_safely_retries(self) -> None:
        case = self.make_case()
        self.set_scenario("success")
        interrupted = (
            self.case_directory(case) / "attempts" / "attempt-0001"
        )
        interrupted.mkdir(parents=True)
        (interrupted / "prompt.txt").write_text(render_prompt(case))

        result = self.make_runner().run_case(case, resume=True)

        self.assertEqual(result.status, "success")
        self.assertTrue(interrupted.exists())
        incident = (
            self.run_dir
            / "transport-incidents"
            / case.sample_id
            / "incident-0001"
        )
        self.assertEqual(
            (incident / "prompt.txt").read_bytes(),
            render_prompt(case).encode("utf-8"),
        )
        self.assertEqual((incident / "stdout.jsonl").read_bytes(), b"")
        self.assertEqual((incident / "stderr.txt").read_bytes(), b"")
        interrupted_record = json.loads(
            (incident / "incident.json").read_text(encoding="utf-8")
        )
        self.assertEqual(interrupted_record["tentative_attempt_number"], 1)
        self.assertEqual(interrupted_record["error_code"], "INTERRUPTED_ATTEMPT")
        self.assertEqual(interrupted_record["retry_disposition"], "retryable")
        self.assertEqual(interrupted_record["usage"], {})
        self.assertIsNone(interrupted_record["returncode"])
        self.assertIsNone(interrupted_record["prediction_sha256"])
        self.assertTrue((self.case_directory(case) / "terminal.json").exists())
        self.assertEqual(len(self.calls()), 1)

    def test_resume_rejects_an_interrupted_attempt_for_another_prompt(self) -> None:
        case = self.make_case()
        other_case = PromptCase(
            sample_id=case.sample_id,
            sequence="MDIFFERENTSEQUENCE",
            catalog_version=case.catalog_version,
            catalog_terms=case.catalog_terms,
        )
        self.set_scenario("success")
        interrupted = (
            self.case_directory(case) / "attempts" / "attempt-0001"
        )
        interrupted.mkdir(parents=True)
        (interrupted / "prompt.txt").write_text(render_prompt(other_case))

        with self.assertRaisesRegex(RunnerError, "prompt"):
            self.make_runner().run_case(case, resume=True)

        self.assertEqual(self.calls(), [])

    def test_resume_seals_trailing_partial_outputs_without_overwriting_bytes(self) -> None:
        case = self.make_case()
        self.set_scenario("success")
        interrupted = self.case_directory(case) / "attempts" / "attempt-0001"
        interrupted.mkdir(parents=True)
        prompt_bytes = render_prompt(case).encode("utf-8")
        stdout_bytes = b'{"type":"thread.started"}\npartial-tail'
        prediction_bytes = b'{"preserved":"uncommitted"}\n'
        (interrupted / "prompt.txt").write_bytes(prompt_bytes)
        (interrupted / "stdout.jsonl").write_bytes(stdout_bytes)
        (interrupted / "prediction.json").write_bytes(prediction_bytes)

        result = self.make_runner().run_case(case, resume=True)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.attempt_count, 1)
        self.assertIsNone(
            json.loads((interrupted / "attempt.json").read_text())["error_code"]
        )
        incident = (
            self.run_dir
            / "transport-incidents"
            / case.sample_id
            / "incident-0001"
        )
        self.assertEqual((incident / "prompt.txt").read_bytes(), prompt_bytes)
        self.assertEqual((incident / "stdout.jsonl").read_bytes(), stdout_bytes)
        self.assertEqual(
            (incident / "prediction.json").read_bytes(), prediction_bytes
        )
        self.assertEqual((incident / "stderr.txt").read_bytes(), b"")
        record = json.loads((incident / "incident.json").read_text())
        self.assertEqual(record["error_code"], "INTERRUPTED_ATTEMPT")
        self.assertEqual(
            record["stdout_sha256"], runner_module._sha256_bytes(stdout_bytes)
        )
        self.assertEqual(
            record["prediction_sha256"],
            runner_module._sha256_bytes(prediction_bytes),
        )
        self.assertEqual(len(self.calls()), 1)

    def test_resume_finalizes_legacy_incident_marker_after_publish_crash(self) -> None:
        case = self.make_case("MLEGACYCRASH")
        self.set_scenario("success")
        interrupted = self.case_directory(case) / "attempts" / "attempt-0001"
        interrupted.mkdir(parents=True)
        prompt_bytes = render_prompt(case).encode("utf-8")
        stdout_bytes = b'{"type":"thread.started"}\n'
        (interrupted / "prompt.txt").write_bytes(prompt_bytes)
        (interrupted / "stdout.jsonl").write_bytes(stdout_bytes)

        runner = self.make_runner()
        with mock.patch.object(
            runner_module,
            "_publish_directory_exclusive",
            side_effect=OSError("simulated crash before directory publish"),
        ):
            with self.assertRaisesRegex(OSError, "simulated crash"):
                runner.run_case(case, resume=True)

        self.assertTrue((interrupted / "incident.json").is_file())
        self.assertEqual((interrupted / "prompt.txt").read_bytes(), prompt_bytes)
        self.assertEqual((interrupted / "stdout.jsonl").read_bytes(), stdout_bytes)
        result = runner.run_case(case, resume=True)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.attempt_count, 1)
        self.assertIsNone(
            json.loads((interrupted / "attempt.json").read_text())["error_code"]
        )
        self.assertTrue(
            (
                self.run_dir
                / "transport-incidents"
                / case.sample_id
                / "incident-0001"
                / "incident.json"
            ).is_file()
        )
        self.assertEqual(len(self.calls()), 1)

    def test_resume_never_overwrites_a_truncated_attempt_completion_marker(self) -> None:
        case = self.make_case()
        self.set_scenario("success")
        interrupted = self.case_directory(case) / "attempts" / "attempt-0001"
        interrupted.mkdir(parents=True)
        (interrupted / "prompt.txt").write_text(render_prompt(case))
        (interrupted / "stdout.jsonl").write_text("partial")
        (interrupted / "stderr.txt").write_text("")
        original_marker = b'{"schema_version":"truncated"'
        (interrupted / "attempt.json").write_bytes(original_marker)

        with self.assertRaisesRegex(RunnerError, "attempt completion marker"):
            self.make_runner().run_case(case, resume=True)

        self.assertEqual(
            (interrupted / "attempt.json").read_bytes(), original_marker
        )
        self.assertEqual(self.calls(), [])

    def test_resume_rebuilds_success_terminal_from_complete_final_attempt(self) -> None:
        case = self.make_case()
        self.set_scenario("success")
        runner = self.make_runner()
        expected = runner.run_case(case)
        terminal_path = self.case_directory(case) / "terminal.json"
        terminal_path.unlink()
        attempt_bytes = (
            self.case_directory(case)
            / "attempts"
            / "attempt-0001"
            / "attempt.json"
        ).read_bytes()

        resumed = runner.run_case(case, resume=True)

        self.assertEqual(resumed, expected)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(
            (
                self.case_directory(case)
                / "attempts"
                / "attempt-0001"
                / "attempt.json"
            ).read_bytes(),
            attempt_bytes,
        )

    def test_resume_atomically_finalizes_complete_staged_attempt_without_recall(self) -> None:
        case = self.make_case("MSTAGE")
        self.set_scenario("success")
        runner = self.make_runner()
        expected = runner.run_case(case)
        terminal_path = self.case_directory(case) / "terminal.json"
        terminal_path.unlink()
        attempt = self.case_directory(case) / "attempts" / "attempt-0001"
        inflight = self.run_dir / ".inflight" / case.sample_id
        inflight.parent.mkdir(parents=True, exist_ok=True)
        os.rename(attempt, inflight)

        resumed = runner.run_case(case, resume=True)

        self.assertEqual(resumed, expected)
        self.assertTrue(attempt.is_dir())
        self.assertFalse(inflight.exists())
        self.assertEqual(len(self.calls()), 1)

    def test_resume_rebuilds_third_attempt_success_without_exhaustion_error(self) -> None:
        case = self.make_case()
        self.set_scenario("mismatch", "mismatch", "success")
        runner = self.make_runner()
        expected = runner.run_case(case)
        terminal_path = self.case_directory(case) / "terminal.json"
        terminal_path.unlink()

        resumed = runner.run_case(case, resume=True)

        self.assertEqual(resumed, expected)
        self.assertEqual(resumed.status, "success")
        self.assertEqual(resumed.attempt_count, 3)
        self.assertEqual(len(self.calls()), 3)
        terminal = json.loads(terminal_path.read_text())
        self.assertIsNone(terminal["error_code"])

    def test_resume_rebuilds_exhausted_error_from_final_attempt_code(self) -> None:
        case = self.make_case()
        self.set_scenario("mismatch")
        runner = self.make_runner()
        expected = runner.run_case(case)
        terminal_path = self.case_directory(case) / "terminal.json"
        terminal_path.unlink()

        resumed = runner.run_case(case, resume=True)

        self.assertEqual(resumed, expected)
        self.assertEqual(resumed.error_code, "CASE_MISMATCH")
        self.assertNotEqual(resumed.error_code, "ATTEMPTS_EXHAUSTED")
        self.assertEqual(len(self.calls()), 3)

    def test_resume_rejects_tampered_completed_attempt_argv(self) -> None:
        case = self.make_case("MARGX")
        self.set_scenario("mismatch")
        runner = self.make_runner(max_attempts=1)
        runner.run_case(case)
        terminal_path = self.case_directory(case) / "terminal.json"
        terminal_path.unlink()
        attempt_path = (
            self.case_directory(case)
            / "attempts"
            / "attempt-0001"
            / "attempt.json"
        )
        payload = json.loads(attempt_path.read_text())
        payload["argv"] = [str(self.fake_codex), "exec", "--unsafe-tamper"]
        attempt_path.write_text(json.dumps(payload) + "\n")

        with self.assertRaisesRegex(RunnerError, "argv"):
            runner.run_case(case, resume=True)

        self.assertEqual(len(self.calls()), 1)

    def test_resume_replays_timeout_instead_of_trusting_attempt_classification(
        self,
    ) -> None:
        case = self.make_case("MREPLAYTIMEOUT")
        runner = self.make_runner(timeout_seconds=0.5)
        self.set_scenario("timeout")
        with self.assertRaises(RunAborted):
            runner.run_cases((case,), concurrency=1)
        marker = (
            self.case_directory(case)
            / "attempts"
            / "attempt-0001"
            / "attempt.json"
        )
        tampered = json.loads(marker.read_text())
        tampered.update(
            {
                "error_code": "TOOL_POLLUTION",
                "error_message": "forged nonretryable classification",
                "retry_disposition": "nonretryable",
            }
        )
        marker.write_text(json.dumps(tampered, sort_keys=True) + "\n")
        calls_before_resume = len(self.calls())
        self.set_scenario("success")

        with self.assertRaisesRegex(RunnerError, "replay"):
            runner.run_case(case, resume=True)

        self.assertFalse((self.case_directory(case) / "terminal.json").exists())
        self.assertEqual(len(self.calls()), calls_before_resume)

    def test_resume_strictly_audits_attempt_metadata_and_duplicate_keys(self) -> None:
        case = self.make_case("MSTRICTATTEMPT")
        runner = self.make_runner(max_attempts=1)
        self.set_scenario("mismatch")
        runner.run_case(case)
        terminal = self.case_directory(case) / "terminal.json"
        terminal.unlink()
        marker = (
            self.case_directory(case)
            / "attempts"
            / "attempt-0001"
            / "attempt.json"
        )
        original_bytes = marker.read_bytes()
        original = json.loads(original_bytes)
        mutations = {
            "started timestamp type": {"started_at": None},
            "completion precedes start": {
                "completed_at": "1970-01-01T00:00:00Z"
            },
            "nonfinite duration": {"duration_seconds": float("nan")},
            "missing returncode": {"returncode": None},
            "forged timeout": {"timed_out": True},
            "forged thread": {"thread_id": "forged-thread"},
            "forged usage": {"usage": {"input_tokens": 999}},
        }
        for label, mutation in mutations.items():
            with self.subTest(label=label):
                marker.write_text(
                    json.dumps({**original, **mutation}, sort_keys=True) + "\n"
                )
                with self.assertRaises(RunnerError):
                    runner.run_case(case, resume=True)
                terminal.unlink(missing_ok=True)
                marker.write_bytes(original_bytes)

        duplicate = original_bytes.rstrip()[:-1] + b',"error_code":"CASE_MISMATCH"}\n'
        marker.write_bytes(duplicate)
        with self.assertRaisesRegex(RunnerError, "duplicate"):
            runner.run_case(case, resume=True)
        self.assertFalse(terminal.exists())
        self.assertEqual(len(self.calls()), 1)

    def test_resume_rejects_noncanonical_saved_prediction_bytes(self) -> None:
        case = self.make_case("MPREDBYTES")
        runner = self.make_runner()
        self.set_scenario("success")
        runner.run_case(case)
        terminal = self.case_directory(case) / "terminal.json"
        terminal.unlink()
        prediction_path = (
            self.case_directory(case)
            / "attempts"
            / "attempt-0001"
            / "prediction.json"
        )
        prediction = json.loads(prediction_path.read_text())
        prediction_path.write_text(json.dumps(prediction, indent=2) + "\n")

        with self.assertRaisesRegex(RunnerError, "prediction.*bytes"):
            runner.run_case(case, resume=True)

        self.assertFalse(terminal.exists())
        self.assertEqual(len(self.calls()), 1)

    def test_resume_existing_terminal_is_strictly_linked_to_replayed_attempt(
        self,
    ) -> None:
        case = self.make_case("MTERMINALLINK")
        runner = self.make_runner()
        self.set_scenario("success")
        runner.run_case(case)
        attempt_path = (
            self.case_directory(case)
            / "attempts"
            / "attempt-0001"
            / "attempt.json"
        )
        original_attempt = attempt_path.read_bytes()
        forged_attempt = json.loads(original_attempt)
        forged_attempt["thread_id"] = "forged-thread"
        attempt_path.write_text(json.dumps(forged_attempt, sort_keys=True) + "\n")

        with self.assertRaisesRegex(RunnerError, "replay"):
            runner.run_case(case, resume=True)

        attempt_path.write_bytes(original_attempt)
        terminal_path = self.case_directory(case) / "terminal.json"
        terminal = json.loads(terminal_path.read_text())
        terminal.update(
            {
                "status": "terminal_error",
                "prediction": None,
                "error_code": "TOOL_POLLUTION",
                "error_message": "forged terminal",
            }
        )
        terminal_path.write_text(json.dumps(terminal, sort_keys=True) + "\n")

        with self.assertRaisesRegex(RunnerError, "terminal.*attempt"):
            runner.run_case(case, resume=True)

        self.assertEqual(len(self.calls()), 1)

    def test_resume_fails_closed_while_pre_crash_process_group_may_be_alive(self) -> None:
        case = self.make_case()
        self.set_scenario("success")
        interrupted = self.case_directory(case) / "attempts" / "attempt-0001"
        interrupted.mkdir(parents=True)
        (interrupted / "prompt.txt").write_text(render_prompt(case))
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        active_root = self.run_dir / ".active"
        active_root.mkdir(parents=True)
        marker = runner_module._active_marker_path(
            self.run_dir, case.sample_id, 1
        )
        marker.write_text(
            json.dumps(
                {
                    "schema_version": "cofactor9.1.active-process.v1",
                    "sample_id": case.sample_id,
                    "attempt_number": 1,
                    "pid": process.pid,
                    "prompt_sha256": runner_module._sha256_text(render_prompt(case)),
                },
                sort_keys=True,
            )
            + "\n"
        )
        try:
            with self.assertRaisesRegex(RunnerError, "process group.*alive"):
                self.make_runner().run_case(case, resume=True)
            self.assertEqual(self.calls(), [])
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)

        resumed = self.make_runner().run_case(case, resume=True)
        self.assertEqual(resumed.status, "success")
        self.assertEqual(resumed.attempt_count, 1)
        self.assertFalse(marker.exists())
        self.assertIsNone(
            json.loads((interrupted / "attempt.json").read_text())["error_code"]
        )
        self.assertTrue(
            (
                self.run_dir
                / "transport-incidents"
                / case.sample_id
                / "incident-0001"
                / "incident.json"
            ).is_file()
        )
        self.assertEqual(len(self.calls()), 1)

    def test_resume_recovers_dead_reaped_inflight_as_nonbudget_incident(self) -> None:
        case = self.make_case("MDEAD")
        self.set_scenario("success")
        inflight = self.run_dir / ".inflight" / case.sample_id
        inflight.mkdir(parents=True)
        (inflight / "prompt.txt").write_text(render_prompt(case))
        active = runner_module._active_marker_path(self.run_dir, case.sample_id, 1)
        active.parent.mkdir(parents=True)
        active.write_text(
            json.dumps(
                {
                    "schema_version": "cofactor9.1.active-process.v1",
                    "sample_id": case.sample_id,
                    "attempt_number": 1,
                    "pid": 999_999_999,
                    "prompt_sha256": runner_module._sha256_text(render_prompt(case)),
                },
                sort_keys=True,
            )
            + "\n"
        )

        result = self.make_runner().run_case(case, resume=True)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.attempt_count, 1)
        incident = json.loads(
            (
                self.run_dir
                / "transport-incidents"
                / case.sample_id
                / "incident-0001"
                / "incident.json"
            ).read_text()
        )
        self.assertEqual(incident["error_code"], "INTERRUPTED_ATTEMPT")
        self.assertFalse(active.exists())
        self.assertEqual(len(self.calls()), 1)

    def test_resume_after_spawn_before_marker_retries_without_duplicate_invocation(
        self,
    ) -> None:
        case = self.make_case("MGATEWINDOW")
        self.set_scenario("success")
        child_ready = self.base / "spawned-before-marker.ready"
        child_pid = os.fork()
        if child_pid == 0:
            real_popen = runner_module.subprocess.Popen

            def spawn_then_kill_parent(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                child_ready.write_text(str(process.pid), encoding="utf-8")
                os.kill(os.getpid(), signal.SIGKILL)
                return process

            with mock.patch.object(
                runner_module.subprocess,
                "Popen",
                side_effect=spawn_then_kill_parent,
            ):
                self.make_runner().run_case(case)
            os._exit(97)

        _, wait_status = os.waitpid(child_pid, 0)
        self.assertTrue(os.WIFSIGNALED(wait_status))
        self.assertEqual(os.WTERMSIG(wait_status), signal.SIGKILL)
        self.assertTrue(child_ready.is_file())

        result = self.make_runner().run_case(case, resume=True)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.attempt_count, 1)
        self.assertEqual(len(self.calls()), 1)
        incident = json.loads(
            (
                self.run_dir
                / "transport-incidents"
                / case.sample_id
                / "incident-0001"
                / "incident.json"
            ).read_text()
        )
        self.assertEqual(incident["error_code"], "INTERRUPTED_ATTEMPT")

    def test_parent_sigkill_after_model_start_raw_replays_durable_capture_once(
        self,
    ) -> None:
        case = self.make_case("MDURABLECAPTURE")
        self.set_case_scenarios(
            (case,),
            ("success",),
            delays=(1.0,),
        )
        runner_pid = os.fork()
        if runner_pid == 0:
            self.make_runner().run_case(case)
            os._exit(96)

        deadline = time.monotonic() + 5.0
        while len(self.calls()) != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(self.calls()), 1, "model invocation did not start")
        os.kill(runner_pid, signal.SIGKILL)
        _, wait_status = os.waitpid(runner_pid, 0)
        self.assertTrue(os.WIFSIGNALED(wait_status))
        self.assertEqual(os.WTERMSIG(wait_status), signal.SIGKILL)

        with self.assertRaisesRegex(RunnerError, "process group.*alive"):
            self.make_runner().run_case(case, resume=True)

        completion = runner_module._launch_completion_path(
            self.run_dir, case.sample_id, 1
        )
        deadline = time.monotonic() + 5.0
        while not completion.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(completion.is_file(), "launcher did not persist completion")

        active = runner_module._active_marker_path(self.run_dir, case.sample_id, 1)
        deadline = time.monotonic() + 5.0
        while active.exists() and time.monotonic() < deadline:
            try:
                resumed = self.make_runner().run_case(case, resume=True)
            except RunnerError as error:
                self.assertRegex(str(error), "process group.*alive")
                time.sleep(0.01)
            else:
                break
        else:
            self.fail("durable orphan capture did not become resumable")

        self.assertEqual(resumed.status, "success")
        self.assertEqual(resumed.attempt_count, 1)
        self.assertEqual(len(self.calls()), 1)
        self.assertFalse(active.exists())

    def test_missing_launcher_completion_is_never_forged_as_native_exit(self) -> None:
        case = self.make_case("MMISSINGCAPTURE")
        self.set_case_scenarios((case,), ("success",), delays=(0.75,))
        errors: list[BaseException] = []

        def run() -> None:
            try:
                self.make_runner().run_case(case)
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=run)
        worker.start()
        reservation = runner_module._launch_reservation_path(
            self.run_dir, case.sample_id, 1
        )
        deadline = time.monotonic() + 5.0
        while not reservation.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(reservation.is_file())
        launch_id = json.loads(reservation.read_text())["launch_id"]
        completion_temporary = runner_module._launch_witness_temporary_path(
            self.run_dir,
            case.sample_id,
            1,
            launch_id,
            "completion",
        )
        completion_temporary.mkdir()
        worker.join(timeout=5.0)
        self.assertFalse(worker.is_alive())
        completion_temporary.rmdir()

        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RunnerError)
        self.assertRegex(str(errors[0]), "durable completion")
        self.assertEqual(len(self.calls()), 1)
        self.assertFalse((self.case_directory(case) / "terminal.json").exists())
        self.assertFalse(
            (self.case_directory(case) / "attempts" / "attempt-0001").exists()
        )

        self.set_scenario("success")
        result = self.make_runner().run_case(case, resume=True)

        self.assertEqual(result.status, "success")
        self.assertEqual(len(self.calls()), 2)
        incident = json.loads(
            (
                self.run_dir
                / "transport-incidents"
                / case.sample_id
                / "incident-0001"
                / "incident.json"
            ).read_text()
        )
        self.assertEqual(incident["error_code"], "INTERRUPTED_ATTEMPT")

    def test_cancellation_before_inner_gate_never_starts_native_model(self) -> None:
        case = self.make_case("MCANCELGATE")
        self.set_scenario("success")
        runner = self.make_runner()
        original_source = runner_module._DURABLE_LAUNCHER_SOURCE
        delayed_source = original_source.replace(
            '                if not cancelled:\n',
            '                time.sleep(0.5)\n'
            '                if not cancelled:\n',
            1,
        )
        self.assertNotEqual(delayed_source, original_source)

        def cancel_after_started_witness() -> None:
            started = runner_module._launch_started_path(
                self.run_dir, case.sample_id, 1
            )
            active = runner_module._active_marker_path(
                self.run_dir, case.sample_id, 1
            )
            deadline = time.monotonic() + 5.0
            while not started.is_file() and time.monotonic() < deadline:
                time.sleep(0.005)
            if started.is_file():
                launcher_pid = json.loads(active.read_text())["launcher_pid"]
                os.kill(launcher_pid, signal.SIGTERM)

        interrupter = threading.Thread(target=cancel_after_started_witness)
        interrupter.start()
        try:
            with mock.patch.object(
                runner_module,
                "_DURABLE_LAUNCHER_SOURCE",
                delayed_source,
            ):
                with self.assertRaises(RunnerError):
                    runner.run_case(case)
        finally:
            interrupter.join(timeout=2.0)

        self.assertEqual(self.calls(), [])
        self.assertFalse(self.fake_codex.with_suffix(".process-started").exists())
        incident = json.loads(
            (
                self.run_dir
                / "transport-incidents"
                / case.sample_id
                / "incident-0001"
                / "incident.json"
            ).read_text()
        )
        self.assertEqual(incident["error_code"], "RUN_CANCELLED")

    def test_success_reaps_same_group_descendants_before_capture_publish(self) -> None:
        case = self.make_case("MREAPDESCENDANT")
        self.set_scenario("lingering_descendant")

        result = self.make_runner().run_case(case)

        self.assertEqual(result.status, "success")
        stdout = (
            self.case_directory(case)
            / "attempts"
            / "attempt-0001"
            / "stdout.jsonl"
        )
        original = stdout.read_bytes()
        time.sleep(1.0)
        self.assertEqual(stdout.read_bytes(), original)
        self.assertNotIn(b"late-descendant-pollution", original)
        self.assertTrue(
            self.fake_codex.with_suffix(".lingering-terminated").is_file()
        )

    def test_resume_cleans_never_published_inflight_staging_boundaries(self) -> None:
        for index, files in enumerate(
            (
                (),
                ("prompt.txt",),
                ("prompt.txt", "stdout.jsonl"),
                ("prompt.txt", "stdout.jsonl", "stderr.txt"),
            ),
            start=1,
        ):
            with self.subTest(files=files):
                case = self.make_case(
                    "MACDEFGHIKLMNPQRSTVWY" + "ACDE"[index - 1]
                )
                staging = runner_module._staged_inflight_directory(
                    self.run_dir, case.sample_id, 1
                )
                staging.mkdir(parents=True)
                for filename in files:
                    value = render_prompt(case) if filename == "prompt.txt" else ""
                    (staging / filename).write_text(value, encoding="utf-8")
                self.set_scenario("success")

                result = self.make_runner().run_case(case, resume=True)

                self.assertEqual(result.status, "success")
                self.assertFalse(staging.exists())

        self.assertEqual(len(self.calls()), 4)

    def test_resume_removes_empty_final_inflight_as_never_started(self) -> None:
        case = self.make_case("MEMPTYINFLIGHT")
        self.set_scenario("success")
        inflight = self.run_dir / ".inflight" / case.sample_id
        inflight.mkdir(parents=True)

        result = self.make_runner().run_case(case, resume=True)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.attempt_count, 1)
        self.assertEqual(len(self.calls()), 1)

    def test_resume_seals_untracked_inflight_then_safely_retries(self) -> None:
        case = self.make_case("MUNKN")
        self.set_scenario("success")
        inflight = self.run_dir / ".inflight" / case.sample_id
        inflight.mkdir(parents=True)
        original_prompt = render_prompt(case).encode("utf-8")
        (inflight / "prompt.txt").write_bytes(original_prompt)

        result = self.make_runner().run_case(case, resume=True)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.attempt_count, 1)
        incident = (
            self.run_dir
            / "transport-incidents"
            / case.sample_id
            / "incident-0001"
        )
        self.assertEqual((incident / "prompt.txt").read_bytes(), original_prompt)
        self.assertEqual(
            json.loads((incident / "incident.json").read_text())["error_code"],
            "INTERRUPTED_ATTEMPT",
        )
        self.assertEqual(len(self.calls()), 1)

    def test_secret_environment_values_are_neither_forwarded_nor_persisted(self) -> None:
        case = self.make_case()
        self.set_scenario("success")
        secret_value = "cofactor-test-secret-do-not-persist-4fbe92"
        original = os.environ.get("COFACTOR_TEST_SECRET")
        os.environ["COFACTOR_TEST_SECRET"] = secret_value
        try:
            result = self.make_runner().run_case(case)
        finally:
            if original is None:
                os.environ.pop("COFACTOR_TEST_SECRET", None)
            else:
                os.environ["COFACTOR_TEST_SECRET"] = original

        self.assertEqual(result.status, "success")
        self.assertEqual(self.calls()[0]["sensitive_env_names"], [])
        for path in self.run_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret_value, path.read_text(errors="replace"))

    def test_atomic_publish_is_durable_and_never_clobbers_existing_bytes(self) -> None:
        staging = self.base / ".staging"
        destination = self.base / "ledger.json"
        with mock.patch.object(
            runner_module.os,
            "fsync",
            wraps=os.fsync,
        ) as fsync:
            runner_module._publish_text_exclusive(
                destination,
                "original\n",
                staging_directory=staging,
            )

        self.assertEqual(destination.read_bytes(), b"original\n")
        self.assertGreaterEqual(fsync.call_count, 2)
        with self.assertRaises(FileExistsError):
            runner_module._publish_text_exclusive(
                destination,
                "replacement\n",
                staging_directory=staging,
            )
        self.assertEqual(destination.read_bytes(), b"original\n")

    def test_failed_atomic_publish_never_leaves_a_truncated_target(self) -> None:
        staging = self.base / ".staging"
        destination = self.base / "ledger.json"

        with mock.patch.object(
            runner_module.os,
            "link",
            side_effect=OSError("simulated publish crash"),
        ):
            with self.assertRaisesRegex(OSError, "publish crash"):
                runner_module._publish_text_exclusive(
                    destination,
                    "complete payload\n",
                    staging_directory=staging,
                )

        self.assertFalse(destination.exists())
        self.assertEqual(tuple(staging.iterdir()), ())

    def test_atomic_directory_publish_refuses_to_replace_any_target(self) -> None:
        staging = self.base / ".staging"
        source = self.base / "source-ledger"
        destination = self.base / "published-ledger"
        source.mkdir()
        destination.mkdir()
        (source / "complete.json").write_text("new")
        (destination / "sentinel.json").write_text("original")

        with self.assertRaises(FileExistsError):
            runner_module._publish_directory_exclusive(
                source,
                destination,
                staging_directory=staging,
            )

        self.assertEqual((destination / "sentinel.json").read_text(), "original")
        self.assertEqual((source / "complete.json").read_text(), "new")

    def test_atomic_directory_publish_fsyncs_every_ledger_file(self) -> None:
        staging = self.base / ".staging"
        source = self.base / "source-ledger"
        destination = self.base / "published-ledger"
        source.mkdir()
        ledger_file = source / "complete.json"
        ledger_file.write_text("complete")
        ledger_inode = ledger_file.stat().st_ino
        fsynced_inodes: list[int] = []
        real_fsync = os.fsync

        def record_fsync(descriptor: int) -> None:
            fsynced_inodes.append(os.fstat(descriptor).st_ino)
            real_fsync(descriptor)

        with mock.patch.object(
            runner_module.os,
            "fsync",
            side_effect=record_fsync,
        ):
            runner_module._publish_directory_exclusive(
                source,
                destination,
                staging_directory=staging,
            )

        self.assertIn(ledger_inode, fsynced_inodes)
        self.assertEqual((destination / "complete.json").read_text(), "complete")

    def test_active_marker_survives_until_complete_attempt_directory_publish(self) -> None:
        case = self.make_case("MACTIVE")
        self.set_scenario("success")
        runner = self.make_runner()
        original = runner_module._publish_directory_exclusive
        marker_seen: list[bool] = []

        def inspect_then_publish(source, destination, *, staging_directory):
            if destination.name == "attempt-0001":
                marker_seen.append(
                    runner_module._active_marker_path(
                        self.run_dir, case.sample_id, 1
                    ).is_file()
                )
                self.assertTrue((source / "attempt.json").is_file())
            return original(
                source,
                destination,
                staging_directory=staging_directory,
            )

        with mock.patch.object(
            runner_module,
            "_publish_directory_exclusive",
            side_effect=inspect_then_publish,
        ):
            result = runner.run_case(case)

        self.assertEqual(result.status, "success")
        self.assertEqual(marker_seen, [True])
        self.assertFalse(
            runner_module._active_marker_path(
                self.run_dir, case.sample_id, 1
            ).exists()
        )

    def test_runner_staging_artifacts_never_pollute_audited_attempt_directory(self) -> None:
        case = self.make_case()
        self.set_scenario("success")

        self.make_runner().run_case(case)

        attempt = self.case_directory(case) / "attempts" / "attempt-0001"
        self.assertEqual(
            {entry.name for entry in attempt.iterdir()},
            {
                "attempt.json",
                "prediction.json",
                "prompt.txt",
                "stderr.txt",
                "stdout.jsonl",
            },
        )

    def test_timeout_terminates_the_entire_process_group_before_retry(self) -> None:
        case = self.make_case()
        self.set_scenario("timeout", "success")
        runner = self.make_runner(timeout_seconds=2.0)

        result = runner.run_case(case)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.attempt_count, 2)
        marker = self.fake_codex.with_suffix(".child-terminated")
        deadline = time.monotonic() + 2.0
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(marker.exists(), "child process did not receive SIGTERM")

    def test_timeout_does_not_hang_on_a_pipe_held_by_an_escaped_child(self) -> None:
        case = self.make_case()
        self.set_scenario("escaped_pipe", "success")
        started = time.monotonic()
        # Leave enough time for a fresh Python process to start under a loaded
        # full-suite run.  The escaped child itself holds the pipe for 5 s, so
        # the upper bound still detects waiting for that unrelated process.
        runner = self.make_runner(timeout_seconds=2.0)
        try:
            result = runner.run_case(case)
        finally:
            pid_path = self.fake_codex.with_suffix(".escaped-pid")
            if pid_path.exists():
                try:
                    os.killpg(int(pid_path.read_text()), 9)
                except ProcessLookupError:
                    pass

        self.assertEqual(result.status, "success")
        self.assertEqual(result.attempt_count, 2)
        self.assertLess(time.monotonic() - started, 5.0)

    def test_three_timeouts_close_as_a_scientific_terminal_error(self) -> None:
        case = self.make_case("MTIME")
        self.set_scenario("timeout")

        result = self.make_runner(timeout_seconds=2.0).run_case(case)

        self.assertEqual(result.status, "terminal_error")
        self.assertEqual(result.error_code, "TIMEOUT")
        self.assertEqual(result.attempt_count, 3)
        self.assertEqual(len(self.calls()), 3)
        self.assertFalse(
            (self.run_dir / "transport-incidents" / case.sample_id).exists()
        )

    def test_batch_timeout_aborts_after_one_consumed_case_attempt(self) -> None:
        cases = (self.make_case("MTMM"), self.make_case("MTMN"))
        self.set_case_scenarios(cases, ("timeout", "success"))

        with self.assertRaises(RunAborted) as raised:
            self.make_runner(timeout_seconds=2.0).run_cases(cases, concurrency=2)

        self.assertEqual(raised.exception.error_code, "TIMEOUT")
        self.assertEqual(len(self.calls()), 1)
        attempt = (
            self.case_directory(cases[0])
            / "attempts"
            / "attempt-0001"
            / "attempt.json"
        )
        self.assertEqual(json.loads(attempt.read_text())["error_code"], "TIMEOUT")
        self.assertFalse((self.case_directory(cases[0]) / "terminal.json").exists())

    def test_run_cases_validates_every_case_and_id_before_starting_work(self) -> None:
        case = self.make_case()
        self.set_scenario("success")
        runner = self.make_runner()

        with self.assertRaisesRegex(TypeError, "PromptCase"):
            runner.run_cases((case, object()), concurrency=2)  # type: ignore[arg-type]
        with self.assertRaisesRegex(RunnerError, "duplicate sample_id"):
            runner.run_cases((case, case), concurrency=2)

        self.assertEqual(self.calls(), [])
        self.assertFalse(self.run_dir.exists())

    def test_run_cases_rejects_invalid_concurrency_before_starting_work(self) -> None:
        case = self.make_case()
        self.set_scenario("success")
        runner = self.make_runner()

        for concurrency in (True, 0, MAX_CONCURRENCY + 1, 1.5, "2"):
            with self.subTest(concurrency=concurrency):
                with self.assertRaisesRegex(ValueError, "concurrency"):
                    runner.run_cases(
                        (case,),
                        concurrency=concurrency,  # type: ignore[arg-type]
                    )

        self.assertEqual(self.calls(), [])
        self.assertFalse(self.run_dir.exists())

    def test_run_cases_is_bounded_and_returns_results_in_input_order(self) -> None:
        cases = tuple(
            self.make_case(sequence)
            for sequence in ("MAAAA", "MBBBB", "MCCCC", "MDDDD")
        )
        self.set_case_scenarios(
            cases,
            ("success", "success", "success", "success"),
            delays=(0.30, 0.05, 0.20, 0.05),
            track_activity=True,
        )

        results = self.make_runner().run_cases(cases, concurrency=2)

        self.assertEqual(
            tuple(result.sample_id for result in results),
            tuple(case.sample_id for case in cases),
        )
        activity = json.loads(
            self.fake_codex.with_suffix(".activity.json").read_text()
        )
        self.assertEqual(activity, {"active": 0, "max_active": 2})
        for case in cases:
            self.assertTrue(
                (self.case_directory(case) / "terminal.json").is_file()
            )

    def test_terminal_case_error_does_not_hide_other_case_results(self) -> None:
        cases = tuple(
            self.make_case(sequence)
            for sequence in ("MEEEE", "MFFFF", "MGGGG")
        )
        self.set_case_scenarios(cases, ("success", "tool", "success"))

        results = self.make_runner().run_cases(cases, concurrency=3)

        self.assertEqual(
            tuple(result.status for result in results),
            ("success", "terminal_error", "success"),
        )
        self.assertEqual(results[1].error_code, "TOOL_POLLUTION")
        self.assertEqual(
            {call["sample_id"] for call in self.calls()},
            {case.sample_id for case in cases},
        )

    def test_existing_terminal_error_is_aggregated_before_new_work_starts(self) -> None:
        existing_case = self.make_case("MHHHH")
        other_case = self.make_case("MIIII")
        self.set_scenario("success")
        runner = self.make_runner()
        runner.run_case(existing_case)

        with self.assertRaises(RunCasesError) as raised:
            runner.run_cases(
                (existing_case, other_case),
                concurrency=2,
                resume=False,
            )

        error = raised.exception
        self.assertEqual(error.completed_results, ())
        self.assertEqual(
            tuple(failure.sample_id for failure in error.failures),
            (existing_case.sample_id,),
        )
        self.assertIsInstance(error.failures[0].error, CompletedCaseError)
        self.assertFalse(
            (self.case_directory(other_case) / "terminal.json").exists()
        )
        self.assertEqual(len(self.calls()), 1)

    def test_direct_systematic_failure_is_one_attempt_and_never_a_terminal(self) -> None:
        case = self.make_case("MJJJJ")
        self.set_scenario("auth")

        with self.assertRaises(SystematicFailure) as raised:
            self.make_runner().run_case(case)

        self.assertEqual(raised.exception.error_code, "AUTH_ERROR")
        self.assertEqual(len(self.calls()), 1)
        self.assertFalse((self.case_directory(case) / "terminal.json").exists())
        incident = json.loads(
            (
                self.run_dir
                / "transport-incidents"
                / case.sample_id
                / "incident-0001"
                / "incident.json"
            ).read_text()
        )
        self.assertEqual(incident["error_code"], "AUTH_ERROR")
        self.assertEqual(incident["tentative_attempt_number"], 1)

    def test_resume_rejects_transport_incident_prompt_identity_drift(self) -> None:
        case = self.make_case("MJKJK")
        runner = self.make_runner()
        self.set_scenario("auth")
        with self.assertRaises(SystematicFailure):
            runner.run_case(case)
        incident_prompt = (
            self.run_dir
            / "transport-incidents"
            / case.sample_id
            / "incident-0001"
            / "prompt.txt"
        )
        incident_prompt.write_text("tampered prompt", encoding="utf-8")
        self.set_scenario("success")

        with self.assertRaisesRegex(RunnerError, "incident.*prompt"):
            runner.run_case(case, resume=True)

        self.assertEqual(len(self.calls()), 1)

    def test_resume_strictly_validates_transport_incident_metadata(self) -> None:
        case = self.make_case("MSTRICTINCIDENT")
        runner = self.make_runner()
        self.set_scenario("auth")
        with self.assertRaises(SystematicFailure):
            runner.run_case(case)
        marker = (
            self.run_dir
            / "transport-incidents"
            / case.sample_id
            / "incident-0001"
            / "incident.json"
        )
        original = json.loads(marker.read_text())
        invalid_mutations = {
            "started timestamp type": {"started_at": None},
            "completed precedes start": {
                "completed_at": "1970-01-01T00:00:00Z"
            },
            "nonfinite duration": {"duration_seconds": float("nan")},
            "boolean returncode": {"returncode": True},
            "timeout semantic conflict": {"timed_out": True},
            "negative redactions": {"redaction_count": -1},
            "boolean usage value": {"usage": {"input_tokens": True}},
            "nonempty incident usage": {"usage": {"input_tokens": 1}},
            "incident thread": {"thread_id": "unexpected-thread"},
            "auth retry semantic conflict": {"retry_disposition": "retryable"},
            "prediction hash without artifact": {"prediction_sha256": "0" * 64},
        }
        self.set_scenario("success")

        for label, mutation in invalid_mutations.items():
            with self.subTest(label=label):
                tampered = {**original, **mutation}
                marker.write_text(
                    json.dumps(tampered, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                with self.assertRaises(RunnerError):
                    runner.run_case(case, resume=True)
                marker.write_text(
                    json.dumps(original, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

        self.assertEqual(len(self.calls()), 1)

    def test_resume_replays_incident_instead_of_trusting_swapped_error_code(
        self,
    ) -> None:
        case = self.make_case("MINCIDENTREPLAY")
        runner = self.make_runner()
        self.set_scenario("auth")
        with self.assertRaises(SystematicFailure):
            runner.run_case(case)
        marker = (
            self.run_dir
            / "transport-incidents"
            / case.sample_id
            / "incident-0001"
            / "incident.json"
        )
        tampered = json.loads(marker.read_text())
        tampered.update(
            {
                "error_code": "CAPACITY_ERROR",
                "retry_disposition": "retryable",
            }
        )
        marker.write_text(json.dumps(tampered, sort_keys=True) + "\n")
        self.set_scenario("success")

        with self.assertRaisesRegex(RunnerError, "incident.*replay"):
            runner.run_case(case, resume=True)

        self.assertEqual(len(self.calls()), 1)

    def test_resume_deduplicates_completed_incident_before_inflight_cleanup(self) -> None:
        case = self.make_case("MJLJL")
        runner = self.make_runner()
        self.set_scenario("auth")
        with self.assertRaises(SystematicFailure):
            runner.run_case(case)
        incident = (
            self.run_dir
            / "transport-incidents"
            / case.sample_id
            / "incident-0001"
        )
        inflight = self.run_dir / ".inflight" / case.sample_id
        inflight.mkdir(parents=True)
        for filename in ("prompt.txt", "stdout.jsonl", "stderr.txt"):
            (inflight / filename).write_bytes((incident / filename).read_bytes())
        self.set_scenario("success")

        result = runner.run_case(case, resume=True)

        self.assertEqual(result.status, "success")
        self.assertFalse(inflight.exists())
        self.assertFalse(
            (
                self.run_dir
                / "transport-incidents"
                / case.sample_id
                / "incident-0002"
            ).exists()
        )
        self.assertEqual(len(self.calls()), 2)

    def test_resume_atomically_finalizes_complete_staged_incident(self) -> None:
        case = self.make_case("MJMLM")
        runner = self.make_runner()
        self.set_scenario("auth")
        with self.assertRaises(SystematicFailure):
            runner.run_case(case)
        incident = (
            self.run_dir
            / "transport-incidents"
            / case.sample_id
            / "incident-0001"
        )
        inflight = self.run_dir / ".inflight" / case.sample_id
        inflight.parent.mkdir(parents=True, exist_ok=True)
        os.rename(incident, inflight)
        self.set_scenario("success")

        result = runner.run_case(case, resume=True)

        self.assertEqual(result.status, "success")
        self.assertTrue(incident.is_dir())
        self.assertFalse(inflight.exists())
        self.assertEqual(len(self.calls()), 2)

    def test_auth_probe_aborts_after_one_external_call_without_terminals(self) -> None:
        cases = tuple(
            self.make_case(sequence)
            for sequence in ("MMMMM", "MNNNN", "MOOOO", "MPPPP", "MQQQQ")
        )
        self.set_case_scenarios(cases, ("auth",) * len(cases))
        runner = self.make_runner(circuit_breaker_threshold=1)

        with self.assertRaises(RunAborted) as raised:
            runner.run_cases(cases, concurrency=2)

        aborted = raised.exception
        self.assertEqual(aborted.error_code, "AUTH_ERROR")
        self.assertEqual(aborted.threshold, 1)
        self.assertEqual(aborted.completed_results, ())
        self.assertEqual(
            aborted.pending_sample_ids,
            tuple(case.sample_id for case in cases),
        )
        self.assertEqual(len(aborted.failures), 1)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.calls()[0]["sample_id"], cases[0].sample_id)
        for case in cases:
            self.assertFalse((self.case_directory(case) / "terminal.json").exists())

        self.set_case_scenarios(cases, ("success",) * len(cases))
        resumed = runner.run_cases(cases, concurrency=2, resume=True)
        self.assertEqual(
            tuple(result.status for result in resumed),
            ("success",) * len(cases),
        )
        self.assertEqual(resumed[0].attempt_count, 1)
        self.assertEqual(len(self.calls()), len(cases) + 1)

    def test_capacity_failure_cancels_inflight_once_without_retry_amplification(self) -> None:
        cases = tuple(
            self.make_case(sequence)
            for sequence in (
                "MWWWW",
                "MYYYY",
                "MZZZZ",
                "MAACC",
                "MBBCC",
                "MCCDD",
            )
        )
        self.set_case_scenarios(
            cases,
            ("success", "capacity", "capacity", "capacity", "capacity", "capacity"),
            delays=(0.0, 0.15, 0.15, 0.15, 0.15, 0.15),
        )

        with self.assertRaises(RunAborted) as raised:
            self.make_runner().run_cases(cases, concurrency=4)

        aborted = raised.exception
        self.assertEqual(aborted.error_code, "CAPACITY_ERROR")
        self.assertEqual(
            tuple(result.sample_id for result in aborted.completed_results),
            (cases[0].sample_id,),
        )
        calls = self.calls()
        self.assertLessEqual(len(calls), 5, "probe + current four in-flight only")
        per_sample: dict[str, int] = {}
        for call in calls:
            sample_id = str(call["sample_id"])
            per_sample[sample_id] = per_sample.get(sample_id, 0) + 1
        self.assertTrue(all(count == 1 for count in per_sample.values()))
        for case in cases[1:]:
            self.assertFalse((self.case_directory(case) / "terminal.json").exists())

    def test_systematic_stop_is_set_before_slow_incident_publication(self) -> None:
        cases = tuple(
            self.make_case(sequence)
            for sequence in ("MKAAA", "MKBBB", "MKCCC")
        )
        self.set_case_scenarios(cases, ("success", "capacity", "mismatch"))
        runner = self.make_runner()
        original = runner._write_transport_incident

        def slow_incident(**kwargs):
            time.sleep(0.35)
            return original(**kwargs)

        with mock.patch.object(
            runner,
            "_write_transport_incident",
            side_effect=slow_incident,
        ):
            with self.assertRaises(RunAborted):
                runner.run_cases(cases, concurrency=2)

        mismatch_calls = [
            call for call in self.calls() if call["sample_id"] == cases[2].sample_id
        ]
        self.assertLessEqual(len(mismatch_calls), 1)

    def test_infrastructure_attempts_do_not_consume_model_output_retry_budget(self) -> None:
        case = self.make_case("MRRRR")
        runner = self.make_runner(max_attempts=3)
        self.set_scenario("capacity")
        with self.assertRaises(SystematicFailure):
            runner.run_case(case)

        self.set_scenario("mismatch")
        result = runner.run_case(case, resume=True)

        self.assertEqual(result.status, "terminal_error")
        self.assertEqual(result.error_code, "CASE_MISMATCH")
        self.assertEqual(result.attempt_count, 3)
        self.assertEqual(len(self.calls()), 4)

    def test_signal_cancels_probe_process_group_and_resume_is_safe(self) -> None:
        if not hasattr(signal, "SIGTERM"):
            self.skipTest("SIGTERM is unavailable")
        case = self.make_case("MSSSS")
        self.set_scenario("timeout")
        runner = self.make_runner(timeout_seconds=30.0)

        def interrupt_after_start() -> None:
            deadline = time.monotonic() + 5
            while not self.calls() and time.monotonic() < deadline:
                time.sleep(0.01)
            os.kill(os.getpid(), signal.SIGTERM)

        interrupter = threading.Thread(target=interrupt_after_start)
        interrupter.start()
        started = time.monotonic()
        try:
            with self.assertRaises(RunCancelled) as raised:
                runner.run_cases((case,), concurrency=1)
        finally:
            interrupter.join(timeout=2)

        self.assertEqual(raised.exception.signal_number, signal.SIGTERM)
        self.assertLess(time.monotonic() - started, 4.0)
        self.assertFalse((self.case_directory(case) / "terminal.json").exists())
        incident = json.loads(
            (
                self.run_dir
                / "transport-incidents"
                / case.sample_id
                / "incident-0001"
                / "incident.json"
            ).read_text()
        )
        self.assertEqual(incident["error_code"], "RUN_CANCELLED")
        self.assertFalse(
            runner_module._active_marker_path(
                self.run_dir, case.sample_id, 1
            ).exists()
        )

        self.set_scenario("success")
        resumed = runner.run_cases((case,), concurrency=1, resume=True)
        self.assertEqual(resumed[0].status, "success")
        self.assertEqual(resumed[0].attempt_count, 1)

    def test_max_attempts_is_hard_capped_at_three(self) -> None:
        with self.assertRaises(ValueError):
            self.make_runner(max_attempts=4)

    def test_circuit_breaker_threshold_is_fixed_at_immediate_abort(self) -> None:
        for threshold in (True, 0, -1, 2, 1.5, "1"):
            with self.subTest(threshold=threshold):
                with self.assertRaisesRegex(ValueError, "circuit_breaker_threshold"):
                    self.make_runner(
                        circuit_breaker_threshold=threshold,  # type: ignore[arg-type]
                    )


if __name__ == "__main__":
    unittest.main()
