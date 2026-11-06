import asyncio
from datetime import datetime
import os
from pathlib import Path
import sqlite3

import httpx
from dotenv import load_dotenv

from .evidence import UTC, Settings, detect, evidence_view, parse_element_set
from .history import HistoryStore

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


class TLEIngestor:
    def __init__(self, history_path=None, refresh_seconds=600, object_limit=50,
                 settings=None, live_enabled=None, clock=None):
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
        self.refresh_seconds = refresh_seconds
        self.object_limit = object_limit
        self.live_enabled = live_enabled if live_enabled is not None else os.getenv("DS_LIVE_ENABLED", "1") == "1"
        self.clock = clock or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()
        self._task = None
        self.records = []
        self.last_success = None
        self.refresh_status = "NOT STARTED"
        self.rejected_records = 0
        self.storage_error = False

    async def start(self):
        try:
            self.store = HistoryStore(self.history_path, self.settings)
            self.store.add([], self.clock())
            self.records, self.rejected_records = self.store.read()
        except (OSError, sqlite3.Error):
            self.storage_error = True
        if self.live_enabled:
            await self.refresh()
            self._task = asyncio.create_task(self._refresh_loop())
        else:
            self.refresh_status = "OFFLINE"

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _refresh_loop(self):
        while True:
            await asyncio.sleep(self.refresh_seconds)
            await self.refresh()

    async def refresh(self):
        async with self._lock:
            try:
                raw_records = await self._fetch_latest_leo_tles()
                await self._accept(raw_records)
            except (httpx.HTTPError, ValueError, RuntimeError, TypeError):
                # Never log request bodies, credential values, or remote error text.
                self.refresh_status = "UNAVAILABLE: live fetch failed or credentials missing"

    async def _accept(self, raw_records):
        if not isinstance(raw_records, list) or not raw_records:
            raise ValueError("No GP records")
        now = self.clock()
        accepted, rejected = [], 0
        for raw in raw_records[:self.object_limit]:
            try:
                if not raw["NORAD_CAT_ID"] or not raw["EPOCH"]:
                    raise ValueError("Missing source provenance")
                record = parse_element_set({
                    "name": raw["OBJECT_NAME"], "tle1": raw["TLE_LINE1"], "tle2": raw["TLE_LINE2"],
                    "catalog_id": raw["NORAD_CAT_ID"], "epoch": raw["EPOCH"],
                }, now)
                if record.epoch > now:
                    raise ValueError("Future epoch")
                accepted.append(record)
            except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
                rejected += 1
        self.rejected_records = rejected
        if not accepted:
            raise ValueError("No valid GP records")
        try:
            if self.store is None:
                self.store = HistoryStore(self.history_path, self.settings)
            self.store.add(accepted, now)
            records, corrupt = self.store.read()
            self.records = records
            self.rejected_records += corrupt
            self.storage_error = False
            self.last_success = now
            self.refresh_status = "LIVE DATA" if not self.rejected_records else "DEGRADED: invalid records rejected"
        except (OSError, sqlite3.Error):
            self.storage_error = True
            self.refresh_status = "UNAVAILABLE: history persistence failed"

    async def get_tle_data(self):
        async with self._lock:
            latest = {r.catalog_id: r for r in self.records}
            return [{"name": r.name, "tle1": r.tle1, "tle2": r.tle2, "catalog_id": r.catalog_id}
                    for r in latest.values() if evidence_view(r, self.clock(), self.settings).validity == "VALID"]

    async def findings(self):
        async with self._lock:
            now = self.clock()
            groups = {}
            for record in self.records:
                groups.setdefault(record.catalog_id, []).append(record)
            findings = [detect(records, now, self.settings) for _, records in sorted(groups.items())]
            if self.storage_error or self.rejected_records:
                findings = [f.model_copy(update={"status": "INSUFFICIENT DATA", "severity": "NONE",
                    "quality": "INVALID OR UNPERSISTED INPUT", "reason": "Resolve rejected records or history storage failure.",
                    "separation_km": None, "comparison_epoch": None}) for f in findings]
            fresh = any(evidence_view(r, now, self.settings).validity == "VALID" for r in self.records)
            live = self.last_success and 0 <= (now - self.last_success).total_seconds() <= self.refresh_seconds * 2
            state = "LIVE DATA" if fresh and live and self.refresh_status == "LIVE DATA" else "HISTORICAL COMPARISON" if fresh else "UNAVAILABLE"
            if self.storage_error or self.rejected_records:
                state = "UNAVAILABLE"
            return {
                "mode": "real", "data_state": state, "evaluated_at": now,
                "status": "EVIDENCE AVAILABLE" if any(f.separation_km is not None for f in findings) else "INSUFFICIENT DATA",
                "refresh_status": self.refresh_status, "last_successful_fetch": self.last_success,
                "last_ingested_at": max((r.ingested_at for r in self.records), default=None),
                "rejected_records": self.rejected_records, "storage_error": self.storage_error,
                "settings": self.settings, "findings": findings, "action_authorized": False,
            }

    async def _fetch_latest_leo_tles(self):
        username, password = os.getenv("SPACETRACK_USER"), os.getenv("SPACETRACK_PASS")
        if not username or not password:
            raise RuntimeError("Credentials unavailable")
        async with httpx.AsyncClient(base_url="https://www.space-track.org", timeout=30.0, follow_redirects=False) as client:
            response = await client.post("/ajaxauth/login", data={"identity": username, "password": password})
            response.raise_for_status()
            response = await client.get(
                "/basicspacedata/query/class/gp/DECAY_DATE/null-val/MEAN_MOTION/11.25--16.0/"
                f"orderby/EPOCH desc/limit/{self.object_limit}/format/json")
            response.raise_for_status()
            return response.json()
