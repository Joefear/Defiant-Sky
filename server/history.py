"""Local, transactional, bounded element-set history. No credentials are stored."""

from datetime import datetime, timedelta
from contextlib import contextmanager
from pathlib import Path
import sqlite3

from .evidence import ElementSet, Settings, parse_element_set
from .polling import MIN_REFRESH_SECONDS, FETCH_TIMEOUT_SECONDS, initial_poll, next_poll


class HistoryStore:
    def __init__(self, path: str | Path, settings: Settings):
        self.path = Path(path)
        self.settings = settings
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS elements (
                digest TEXT PRIMARY KEY, catalog_id INTEGER NOT NULL,
                epoch TEXT NOT NULL, payload TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS provider_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                minute INTEGER NOT NULL, next_poll TEXT NOT NULL,
                last_attempt TEXT, last_success TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS rejected_objects (
                catalog_id INTEGER PRIMARY KEY, after_epoch TEXT NOT NULL)""")
            db.execute("CREATE TABLE IF NOT EXISTS ingestion_health (singleton INTEGER PRIMARY KEY CHECK(singleton=1), unidentified_rejection INTEGER NOT NULL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, records: list[ElementSet], now: datetime, *, rejected=None, unidentified=False, cohort=()) -> None:
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
            if rejected is not None:
                self._record_ingestion(db, rejected, unidentified, records, now, cohort)

    def reserve_poll(self, now: datetime, interval: int, minute_factory) -> tuple[bool, datetime]:
        if interval < MIN_REFRESH_SECONDS:
            raise ValueError("GP polling requires at least 3600 seconds")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = db.execute("SELECT minute, next_poll, last_attempt FROM provider_state WHERE singleton=1").fetchone()
            if state is None:
                minute = minute_factory()
                if minute not in range(1, 60) or minute == 30:
                    raise ValueError("Invalid polling minute")
                due = initial_poll(now, minute)
                db.execute("INSERT INTO provider_state VALUES (1, ?, ?, NULL, NULL)", (minute, due.isoformat()))
                return False, due
            minute, timestamp, last_attempt = state
            due = datetime.fromisoformat(timestamp)
            if due.tzinfo is None or minute not in range(1, 60) or minute == 30:
                raise ValueError("Invalid persisted polling state")
            if last_attempt:
                previous = datetime.fromisoformat(last_attempt)
                if previous.tzinfo is None:
                    raise ValueError("Invalid persisted attempt")
                due = max(due, next_poll(previous, interval + FETCH_TIMEOUT_SECONDS, minute))
            # A restart, a failed request, or a backwards clock never clears this lease.
            if now < due:
                return False, due
            if now.minute in {0, 30}:
                due = initial_poll(now, minute)
                db.execute("UPDATE provider_state SET next_poll=? WHERE singleton=1", (due.isoformat(),))
                return False, due
            # The GP request can begin late in the bounded login+fetch operation.
            # Reserve its whole budget too, so retries remain >= interval apart
            # even after a crash or a slower previous login.
            due = next_poll(now, interval + FETCH_TIMEOUT_SECONDS, minute)
            db.execute("UPDATE provider_state SET next_poll=?, last_attempt=? WHERE singleton=1", (due.isoformat(), now.isoformat()))
            return True, due

    def _record_ingestion(self, db, rejected: dict[int, datetime], unidentified: bool,
                          accepted: list[ElementSet], now: datetime, cohort: tuple[int, ...]):
        for object_id, epoch in rejected.items():
            db.execute("""INSERT INTO rejected_objects VALUES (?, ?) ON CONFLICT(catalog_id)
                    DO UPDATE SET after_epoch=MAX(after_epoch, excluded.after_epoch)""", (object_id, epoch.isoformat()))
        for record in accepted:
            if record.catalog_id not in rejected:
                db.execute("DELETE FROM rejected_objects WHERE catalog_id=? AND after_epoch < ?",
                               (record.catalog_id, record.epoch.isoformat()))
        if cohort:
            marks = ",".join("?" for _ in cohort)
            db.execute(f"DELETE FROM rejected_objects WHERE catalog_id NOT IN ({marks})", cohort)
        db.execute("INSERT OR REPLACE INTO ingestion_health VALUES (1, ?)", (int(unidentified),))
        if accepted:
            db.execute("UPDATE provider_state SET last_success=? WHERE singleton=1", (now.isoformat(),))

    def ingestion_health(self):
        with self.connect() as db:
            rejected = dict(db.execute("SELECT catalog_id, after_epoch FROM rejected_objects"))
            unknown = db.execute("SELECT unidentified_rejection FROM ingestion_health WHERE singleton=1").fetchone()
        return rejected, bool(unknown and unknown[0])

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
