import asyncio
from datetime import timedelta
import sqlite3
from unittest.mock import AsyncMock

import pytest

from server.tle_ingestor import TLEIngestor


def gp(records):
    return [{"OBJECT_NAME": r.name, "NORAD_CAT_ID": r.catalog_id, "EPOCH": r.epoch.isoformat(),
             "TLE_LINE1": r.tle1, "TLE_LINE2": r.tle2} for r in records]


def service(path, now, ids=(99999,)):
    return TLEIngestor(history_path=path, catalog_ids=ids, clock=lambda: now, minute_factory=lambda: 17)


def failure(*args, **kwargs):
    raise sqlite3.OperationalError("database is locked")


def test_transient_scheduler_failure_keeps_valid_history_and_cooldown(tmp_path, now, records):
    async def scenario():
        s = service(tmp_path / "history.sqlite3", now)
        await s.start()
        await s._accept(gp(records))
        s.live_enabled = True
        s._fetch_latest_leo_tles = AsyncMock(return_value=gp(records))
        await s.refresh()
        due = s.next_poll_at
        reserve = s.store.reserve_poll
        s.store.reserve_poll = failure
        await s.refresh()
        report = await s.findings()
        assert report["scheduling_error"] and not report["storage_error"]
        assert report["data_state"] == "HISTORICAL COMPARISON"
        assert report["findings"][0].separation_km is not None
        assert not report["action_authorized"] and not report["findings"][0].action_authorized
        assert len(await s.get_tle_data()) == 1
        assert s.next_poll_at == due and s.scheduler_retry_at == now + timedelta(seconds=60)
        s.store.reserve_poll = reserve
        await s.refresh()  # Recovery cannot bypass backoff.
        assert s.scheduling_error
        s.clock = lambda: now + timedelta(seconds=60)
        await s.refresh()
        assert not s.scheduling_error and s.next_poll_at == due
        s._fetch_latest_leo_tles.assert_not_awaited()
        restarted = service(s.history_path, now + timedelta(seconds=61))
        restarted.live_enabled = True
        restarted._fetch_latest_leo_tles = AsyncMock()
        await restarted.start()
        assert restarted.next_poll_at == due
        assert (await restarted.findings())["data_state"] == "HISTORICAL COMPARISON"
        restarted._fetch_latest_leo_tles.assert_not_awaited()
        await restarted.stop()
        s.clock = lambda: due
        await s.refresh()
        s._fetch_latest_leo_tles.assert_awaited_once()
    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", ["corrupt", "unreadable", "stale", "quarantined", "unpersisted"])
def test_scheduler_error_never_revives_invalid_evidence(tmp_path, now, records, invalid, monkeypatch):
    async def scenario():
        s = service(tmp_path / "history.sqlite3", now)
        await s.start()
        await s._accept(gp(records))
        if invalid == "corrupt":
            with s.store.connect() as db: db.execute("UPDATE elements SET payload='{}'")
        if invalid == "unreadable": monkeypatch.setattr(s.store, "read", failure)
        if invalid == "stale": s.clock = lambda: now + timedelta(days=4)
        if invalid == "quarantined":
            bad = gp(records)[-1]; bad["TLE_LINE1"] = "invalid"
            await s._accept([bad])
        if invalid == "unpersisted":
            monkeypatch.setattr(s.store, "add", failure)
            await s._accept(gp(records))
        s.live_enabled = True
        s._fetch_latest_leo_tles = AsyncMock()
        monkeypatch.setattr(s.store, "reserve_poll", failure)
        await s.refresh()
        report = await s.findings()
        assert report["scheduling_error"] and report["data_state"] == "UNAVAILABLE"
        assert report["status"] == "INSUFFICIENT DATA" and not report["action_authorized"]
        assert all(not f.action_authorized for f in report["findings"])
        assert await s.get_tle_data() == []
        s._fetch_latest_leo_tles.assert_not_awaited()
    asyncio.run(scenario())


def test_corrupt_persisted_schedule_retries_with_backoff_and_preserves_history(tmp_path, now, records, monkeypatch):
    async def scenario():
        s = service(tmp_path / "history.sqlite3", now)
        await s.start()
        await s._accept(gp(records))
        s.live_enabled = True
        await s.refresh()
        due = s.next_poll_at
        with s.store.connect() as db: db.execute("UPDATE provider_state SET next_poll='invalid'")
        s.clock = lambda: due
        s._fetch_latest_leo_tles = AsyncMock()
        await s.refresh()
        delays = []
        async def stop_sleep(delay):
            delays.append(delay)
            raise asyncio.CancelledError
        monkeypatch.setattr("server.tle_ingestor.asyncio.sleep", stop_sleep)
        with pytest.raises(asyncio.CancelledError): await s._refresh_loop()
        assert delays == [60]
        restarted = service(s.history_path, due)
        restarted.live_enabled = True
        restarted._fetch_latest_leo_tles = AsyncMock()
        await restarted.start()
        assert (await restarted.findings())["data_state"] == "HISTORICAL COMPARISON"
        assert restarted.scheduling_error
        restarted._fetch_latest_leo_tles.assert_not_awaited()
        await restarted.stop()
        s._fetch_latest_leo_tles.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [[], {"error": "offline fixture"}])
def test_unidentified_batch_with_history_remains_conservatively_blocked(tmp_path, now, records, payload):
    async def scenario():
        s = service(tmp_path / "history.sqlite3", now)
        await s.start()
        await s._accept(gp(records))
        await s._accept(payload)
        restarted = service(s.history_path, now)
        await restarted.start()
        report = await restarted.findings()
        assert len(restarted.records) == 2
        assert report["unidentified_rejection"] and report["data_state"] == "UNAVAILABLE"
        assert not report["action_authorized"]
    asyncio.run(scenario())


def test_old_quarantine_hidden_after_cohort_change(tmp_path, now, records):
    async def scenario():
        s = service(tmp_path / "history.sqlite3", now)
        await s.start()
        bad = gp(records)[0]; bad["TLE_LINE1"] = "bad"
        await s._accept([bad])
        changed = service(s.history_path, now, ids=(12345,))
        await changed.start()
        assert (await changed.findings())["quarantined_catalog_ids"] == []
        assert 99999 in changed.store.ingestion_health()[0]  # Read did not discard persisted safety state.
    asyncio.run(scenario())


def test_unpersisted_failure_flag_is_process_local(tmp_path, now, records, monkeypatch):
    async def scenario():
        s = service(tmp_path / "history.sqlite3", now)
        await s.start()
        await s._accept(gp(records))
        monkeypatch.setattr(s.store, "add", failure)
        await s._accept(gp(records))
        assert (await s.findings())["data_state"] == "UNAVAILABLE"
        restarted = service(s.history_path, now)
        await restarted.start()
        report = await restarted.findings()
        assert report["data_state"] == "HISTORICAL COMPARISON"
        assert not report["action_authorized"] and all(not f.action_authorized for f in report["findings"])
    asyncio.run(scenario())
