"""All provider requests below use MockTransport and synthetic public-shaped GP data."""
import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sgp4.io import fix_checksum

from server.evidence import Settings, parse_element_set
from server.history import HistoryStore
from server.polling import cohort_ids
from server.tle_ingestor import TLEIngestor


def gp(record):
    return {"OBJECT_NAME": record.name, "NORAD_CAT_ID": str(record.catalog_id),
            "TLE_LINE1": record.tle1, "TLE_LINE2": record.tle2,
            "EPOCH": record.epoch.replace(tzinfo=None).isoformat(timespec="microseconds")}


def service(tmp_path, now, **kwargs):
    return TLEIngestor(history_path=tmp_path / "history.sqlite3", clock=lambda: now,
                       catalog_ids=[99999], minute_factory=lambda: 17, **kwargs)


def test_disabled_by_default_even_when_refresh_called(tmp_path, now):
    s = service(tmp_path, now)
    s._fetch_latest_leo_tles = AsyncMock()
    asyncio.run(s.start())
    asyncio.run(s.refresh())
    assert not s.live_enabled and s._task is None
    s._fetch_latest_leo_tles.assert_not_awaited()


@pytest.mark.parametrize("interval", [0, 600, 3599])
def test_minimum_provider_interval(tmp_path, now, interval):
    with pytest.raises(ValueError): service(tmp_path, now, refresh_seconds=interval)


@pytest.mark.parametrize("value", ["0", "100000", "25544/format/json", "NaN", "1.5", list(range(1, 52))])
def test_bounded_cohort_validation(value):
    with pytest.raises(ValueError): cohort_ids(value)


def test_stable_cohort_order_and_explicit_configuration(tmp_path, now):
    assert cohort_ids("25544,12345,25544") == (12345, 25544)
    with pytest.raises(ValueError): TLEIngestor(live_enabled=True)
    with pytest.raises(ValueError): TLEIngestor(catalog_ids=[1, 2], settings=Settings(max_objects=1))


def test_persisted_cooldown_survives_failure_restart_and_cohort_change(tmp_path, now):
    async def scenario():
        s = service(tmp_path, now, live_enabled=True)
        s._fetch_latest_leo_tles = AsyncMock(side_effect=RuntimeError("remote error"))
        await s.start()
        first_due = s.next_poll_at
        assert first_due.minute == 17
        s._fetch_latest_leo_tles.assert_not_awaited()
        s.clock = lambda: first_due
        await s.refresh()
        next_due = s.next_poll_at
        assert (next_due - first_due).total_seconds() >= 3600
        await s.refresh()
        s._fetch_latest_leo_tles.assert_awaited_once()
        await s.stop()
        restarted = TLEIngestor(history_path=s.history_path, live_enabled=True,
            catalog_ids=[12345], clock=lambda: first_due + timedelta(minutes=1))
        restarted._fetch_latest_leo_tles = AsyncMock()
        await restarted.start()
        assert restarted.next_poll_at == next_due
        restarted._fetch_latest_leo_tles.assert_not_awaited()
        await restarted.stop()
    asyncio.run(scenario())


def test_backward_clock_and_longer_interval_do_not_bypass_lease(tmp_path, now):
    store = HistoryStore(tmp_path / "history.sqlite3", Settings())
    _, due = store.reserve_poll(now, 3600, lambda: 17)
    reserved, later = store.reserve_poll(due, 3600, lambda: 17)
    assert reserved
    assert not store.reserve_poll(due - timedelta(days=1), 3600, lambda: 17)[0]
    assert not store.reserve_poll(later, 7200, lambda: 17)[0]


@pytest.mark.parametrize("minute", [0, 30])
def test_delayed_wakeup_avoids_busy_minutes(tmp_path, now, minute):
    store = HistoryStore(tmp_path / "history.sqlite3", Settings())
    store.reserve_poll(now, 3600, lambda: 17)
    delayed = now.replace(minute=minute) + timedelta(hours=2)
    reserved, due = store.reserve_poll(delayed, 3600, lambda: 17)
    assert not reserved and due.minute == 17 and due > delayed


def test_shared_database_allows_only_one_reservation(tmp_path, now):
    a = HistoryStore(tmp_path / "history.sqlite3", Settings())
    b = HistoryStore(a.path, Settings())
    _, due = a.reserve_poll(now, 3600, lambda: 17)
    assert a.reserve_poll(due, 3600, lambda: 17)[0]
    assert not b.reserve_poll(due, 3600, lambda: 17)[0]


