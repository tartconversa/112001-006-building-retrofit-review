// DOM-level smoke test: loads the real page markup, runs app.js against it with
// fetch stubbed to a representative analysis payload. Catches render-time
// exceptions and verifies the service-gate text reaches the screen.
//
// Requires jsdom (a dev-only dependency; the app itself has none):
//   npm install --no-save jsdom
// The suite skips itself when jsdom is unavailable.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const staticDir = join(here, "..", "..", "retrofit", "static");
const html = readFileSync(join(staticDir, "index.html"), "utf-8");
const appJs = readFileSync(join(staticDir, "app.js"), "utf-8")
  .replace('from "./charts.js"', 'from "/charts.js"');

let JSDOM = null;
try {
  ({ JSDOM } = await import("jsdom"));
} catch {
  // jsdom is optional tooling, not an application dependency.
}
const requiresJsdom = JSDOM ? test : test.skip;

function assessment(overrides) {
  return {
    project_id: "PR01",
    building_id: "BL01",
    name: "照明与空调改造",
    measures: "LED",
    phase: "completed",
    decision: "priority",
    decision_label: "优先",
    reasons: ["verified_saving"],
    explanation: "实测节能 20.7%；舒适度抽测 110 人、满意率下界 88% ≥ 目标 85%。",
    works: { start: "2025-06-01", end: "2025-07-01", days: 30, label: "改造施工期" },
    commissioning: { start: "2025-07-01", end: "2025-07-15", days: 14, label: "设备投运/调试期" },
    post: { start: "2025-07-15", end: "2025-10-13", days: 90, label: "改造后稳定运行期" },
    baseline: { start: "2024-07-15", end: "2024-10-13", days: 90, label: "上年同期基线" },
    baseline_kind: "same_period",
    baseline_energy: { kwh_e: 52000, electric_kwh: 50000, gas_m3: 200, degree_days: 2400, meter_ids: ["M1"] },
    post_energy: { kwh_e: 41000, electric_kwh: 39000, gas_m3: 200, degree_days: 2300, meter_ids: ["M1"] },
    saving_pct: 20.7,
    raw_saving_pct: 20.7,
    weather_adjustment_pct: null,
    schedule_adjustment_pct: null,
    annualised_saving_kwh_e: 44600,
    comfort: {
      post_pct: 93,
      interval_low: 88,
      interval_high: 96,
      sample_count: 110,
      target: 85,
      target_use_type: "library",
      state: "pass",
      samples_sufficient: true,
      records: ["C02"],
      complaints: 1,
      notes: [],
    },
    confounders: [],
    closure: null,
    outstanding: [],
    evidence_score: 0.95,
    rank_score: 980,
    is_completed: true,
    predicted_saving_pct: null,
    cost: null,
    status: "completed",
    approval: {},
    verification_measurements: [],
    precedent: null,
    ...overrides,
  };
}

function payload() {
  const projects = [
    assessment(),
    assessment({
      project_id: "PR02",
      building_id: "BL02",
      name: "门诊区热回收",
      decision: "service_fail",
      decision_label: "服务不达标",
      reasons: ["comfort_below_target"],
      explanation: "改造后舒适度满意率区间 64%–79%，上界仍低于目标 90%。实测节能 +11%，但服务未达标不能由节能抵消。",
      saving_pct: 11,
      comfort: {
        post_pct: 72,
        interval_low: 64,
        interval_high: 79,
        sample_count: 88,
        target: 90,
        target_use_type: "clinic",
        state: "fail",
        samples_sufficient: true,
        records: ["C04"],
        complaints: 14,
        notes: [],
      },
      annualised_saving_kwh_e: 3580,
    }),
    assessment({
      project_id: "PR07",
      building_id: "BL04",
      name: "屋顶光伏与储能",
      phase: "proposed",
      decision: "conditional",
      decision_label: "有条件优先",
      reasons: ["pipeline_candidate", "awaiting_implementation"],
      explanation: "下一年度候选措施第 1 位。预计节能 18.0%；完工验收后须用实测复核。",
      is_completed: false,
      predicted_saving_pct: 18,
      saving_pct: null,
      comfort: null,
      post: null,
      baseline_kind: "preceding",
      closure: {
        days: 8,
        share_of_works_pct: 14,
        open_days_lost: 6.8,
        open_hours_lost: 68,
        conflicts_open_target: false,
        registerable: true,
        note: "预计停用 8 天。",
      },
      annualised_saving_kwh_e: 4580,
      cost: 980000,
    }),
  ];
  return {
    analysis: {
      as_of: "2026-11-20",
      config: {
        baseline_mode: "same_period_last_year",
        comparison_days: 90,
        min_energy_coverage_pct: 95,
        min_post_days: 60,
        gas_kwh_per_m3: 9.9,
        min_comfort_samples: 30,
        min_comfort_coverage_pct: 15,
        comfort_confidence: 0.9,
        meaningful_saving_pct: 5,
        schedule_tolerance_pct: 5,
        weather_sensitivity_pct: 15,
        max_closure_share_pct: 25,
      },
      summary: {
        buildings: 4,
        projects: 3,
        completed_projects: 2,
        candidate_projects: 1,
        decision_counts: { priority: 1, service_fail: 1, conditional: 1 },
        verified_annual_saving_kwh_e: 44600,
        completed_ranked: ["PR01", "PR02"],
        candidates_ranked: ["PR07"],
        held: [],
        input_errors: 0,
        input_warnings: 0,
      },
      data_issues: [],
      warnings: [],
      projects,
      completed_ranking: ["PR01"],
      candidate_ranking: ["PR07"],
    },
    approvals: {},
    approval_log: { approvals: [], log: [] },
    reaudit: { affected: [], rankings: {}, summary: {}, data_issues: [] },
    records: {},
    building_options: [{ building_id: "BL01", use_type: "library" }],
  };
}

