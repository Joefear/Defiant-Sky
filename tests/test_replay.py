from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sgp4.io import fix_checksum

from server.evidence import Settings, detect, parse_element_set
from server.replay import Dataset, ReplayConfig, distribution, replay


def envelope(raw, record_id):
    row = dict(raw, record_id=record_id)
    parsed = parse_element_set(row, datetime.fromisoformat(row["ingested_at"]), "demo")
    row["epoch"] = parsed.epoch.isoformat()
    row["content_sha256"] = parsed.provenance.content_sha256
    return row


@pytest.fixture
def dataset(fixture):
    return {"schema_version": 1, "dataset_id": "unit-synthetic", "kind": "synthetic",
            "source": "DS-M1 synthetic fixture", "usage_basis": "Repository-authored synthetic test data",
            "records": [envelope(raw, f"set-{i}") for i, raw in enumerate(fixture["records"])]}


@pytest.fixture
def config():
    return ReplayConfig(training_end="2026-11-05T18:00:00Z", calibration_end="2026-11-06T12:00:00Z")


def run(dataset, config):
    return replay(Dataset.model_validate(dataset), config)


def changed(row, *, day=None, anomaly=None, record_id="changed", at=None):
    row = dict(row, record_id=record_id)
    if day is not None:
        row["tle1"] = fix_checksum(row["tle1"][:20] + f"{day:012.8f}" + row["tle1"][32:])
    if anomaly is not None:
        row["tle2"] = fix_checksum(row["tle2"][:43] + f"{anomaly:8.4f}" + row["tle2"][51:])
    if at:
        row["ingested_at"] = at
    row.pop("epoch", None)
    row.pop("content_sha256", None)
    return envelope(row, record_id)


def test_reuses_detector_and_known_sgp4_result(dataset, config, monkeypatch):
    calls = []

    def spy(records, now, settings):
        assert all(r.ingested_at <= now for r in records)
        calls.append(now)
        return detect(records, now, settings)

    monkeypatch.setattr("server.replay.evidence.detect", spy)
    report = run(dataset, config)
    assert len(calls) == 2
    assert report["metrics"]["valid_comparable_pairs"] == 1
    assert report["metrics"]["insufficient_data_outcomes"] == 1
    assert report["metrics"]["objects_evaluated"] == 1
    assert report["outcomes"][1]["separation_km"] == pytest.approx(223.72912913292396, abs=1e-6)
    assert report["metrics"]["epoch_gap_hours"]["median"] == 12
    assert [r["candidate_rate"] for r in report["metrics"]["threshold_sensitivity"]] == [1, 1, 1, 0]
    assert not report["action_authorized"]
    assert all(not r["action_authorized"] for r in report["outcomes"])
    assert "INCOMPLETE" in report["calibration"]["status"]


def test_determinism_and_input_order(dataset, config):
    first = run(dataset, config)
    assert first == run(dataset, config)
    dataset["records"].reverse()
    reversed_report = run(dataset, config)
    assert first["outcomes"] == reversed_report["outcomes"]
    assert first["partitions"] == reversed_report["partitions"]
    assert first["dataset"]["canonical_sha256"] != reversed_report["dataset"]["canonical_sha256"]


def test_duplicate_keeps_original_ingestion_and_does_not_count_pair(dataset, config):
    original = deepcopy(dataset["records"][1])
    dataset["records"].append(dict(original, record_id="duplicate", ingested_at="2026-11-06T13:00:00Z"))
    report = run(dataset, config)
    assert report["exclusion_counts"] == {"DUPLICATE CONTENT": 1}
    assert report["coverage"]["accepted_records"] == 2
    assert report["metrics"]["valid_comparable_pairs"] == 1
    assert report["partitions"]["held_out"]["valid_comparable_pairs"] == 0
    assert report["accepted_records"][1]["ingested_at"] == original["ingested_at"]


def test_identical_and_empty_negative_controls(dataset, config):
    dataset["records"] = [dataset["records"][0], dict(dataset["records"][0], record_id="duplicate")]
    report = run(dataset, config)
    assert report["metrics"]["insufficient_data_outcomes"] == 1
    assert report["metrics"]["valid_comparable_pairs"] == 0
    assert all(r["candidate_rate"] is None for r in report["metrics"]["threshold_sensitivity"])
    dataset["records"] = []
    assert run(dataset, config)["metrics"]["objects_evaluated"] == 0


@pytest.mark.parametrize("case,quality", [("stale", "STALE OR FUTURE"), ("future", "STALE OR FUTURE"),
                                         ("gap", "INCOMPARABLE EPOCHS"), ("ambiguous", "INCOMPARABLE EPOCHS")])
