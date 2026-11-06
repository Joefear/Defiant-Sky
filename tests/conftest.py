from datetime import datetime
import json
from pathlib import Path

import pytest

from server.evidence import parse_element_set


@pytest.fixture
def fixture():
    return json.loads((Path(__file__).parents[1] / "server/fixtures/synthetic.json").read_text())


@pytest.fixture
def now(fixture):
    return datetime.fromisoformat(fixture["evaluated_at"])


@pytest.fixture
def records(fixture):
    # A fake Space-Track transport is used only in tests of the real ingestion boundary.
    return [parse_element_set(raw, datetime.fromisoformat(raw["ingested_at"])) for raw in fixture["records"]]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for name in ("DS_THRESHOLD_KM", "DS_MAX_AGE_HOURS", "DS_MAX_GAP_HOURS", "DS_RETENTION_DAYS", "DS_SETS_PER_OBJECT", "DS_MAX_OBJECTS", "DS_CATALOG_IDS", "DS_REFRESH_SECONDS", "DS_HISTORY_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("SPACETRACK_USER", raising=False)
    monkeypatch.delenv("SPACETRACK_PASS", raising=False)
    monkeypatch.setenv("DS_LIVE_ENABLED", "0")
    monkeypatch.delenv("GUARDRAIL_PATH", raising=False)
