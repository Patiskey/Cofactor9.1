from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from cofactor_bench.prompt import (
    CatalogTerm,
    PromptCase,
    create_prompt_case,
    render_prompt,
)
from cofactor_bench.runner import (
    CodexExecRunner,
    CompletedCaseError,
    DISABLED_FEATURES,
    MAX_CONCURRENCY,
    RunAborted,
    RunCasesError,
    RunnerError,
    build_codex_argv,
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
        circuit_breaker_threshold: int = 3,
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

    def test_resume_continues_after_an_interrupted_append_only_attempt(self) -> None:
        case = self.make_case()
        self.set_scenario("success")
        interrupted = (
            self.case_directory(case) / "attempts" / "attempt-0001"
        )
        interrupted.mkdir(parents=True)
        (interrupted / "prompt.txt").write_text(render_prompt(case))

        result = self.make_runner().run_case(case, resume=True)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.attempt_count, 2)
        self.assertTrue((interrupted / "prompt.txt").is_file())
        self.assertTrue(
            (
                self.case_directory(case)
                / "attempts"
                / "attempt-0002"
                / "attempt.json"
            ).is_file()
        )
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

    def test_timeout_terminates_the_entire_process_group_before_retry(self) -> None:
        case = self.make_case()
        self.set_scenario("timeout", "success")

        result = self.make_runner(timeout_seconds=1.0).run_case(case)

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
        try:
            result = self.make_runner(timeout_seconds=0.5).run_case(case)
        finally:
            pid_path = self.fake_codex.with_suffix(".escaped-pid")
            if pid_path.exists():
                try:
                    os.killpg(int(pid_path.read_text()), 9)
                except ProcessLookupError:
                    pass

        self.assertEqual(result.status, "success")
        self.assertEqual(result.attempt_count, 2)
        self.assertLess(time.monotonic() - started, 3.5)

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

    def test_worker_exception_is_aggregated_after_other_cases_finish(self) -> None:
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
        self.assertEqual(
            tuple(result.sample_id for result in error.completed_results),
            (other_case.sample_id,),
        )
        self.assertEqual(
            tuple(failure.sample_id for failure in error.failures),
            (existing_case.sample_id,),
        )
        self.assertIsInstance(error.failures[0].error, CompletedCaseError)
        self.assertTrue(
            (self.case_directory(other_case) / "terminal.json").is_file()
        )
        self.assertEqual(len(self.calls()), 2)

    def test_process_failures_are_classified_for_the_circuit_breaker(self) -> None:
        cases = tuple(
            self.make_case(sequence)
            for sequence in ("MJJJJ", "MKKKK", "MLLLL")
        )
        self.set_case_scenarios(cases, ("auth", "capacity", "transport"))
        runner = self.make_runner(max_attempts=1)

        results = tuple(runner.run_case(case) for case in cases)

        self.assertEqual(
            tuple(result.error_code for result in results),
            ("AUTH_ERROR", "CAPACITY_ERROR", "TRANSPORT_ERROR"),
        )

    def test_circuit_breaker_stops_new_cases_and_reports_resumable_pending_ids(self) -> None:
        cases = tuple(
            self.make_case(sequence)
            for sequence in ("MMMMM", "MNNNN", "MOOOO", "MPPPP", "MQQQQ")
        )
        self.set_case_scenarios(cases, ("auth",) * len(cases))
        runner = self.make_runner(
            max_attempts=1,
            circuit_breaker_threshold=2,
        )

        with self.assertRaises(RunAborted) as raised:
            runner.run_cases(cases, concurrency=2)

        aborted = raised.exception
        self.assertEqual(aborted.error_code, "AUTH_ERROR")
        self.assertEqual(aborted.threshold, 2)
        self.assertEqual(
            tuple(result.sample_id for result in aborted.completed_results),
            tuple(case.sample_id for case in cases[:2]),
        )
        self.assertEqual(
            aborted.pending_sample_ids,
            tuple(case.sample_id for case in cases[2:]),
        )
        self.assertEqual(aborted.failures, ())
        self.assertEqual(
            {call["sample_id"] for call in self.calls()},
            {case.sample_id for case in cases[:2]},
        )
        for case in cases[2:]:
            self.assertFalse(self.case_directory(case).exists())

        self.set_case_scenarios(cases, ("success",) * len(cases))
        resumed = runner.run_cases(cases, concurrency=2, resume=True)
        self.assertEqual(
            tuple(result.status for result in resumed),
            ("terminal_error", "terminal_error", "success", "success", "success"),
        )
        self.assertEqual(len(self.calls()), len(cases))

    def test_circuit_breaker_waits_for_already_in_flight_case(self) -> None:
        cases = tuple(
            self.make_case(sequence)
            for sequence in ("MWWWW", "MYYYY", "MZZZZ", "MAACC")
        )
        self.set_case_scenarios(
            cases,
            ("auth", "success", "success", "success"),
            delays=(0.0, 0.25, 0.0, 0.0),
        )

        with self.assertRaises(RunAborted) as raised:
            self.make_runner(
                max_attempts=1,
                circuit_breaker_threshold=1,
            ).run_cases(cases, concurrency=2)

        aborted = raised.exception
        self.assertEqual(
            tuple(result.sample_id for result in aborted.completed_results),
            tuple(case.sample_id for case in cases[:2]),
        )
        self.assertEqual(aborted.completed_results[1].status, "success")
        self.assertEqual(
            aborted.pending_sample_ids,
            tuple(case.sample_id for case in cases[2:]),
        )
        self.assertEqual(len(self.calls()), 2)

    def test_success_resets_consecutive_systematic_failure_count(self) -> None:
        cases = tuple(
            self.make_case(sequence)
            for sequence in ("MRRRR", "MSSSS", "MTTTT", "MVVVV")
        )
        self.set_case_scenarios(cases, ("auth", "success", "auth", "success"))

        results = self.make_runner(
            max_attempts=1,
            circuit_breaker_threshold=2,
        ).run_cases(cases, concurrency=1)

        self.assertEqual(
            tuple(result.status for result in results),
            ("terminal_error", "success", "terminal_error", "success"),
        )

    def test_max_attempts_is_hard_capped_at_three(self) -> None:
        with self.assertRaises(ValueError):
            self.make_runner(max_attempts=4)

    def test_circuit_breaker_threshold_must_be_a_positive_integer(self) -> None:
        for threshold in (True, 0, -1, 1.5, "2"):
            with self.subTest(threshold=threshold):
                with self.assertRaisesRegex(ValueError, "circuit_breaker_threshold"):
                    self.make_runner(
                        circuit_breaker_threshold=threshold,  # type: ignore[arg-type]
                    )


if __name__ == "__main__":
    unittest.main()
