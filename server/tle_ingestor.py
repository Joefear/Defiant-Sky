import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv


load_dotenv()


class TLEIngestor:
    def __init__(
        self,
        cache_path: str | Path = "data/tle_cache.json",
        refresh_seconds: int = 600,
        object_limit: int = 50,
    ) -> None:
        self.cache_path = Path(cache_path)
        self.refresh_seconds = refresh_seconds
        self.object_limit = object_limit
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._tle_data: list[dict[str, str]] = []

    async def start(self) -> None:
        await self._load_cache()
        if not self._tle_data:
            await self.refresh()
        self._task = asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def get_tle_data(self) -> list[dict[str, str]]:
        async with self._lock:
            return list(self._tle_data)

    async def refresh(self) -> None:
        try:
            tle_data = await self._fetch_latest_leo_tles()
        except Exception as exc:
            print(f"TLE refresh failed: {exc}")
            return

        async with self._lock:
            self._tle_data = tle_data

        await self._write_cache(tle_data)

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self.refresh_seconds)
            await self.refresh()

    async def _load_cache(self) -> None:
        if not self.cache_path.exists():
            return

        try:
            cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            tles = cache.get("tles", [])
            if isinstance(tles, list):
                async with self._lock:
                    self._tle_data = [
                        item
                        for item in tles
                        if isinstance(item, dict)
                        and isinstance(item.get("name"), str)
                        and isinstance(item.get("tle1"), str)
                        and isinstance(item.get("tle2"), str)
                    ]
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Unable to read TLE cache: {exc}")

    async def _write_cache(self, tle_data: list[dict[str, str]]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "source": "Space-Track.org",
            "tles": tle_data,
        }
        await asyncio.to_thread(
            self.cache_path.write_text,
            json.dumps(payload, indent=2),
            "utf-8",
        )

    async def _fetch_latest_leo_tles(self) -> list[dict[str, str]]:
        username = os.getenv("SPACETRACK_USER")
        password = os.getenv("SPACETRACK_PASS")
        if not username or not password:
            raise RuntimeError(
                "SPACETRACK_USER and SPACETRACK_PASS must be set to fetch live TLE data"
            )

        async with httpx.AsyncClient(
            base_url="https://www.space-track.org",
            follow_redirects=True,
            timeout=30.0,
        ) as client:
            login_response = await client.post(
                "/ajaxauth/login",
                data={"identity": username, "password": password},
            )
            login_response.raise_for_status()

            query = (
                "/basicspacedata/query/class/gp/"
                "DECAY_DATE/null-val/"
                "MEAN_MOTION/11.25--16.0/"
                "orderby/EPOCH desc/"
                f"limit/{self.object_limit}/"
                "format/json"
            )
            response = await client.get(query)
            response.raise_for_status()
            records = response.json()

        return self._records_to_tle(records)

    def _records_to_tle(self, records: Any) -> list[dict[str, str]]:
        if not isinstance(records, list):
            raise ValueError("Unexpected Space-Track response format")

        tles: list[dict[str, str]] = []
        seen_catalog_ids: set[str] = set()

        for record in records:
            if not isinstance(record, dict):
                continue

            catalog_id = str(record.get("NORAD_CAT_ID", "")).strip()
            if catalog_id and catalog_id in seen_catalog_ids:
                continue

            name = str(record.get("OBJECT_NAME") or record.get("OBJECT_ID") or "").strip()
            tle1 = str(record.get("TLE_LINE1") or "").strip()
            tle2 = str(record.get("TLE_LINE2") or "").strip()

            if name and tle1.startswith("1 ") and tle2.startswith("2 "):
                tles.append({"name": name, "tle1": tle1, "tle2": tle2})
                if catalog_id:
                    seen_catalog_ids.add(catalog_id)

            if len(tles) >= self.object_limit:
                break

        if not tles:
            raise ValueError("Space-Track returned no usable TLE records")

        return tles
