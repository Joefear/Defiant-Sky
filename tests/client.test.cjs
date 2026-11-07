const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../client/main.js"), "utf8");

class Element {
  constructor() { this.textContent = ""; this.children = []; this.dataset = {}; this.events = {}; this.checked = false; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, callback) { this.events[name] = callback; }
}
const flush = () => new Promise((resolve) => setImmediate(resolve));
const finding = {
  finding_id: "f".repeat(64), catalog_id: 99999, name: "<script>unsafe</script>",
  severity: "REVIEW", quality: "COMPARABLE", status: "CANDIDATE ORBITAL-ELEMENT DIVERGENCE",
  reason: "Comparison", separation_km: 223.729, threshold_km: 10,
  comparison_epoch: "2026-11-06T00:00:00Z", evaluated_at: "2026-11-06T01:00:00Z",
  evidence: [], limitations: "Not proof of maneuver",
};
const snapshot = (demo, findings = []) => ({ mode: demo ? "demo" : "real", data_state: demo ? "SIMULATED DEMO" : "UNAVAILABLE", status: "INSUFFICIENT DATA", findings, tles: [], evaluated_at: "2026-11-06T01:00:00Z" });

function harness(fetcher) {
  const elements = new Map();
  const get = (id) => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const timers = new Map();
  let timerId = 0;
  const calls = [];
  const context = {
    location: { protocol: "http:" },
    document: { getElementById: get, createElement: () => new Element(), body: { classList: { toggle() {} } } },
    fetch: async (url, options) => { calls.push({ url, options }); return { ok: true, json: async () => fetcher(url, options) }; },
    setTimeout: (callback) => { timers.set(++timerId, callback); return timerId; },
    clearTimeout: (id) => timers.delete(id), setInterval: () => {},
    Date, console,
  };
  vm.runInNewContext(source, context);
  return { get, timers, calls };
}

test("real mode never schedules a staged finding or requests governance automatically", async () => {
  const app = harness((url) => url.endsWith("/tle") ? [] : snapshot(false));
  await flush();
  assert.equal(app.timers.size, 0);
  assert.equal(app.get("findings").children.length, 0);
  assert.match(app.get("summary").textContent, /INSUFFICIENT DATA/);
  assert.equal(app.calls.length, 2);
});

test("explicit demo renders synthetic finding and cannot authorize actions", async () => {
  const app = harness((url) => url.endsWith("/demo") ? snapshot(true, [finding]) : url.endsWith("/tle") ? [] : snapshot(false));
  await flush();
  app.get("demoSwitch").checked = true;
  await app.get("demoSwitch").events.change();
  assert.equal(app.get("dataState").textContent, "SIMULATED DEMO");
  for (const callback of app.timers.values()) callback();
  const card = app.get("findings").children[0];
  assert.equal(card.children[0].textContent, "<script>unsafe</script> · NORAD 99999");
  assert.equal(card.children.at(-1).disabled, true);
  assert.equal(app.calls.some(({ options }) => options.method === "POST"), false);
});

test("turning demo off cancels pending staged output and ignores stale callbacks", async () => {
  const app = harness((url) => url.endsWith("/demo") ? snapshot(true, [finding]) : url.endsWith("/tle") ? [] : snapshot(false));
  await flush();
  app.get("demoSwitch").checked = true;
  await app.get("demoSwitch").events.change();
  const staleCallback = [...app.timers.values()][0];
  app.get("demoSwitch").checked = false;
  await app.get("demoSwitch").events.change();
  staleCallback();
  assert.equal(app.timers.size, 0);
  assert.equal(app.get("findings").children.length, 0);
  assert.equal(app.get("dataState").textContent, "UNAVAILABLE");
});

test("late demo fetch cannot overwrite real mode", async () => {
  let resolveDemo;
  const pending = new Promise((resolve) => { resolveDemo = resolve; });
  const app = harness((url) => url.endsWith("/demo") ? pending : url.endsWith("/tle") ? [] : snapshot(false));
  await flush();
  app.get("demoSwitch").checked = true;
  const switching = app.get("demoSwitch").events.change();
  app.get("demoSwitch").checked = false;
  await app.get("demoSwitch").events.change();
  resolveDemo(snapshot(true, [finding]));
  await switching;
  assert.equal(app.timers.size, 0);
  assert.equal(app.get("dataState").textContent, "UNAVAILABLE");
});

test("failed API clears evidence and leaves actions blocked", async () => {
  const app = harness(() => { throw new Error("network failure"); });
  await flush();
  assert.equal(app.get("dataState").textContent, "UNAVAILABLE");
  assert.match(app.get("summary").textContent, /INSUFFICIENT DATA/);
  assert.match(app.get("governanceStatus").textContent, /blocked/);
});

test("real mode displays cohort, persisted schedule and quarantined objects", async () => {
  const report = { ...snapshot(false), catalog_ids: [12345, 99999],
    next_poll_at: "2026-11-06T02:18:00Z", quarantined_catalog_ids: [99999],
    refresh_status: "COOLDOWN", rejected_records: 1 };
  const app = harness((url) => url.endsWith("/tle") ? [] : report);
  await flush();
  assert.match(app.get("refreshStatus").textContent, /Cohort: 12345, 99999/);
  assert.match(app.get("refreshStatus").textContent, /Next retrieval: 2026-11-06T02:18:00Z/);
  assert.match(app.get("refreshStatus").textContent, /Quarantined: 99999/);
  assert.equal(app.timers.size, 0);
});

test("scheduling failure is distinct from unavailable history", async () => {
  const report = { ...snapshot(false, [finding]), data_state: "HISTORICAL COMPARISON",
    live_enabled: true, scheduling_error: true, storage_error: false,
    refresh_status: "SCHEDULING UNAVAILABLE", scheduler_retry_at: "2026-11-06T01:01:00Z" };
  const app = harness((url) => url.endsWith("/tle") ? [] : report);
  await flush();
  assert.equal(app.get("dataState").textContent, "HISTORICAL COMPARISON");
  assert.match(app.get("refreshStatus").textContent, /History: no integrity error/);
  assert.match(app.get("refreshStatus").textContent, /Next retrieval: blocked \(scheduling failure\)/);
  assert.match(app.get("refreshStatus").textContent, /Scheduler retry: 2026-11-06T01:01:00Z/);
  assert.doesNotMatch(app.get("refreshStatus").textContent, /disabled/);
  assert.equal(app.timers.size, 0);
});

test("late advisory result cannot overwrite a mode switch", async () => {
  let resolveReview;
  const pending = new Promise((resolve) => { resolveReview = resolve; });
  const app = harness((url) => url.endsWith("/evaluate") ? pending : url.endsWith("/demo") ? snapshot(true) : url.endsWith("/tle") ? [] : snapshot(false, [finding]));
  await flush();
  app.get("findings").children[0].children.at(-1).events.click();
  app.get("demoSwitch").checked = true;
  await app.get("demoSwitch").events.change();
  resolveReview({ action: "allow", status: "ADVISORY ONLY", summary: "old response" });
  await flush();
  assert.match(app.get("governanceStatus").textContent, /NOT EVALUATED/);
});
