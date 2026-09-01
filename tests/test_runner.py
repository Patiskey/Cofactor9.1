from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from cofactor_bench.prompt import PromptCase, create_prompt_case, render_prompt
from cofactor_bench.runner import (
    CodexExecRunner,
    CompletedCaseError,
    DISABLED_FEATURES,
    RunnerError,
    build_codex_argv,
)


FAKE_CODEX = r'''#!/usr/bin/env python3
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

try:
    prior_calls = calls_path.read_text(encoding="utf-8").splitlines()
except FileNotFoundError:
    prior_calls = []
call_number = len(prior_calls) + 1
kinds = scenario.get("kinds", ["success"])
kind = kinds[min(call_number - 1, len(kinds) - 1)]
forbidden_markers = ("KEY", "TOKEN", "SECRET", "PASSWORD")
observed = {
    "args": sys.argv[1:],
    "cwd": os.getcwd(),
    "entries_before": sorted(os.listdir(".")),
    "prompt": prompt,
    "sensitive_env_names": sorted(
        name for name in os.environ
        if any(marker in name.upper() for marker in forbidden_markers)
    ),
}
with calls_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(observed, sort_keys=True) + "\n")

sample_id = payload["sample_id"]
prediction = {
    "schema_version": "cofactor9.1.response.v1",
    "sample_id": sample_id,
    "status": "predict",
    "best_guess": [payload["label_catalog"]["labels"][0]],
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
if kind == "tool_exit":
    raise SystemExit(7)
'''


def label_catalog() -> tuple[str, ...]:
    return tuple(f"CHEBI:{index}" for index in range(1, 105))


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

    def make_case(self) -> PromptCase:
        return create_prompt_case(
            sequence="MSEQUENCEUX",
            allowed_labels=label_catalog(),
            catalog_version="uniprot-2026_02-chebi-v1",
        )

    def set_scenario(self, *kinds: str) -> None:
        self.fake_codex.with_suffix(".scenario.json").write_text(
            json.dumps({"kinds": list(kinds)}),
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
    ) -> CodexExecRunner:
        return CodexExecRunner(
            run_dir=self.run_dir,
            executable=self.fake_codex,
            schema_path=self.schema_path,
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
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
            allowed_labels=case.allowed_labels,
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
            allowed_labels=case.allowed_labels,
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

    def test_max_attempts_is_hard_capped_at_three(self) -> None:
        with self.assertRaises(ValueError):
            self.make_runner(max_attempts=4)


if __name__ == "__main__":
    unittest.main()
