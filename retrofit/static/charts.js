/* Public-building retrofit review — chart helpers.
 * Colors are the validated categorical slots (blue / orange / aqua) and the
 * fixed status palette. Light-mode aqua is sub-3:1 against the surface, so
 * charts using it always carry direct labels and a table view (relief rule).
 */

export const SERIES = ["#2a78d6", "#eb6834", "#1baf7a"];
export const STATUS = {
  priority: "#0ca30c",
  unproven: "#fab219",
  defer: "#ec835a",
  service_fail: "#d03b3b",
  incomparable: "#898781",
};

const PHASE_COLORS = {
  baseline: "#2a78d6",
  works: "#898781",
  commissioning: "#eda100",
  post: "#1baf7a",
};

export function statusOf(decision) {
  switch (decision) {
    case "priority":
      return "priority";
    case "conditional":
      return "priority";
    case "needs_measurement":
    case "not_started":
      return "unproven";
    case "defer":
      return "defer";
    case "service_fail":
      return "service_fail";
    case "incomparable":
      return "incomparable";
    default:
      return "unproven";
  }
}

export function pctFmt(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return `${Number(value).toFixed(digits)}%`;
}

export function numFmt(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(Number(value));
}

export function compactFmt(value) {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (Math.abs(n) >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (Math.abs(n) >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return numFmt(n);
}

export function buildScene({ axis = [], series = [] } = {}) {
  return {
    axis: axis.slice(),
    series: series.map((s, i) => ({ color: SERIES[i % SERIES.length], ...s })),
  };
}

const SVG_NS = "http://www.w3.org/2000/svg";

function el(name, attrs = {}, text = null) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== null && value !== undefined) node.setAttribute(key, String(value));
  }
  if (text !== null) node.textContent = text;
  return node;
}

function niceTicks(max, count = 4) {
  if (!(max > 0)) return [0];
  const raw = max / count;
  const power = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * power).find((s) => s >= raw) || raw;
  const ticks = [];
  for (let v = 0; v <= max + step * 0.001; v += step) ticks.push(v);
  return ticks;
}

/** Grouped comparison of baseline vs post for one building (kWh-e). */
export function energyComparisonSvg(assessment, width = 520, height = 210) {
  const svg = el("svg", {
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": "基线期与改造后稳定运行期能耗对比",
    class: "chart",
  });
  const base = assessment.baseline_energy?.kwh_e ?? null;
  const post = assessment.post_energy?.kwh_e ?? null;
  const left = 52;
  const right = width - 16;
  const top = 28;
  const bottom = height - 34;
  const values = [base, post].filter((v) => v !== null && v !== undefined);
  if (!values.length) {
    svg.appendChild(el("text", { x: width / 2, y: height / 2, "text-anchor": "middle", class: "chart-empty" }, "暂无可比较的能耗资料"));
    return svg;
  }
  const ticks = niceTicks(Math.max(...values));
  const maxTick = ticks[ticks.length - 1] || 1;
  const y = (v) => bottom - (v / maxTick) * (bottom - top);
  for (const tick of ticks) {
    svg.appendChild(el("line", { x1: left, x2: right, y1: y(tick), y2: y(tick), class: "grid" }));
    svg.appendChild(el("text", { x: left - 8, y: y(tick) + 4, "text-anchor": "end", class: "tick" }, compactFmt(tick)));
  }
  svg.appendChild(el("line", { x1: left, x2: right, y1: bottom, y2: bottom, class: "axis" }));
  const bars = [
    { label: "基线期", value: base, color: PHASE_COLORS.baseline, range: assessment.baseline },
    { label: "改造后", value: post, color: PHASE_COLORS.post, range: assessment.post },
  ];
  const slot = (right - left) / bars.length;
  const barW = Math.min(24, slot * 0.42);
  bars.forEach((bar, i) => {
    if (bar.value === null || bar.value === undefined) return;
    const cx = left + slot * (i + 0.5);
    const x = cx - barW / 2;
    const yy = y(bar.value);
    const h = bottom - yy;
    const r = Math.min(4, h);
    svg.appendChild(
      el("path", {
        d: `M${x},${bottom} L${x},${yy + r} Q${x},${yy} ${x + r},${yy} L${x + barW - r},${yy} Q${x + barW},${yy} ${x + barW},${yy + r} L${x + barW},${bottom} Z`,
        fill: bar.color,
      })
    );
    svg.appendChild(el("title", {}, `${bar.label}：${numFmt(bar.value)} kWh-e${bar.range ? `（${bar.range.start} 至 ${bar.range.end}，${bar.range.days} 天）` : ""}`));
    svg.appendChild(el("text", { x: cx, y: yy - 8, "text-anchor": "middle", class: "value-label" }, compactFmt(bar.value)));
    svg.appendChild(el("text", { x: cx, y: bottom + 18, "text-anchor": "middle", class: "axis-label" }, bar.label));
  });
  if (assessment.saving_pct !== null && assessment.saving_pct !== undefined) {
    const label = `实测节能 ${pctFmt(assessment.saving_pct)}`;
    svg.appendChild(el("text", { x: right, y: top - 8, "text-anchor": "end", class: "value-label" }, label));
  }
  return svg;
}

