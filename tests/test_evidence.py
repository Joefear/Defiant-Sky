from datetime import timedelta

import pytest
from sgp4.io import fix_checksum

from server.evidence import Settings, detect, parse_element_set, separation_km


def test_identical_updates_are_not_history(records, now):
    finding = detect([records[0], records[0]], now, Settings())
    assert finding.status == "INSUFFICIENT DATA"
    assert finding.separation_km is None
    assert separation_km(records[0], records[0]) == 0


def test_known_changed_fixture(records, now):
    finding = detect(records, now, Settings())
    assert finding.status == "CANDIDATE ORBITAL-ELEMENT DIVERGENCE"
    assert finding.separation_km == pytest.approx(223.72912913292396, abs=1e-6)
    assert finding.comparison_epoch == records[1].epoch
    assert finding.evidence[0].record.provenance.source == "Space-Track.org"
    assert not finding.action_authorized
    assert "confidence" not in finding.model_dump()
    assert detect(list(reversed(records)), now, Settings()).finding_id == finding.finding_id


def test_threshold_is_configurable(records, now):
    assert detect(records, now, Settings(threshold_km=300)).status == "WITHIN THRESHOLD"
    metric = separation_km(*records)
    assert detect(records, now, Settings(threshold_km=metric)).status == "WITHIN THRESHOLD"
    with pytest.raises(ValueError):
        Settings(threshold_km=float("nan"))


@pytest.mark.parametrize("offset", [timedelta(days=4), -timedelta(days=2)])
def test_stale_or_future_insufficient(records, now, offset):
    finding = detect(records, now + offset, Settings())
    assert finding.status == "INSUFFICIENT DATA"
    assert finding.separation_km is None


def test_same_epoch_and_long_gap(records, now):
    conflicting = records[0].model_copy(update={"epoch": records[1].epoch})
    assert detect([conflicting, records[1]], now, Settings()).quality == "INCOMPARABLE EPOCHS"
    assert detect(records, now, Settings(max_gap_hours=1)).status == "INSUFFICIENT DATA"


def test_catalog_and_mode_mismatch(records, now):
    changed = records[0].model_copy(update={"catalog_id": 12345})
    assert detect([changed, records[1]], now, Settings()).quality == "INVALID PROVENANCE"
    demo = records[0].model_copy(update={"provenance": records[0].provenance.model_copy(update={"mode": "demo"})})
    assert detect([demo, records[1]], now, Settings()).quality == "INVALID PROVENANCE"


def test_ambiguous_previous_epoch_is_not_cherry_picked(records, now):
    conflicting = records[0].model_copy(update={"provenance": records[0].provenance.model_copy(update={"content_sha256": "different"})})
    assert detect([records[0], conflicting, records[1]], now, Settings()).status == "INSUFFICIENT DATA"


@pytest.mark.parametrize("change", ["checksum", "catalog", "line_catalog", "epoch", "format", "inclination", "epoch_day"])
def test_invalid_inputs_rejected(fixture, now, change):
    raw = fixture["records"][0].copy()
    if change == "checksum": raw["tle1"] = raw["tle1"][:-1] + "0"
    if change == "catalog": raw["catalog_id"] = 12345
    if change == "line_catalog": raw["tle2"] = fix_checksum(raw["tle2"].replace("99999", "12345"))
    if change == "epoch": raw["epoch"] = "2026-10-01T00:00:00Z"
    if change == "format": raw["tle1"] = "1 bad"
    if change == "inclination": raw["tle2"] = fix_checksum(raw["tle2"][:8] + "181.0000" + raw["tle2"][16:])
    if change == "epoch_day": raw["tle1"] = fix_checksum(raw["tle1"][:20] + "367.50000000" + raw["tle1"][32:])
    with pytest.raises(ValueError): parse_element_set(raw, now)


def test_propagation_failure_is_not_candidate(records, now, monkeypatch):
    def fail(*args): raise ValueError("propagation error")
    monkeypatch.setattr("server.evidence.separation_km", fail)
    finding = detect(records, now, Settings())
    assert finding.status == "INSUFFICIENT DATA"
    assert finding.quality == "PROPAGATION FAILED"


def test_sgp4_reference_vector():
    # Independent published python-sgp4 reference example, not the synthetic golden value.
    from sgp4.api import Satrec
    sat = Satrec.twoline2rv(
        "1 25544U 98067A   19343.69339541  .00001764  00000-0  38792-4 0  9991",
        "2 25544  51.6439 211.2001 0007417  17.6667  85.6398 15.50103472202482")
    error, position, _ = sat.sgp4(2458826.5, 0.8625)
    assert error == 0
    assert position == pytest.approx((-6088.92, -936.13, -2866.44), abs=.01)
