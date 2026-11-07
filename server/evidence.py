"""Published orbital elements, not independent sensor observations."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import math
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sgp4.api import Satrec, WGS72, jday
from sgp4.earth_gravity import wgs72
from sgp4.io import twoline2rv, verify_checksum

UTC = timezone.utc
LIMITATIONS = (
    "SGP4/WGS72 TEME position separation between two published element sets at the newer epoch. "
    "This is model-to-model disagreement, not an independent observation residual. "
    "Catalog fitting noise, drag and propagation error can cause divergence. No covariance is "
    "available; uncertainty is unquantified. The threshold is a screening setting, not a "
    "calibrated probability or evidence of maneuver, intent, threat or unexplained origin."
)


class Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class Settings(Record):
    threshold_km: float = Field(default=10.0, gt=0)
    max_age_hours: float = Field(default=72.0, gt=0)
    max_gap_hours: float = Field(default=72.0, gt=0)
    retention_days: int = Field(default=30, ge=1)
    sets_per_object: int = Field(default=32, ge=2)
    max_objects: int = Field(default=500, ge=1)


class Elements(Record):
    inclination_deg: float
    raan_deg: float
    eccentricity: float
    argument_of_perigee_deg: float
    mean_anomaly_deg: float
    mean_motion_rev_day: float
    bstar: float


class Provenance(Record):
    source: Literal["Space-Track.org", "DS-M1 synthetic fixture"]
    kind: Literal["published_orbital_elements"] = "published_orbital_elements"
    mode: Literal["real", "demo"]
    content_sha256: str
    # Content identity only; never an audit signature or governance approval.


class ElementSet(Record):
    catalog_id: int
    name: str
    epoch: AwareDatetime
    ingested_at: AwareDatetime
    tle1: str
    tle2: str
    elements: Elements
    provenance: Provenance


class EvidenceView(Record):
    record: ElementSet
    age_hours: float
    validity: Literal["VALID", "STALE", "FUTURE"]


class Finding(Record):
    finding_id: str
    catalog_id: int
    name: str
    mode: Literal["real", "demo"]
    status: Literal["CANDIDATE ORBITAL-ELEMENT DIVERGENCE", "WITHIN THRESHOLD", "INSUFFICIENT DATA"]
    severity: Literal["REVIEW", "INFORMATION", "NONE"]
    quality: str
    reason: str
    evaluated_at: AwareDatetime
    comparison_epoch: AwareDatetime | None = None
    separation_km: float | None = None
    threshold_km: float
    evidence: list[EvidenceView]
    limitations: str = LIMITATIONS
    action_authorized: Literal[False] = False


def parse_element_set(raw: dict, ingested_at: datetime, mode: str = "real") -> ElementSet:
    """Validate fixed-width TLEs and reconcile the GP catalog ID/epoch when supplied."""
    if ingested_at.tzinfo is None or mode not in {"real", "demo"}:
        raise ValueError("Invalid ingestion context")
    line1, line2 = raw["tle1"].rstrip(), raw["tle2"].rstrip()
    name = raw["name"].strip()
    if not name or len(name) > 256 or any(len(s) != 69 or not s.isascii() for s in (line1, line2)):
        raise ValueError("Invalid TLE format")
    if not line1[68].isdigit() or not line2[68].isdigit():
        raise ValueError("Missing TLE checksum")
    verify_checksum(line1, line2)
    twoline2rv(line1, line2, wgs72)  # Strict parser; accelerated Satrec is permissive.
    sat = Satrec.twoline2rv(line1, line2, WGS72)
    catalog_id = sat.satnum
    if raw.get("catalog_id") is not None and int(raw["catalog_id"]) != catalog_id:
        raise ValueError("Catalog ID mismatch")
    year = int(line1[18:20])
    year += 1900 if year >= 57 else 2000
    day = float(line1[20:32])
    start = datetime(year, 1, 1, tzinfo=UTC)
    if not 1 <= day < (datetime(year + 1, 1, 1, tzinfo=UTC) - start).days + 1:
        raise ValueError("Invalid element epoch")
    epoch = start + timedelta(days=day - 1)
    if raw.get("epoch"):
        supplied = raw["epoch"] if isinstance(raw["epoch"], datetime) else datetime.fromisoformat(raw["epoch"].replace("Z", "+00:00"))
        if supplied.tzinfo is None:  # Space-Track EPOCH is UTC without suffix.
            supplied = supplied.replace(tzinfo=UTC)
        if abs((epoch - supplied).total_seconds()) > 0.001:
            raise ValueError("GP/TLE epoch mismatch")
    values = Elements(
        inclination_deg=float(line2[8:16]), raan_deg=float(line2[17:25]),
        eccentricity=float("0." + line2[26:33]),
        argument_of_perigee_deg=float(line2[34:42]), mean_anomaly_deg=float(line2[43:51]),
        mean_motion_rev_day=float(line2[52:63]), bstar=sat.bstar,
    )
    if not (0 <= values.inclination_deg <= 180 and 0 <= values.eccentricity < 1
            and 0 < values.mean_motion_rev_day <= 18
            and all(0 <= x < 360 for x in (values.raan_deg, values.argument_of_perigee_deg, values.mean_anomaly_deg))):
        raise ValueError("Invalid orbital elements")
    error, position, _ = sat.sgp4(sat.jdsatepoch, sat.jdsatepochF)
    if error or not all(math.isfinite(v) for v in position):
        raise ValueError("SGP4 invalid at epoch")
    return ElementSet(
        catalog_id=catalog_id, name=name, epoch=epoch, ingested_at=ingested_at,
        tle1=line1, tle2=line2, elements=values,
        provenance=Provenance(source="Space-Track.org" if mode == "real" else "DS-M1 synthetic fixture",
                              mode=mode, content_sha256=sha256(f"{line1}\n{line2}".encode("ascii")).hexdigest()),
    )


def evidence_view(record: ElementSet, now: datetime, settings: Settings) -> EvidenceView:
    age = (now - record.epoch).total_seconds() / 3600
    ingestion_age = (now - record.ingested_at).total_seconds() / 3600
    validity = "FUTURE" if age < 0 or ingestion_age < 0 else "STALE" if max(age, ingestion_age) > settings.max_age_hours else "VALID"
    return EvidenceView(record=record, age_hours=age, validity=validity)


def separation_km(previous: ElementSet, current: ElementSet) -> float:
    at = current.epoch
    jd, fraction = jday(at.year, at.month, at.day, at.hour, at.minute, at.second + at.microsecond / 1e6)
    positions = []
    for record in (previous, current):
        sat = Satrec.twoline2rv(record.tle1, record.tle2, WGS72)
        error, position, _ = sat.sgp4(jd, fraction)
        if error or not all(math.isfinite(v) for v in position):
            raise ValueError("SGP4 propagation failed")
        positions.append(position)
    return math.dist(*positions)


def detect(records: list[ElementSet], now: datetime, settings: Settings) -> Finding:
    if not records:
        raise ValueError("An object record is required")
    # Content deduplication also protects direct callers outside the history store.
    records = sorted({r.provenance.content_sha256: r for r in records}.values(), key=lambda r: (r.epoch, r.provenance.content_sha256))
    current = records[-1]
    pair = records[-2:]
    views = [evidence_view(r, now, settings) for r in pair]
    reason = "Two distinct element sets at increasing epochs are required."
    quality = "INSUFFICIENT HISTORY"
    metric = None
    if len({(r.catalog_id, r.provenance.source, r.provenance.mode) for r in records}) != 1:
        reason, quality = "Catalog ID or provenance mismatch.", "INVALID PROVENANCE"
    elif any(v.validity != "VALID" for v in views):
        reason, quality = "Stale or future-dated evidence cannot support a current finding.", "STALE OR FUTURE"
    elif len(pair) == 2:
        gap = (current.epoch - pair[0].epoch).total_seconds() / 3600
        ambiguous = any(sum(r.epoch == selected.epoch for r in records) > 1 for selected in pair)
        if ambiguous or not 0 < gap <= settings.max_gap_hours:
            reason, quality = "Epochs are equal or exceed the comparison window.", "INCOMPARABLE EPOCHS"
        else:
            try:
                metric = separation_km(*pair)
                reason, quality = "Compare both SGP4 positions at the newer element-set epoch.", "COMPARABLE ELEMENT SETS; UNCERTAINTY UNQUANTIFIED"
            except ValueError:
                reason, quality = "SGP4 could not propagate both sets.", "PROPAGATION FAILED"
    status = "INSUFFICIENT DATA" if metric is None else "CANDIDATE ORBITAL-ELEMENT DIVERGENCE" if metric > settings.threshold_km else "WITHIN THRESHOLD"
    identity = "|".join([current.provenance.mode, *(r.provenance.content_sha256 for r in pair), settings.model_dump_json()])
    return Finding(
        finding_id=sha256(identity.encode()).hexdigest(), catalog_id=current.catalog_id,
        name=current.name, mode=current.provenance.mode, status=status,
        severity="NONE" if metric is None else "REVIEW" if metric > settings.threshold_km else "INFORMATION",
        quality=quality, reason=reason, evaluated_at=now, comparison_epoch=current.epoch if metric is not None else None,
        separation_km=metric, threshold_km=settings.threshold_km, evidence=views,
    )
