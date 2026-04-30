from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .tle_ingestor import TLEIngestor


ingestor = TLEIngestor()


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
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/api/tle")
async def get_tle():
    return await ingestor.get_tle_data()


@app.get("/api/health")
async def health():
    return {"status": "NOMINAL"}
