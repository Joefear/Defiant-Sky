"""Space-Track GP schedule: persisted reservation before any network I/O."""

from datetime import datetime, timedelta
import random

from .evidence import UTC

MIN_REFRESH_SECONDS = 3600
FETCH_TIMEOUT_SECONDS = 60
MAX_COHORT_SIZE = 50


def catalog_id(value) -> int:
    text = str(value)
    if not text.isascii() or not text.isdecimal() or not 1 <= int(text) <= 99999:
        raise ValueError("DS-M1 requires numeric TLE-compatible catalog IDs from 1 to 99999")
    return int(text)


def cohort_ids(value, limit=MAX_COHORT_SIZE) -> tuple[int, ...]:
    values = value.split(",") if isinstance(value, str) and value.strip() else value or []
    result = tuple(sorted({catalog_id(str(item).strip()) for item in values}))
    if not 1 <= limit <= MAX_COHORT_SIZE or len(result) > limit:
        raise ValueError("Catalog cohort exceeds configured limit (maximum 50)")
    return result


def choose_minute() -> int:
    return random.SystemRandom().choice([m for m in range(1, 60) if m != 30])


def initial_poll(now: datetime, minute: int) -> datetime:
    due = now.replace(minute=minute, second=0, microsecond=0)
    return due if due > now else due + timedelta(hours=1)


def next_poll(now: datetime, interval: int, minute: int) -> datetime:
    due = now + timedelta(seconds=interval)
    # The added network budget can shift the minute between attempts. A delayed
    # wake or non-hourly interval must also avoid the busy minutes 00 and 30.
    if due.minute in {0, 30}:
        due = initial_poll(due, minute)
    return due.astimezone(UTC)
