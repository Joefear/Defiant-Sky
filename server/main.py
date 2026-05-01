from contextlib import asynccontextmanager
from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

GUARDRAIL_PATH = Path("C:/Users/samcf/Desktop/Dev/defiant-guardrail/packages/guardrail-core-python")
if str(GUARDRAIL_PATH) not in sys.path:
    sys.path.insert(0, str(GUARDRAIL_PATH))

try:
    from guardrail_core.evaluator import evaluate
except ImportError:
    from guardrail_core.evaluator import evaluate_request as evaluate
from guardrail_core.models import DecisionRequest

from .tle_ingestor import TLEIngestor


ingestor = TLEIngestor()
CLIENT_DIR = Path(__file__).parent.parent / "client"


class ClassificationInput(BaseModel):
    rso_name: str
    classification: str
    confidence: float
    risk_category: str
    policy_pack: str = "default.yml"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await ingestor.start()
    try:
        yield
    finally:
        await ingestor.stop()


app = FastAPI(title="Space Domain Awareness API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost",
        "http://localhost:8000",
        "http://127.0.0.1",
        "http://127.0.0.1:8000",
        "null",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/api/tle")
async def get_tle():
    return await ingestor.get_tle_data()


@app.get("/api/health")
async def health():
    return {"status": "NOMINAL"}


@app.post("/api/guardrail/evaluate")
async def evaluate_guardrail(classification_input: ClassificationInput):
    try:
        request = DecisionRequest(
            text=(
                "SDA anomaly classification: "
                f"{classification_input.classification}, "
                f"confidence {classification_input.confidence}, "
                f"RSO {classification_input.rso_name}, "
                f"risk {classification_input.risk_category}"
            ),
            policy_pack=None,
            policy_files=["default.yml"],
            mode="action",
            requested_tool_name="sda_anomaly_response",
            requested_command=None,
            session_summary=None,
            session_anomaly_flags=None,
            code_graph_summary=None,
            include_trace=True,
        )
        response = evaluate(request)
        return {
            "action": response.action_recommendation,
            "trace_id": response.trace_id,
            "policy_id": response.policy_id,
            "summary": response.summary,
            "findings": response.findings,
            "created_at": response.created_at,
            "decision_id": response.decision_id,
        }
    except Exception:
        return {
            "action": "block",
            "summary": "Guardrail evaluation failed",
            "trace_id": "ERROR",
            "findings": [],
        }


app.mount("/", StaticFiles(directory=str(CLIENT_DIR), html=True), name="client")
