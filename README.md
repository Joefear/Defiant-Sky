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

Live ingestion is **disabled by default**. Credentialed validation requires separate
owner authorization. After approval, configure `SPACETRACK_USER`, `SPACETRACK_PASS`
and an explicit `DS_CATALOG_IDS` comma-separated cohort locally, then set
`DS_LIVE_ENABLED=1`. Never commit credentials. The cohort is sorted, deduplicated,
stable across retrievals and limited to 50 numeric TLE-compatible IDs (1–99999).
There is no global latest-50 selection, automatic cohort discovery or historical backfill.
Missing/decayed objects remain missing; other objects are not substituted automatically.

A first valid fetch normally produces **INSUFFICIENT DATA**: two distinct comparable
epochs for the same object must accumulate before comparison. Identical downloads
and repeated epochs do not create comparable history. The cohort permits repeated
coverage of the same objects but cannot guarantee provider publication of new epochs.

### Provider polling and restart safety

[Official Space-Track API guidance](https://www.space-track.org/documentation#/api),
checked 2026-11-06, specifies hourly GP retrieval, off-peak scheduling, aggregate
queries and bounds below 30 requests/minute and 300/hour. DS-M1 sends one login and
one aggregate GP query per scheduled attempt. The query uses `NORAD_CAT_ID`,
`DECAY_DATE/null-val`, `EPOCH/>now-10`, the existing LEO mean-motion range, and `gp`
rather than `gp_history`. The full request format remains subject to authorized
live verification; offline transport tests validate the constructed URL and parser.

`DS_REFRESH_SECONDS` defaults to 3600 and cannot be lower. The first retrieval is
scheduled at a randomly selected minute excluding 00 and 30, with no immediate
startup fetch. An atomic SQLite reservation records the next permitted attempt
**before** login. A 60-second total network budget is added to the cooldown, giving
at least 60 minutes between GP requests even with a delayed login or crash. Delayed
wakes at minute 00 or 30 are rescheduled. Failures, cancellation, backwards clock
changes and process restarts retain the reservation. Requests sharing this database
cannot reserve the same interval; historical reads remain responsive during fetches.

Use the same persistent `DS_HISTORY_PATH` across restarts and all instances sharing
this account. Replacing/deleting it or using another database loses coordination;
the app cannot police other tools or account users. Before authorized use, coordinate
those clients with the owner. API/UI reads do not trigger a provider request.

Use one server worker for this local prototype. Bind to loopback; this milestone
does not provide a multi-user authentication or operational authorization service.

## What the labels mean

| Label | Meaning |
| --- | --- |
| LIVE DATA | A successful live fetch occurred within two refresh intervals and usable fresh elements exist. This describes feed activity, not live sensor tracking. |
| HISTORICAL COMPARISON | Locally retained, still-valid element sets are being reviewed; the service may be offline or a fetch may have failed. Each real finding is a historical comparison even while the feed is live. |
| SIMULATED DEMO | Explicit switch; synthetic catalog object 99999 at a fixed simulation clock. No real history writes, policy evaluation or authorization. |
| UNAVAILABLE | No usable fresh evidence, an unidentified/corrupt input, storage failure, or API failure. Object-specific rejections suppress that object's finding; unrelated valid objects remain reviewable with degraded feed status. |

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
Identifiable rejected updates quarantine only that cohort object's findings and
globe position. Quarantine is persisted with history and survives unrelated batches
and restarts until a strictly newer valid epoch arrives. A duplicate cannot clear it.
An unidentified malformed batch or corrupt stored row fails closed globally; a
subsequent identifiable batch can clear the unidentified-batch flag. Corrupt stored
rows require owner investigation or retention expiry. Do not delete the database
to repair it without preserving its retrieval cooldown. Diagnostics contain counts
and catalog IDs, never remote response bodies or credentials.

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
| DS_LIVE_ENABLED | 0 | Set 1 only after explicit configuration and owner authorization for live access. |
| DS_CATALOG_IDS | empty | Stable comma-separated cohort; required for live mode; at most 50 IDs in 1–99999. |
| DS_REFRESH_SECONDS | 3600 | Minimum permitted interval, plus the 60-second network budget; busy-minute deferral can extend it. |
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
errors, unknown decisions and missing/malformed decision identifiers return `block` and
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
disabled operational-action control. `decision_source` distinguishes local safety
blocks from external Guardrail recommendations. The adapter revalidates evidence,
requires canonical nonzero UUID trace/decision IDs, policy ID `sda-ops-v1`, an aware
recent decision timestamp (at most five minutes old, one minute future clock skew),
and no engine `evaluation_error` findings. An engine failure cannot masquerade as a
successful policy block. No returned ID is converted into operational authorization.

The installed runtime at Guardrail commit `63f9dee7f9188df0008ca42a7de8ca195104c644`
was tested using unchanged copies of its Python source, compiled TypeScript bridge
and SDA policy. Actual `evaluate_request(DecisionRequest(...))` returned
`require_approval` for `sda_anomaly_response`; returned IDs matched the isolated audit
record. Missing-policy and bridge-failure cases returned local fail-closed results.
Other policy/runtime versions still require isolated compatibility verification.

### Repeat isolated installed-engine verification

```powershell
python -m pip install -r requirements-guardrail-test.txt
python tools/prepare_guardrail_copy.py C:/path/to/defiant-guardrail C:/task-work/new-guardrail-copy
$env:DS_GUARDRAIL_TEST_COPY = 'C:/task-work/new-guardrail-copy'
$env:PYTHONDONTWRITEBYTECODE = '1'
python -m pytest tests/test_guardrail_installed.py -q
Remove-Item Env:DS_GUARDRAIL_TEST_COPY
```

The copy destination must be new and outside the source repository. The helper
copies runtime code and policy only, records source hashes, and never imports the
source installation. Tests refuse the original path and assert the bridge and audit
paths resolve inside the copy. Runtime code and policies are not edited. Fault tests
change in-memory lookup functions only. The unchanged SDA policy always requires
approval for this action: clean `allow`/`block`/`require_approval` adapter outcomes
are covered by stubs. A separate actual-engine nonmatching-tool `allow` probe is
replayed through the adapter to check the response contract; it is not evidence of
an SDA `allow`. The actual-engine suite is opt-in and skipped in ordinary CI.

### Authorized live validation still required

No credentialed Space-Track requests were performed for this milestone. After
separate approval, the owner should select a small cohort, coordinate account usage,
configure credentials locally, and retain one history database. Observe the first
scheduled fetch and a later permitted fetch with a new published epoch. Capture
sanitized request times, object IDs, epochs, validity, deduplication counts and
candidate/insufficient-data outcomes. Restart during cooldown and verify no extra
login/query. Do not speed up polling or backfill if no new epoch is published.
Actual API field availability, authentication and repeated-element coverage remain
unverified until that evidence is obtained. This procedure grants no authority to
run live requests now, merge, deploy, or enable operational actions.

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
