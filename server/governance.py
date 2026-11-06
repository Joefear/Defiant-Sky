"""Advisory external Guardrail boundary. DS-M1 has no operational action executor."""

import importlib
import os
from pathlib import Path
import sys

from .evidence import Finding


class Governance:
    def __init__(self):
        self.evaluate = None
        self.request_type = None
        self.load_attempted = False

    def load(self):
        if self.load_attempted:
            return
        self.load_attempted = True
        path = os.getenv("GUARDRAIL_PATH")
        if path and str(Path(path)) not in sys.path:
            sys.path.insert(0, str(Path(path)))
        try:
            evaluator = importlib.import_module("guardrail_core.evaluator")
            self.evaluate = getattr(evaluator, "evaluate", None) or evaluator.evaluate_request
            self.request_type = importlib.import_module("guardrail_core.models").DecisionRequest
        except Exception:
            self.evaluate = self.request_type = None

    def review(self, finding: Finding | None):
        result = {"action": "block", "action_authorized": False, "status": "INSUFFICIENT EVIDENCE",
                  "summary": "Finding is missing, stale, simulated or lacks comparable evidence.",
                  "trace_id": None, "decision_id": None, "policy_id": None, "created_at": None}
        if finding is None or finding.mode != "real" or finding.separation_km is None:
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
            if any(not value for value in ids.values()):
                raise ValueError("Incomplete Guardrail record")
            return {**result, **ids, "status": "ADVISORY ONLY", "action": action,
                    "summary": "Guardrail recommendation only. DS-M1 does not authorize or execute operational actions."}
        except Exception:
            return {**result, "status": "UNAVAILABLE", "summary": "Guardrail evaluation failed; operational actions blocked."}
