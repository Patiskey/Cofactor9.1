from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

from cofactor_bench.clusters import (
    CLUSTER_RECORD_SCHEMA_VERSION,
    DEFAULT_DIAMOND_SHA256,
    DEFAULT_DIAMOND_VERSION,
    EXPECTED_RECORD_COUNT,
    IDENTITY_PERCENT,
    MANIFEST_SCHEMA_VERSION,
    MUTUAL_COVERAGE_PERCENT,
    HomologyClusterError,
    build_homology_clusters,
    build_homology_clusters_from_config,
    compute_homology_cluster_artifacts,
    validate_homology_clusters,
    validate_homology_clusters_from_config,
)


PROJECT_ROOT = Path(__file__).parents[1]
FULL_STRUCTURED = PROJECT_ROOT / "data" / "derived" / "full_structured.jsonl"
REAL_DIAMOND = Path("/opt/homebrew/bin/diamond")


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _fixture_full_bytes(count: int = 4) -> bytes:
    sequences = ("MAAAA", "MAAAT", "MGGGG", "MCCCC")
    rows: list[dict[str, object]] = []
    for index in range(count):
        sequence = sequences[index]
        rows.append(
            {
                "schema_version": "cofactor9.1.view-record.v1",
                "dataset_version": "Cofactor9.1",
                "derivation": {"rule_version": "cofactor9.1.views.v2"},
                "entry": {"accession": f"P{index + 1:05d}"},
                "sequence": {
                    "value": sequence,
                    "sha256": hashlib.sha256(sequence.encode("ascii")).hexdigest(),
                },
            }
        )
    return b"".join(_json_bytes(row) for row in rows)


def _write_fake_diamond(root: Path) -> tuple[Path, str, Path]:
    binary = root / "fake-diamond"
    mode_path = root / "fake-diamond.mode"
    source = f"""#!{sys.executable}
from pathlib import Path
import sys

args = sys.argv[1:]
mode_path = Path(__file__).with_name("fake-diamond.mode")
mode = mode_path.read_text(encoding="utf-8").strip() if mode_path.exists() else "normal"
if args == ["version"]:
    print("diamond version 2.2.5")
    raise SystemExit(0)
if not args:
    raise SystemExit(90)
command = args[0]
def option(name):
    return args[args.index(name) + 1]
if command == "makedb":
    data = Path(option("--in")).read_bytes()
    Path(option("--db") + ".dmnd").write_bytes(data)
    raise SystemExit(0)
if command == "cluster":
    lines = Path(option("--db")).read_text(encoding="ascii").splitlines()
    ids = [line[1:] for line in lines if line.startswith(">")]
    pairs = []
    if ids:
        representative = ids[0]
        pairs.append((representative, representative))
    if len(ids) > 1:
        pairs.append((ids[0], ids[1]))
    for value in ids[2:]:
        pairs.append((value, value))
    if mode == "reverse":
        pairs.reverse()
    elif mode == "missing":
        pairs = pairs[:-1]
    elif mode == "duplicate" and pairs:
        pairs.append(pairs[-1])
    elif mode == "unknown":
        pairs.append(("seq99999999", "seq99999999"))
    Path(option("--out")).write_text(
        "".join(f"{{representative}}\\t{{member}}\\n" for representative, member in pairs),
        encoding="ascii",
    )
    raise SystemExit(0)
raise SystemExit(91)
"""
    binary.write_text(source, encoding="utf-8")
    binary.chmod(0o755)
    return binary, hashlib.sha256(binary.read_bytes()).hexdigest(), mode_path


