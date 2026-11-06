import asyncio
from datetime import timedelta
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest

from server.governance import Governance
from server.main import create_app
from server.tle_ingestor import TLEIngestor


def gp(record):
    return {"OBJECT_NAME": record.name, "NORAD_CAT_ID": record.catalog_id,
            "TLE_LINE1": record.tle1, "TLE_LINE2": record.tle2, "EPOCH": record.epoch.isoformat()}


def ingestor(tmp_path, now):
    return TLEIngestor(history_path=tmp_path / "history.sqlite3", live_enabled=False, clock=lambda: now)


def test_offline_startup_and_demo_segregation(tmp_path, now):
    service = ingestor(tmp_path, now)
    with TestClient(create_app(service)) as client:
        snapshot = client.get("/api/findings").json()
        assert snapshot["status"] == "INSUFFICIENT DATA"
        assert snapshot["data_state"] == "UNAVAILABLE"
        demo = client.get("/api/demo").json()
        assert demo["mode"] == "demo" and demo["data_state"] == "SIMULATED DEMO"
        assert demo["findings"][0]["separation_km"] == pytest.approx(223.72912913292396)
        assert client.get("/api/tle").json() == []
        assert client.get("/api/findings").json()["findings"] == []
        result = client.post("/api/guardrail/evaluate", json={"finding_id": demo["findings"][0]["finding_id"]}).json()
        assert result["action"] == "block" and result["trace_id"] is None
        assert client.get("/").status_code == 200
        assert client.get("/api/health").json()["data_state"] == "UNAVAILABLE"


def test_live_fetch_comparison_and_restart(tmp_path, now, records):
    service = ingestor(tmp_path, now)
    with TestClient(create_app(service)) as client:
        service._fetch_latest_leo_tles = AsyncMock(return_value=[gp(records[0])])
        asyncio.run(service.refresh())
        assert client.get("/api/findings").json()["status"] == "INSUFFICIENT DATA"
        service._fetch_latest_leo_tles = AsyncMock(return_value=[gp(records[1])])
        asyncio.run(service.refresh())
        snapshot = client.get("/api/findings").json()
        assert snapshot["data_state"] == "LIVE DATA"
        assert snapshot["findings"][0]["status"] == "CANDIDATE ORBITAL-ELEMENT DIVERGENCE"
        assert client.get("/api/tle").json()[0]["catalog_id"] == 99999
    with TestClient(create_app(ingestor(tmp_path, now))) as client:
        snapshot = client.get("/api/findings").json()
        assert snapshot["data_state"] == "HISTORICAL COMPARISON"
        assert snapshot["last_successful_fetch"] is None
        assert len(snapshot["findings"][0]["evidence"]) == 2


def test_live_failure_keeps_historical_data_without_leaking_credentials(tmp_path, now, records, capsys):
    service = ingestor(tmp_path, now)
    with TestClient(create_app(service)) as client:
        asyncio.run(service._accept([gp(r) for r in records]))
        service._fetch_latest_leo_tles = AsyncMock(side_effect=RuntimeError("secret password"))
        asyncio.run(service.refresh())
        response = client.get("/api/findings")
        assert response.json()["data_state"] == "HISTORICAL COMPARISON"
        assert "secret password" not in response.text + capsys.readouterr().out


@pytest.mark.parametrize("invalid", ["checksum", "missing_id", "missing_epoch", "empty_epoch", "mismatched_id"])
def test_rejected_update_cannot_reuse_previous_candidate(tmp_path, now, records, invalid):
    service = ingestor(tmp_path, now)
    with TestClient(create_app(service)) as client:
        asyncio.run(service._accept([gp(r) for r in records]))
        raw = gp(records[-1])
        if invalid == "checksum": raw["TLE_LINE1"] = raw["TLE_LINE1"][:-1] + "0"
        if invalid == "missing_id": raw.pop("NORAD_CAT_ID")
        if invalid == "missing_epoch": raw.pop("EPOCH")
        if invalid == "empty_epoch": raw["EPOCH"] = ""
        if invalid == "mismatched_id": raw["NORAD_CAT_ID"] = 12345
        service._fetch_latest_leo_tles = AsyncMock(return_value=[raw])
        asyncio.run(service.refresh())
        response = client.get("/api/findings").json()
        assert response["data_state"] == "UNAVAILABLE"
        assert response["findings"][0]["separation_km"] is None


def test_stale_data_is_not_drawn_or_authorized(tmp_path, now, records):
    service = ingestor(tmp_path, now)
    with TestClient(create_app(service)) as client:
        asyncio.run(service._accept([gp(r) for r in records]))
        finding_id = client.get("/api/findings").json()["findings"][0]["finding_id"]
        service.clock = lambda: now + timedelta(days=4)
        assert client.get("/api/tle").json() == []
        assert client.get("/api/findings").json()["status"] == "INSUFFICIENT DATA"
        result = client.post("/api/guardrail/evaluate", json={"finding_id": finding_id}).json()
        assert result["status"] == "INSUFFICIENT EVIDENCE"
        assert not result["action_authorized"]


def test_storage_failure_fails_closed(tmp_path, now, records, monkeypatch):
    service = ingestor(tmp_path, now)
    with TestClient(create_app(service)) as client:
        def fail(*args): raise sqlite3.OperationalError("disk full")
        monkeypatch.setattr(service.store, "add", fail)
        asyncio.run(service._accept([gp(r) for r in records]))
        response = client.get("/api/findings").json()
        assert response["storage_error"] and response["status"] == "INSUFFICIENT DATA"


@pytest.mark.parametrize("behavior", ["missing", "exception", "allow", "require_approval", "incomplete", "unknown"])
def test_governance_boundary(tmp_path, now, records, behavior, monkeypatch):
    governance = Governance()
    if behavior == "missing":
        def unavailable(*args): raise ImportError("absent")
        monkeypatch.setattr("server.governance.importlib.import_module", unavailable)
    else:
        governance.load_attempted = True
        governance.request_type = lambda **kwargs: SimpleNamespace(**kwargs)
        def evaluate(request):
            assert "223.729" in request.text
            assert "not a maneuver classification" in request.text
            if behavior == "exception": raise RuntimeError("private data")
            return SimpleNamespace(action_recommendation=behavior if behavior in {"allow", "require_approval", "unknown"} else "allow",
                                   trace_id=None if behavior == "incomplete" else "external-trace",
                                   decision_id="external-decision", policy_id="external-policy", created_at=now.isoformat())
        governance.evaluate = evaluate
    service = ingestor(tmp_path, now)
    with TestClient(create_app(service, governance)) as client:
        asyncio.run(service._accept([gp(r) for r in records]))
        finding_id = client.get("/api/findings").json()["findings"][0]["finding_id"]
        response = client.post("/api/guardrail/evaluate", json={"finding_id": finding_id}).json()
        assert not response["action_authorized"]
        if behavior in {"allow", "require_approval"}:
            assert response["action"] == behavior and response["trace_id"] == "external-trace"
        else:
            assert response["status"] == "UNAVAILABLE" and response["trace_id"] is None
            assert response["action"] == "block"
        assert client.post("/api/guardrail/evaluate", json={"classification": "maneuver", "confidence": 99}).status_code == 422
