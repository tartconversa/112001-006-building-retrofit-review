import { test } from "node:test";
import assert from "node:assert/strict";

// Minimal DOM shim: enough for charts.js to build SVG trees without a browser.
class Node {
  constructor(name) {
    this.name = name;
    this.attrs = {};
    this.children = [];
    this._text = null;
  }
  setAttribute(key, value) {
    this.attrs[key] = String(value);
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  set textContent(value) {
    this._text = String(value);
  }
  get textContent() {
    return this._text;
  }
}

globalThis.document = {
  createElementNS: (_ns, name) => new Node(name),
  createElement: (name) => new Node(name),
};

const {
  statusOf,
  pctFmt,
  numFmt,
  buildScene,
  energyComparisonSvg,
  savingRankSvg,
  comfortIntervalSvg,
  phaseTimelineSvg,
  SERIES,
} = await import("../../retrofit/static/charts.js");

test("palette has eight fixed categorical slots", () => {
  assert.equal(SERIES.length, 3);
  assert.equal(new Set(SERIES).size, SERIES.length);
});

test("status mapping keeps service failure distinct from deferral", () => {
  assert.equal(statusOf("priority"), "priority");
  assert.equal(statusOf("conditional"), "priority");
  assert.equal(statusOf("needs_measurement"), "unproven");
  assert.equal(statusOf("defer"), "defer");
  assert.equal(statusOf("service_fail"), "service_fail");
  assert.equal(statusOf("incomparable"), "incomparable");
});

test("formatters handle missing values without NaN", () => {
  assert.equal(pctFmt(null), "—");
  assert.equal(pctFmt(87.55), "87.5%");
  assert.equal(numFmt(undefined), "—");
  assert.equal(numFmt(1234.4), "1,234");
});

test("buildScene assigns fixed colors in order", () => {
  const scene = buildScene({ axis: [1, 2], series: [{ name: "a" }, { name: "b" }] });
  assert.equal(scene.series[0].color, SERIES[0]);
  assert.equal(scene.series[1].color, SERIES[1]);
});

test("energy comparison draws two labelled bars and a saving label", () => {
  const svg = energyComparisonSvg({
    baseline_energy: { kwh_e: 10000 },
    post_energy: { kwh_e: 8000 },
    baseline: { start: "2025-01-01", end: "2025-03-31", days: 90 },
    post: { start: "2026-01-01", end: "2026-03-31", days: 90 },
    saving_pct: 20,
  });
  const texts = svg.children.filter((c) => c.name === "text").map((c) => c.textContent);
  assert.ok(texts.includes("基线期"));
  assert.ok(texts.includes("改造后"));
  assert.ok(texts.some((t) => t.includes("20.0%")));
  const bars = svg.children.filter((c) => c.name === "path");
  assert.equal(bars.length, 2);
});

test("energy comparison renders empty state instead of fake zero bars", () => {
  const svg = energyComparisonSvg({
    baseline_energy: { kwh_e: null },
    post_energy: { kwh_e: null },
  });
  assert.equal(svg.children.filter((c) => c.name === "path").length, 0);
  assert.ok(svg.children.some((c) => (c.textContent || "").includes("暂无可比较")));
});

test("comfort interval marks the service target and the interval", () => {
  const svg = comfortIntervalSvg({
    interval_low: 86,
    interval_high: 95,
    post_pct: 91,
    target: 85,
    state: "pass",
  });
  const texts = svg.children.filter((c) => c.name === "text").map((c) => c.textContent);
  assert.ok(texts.some((t) => t.includes("目标")));
  assert.ok(texts.includes("86%"));
  assert.ok(texts.includes("95%"));
  assert.equal(svg.children.filter((c) => c.name === "circle").length, 1);
});

test("comfort interval with no survey says so", () => {
  const svg = comfortIntervalSvg({});
  assert.ok(svg.children.some((c) => (c.textContent || "").includes("缺少")));
});

test("timeline separates works and commissioning windows", () => {
  const svg = phaseTimelineSvg({
    baseline: { start: "2024-07-01", end: "2024-09-29", days: 90 },
    works: { start: "2025-06-01", end: "2025-07-01", days: 30 },
    commissioning: { start: "2025-07-01", end: "2025-07-15", days: 14 },
    post: { start: "2025-07-15", end: "2025-10-13", days: 90 },
  });
  const labels = svg.children.filter((c) => c.name === "text" && c.attrs.class === "axis-label").map((c) => c.textContent);
  assert.deepEqual(labels, ["基线期", "改造施工期", "设备投运期", "改造后比较期"]);
  const titles = svg.children.filter((c) => c.name === "title").map((c) => c.textContent);
  assert.ok(titles.some((t) => t.startsWith("设备投运期")));
});

test("saving rank chart handles negative saving distinctly", () => {
  const svg = savingRankSvg([
    { name: "A", project_id: "A", annualised_saving_kwh_e: 5000, decision_label: "优先" },
    { name: "B", project_id: "B", annualised_saving_kwh_e: -1200, decision_label: "需补测" },
  ]);
  const rects = svg.children.filter((c) => c.name === "rect" && c.attrs.fill);
  assert.equal(rects.length, 2);
  assert.notEqual(rects[0].attrs.fill, rects[1].attrs.fill);
});