class HomologyClusterUnitTests(unittest.TestCase):
    def test_fake_diamond_build_is_canonical_manifested_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary, binary_sha256, mode_path = _write_fake_diamond(root)
            (root / "data" / "derived").mkdir(parents=True)
            full = root / "data" / "derived" / "full.jsonl"
            full.write_bytes(_fixture_full_bytes())

            first = build_homology_clusters(
                project_root=root,
                full_structured_path="data/derived/full.jsonl",
                clusters_path="data/derived/clusters.jsonl",
                manifest_path="data/derived/clusters.manifest.json",
                diamond_binary=binary,
                expected_binary_sha256=binary_sha256,
                expected_record_count=4,
            )
            clusters = root / "data" / "derived" / "clusters.jsonl"
            manifest_path = root / "data" / "derived" / "clusters.manifest.json"
            original = {clusters: clusters.read_bytes(), manifest_path: manifest_path.read_bytes()}
            for path in original:
                os.utime(path, ns=(1_000_000_000, 1_000_000_000))

            second = build_homology_clusters(
                project_root=root,
                full_structured_path="data/derived/full.jsonl",
                clusters_path="data/derived/clusters.jsonl",
                manifest_path="data/derived/clusters.manifest.json",
                diamond_binary=binary,
                expected_binary_sha256=binary_sha256,
                expected_record_count=4,
            )
            validated = validate_homology_clusters(
                project_root=root,
                full_structured_path="data/derived/full.jsonl",
                clusters_path="data/derived/clusters.jsonl",
                manifest_path="data/derived/clusters.manifest.json",
                diamond_binary=binary,
                expected_binary_sha256=binary_sha256,
                expected_record_count=4,
            )

            self.assertEqual(first, second)
            self.assertEqual(second, validated)
            self.assertEqual(original, {path: path.read_bytes() for path in original})
            self.assertTrue(all(path.stat().st_mtime_ns == 1_000_000_000 for path in original))
            rows = [json.loads(line) for line in clusters.read_text().splitlines()]
            self.assertEqual([row["accession"] for row in rows], sorted(row["accession"] for row in rows))
            self.assertEqual(len(rows), 4)
            self.assertEqual(len({row["accession"] for row in rows}), 4)
            self.assertEqual(rows[0]["cluster_id"], rows[1]["cluster_id"])
            self.assertEqual(rows[0]["member_count"], 2)
            self.assertEqual(rows[0]["representative_accession"], "P00001")
            self.assertTrue(
                all(
                    set(row)
                    == {
                        "accession",
                        "cluster_id",
                        "member_count",
                        "representative_accession",
                        "schema_version",
                    }
                    for row in rows
                )
            )
            self.assertTrue(all(row["schema_version"] == CLUSTER_RECORD_SCHEMA_VERSION for row in rows))

            manifest = json.loads(manifest_path.read_bytes())
            self.assertEqual(manifest["schema_version"], MANIFEST_SCHEMA_VERSION)
            self.assertEqual(manifest["algorithm"]["identity_percent"], IDENTITY_PERCENT)
            self.assertEqual(manifest["algorithm"]["mutual_coverage_percent"], MUTUAL_COVERAGE_PERCENT)
            self.assertEqual(manifest["algorithm"]["threads"], 1)
            self.assertTrue(manifest["algorithm"]["no_parse_seqids"])
            self.assertEqual(manifest["binary"]["sha256"], binary_sha256)
            self.assertEqual(manifest["binary"]["version"], DEFAULT_DIAMOND_VERSION)
            self.assertEqual(
                manifest["counts"],
                {
                    "cluster_count": 3,
                    "input_records": 4,
                    "largest_cluster_size": 2,
                    "multi_member_clusters": 1,
                    "output_records": 4,
                    "singleton_clusters": 2,
                    "unique_accessions": 4,
                },
            )
            self.assertEqual(manifest["cluster_size_distribution"], {"1": 2, "2": 1})
            self.assertEqual(
                manifest["output_sha256"]["homology_clusters"],
                hashlib.sha256(clusters.read_bytes()).hexdigest(),
            )
            for command in ("makedb", "cluster"):
                argv = manifest["commands"][command]
                self.assertEqual(argv[0], str(binary.resolve()))
                self.assertIn("--threads", argv)
                self.assertEqual(argv[argv.index("--threads") + 1], "1")
                self.assertIn("--no-parse-seqids", argv)
            cluster_argv = manifest["commands"]["cluster"]
            self.assertEqual(cluster_argv[cluster_argv.index("--id") + 1], "90")
            self.assertEqual(cluster_argv[cluster_argv.index("--mutual-cover") + 1], "80")
            self.assertEqual(
                manifest["commands"],
                {
                    "version": [str(binary.resolve()), "version"],
                    "makedb": [
                        str(binary.resolve()),
                        "makedb",
                        "--in",
                        "input.fasta",
                        "--db",
                        "database",
                        "--threads",
                        "1",
                        "--no-parse-seqids",
                    ],
                    "cluster": [
                        str(binary.resolve()),
                        "cluster",
                        "--db",
                        "database.dmnd",
                        "--out",
                        "clusters.tsv",
                        "--id",
                        "90",
                        "--mutual-cover",
                        "80",
                        "--threads",
                        "1",
                        "--no-parse-seqids",
                    ],
                },
            )

            mode_path.write_text("reverse", encoding="utf-8")
            recomputed = compute_homology_cluster_artifacts(
                full.read_bytes(),
                diamond_binary=binary,
                expected_binary_sha256=binary_sha256,
                expected_record_count=4,
            )
            self.assertEqual(recomputed.clusters_jsonl, clusters.read_bytes())
            self.assertEqual(recomputed.manifest_json, manifest_path.read_bytes())

    def test_malformed_diamond_membership_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary, binary_sha256, mode_path = _write_fake_diamond(root)
            for mode, message in (
                ("missing", "cover"),
                ("duplicate", "Duplicate"),
                ("unknown", "unknown"),
            ):
                with self.subTest(mode=mode):
                    mode_path.write_text(mode, encoding="utf-8")
                    with self.assertRaisesRegex(HomologyClusterError, message):
                        compute_homology_cluster_artifacts(
                            _fixture_full_bytes(),
                            diamond_binary=binary,
                            expected_binary_sha256=binary_sha256,
                            expected_record_count=4,
                        )

    def test_frozen_binary_hash_and_version_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary, binary_sha256, _ = _write_fake_diamond(root)
            with self.assertRaisesRegex(HomologyClusterError, "SHA256"):
                compute_homology_cluster_artifacts(
                    _fixture_full_bytes(),
                    diamond_binary=binary,
                    expected_binary_sha256="0" * 64,
                    expected_record_count=4,
                )
            with self.assertRaisesRegex(HomologyClusterError, "version"):
                compute_homology_cluster_artifacts(
                    _fixture_full_bytes(),
                    diamond_binary=binary,
                    expected_binary_sha256=binary_sha256,
                    expected_diamond_version="9.9.9",
                    expected_record_count=4,
                )

    def test_full_input_integrity_is_checked_before_diamond_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary, binary_sha256, _ = _write_fake_diamond(root)
            rows = [json.loads(line) for line in _fixture_full_bytes().splitlines()]
            rows[1]["entry"]["accession"] = rows[0]["entry"]["accession"]
            bad_accessions = b"".join(_json_bytes(row) for row in rows)
            with self.assertRaisesRegex(HomologyClusterError, "Duplicate accession"):
                compute_homology_cluster_artifacts(
                    bad_accessions,
                    diamond_binary=binary,
                    expected_binary_sha256=binary_sha256,
                    expected_record_count=4,
                )

            rows = [json.loads(line) for line in _fixture_full_bytes().splitlines()]
            rows[1]["sequence"]["sha256"] = "f" * 64
            bad_hash = b"".join(_json_bytes(row) for row in rows)
            with self.assertRaisesRegex(HomologyClusterError, "sequence SHA256"):
                compute_homology_cluster_artifacts(
                    bad_hash,
                    diamond_binary=binary,
                    expected_binary_sha256=binary_sha256,
                    expected_record_count=4,
                )

    def test_project_artifact_paths_must_be_relative_and_cannot_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary, binary_sha256, _ = _write_fake_diamond(root)
            for configured in ("../outside.jsonl", str(root / "absolute.jsonl")):
                with self.subTest(configured=configured):
                    with self.assertRaisesRegex(HomologyClusterError, "project"):
                        build_homology_clusters(
                            project_root=root,
                            full_structured_path=configured,
                            clusters_path="data/clusters.jsonl",
                            manifest_path="data/clusters.manifest.json",
                            diamond_binary=binary,
                            expected_binary_sha256=binary_sha256,
                            expected_record_count=4,
                        )

    def test_config_entry_points_use_declared_paths_and_reject_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary, binary_sha256, _ = _write_fake_diamond(root)
            (root / "config").mkdir()
            (root / "data").mkdir()
            (root / "data" / "full.jsonl").write_bytes(_fixture_full_bytes())
            config_path = root / "config" / "benchmark.json"
            config = {
                "schema_version": "cofactor9.1.config.v1",
                "dataset_version": "Cofactor9.1",
                "paths": {
                    "full_structured": "data/full.jsonl",
                    "homology_clusters": "data/clusters.jsonl",
                    "homology_clusters_manifest": "data/clusters.manifest.json",
                },
            }
            config_path.write_bytes(_json_bytes(config))

            built = build_homology_clusters_from_config(
                config_path,
                diamond_binary=binary,
                expected_binary_sha256=binary_sha256,
                expected_record_count=4,
            )
            validated = validate_homology_clusters_from_config(
                config_path,
                diamond_binary=binary,
                expected_binary_sha256=binary_sha256,
                expected_record_count=4,
            )
            self.assertEqual(built, validated)
            self.assertEqual(built.record_count, 4)

            config["paths"]["homology_clusters"] = "../outside.jsonl"
            config_path.write_bytes(_json_bytes(config))
            with self.assertRaisesRegex(HomologyClusterError, "project"):
                build_homology_clusters_from_config(
                    config_path,
                    diamond_binary=binary,
                    expected_binary_sha256=binary_sha256,
                    expected_record_count=4,
                )

    def test_partial_or_tampered_existing_artifacts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary, binary_sha256, _ = _write_fake_diamond(root)
            (root / "data").mkdir()
            (root / "data" / "full.jsonl").write_bytes(_fixture_full_bytes())
            clusters = root / "data" / "clusters.jsonl"
            clusters.write_bytes(b"sentinel\n")
            with self.assertRaisesRegex(HomologyClusterError, "Incomplete.*refusing"):
                build_homology_clusters(
                    project_root=root,
                    full_structured_path="data/full.jsonl",
                    clusters_path="data/clusters.jsonl",
                    manifest_path="data/clusters.manifest.json",
                    diamond_binary=binary,
                    expected_binary_sha256=binary_sha256,
                    expected_record_count=4,
                )
            self.assertEqual(clusters.read_bytes(), b"sentinel\n")

            clusters.unlink()
            build_homology_clusters(
                project_root=root,
                full_structured_path="data/full.jsonl",
                clusters_path="data/clusters.jsonl",
                manifest_path="data/clusters.manifest.json",
                diamond_binary=binary,
                expected_binary_sha256=binary_sha256,
                expected_record_count=4,
            )
            clusters.write_bytes(b"tampered\n")
            with self.assertRaisesRegex(HomologyClusterError, "refusing to overwrite"):
                build_homology_clusters(
                    project_root=root,
                    full_structured_path="data/full.jsonl",
                    clusters_path="data/clusters.jsonl",
                    manifest_path="data/clusters.manifest.json",
                    diamond_binary=binary,
                    expected_binary_sha256=binary_sha256,
                    expected_record_count=4,
                )
            self.assertEqual(clusters.read_bytes(), b"tampered\n")


