"""Offline, in-memory arrival-time replay. No app, provider, history or policy imports."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
import platform
import sys
from typing import Literal

from pydantic import AwareDatetime, Field, StrictInt, ValidationError, model_validator

from . import evidence
from .evidence import Record, Settings, parse_element_set


class Dataset(Record):
    schema_version: Literal[1]
    dataset_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    kind: Literal["synthetic", "real"]
    source: Literal["DS-M1 synthetic fixture", "Space-Track.org"]
    # An operator assertion, not an independently verified license or attestation.
    usage_basis: str = Field(min_length=1, max_length=512)
    records: list[dict]

    @model_validator(mode="after")
    def consistent_origin(self):
        if (self.kind == "synthetic") != (self.source == "DS-M1 synthetic fixture"):
            raise ValueError("Dataset kind/source mismatch")
        return self


class InputRecord(Record):
    record_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    catalog_id: StrictInt = Field(ge=1, le=99999)
    name: str
    epoch: AwareDatetime
    ingested_at: AwareDatetime
    tle1: str
    tle2: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReplayConfig(Record):
    # Windows are defined by availability (original ingestion time), NOT element epoch.
    training_end: AwareDatetime
    calibration_end: AwareDatetime
    thresholds_km: tuple[float, ...] = (1.0, 10.0, 100.0, 300.0)
    settings: Settings = Settings()

    @model_validator(mode="after")
    def valid_plan(self):
        if self.training_end >= self.calibration_end:
            raise ValueError("training_end must precede calibration_end")
        if not self.thresholds_km or tuple(sorted(set(self.thresholds_km))) != self.thresholds_km:
            raise ValueError("Threshold grid must be nonempty, unique and increasing")
        for threshold in self.thresholds_km:
            Settings(threshold_km=threshold)  # Same finite, positive constraint as DS-M1.
        return self

    def partition(self, at: datetime) -> str:
        return "training" if at < self.training_end else "calibration" if at < self.calibration_end else "held_out"


def identity(value) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def distribution(values: list[float]) -> dict:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "min": None, "median": None, "p95": None, "max": None}

    def quantile(p):
        index = (len(ordered) - 1) * p
        lower = int(index)
        upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)

    return {"count": len(ordered), "min": ordered[0], "median": quantile(.5), "p95": quantile(.95), "max": ordered[-1]}


def summarize(outcomes: list[dict], thresholds: tuple[float, ...]) -> dict:
    pairs = [row for row in outcomes if row["new_comparable_pair"]]
    separations = [row["separation_km"] for row in pairs]
    return {
        "objects_evaluated": len({row["catalog_id"] for row in outcomes}),
        "evaluation_count": len(outcomes),
        "valid_comparable_pairs": len(pairs),
        "insufficient_data_outcomes": sum(row["status"] == "INSUFFICIENT DATA" for row in outcomes),
        "quality_counts": dict(sorted(Counter(row["quality"] for row in outcomes).items())),
        "epoch_gap_hours": distribution([row["epoch_gap_hours"] for row in pairs]),
        "separation_km": distribution(separations),
        "threshold_sensitivity": [
            {"threshold_km": threshold, "exceedance_count": sum(x > threshold for x in separations),
             "candidate_rate": sum(x > threshold for x in separations) / len(pairs) if pairs else None,
             "denominator": len(pairs)} for threshold in thresholds
        ],
    }


def replay(dataset: Dataset, config: ReplayConfig) -> dict:
    """Reparse every TLE, group simultaneous arrivals, then call the unchanged detector.

    Whole-file validation never changes an earlier accepted event based on future
    records. Record IDs and content are deduplicated only when they become available.
    No synthetic/real mixing, store writes, retention or wall-clock access occurs.
    """
    mode = "demo" if dataset.kind == "synthetic" else "real"
    exclusions = []
    arrivals = defaultdict(list)

    def exclude(index, reason, record_id=None, at=None):
        exclusions.append({"input_index": index, "record_id": record_id, "reason": reason,
                           "partition": config.partition(at) if at is not None else "unassigned"})

    for index, raw in enumerate(dataset.records):
        try:
            row = InputRecord.model_validate(raw)
        except ValidationError:
            # Do not echo raw values or exception strings (which may contain source data).
            exclude(index, "INVALID RECORD SCHEMA")
            continue
        try:
            record = parse_element_set(row.model_dump(), row.ingested_at, mode)
            if record.provenance.content_sha256 != row.content_sha256:
                raise ValueError("Content identity mismatch")
        except (ValueError, TypeError, KeyError, OverflowError):
            arrivals[row.ingested_at].append((row.catalog_id, index, row, None))
            continue
        arrivals[row.ingested_at].append((record.catalog_id, index, row, record))

    histories = defaultdict(dict)
    seen_ids = set()
    seen_pairs = set()
    outcomes = []
    accepted = []
    # A malformed identified arrival suppresses that object's comparisons until a
    # strictly newer valid epoch. This wrapper gate never computes a new detector.
    quarantines = {}
    for at, batch in sorted(arrivals.items()):
        objects = defaultdict(list)
        for item in batch:
            objects[item[0]].append(item)
        for catalog_id, items in sorted(objects.items()):
            history = histories[catalog_id]
            changed = False
            bad = False
            # Content-first order is stable under input order changes, including ties.
            for _, index, row, record in sorted(items, key=lambda item: (item[2].record_id, item[2].content_sha256)):
                if record is None:
                    exclude(index, "INVALID TLE OR CONTENT IDENTITY", row.record_id, at)
                    bad = True
                    quarantines[catalog_id] = max(quarantines.get(catalog_id, row.epoch), row.epoch,
                                                  *(r.epoch for r in history.values()))
                    continue
                if row.record_id in seen_ids:
                    exclude(index, "REUSED RECORD ID", row.record_id, at)
                    bad = True
                    quarantines[catalog_id] = max(quarantines.get(catalog_id, record.epoch), record.epoch,
                                                  *(r.epoch for r in history.values()))
                    continue
                seen_ids.add(row.record_id)
                content = record.provenance.content_sha256
                if content in history:
                    exclude(index, "DUPLICATE CONTENT", row.record_id, at)
                    continue
                history[content] = record
                changed = True
                accepted.append({"record_id": row.record_id, "catalog_id": catalog_id,
                                 "epoch": utc(record.epoch), "ingested_at": utc(record.ingested_at),
                                 "content_sha256": content, "partition": config.partition(at)})
            if not changed and not bad:
                continue  # A duplicate retrieval never creates another pair/decision.
            newest = max((r.epoch for r in history.values()), default=None)
            if bad and newest is not None:
                # Freeze the quarantine after the entire simultaneous batch. IDs
                # must not decide whether a co-arriving valid set can clear it later.
                quarantines[catalog_id] = max(quarantines[catalog_id], newest)
            if not bad and newest is not None and catalog_id in quarantines and newest > quarantines[catalog_id]:
                del quarantines[catalog_id]
            result = {"catalog_id": catalog_id, "evaluated_at": utc(at), "partition": config.partition(at),
                      "status": "INSUFFICIENT DATA", "quality": "REJECTED UPDATE QUARANTINE",
                      "separation_km": None, "epoch_gap_hours": None, "evidence": [],
                      "new_comparable_pair": False, "action_authorized": False}
            if history and catalog_id not in quarantines:
                finding = evidence.detect(list(history.values()), at, config.settings)
                result.update(status=finding.status, quality=finding.quality, separation_km=finding.separation_km,
                              action_authorized=finding.action_authorized)
                selected = [view.record for view in finding.evidence]
                result["evidence"] = [{"content_sha256": r.provenance.content_sha256,
                                       "epoch": utc(r.epoch), "ingested_at": utc(r.ingested_at)} for r in selected]
                if len(selected) == 2:
                    result["epoch_gap_hours"] = (selected[1].epoch - selected[0].epoch).total_seconds() / 3600
                if finding.separation_km is not None:
                    key = (catalog_id, *(r.provenance.content_sha256 for r in selected))
                    result["new_comparable_pair"] = key not in seen_pairs
                    seen_pairs.add(key)
            outcomes.append(result)

    config_json = config.model_dump(mode="json")
    partitions = {}
    for partition in ("training", "calibration", "held_out"):
        subset = [row for row in outcomes if row["partition"] == partition]
        partitions[partition] = summarize(subset, config.thresholds_km)
        partitions[partition]["exclusions"] = dict(sorted(Counter(
            row["reason"] for row in exclusions if row["partition"] == partition).items()))
        # Prefix-scoped identity, so future data cannot change the calibration record.
        partitions[partition]["accepted_records_sha256"] = identity([r for r in accepted if r["partition"] == partition])
    return {
        "schema_version": 1, "dataset": {"dataset_id": dataset.dataset_id, "kind": dataset.kind,
            "source": dataset.source, "usage_basis": dataset.usage_basis,
            "canonical_sha256": identity(dataset.model_dump(mode="json"))},
        "configuration": config_json, "configuration_sha256": identity(config_json),
        "reproducibility": {"detector": "server.evidence.detect", "sgp4": version("sgp4"),
            "python": platform.python_version(), "detector_source_sha256": sha256(Path(evidence.__file__).read_bytes()).hexdigest(),
            "replay_source_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
            "ordering": "original ingested_at UTC; simultaneous arrivals per object applied atomically",
            "clock": "original ingestion timestamp; no wall clock", "storage": "memory only"},
        "coverage": {"input_records": len(dataset.records), "accepted_records": len(accepted),
            "excluded_records": len(exclusions), "arrival_start": utc(min(arrivals)) if arrivals else None,
            "arrival_end": utc(max(arrivals)) if arrivals else None},
        "metrics": summarize(outcomes, config.thresholds_km), "partitions": partitions,
        "exclusion_counts": dict(sorted(Counter(row["reason"] for row in exclusions).items())),
        "exclusions": sorted(exclusions, key=lambda row: row["input_index"]),
        "accepted_records": accepted, "outcomes": outcomes,
        "calibration": {"status": "INCOMPLETE — no empirical calibration or independent labels",
            "threshold_selection": "predeclared configuration; no fitted threshold or held-out optimization",
            "validated_probability": None},
        "limitations": [evidence.LIMITATIONS,
            "Synthetic records test software behavior only; source/license declarations are operator assertions.",
            "Candidate rates describe unique comparable pairs, not precision, recall or false-positive rates.",
            "Warm-start history from earlier partitions is allowed; held-out updates never enter earlier decisions.",
            "No automatic threshold fitting. A separate frozen plan and independent real evidence are required for calibration.",
            "Offline replay retains all history; it does not simulate DS-M1 bounded retention or provider scheduling.",
            "Invalid schema records are excluded before replay and cannot be assigned a reliable event time/object.",
            "Reports contain record identifiers, hashes and timestamps: review data rights before sharing."],
        "action_authorized": False,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path, help="Local JSON only; never downloaded")
    parser.add_argument("--config", type=Path, required=True, help="Frozen split/threshold JSON plan")
    args = parser.parse_args(argv)
    try:
        dataset = Dataset.model_validate_json(args.dataset.read_bytes())
        config = ReplayConfig.model_validate_json(args.config.read_bytes())
        result = replay(dataset, config)
    except (OSError, ValueError, TypeError, OverflowError):
        print("Replay rejected: unreadable or invalid local dataset/configuration.", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
