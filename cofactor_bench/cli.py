"""Command-line entry points for the reproducible Cofactor9.1 pipeline."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any

from .build import build_from_config, verify_foundation_generation, verify_source_manifest
from .cases import build_cases_from_config, validate_cases_from_config
from .run import (
    RunContractError,
    execute_run_from_config,
    inspect_run_progress_from_config,
    validate_run_from_config,
    verified_run_snapshot_from_config,
)
from .runner import RunAborted, RunCasesError
from .views import build_views_from_config


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _json_print(value: object, *, stream: Any | None = None) -> None:
    if stream is None:
        stream = sys.stdout
    stream.write(_json_bytes(value).decode("utf-8"))
    stream.flush()


def _summary_value(value: object) -> object:
    to_dict = getattr(value, "to_dict", None)
    return to_dict() if callable(to_dict) else value


def _load_config(config_path: str | Path) -> tuple[Path, Path, dict[str, Any]]:
    config_file = Path(config_path).resolve()
    try:
        raw = config_file.read_bytes()
    except OSError as error:
        raise RunContractError(f"cannot read benchmark config: {error}") from error

    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RunContractError(f"duplicate config field {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise RunContractError(f"non-finite config number {value!r}")

    try:
        config = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicate,
            parse_constant=reject_constant,
        )
    except RunContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunContractError(f"benchmark config is invalid: {error}") from error
    if not isinstance(config, dict):
        raise RunContractError("benchmark config must contain an object")
    if config.get("schema_version") != "cofactor9.1.config.v1":
        raise RunContractError("benchmark config schema_version is unsupported")
    return config_file, config_file.parent.parent.resolve(), config


def _configured_path(
    *,
    project_root: Path,
    config: Mapping[str, Any],
    key: str,
) -> Path:
    paths = config.get("paths")
    if not isinstance(paths, Mapping):
        raise RunContractError("benchmark config.paths must be an object")
    configured = paths.get(key)
    if not isinstance(configured, str) or not configured:
        raise RunContractError(f"benchmark config.paths.{key} must be a string")
    relative = Path(configured)
    if relative.is_absolute():
        raise RunContractError(f"benchmark config.paths.{key} must be relative")
    resolved = (project_root / relative).resolve()
    if not resolved.is_relative_to(project_root):
        raise RunContractError(f"benchmark config.paths.{key} escapes project root")
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise RunContractError(f"cannot hash artifact {path}: {error}") from error
    return digest.hexdigest()


def _build_clusters(config_path: str | Path) -> object:
    from .clusters import build_homology_clusters_from_config

    return build_homology_clusters_from_config(config_path)


def _validate_clusters(config_path: str | Path) -> object:
    from .clusters import validate_homology_clusters_from_config

    return validate_homology_clusters_from_config(config_path)


def _validate_views(config_path: str | Path) -> dict[str, object]:
    """Hash-validate every published view without rewriting it."""

    config_file, root, config = _load_config(config_path)
    foundation = verify_foundation_generation(config_file)
    audit_path = _configured_path(
        project_root=root, config=config, key="full_structured"
    ).parent / "view_audit.json"
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunContractError(f"view audit is unreadable: {error}") from error
    if not isinstance(audit, Mapping):
        raise RunContractError("view audit must contain an object")
    if audit.get("schema_version") != "cofactor9.1.view-audit.v1":
        raise RunContractError("view audit schema_version is unsupported")
    if audit.get("dataset_version") != config.get("dataset_version"):
        raise RunContractError("view audit dataset_version differs from config")
    recorded = audit.get("output_artifact_sha256")
    if not isinstance(recorded, Mapping):
        raise RunContractError("view audit output hashes are missing")
    configured_keys = {
        "full_structured": "full_structured",
        "single_clean": "single_clean",
        "core_provisional": "core_provisional",
        "ambiguity_challenge": "ambiguity_challenge",
        "label_catalog": "label_catalog",
    }
    observed: dict[str, str] = {}
    for audit_key, config_key in configured_keys.items():
        path = _configured_path(project_root=root, config=config, key=config_key)
        observed[audit_key] = _sha256_file(path)
    observed["ontology_audit"] = _sha256_file(
        audit_path.with_name("ontology_audit.json")
    )
    if dict(recorded) != observed:
        raise RunContractError("published view artifact hashes differ from view audit")
    summary = audit.get("summary")
    if not isinstance(summary, Mapping):
        raise RunContractError("view audit summary is missing")
    if summary.get("full_structured_accessions") != 5_337:
        raise RunContractError("Full-Structured must contain exactly 5,337 accessions")
    foundation_master = foundation.get("artifacts", {}).get("master", {})
    input_hashes = audit.get("input_hashes")
    if (
        not isinstance(input_hashes, Mapping)
        or input_hashes.get("master_sha256") != foundation_master.get("sha256")
    ):
        raise RunContractError("view audit is not bound to the current Master-5337")
    return {
        "view_audit_sha256": _sha256_file(audit_path),
        "output_sha256": observed,
        "summary": dict(summary),
    }


def _build_command(config_path: str | Path) -> dict[str, object]:
    foundation = build_from_config(config_path)
    views = build_views_from_config(config_path)
    clusters = _build_clusters(config_path)
    cases = build_cases_from_config(config_path)
    return {
        "command": "build",
        "stages": {
            "foundation": _summary_value(foundation),
            "views": _summary_value(views),
            "clusters": _summary_value(clusters),
            "cases": _summary_value(cases),
        },
    }


def _validate_command(
    config_path: str | Path,
    *,
    stage: str,
    run_id: str | None,
    executable: str | Path,
) -> dict[str, object]:
    requested = (
        ("raw", "master", "views", "clusters", "cases")
        if stage == "all"
        else (stage,)
    )
    if stage == "all" and run_id is not None:
        requested += ("run",)
    if "run" in requested and run_id is None:
        raise RunContractError("validate --stage run requires --run-id")
    values: dict[str, object] = {}
    for item in requested:
        if item == "raw":
            values[item] = verify_source_manifest(config_path)
        elif item == "master":
            values[item] = verify_foundation_generation(config_path)
        elif item == "views":
            values[item] = _validate_views(config_path)
        elif item == "clusters":
            values[item] = _summary_value(_validate_clusters(config_path))
        elif item == "cases":
            values[item] = _summary_value(validate_cases_from_config(config_path))
        elif item == "run":
            assert run_id is not None
            values[item] = validate_run_from_config(
                config_path=config_path,
                run_id=run_id,
                executable=executable,
            ).to_dict()
        else:  # pragma: no cover - argparse constrains this path.
            raise RunContractError(f"unknown validation stage {item!r}")
    return {"command": "validate", "stages": values}


_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SCORE_STAGING_PATTERN = re.compile(
    r"score-(metrics\.json|report\.md)-([0-9a-f]{64})\.tmp\Z"
)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError as error:
        raise RunContractError(f"cannot open score directory {path}: {error}") from error
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise RunContractError(f"cannot sync score directory {path}: {error}") from error
    finally:
        os.close(descriptor)


def _score_run_location(
    config_path: str | Path,
    *,
    run_id: str,
) -> tuple[Path, Path]:
    _, project_root, config = _load_config(config_path)
    if _RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise RunContractError(f"unsafe run_id {run_id!r}")
    runs_root = _configured_path(
        project_root=project_root,
        config=config,
        key="runs",
    )
    run_dir = runs_root / run_id
    if run_dir.parent != runs_root or run_dir.is_symlink():
        raise RunContractError("score run directory is unsafe")
    if not run_dir.is_dir():
        raise RunContractError(f"score run does not exist: {run_id!r}")
    return project_root, run_dir


@contextmanager
def _score_publication_lock(project_root: Path, *, run_id: str):
    """Serialize run and score lifecycle operations without changing the run tree."""

    lock_root = project_root / ".run-locks"
    if lock_root.is_symlink():
        raise RunContractError("run lock directory is unsafe")
    try:
        lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        raise RunContractError(f"cannot create run lock directory: {error}") from error
    lock_path = lock_root / f"{run_id}.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise RunContractError(f"cannot open score publication lock: {error}") from error
    locked = False
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RunContractError("score publication lock is not a regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RunContractError(
                f"run {run_id!r} has an active invocation or score publication"
            ) from error
        locked = True
        _fsync_directory(lock_root)
        yield
    finally:
        try:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _score_staging_directory(run_dir: Path) -> Path:
    staging = run_dir / ".staging"
    if staging.is_symlink():
        raise RunContractError("score staging directory is unsafe")
    existed = staging.exists()
    try:
        staging.mkdir(mode=0o700, exist_ok=True)
    except OSError as error:
        raise RunContractError(f"cannot create score staging directory: {error}") from error
    if not staging.is_dir():
        raise RunContractError("score staging path is not a directory")
    if not existed:
        _fsync_directory(run_dir)
    return staging


def _read_exact_score_output(path: Path, expected: bytes) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise RunContractError(f"cannot inspect score output {path}: {error}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise RunContractError(f"score output is not a real file: {path}")
    try:
        observed = path.read_bytes()
    except OSError as error:
        raise RunContractError(f"cannot read score output {path}: {error}") from error
    if observed != expected:
        raise RunContractError(
            f"score output already exists with different bytes: {path}"
        )
    return True


def _recover_score_publications(run_dir: Path) -> None:
    """Remove only score-owned crash residue before immutable run validation."""

    staging = _score_staging_directory(run_dir)
    changed = False
    try:
        entries = tuple(sorted(staging.iterdir(), key=lambda item: item.name))
    except OSError as error:
        raise RunContractError(f"cannot inspect score staging directory: {error}") from error
    for entry in entries:
        if not entry.name.startswith("score-"):
            continue
        match = _SCORE_STAGING_PATTERN.fullmatch(entry.name)
        if match is None:
            raise RunContractError(
                f"unexpected score staging artifact {entry.name!r}"
            )
        try:
            metadata = entry.lstat()
        except OSError as error:
            raise RunContractError(
                f"cannot inspect score staging artifact {entry.name!r}: {error}"
            ) from error
        if not stat.S_ISREG(metadata.st_mode):
            raise RunContractError(
                f"score staging artifact {entry.name!r} is unsafe"
            )
        try:
            payload = entry.read_bytes()
        except OSError as error:
            raise RunContractError(
                f"cannot read score staging artifact {entry.name!r}: {error}"
            ) from error
        target = run_dir / match.group(1)
        payload_is_complete = hashlib.sha256(payload).hexdigest() == match.group(2)
        if payload_is_complete and target.exists():
            _read_exact_score_output(target, payload)
        try:
            entry.unlink()
        except OSError as error:
            raise RunContractError(
                f"cannot remove recovered score staging artifact {entry.name!r}: {error}"
            ) from error
        changed = True
    if changed:
        _fsync_directory(staging)


def _stage_score_output(run_dir: Path, name: str, value: bytes) -> Path:
    if name not in {"metrics.json", "report.md"}:
        raise RunContractError(f"unsupported score output {name!r}")
    staging = _score_staging_directory(run_dir)
    digest = hashlib.sha256(value).hexdigest()
    temporary = staging / f"score-{name}-{digest}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(temporary, flags, 0o600)
    except FileExistsError:
        if not _read_exact_score_output(temporary, value):  # pragma: no cover
            raise AssertionError("existing staging file vanished")
        return temporary
    except OSError as error:
        raise RunContractError(f"cannot create score staging artifact: {error}") from error
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as error:
        raise RunContractError(f"cannot write score staging artifact: {error}") from error
    return temporary


def _publish_score_outputs(
    run_dir: Path,
    outputs: Sequence[tuple[str, bytes]],
) -> None:
    """Stage all outputs durably, then no-clobber publish them as one pair."""

    normalized = tuple(outputs)
    if not normalized or len({name for name, _ in normalized}) != len(normalized):
        raise RunContractError("score outputs must be unique and nonempty")
    for name, value in normalized:
        _read_exact_score_output(run_dir / name, value)
    staged = tuple(
        (name, value, _stage_score_output(run_dir, name, value))
        for name, value in normalized
    )
    staging = _score_staging_directory(run_dir)
    _fsync_directory(staging)
    for name, value, temporary in staged:
        target = run_dir / name
        if _read_exact_score_output(target, value):
            continue
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            _read_exact_score_output(target, value)
        except OSError as error:
            raise RunContractError(f"cannot publish score output {target}: {error}") from error
    _fsync_directory(run_dir)
    for _, _, temporary in staged:
        try:
            temporary.unlink()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise RunContractError(f"cannot clean score staging artifact: {error}") from error
    _fsync_directory(staging)


def _publish_exact(path: Path, value: bytes) -> None:
    """Compatibility helper for one score output; callers provide serialization."""

    _recover_score_publications(path.parent)
    _publish_score_outputs(path.parent, ((path.name, value),))


def _score_command(
    config_path: str | Path,
    *,
    run_id: str,
) -> dict[str, object]:
    from .reporting import render_markdown, report_json_bytes, score_run

    project_root, expected_run_dir = _score_run_location(
        config_path,
        run_id=run_id,
    )
    with _score_publication_lock(project_root, run_id=run_id):
        _recover_score_publications(expected_run_dir)
        snapshot = verified_run_snapshot_from_config(
            config_path=config_path,
            run_id=run_id,
            executable=None,
        )
        run_dir = snapshot.run_dir
        if run_dir != expected_run_dir:
            raise RunContractError("verified snapshot run directory differs from config")
        report = score_run(
            verified_run_snapshot=snapshot,
            formal=True,
            expected_case_count=5_337,
        )
        metrics_bytes = report_json_bytes(report)
        markdown_bytes = render_markdown(report).encode("utf-8")
        metrics_path = run_dir / "metrics.json"
        report_path = run_dir / "report.md"
        _publish_score_outputs(
            run_dir,
            (
                ("metrics.json", metrics_bytes),
                ("report.md", markdown_bytes),
            ),
        )
    return {
        "command": "score",
        "run": snapshot.validation.to_dict(),
        "provenance": {
            "run_manifest_sha256": snapshot.manifest_sha256,
            "ledger_composite_sha256": snapshot.ledger_composite_sha256,
            "provenance_composite_sha256": (
                snapshot.provenance_composite_sha256
            ),
            "artifact_sha256": dict(snapshot.artifact_sha256),
            "evaluation_implementation_sha256": dict(
                snapshot.evaluation_implementation_sha256
            ),
        },
        "outputs": {
            "metrics": str(metrics_path),
            "metrics_sha256": hashlib.sha256(metrics_bytes).hexdigest(),
            "report": str(report_path),
            "report_sha256": hashlib.sha256(markdown_bytes).hexdigest(),
        },
        "headline": report.to_dict().get("headline", {}),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m cofactor_bench.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="build all deterministic artifacts")
    build.add_argument("--config", default="config/benchmark.json")

    validate = subparsers.add_parser("validate", help="validate frozen artifacts")
    validate.add_argument("--config", default="config/benchmark.json")
    validate.add_argument(
        "--stage",
        choices=("all", "raw", "master", "views", "clusters", "cases", "run"),
        default="all",
    )
    validate.add_argument("--run-id")
    validate.add_argument("--codex-executable", default="codex")

    run = subparsers.add_parser("run", help="create or exactly resume a model run")
    run.add_argument("--config", default="config/benchmark.json")
    run.add_argument("--run-id", required=True)
    run.add_argument("--resume", action="store_true")
    run.add_argument("--concurrency", type=int)
    run.add_argument("--timeout-seconds", type=float)
    run.add_argument("--circuit-breaker-threshold", type=int, choices=(1,))
    run.add_argument("--limit", type=int)
    run.add_argument("--infrastructure-gate", action="store_true")
    run.add_argument("--codex-executable", default="codex")

    score = subparsers.add_parser("score", help="score one complete formal run")
    score.add_argument("--config", default="config/benchmark.json")
    score.add_argument("--run-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    raw_argv = tuple(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw_argv)
    try:
        if args.command == "build":
            result = _build_command(args.config)
        elif args.command == "validate":
            result = _validate_command(
                args.config,
                stage=args.stage,
                run_id=args.run_id,
                executable=args.codex_executable,
            )
        elif args.command == "run":
            result = execute_run_from_config(
                config_path=args.config,
                run_id=args.run_id,
                resume=args.resume,
                concurrency=args.concurrency,
                timeout_seconds=args.timeout_seconds,
                circuit_breaker_threshold=args.circuit_breaker_threshold,
                limit=args.limit,
                infrastructure_gate=args.infrastructure_gate,
                executable=args.codex_executable,
                invocation_argv=(
                    sys.executable,
                    "-m",
                    "cofactor_bench.cli",
                    *raw_argv,
                ),
            ).to_dict()
        elif args.command == "score":
            result = _score_command(
                args.config,
                run_id=args.run_id,
            )
        else:  # pragma: no cover - argparse constrains this path.
            parser.error(f"unsupported command {args.command!r}")
            return 2
    except (RunAborted, RunCasesError) as error:
        error_payload: dict[str, object] = {
            "status": "run_interrupted",
            "error_type": type(error).__name__,
            "message": str(error),
            "completed_result_count": len(error.completed_results),
            "worker_failure_count": len(error.failures),
        }
        pending = getattr(error, "pending_sample_ids", ())
        error_payload["pending_case_count"] = len(pending)
        error_payload["pending_sample_ids_preview"] = list(pending[:10])
        try:
            progress = inspect_run_progress_from_config(
                config_path=args.config,
                run_id=args.run_id,
            )
        except Exception as progress_error:
            error_payload["progress_error"] = str(progress_error)
        else:
            error_payload["progress"] = progress.to_dict()
        _json_print(error_payload, stream=sys.stderr)
        return 3
    except Exception as error:
        _json_print(
            {
                "status": "error",
                "error_type": type(error).__name__,
                "message": str(error),
            },
            stream=sys.stderr,
        )
        return 1
    _json_print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