@unittest.skipUnless(FULL_STRUCTURED.is_file() and REAL_DIAMOND.exists(), "frozen integration inputs unavailable")
class FrozenHomologyClusterIntegrationTests(unittest.TestCase):
    def test_real_diamond_covers_all_5337_full_records_deterministically(self) -> None:
        artifacts = compute_homology_cluster_artifacts(
            FULL_STRUCTURED.read_bytes(),
            diamond_binary=REAL_DIAMOND,
            expected_binary_sha256=DEFAULT_DIAMOND_SHA256,
            expected_record_count=EXPECTED_RECORD_COUNT,
            timeout_seconds=1_200,
        )
        rows = [json.loads(line) for line in artifacts.clusters_jsonl.splitlines()]
        self.assertEqual(len(rows), EXPECTED_RECORD_COUNT)
        self.assertEqual(len({row["accession"] for row in rows}), EXPECTED_RECORD_COUNT)
        self.assertEqual(artifacts.manifest["binary"]["version"], DEFAULT_DIAMOND_VERSION)
        self.assertEqual(artifacts.manifest["counts"]["input_records"], EXPECTED_RECORD_COUNT)
        self.assertEqual(artifacts.manifest["counts"]["output_records"], EXPECTED_RECORD_COUNT)
        self.assertEqual(
            artifacts.manifest["counts"],
            {
                "cluster_count": 5066,
                "input_records": 5337,
                "largest_cluster_size": 6,
                "multi_member_clusters": 226,
                "output_records": 5337,
                "singleton_clusters": 4840,
                "unique_accessions": 5337,
            },
        )
        self.assertEqual(
            artifacts.manifest["cluster_size_distribution"],
            {"1": 4840, "2": 190, "3": 29, "4": 6, "6": 1},
        )
        self.assertEqual(
            artifacts.manifest["input_sha256"]["full_structured"],
            "57cf6b5c74de55306b9bbc623514aabdfed6919f291e92ec8ddcfc21599a44fa",
        )
        self.assertEqual(
            artifacts.manifest["input_sha256"]["canonical_fasta"],
            "30c8e783ad854cb8bc77b99fdd5dcaf1fca0233407ce427bba034f7437f7d3a8",
        )
        self.assertEqual(
            artifacts.manifest["output_sha256"]["homology_clusters"],
            "330cf792ffe6b2c8888a1cc3bf6b2865c420f90e42d78ccd47fff3897cc91671",
        )
        self.assertEqual(sum(row["member_count"] > 1 for row in rows), 497)

        cluster_by_accession = {row["accession"]: row["cluster_id"] for row in rows}
        exact_entities: dict[str, tuple[str, ...]] = {}
        for line in FULL_STRUCTURED.read_bytes().splitlines():
            source = json.loads(line)
            exact = source["derived"]["exact_sequence"]
            exact_entities[exact["sequence_entity_id"]] = tuple(exact["members"])
        duplicate_entities = [
            members for members in exact_entities.values() if len(members) > 1
        ]
        self.assertEqual(len(exact_entities), 5295)
        self.assertEqual(len(duplicate_entities), 39)
        self.assertEqual(sum(map(len, duplicate_entities)), 81)
        self.assertTrue(
            all(
                len({cluster_by_accession[accession] for accession in members}) == 1
                for members in duplicate_entities
            )
        )


if __name__ == "__main__":
    unittest.main()
