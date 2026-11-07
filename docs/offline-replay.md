# DS-M2 Stage A: offline historical replay

This stage verifies replay and screening measurements. **Empirical detector
calibration is incomplete.** It adds no provider acquisition, alerting, operational
executor, sensor ingestion or changes to the DS-M1 detector/Guardrail adapter.
All reports retain `action_authorized: false`.

## Run the reproducible synthetic example

From the repository root, with the existing pinned Python dependencies installed:

```text
python -m server.replay server/fixtures/replay-synthetic.json --config server/fixtures/replay-config.json
python -m pytest -q
node --test tests/client.test.cjs
```

The CLI reads two local JSON files and prints machine-readable JSON to stdout.
It has no database argument, output-file writer, environment configuration,
credential handling, app startup, policy evaluation or network client. Replay
storage is memory-only. Do not redirect stdout onto any input, live database or
evidence file. Keep private evaluation datasets/reports outside this repository.
No production history or polling reservations are opened or modified by replay.

The committed sample is entirely artificial: two synthetic catalog IDs, three
epochs per object, an exact duplicate retrieval and an intentionally incorrect
content hash. It derives from the existing DS-M1 artificial TLE fixture, changing
catalog numbers, epochs and mean anomalies with recalculated checksums. It is not
an orbital truth simulation or a collection of real observations. Negative-control
tests additionally cover empty/single-set history, stale/future input, corrupt TLEs,
ambiguous epochs, excess gaps and injected propagation failures. The injected
failure tests validate fail-closed handling, not natural failure prevalence.

Expected sample output: 8 input records, 6 accepted, 2 excluded (one duplicate,
one identity mismatch), 2 objects, 4 unique comparable pairs and 3 insufficient
outcomes. All comparable epoch gaps are 12 hours. Separations range from
61.884297 to 223.729129 km (median 142.806713 km). Grid thresholds 1/10/100/300 km
yield 4/4/2/0 exceedances, or 100%/100%/50%/0% synthetic candidate rates. Training,
calibration and held-out partitions have 0/2/2 comparable pairs respectively.
These artificial values demonstrate sensitivity arithmetic only, not real-world
screening effectiveness or a recommendation to use any of these thresholds.

## Local dataset contract

`schema_version: 1`, an identifier `dataset_id`, `kind` (`synthetic` or `real`),
`source`, a nonempty `usage_basis`, and a `records` array are required. Unknown
fields are rejected. Each record supplies:

| Field | Meaning |
| --- | --- |
| `record_id` | Unique identifier in this dataset (ASCII letters, digits, dot, underscore, hyphen). |
| `catalog_id`, `name` | Original catalog identifier and object name. |
| `epoch` | UTC-aware element epoch, reconciled with the TLE to DS-M1's 1 ms tolerance. |
| `ingested_at` | Original timezone-aware availability/ingestion timestamp, never file import time. |
| `tle1`, `tle2` | Original fixed-width, checksum-valid TLEs. |
| `content_sha256` | Lowercase SHA-256 of the two right-trimmed ASCII TLE lines joined by one LF. |

Use the fixture as a complete schema example. Every TLE is reparsed through
`server.evidence.parse_element_set`; serialized derived orbital values are not
trusted. Source types deliberately match DS-M1: real records must declare
`Space-Track.org`; synthetic records must declare `DS-M1 synthetic fixture` and
are parsed in demo mode. One dataset has one source/kind. Other provider adapters
are not implemented. Never change a synthetic source label to claim real evidence.
Source and usage declarations are operator assertions, not verified licensing or
cryptographic attestations. A content hash detects mismatch, not source authenticity.

A future, properly authorized local export can use this contract without any
network acquisition in this tool. The owner must verify data rights and preserve
original availability timestamps. Historical element epochs alone do not establish
when data was available: exports lacking ingestion timestamps are unsuitable for
this replay. Do not reconstruct timestamps by pretending ingestion occurred at the
element epoch. Do not commit restricted provider records. Reports omit TLE lines,
object names and raw parser errors but retain identifiers, timestamps, content hashes
and the operator's usage statement; review their sensitivity before sharing.

## Replay and exclusions

Events sort by original `ingested_at` in UTC. Equal-time arrivals for an object are
applied as one atomic batch before evaluation, preventing file-order tie selection.
An earlier decision never sees a later ingestion. Future-dated epochs are kept
visible to the unchanged DS-M1 validity gate and yield insufficient data rather
than becoming eligible earlier than their arrival. Original ingestion timestamps
are retained on duplicate content. Identical retrievals are counted as exclusions,
and produce neither new decisions nor new pairs.

Valid unique arrivals trigger `server.evidence.detect` on that object's available
history. This is the **same** detector used by DS-M1: latest two unique sets,
ambiguity/age/gap/provenance checks, SGP4/WGS72 at the newer epoch, TEME position
distance, strictly greater-than threshold comparison. No alternate orbital metric
or threshold classifier is used for findings. The grid counts strict exceedances
of that detector's metric.

