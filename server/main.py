from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .demo import demo_snapshot
from .governance import Governance
from .tle_ingestor import TLEIngestor


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    finding_id: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]+$")


def create_app(ingestor=None, governance=None):
    ingestor = ingestor or TLEIngestor()
    governance = governance or Governance(settings=ingestor.settings, clock=ingestor.clock)

    @asynccontextmanager
    async def lifespan(app):
        await ingestor.start()
        try:
            yield
        finally:
            await ingestor.stop()

    app = FastAPI(title="Defiant Sky DS-M1", lifespan=lifespan)
    app.add_middleware(CORSMiddleware,
        allow_origins=["http://localhost:8000", "http://127.0.0.1:8000", "null"],
        allow_credentials=False, allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Content-Type"])

    @app.get("/api/tle")
    async def get_tle():
        return await ingestor.get_tle_data()

    @app.get("/api/findings")
    async def get_findings():
        return await ingestor.findings()

    @app.get("/api/demo")
    async def get_demo():
        return demo_snapshot()

    @app.get("/api/health")
    async def health():
        snapshot = await ingestor.findings()
        return {"status": "RUNNING", "data_state": snapshot["data_state"],
                "evidence_status": snapshot["status"], "action_authorized": False}

    @app.post("/api/guardrail/evaluate")
    async def evaluate_guardrail(body: ReviewInput):
        snapshot = await ingestor.findings()
        finding = next((f for f in snapshot["findings"] if f.finding_id == body.finding_id), None)
        return governance.review(finding)

    app.mount("/", StaticFiles(directory=str(Path(__file__).resolve().parent.parent / "client"), html=True), name="client")
    return app


app = create_app()
