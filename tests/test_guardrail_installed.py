"""Opt-in tests of a byte-identical disposable Guardrail runtime copy.

Normal CI skips these. Set DS_GUARDRAIL_TEST_COPY only to a copy created by
tools/prepare_guardrail_copy.py; never point it at the owner's installation.
"""
from datetime import datetime, timedelta
import importlib
import json
import os
from pathlib import Path
import sys

import pytest
from sgp4.io import fix_checksum

from server.evidence import UTC, Settings, detect, parse_element_set
from server.governance import Governance


@pytest.fixture
def runtime(monkeypatch):
    configured = os.getenv("DS_GUARDRAIL_TEST_COPY")
    if not configured:
        pytest.skip("Actual installed-engine test requires a disposable runtime copy")
    root = Path(configured).resolve()
    manifest_path = root / "source-manifest.json"
    if not manifest_path.is_file():
        pytest.fail("Disposable-copy manifest required; refusing to import runtime")
    manifest = json.loads(manifest_path.read_text())
    source = Path(manifest["source_root"]).resolve()
    if root == source or root.is_relative_to(source) or source.is_relative_to(root):
        pytest.fail("Test runtime must be separate from the installed repository")
    package = root / "packages/guardrail-core-python"
    monkeypatch.syspath_prepend(str(package))
    evaluator = importlib.import_module("guardrail_core.evaluator")
    audit = importlib.import_module("guardrail_core.audit")
    assert Path(evaluator.__file__).resolve().is_relative_to(root)
    assert evaluator.PROJECT_ROOT == root
    assert audit.AUDIT_LOG_PATH.is_relative_to(root)
    return evaluator, audit


@pytest.fixture
def current_finding(records):
    now = datetime.now(UTC)
    rebuilt = []
    for record, hours in zip(records, (13, 1)):
        epoch = now - timedelta(hours=hours)
        day = (epoch - epoch.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 86400 + 1
        epoch_text = f"{epoch.year % 100:02d}{day:012.8f}"
        raw = record.model_dump()
        raw.pop("epoch")
        raw["tle1"] = fix_checksum(record.tle1[:18] + epoch_text + record.tle1[32:])
        rebuilt.append(parse_element_set(raw, now))
    return detect(rebuilt, now, Settings())


def test_actual_sda_policy_bridge_and_audit(runtime, current_finding):
    evaluator, audit = runtime
    result = Governance().review(current_finding)
    assert result["status"] == "ADVISORY ONLY"
    assert result["action"] == "require_approval"
    assert result["decision_source"] == "external_guardrail"
    assert result["policy_id"] == "sda-ops-v1"
    assert not result["action_authorized"]
    last_line = audit.AUDIT_LOG_PATH.read_text().splitlines()[-1]
    assert result["trace_id"] in last_line and result["decision_id"] in last_line
    assert evaluator.get_bridge_command()[1].endswith("bridge.js")


def test_actual_engine_missing_policy_fails_closed(runtime, current_finding, tmp_path, monkeypatch):
    evaluator, _ = runtime
    monkeypatch.setattr(evaluator, "POLICIES_ROOT", tmp_path)
    result = Governance().review(current_finding)
    assert result["status"] == "UNAVAILABLE" and result["action"] == "block"
    assert result["trace_id"] is None and not result["action_authorized"]


def test_actual_python_engine_bridge_failure_fails_closed(runtime, current_finding, monkeypatch):
    evaluator, _ = runtime
    def missing_bridge(): raise FileNotFoundError("isolated test failure")
    monkeypatch.setattr(evaluator, "get_bridge_command", missing_bridge)
    result = Governance().review(current_finding)
    assert result["status"] == "UNAVAILABLE" and not result["action_authorized"]


@pytest.mark.parametrize("case", ["stale", "simulated", "invalid"])
def test_actual_integration_gates_do_not_invoke_engine(runtime, current_finding, case):
    _, audit = runtime
    gate = Governance()
    if case == "stale": gate.clock = lambda: datetime.now(UTC) + timedelta(days=4)
    if case == "simulated": current_finding = current_finding.model_copy(update={"mode": "demo"})
    if case == "invalid": current_finding = current_finding.model_copy(update={"separation_km": -1})
    before = audit.AUDIT_LOG_PATH.read_bytes() if audit.AUDIT_LOG_PATH.exists() else b""
    result = gate.review(current_finding)
    assert result["status"] == "INSUFFICIENT EVIDENCE" and not result["action_authorized"]
    after = audit.AUDIT_LOG_PATH.read_bytes() if audit.AUDIT_LOG_PATH.exists() else b""
    assert after == before


def test_actual_engine_allow_probe_is_still_advisory(runtime, current_finding):
    evaluator, _ = runtime
    from guardrail_core.models import DecisionRequest
    # The unchanged SDA rule always requires approval for sda_anomaly_response.
    # A separate nonmatching tool probes the engine's allow return contract.
    reply = evaluator.evaluate_request(DecisionRequest(text="Isolated compatibility probe",
        policy_files=["sda-ops-v1.yml"], requested_tool_name="sda_readonly_probe", include_trace=True))
    assert reply.action_recommendation == "allow"
    gate = Governance()
    gate.load_attempted = True
    gate.request_type = DecisionRequest
    gate.evaluate = lambda request: reply  # Response-contract replay, not an SDA allow.
    result = gate.review(current_finding)
    assert result["status"] == "ADVISORY ONLY" and result["action"] == "allow"
    assert not result["action_authorized"]
