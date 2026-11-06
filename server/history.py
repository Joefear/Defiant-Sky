"""Local, transactional, bounded element-set history. No credentials are stored."""

from datetime import datetime, timedelta
from contextlib import contextmanager
from pathlib import Path
import sqlite3

from .evidence import ElementSet, Settings, parse_element_set


class HistoryStore:
    def __init__(self, path: str | Path, settings: Settings):
        self.path = Path(path)
        self.settings = settings
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS elements (
                digest TEXT PRIMARY KEY, catalog_id INTEGER NOT NULL,
                epoch TEXT NOT NULL, payload TEXT NOT NULL)""")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, records: list[ElementSet], now: datetime) -> None:
        if any(r.provenance.mode != "real" or r.provenance.source != "Space-Track.org" for r in records):
            raise ValueError("Demo data cannot enter real history")
        with self.connect() as db:
            db.executemany("INSERT OR IGNORE INTO elements VALUES (?, ?, ?, ?)", [
                (r.provenance.content_sha256, r.catalog_id, r.epoch.isoformat(), r.model_dump_json()) for r in records
            ])
            cutoff = (now - timedelta(days=self.settings.retention_days)).isoformat()
            db.execute("DELETE FROM elements WHERE epoch < ?", (cutoff,))
            db.execute("""DELETE FROM elements WHERE digest IN (
                SELECT digest FROM (SELECT digest, ROW_NUMBER() OVER (
                    PARTITION BY catalog_id ORDER BY epoch DESC, digest) AS n FROM elements)
                WHERE n > ?)""", (self.settings.sets_per_object,))
            db.execute("""DELETE FROM elements WHERE catalog_id NOT IN (
                SELECT catalog_id FROM elements GROUP BY catalog_id
                ORDER BY MAX(epoch) DESC, catalog_id LIMIT ?)""", (self.settings.max_objects,))

    def read(self) -> tuple[list[ElementSet], int]:
        records, rejected = [], 0
        with self.connect() as db:
            rows = db.execute("SELECT digest, catalog_id, epoch, payload FROM elements ORDER BY epoch, digest").fetchall()
        for digest, catalog_id, epoch, payload in rows:
            try:
                stored = ElementSet.model_validate_json(payload)
                rebuilt = parse_element_set(stored.model_dump(), stored.ingested_at, "real")
                if rebuilt != stored or (digest, catalog_id, epoch) != (stored.provenance.content_sha256, stored.catalog_id, stored.epoch.isoformat()):
                    raise ValueError("Stored provenance mismatch")
                records.append(stored)
            except (ValueError, TypeError, KeyError, AttributeError):
                rejected += 1
        return records, rejected
