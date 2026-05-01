(function () {
  const API_URL = "http://localhost:8000/api/tle";
  const GUARDRAIL_API_URL = "http://localhost:8000/api/guardrail/evaluate";
  const UPDATE_INTERVAL_MS = 2000;
  const LIGHT_BLUE = "#E8F4FD";
  const ALERT_RED = "#FF5C5C";
  const ALERT_YELLOW = "#FFC300";
  const SATELLITE_WHITE = "#FFFFFF";

  const lastUpdatedElement = document.getElementById("lastUpdated");
  const objectCountElement = document.getElementById("objectCount");
  const systemStatusElement = document.getElementById("systemStatus");
  const anomalyAlertElement = document.getElementById("anomalyAlert");
  const anomalyRsoElement = document.getElementById("anomalyRso");
  const anomalyTimeElement = document.getElementById("anomalyTime");
  const aiAnalysisPanelElement = document.getElementById("aiAnalysisPanel");
  const governancePanelElement = document.getElementById("governancePanel");
  const governanceDecisionElement = document.getElementById("governanceDecision");
  const governanceTraceElement = document.getElementById("governanceTrace");

  const trackedObjects = [];
  let anomalyTarget = null;
  let anomalyScheduled = false;
  let anomalyTriggered = false;
  let guardrailEvaluationRequested = false;

  Cesium.Ion.defaultAccessToken = "";

  const viewer = new Cesium.Viewer("cesiumContainer", {
    animation: false,
    baseLayer: false,
    baseLayerPicker: false,
    fullscreenButton: false,
    geocoder: false,
    homeButton: false,
    infoBox: false,
    sceneModePicker: false,
    selectionIndicator: false,
    timeline: false,
    navigationHelpButton: false,
    shouldAnimate: false,
    skyAtmosphere: false,
    skyBox: false,
  });

  viewer.cesiumWidget.creditContainer.style.display = "none";
  viewer.scene.backgroundColor = Cesium.Color.fromCssColorString("#050711");
  viewer.scene.globe.baseColor = Cesium.Color.fromCssColorString("#101625");
  viewer.scene.globe.enableLighting = true;
  viewer.scene.screenSpaceCameraController.minimumZoomDistance = 1500000;
  viewer.scene.screenSpaceCameraController.maximumZoomDistance = 90000000;
  viewer.camera.setView({
    destination: Cesium.Cartesian3.fromDegrees(-95.0, 25.0, 24000000),
  });

  async function loadTleData() {
    setStatus("LOADING", LIGHT_BLUE);

    try {
      const response = await fetch(API_URL, { cache: "no-store" });
      if (!response.ok) {
        throw new Error(`TLE request failed with ${response.status}`);
      }

      const tles = await response.json();
      if (!Array.isArray(tles)) {
        throw new Error("TLE response was not an array");
      }

      clearTrackedObjects();

      tles.forEach((tle) => {
        if (!tle.name || !tle.tle1 || !tle.tle2) {
          return;
        }

        const satrec = satellite.twoline2satrec(tle.tle1, tle.tle2);
        const entity = viewer.entities.add({
          name: tle.name,
          position: Cesium.Cartesian3.ZERO,
          point: {
            color: Cesium.Color.fromCssColorString(SATELLITE_WHITE),
            pixelSize: 7,
            outlineColor: Cesium.Color.fromCssColorString("#0A0F1F"),
            outlineWidth: 1,
          },
          label: {
            text: tle.name,
            font: "12px Arial, sans-serif",
            fillColor: Cesium.Color.fromCssColorString(LIGHT_BLUE),
            outlineColor: Cesium.Color.BLACK,
            outlineWidth: 2,
            style: Cesium.LabelStyle.FILL_AND_OUTLINE,
            pixelOffset: new Cesium.Cartesian2(0, -16),
            distanceDisplayCondition: new Cesium.DistanceDisplayCondition(0, 9000000),
          },
        });

        trackedObjects.push({ entity, satrec });
      });

      updateSatellitePositions();
      scheduleAnomalyTrigger();
      objectCountElement.textContent = String(trackedObjects.length);
      lastUpdatedElement.textContent = new Date().toISOString();
      setStatus("NOMINAL", LIGHT_BLUE);
    } catch (error) {
      console.error(error);
      setStatus("DEGRADED", ALERT_RED);
      lastUpdatedElement.textContent = "UNAVAILABLE";
    }
  }

  function clearTrackedObjects() {
    trackedObjects.forEach(({ entity }) => viewer.entities.remove(entity));
    trackedObjects.length = 0;
    objectCountElement.textContent = "0";
  }

  function scheduleAnomalyTrigger() {
    if (anomalyScheduled || anomalyTriggered || trackedObjects.length === 0) {
      return;
    }

    anomalyTarget = trackedObjects[0];
    anomalyScheduled = true;
    window.setTimeout(triggerAnomaly, 10000);
  }

  function triggerAnomaly() {
    if (anomalyTriggered || !anomalyTarget) {
      return;
    }

    anomalyTriggered = true;
    anomalyTarget.entity.point.color = Cesium.Color.fromCssColorString(ALERT_YELLOW);
    anomalyTarget.entity.point.pixelSize = 10;

    anomalyRsoElement.textContent = anomalyTarget.entity.name || "UNKNOWN";
    anomalyTimeElement.textContent = new Date().toISOString();
    anomalyAlertElement.style.display = "block";
    window.setTimeout(showAiAnalysisPanel, 3000);
  }

  function showAiAnalysisPanel() {
    aiAnalysisPanelElement.style.display = "block";
    window.setTimeout(evaluateGuardrailPolicy, 1000);
  }

  async function evaluateGuardrailPolicy() {
    if (guardrailEvaluationRequested || !anomalyTarget) {
      return;
    }

    guardrailEvaluationRequested = true;
    governancePanelElement.style.display = "block";
    setGovernanceDecision("EVALUATING");

    try {
      const response = await fetch(GUARDRAIL_API_URL, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          rso_name: anomalyTarget.entity.name || "UNKNOWN",
          classification: "Unscheduled Maneuver",
          confidence: 87.4,
          risk_category: "MODERATE",
          policy_pack: "default.yml",
        }),
      });

      if (!response.ok) {
        throw new Error(`Guardrail request failed with ${response.status}`);
      }

      const result = await response.json();
      setGovernanceDecision(result.action || "UNKNOWN");
      governanceTraceElement.textContent = result.trace_id || "UNKNOWN";
    } catch (error) {
      console.error(error);
      setGovernanceDecision("block");
      governanceTraceElement.textContent = "ERROR";
    }
  }

  function setGovernanceDecision(action) {
    const normalizedAction = String(action || "UNKNOWN").toLowerCase();
    const decisionMap = {
      allow: { label: "ALLOW", color: "#4CAF50" },
      modify: { label: "MODIFY", color: "#FFC300" },
      require_approval: { label: "MODIFY", color: "#FFC300" },
      block: { label: "BLOCK", color: "#FF5252" },
    };
    const decision = decisionMap[normalizedAction] || {
      label: String(action || "UNKNOWN").toUpperCase(),
      color: "#E8F4FD",
    };

    governanceDecisionElement.textContent = decision.label;
    governanceDecisionElement.style.color = decision.color;
  }

  function updateSatellitePositions() {
    const now = new Date();

    trackedObjects.forEach(({ entity, satrec }) => {
      const positionAndVelocity = satellite.propagate(satrec, now);
      const positionEci = positionAndVelocity.position;

      if (!positionEci) {
        entity.show = false;
        return;
      }

      const gmst = satellite.gstime(now);
      const geodetic = satellite.eciToGeodetic(positionEci, gmst);
      const longitude = satellite.degreesLong(geodetic.longitude);
      const latitude = satellite.degreesLat(geodetic.latitude);
      const altitudeMeters = geodetic.height * 1000;

      entity.position = Cesium.Cartesian3.fromDegrees(
        longitude,
        latitude,
        altitudeMeters,
      );
      entity.show = true;
    });
  }

  function setStatus(status, color) {
    systemStatusElement.textContent = status;
    systemStatusElement.style.color = color;
  }

  loadTleData();
  window.setInterval(updateSatellitePositions, UPDATE_INTERVAL_MS);
  window.setInterval(loadTleData, 10 * 60 * 1000);
})();
