"""Deterministic near-homology clusters for Full-Structured-5337.

DIAMOND is an external, frozen scientific dependency.  This module validates
its executable identity, invokes it without a shell in an isolated temporary
directory, canonicalizes its partition independently of DIAMOND's arbitrary
representative choice, and publishes immutable JSONL plus an audit manifest.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from typing import Any


EXPECTED_RECORD_COUNT = 5_337
IDENTITY_PERCENT = 90
MUTUAL_COVERAGE_PERCENT = 80
THREADS = 1
DEFAULT_DIAMOND_PATH = Path("/opt/homebrew/bin/diamond")
DEFAULT_DIAMOND_VERSION = "2.2.5"
DEFAULT_DIAMOND_SHA256 = (
    "53546dfbe539fd00a7cc2a7a91e00c9d9a770fd4a0bc60471b3f8267bdfceff4"
)

CLUSTER_RECORD_SCHEMA_VERSION = "cofactor9.1.homology-cluster-record.v1"
MANIFEST_SCHEMA_VERSION = "cofactor9.1.homology-cluster-manifest.v1"
CLUSTER_ID_VERSION = "cofactor9.1.homology-cluster-id.v1"

_VIEW_RECORD_SCHEMA_VERSION = "cofactor9.1.view-record.v1"
_ACCESSION = re.compile(r"[A-Z0-9]{6,10}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SEQUENCE = re.compile(r"[A-Z]+\Z")
_INTERNAL_ID = re.compile(r"seq[0-9]{8}\Z")
_VERSION_OUTPUT = re.compile(r"diamond version ([0-9]+\.[0-9]+\.[0-9]+)\Z")
_CLUSTER_ID_DOMAIN = (CLUSTER_ID_VERSION + "\0").encode("ascii")


class HomologyClusterError(ValueError):
    """Raised when clustering inputs, execution, or artifacts violate policy."""


@dataclass(frozen=True, slots=True)
class HomologyClusterArtifacts:
    """Canonical in-memory cluster records and their deterministic bytes."""

    records: tuple[dict[str, Any], ...]
    manifest: dict[str, Any]
    clusters_jsonl: bytes
    manifest_json: bytes


@dataclass(frozen=True, slots=True)
class HomologyClusterBuildSummary:
    """Compact result shared by immutable build and strict validation."""

    record_count: int
    cluster_count: int
    singleton_cluster_count: int
    multi_member_cluster_count: int
    largest_cluster_size: int
    input_sha256: dict[str, str]
    output_sha256: dict[str, str]
    clusters_path: Path
    manifest_path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_count": self.record_count,
            "cluster_count": self.cluster_count,
            "singleton_cluster_count": self.singleton_cluster_count,
            "multi_member_cluster_count": self.multi_member_cluster_count,
            "largest_cluster_size": self.largest_cluster_size,
            "input_sha256": dict(self.input_sha256),
            "output_sha256": dict(self.output_sha256),
            "paths": {
                "clusters": str(self.clusters_path),
                "manifest": str(self.manifest_path),
            },
        }


@dataclass(frozen=True, slots=True)
class _SequenceRecord:
    accession: str
    sequence: str
    sequence_sha256: str


@dataclass(frozen=True, slots=True)
class _ParsedFull:
    dataset_version: str
    view_rule_version: str
    records: tuple[_SequenceRecord, ...]


@dataclass(frozen=True, slots=True)
class _DiamondIdentity:
    path: Path
    sha256: str
    version: str
    version_output: str


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise HomologyClusterError(f"Cannot hash DIAMOND binary: {error}") from error
    return digest.hexdigest()


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _jsonl_bytes(records: tuple[Mapping[str, Any], ...]) -> bytes:
    return b"".join(_json_bytes(record) for record in records)


def _reject_json_constant(value: str) -> None:
    raise HomologyClusterError(f"Non-finite JSON number {value!r} is not allowed")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HomologyClusterError(f"Duplicate JSON field {key!r} is not allowed")
        result[key] = value
    return result


def _decode_jsonl(value: bytes, *, location: str) -> list[dict[str, Any]]:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise HomologyClusterError(f"{location} is not valid UTF-8") from error
    if not text:
        raise HomologyClusterError(f"{location} is empty")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            raise HomologyClusterError(
                f"{location} line {line_number} is blank; blank lines are forbidden"
            )
        try:
            decoded = json.loads(
                line,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except HomologyClusterError:
            raise
        except json.JSONDecodeError as error:
            raise HomologyClusterError(
                f"{location} line {line_number} is not valid JSON: {error.msg}"
            ) from error
        if not isinstance(decoded, dict):
            raise HomologyClusterError(
                f"{location} line {line_number} must contain a JSON object"
            )
        records.append(decoded)
    return records


def _decode_json(value: bytes, *, location: str) -> Any:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise HomologyClusterError(f"{location} is not valid UTF-8") from error
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except HomologyClusterError:
        raise
    except json.JSONDecodeError as error:
        raise HomologyClusterError(
            f"{location} is not valid JSON: {error.msg}"
        ) from error


def _required_mapping(
    container: Mapping[str, Any],
    key: str,
    *,
    location: str,
) -> Mapping[str, Any]:
    value = container.get(key)
    if not isinstance(value, Mapping):
        raise HomologyClusterError(f"{location}.{key} must be a JSON object")
    return value


def _required_string(
    container: Mapping[str, Any],
    key: str,
    *,
    location: str,
) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value:
        raise HomologyClusterError(f"{location}.{key} must be a nonempty string")
    return value


def _positive_record_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HomologyClusterError("expected_record_count must be a positive integer")
    return value


def _positive_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HomologyClusterError("timeout_seconds must be a positive number")
    result = float(value)
    if not 0 < result <= 86_400:
        raise HomologyClusterError(
            "timeout_seconds must be greater than zero and at most 86400"
        )
    return result


def _parse_full_structured(value: bytes, *, expected_count: int) -> _ParsedFull:
    decoded = _decode_jsonl(value, location="Full-Structured input")
    if len(decoded) != expected_count:
        raise HomologyClusterError(
            "Full-Structured input must contain exactly "
            f"{expected_count} records; observed {len(decoded)}"
        )

    dataset_version: str | None = None
    view_rule_version: str | None = None
    accessions: set[str] = set()
    records: list[_SequenceRecord] = []
    for index, row in enumerate(decoded, start=1):
        location = f"Full-Structured record {index}"
        if row.get("schema_version") != _VIEW_RECORD_SCHEMA_VERSION:
            raise HomologyClusterError(f"{location} schema_version is unsupported")
        current_dataset = _required_string(row, "dataset_version", location=location)
        if dataset_version is None:
            dataset_version = current_dataset
        elif current_dataset != dataset_version:
            raise HomologyClusterError(
                f"{location} dataset_version differs from earlier records"
            )
        derivation = _required_mapping(row, "derivation", location=location)
        current_rule = _required_string(
            derivation,
            "rule_version",
            location=f"{location}.derivation",
        )
        if view_rule_version is None:
            view_rule_version = current_rule
        elif current_rule != view_rule_version:
            raise HomologyClusterError(
                f"{location} derivation.rule_version differs from earlier records"
            )

        entry = _required_mapping(row, "entry", location=location)
        accession = _required_string(entry, "accession", location=f"{location}.entry")
        if _ACCESSION.fullmatch(accession) is None:
            raise HomologyClusterError(f"{location} has an invalid accession")
        if accession in accessions:
            raise HomologyClusterError(f"Duplicate accession {accession!r}")
        accessions.add(accession)

        sequence = _required_mapping(row, "sequence", location=location)
        sequence_value = _required_string(
            sequence,
            "value",
            location=f"{location}.sequence",
        )
        if _SEQUENCE.fullmatch(sequence_value) is None:
            raise HomologyClusterError(
                f"{location} sequence must contain only uppercase ASCII letters"
            )
        sequence_sha256 = _required_string(
            sequence,
            "sha256",
            location=f"{location}.sequence",
        )
        if _SHA256.fullmatch(sequence_sha256) is None:
            raise HomologyClusterError(f"{location} has an invalid sequence SHA256")
        observed_sha256 = _sha256_bytes(sequence_value.encode("ascii"))
        if not hmac.compare_digest(observed_sha256, sequence_sha256):
            raise HomologyClusterError(f"{location} sequence SHA256 does not match value")
        records.append(
            _SequenceRecord(
                accession=accession,
                sequence=sequence_value,
                sequence_sha256=sequence_sha256,
            )
        )

    if dataset_version is None or view_rule_version is None:
        raise HomologyClusterError("Full-Structured input contains no records")
    return _ParsedFull(
        dataset_version=dataset_version,
        view_rule_version=view_rule_version,
        records=tuple(sorted(records, key=lambda item: item.accession)),
    )


def _fasta_and_mapping(
    records: tuple[_SequenceRecord, ...],
) -> tuple[bytes, dict[str, str]]:
    chunks: list[bytes] = []
    accession_by_id: dict[str, str] = {}
    for index, record in enumerate(records, start=1):
        internal_id = f"seq{index:08d}"
        accession_by_id[internal_id] = record.accession
        chunks.append(f">{internal_id}\n{record.sequence}\n".encode("ascii"))
    return b"".join(chunks), accession_by_id


def _validate_expected_sha256(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise HomologyClusterError(
            "expected_binary_sha256 must be a lowercase 64-character SHA256"
        )
    return value


def _resolve_diamond(
    diamond_binary: str | Path,
    *,
    expected_sha256: str,
) -> tuple[Path, str]:
    configured = Path(diamond_binary)
    if not configured.is_absolute():
        raise HomologyClusterError("DIAMOND binary path must be absolute")
    try:
        resolved = configured.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as error:
        raise HomologyClusterError(f"Cannot resolve DIAMOND binary: {error}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise HomologyClusterError("DIAMOND binary must resolve to a regular file")
    if not os.access(resolved, os.X_OK):
        raise HomologyClusterError("DIAMOND binary is not executable")
    observed_sha256 = _sha256_file(resolved)
    if not hmac.compare_digest(observed_sha256, expected_sha256):
        raise HomologyClusterError(
            "DIAMOND binary SHA256 differs from the frozen expectation; "
            f"expected {expected_sha256}, observed {observed_sha256}"
        )
    return resolved, observed_sha256


def _run_command(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[bytes]:
    environment = {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.defpath,
        "TMPDIR": str(cwd),
    }
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise HomologyClusterError(
            f"DIAMOND command {argv[1]!r} exceeded {timeout_seconds:g} seconds"
        ) from error
    except OSError as error:
        raise HomologyClusterError(
            f"Cannot execute DIAMOND command {argv[1]!r}: {error}"
        ) from error
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        if len(stderr) > 2_000:
            stderr = stderr[-2_000:]
        raise HomologyClusterError(
            f"DIAMOND command {argv[1]!r} failed with exit code "
            f"{completed.returncode}: {stderr}"
        )
    return completed


def _verify_binary_unchanged(identity: _DiamondIdentity) -> None:
    observed = _sha256_file(identity.path)
    if not hmac.compare_digest(observed, identity.sha256):
        raise HomologyClusterError("DIAMOND binary changed during clustering")


def _diamond_identity(
    binary_path: Path,
    binary_sha256: str,
    *,
    expected_version: str,
    cwd: Path,
    timeout_seconds: float,
) -> tuple[_DiamondIdentity, tuple[str, ...]]:
    version_argv = (str(binary_path), "version")
    completed = _run_command(
        version_argv,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )
    try:
        version_output = completed.stdout.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise HomologyClusterError("DIAMOND version output is not ASCII") from error
    match = _VERSION_OUTPUT.fullmatch(version_output)
    if match is None:
        raise HomologyClusterError(
            f"Unexpected DIAMOND version output {version_output!r}"
        )
    observed_version = match.group(1)
    if observed_version != expected_version:
        raise HomologyClusterError(
            "DIAMOND version differs from the frozen expectation; "
            f"expected {expected_version}, observed {observed_version}"
        )
    identity = _DiamondIdentity(
        path=binary_path,
        sha256=binary_sha256,
        version=observed_version,
        version_output=version_output,
    )
    _verify_binary_unchanged(identity)
    return identity, version_argv


def _read_generated_regular_file(path: Path, *, description: str) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise HomologyClusterError(f"DIAMOND did not create {description}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise HomologyClusterError(
            f"DIAMOND {description} must be a regular non-symlink file"
        )
    try:
        return path.read_bytes()
    except OSError as error:
        raise HomologyClusterError(f"Cannot read DIAMOND {description}: {error}") from error


def _parse_cluster_partition(
    value: bytes,
    *,
    accession_by_id: Mapping[str, str],
) -> tuple[tuple[str, ...], ...]:
    try:
        text = value.decode("ascii")
    except UnicodeDecodeError as error:
        raise HomologyClusterError("DIAMOND cluster output is not ASCII") from error
    if not text:
        raise HomologyClusterError("DIAMOND cluster output is empty")

    members_by_representative: dict[str, list[str]] = defaultdict(list)
    seen_members: set[str] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            raise HomologyClusterError(
                f"DIAMOND cluster output line {line_number} is blank"
            )
        fields = line.split("\t")
        if len(fields) != 2:
            raise HomologyClusterError(
                f"DIAMOND cluster output line {line_number} must have two TSV fields"
            )
        representative, member = fields
        for role, internal_id in (
            ("representative", representative),
            ("member", member),
        ):
            if _INTERNAL_ID.fullmatch(internal_id) is None:
                raise HomologyClusterError(
                    f"DIAMOND returned an invalid {role} ID {internal_id!r}"
                )
            if internal_id not in accession_by_id:
                raise HomologyClusterError(
                    f"DIAMOND returned an unknown {role} ID {internal_id!r}"
                )
        if member in seen_members:
            raise HomologyClusterError(
                f"Duplicate DIAMOND cluster member {member!r}"
            )
        seen_members.add(member)
        members_by_representative[representative].append(member)

    expected_members = set(accession_by_id)
    if seen_members != expected_members:
        missing = sorted(expected_members - seen_members)
        unexpected = sorted(seen_members - expected_members)
        raise HomologyClusterError(
            "DIAMOND cluster members do not cover the Full input exactly; "
            f"missing={missing[:20]}, unexpected={unexpected[:20]}"
        )

    partitions: list[tuple[str, ...]] = []
    for representative, internal_members in members_by_representative.items():
        if representative not in internal_members:
            raise HomologyClusterError(
                "DIAMOND cluster representative is not present among its members: "
                f"{representative!r}"
            )
        accessions = tuple(
            sorted(accession_by_id[internal_id] for internal_id in internal_members)
        )
        partitions.append(accessions)
    return tuple(sorted(partitions))


def _stable_cluster_id(accessions: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    digest.update(_CLUSTER_ID_DOMAIN)
    for accession in accessions:
        digest.update(accession.encode("ascii"))
        digest.update(b"\0")
    return "cluster_" + digest.hexdigest()


def _canonical_cluster_records(
    partitions: tuple[tuple[str, ...], ...],
) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    seen_cluster_ids: set[str] = set()
    for accessions in partitions:
        cluster_id = _stable_cluster_id(accessions)
        if cluster_id in seen_cluster_ids:
            raise HomologyClusterError("Stable cluster ID collision")
        seen_cluster_ids.add(cluster_id)
        representative_accession = accessions[0]
        for accession in accessions:
            rows.append(
                {
                    "schema_version": CLUSTER_RECORD_SCHEMA_VERSION,
                    "accession": accession,
                    "cluster_id": cluster_id,
                    "representative_accession": representative_accession,
                    "member_count": len(accessions),
                }
            )
    return tuple(sorted(rows, key=lambda row: row["accession"]))


def compute_homology_cluster_artifacts(
    full_structured_bytes: bytes,
    *,
    diamond_binary: str | Path = DEFAULT_DIAMOND_PATH,
    expected_binary_sha256: str = DEFAULT_DIAMOND_SHA256,
    expected_diamond_version: str = DEFAULT_DIAMOND_VERSION,
    expected_record_count: int = EXPECTED_RECORD_COUNT,
    timeout_seconds: int | float = 600,
) -> HomologyClusterArtifacts:
    """Run the frozen DIAMOND protocol and return canonical artifact bytes."""

    if not isinstance(full_structured_bytes, bytes):
        raise TypeError("full_structured_bytes must be bytes")
    expected_count = _positive_record_count(expected_record_count)
    timeout = _positive_timeout(timeout_seconds)
    expected_sha256 = _validate_expected_sha256(expected_binary_sha256)
    if not isinstance(expected_diamond_version, str) or not expected_diamond_version:
        raise HomologyClusterError("expected_diamond_version must be nonempty")
    parsed = _parse_full_structured(
        full_structured_bytes,
        expected_count=expected_count,
    )
    fasta_bytes, accession_by_id = _fasta_and_mapping(parsed.records)
    binary_path, binary_sha256 = _resolve_diamond(
        diamond_binary,
        expected_sha256=expected_sha256,
    )

    with tempfile.TemporaryDirectory(prefix="cofactor9.1-diamond-") as directory:
        workspace = Path(directory)
        fasta_path = workspace / "input.fasta"
        database_prefix = "database"
        database_path = workspace / "database.dmnd"
        cluster_output_path = workspace / "clusters.tsv"
        fasta_path.write_bytes(fasta_bytes)

        identity, version_argv = _diamond_identity(
            binary_path,
            binary_sha256,
            expected_version=expected_diamond_version,
            cwd=workspace,
            timeout_seconds=timeout,
        )
        makedb_argv = (
            str(binary_path),
            "makedb",
            "--in",
            "input.fasta",
            "--db",
            database_prefix,
            "--threads",
            str(THREADS),
            "--no-parse-seqids",
        )
        cluster_argv = (
            str(binary_path),
            "cluster",
            "--db",
            "database.dmnd",
            "--out",
            "clusters.tsv",
            "--id",
            str(IDENTITY_PERCENT),
            "--mutual-cover",
            str(MUTUAL_COVERAGE_PERCENT),
            "--threads",
            str(THREADS),
            "--no-parse-seqids",
        )
        _run_command(makedb_argv, cwd=workspace, timeout_seconds=timeout)
        _verify_binary_unchanged(identity)
        _read_generated_regular_file(database_path, description="database")
        _run_command(cluster_argv, cwd=workspace, timeout_seconds=timeout)
        _verify_binary_unchanged(identity)
        cluster_output = _read_generated_regular_file(
            cluster_output_path,
            description="cluster output",
        )

    partitions = _parse_cluster_partition(
        cluster_output,
        accession_by_id=accession_by_id,
    )
    records = _canonical_cluster_records(partitions)
    if len(records) != expected_count:
        raise HomologyClusterError(
            f"Canonical cluster rows differ from expected {expected_count}"
        )
    if len({record["accession"] for record in records}) != expected_count:
        raise HomologyClusterError("Canonical cluster rows contain duplicate accessions")

    cluster_sizes = [len(partition) for partition in partitions]
    size_distribution = Counter(cluster_sizes)
    clusters_jsonl = _jsonl_bytes(records)
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "dataset_version": parsed.dataset_version,
        "view_rule_version": parsed.view_rule_version,
        "cluster_record_schema_version": CLUSTER_RECORD_SCHEMA_VERSION,
        "algorithm": {
            "name": "DIAMOND cluster",
            "identity_percent": IDENTITY_PERCENT,
            "mutual_coverage_percent": MUTUAL_COVERAGE_PERCENT,
            "threads": THREADS,
            "no_parse_seqids": True,
            "cluster_id_version": CLUSTER_ID_VERSION,
            "stable_representative_rule": "lexicographically-smallest-accession",
        },
        "binary": {
            "path": str(identity.path),
            "sha256": identity.sha256,
            "version": identity.version,
            "version_output": identity.version_output,
        },
        "commands": {
            "version": list(version_argv),
            "makedb": list(makedb_argv),
            "cluster": list(cluster_argv),
        },
        "input_sha256": {
            "full_structured": _sha256_bytes(full_structured_bytes),
            "canonical_fasta": _sha256_bytes(fasta_bytes),
        },
        "output_sha256": {
            "homology_clusters": _sha256_bytes(clusters_jsonl),
        },
        "counts": {
            "input_records": len(parsed.records),
            "output_records": len(records),
            "unique_accessions": len({record["accession"] for record in records}),
            "cluster_count": len(partitions),
            "singleton_clusters": size_distribution.get(1, 0),
            "multi_member_clusters": sum(
                count for size, count in size_distribution.items() if size > 1
            ),
            "largest_cluster_size": max(cluster_sizes),
        },
        "cluster_size_distribution": {
            str(size): count for size, count in sorted(size_distribution.items())
        },
    }
    return HomologyClusterArtifacts(
        records=records,
        manifest=manifest,
        clusters_jsonl=clusters_jsonl,
        manifest_json=_json_bytes(manifest),
    )


def _resolve_project_artifact_paths(
    *,
    project_root: str | Path,
    full_structured_path: str | Path,
    clusters_path: str | Path,
    manifest_path: str | Path,
) -> tuple[Path, Path, Path]:
    try:
        root = Path(project_root).resolve(strict=True)
    except OSError as error:
        raise HomologyClusterError(f"Cannot resolve project root: {error}") from error
    if not root.is_dir():
        raise HomologyClusterError("Project root must be a directory")

    def resolve_relative(value: str | Path, *, description: str) -> Path:
        configured = Path(value)
        if configured.is_absolute():
            raise HomologyClusterError(
                f"Configured project {description} path must be relative"
            )
        resolved = (root / configured).resolve()
        if not resolved.is_relative_to(root):
            raise HomologyClusterError(
                f"Configured project {description} path escapes project root"
            )
        return resolved

    full = resolve_relative(full_structured_path, description="Full-Structured")
    clusters = resolve_relative(clusters_path, description="clusters")
    manifest = resolve_relative(manifest_path, description="manifest")
    if clusters.suffix != ".jsonl":
        raise HomologyClusterError("Configured project clusters path must end in .jsonl")
    if manifest.suffix != ".json":
        raise HomologyClusterError("Configured project manifest path must end in .json")
    if len({full, clusters, manifest}) != 3:
        raise HomologyClusterError("Cluster input and output paths must be distinct")
    return full, clusters, manifest


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _read_frozen_input(path: Path) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise HomologyClusterError(f"Cannot inspect Full-Structured input: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise HomologyClusterError(
            "Full-Structured input must be a regular non-symlink file"
        )
    try:
        return path.read_bytes()
    except OSError as error:
        raise HomologyClusterError(f"Cannot read Full-Structured input: {error}") from error


def _verify_regular_output(path: Path, *, description: str) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise HomologyClusterError(f"Cannot inspect {description}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise HomologyClusterError(f"{description} must be a regular non-symlink file")
    try:
        return path.read_bytes()
    except OSError as error:
        raise HomologyClusterError(f"Cannot read {description}: {error}") from error


def _verify_existing_artifacts(
    artifacts: HomologyClusterArtifacts,
    *,
    clusters_path: Path,
    manifest_path: Path,
) -> None:
    expected = {
        clusters_path: (artifacts.clusters_jsonl, "homology clusters"),
        manifest_path: (artifacts.manifest_json, "homology cluster manifest"),
    }
    for path, (payload, description) in expected.items():
        observed = _verify_regular_output(path, description=description)
        if not hmac.compare_digest(observed, payload):
            raise HomologyClusterError(
                f"Existing {description} differs from deterministic bytes"
            )


def _exclusive_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o644)
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as error:
        raise HomologyClusterError(
            f"Output appeared concurrently at {path}; refusing to overwrite"
        ) from error
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _publish_new_artifacts(
    artifacts: HomologyClusterArtifacts,
    *,
    clusters_path: Path,
    manifest_path: Path,
) -> None:
    created: list[Path] = []
    try:
        for path, value in (
            (clusters_path, artifacts.clusters_jsonl),
            (manifest_path, artifacts.manifest_json),
        ):
            _exclusive_write(path, value)
            created.append(path)
        _verify_existing_artifacts(
            artifacts,
            clusters_path=clusters_path,
            manifest_path=manifest_path,
        )
    except BaseException:
        for path in reversed(created):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _summary(
    artifacts: HomologyClusterArtifacts,
    *,
    clusters_path: Path,
    manifest_path: Path,
) -> HomologyClusterBuildSummary:
    counts = artifacts.manifest["counts"]
    return HomologyClusterBuildSummary(
        record_count=counts["output_records"],
        cluster_count=counts["cluster_count"],
        singleton_cluster_count=counts["singleton_clusters"],
        multi_member_cluster_count=counts["multi_member_clusters"],
        largest_cluster_size=counts["largest_cluster_size"],
        input_sha256=dict(artifacts.manifest["input_sha256"]),
        output_sha256={
            **dict(artifacts.manifest["output_sha256"]),
            "manifest": _sha256_bytes(artifacts.manifest_json),
        },
        clusters_path=clusters_path,
        manifest_path=manifest_path,
    )


def build_homology_clusters(
    *,
    project_root: str | Path,
    full_structured_path: str | Path = "data/derived/full_structured.jsonl",
    clusters_path: str | Path = "data/derived/homology_clusters.jsonl",
    manifest_path: str | Path = "data/derived/homology_clusters.manifest.json",
    diamond_binary: str | Path = DEFAULT_DIAMOND_PATH,
    expected_binary_sha256: str = DEFAULT_DIAMOND_SHA256,
    expected_diamond_version: str = DEFAULT_DIAMOND_VERSION,
    expected_record_count: int = EXPECTED_RECORD_COUNT,
    timeout_seconds: int | float = 600,
) -> HomologyClusterBuildSummary:
    """Build immutable cluster artifacts, or verify an identical prior build."""

    full, clusters, manifest = _resolve_project_artifact_paths(
        project_root=project_root,
        full_structured_path=full_structured_path,
        clusters_path=clusters_path,
        manifest_path=manifest_path,
    )
    existence = {
        clusters: _path_lexists(clusters),
        manifest: _path_lexists(manifest),
    }
    if any(existence.values()) and not all(existence.values()):
        raise HomologyClusterError(
            "Incomplete existing homology cluster artifact set; refusing to "
            "overwrite or repair"
        )
    artifacts = compute_homology_cluster_artifacts(
        _read_frozen_input(full),
        diamond_binary=diamond_binary,
        expected_binary_sha256=expected_binary_sha256,
        expected_diamond_version=expected_diamond_version,
        expected_record_count=expected_record_count,
        timeout_seconds=timeout_seconds,
    )
    if all(existence.values()):
        try:
            _verify_existing_artifacts(
                artifacts,
                clusters_path=clusters,
                manifest_path=manifest,
            )
        except HomologyClusterError as error:
            raise HomologyClusterError(
                "Existing homology cluster artifact set differs; refusing to "
                f"overwrite: {error}"
            ) from error
        return _summary(
            artifacts,
            clusters_path=clusters,
            manifest_path=manifest,
        )

    _publish_new_artifacts(
        artifacts,
        clusters_path=clusters,
        manifest_path=manifest,
    )
    return _summary(
        artifacts,
        clusters_path=clusters,
        manifest_path=manifest,
    )


def validate_homology_clusters(
    *,
    project_root: str | Path,
    full_structured_path: str | Path = "data/derived/full_structured.jsonl",
    clusters_path: str | Path = "data/derived/homology_clusters.jsonl",
    manifest_path: str | Path = "data/derived/homology_clusters.manifest.json",
    diamond_binary: str | Path = DEFAULT_DIAMOND_PATH,
    expected_binary_sha256: str = DEFAULT_DIAMOND_SHA256,
    expected_diamond_version: str = DEFAULT_DIAMOND_VERSION,
    expected_record_count: int = EXPECTED_RECORD_COUNT,
    timeout_seconds: int | float = 600,
) -> HomologyClusterBuildSummary:
    """Re-run DIAMOND and byte-verify existing immutable artifacts."""

    full, clusters, manifest = _resolve_project_artifact_paths(
        project_root=project_root,
        full_structured_path=full_structured_path,
        clusters_path=clusters_path,
        manifest_path=manifest_path,
    )
    missing = [
        str(path)
        for path in (clusters, manifest)
        if not _path_lexists(path)
    ]
    if missing:
        raise HomologyClusterError(
            "Missing homology cluster artifacts: " + ", ".join(missing)
        )
    artifacts = compute_homology_cluster_artifacts(
        _read_frozen_input(full),
        diamond_binary=diamond_binary,
        expected_binary_sha256=expected_binary_sha256,
        expected_diamond_version=expected_diamond_version,
        expected_record_count=expected_record_count,
        timeout_seconds=timeout_seconds,
    )
    _verify_existing_artifacts(
        artifacts,
        clusters_path=clusters,
        manifest_path=manifest,
    )
    return _summary(
        artifacts,
        clusters_path=clusters,
        manifest_path=manifest,
    )


def _config_cluster_paths(
    config_path: str | Path,
) -> tuple[Path, str, str, str]:
    configured = Path(config_path)
    try:
        config_file = configured.resolve(strict=True)
        metadata = config_file.stat()
    except OSError as error:
        raise HomologyClusterError(f"Cannot resolve benchmark config: {error}") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise HomologyClusterError("Benchmark config must be a regular file")
    try:
        decoded = _decode_json(
            config_file.read_bytes(),
            location="benchmark config",
        )
    except OSError as error:
        raise HomologyClusterError(f"Cannot read benchmark config: {error}") from error
    if not isinstance(decoded, Mapping):
        raise HomologyClusterError("Benchmark config must contain a JSON object")
    if decoded.get("schema_version") != "cofactor9.1.config.v1":
        raise HomologyClusterError("Benchmark config schema_version is unsupported")
    _required_string(decoded, "dataset_version", location="benchmark config")
    paths = _required_mapping(decoded, "paths", location="benchmark config")
    full = _required_string(
        paths,
        "full_structured",
        location="benchmark config.paths",
    )
    clusters = _required_string(
        paths,
        "homology_clusters",
        location="benchmark config.paths",
    )
    manifest = _required_string(
        paths,
        "homology_clusters_manifest",
        location="benchmark config.paths",
    )
    root = config_file.parent.parent.resolve()
    # Resolve here as an early config-boundary check.  The build/validate entry
    # points repeat the same check before touching an artifact.
    _resolve_project_artifact_paths(
        project_root=root,
        full_structured_path=full,
        clusters_path=clusters,
        manifest_path=manifest,
    )
    return root, full, clusters, manifest


def build_homology_clusters_from_config(
    config_path: str | Path,
    *,
    diamond_binary: str | Path = DEFAULT_DIAMOND_PATH,
    expected_binary_sha256: str = DEFAULT_DIAMOND_SHA256,
    expected_diamond_version: str = DEFAULT_DIAMOND_VERSION,
    expected_record_count: int = EXPECTED_RECORD_COUNT,
    timeout_seconds: int | float = 600,
) -> HomologyClusterBuildSummary:
    """Build the configured immutable cluster artifacts."""

    root, full, clusters, manifest = _config_cluster_paths(config_path)
    return build_homology_clusters(
        project_root=root,
        full_structured_path=full,
        clusters_path=clusters,
        manifest_path=manifest,
        diamond_binary=diamond_binary,
        expected_binary_sha256=expected_binary_sha256,
        expected_diamond_version=expected_diamond_version,
        expected_record_count=expected_record_count,
        timeout_seconds=timeout_seconds,
    )


def validate_homology_clusters_from_config(
    config_path: str | Path,
    *,
    diamond_binary: str | Path = DEFAULT_DIAMOND_PATH,
    expected_binary_sha256: str = DEFAULT_DIAMOND_SHA256,
    expected_diamond_version: str = DEFAULT_DIAMOND_VERSION,
    expected_record_count: int = EXPECTED_RECORD_COUNT,
    timeout_seconds: int | float = 600,
) -> HomologyClusterBuildSummary:
    """Recompute and byte-verify the configured cluster artifacts."""

    root, full, clusters, manifest = _config_cluster_paths(config_path)
    return validate_homology_clusters(
        project_root=root,
        full_structured_path=full,
        clusters_path=clusters,
        manifest_path=manifest,
        diamond_binary=diamond_binary,
        expected_binary_sha256=expected_binary_sha256,
        expected_diamond_version=expected_diamond_version,
        expected_record_count=expected_record_count,
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "CLUSTER_ID_VERSION",
    "CLUSTER_RECORD_SCHEMA_VERSION",
    "DEFAULT_DIAMOND_PATH",
    "DEFAULT_DIAMOND_SHA256",
    "DEFAULT_DIAMOND_VERSION",
    "EXPECTED_RECORD_COUNT",
    "IDENTITY_PERCENT",
    "MANIFEST_SCHEMA_VERSION",
    "MUTUAL_COVERAGE_PERCENT",
    "THREADS",
    "HomologyClusterArtifacts",
    "HomologyClusterBuildSummary",
    "HomologyClusterError",
    "build_homology_clusters",
    "build_homology_clusters_from_config",
    "compute_homology_cluster_artifacts",
    "validate_homology_clusters",
    "validate_homology_clusters_from_config",
]
