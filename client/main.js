(function () {
  const API_URL = "http://localhost:8000/api/tle";
  const UPDATE_INTERVAL_MS = 2000;
  const LIGHT_BLUE = "#E8F4FD";
  const ALERT_RED = "#FF5C5C";

  const lastUpdatedElement = document.getElementById("lastUpdated");
  const objectCountElement = document.getElementById("objectCount");
  const systemStatusElement = document.getElementById("systemStatus");

  const trackedObjects = [];

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
            color: Cesium.Color.fromCssColorString(LIGHT_BLUE),
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