/** Horizontal bar of annualised saving, used for the investment ranking. */
export function savingRankSvg(items, width = 620) {
  const rows = items.filter((a) => a.annualised_saving_kwh_e !== null && a.annualised_saving_kwh_e !== undefined);
  const rowH = 30;
  const height = Math.max(rows.length * rowH + 30, 120);
  const svg = el("svg", {
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": "按年化节能量排序的投资优先级",
    class: "chart",
  });
  if (!rows.length) {
    svg.appendChild(el("text", { x: width / 2, y: height / 2, "text-anchor": "middle", class: "chart-empty" }, "暂无已核实节能量的项目"));
    return svg;
  }
  const left = 190;
  const right = width - 120;
  const max = Math.max(...rows.map((r) => Math.abs(r.annualised_saving_kwh_e)));
  rows.forEach((row, i) => {
    const value = row.annualised_saving_kwh_e;
    const yy = 14 + i * rowH;
    const barH = 14;
    const w = Math.max(2, (Math.abs(value) / max) * (right - left));
    const color = value > 0 ? PHASE_COLORS.post : STATUS.service_fail;
    svg.appendChild(el("text", { x: 0, y: yy + 11, class: "axis-label" }, `${row.name || row.project_id}`));
    svg.appendChild(el("rect", { x: left, y: yy, width: right - left, height: barH, rx: 2, class: "track" }));
    svg.appendChild(el("rect", { x: left, y: yy, width: w, height: barH, rx: 4, fill: color }));
    svg.appendChild(el("text", { x: left + w + 8, y: yy + 11, class: "value-label" }, `${compactFmt(value)} kWh-e`));
    svg.appendChild(el("title", {}, `${row.name || row.project_id} · ${row.decision_label} · 年化 ${numFmt(value)} kWh-e`));
  });
  return svg;
}

