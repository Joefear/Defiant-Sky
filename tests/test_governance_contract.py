from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from server.evidence import Settings, detect
from server.governance import Governance


def response(now, **overrides):
    fields = dict(action_recommendation="allow", policy_id="sda-ops-v1",
        trace_id="00e5524b-e7ca-4dcf-814c-ddfc2fb647cc", decision_id="37af7db5-1908-4f98-912f-169bd5f66289",
        created_at=now.isoformat(), findings=[])
    return SimpleNamespace(**(fields | overrides))


def adapter(now, reply):
    gate = Governance(clock=lambda: now)
    gate.load_attempted = True
    gate.request_type = lambda **kwargs: SimpleNamespace(**kwargs)
    gate.evaluate = Mock(return_value=reply)
    return gate


@pytest.mark.parametrize("change", [
    {"trace_id": None}, {"trace_id": " "}, {"trace_id": 123}, {"trace_id": {"id": "forged"}},
    {"trace_id": "00000000-0000-0000-0000-000000000000"}, {"decision_id": "malformed"},
    {"policy_id": "unknown-policy"}, {"policy_id": "other-policy"}, {"created_at": "bad"},
    {"created_at": "2026-11-06T01:00:00"}, {"findings": None},
    {"findings": [{"finding_type": "evaluation_error"}]},
])
def test_malformed_external_provenance_fails_closed(now, records, change):
    gate = adapter(now, response(now, **change))
    result = gate.review(detect(records, now, Settings()))
    assert result["status"] == "UNAVAILABLE" and result["action"] == "block"
    assert result["trace_id"] is None and not result["action_authorized"]
    assert result["decision_source"] == "local_safety_gate"


@pytest.mark.parametrize("offset", [-301, 61])
def test_stale_or_future_decision_time(now, records, offset):
    gate = adapter(now, response(now, created_at=(now + timedelta(seconds=offset)).isoformat()))
    assert gate.review(detect(records, now, Settings()))["status"] == "UNAVAILABLE"


@pytest.mark.parametrize("case", ["stale", "simulated", "invalid_tle", "forged_metric", "missing"])
def test_adapter_revalidates_evidence_without_api_lookup(now, records, case):
    finding = detect(records, now, Settings())
    gate = adapter(now, response(now))
    if case == "stale": gate.clock = lambda: now + timedelta(days=4)
    if case == "simulated": finding = finding.model_copy(update={"mode": "demo"})
    if case == "invalid_tle":
        view = finding.evidence[0].model_copy(update={"record": records[0].model_copy(update={"tle1": "bad"})})
        finding = finding.model_copy(update={"evidence": [view, finding.evidence[1]]})
    if case == "forged_metric": finding = finding.model_copy(update={"separation_km": 9000})
    if case == "missing": finding = None
    result = gate.review(finding)
    assert result["status"] == "INSUFFICIENT EVIDENCE" and not result["action_authorized"]
    gate.evaluate.assert_not_called()
