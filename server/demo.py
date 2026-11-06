"""Explicit synthetic demonstration; never inserted into the live history store."""

from datetime import datetime
import json
from pathlib import Path

from .evidence import Settings, detect, parse_element_set


def demo_snapshot():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "synthetic.json").read_text())
    now = datetime.fromisoformat(fixture["evaluated_at"])
    records = [parse_element_set(raw, datetime.fromisoformat(raw["ingested_at"]), "demo") for raw in fixture["records"]]
    return {
        "mode": "demo", "data_state": "SIMULATED DEMO", "status": "SYNTHETIC FIXTURE",
        "evaluated_at": now, "last_ingested_at": records[-1].ingested_at,
        "refresh_status": "No live fetch; fixed historical simulation clock", "settings": Settings(),
        "action_authorized": False, "findings": [detect(records, now, Settings())],
        "tles": [{"name": r.name, "tle1": r.tle1, "tle2": r.tle2, "catalog_id": r.catalog_id} for r in records[-1:]],
    }


if __name__ == "__main__":
    from fastapi.encoders import jsonable_encoder
    print(json.dumps(jsonable_encoder(demo_snapshot()), indent=2))
