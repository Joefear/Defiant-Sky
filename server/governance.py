"""Advisory external Guardrail boundary. DS-M1 has no operational action executor."""

import importlib
from datetime import datetime
import os
from uuid import UUID

from .evidence import UTC, Finding, Settings, detect, parse_element_set
from .guardrail_loader import load_configured


class Governance:
    def __init__(self, settings=None, clock=None):
        self.evaluate = None
        self.request_type = None
        self.load_attempted = False
        self.settings = settings or Settings()
        self.clock = clock or (lambda: datetime.now(UTC))

    def load(self):
        if self.load_attempted:
            return
        self.load_attempted = True
        path = os.getenv("GUARDRAIL_PATH")
        try:
            if path is not None:
                if not path.strip():
                    raise ValueError("Empty configured Guardrail path")
                evaluator, models = load_configured(path)
            else:
                evaluator = importlib.import_module("guardrail_core.evaluator")
                models = importlib.import_module("guardrail_core.models")
            self.evaluate = getattr(evaluator, "evaluate", None) or evaluator.evaluate_request
            self.request_type = models.DecisionRequest
        except Exception:
            self.evaluate = self.request_type = None

    def review(self, finding: Finding | None):
        result = {"action": "block", "action_authorized": False, "status": "INSUFFICIENT EVIDENCE", "decision_source": "local_safety_gate",
                  "summary": "Finding is missing, stale, simulated or lacks comparable evidence.",
                  "trace_id": None, "decision_id": None, "policy_id": None, "created_at": None}
        if finding is None or finding.mode != "real" or finding.separation_km is None:
            return result
        # Also fail closed for direct adapter callers, independently of API lookup.
        try:
            records = [view.record for view in finding.evidence]
            for record in records:
                if parse_element_set(record.model_dump(), record.ingested_at, "real") != record:
                    raise ValueError("Invalid evidence provenance")
            current = detect(records, self.clock(), self.settings)
            if current.finding_id != finding.finding_id or current.separation_km is None or current.separation_km != finding.separation_km:
                return result
        except (ValueError, TypeError, KeyError, AttributeError):
            return result
        self.load()
        if self.evaluate is None:
            return {**result, "status": "UNAVAILABLE", "summary": "Guardrail unavailable; operational actions blocked."}
        try:
            request = self.request_type(
                text="Read-only orbital-element finding; not a maneuver classification: " + finding.model_dump_json(),
                policy_pack=None, policy_files=["sda-ops-v1.yml"], mode="action",
                requested_tool_name="sda_anomaly_response", requested_command=None,
                session_summary=None, session_anomaly_flags=None, code_graph_summary=None, include_trace=True,
            )
            response = self.evaluate(request)
            action = response.action_recommendation
            if action not in {"allow", "block", "modify", "require_approval"}:
                raise ValueError("Unknown Guardrail decision")
            ids = {key: getattr(response, key, None) for key in ("trace_id", "decision_id", "policy_id", "created_at")}
            for key in ("trace_id", "decision_id"):
                value = ids[key]
                if not isinstance(value, str) or str(UUID(value)) != value.lower() or UUID(value).int == 0:
                    raise ValueError("Malformed Guardrail identifier")
            if ids["policy_id"] != "sda-ops-v1" or not isinstance(ids["created_at"], str):
                raise ValueError("Missing or unexpected policy provenance")
            timestamp = datetime.fromisoformat(ids["created_at"].replace("Z", "+00:00"))
            if timestamp.tzinfo is None or not -60 <= (self.clock() - timestamp).total_seconds() <= 300:
                raise ValueError("Stale or malformed decision time")
            findings = getattr(response, "findings", None)
            if not isinstance(findings, list) or any(not isinstance(f, dict) or f.get("finding_type") == "evaluation_error" for f in findings):
                raise ValueError("Guardrail evaluation reported integration failure")
            return {**result, **ids, "status": "ADVISORY ONLY", "action": action, "decision_source": "external_guardrail",
                    "summary": "Guardrail recommendation only. DS-M1 does not authorize or execute operational actions."}
        except Exception:
            return {**result, "status": "UNAVAILABLE", "summary": "Guardrail evaluation failed; operational actions blocked."}