/** Comfort estimate with its confidence interval against a service target. */
export function comfortIntervalSvg(comfort, width = 620) {
  const height = 58;
  const svg = el("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "舒适度满意率区间与服务目标", class: "chart" });
  const left = 8;
  const right = width - 8;
  const y = 30;
  const x = (v) => left + (Math.min(Math.max(v, 0), 100) / 100) * (right - left);
  svg.appendChild(el("rect", { x: left, y: y - 2, width: right - left, height: 4, rx: 2, class: "track" }));
  if (comfort?.target !== null && comfort?.target !== undefined) {
    svg.appendChild(el("line", { x1: x(comfort.target), x2: x(comfort.target), y1: 12, y2: 44, class: "target-line" }));
    svg.appendChild(el("text", { x: x(comfort.target), y: 8, "text-anchor": "middle", class: "tick" }, `目标 ${pctFmt(comfort.target, 0)}`));
  }
  if (comfort?.interval_low !== null && comfort?.interval_low !== undefined) {
    const color = comfort.state === "fail" ? STATUS.service_fail : comfort.state === "pass" ? STATUS.priority : STATUS.unproven;
    svg.appendChild(el("line", { x1: x(comfort.interval_low), x2: x(comfort.interval_high), y1: y, y2: y, stroke: color, "stroke-width": 6, "stroke-linecap": "round" }));
    if (comfort.post_pct !== null && comfort.post_pct !== undefined) {
      svg.appendChild(el("circle", { cx: x(comfort.post_pct), cy: y, r: 6, fill: color, stroke: "var(--surface-1)", "stroke-width": 2 }));
    }
    svg.appendChild(el("text", { x: x(comfort.interval_low), y: 52, "text-anchor": "middle", class: "tick" }, pctFmt(comfort.interval_low, 0)));
    svg.appendChild(el("text", { x: x(comfort.interval_high), y: 52, "text-anchor": "middle", class: "tick" }, pctFmt(comfort.interval_high, 0)));
  } else {
    svg.appendChild(el("text", { x: width / 2, y: 34, "text-anchor": "middle", class: "chart-empty" }, "缺少可用的舒适度抽测"));
  }
  return svg;
}

/** Timeline of disjoint phases: baseline, works, commissioning, post. */
export function phaseTimelineSvg(assessment, width = 620) {
  const height = 104;
  const svg = el("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "基线与施工、投运、比较期时间轴", class: "chart" });
  const phases = [
    { key: "baseline", label: "基线期", window: assessment.baseline, color: PHASE_COLORS.baseline },
    { key: "works", label: "改造施工期", window: assessment.works, color: PHASE_COLORS.works },
    { key: "commissioning", label: "设备投运期", window: assessment.commissioning, color: PHASE_COLORS.commissioning },
    { key: "post", label: "改造后比较期", window: assessment.post, color: PHASE_COLORS.post },
  ].filter((p) => p.window);
  if (!phases.length) {
    svg.appendChild(el("text", { x: width / 2, y: height / 2, "text-anchor": "middle", class: "chart-empty" }, "尚无时间轴资料"));
    return svg;
  }
  const toDate = (s) => new Date(`${s}T00:00:00Z`).getTime();
  const min = Math.min(...phases.map((p) => toDate(p.window.start)));
  const max = Math.max(...phases.map((p) => toDate(p.window.end)));
  const span = Math.max(max - min, 1);
  const left = 8;
  const right = width - 8;
  const x = (t) => left + ((t - min) / span) * (right - left);
  const barY = 56;
  const barH = 16;
  const labelWidth = (text) => Array.from(text).reduce((sum, ch) => sum + (/[一-鿿]/.test(ch) ? 11.5 : 6.5), 0);

  // Stagger labels into two rows so short adjacent phases stay readable.
  const placed = [];
  for (const phase of phases) {
    const x1 = x(toDate(phase.window.start));
    const x2 = x(toDate(phase.window.end));
    const w = Math.max(2, x2 - x1);
    svg.appendChild(el("rect", { x: x1, y: barY, width: w, height: barH, rx: 3, fill: phase.color }));
    svg.appendChild(el("title", {}, `${phase.label}：${phase.window.start} 至 ${phase.window.end}（${phase.window.days} 天）`));
  }
  let row = 0;
  let rowEnds = [-Infinity, -Infinity];
  for (const phase of phases) {
    const x1 = x(toDate(phase.window.start));
    const x2 = x(toDate(phase.window.end));
    const text = labelWidth(phase.label);
    let centre = (x1 + x2) / 2;
    centre = Math.min(Math.max(centre, text / 2 + left), right - text / 2);
    let lane = rowEnds.findIndex((end) => centre - text / 2 > end + 6);
    if (lane === -1) lane = rowEnds[0] <= rowEnds[1] ? 0 : 1;
    const labelY = lane === 0 ? 34 : 18;
    const startTx = Math.min(Math.max(centre - text / 2, left), right - text);
    svg.appendChild(el("text", { x: startTx, y: labelY, class: "axis-label" }, phase.label));
    if (Math.abs(centre - (x1 + x2) / 2) > 1 || lane === 1) {
      svg.appendChild(el("line", { x1: x1 + 1, x2: x1 + 1, y1: labelY + 4, y2: barY, class: "axis" }));
    }
    rowEnds[lane] = startTx + text;
  }
  svg.appendChild(el("text", { x: left, y: height - 12, class: "tick" }, phases[0].window.start));
  svg.appendChild(el("text", { x: right, y: height - 12, "text-anchor": "end", class: "tick" }, phases[phases.length - 1].window.end));
  return svg;
}
