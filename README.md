# Defiant Sky — DS-M1 orbital evidence prototype

Defiant Sky compares successive published Space-Track element sets and displays
explainable **candidate orbital-element divergence**. It is a local FastAPI and
CesiumJS prototype, with a separate, explicitly selected synthetic demonstration.
It does not detect maneuvers, intent, threats, unidentified aircraft or alien origin.

## Setup and run

Tested with Python 3.11 and Node.js 22+ (Node is needed only for client tests).
From the repository root on Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -c constraints.txt -r requirements-dev.txt
Copy-Item .env.example .env  # First setup only; do not overwrite an existing .env.
$env:DS_LIVE_ENABLED = '0'   # Offline review: no credentials or external data fetch.
python -m uvicorn server.main:app --host 127.0.0.1 --port 8000
```

On macOS/Linux use `source .venv/bin/activate` and
`DS_LIVE_ENABLED=0 python -m uvicorn server.main:app --host 127.0.0.1 --port 8000`.
Open [localhost:8000](http://localhost:8000). The API and findings work offline;
the globe currently loads the existing pinned CesiumJS 1.114 and satellite.js 5.0.0
scripts from public CDNs, so a fresh browser needs internet access for visualization.
The findings panel remains usable if those libraries or WebGL are unavailable.

For live collection, set `SPACETRACK_USER` and `SPACETRACK_PASS` locally in `.env`,
then set `DS_LIVE_ENABLED=1` and restart. Do not commit credentials. The ingestor
requests up to 50 latest LEO GP records at startup and every 600 seconds. A first
valid fetch normally produces **INSUFFICIENT DATA**: two distinct comparable epochs
for the same object must accumulate before any comparison is possible. Repeated
identical downloads do not create history. The top-50 query does not guarantee
continuous coverage of the same objects.

Use one server worker for this local prototype. Bind to loopback; this milestone
does not provide a multi-user authentication or operational authorization service.

## What the labels mean

| Label | Meaning |
| --- | --- |
| LIVE DATA | A successful live fetch occurred within two refresh intervals and usable fresh elements exist. This describes feed activity, not live sensor tracking. |
| HISTORICAL COMPARISON | Locally retained, still-valid element sets are being reviewed; the service may be offline or a fetch may have failed. Each real finding is a historical comparison even while the feed is live. |
| SIMULATED DEMO | Explicit switch; synthetic catalog object 99999 at a fixed simulation clock. No real history writes, policy evaluation or authorization. |
| UNAVAILABLE | No fresh evidence, rejected source records, storage failure, or API failure. No operational action is authorized. |

Each object finding is one of `INSUFFICIENT DATA`, `WITHIN THRESHOLD`, or
`CANDIDATE ORBITAL-ELEMENT DIVERGENCE`. `REVIEW` severity means review of a screening
threshold crossing, not a risk or threat classification. No numerical confidence
or probability is calculated. A within-threshold result is not proof of nominal behavior.

The default page has no timer-triggered anomaly. The demo switch starts a brief
staged sequence using the same detector on synthetic elements. Turning it off clears
the synthetic objects, findings and pending callbacks. The demo uses a fixed epoch
for the globe, so old fixture elements are not falsely shown as current observations.

## Evidence and metric

`server/evidence.py` defines typed records containing NORAD catalog ID, object name,
element epoch, ingestion time, orbital elements, original TLE lines, source, mode,
and SHA-256 content identity. The hash identifies the two TLE lines separated by a
newline; it is **not** a digital signature, trusted-source attestation or audit fingerprint.
Validity and element age are computed at request time. Space-Track GP catalog IDs
and epochs must agree with the TLEs. Fixed-width formatting, checksums, orbital
ranges, finite values and SGP4 validity at epoch are checked before persistence.

The detector selects the latest two distinct sets for an object and requires
strictly increasing, unambiguous epochs within the configured gap. At the **newer
epoch**, it propagates both sets using `sgp4==2.25`, WGS72, and computes:

```text
separation_km = norm(position_from_older_TLE(t_new) - position_from_newer_TLE(t_new))
candidate if separation_km > DS_THRESHOLD_KM
```

Both positions use the same TEME frame and units of kilometers. This is a
model-to-model consistency metric, **not** a residual against independent radar,
optical or RF measurements. See the [SGP4 package documentation](https://pypi.org/project/sgp4/)
for the propagator, coordinate frame, units and error handling.

Catalog fitting noise, drag, limited TLE precision and propagation error can all
create disagreement. There is no covariance or calibrated uncertainty estimate.
The 10 km default is an illustrative screening setting requiring validation, not
an operational threshold. Stale or future input, missing provenance, catalog/source
mismatch, ambiguous/equal epochs, excessive epoch gaps, insufficient history and
propagation failures yield `INSUFFICIENT DATA` with no metric or confidence.
Any rejected record degrades the current batch rather than silently reusing a prior
candidate as current evidence. Diagnostic rejection counts do not contain remote
response bodies or credentials.

## Persistence and settings

Valid real sets are saved transactionally in `data/element_history.sqlite3`, ignored
by Git. History is deduplicated by TLE content, retaining the original ingestion time.
Retention runs at startup and each successful ingestion. Records are bounded by
age, per-object count, and total object count; records removed by these limits
cannot contribute to subsequent comparisons. SQLite may retain freed pages for reuse.
Loaded records are reconstructed and validated against their TLEs and provenance.
This is local persistence, not a tamper-proof audit ledger.

The previous `data/tle_cache.json` is left untouched and is **not** promoted into
evidence: the legacy schema lacks trustworthy per-record ingestion/provenance.
Fresh collection starts the new history. Existing `.env` files remain supported.

| Environment setting | Default | Purpose |
| --- | --- | --- |
| DS_LIVE_ENABLED | 1 | Set 0 to disable Space-Track fetches. |
| DS_HISTORY_PATH | repository `data/element_history.sqlite3` | Local persistence path. |
| DS_THRESHOLD_KM | 10 | Strictly-greater-than candidate threshold. |
| DS_MAX_AGE_HOURS | 72 | Maximum epoch and ingestion age of both sets. |
| DS_MAX_GAP_HOURS | 72 | Maximum separation between element epochs. |
| DS_RETENTION_DAYS | 30 | Retain sets newer than this epoch age. |
| DS_SETS_PER_OBJECT | 32 | Maximum distinct sets per object (minimum 2). |
| DS_MAX_OBJECTS | 500 | Maximum retained catalog objects. |
| GUARDRAIL_PATH | unset | Optional external Guardrail Python package directory. |

Invalid settings fail startup rather than silently changing detector behavior.
No credentials are returned by the API or written to history. Live fetch errors
retain previous valid history with explicit fetch status; stale history never
becomes current merely because it was read or downloaded again.

## API and governance boundary

| Endpoint | Behavior |
| --- | --- |
| GET /api/tle | Compatible `name`, `tle1`, `tle2` list, plus `catalog_id`; fresh valid real objects only. |
| GET /api/findings | Read-only structured findings, source state, ingestion time, thresholds, evidence and limitations. |
| GET /api/demo | Isolated synthetic sample and fixed evaluation clock; never mutates real history. |
| GET /api/health | Process state and evidence availability; does not claim universal NOMINAL status. |
| POST /api/guardrail/evaluate | Accepts `{"finding_id":"<64-character ID>"}`; resolves current evidence on the server and requests an advisory external policy evaluation. |

The Guardrail adapter preserves the existing `sda-ops-v1.yml` /
`sda_anomaly_response` boundary without changing Guardrail, Spartan or DGCE.
It supports the existing `evaluate` / `evaluate_request` import variants.
Guardrail is optional for read-only evidence review; import errors, evaluation
errors, unknown decisions and missing decision identifiers return `block` and
`UNAVAILABLE`. Missing/stale/simulated evidence is blocked before evaluation.
The old client-supplied classification/confidence POST schema is intentionally
rejected. A finding ID is rechecked at evaluation time, so it cannot preserve
authorization after the underlying evidence becomes stale.

**Finding displayed does not mean action authorized.** All DS-M1 responses report
`action_authorized: false`, even if an external engine recommends `allow`. The UI
shows only actual returned trace/decision/policy IDs and time, never fabricated
approvals, policy checkmarks, signatures or fingerprints. There is no operational
action executor, durable operator-action audit stream, or claimed GDF certification.
The prior Escalate/Monitor/Ignore console actions are replaced with an explicit
disabled operational-action control. Real Guardrail policy compatibility still
requires owner validation against the installed package and policy pack.

## Offline verification and reproducible sample

```powershell
python -m pytest -q
node --test tests/client.test.cjs
python -m server.demo
```

The sample command prints a **SIMULATED DEMO** finding with separation
`223.72912913292396 km` at `2026-11-06T00:00:00Z`, using a fixed evaluation clock of
`2026-11-06T01:00:00Z`. Fixture generation is synthetic; these are not downloaded
Space-Track records and are never promoted into a live catalog by the demo path.
Real mode with no local history correctly returns `INSUFFICIENT DATA`.

Tests cover deduplication, known SGP4 output, changed elements, thresholds, stale
and invalid data, catalog/epoch mismatch, ambiguous epochs, persistence, retention,
corrupt/missing provenance, source failures, simulated/live segregation, missing or
failing Guardrail, advisory-only results, and frontend mode-switch races. Transport
and governance fixtures are fakes; tests use neither live credentials nor external
services. GitHub Actions runs the suite on Windows and Linux. `constraints.txt`
records the tested dependency versions.

## Baseline and historical intent

DS-M1 starts from commit `564b803ce26cb896e2ef8bcc23846ae599f9f215` (Step 7).
The owner checkout was clean on `main` and matched `origin/main` before development;
implementation used the dedicated `codex/ds-m1-orbital-evidence` worktree branch.
Historical project versions v1 through v1.3, the control-plane pitch, concept pitch
and dual-domain prospectus were read as architecture/history references only.
The reference directory was not changed and its contents are not redistributed here.

The references establish a layered ingestion → visualization → analytics →
governance → operator intent, and document the evolution from build plans to a
completed staged demo. The repository confirmed a live ingestion/visualization
foundation, timed anomaly trigger and hardcoded classifier, rather than trained
ML, Kalman tracking, conjunction assessment or sensor fusion. The old displayed
SHA-256 fingerprint was a trace-ID substring, and operator actions were console
logs. Those are not validated audit or authorization capabilities. Historical
operational/TRL claims are not implementation evidence or independently verified.
DS-M1 follows Issue #1's explicit distinction between displaying findings and
authorizing actions, rather than implying historical plans are already implemented.

## Separately gated future observation adapter (documentation only)

A future radar/optical/RF adapter would require sensor identity and calibration,
UTC timestamp and clock uncertainty, geolocation and coordinate frame, measurement
units, measurement uncertainty/covariance, provenance, and raw-evidence references.
It must retain the distinction between published orbital elements and independent
observations; a TLE is not a radar detection.

Before any classification, that milestone must validate time/frame conversions,
correlate detections into tracks with explicit association uncertainty, and
cross-check known satellites, aircraft and other explainable sources. Ambiguous
association or missing evidence must permit an **unresolved observation** outcome.
“Unidentified” means no reliable identity match; it does not establish an
“unexplained” physical phenomenon. Orbital TLE catalogs alone cannot discover a
previously untracked airborne UFO.

Owner gates: approve the measurement contract and lawful data access, validate
against known events and negative controls, calibrate uncertainty/false-positive
rates, and integrate authenticated human review with a real durable GDF audit
boundary before considering operational use. No autonomous response, pursuit,
engagement, production multi-sensor fusion or trained classifier is included in
DS-M1. The next milestone should first validate the screening metric on approved
historical orbital updates and harden the governance/audit integration.
