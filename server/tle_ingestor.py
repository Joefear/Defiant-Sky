import asyncio
from datetime import datetime, timedelta
import os
from pathlib import Path
import sqlite3

import httpx
from dotenv import load_dotenv

from .evidence import UTC, Settings, detect, evidence_view, parse_element_set
from .history import HistoryStore
from .polling import MIN_REFRESH_SECONDS, FETCH_TIMEOUT_SECONDS, catalog_id, cohort_ids, choose_minute

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


class TLEIngestor:
    def __init__(self, history_path=None, refresh_seconds=None, object_limit=50,
                 settings=None, live_enabled=None, clock=None, catalog_ids=None,
                 transport=None, minute_factory=choose_minute):
        self.settings = settings or Settings(
            threshold_km=os.getenv("DS_THRESHOLD_KM", "10"),
            max_age_hours=os.getenv("DS_MAX_AGE_HOURS", "72"),
            max_gap_hours=os.getenv("DS_MAX_GAP_HOURS", "72"),
            retention_days=os.getenv("DS_RETENTION_DAYS", "30"),
            sets_per_object=os.getenv("DS_SETS_PER_OBJECT", "32"),
            max_objects=os.getenv("DS_MAX_OBJECTS", "500"),
        )
        self.history_path = history_path or os.getenv("DS_HISTORY_PATH", str(ROOT / "data" / "element_history.sqlite3"))
        self.store = None
        self.refresh_seconds = int(refresh_seconds if refresh_seconds is not None else os.getenv("DS_REFRESH_SECONDS", "3600"))
        if self.refresh_seconds < MIN_REFRESH_SECONDS:
            raise ValueError("Space-Track GP polling requires at least 3600 seconds")
        self.object_limit = object_limit
        self.catalog_ids = cohort_ids(catalog_ids if catalog_ids is not None else os.getenv("DS_CATALOG_IDS", ""), object_limit)
        if len(self.catalog_ids) > self.settings.max_objects:
            raise ValueError("History capacity must cover the configured cohort")
        configured_live = os.getenv("DS_LIVE_ENABLED", "0")
        if configured_live not in {"0", "1"}:
            raise ValueError("DS_LIVE_ENABLED must be 0 or 1")
        self.live_enabled = live_enabled if live_enabled is not None else configured_live == "1"
        if self.live_enabled and not self.catalog_ids:
            raise ValueError("Live ingestion requires an explicit DS_CATALOG_IDS cohort")
        self.transport = transport
        self.minute_factory = minute_factory
        self.clock = clock or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()
        self._task = None
        self.records = []
        self.last_success = None
        self.next_poll_at = None
        self.refresh_status = "NOT STARTED"
        self.rejected_records = 0
        self.rejected_objects = {}
        self.unidentified_rejection = False
        self.storage_error = False
        self.unpersisted_input = False
        self.scheduling_error = False
        self.scheduler_retry_at = None

    def _read_history(self):
        records, self.rejected_records = self.store.read()
        self.records = [r for r in records if not self.catalog_ids or r.catalog_id in self.catalog_ids]
        self.rejected_objects, self.unidentified_rejection = self.store.ingestion_health()
        if self.catalog_ids:
            self.rejected_objects = {key: value for key, value in self.rejected_objects.items() if key in self.catalog_ids}
        self.unidentified_rejection = self.unidentified_rejection or bool(self.rejected_records)

    def _verify_history(self):
        try:
            self._read_history()
            self.storage_error = self.unpersisted_input
        except (OSError, sqlite3.Error, ValueError, TypeError):
            self.storage_error = True

    async def start(self):
        try:
            self.store = HistoryStore(self.history_path, self.settings)
            self.store.add([], self.clock())
            self._read_history()
        except (OSError, sqlite3.Error, ValueError):
            self.storage_error = True
        if self.live_enabled:
            await self.refresh()
            self._task = asyncio.create_task(self._refresh_loop())
        else:
            self.refresh_status = "OFFLINE: live ingestion disabled"

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _refresh_loop(self):
        while True:
            due = self.scheduler_retry_at or self.next_poll_at
            delay = max(1, (due - self.clock()).total_seconds()) if due else self.refresh_seconds
            await asyncio.sleep(delay)
            await self.refresh()

    async def refresh(self):
        if not self.live_enabled:
            self.refresh_status = "OFFLINE: live ingestion disabled"
            return
        # Only one fetch per service; SQLite also serializes reservation by processes
        # sharing this history file. Readers do not wait for network requests.
        async with self._refresh_lock:
            if self.scheduler_retry_at and self.clock() < self.scheduler_retry_at:
                return
            try:
                if self.store is None:
                    self.store = HistoryStore(self.history_path, self.settings)
                reserved, self.next_poll_at = self.store.reserve_poll(self.clock(), self.refresh_seconds, self.minute_factory)
                if self.scheduling_error:
                    self._verify_history()
                self.scheduling_error = False
                self.scheduler_retry_at = None
                if not reserved:
                    self.refresh_status = "COOLDOWN: waiting for scheduled GP retrieval"
                    return
            except (OSError, sqlite3.Error, ValueError, TypeError):
                self.scheduling_error = True
                self.scheduler_retry_at = self.clock() + timedelta(seconds=60)
                if self.store is not None:
                    self._verify_history()
                else:
                    self.storage_error = True
                self.refresh_status = "SCHEDULING UNAVAILABLE: provider requests blocked; history checked separately"
                return
            try:
                raw_records = await asyncio.wait_for(self._fetch_latest_leo_tles(), timeout=FETCH_TIMEOUT_SECONDS)
                async with self._lock:
                    await self._accept(raw_records)
            except (httpx.HTTPError, ValueError, RuntimeError, TypeError, TimeoutError):
                self.refresh_status = "UNAVAILABLE: GP retrieval failed; reserved cooldown retained"

    def _rejection_epoch(self, raw, object_id, now):
        known = [r.epoch for r in self.records if r.catalog_id == object_id]
        try:
            epoch = datetime.fromisoformat(raw["EPOCH"].replace("Z", "+00:00"))
            epoch = epoch.replace(tzinfo=UTC) if epoch.tzinfo is None else epoch.astimezone(UTC)
            if epoch <= now:
                known.append(epoch)
        except (KeyError, ValueError, TypeError, AttributeError):
            pass
        return max(known, default=now)

    async def _accept(self, raw_records):
        now = self.clock()
        accepted, rejected = [], {}
        unidentified = not isinstance(raw_records, list) or not raw_records or len(raw_records) > self.object_limit * 2
        invalid_count = int(unidentified)
        for raw in [] if unidentified else raw_records:
            object_id = None
            try:
                object_id = catalog_id(raw["NORAD_CAT_ID"])
                if object_id not in self.catalog_ids:
                    object_id = None
                    raise ValueError("Unexpected object outside cohort")
                if not raw["EPOCH"]:
                    raise ValueError("Missing epoch provenance")
                record = parse_element_set({
                    "name": raw["OBJECT_NAME"], "tle1": raw["TLE_LINE1"], "tle2": raw["TLE_LINE2"],
                    "catalog_id": object_id, "epoch": raw["EPOCH"],
                }, now)
                if record.epoch > now:
                    raise ValueError("Future epoch")
                accepted.append(record)
            except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
                invalid_count += 1
                if object_id is None:
                    unidentified = True
                else:
                    rejected[object_id] = max(rejected.get(object_id, datetime.min.replace(tzinfo=UTC)), self._rejection_epoch(raw, object_id, now))
        try:
            if self.store is None:
                self.store = HistoryStore(self.history_path, self.settings)
            self.store.add(accepted, now, rejected=rejected, unidentified=unidentified, cohort=self.catalog_ids)
            self._read_history()
            self.rejected_records += invalid_count
            self.unpersisted_input = False
            self.storage_error = False
            if accepted:
                self.last_success = now
            self.refresh_status = "DEGRADED: rejected or missing GP records" if invalid_count or self.rejected_objects else "LIVE DATA"
        except (OSError, sqlite3.Error):
            self.unpersisted_input = True
            self.storage_error = True
            self.refresh_status = "UNAVAILABLE: history persistence failed"

    def _usable(self, record, now):
        return (not self.storage_error and not self.unidentified_rejection
                and record.catalog_id not in self.rejected_objects
                and evidence_view(record, now, self.settings).validity == "VALID")

    async def get_tle_data(self):
        async with self._lock:
            latest = {r.catalog_id: r for r in self.records}
            return [{"name": r.name, "tle1": r.tle1, "tle2": r.tle2, "catalog_id": r.catalog_id}
                    for r in latest.values() if self._usable(r, self.clock())]

    async def findings(self):
        async with self._lock:
            now = self.clock()
            groups = {}
            for record in self.records:
                groups.setdefault(record.catalog_id, []).append(record)
            findings = [detect(records, now, self.settings) for _, records in sorted(groups.items())]
            findings = [f.model_copy(update={"status": "INSUFFICIENT DATA", "severity": "NONE",
                "quality": "INVALID OR UNPERSISTED INPUT", "reason": "Rejected evidence requires a newer valid update; history must be available.",
                "separation_km": None, "comparison_epoch": None})
                if self.storage_error or self.unidentified_rejection or f.catalog_id in self.rejected_objects else f for f in findings]
            fresh = any(self._usable(r, now) for r in self.records)
            live = self.last_success and 0 <= (now - self.last_success).total_seconds() <= self.refresh_seconds * 2
            state = "LIVE DATA" if fresh and live and self.refresh_status == "LIVE DATA" else "HISTORICAL COMPARISON" if fresh else "UNAVAILABLE"
            return {
                "mode": "real", "data_state": state, "evaluated_at": now,
                "status": "EVIDENCE AVAILABLE" if any(f.separation_km is not None for f in findings) else "INSUFFICIENT DATA",
                "refresh_status": self.refresh_status, "last_successful_fetch": self.last_success,
                "last_ingested_at": max((r.ingested_at for r in self.records), default=None),
                "rejected_records": self.rejected_records, "quarantined_catalog_ids": sorted(self.rejected_objects),
                "unidentified_rejection": self.unidentified_rejection, "storage_error": self.storage_error,
                "scheduling_error": self.scheduling_error, "scheduler_retry_at": self.scheduler_retry_at,
                "live_enabled": self.live_enabled, "catalog_ids": self.catalog_ids,
                "refresh_seconds": self.refresh_seconds, "next_poll_at": self.next_poll_at,
                "settings": self.settings, "findings": findings, "action_authorized": False,
            }

    async def _fetch_latest_leo_tles(self):
        username, password = os.getenv("SPACETRACK_USER"), os.getenv("SPACETRACK_PASS")
        if not username or not password:
            raise RuntimeError("Credentials unavailable")
        if not self.catalog_ids:
            raise RuntimeError("Cohort unavailable")
        async with httpx.AsyncClient(base_url="https://www.space-track.org", timeout=30.0,
                                     follow_redirects=False, transport=self.transport) as client:
            response = await client.post("/ajaxauth/login", data={"identity": username, "password": password})
            response.raise_for_status()
            ids = ",".join(map(str, self.catalog_ids))
            response = await client.get(
                f"/basicspacedata/query/class/gp/NORAD_CAT_ID/{ids}/DECAY_DATE/null-val/EPOCH/%3Enow-10/"
                f"MEAN_MOTION/11.25--16.0/orderby/NORAD_CAT_ID/limit/{len(self.catalog_ids)}/format/json")
            response.raise_for_status()
            return response.json()