function boot() {
  const dom = new JSDOM(html, { url: "http://localhost/", runScripts: "outside-only", pretendToBeVisual: true });
  const { window } = dom;
  window.fetch = async (url) => {
    if (String(url).includes("/api/analysis")) {
      return { ok: true, status: 200, json: async () => payload() };
    }
    return { ok: true, status: 200, json: async () => ({ payload: payload() }) };
  };
  window.localStorage.setItem("retrofit.actor", "测试员");
  const errors = [];
  window.addEventListener("error", (event) => errors.push(event.message));
  const module = new window.Function(appJsNoImports());
  module.call(window);
  return { dom, window, errors };
}

function appJsNoImports() {
  // charts.js is pure functions; inline it so the test needs no module loader.
  const charts = readFileSync(join(staticDir, "charts.js"), "utf-8")
    .replace(/^export /gm, "")
    .replace(/^const SVG_NS/m, "const SVG_NS");
  const body = appJs.replace(/^import \{[\s\S]*?\} from "\/charts\.js";\n/, "");
  return `${charts}\n${body}`;
}

async function settle(window) {
  await new Promise((resolve) => setTimeout(resolve, 30));
  await window.flushMicrotasks?.();
}

requiresJsdom("overview renders service-failure evidence without hiding the energy number", async () => {
  const { window, errors } = boot();
  await settle(window);
  const text = window.document.body.textContent;
  assert.deepEqual(errors, []);
  assert.ok(text.includes("公共建筑改造复核"));
  assert.ok(text.includes("改造后舒适度满意率区间 64%–79%"));
  assert.ok(text.includes("服务未达标不能由节能抵消"));
  assert.ok(text.includes("已核实年化节能量"));
});

requiresJsdom("ranking tab lists completed and candidate measures separately", async () => {
  const { window } = boot();
  await settle(window);
  const tabs = Array.from(window.document.querySelectorAll(".tabs button"));
  const ranking = tabs.find((b) => b.textContent === "投资排序");
  ranking.dispatchEvent(new window.Event("click"));
  await settle(window);
  const text = window.document.body.textContent;
  assert.ok(text.includes("已完成项目（按实测表现）"));
  assert.ok(text.includes("下一年度候选措施（预计值 + 约束条件）"));
  assert.ok(text.includes("屋顶光伏与储能"));
});

requiresJsdom("clicking a project opens the basis drawer with separate phases", async () => {
  const { window } = boot();
  await settle(window);
  const button = Array.from(window.document.querySelectorAll("button")).find((b) => b.textContent === "查看完整依据");
  button.dispatchEvent(new window.Event("click"));
  await settle(window);
  const drawer = window.document.querySelector(".drawer");
  assert.ok(drawer);
  const text = drawer.textContent;
  assert.ok(text.includes("改造施工期"));
  assert.ok(text.includes("设备投运期"));
  assert.ok(text.includes("改造后比较期"));
  assert.ok(text.includes("批准时的依据") === false);
  assert.ok(text.includes("舒适度与服务保障"));
});