An identified invalid TLE/content hash or reused record ID is excluded and places
that object in a conservative replay quarantine. It produces an insufficient-data
outcome. Comparisons resume only after a strictly newer valid epoch, never a
duplicate. A malformed record schema has no trusted identity/time and is recorded
as an **unassigned exclusion**, not assigned a fabricated epoch or catalog ID.
This offline rejection ledger is not a simulation of production batch quarantine.
All exclusions have an input index and reason. Reused IDs are checked as they
arrive, so a future ID conflict cannot invalidate a past decision retrospectively.

All unique records are retained in memory to preserve historical ambiguity.
Replay does not reproduce DS-M1 retention eviction, SQLite persistence, provider
polling or restart behavior. Large datasets require sufficient memory and time;
streaming and performance optimization are deferred. No automatic cohort selection
or data-quality cherry-picking is performed: the selection is the complete local
input file, with every rejection recorded.

## Metric definitions

| Metric | Definition |
| --- | --- |
| Objects evaluated | Distinct catalog IDs with at least one replay outcome, including an identified invalid-only object. |
| Valid comparable pairs | Distinct catalog/content-hash pairs producing a finite SGP4 separation; attributed to their first successful comparison only. |
| Insufficient outcomes | Evaluations with `INSUFFICIENT DATA`, including quarantine; duplicate-only arrivals are exclusions, not evaluations. |
| Epoch gap | Newer minus older selected epoch in hours; summary distribution uses unique comparable pairs. Raw outcomes also expose gaps for two-set insufficient cases. |
| Separation distribution | Min, median, linearly interpolated p95, max in km over unique comparable pairs. Empty summaries contain null values. |
| Candidate rate | Strict threshold exceedances divided by unique comparable pairs. Null when there are no comparable pairs. |
| Quality/exclusion counts | Detector/quarantine quality per evaluation, plus separately recorded rejected records and duplicates. |

Late-arriving older records may cause reevaluation of an existing pair; their
outcomes remain visible but never inflate pair distributions or candidate rates.
Repeated insufficient decisions are event counts, not independent trials. Pairs
share element sets and objects, so observations are correlated. A sample p95 is a
descriptive statistic, not a confidence bound. No precision, recall, false-positive
rate, maneuver label or numerical confidence is produced without independent truth.

## Chronological calibration and held-out structure

The frozen config supplies `training_end`, `calibration_end`, an ascending, unique,
positive finite `thresholds_km` grid and optional DS-M1 `settings` (defaults otherwise).
Training arrivals precede `training_end`; calibration arrivals are at or after
that boundary and before `calibration_end`; held-out arrivals are at or after
`calibration_end`. Boundaries are based on availability, not element epochs.
Both timestamps must be aware and strictly increasing.

Each partition has its own metrics, exclusion counts and accepted-record hash.
Earlier history can seed later comparisons (warm start), but later records cannot
seed earlier comparisons. Pair attribution is global and first-use only. A pair
first compared in calibration cannot inflate held-out counts if reevaluated later.
Unassigned exclusions are reported globally, not silently attributed to a split.

**There is no fitting algorithm or automated threshold selection in Stage A.**
Thresholds and the single detector setting must be predeclared in the config;
the tool records their identity and never chooses them from held-out results.
The calibration partition is an evaluation scaffold, not an assertion that
calibration occurred. Full-report held-out sensitivity is for final descriptive
evaluation; adjusting a plan after inspecting it requires a new untouched holdout.
The tool cannot police an operator's external threshold choices or reruns.
Reports always mark empirical calibration incomplete, for synthetic and real input.

Tests append/change later records and verify training/calibration outputs remain
identical, exercise exact boundaries and simultaneous arrivals, and check original
ingestion times and strict threshold equality. Reproducibility includes canonical
dataset/config hashes, source-file hashes, Python/SGP4 versions, record identities,
split definitions and event ordering. File line endings and runtime versions can
change provenance hashes; cross-platform floating-point comparisons use tolerances,
not a claim of universal byte-identical numerical output.

## Available evidence and scientific disposition

The owner-authorized DS-M1 ISS validation completed three scheduled retrievals,
approximately 61 minutes apart, retaining **two** distinct epochs and one comparable
pair. Its recorded separation was about 0.0812 km; the third retrieval deduplicated.
DS-M1 acceptance and merge evidence remain in PR #2 and the owner's local sanitized
reports. No additional records were acquired for Stage A. No raw live records or
production database are included here or used by the committed example.

That one-object, one-pair sample cannot establish screening performance or calibrate
thresholds. Synthetic counts verify the implementation, not real orbital behavior.
Required future evidence includes owner-approved, lawfully usable, provenance-rich
historical updates across objects and orbital regimes, reliable availability times,
coverage/gap/missingness documentation, independently established labels/negative
controls where available, and a frozen chronological evaluation plan. Study sample
size, dependence, selection bias, uncertainty and regime sensitivity before any
calibration claim. No calibrated probability or validated maneuver detection is
claimed. Radar/optical/RF fusion and operational capabilities remain separately gated.
