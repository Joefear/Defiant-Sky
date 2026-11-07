from datetime import timedelta
import json

import pytest
from sgp4.io import fix_checksum

from server.evidence import Settings, parse_element_set
from server.history import HistoryStore


def test_persistence_dedup_and_original_ingestion(tmp_path, records, now):
    path = tmp_path / "history.sqlite3"
    store = HistoryStore(path, Settings())
    store.add(records, now)
    store.add([r.model_copy(update={"ingested_at": now}) for r in records], now)
    restored, rejected = HistoryStore(path, Settings()).read()
    assert restored == records
    assert rejected == 0


def test_bounded_retention(tmp_path, records, now):
    store = HistoryStore(tmp_path / "history.sqlite3", Settings(sets_per_object=2, max_objects=1))
    newer = records[-1].model_dump()
    newer["tle1"] = fix_checksum(newer["tle1"][:20] + "310.01000000" + newer["tle1"][32:])
    newer.pop("epoch")
    third = parse_element_set(newer, now)
    store.add([*records, third], now)
    assert store.read()[0] == [records[-1], third]
    other = third.model_dump()
    other.pop("catalog_id")
    other["tle1"] = fix_checksum(other["tle1"].replace("99999", "12345"))
    other["tle2"] = fix_checksum(other["tle2"].replace("99999", "12345"))
    store.add([parse_element_set(other, now)], now)
    assert len({r.catalog_id for r in store.read()[0]}) == 1
    store.add([], now + timedelta(days=31))
    assert store.read() == ([], 0)


def test_demo_cannot_enter_real_store(tmp_path, records, now):
    store = HistoryStore(tmp_path / "history.sqlite3", Settings())
    demo = parse_element_set(records[0].model_dump(), now, "demo")
    with pytest.raises(ValueError): store.add([demo], now)
    assert store.read() == ([], 0)


@pytest.mark.parametrize("tamper", ["source", "missing_provenance", "elements", "digest", "bad_json"])
def test_corrupt_or_unprovenanced_cache_rejected(tmp_path, records, now, tamper):
    store = HistoryStore(tmp_path / "history.sqlite3", Settings())
    store.add(records[:1], now)
    raw = records[0].model_dump(mode="json")
    if tamper == "source": raw["provenance"]["source"] = "unknown"
    if tamper == "missing_provenance": raw.pop("provenance")
    if tamper == "elements": raw["elements"]["inclination_deg"] = 1
    if tamper == "digest": raw["provenance"]["content_sha256"] = "forged"
    with store.connect() as db:
        db.execute("UPDATE elements SET payload=?", ("{" if tamper == "bad_json" else json.dumps(raw),))
    assert store.read() == ([], 1)