def test_negative_quality_controls(dataset, config, case, quality):
    if case == "stale":
        dataset["records"][1]["ingested_at"] = "2026-11-10T00:05:00Z"
    elif case == "future":
        dataset["records"][1]["ingested_at"] = "2026-11-05T23:00:00Z"
    elif case == "gap":
        config = config.model_copy(update={"settings": Settings(max_gap_hours=1)})
    else:
        dataset["records"].append(changed(dataset["records"][1], anomaly=5))
    report = run(dataset, config)
    assert report["outcomes"][-1]["quality"] == quality
    assert report["metrics"]["valid_comparable_pairs"] == 0


@pytest.mark.parametrize("damage", ["tle", "hash", "epoch", "catalog", "naive", "extra"])
def test_corrupt_records_are_excluded_without_echoing_input(dataset, config, damage):
    row = dataset["records"][1]
    if damage == "tle": row["tle1"] = "PRIVATE-CORRUPTED-RECORD"
    if damage == "hash": row["content_sha256"] = "0" * 64
    if damage == "epoch": row["epoch"] = "2026-11-04T00:00:00Z"
    if damage == "catalog": row["catalog_id"] = 12345
    if damage == "naive": row["ingested_at"] = "2026-11-06T00:05:00"
    if damage == "extra": row["secret"] = "PRIVATE-CORRUPTED-RECORD"
    report = run(dataset, config)
    assert report["coverage"]["excluded_records"] == 1
    assert report["metrics"]["valid_comparable_pairs"] == 0
    serialized = json.dumps(report)
    assert "PRIVATE-CORRUPTED-RECORD" not in serialized
    assert "tle1" not in serialized and "tle2" not in serialized


def test_quarantine_duplicate_cannot_clear_but_newer_valid_can(dataset, config):
    bad = changed(dataset["records"][1], day=310.25, record_id="bad", at="2026-11-06T06:05:00Z")
    bad["content_sha256"] = "0" * 64
    dataset["records"] += [bad, dict(dataset["records"][1], record_id="dup", ingested_at="2026-11-06T07:00:00Z"),
                           changed(dataset["records"][1], day=310.5, record_id="recovery", at="2026-11-06T12:05:00Z")]
    report = run(dataset, config)
    assert report["outcomes"][-2]["quality"] == "REJECTED UPDATE QUARANTINE"
    assert report["outcomes"][-1]["separation_km"] is not None


def test_propagation_failure_remains_insufficient(dataset, config, monkeypatch):
    def failure(*args):
        raise ValueError("synthetic propagator failure")
    monkeypatch.setattr("server.evidence.separation_km", failure)
    report = run(dataset, config)
    assert report["outcomes"][-1]["quality"] == "PROPAGATION FAILED"
    assert report["metrics"]["valid_comparable_pairs"] == 0
    assert report["metrics"]["insufficient_data_outcomes"] == 2


def test_threshold_strict_equality_and_distribution(dataset, config):
    metric = run(dataset, config)["outcomes"][-1]["separation_km"]
    config = config.model_copy(update={"thresholds_km": (metric,), "settings": Settings(threshold_km=metric)})
    report = run(dataset, config)
    assert report["outcomes"][-1]["status"] == "WITHIN THRESHOLD"
    assert report["metrics"]["threshold_sensitivity"][0]["exceedance_count"] == 0
    assert distribution([0, 10, 20, 30]) == {"count": 4, "min": 0, "median": 15, "p95": pytest.approx(28.5), "max": 30}


def test_held_out_cannot_change_prefix_or_calibration(dataset, config):
    prefix = run(dataset, config)
    late = changed(dataset["records"][1], day=310.5, anomaly=120, record_id="held-out", at="2026-11-06T12:00:00Z")
    dataset["records"].insert(0, late)  # File order is not availability order.
    report = run(dataset, config)
    assert report["outcomes"][:2] == prefix["outcomes"]
    for partition in ("training", "calibration"):
        assert report["partitions"][partition] == prefix["partitions"][partition]
    assert report["partitions"]["held_out"]["valid_comparable_pairs"] == 1
    assert report["outcomes"][-1]["evidence"][0]["ingested_at"] < config.calibration_end.isoformat()
    assert report["calibration"] == prefix["calibration"]


def test_late_older_epoch_does_not_repeat_pair(dataset, config):
    dataset["records"].append(changed(dataset["records"][0], day=309, record_id="late-old", at="2026-11-06T13:00:00Z"))
    report = run(dataset, config)
    assert report["metrics"]["evaluation_count"] == 3
    assert report["metrics"]["valid_comparable_pairs"] == 1
    assert not report["outcomes"][-1]["new_comparable_pair"]


def test_equal_arrival_batch_is_atomic_and_reused_id_fails_closed(dataset, config):
    dataset["records"][0]["ingested_at"] = dataset["records"][1]["ingested_at"]
    assert run(dataset, config)["metrics"]["evaluation_count"] == 1
    dataset["records"][1]["record_id"] = dataset["records"][0]["record_id"]
    report = run(dataset, config)
    assert report["exclusion_counts"] == {"REUSED RECORD ID": 1}
    assert report["metrics"]["valid_comparable_pairs"] == 0


