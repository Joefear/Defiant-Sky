(function () {
  "use strict";
  const base = location.protocol === "file:" ? "http://localhost:8000" : "";
  const el = (id) => document.getElementById(id);
  let generation = 0;
  let demoTimer = null;
  let demoClock = null;
  let viewer = null;
  let tracked = [];
  let reviewGeneration = 0;

  // Findings remain usable when remote visualization libraries or WebGL are unavailable.
  try {
    Cesium.Ion.defaultAccessToken = "";
    viewer = new Cesium.Viewer("cesiumContainer", {
      animation: false, baseLayer: false, baseLayerPicker: false, fullscreenButton: false,
      geocoder: false, homeButton: false, infoBox: false, sceneModePicker: false,
      selectionIndicator: false, timeline: false, navigationHelpButton: false,
      shouldAnimate: false, skyAtmosphere: false, skyBox: false,
    });
    viewer.scene.backgroundColor = Cesium.Color.fromCssColorString("#050711");
    viewer.scene.globe.baseColor = Cesium.Color.fromCssColorString("#101625");
    viewer.camera.setView({ destination: Cesium.Cartesian3.fromDegrees(-95, 25, 24000000) });
    if (typeof satellite === "undefined") throw new Error("satellite.js unavailable");
    el("globeStatus").textContent = "SGP4 propagated positions (not sensor tracks)";
  } catch (_) {
    viewer = null;
    el("globeStatus").textContent = "Globe unavailable: libraries or WebGL missing";
  }

  async function get(path) {
    const response = await fetch(base + path, { cache: "no-store" });
    if (!response.ok) throw new Error("API unavailable");
    return response.json();
  }

  function clearGlobe() {
    if (viewer) tracked.forEach(({ entity }) => viewer.entities.remove(entity));
    tracked = [];
    el("objectCount").textContent = "0";
  }

  function showObjects(tles, findings) {
    clearGlobe();
    if (!viewer) return;
    const candidates = new Set(findings.filter((f) => f.severity === "REVIEW").map((f) => f.catalog_id));
    tles.forEach((tle) => {
      const satrec = satellite.twoline2satrec(tle.tle1, tle.tle2);
      const entity = viewer.entities.add({ name: tle.name, position: Cesium.Cartesian3.ZERO,
        point: { pixelSize: 7, color: candidates.has(tle.catalog_id) ? Cesium.Color.YELLOW : Cesium.Color.WHITE },
        label: { text: tle.name, font: "12px Arial", pixelOffset: new Cesium.Cartesian2(0, -16),
          distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 9000000) },
      });
      tracked.push({ entity, satrec });
    });
    updatePositions();
  }

  function updatePositions() {
    if (!viewer) return;
    const now = demoClock || new Date();
    let visible = 0;
    tracked.forEach(({ entity, satrec }) => {
      const position = satellite.propagate(satrec, now).position;
      if (!position || ![position.x, position.y, position.z].every(Number.isFinite)) { entity.show = false; return; }
      const geo = satellite.eciToGeodetic(position, satellite.gstime(now));
      entity.position = Cesium.Cartesian3.fromDegrees(satellite.degreesLong(geo.longitude), satellite.degreesLat(geo.latitude), geo.height * 1000);
      entity.show = true;
      visible += 1;
    });
    el("objectCount").textContent = String(visible);
  }

  function paragraph(parent, text, className = "") {
    const p = document.createElement("p");
    p.className = className;
    p.textContent = text;
    parent.append(p);
  }

  function renderFindings(findings, demo, token) {
    el("findings").replaceChildren();
    findings.forEach((finding) => {
      const card = document.createElement("article");
      card.className = "finding";
      card.dataset.severity = finding.severity;
      const title = document.createElement("h3");
      title.textContent = `${finding.name} · NORAD ${finding.catalog_id}`;
      card.append(title);
      paragraph(card, `${demo ? "SIMULATED DEMO · " : "HISTORICAL COMPARISON · "}${finding.status}`);
      paragraph(card, `Severity: ${finding.severity} | Quality: ${finding.quality}`);
      paragraph(card, finding.reason);
      paragraph(card, `Separation: ${finding.separation_km === null ? "not computed" : finding.separation_km.toFixed(3) + " km"} | Threshold: ${finding.threshold_km} km`);
      paragraph(card, `Comparison epoch: ${finding.comparison_epoch || "unavailable"} | Evaluated: ${finding.evaluated_at}`);
      finding.evidence.forEach((evidence) => {
        const r = evidence.record;
        paragraph(card, `${r.provenance.source} · ${r.provenance.kind} · ${evidence.validity}\nEpoch: ${r.epoch}\nIngested: ${r.ingested_at}\nElement age: ${evidence.age_hours.toFixed(2)} hours`, "evidence");
      });
      paragraph(card, finding.limitations, "limits");
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = "Element sets and provenance (content hashes, not signatures)";
      const pre = document.createElement("pre");
      pre.textContent = JSON.stringify(finding.evidence, null, 2);
      details.append(summary, pre);
      card.append(details);
      const review = document.createElement("button");
      review.type = "button";
      review.textContent = "Request advisory Guardrail evaluation";
      review.disabled = demo || finding.separation_km === null;
      review.addEventListener("click", () => reviewFinding(finding.finding_id, token));
      card.append(review);
      el("findings").append(card);
    });
  }

  async function reviewFinding(id, token) {
    const request = ++reviewGeneration;
    el("governanceStatus").textContent = "EVALUATING — operational actions blocked.";
    el("governanceTrace").textContent = "";
    try {
      const response = await fetch(base + "/api/guardrail/evaluate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ finding_id: id }) });
      if (!response.ok) throw new Error("Unavailable");
      const result = await response.json();
      if (token !== generation || request !== reviewGeneration) return;
      el("governanceStatus").textContent = `${result.status} · ${result.action}: ${result.summary}`;
      el("governanceTrace").textContent = JSON.stringify({ decision_source: result.decision_source, trace_id: result.trace_id, decision_id: result.decision_id, policy_id: result.policy_id, created_at: result.created_at, action_authorized: false }, null, 2);
    } catch (_) {
      if (token === generation && request === reviewGeneration) el("governanceStatus").textContent = "UNAVAILABLE — operational actions blocked.";
    }
  }

  async function load() {
    const token = ++generation;
    const demo = el("demoSwitch").checked;
    clearTimeout(demoTimer);
    demoClock = null;
    clearGlobe();
    document.body.classList.toggle("demo", demo);
    el("demoStage").hidden = !demo;
    el("demoStage").textContent = demo ? "Synthetic sequence: loading element sets…" : "";
    el("dataState").textContent = demo ? "SIMULATED DEMO" : "UNAVAILABLE";
    el("summary").textContent = "Loading evidence…";
    el("refreshStatus").textContent = "";
    el("findings").replaceChildren();
    el("lastUpdated").textContent = "UNAVAILABLE";
    el("governanceStatus").textContent = "NOT EVALUATED — operational actions blocked.";
    el("governanceTrace").textContent = "";
    try {
      const [snapshot, tles] = demo ? [await get("/api/demo"), null] : await Promise.all([get("/api/findings"), get("/api/tle")]);
      if (token !== generation) return;
      if (snapshot.mode !== (demo ? "demo" : "real")) throw new Error("Mode mismatch");
      el("dataState").textContent = snapshot.data_state;
      el("summary").textContent = `${snapshot.status} · ${snapshot.findings.length} catalog object(s)`;
      el("refreshStatus").textContent = demo
        ? snapshot.refresh_status
        : `${snapshot.refresh_status} | History: ${snapshot.storage_error ? "unavailable" : snapshot.unidentified_rejection ? "invalid input" : "no integrity error"} | Cohort: ${(snapshot.catalog_ids || []).join(", ") || "not configured"} | Next retrieval: ${snapshot.scheduling_error ? "blocked (scheduling failure)" : snapshot.next_poll_at || (snapshot.live_enabled ? "not scheduled" : "disabled")} | Scheduler retry: ${snapshot.scheduler_retry_at || "none"} | Rejected this load/batch: ${snapshot.rejected_records || 0} | Quarantined: ${(snapshot.quarantined_catalog_ids || []).join(", ") || "none"}`;
      el("lastUpdated").textContent = snapshot.last_ingested_at || "UNAVAILABLE";
      if (demo) {
        demoClock = new Date(snapshot.evaluated_at);
        el("demoStage").textContent = `Synthetic sequence: element sets loaded. Fixed clock ${snapshot.evaluated_at}`;
        showObjects(snapshot.tles, []);
        // The only staged alert is behind the explicit switch and a request-generation guard.
        demoTimer = setTimeout(() => {
          if (token !== generation || !el("demoSwitch").checked) return;
          el("demoStage").textContent = "Synthetic comparison displayed. Governance and operational actions remain blocked.";
          renderFindings(snapshot.findings, true, token);
          showObjects(snapshot.tles, snapshot.findings);
        }, 3000);
      } else {
        renderFindings(snapshot.findings, false, token);
        showObjects(tles, snapshot.findings);
      }
    } catch (_) {
      if (token !== generation) return;
      el("dataState").textContent = "UNAVAILABLE";
      el("summary").textContent = "INSUFFICIENT DATA — evidence could not be loaded.";
      clearGlobe();
    }
  }

  el("demoSwitch").addEventListener("change", load);
  load();
  setInterval(updatePositions, 2000);
  setInterval(() => { if (!el("demoSwitch").checked) load(); }, 30000);
})();
