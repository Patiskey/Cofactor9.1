# Runtime worker log

## Scope

Owned and changed only:

- `cofactor_bench/prompt.py`
- `cofactor_bench/runner.py`
- `tests/test_prompt.py`
- `tests/test_runner.py`
- `schemas/model-response.schema.json`
- `reports/agent_logs/runtime_worker.md`

No real Codex/model request or network benchmark call was made. The transport was
tested only with a local fake executable.

## TDD evidence

Initial RED, before either production module existed:

```text
$ python3 -m unittest tests.test_prompt tests.test_runner -v
test_prompt (unittest.loader._FailedTest.test_prompt) ... ERROR
test_runner (unittest.loader._FailedTest.test_runner) ... ERROR
...
ModuleNotFoundError: No module named 'cofactor_bench.prompt'
...
Ran 2 tests in 0.000s
FAILED (errors=2)
```

Prompt/schema GREEN:

```text
$ python3 -m unittest tests.test_prompt -v
Ran 7 tests in 0.001s
OK
```

Review-driven regression tests were also observed RED before their fixes:

```text
$ python3 -m unittest \
    tests.test_runner.CodexExecRunnerTests.test_tool_pollution_takes_priority_over_nonzero_process_exit \
    tests.test_runner.CodexExecRunnerTests.test_resume_rejects_a_different_prompt_with_the_same_sample_id -v
Ran 2 tests in 0.942s
FAILED (failures=2)

$ python3 -m unittest \
    tests.test_runner.CodexExecRunnerTests.test_nonstandard_nonfinite_outer_json_is_rejected -v
Ran 1 test in 0.414s
FAILED (failures=1)

$ python3 -m unittest \
    tests.test_runner.CodexExecRunnerTests.test_resume_rejects_an_interrupted_attempt_for_another_prompt -v
Ran 1 test in 0.265s
FAILED (failures=1)
```

The escaped-child timeout regression initially reproduced the unbounded pipe
wait and macOS `EPERM` process-group edge:

```text
$ python3 -m unittest \
    tests.test_runner.CodexExecRunnerTests.test_timeout_does_not_hang_on_a_pipe_held_by_an_escaped_child -v
subprocess.TimeoutExpired ...
PermissionError: [Errno 1] Operation not permitted
Ran 1 test in 2.066s
FAILED (errors=1)
```

Final runner GREEN:

```text
$ python3 -m unittest tests.test_runner -v
Ran 16 tests in 6.633s
OK
```

Final full-project GREEN after the data worker completed its concurrent slice:

```text
$ python3 -m unittest discover -s tests -v
Ran 76 tests in 10.198s
OK
```

## Implemented contract

- Prompt cases have exactly `sample_id`, `sequence`, and a nested 104-entry
  label catalog with its version. IDs use 128 random bits and expose no source
  accession or gold metadata.
- Prompt and response payloads are closed, typed, and validated without repair.
  `U` and `X` remain legal uppercase sequence symbols.
- Codex argv is fixed to `gpt-5.6-sol`, maximum reasoning, fast service, no
  approval, no web search, ephemeral/read-only execution, ignored user config
  and rules, and the full audited feature-disable list.
- Every attempt starts in a new mode-0700 empty directory under `/private/tmp`.
  The process uses an argv list with `shell=False`, receives the prompt on
  stdin, and receives only a fixed environment allowlist.
- Outer Codex JSONL and inner response JSON are parsed separately. Tool events,
  unknown events/items, duplicate keys, and non-finite JSON numbers fail closed.
- Attempt artifacts and `terminal.json` use exclusive creation. Resume skips a
  terminal case, continues the next attempt after interruption, binds saved
  results to the exact prompt hash, and never exceeds three attempts.
- Timeout handling targets the whole process group and keeps all drain/kill
  waits bounded, including an escaped child that retains a pipe.
- Environment variables whose names contain `KEY`, `TOKEN`, `SECRET`, or
  `PASSWORD` are never forwarded or recorded. Known sensitive environment
  values are redacted defensively from captured output before persistence.

## Integration notes

- `create_prompt_case(...)` generates a new random ID. A run creator must persist
  `PromptCase.to_payload()` and reload it with `PromptCase.from_payload()` for
  `--resume`; it must not regenerate IDs.
- `CodexExecRunner` accepts only a validated `PromptCase`, keeping accession,
  organism, EC, notes, evidence IDs, PMIDs, and gold labels outside its API.
- The formal schema uses `pattern`, `minItems`, `maxItems`, and `uniqueItems`.
  The real smoke gate should confirm the authenticated runtime accepts this
  Structured Outputs subset before launching the full run.