@pytest.mark.parametrize("grid", [(), (10, 1), (1, 1), (0,), (-1,), (float("nan"),), (float("inf"),)])
def test_invalid_threshold_grids_rejected(config, grid):
    with pytest.raises(ValueError):
        ReplayConfig(**dict(config.model_dump(), thresholds_km=grid))


def test_origin_and_split_boundaries(dataset, config):
    assert config.partition(config.training_end) == "calibration"
    assert config.partition(config.calibration_end) == "held_out"
    with pytest.raises(ValueError):
        ReplayConfig(training_end=config.calibration_end, calibration_end=config.training_end)
    dataset["kind"] = "real"
    with pytest.raises(ValueError): Dataset.model_validate(dataset)
    dataset["source"] = "Space-Track.org"
    # Only tests schema support, not a claim that the synthetic TLEs are real.
    assert Dataset.model_validate(dataset).kind == "real"


def test_cli_never_opens_live_storage_or_network(dataset, config, tmp_path):
    input_path, config_path = tmp_path / "dataset.json", tmp_path / "config.json"
    input_path.write_text(json.dumps(dataset), encoding="utf-8")
    config_path.write_text(config.model_dump_json(), encoding="utf-8")
    sentinel = tmp_path / "do-not-touch.sqlite3"
    sentinel.write_bytes(b"persistent history and polling reservation sentinel")
    before = sha256(sentinel.read_bytes()).hexdigest()
    env = dict(os.environ, DS_LIVE_ENABLED="1", DS_HISTORY_PATH=str(sentinel), PYTHONDONTWRITEBYTECODE="1")
    # Fresh process: reject provider/storage imports and socket access before replay import.
    code = '''import sys, socket, runpy
class Block:
    def find_spec(self, name, *args):
        if name in ("server.main", "server.history", "server.tle_ingestor", "server.governance", "sqlite3", "httpx"):
            raise AssertionError("Forbidden live/storage import: " + name)
sys.meta_path.insert(0, Block())
def deny(*args, **kwargs): raise AssertionError("Network forbidden")
socket.socket = deny
sys.argv = ["server.replay", sys.argv[1], "--config", sys.argv[2]]
runpy.run_module("server.replay", run_name="__main__")
'''
    result = subprocess.run([sys.executable, "-c", code, str(input_path), str(config_path)],
                            cwd=Path(__file__).parents[1], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["metrics"]["valid_comparable_pairs"] == 1
    assert sha256(sentinel.read_bytes()).hexdigest() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.json", "dataset.json", "do-not-touch.sqlite3"]


def test_cli_bad_input_is_sanitized(tmp_path):
    from server.replay import main
    source = tmp_path / "private.json"
    source.write_text("not json", encoding="utf-8")
    assert main([str(source), "--config", str(source)]) == 2


def test_committed_sample_coverage_and_partition_metrics():
    fixtures = Path(__file__).parents[1] / "server/fixtures"
    dataset = Dataset.model_validate_json((fixtures / "replay-synthetic.json").read_bytes())
    config = ReplayConfig.model_validate_json((fixtures / "replay-config.json").read_bytes())
    report = replay(dataset, config)
    assert dataset.kind == "synthetic"
    assert report["coverage"]["input_records"] == 8
    assert report["coverage"]["accepted_records"] == 6
    assert report["coverage"]["excluded_records"] == 2
    assert report["metrics"]["objects_evaluated"] == 2
    assert report["metrics"]["valid_comparable_pairs"] == 4
    assert report["metrics"]["insufficient_data_outcomes"] == 3
    assert [report["partitions"][p]["valid_comparable_pairs"] for p in ("training", "calibration", "held_out")] == [0, 2, 2]
    assert [x["exceedance_count"] for x in report["metrics"]["threshold_sensitivity"]] == [4, 4, 2, 0]
    assert report["metrics"]["separation_km"]["min"] == pytest.approx(61.88429733773264, abs=1e-6)


def test_simultaneous_quarantine_boundary_does_not_depend_on_record_ids(dataset, config):
    first = dataset["records"][0]
    valid = changed(first, day=310.5, record_id="z-valid", at="2026-11-06T12:05:00Z")
    bad = changed(first, day=310, record_id="a-bad", at="2026-11-06T12:05:00Z")
    bad["content_sha256"] = "0" * 64
    late_old = changed(first, day=309.75, record_id="late-old", at="2026-11-06T13:05:00Z")
    recovery = changed(first, day=311, record_id="recovery", at="2026-11-07T00:05:00Z")
    dataset["records"] = [first, bad, valid, late_old, recovery]
    before = run(dataset, config)
    bad["record_id"] = "zz-bad"  # Same content/time, reverses internal tie processing.
    after = run(dataset, config)
    assert before["outcomes"] == after["outcomes"]
    assert before["metrics"] == after["metrics"]
    assert before["outcomes"][-2]["quality"] == "REJECTED UPDATE QUARANTINE"
    assert before["outcomes"][-1]["new_comparable_pair"]