def test_network_budget_is_included_in_persisted_cooldown(tmp_path, now):
    store = HistoryStore(tmp_path / "history.sqlite3", Settings())
    _, due = store.reserve_poll(now, 3600, lambda: 17)
    reserved, next_due = store.reserve_poll(due, 3600, lambda: 17)
    assert reserved and (next_due - due).total_seconds() >= 3660
    restarted = HistoryStore(store.path, Settings())
    assert not restarted.reserve_poll(due + timedelta(seconds=3659), 3600, lambda: 17)[0]


@pytest.mark.parametrize("failure", ["corrupt_state", "write_failure"])
def test_reservation_failure_prevents_network(tmp_path, now, monkeypatch, failure):
    async def scenario():
        s = service(tmp_path, now, live_enabled=True)
        await s.start()
        s._fetch_latest_leo_tles = AsyncMock()
        due = s.next_poll_at
        s.clock = lambda: due
        if failure == "corrupt_state":
            with s.store.connect() as db:
                db.execute("UPDATE provider_state SET next_poll='invalid'")
        else:
            def fail(*args): raise OSError("cannot persist")
            monkeypatch.setattr(s.store, "reserve_poll", fail)
        await s.refresh()
        s._fetch_latest_leo_tles.assert_not_awaited()
        assert s.scheduling_error and not s.storage_error
        assert s.refresh_status.startswith("SCHEDULING UNAVAILABLE")
        await s.stop()
    asyncio.run(scenario())


def test_readers_and_duplicate_refreshes_while_fetch_pending(tmp_path, now, records):
    async def scenario():
        s = service(tmp_path, now, live_enabled=True)
        await s.start()
        started, release = asyncio.Event(), asyncio.Event()
        async def pending_fetch():
            started.set()
            await release.wait()
            return [gp(records[-1])]
        s._fetch_latest_leo_tles = AsyncMock(side_effect=pending_fetch)
        due = s.next_poll_at
        s.clock = lambda: due
        first = asyncio.create_task(s.refresh())
        await started.wait()
        second = asyncio.create_task(s.refresh())
        assert (await asyncio.wait_for(s.findings(), timeout=1))["status"] == "INSUFFICIENT DATA"
        assert await asyncio.wait_for(s.get_tle_data(), timeout=1) == []
        release.set()
        await asyncio.gather(first, second)
        s._fetch_latest_leo_tles.assert_awaited_once()
        await s.stop()
    asyncio.run(scenario())


def test_refresh_loop_scheduling_and_cancellation(tmp_path, now, monkeypatch):
    async def scenario():
        s = service(tmp_path, now, live_enabled=True)
        s._fetch_latest_leo_tles = AsyncMock(return_value=[])
        started = asyncio.Event()
        original_sleep = asyncio.sleep
        calls = []
        async def sleep(delay):
            calls.append(delay)
            started.set()
            await original_sleep(1000)
        monkeypatch.setattr("server.tle_ingestor.asyncio.sleep", sleep)
        await s.start()
        await started.wait()
        assert calls == [(s.next_poll_at - now).total_seconds()]
        await s.stop()
        assert s._task is None
    asyncio.run(scenario())


def test_realistic_fractional_gp_epoch_and_tle_agreement(tmp_path, now, records):
    raw = gp(records[0])
    raw["TLE_LINE1"] = fix_checksum(raw["TLE_LINE1"][:20] + "309.50000001" + raw["TLE_LINE1"][32:])
    raw["EPOCH"] = "2026-11-05T12:00:00.001000"  # TLE fractional day is 0.000864 s.
    s = service(tmp_path, now, live_enabled=False)
    asyncio.run(s._accept([raw]))
    assert len(s.records) == 1 and s.records[0].epoch.microsecond == 864
    raw["EPOCH"] = "2026-11-05T12:00:00.003000"
    asyncio.run(s._accept([raw]))
    assert 99999 in s.rejected_objects


def test_repeated_cohort_fetches_deduplicate_and_create_history(tmp_path, now, records, monkeypatch):
    monkeypatch.setenv("SPACETRACK_USER", "offline-placeholder")
    monkeypatch.setenv("SPACETRACK_PASS", "offline-placeholder")
    async def scenario():
        batches = [[gp(records[0])], [gp(records[0])], [gp(records[1])]]
        requests = []
        def handle(request):
            requests.append((request.method, request.url.path))
            if request.url.path == "/ajaxauth/login": return httpx.Response(200)
            return httpx.Response(200, json=batches.pop(0))
        s = service(tmp_path, now, live_enabled=True, transport=httpx.MockTransport(handle))
        await s.start()
        assert requests == []
        for expected in (1, 1, 2):
            due = s.next_poll_at
            s.clock = lambda: due
            await s.refresh()
            assert len(s.records) == expected
        assert (await s.findings())["findings"][0].separation_km is not None
        queries = [path for method, path in requests if method == "GET"]
        assert len(queries) == 3 and len(set(queries)) == 1
        assert "/NORAD_CAT_ID/99999/" in queries[0] and "/EPOCH/>now-10/" in queries[0]
        assert "gp_history" not in queries[0] and "/limit/1/" in queries[0]
        await s.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["unauthorized", "non_json", "not_list", "empty", "malformed_record", "timeout"])
def test_provider_failure_keeps_cooldown_and_no_invented_alert(tmp_path, now, monkeypatch, kind, capsys):
    monkeypatch.setenv("SPACETRACK_USER", "offline-placeholder")
    monkeypatch.setenv("SPACETRACK_PASS", "offline-placeholder")
    calls = []
    def handle(request):
        calls.append(request.url.path)
        if request.url.path == "/ajaxauth/login": return httpx.Response(200)
        if kind == "unauthorized": return httpx.Response(401)
        if kind == "non_json": return httpx.Response(200, text="not-json")
        if kind == "timeout": raise httpx.ReadTimeout("private provider response", request=request)
        return httpx.Response(200, json={"not": "a list"} if kind == "not_list" else [] if kind == "empty" else [{}])
    async def scenario():
        s = service(tmp_path, now, live_enabled=True, transport=httpx.MockTransport(handle))
        await s.start()
        due = s.next_poll_at
        s.clock = lambda: due
        await s.refresh()
        await s.refresh()
        report = await s.findings()
        assert report["status"] == "INSUFFICIENT DATA" and not report["action_authorized"]
        assert len(calls) == 2
        assert "private provider response" not in str(report)
        await s.stop()
    asyncio.run(scenario())
    assert "offline-placeholder" not in capsys.readouterr().out


def test_missing_credentials_never_sends_request(tmp_path, now):
    def deny(request): raise AssertionError("No request permitted")
    s = service(tmp_path, now, transport=httpx.MockTransport(deny))
    with pytest.raises(RuntimeError): asyncio.run(s._fetch_latest_leo_tles())


def test_rejection_is_per_object_durable_and_requires_newer_set(tmp_path, now, records):
    other = gp(records[0])
    other["NORAD_CAT_ID"] = "12345"
    other["TLE_LINE1"] = fix_checksum(other["TLE_LINE1"].replace("99999", "12345"))
    other["TLE_LINE2"] = fix_checksum(other["TLE_LINE2"].replace("99999", "12345"))
    async def scenario():
        s = TLEIngestor(history_path=tmp_path / "history.sqlite3", catalog_ids=[99999, 12345], clock=lambda: now)
        await s.start()
        await s._accept([gp(r) for r in records] + [other])
        bad = gp(records[-1]); bad["TLE_LINE1"] = "invalid"
        await s._accept([bad, other])
        assert [r["catalog_id"] for r in await s.get_tle_data()] == [12345]
        await s._accept([other])
        assert 99999 in s.rejected_objects
        restarted = TLEIngestor(history_path=s.history_path, catalog_ids=[99999, 12345], clock=lambda: now)
        await restarted.start()
        assert 99999 in restarted.rejected_objects
        await restarted._accept([gp(records[-1])])
        assert 99999 in restarted.rejected_objects  # Duplicate cannot clear a rejection.
        newer = gp(records[-1])
        newer["TLE_LINE1"] = fix_checksum(newer["TLE_LINE1"][:20] + "310.01000000" + newer["TLE_LINE1"][32:])
        newer["EPOCH"] = "2026-11-06T00:14:24.000000"
        await restarted._accept([newer])
        assert not restarted.rejected_objects
        assert len(restarted.records) == 4
    asyncio.run(scenario())


def test_duplicate_and_stale_batch_are_not_extra_evidence(tmp_path, now, records):
    s = service(tmp_path, now, live_enabled=False)
    asyncio.run(s._accept([gp(records[0]), gp(records[0])]))
    assert len(s.records) == 1
    assert asyncio.run(s.findings())["status"] == "INSUFFICIENT DATA"
    s.clock = lambda: now + timedelta(days=4)
    asyncio.run(s._accept([gp(r) for r in records]))
    assert asyncio.run(s.findings())["status"] == "INSUFFICIENT DATA"
    assert asyncio.run(s.get_tle_data()) == []
