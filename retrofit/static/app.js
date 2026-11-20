import {
  statusOf,
  pctFmt,
  numFmt,
  compactFmt,
  energyComparisonSvg,
  savingRankSvg,
  comfortIntervalSvg,
  phaseTimelineSvg,
} from "./charts.js";

const state = {
  payload: null,
  tab: "overview",
  selected: null,
  actor: localStorage.getItem("retrofit.actor") || "资产管理员",
};

const DECISION_HELP = {
  priority: "已完成、实测节能成立且服务达标，列入优先投资排序。",
  conditional: "已登记拟改造措施，等待实施与实测复核；按预计节能量与本地业绩排序。",
  needs_measurement: "证据不足或存在未消除的混杂因素，需补测/补录后再排序。",
  defer: "节能不成立、低于门槛或与开放服务冲突，暂缓安排资金。",
  service_fail: "改造后舒适度未达标，节能不能抵消服务缺口，先整改。",
  incomparable: "用途或开放基准改变，前后不可比，不得计为设备效率提升。",
  not_started: "尚未进入评估。",
};

const REASON_TEXT = {
  verified_saving: "实测节能成立",
  pipeline_candidate: "下一年度候选措施",
  awaiting_implementation: "待实施与实测复核",
  comfort_below_target: "舒适度低于目标",
  comfort_evidence_insufficient: "舒适度证据不足",
  baseline_comfort_unproven: "立项前舒适度基线不足",
  baseline_comfort_below_target: "立项前舒适度已低于目标",
  use_type_change: "用途变更",
  energy_data_gap: "能耗资料缺口",
  post_period_too_short: "改造后稳定期不足",
  no_energy_comparison: "无可配对能耗",
  negative_saving: "实测能耗上升",
  saving_below_threshold: "节能低于门槛",
  predicted_saving_below_threshold: "预计节能低于门槛",
  no_predicted_saving: "未登记预计节能",
  closure_conflict: "停用时段冲突",
  open_service_below_target: "开放服务低于目标",
  service_mitigation_required: "需先保障开放服务",
  meter_replacement: "电表更换未核对",
  tenant_change: "租户变化未核实",
  use_change_event: "登记了用途变化",
  other_commissioning: "其他设备投运",
  weather_difference: "气象差异已修正",
};

const CONFOUNDER_TEXT = {
  schedule_change: "开放时间变化",
  open_days_change: "开放天数变化",
  weather_difference: "气象差异",
  weather_not_normalised: "气象未修正",
  meter_replacement: "电表更换",
  tenant_change: "租户变化",
  use_change_event: "用途变化",
  other_commissioning: "其他设备投运",
  other_event: "其他现场事件",
};

const STATUS_LABEL = {
  priority: "可优先",
  unproven: "需补测",
  defer: "暂缓",
  service_fail: "服务不达标",
  incomparable: "不可比",
};

// ---------------------------------------------------------------------------
// utilities
// ---------------------------------------------------------------------------

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === "string" || typeof child === "number" ? document.createTextNode(String(child)) : child);
  }
  return node;
}

function badge(decision) {
  const status = statusOf(decision);
  return h(
    "span",
    { class: `badge ${status}` },
    h("span", { class: "dot" }),
    DECISION_LABEL[decision] || DECISION_LABEL.unproven
  );
}

const DECISION_LABEL = {
  priority: "优先",
  conditional: "有条件优先",
  needs_measurement: "需补测",
  defer: "暂缓",
  service_fail: "服务不达标",
  incomparable: "不可比",
  not_started: "未评估",
};

function toast(message, isError = false) {
  $$(".toast").forEach((n) => n.remove());
  const node = h("div", { class: `toast${isError ? " error" : ""}`, role: "status" }, message);
  document.body.appendChild(node);
  setTimeout(() => node.remove(), 5200);
}

async function api(path, method = "GET", body = null) {
  const options = { method, headers: {} };
  if (body !== null) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    throw new Error(payload?.error || `请求失败 (${response.status})`);
  }
  return payload;
}

function applyPayload(payload) {
  if (!payload) return;
  if (payload.payload) payload = payload.payload;
  state.payload = payload;
  render();
}

async function mutate(path, body, successMessage) {
  try {
    const result = await api(path, "POST", body);
    if (result?.payload) state.payload = result.payload;
    render();
    if (successMessage) toast(successMessage);
    return result;
  } catch (error) {
    toast(error.message, true);
    return null;
  }
}

function pctClass(value) {
  return value >= 0 ? "good" : "bad";
}

function decisionOrder(a) {
  const order = { priority: 0, conditional: 0, needs_measurement: 1, defer: 2, service_fail: 3, incomparable: 4 };
  return order[a.decision] ?? 5;
}

// ---------------------------------------------------------------------------
// render: shell
// ---------------------------------------------------------------------------

const TABS = [
  ["overview", "总览"],
  ["ranking", "投资排序"],
  ["projects", "项目与解释"],
  ["register", "登记措施"],
  ["verify", "验收与复核"],
  ["data", "资料与更正"],
  ["audit", "审批与留痕"],
  ["method", "口径说明"],
];

function render() {
  const root = $("#app");
  root.replaceChildren();
  if (!state.payload) {
    root.appendChild(h("p", { class: "meta" }, "正在载入…"));
    return;
  }
  const analysis = state.payload.analysis;
  root.appendChild(renderHeader(analysis));
  const tabs = h("nav", { class: "tabs", role: "tablist" });
  for (const [key, label] of TABS) {
    tabs.appendChild(
      h(
        "button",
        {
          role: "tab",
          "aria-selected": state.tab === key ? "true" : "false",
          onclick: () => {
            state.tab = key;
            render();
            window.scrollTo({ top: 0 });
          },
        },
        label
      )
    );
  }
  root.appendChild(tabs);
  const view = h("main");
  switch (state.tab) {
    case "overview":
      view.appendChild(renderOverview(analysis));
      break;
    case "ranking":
      view.appendChild(renderRanking(analysis));
      break;
    case "projects":
      view.appendChild(renderProjects(analysis));
      break;
    case "register":
      view.appendChild(renderRegister(analysis));
      break;
    case "verify":
      view.appendChild(renderVerify(analysis));
      break;
    case "data":
      view.appendChild(renderData(analysis));
      break;
    case "audit":
      view.appendChild(renderAudit(analysis));
      break;
    case "method":
      view.appendChild(renderMethod(analysis));
      break;
  }
  root.appendChild(view);
  if (state.selected) root.appendChild(renderDrawer(analysis, state.selected));
}

function renderHeader(analysis) {
  const header = h("header", { class: "top" });
  header.appendChild(
    h(
      "div",
      {},
      h("h1", {}, "公共建筑改造复核"),
      h(
        "p",
        {},
        "按可比口径复核已完成改造的实际表现，并在服务保障前提下安排下一年度资金。施工期与设备投运期分开核算，用途或开放时间变化不计为设备效率。"
      )
    )
  );
  const search = h("input", {
    type: "search",
    placeholder: "搜索项目编号 / 建筑",
    "aria-label": "搜索项目",
    value: state.search || "",
    oninput: (event) => {
      state.search = event.target.value.trim();
      state.tab = "projects";
      render();
      // render() rebuilds the header; keep the caret in the search box.
      const next = $('input[type="search"]');
      if (next) {
        next.focus();
        next.setSelectionRange(next.value.length, next.value.length);
      }
    },
  });
  const toolbar = h(
    "div",
    { class: "toolbar" },
    h(
      "label",
      { class: "meta", style: "display:flex;gap:6px;align-items:center" },
      "评估日期",
      h("input", {
        type: "date",
        value: analysis.as_of,
        onchange: (event) => mutate("/api/as-of", { as_of: event.target.value }, "评估日期已更新"),
      })
    ),
    h(
      "label",
      { class: "meta", style: "display:flex;gap:6px;align-items:center" },
      "操作人",
      h("input", {
        type: "text",
        value: state.actor,
        style: "width:110px",
        onchange: (event) => {
          state.actor = event.target.value.trim() || "资产管理员";
          localStorage.setItem("retrofit.actor", state.actor);
        },
      })
    ),
    search,
    h(
      "button",
      {
        class: "primary",
        onclick: () => {
          state.tab = "register";
          render();
        },
      },
      "登记拟改造措施"
    )
  );
  header.appendChild(toolbar);
  return header;
}

// ---------------------------------------------------------------------------
// overview
// ---------------------------------------------------------------------------

function renderOverview(analysis) {
  const panel = h("section", { class: "panel" });
  const summary = analysis.summary;
  const counts = summary.decision_counts || {};
  const verifiedProjects = analysis.projects.filter((p) => p.decision === "priority");
  const comfortFail = analysis.projects.filter((p) => p.decision === "service_fail");
  const blocked = analysis.projects.filter((p) => p.decision === "needs_measurement");

  panel.appendChild(h("h2", {}, "本年度的投资判断依据"));
  panel.appendChild(
    h(
      "p",
      { class: "sub" },
      `评估基准日 ${analysis.as_of}。已完成项目按实测表现排序；服务达标是进入优先排序的前提，节能不能抵消舒适度缺口。`
    )
  );

  const stats = h("div", { class: "grid three" });
  stats.appendChild(
    statTile(
      "已核实年化节能量",
      `${compactFmt(summary.verified_annual_saving_kwh_e)} kWh-e`,
      `${verifiedProjects.length} 个已完成项目通过服务与证据门槛`
    )
  );
  stats.appendChild(statTile("已完成项目", String(summary.completed_projects), `其中需补测 ${counts.needs_measurement || 0} 个`));
  stats.appendChild(statTile("下一年度候选", String(summary.candidate_projects), `可排序 ${summary.candidates_ranked.length} 个`));
  stats.appendChild(statTile("服务不达标", String(comfortFail.length), "不进入优先排序，先整改"));
  stats.appendChild(statTile("待补测/待核实", String(blocked.length), "资料或抽测不足"));
  stats.appendChild(statTile("资料问题", String(summary.input_errors + summary.input_warnings), `错误 ${summary.input_errors} · 提醒 ${summary.input_warnings}`));
  panel.appendChild(stats);

  if (analysis.warnings.length) {
    const notice = h("div", { class: "notice" });
    for (const warning of analysis.warnings) notice.appendChild(h("div", {}, warning));
    panel.appendChild(notice);
  }

  panel.appendChild(h("h3", {}, "已完成项目：实测节能与用能变化"));
  panel.appendChild(
    h(
      "p",
      { class: "sub" },
      "柱形为可比口径下的期间用能（电 + 燃气按 9.9 kWh/m³ 折算）。开放时间与采暖度日差异已单独说明。"
    )
  );
  panel.appendChild(
    h(
      "div",
      { class: "legend" },
      h("span", {}, h("i", { style: "background:var(--series-1)" }), "基线期"),
      h("span", {}, h("i", { style: "background:var(--series-3)" }), "改造后稳定运行期")
    )
  );
  const chartWrap = h("div", { class: "grid two" });
  const completed = analysis.projects.filter((p) => p.is_completed);
  if (!completed.length) {
    chartWrap.appendChild(h("div", { class: "notice" }, "暂无已完成项目。可在“登记措施”中录入拟改造措施，或导入验收资料。"));
  }
  for (const project of completed.slice(0, 6)) {
    const box = h("div", { class: "card" });
    box.appendChild(
      h(
        "div",
        { class: "card-head" },
        h("div", {}, h("h3", {}, project.name), h("div", { class: "meta" }, `${project.building_id} · ${project.project_id}`)),
        badge(project.decision)
      )
    );
    box.appendChild(energyComparisonSvg(project));
    box.appendChild(phaseTimelineSvg(project));
    box.appendChild(
      h(
        "p",
        { class: "reason" },
        h("strong", {}, "结论："),
        project.explanation
      )
    );
    box.appendChild(h("button", { onclick: () => openProject(project.project_id) }, "查看完整依据"));
    chartWrap.appendChild(box);
  }
  panel.appendChild(chartWrap);
  return panel;
}

function statTile(label, value, hint) {
  return h("div", { class: "stat" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), hint ? h("div", { class: "hint" }, hint) : null);
}

// ---------------------------------------------------------------------------
// ranking
// ---------------------------------------------------------------------------

function renderRanking(analysis) {
  const panel = h("section", { class: "panel" });
  panel.appendChild(h("h2", {}, "下一年度资金排序"));
  panel.appendChild(
    h(
      "p",
      { class: "sub" },
      "已完成项目提供实际业绩；候选措施按预计节能量、本地业绩参照和投资强度排序。两者分开列出，避免把预计值当成实测结果。"
    )
  );

  panel.appendChild(h("h3", {}, "一、已完成项目（按实测表现）"));
  const completed = analysis.projects.filter((p) => p.is_completed);
  if (!completed.length) {
    panel.appendChild(h("div", { class: "notice" }, "尚无已完成项目。"));
  } else {
    panel.appendChild(savingRankSvg(analysis.projects.filter((p) => p.is_completed && p.decision === "priority")));
    const table = h("div", { class: "table-wrap" });
    table.appendChild(completedTable(analysis));
    panel.appendChild(table);
  }

  panel.appendChild(h("h3", {}, "二、下一年度候选措施（预计值 + 约束条件）"));
  const candidates = analysis.projects.filter((p) => !p.is_completed);
  if (!candidates.length) {
    panel.appendChild(h("div", { class: "notice" }, "尚未登记候选措施。可在“登记措施”页录入措施、预计停用时段和预计节能率。"));
  } else {
    const table = h("div", { class: "table-wrap" });
    table.appendChild(candidateTable(analysis));
    panel.appendChild(table);
  }

  panel.appendChild(h("h3", {}, "三、暂缓与需补测"));
  const held = analysis.projects.filter((p) => ["defer", "needs_measurement", "service_fail", "incomparable"].includes(p.decision));
  if (!held.length) {
    panel.appendChild(h("div", { class: "notice good" }, "当前没有暂缓或待补测项目。"));
  } else {
    for (const project of held) {
      panel.appendChild(projectCard(project));
    }
  }
  return panel;
}

function completedTable(analysis) {
  const table = h("table");
  table.appendChild(
    h(
      "thead",
      {},
      h(
        "tr",
        {},
        h("th", {}, "排序"),
        h("th", {}, "项目"),
        h("th", {}, "建筑 / 用途"),
        h("th", { class: "num" }, "实测节能"),
        h("th", { class: "num" }, "年化 kWh-e"),
        h("th", { class: "num" }, "舒适度区间"),
        h("th", { class: "num" }, "目标"),
        h("th", {}, "结论"),
        h("th", {}, "")
      )
    )
  );
  const body = h("tbody");
  const ranked = analysis.projects
    .filter((p) => p.is_completed)
    .sort((a, b) => {
      const ao = decisionOrder(a);
      const bo = decisionOrder(b);
      if (ao !== bo) return ao - bo;
      return (b.rank_score || 0) - (a.rank_score || 0);
    });
  let rank = 0;
  for (const project of ranked) {
    const rankable = ["priority", "needs_measurement", "defer"].includes(project.decision);
    if (rankable) rank += 1;
    const comfort = project.comfort || {};
    body.appendChild(
      h(
        "tr",
        {},
        h("td", {}, rankable ? String(rank) : "—"),
        h("td", {}, h("button", { class: "linklike", onclick: () => openProject(project.project_id) }, project.name), h("div", { class: "meta" }, project.project_id)),
        h("td", {}, project.building_id),
        h("td", { class: "num" }, pctFmt(project.saving_pct)),
        h("td", { class: "num" }, numFmt(project.annualised_saving_kwh_e)),
        h("td", { class: "num" }, comfort.interval_low != null ? `${pctFmt(comfort.interval_low, 0)}–${pctFmt(comfort.interval_high, 0)}` : "—"),
        h("td", { class: "num" }, pctFmt(comfort.target, 0)),
        h("td", {}, badge(project.decision)),
        h("td", {}, h("button", { onclick: () => openProject(project.project_id) }, "依据"))
      )
    );
  }
  table.appendChild(body);
  return table;
}

function candidateTable(analysis) {
  const table = h("table");
  table.appendChild(
    h(
      "thead",
      {},
      h(
        "tr",
        {},
        h("th", {}, "排序"),
        h("th", {}, "措施"),
        h("th", {}, "建筑"),
        h("th", { class: "num" }, "预计节能"),
        h("th", { class: "num" }, "年化 kWh-e"),
        h("th", { class: "num" }, "预计停用"),
        h("th", {}, "开放服务"),
        h("th", {}, "结论"),
        h("th", {}, "")
      )
    )
  );
  const body = h("tbody");
  const sorted = analysis.projects
    .filter((p) => !p.is_completed)
    .sort((a, b) => {
      const ao = decisionOrder(a);
      const bo = decisionOrder(b);
      if (ao !== bo) return ao - bo;
      return (b.rank_score || 0) - (a.rank_score || 0);
    });
  let rank = 0;
  for (const project of sorted) {
    const rankable = ["conditional", "priority"].includes(project.decision);
    if (rankable) rank += 1;
    const closure = project.closure;
    body.appendChild(
      h(
        "tr",
        {},
        h("td", {}, rankable ? String(rank) : "—"),
        h("td", {}, h("strong", {}, project.name), h("div", { class: "meta" }, project.measures || project.project_id)),
        h("td", {}, project.building_id),
        h("td", { class: "num" }, pctFmt(project.predicted_saving_pct)),
        h("td", { class: "num" }, numFmt(project.annualised_saving_kwh_e)),
        h("td", { class: "num" }, closure ? `${closure.days} 天` : "—"),
        h("td", {}, closure ? (closure.conflicts_open_target ? "与开放目标冲突" : closure.note.includes("超过") ? "需调整施工组织" : "可临时安排") : "未登记"),
        h("td", {}, badge(project.decision)),
        h("td", {}, h("button", { onclick: () => openProject(project.project_id) }, "依据"))
      )
    );
  }
  table.appendChild(body);
  return table;
}

function projectCard(project) {
  const card = h("div", { class: "card" });
  card.appendChild(
    h(
      "div",
      { class: "card-head" },
      h(
        "div",
        {},
        h("h3", {}, project.name),
        h("div", { class: "meta" }, `${project.building_id} · ${project.project_id} · ${project.phase}`)
      ),
      badge(project.decision)
    )
  );
  card.appendChild(h("p", { class: "reason" }, h("strong", {}, "理由："), project.explanation));
  if (project.reasons?.length) {
    const chips = h("div", { class: "chips" });
    for (const reason of project.reasons) {
      chips.appendChild(h("span", { class: "chip" }, REASON_TEXT[reason] || reason));
    }
    card.appendChild(chips);
  }
  if (project.outstanding?.length) {
    const list = h("ul", { class: "list" });
    for (const item of project.outstanding) list.appendChild(h("li", {}, item));
    card.appendChild(h("h3", {}, "未完成事项"));
    card.appendChild(list);
  }
  if (!project.is_completed && ["conditional", "priority"].includes(project.decision)) {
    card.appendChild(approveControl(project));
  }
  card.appendChild(h("button", { onclick: () => openProject(project.project_id) }, "查看完整证据"));
  return card;
}

function approveControl(project) {
  const note = h("input", { type: "text", placeholder: "批准说明（留痕）", style: "min-width:200px;flex:1" });
  return h(
    "div",
    { style: "display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;align-items:center" },
    note,
    h(
      "button",
      {
        class: "primary",
        onclick: async () => {
          await mutate(
            "/api/approve",
            { project_id: project.project_id, actor: state.actor, note: note.value || "批准立项" },
            `已批准 ${project.project_id}，依据已冻结留痕`
          );
        },
      },
      "批准并冻结依据"
    ),
    h("span", { class: "meta" }, "批准后可将项目移交设施团队")
  );
}

// ---------------------------------------------------------------------------
// projects list
// ---------------------------------------------------------------------------

function renderProjects(analysis) {
  const panel = h("section", { class: "panel" });
  panel.appendChild(h("h2", {}, "项目与解释"));
  panel.appendChild(
    h(
      "p",
      { class: "sub" },
      "每个项目都给出结论、理由代码和完整说明。可据此向决策者解释为何优先、暂缓或需补测。"
    )
  );
  const query = (state.search || "").toLowerCase();
  const projects = analysis.projects
    .filter((p) => !query || p.project_id.toLowerCase().includes(query) || p.name.toLowerCase().includes(query) || p.building_id.toLowerCase().includes(query))
    .sort((a, b) => decisionOrder(a) - decisionOrder(b) || (b.rank_score || 0) - (a.rank_score || 0));
  if (!projects.length) {
    panel.appendChild(h("div", { class: "notice" }, "没有匹配的项目。"));
    return panel;
  }
  for (const project of projects) panel.appendChild(projectCard(project));
  return panel;
}

// ---------------------------------------------------------------------------
// detail drawer
// ---------------------------------------------------------------------------

function openProject(projectId) {
  state.selected = projectId;
  render();
}

function closeProject() {
  state.selected = null;
  render();
}

function renderDrawer(analysis, projectId) {
  const project = analysis.projects.find((p) => p.project_id === projectId);
  if (!project) return h("div");
  const overlay = h("div", { class: "drawer", role: "dialog", "aria-modal": "true", "aria-label": `${project.name} 评估依据` });
  const inner = h("div", { class: "drawer-inner" });
  inner.appendChild(
    h(
      "div",
      { class: "drawer-head" },
      h("div", {}, h("h2", {}, project.name), h("div", { class: "meta" }, `${project.building_id} · ${project.project_id}`)),
      h("button", { onclick: closeProject, "aria-label": "关闭" }, "关闭")
    )
  );
  inner.appendChild(h("div", {}, badge(project.decision), " ", h("span", { class: "meta" }, DECISION_HELP[project.decision] || "")));

  inner.appendChild(h("p", { class: "reason", style: "margin-top:12px" }, project.explanation));

  inner.appendChild(h("h3", {}, "比较期（施工期与设备投运期分开）"));
  inner.appendChild(phaseTimelineSvg(project, 600));
  const kv = h("dl", { class: "kv" });
  const addKv = (label, value) => {
    kv.appendChild(h("dt", {}, label));
    kv.appendChild(h("dd", {}, value));
  };
  addKv("基线期", project.baseline ? `${project.baseline.start} 至 ${project.baseline.end}（${project.baseline.days} 天）` : "—");
  addKv("基线类型", project.baseline_kind === "same_period" ? "上年同期" : "改造前相邻期");
  addKv("改造施工期", project.works ? `${project.works.start} 至 ${project.works.end}（${project.works.days} 天）` : "—");
  addKv("设备投运期", project.commissioning ? `${project.commissioning.start} 至 ${project.commissioning.end}（${project.commissioning.days} 天）` : "未登记（施工结束后直接起算比较期）");
  addKv("改造后比较期", project.post ? `${project.post.start} 至 ${project.post.end}（${project.post.days} 天）` : "—");
  inner.appendChild(kv);

  inner.appendChild(h("h3", {}, "能耗对比"));
  inner.appendChild(energyComparisonSvg(project, 600));
  const energyKv = h("dl", { class: "kv" });
  const addEnergy = (label, value) => {
    energyKv.appendChild(h("dt", {}, label));
    energyKv.appendChild(h("dd", {}, value));
  };
  const base = project.baseline_energy;
  const post = project.post_energy;
  addEnergy("基线期用能", base ? `${numFmt(base.electric_kwh)} kWh 电 + ${numFmt(base.gas_m3)} m³ 气 = ${numFmt(base.kwh_e)} kWh-e` : "—");
  addEnergy("改造后用能", post ? `${numFmt(post.electric_kwh)} kWh 电 + ${numFmt(post.gas_m3)} m³ 气 = ${numFmt(post.kwh_e)} kWh-e` : "—");
  addEnergy("原始变化", pctFmt(project.raw_saving_pct));
  addEnergy("开放时间折算", project.schedule_adjustment_pct != null ? pctFmt(project.schedule_adjustment_pct) : "无差异");
  addEnergy("气象修正", project.weather_adjustment_pct != null ? `${pctFmt(project.weather_adjustment_pct)}（按采暖度日比）` : "未修正（度日缺失或一致）");
  addEnergy("可比节能", pctFmt(project.saving_pct));
  addEnergy("年化节能量", project.annualised_saving_kwh_e != null ? `${numFmt(project.annualised_saving_kwh_e)} kWh-e` : "—");
  inner.appendChild(energyKv);
  if (base?.meter_ids?.length || post?.meter_ids?.length) {
    const meters = new Set([...(base?.meter_ids || []), ...(post?.meter_ids || [])]);
    if (meters.size > 1) {
      inner.appendChild(h("div", { class: "notice" }, `比较期涉及多个电表编号：${Array.from(meters).join("、")}。若期间更换电表，需核对读数衔接后再计节能量。`));
    }
  }

  inner.appendChild(h("h3", {}, "舒适度与服务保障"));
  inner.appendChild(comfortIntervalSvg(project.comfort || {}, 600));
  const comfort = project.comfort || {};
  const comfortKv = h("dl", { class: "kv" });
  const addComfort = (label, value) => {
    comfortKv.appendChild(h("dt", {}, label));
    comfortKv.appendChild(h("dd", {}, value));
  };
  addComfort("改造后满意率", comfort.post_pct != null ? pctFmt(comfort.post_pct) : "无抽测");
  addComfort("置信区间", comfort.interval_low != null ? `${pctFmt(comfort.interval_low)} – ${pctFmt(comfort.interval_high)}（90% 置信）` : "—");
  addComfort("有效样本", comfort.sample_count != null ? `${comfort.sample_count} 人` : "未提供样本量");
  addComfort("服务目标", comfort.target != null ? `${pctFmt(comfort.target, 0)}（${comfort.target_use_type}）` : "未找到");
  addComfort("判定", comfortStateText(comfort.state));
  inner.appendChild(comfortKv);
  if (comfort.notes?.length) {
    const list = h("ul", { class: "list" });
    for (const note of comfort.notes) list.appendChild(h("li", {}, note));
    inner.appendChild(list);
  }

  if (project.closure) {
    inner.appendChild(h("h3", {}, "计划停用对开放服务的影响"));
    inner.appendChild(h("p", { class: "reason" }, project.closure.note));
    const closureKv = h("dl", { class: "kv" });
    closureKv.appendChild(h("dt", {}, "停用天数"));
    closureKv.appendChild(h("dd", {}, `${project.closure.days} 天（占施工期 ${pctFmt(project.closure.share_of_works_pct, 0)}）`));
    closureKv.appendChild(h("dt", {}, "损失开放时间"));
    closureKv.appendChild(h("dd", {}, `${numFmt(project.closure.open_days_lost)} 开放日 / ${numFmt(project.closure.open_hours_lost)} 开放小时`));
    inner.appendChild(closureKv);
  }

  inner.appendChild(h("h3", {}, "混杂因素"));
  if (!project.confounders?.length) {
    inner.appendChild(h("div", { class: "notice good" }, "比较期内未登记影响能耗归因的混杂因素。"));
  } else {
    for (const confounder of project.confounders) {
      const severity = confounder.severity === "blocking" ? "block" : confounder.severity === "warning" ? "warn" : "";
      inner.appendChild(h("div", { class: `notice ${confounder.severity === "blocking" ? "error" : ""}` }, h("span", { class: `chip ${severity}` }, CONFOUNDER_TEXT[confounder.code] || confounder.code), " ", confounder.message));
    }
  }

  inner.appendChild(h("h3", {}, "实测复核记录"));
  if (!project.verification_measurements?.length) {
    inner.appendChild(h("p", { class: "meta" }, "尚无投运后实测记录。"));
  } else {
    const table = h("table");
    table.appendChild(h("thead", {}, h("tr", {}, h("th", {}, "编号"), h("th", {}, "日期"), h("th", { class: "num" }, "电 kWh"), h("th", { class: "num" }, "气 m³"), h("th", { class: "num" }, "度日"), h("th", { class: "num" }, "满意率"), h("th", {}, "说明"))));
    const body = h("tbody");
    for (const m of project.verification_measurements) {
      body.appendChild(h("tr", {}, h("td", {}, m.measurement_id), h("td", {}, m.date), h("td", { class: "num" }, numFmt(m.electric_kwh)), h("td", { class: "num" }, numFmt(m.gas_m3)), h("td", { class: "num" }, numFmt(m.degree_days)), h("td", { class: "num" }, pctFmt(m.comfort_sample_pct)), h("td", {}, m.note || "")));
    }
    table.appendChild(body);
    inner.appendChild(table);
  }

  if (project.outstanding?.length) {
    inner.appendChild(h("h3", {}, "未完成事项"));
    const list = h("ul", { class: "list" });
    for (const item of project.outstanding) list.appendChild(h("li", {}, item));
    inner.appendChild(list);
  }

  const approval = analysis.approvals?.[project.project_id];
  if (approval) {
    inner.appendChild(h("h3", {}, "批准时的依据（保留）"));
    inner.appendChild(h("p", { class: "meta" }, `批准人 ${approval.approved_by} · ${approval.approved_at} · 基于 ${approval.decided_on} 的资料`));
    inner.appendChild(h("p", { class: "reason" }, approval.basis.explanation));
    if (approval.handoff) {
      inner.appendChild(h("p", { class: "meta" }, `已移交 ${approval.handoff.team}（${approval.handoff.at}）`));
    }
    if (approval.acceptance?.recheck?.changes?.length) {
      inner.appendChild(h("h3", {}, "验收后与原批准依据的差异"));
      const list = h("ul", { class: "list" });
      for (const change of approval.acceptance.recheck.changes) list.appendChild(h("li", {}, change));
      inner.appendChild(list);
    }
  }

  const actions = h("div", { style: "margin-top:16px; display:flex; gap:8px; flex-wrap:wrap" });
  actions.appendChild(h("button", { onclick: closeProject }, "关闭"));
  inner.appendChild(actions);
  overlay.appendChild(inner);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) closeProject();
  });
  return overlay;
}

function comfortStateText(state) {
  return (
    {
      pass: "达标（区间下界不低于目标）",
      fail: "不达标（区间上界低于目标）",
      unproven: "证据不足/区间跨越目标，不能判定",
      no_data: "无可用抽测",
    }[state] || "—"
  );
}

// ---------------------------------------------------------------------------
// register measure
// ---------------------------------------------------------------------------

function renderRegister(analysis) {
  const panel = h("section", { class: "panel" });
  panel.appendChild(h("h2", {}, "登记拟改造措施"));
  panel.appendChild(
    h(
      "p",
      { class: "sub" },
      "登记后立即查看对开放服务、能耗预计和舒适度证据的影响。计划停用时段必须落在施工期内，未来比较期会把它从开放日中剔除。"
    )
  );
  const buildings = state.payload.building_options || [];
  const options = buildings.map((b) => h("option", { value: b.building_id }, `${b.building_id} · ${b.use_type || "未知用途"}`));
  const form = h("form", { class: "form" });
  const field = (label, input) => h("label", {}, label, input);
  const idInput = h("input", { name: "project_id", required: true, placeholder: "如 PR05" });
  const buildingSelect = h("select", { name: "building_id", required: true }, ...options);
  const nameInput = h("input", { name: "name", placeholder: "如 照明与空调联动改造" });
  const measuresInput = h("textarea", { name: "measures", rows: 2, class: "wide", placeholder: "措施内容、设备型号、范围" });
  form.appendChild(field("项目编号", idInput));
  form.appendChild(field("建筑", buildingSelect));
  form.appendChild(field("措施名称", nameInput));
  form.appendChild(field("施工开始", h("input", { type: "date", name: "measure_start", required: true })));
  form.appendChild(field("施工结束", h("input", { type: "date", name: "measure_end", required: true })));
  form.appendChild(field("投运开始（可选）", h("input", { type: "date", name: "commissioning_start" })));
  form.appendChild(field("投运结束（可选）", h("input", { type: "date", name: "commissioning_end" })));
  form.appendChild(field("计划停用开始", h("input", { type: "date", name: "planned_closure_start" })));
  form.appendChild(field("计划停用结束", h("input", { type: "date", name: "planned_closure_end" })));
  form.appendChild(field("预计节能率（%）", h("input", { type: "number", step: "0.1", min: "0", max: "100", name: "predicted_saving_pct" })));
  form.appendChild(field("预计投资（元）", h("input", { type: "number", step: "1000", min: "0", name: "cost" })));
  form.appendChild(h("div", { class: "wide" }, measuresInput));
  form.appendChild(
    h(
      "div",
      { class: "actions" },
      h("button", { class: "primary", type: "submit" }, "登记并评估"),
      h("span", { class: "meta" }, "登记不等同批准；需在排序页确认证据充分后再批准。")
    )
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(form).entries());
    data.actor = state.actor;
    const result = await mutate("/api/projects", data, "措施已登记，已更新排序影响");
    if (result) {
      state.tab = "projects";
      state.selected = data.project_id;
      render();
    }
  });
  panel.appendChild(form);

  panel.appendChild(h("h3", {}, "开盘前的服务与舒适度校验"));
  panel.appendChild(
    h(
      "p",
      { class: "sub" },
      "候选措施的开放安排、近期舒适度抽测会先与用途目标比对。开放时间低于目标或舒适度基线不足时，措施不会进入优先排序。"
    )
  );
  return panel;
}

// ---------------------------------------------------------------------------
// verify / accept
// ---------------------------------------------------------------------------

function renderVerify(analysis) {
  const panel = h("section", { class: "panel" });
  panel.appendChild(h("h2", {}, "完工验收与实测复核"));
  panel.appendChild(
    h(
      "p",
      { class: "sub" },
      "把已批准项目交给设施团队，并在验收后补录实测能耗/舒适度。补录后系统会重新计算结论，并与批准时的依据逐项比对。"
    )
  );
  const approved = analysis.projects.filter((p) => p.phase === "approved" || p.phase === "in_progress" || p.phase === "commissioning");
  const completed = analysis.projects.filter((p) => p.is_completed);

  panel.appendChild(h("h3", {}, "待跟进项目"));
  if (!approved.length) panel.appendChild(h("div", { class: "notice" }, "没有待跟进项目。"));
  for (const project of approved) {
    const card = h("div", { class: "card" });
    card.appendChild(h("div", { class: "card-head" }, h("div", {}, h("h3", {}, project.name), h("div", { class: "meta" }, `${project.project_id} · ${project.phase}`)), badge(project.decision)));
    const approval = analysis.approvals?.[project.project_id];
    if (approval) {
      card.appendChild(h("p", { class: "meta" }, `批准人 ${approval.approved_by} · ${approval.approved_at}`));
      if (!approval.handoff) {
        const team = h("input", { type: "text", placeholder: "设施团队名称", value: "设施运维组" });
        card.appendChild(
          h(
            "div",
            { style: "display:flex;gap:8px;flex-wrap:wrap;margin-top:8px" },
            team,
            h(
              "button",
              {
                onclick: () =>
                  mutate("/api/handoff", { project_id: project.project_id, team: team.value, actor: state.actor, note: "移交跟进" }, "已移交设施团队"),
              },
              "移交设施团队"
            )
          )
        );
      } else {
        card.appendChild(h("p", { class: "meta" }, `已移交 ${approval.handoff.team} · ${approval.handoff.at}`));
      }
    } else {
      card.appendChild(h("p", { class: "meta" }, "尚未批准。请在排序页确认依据后批准，再移交设施团队。"));
    }
    panel.appendChild(card);
  }

  panel.appendChild(h("h3", {}, "补录实测资料"));
  const projectOptions = analysis.projects.map((p) => h("option", { value: p.project_id }, `${p.project_id} · ${p.name}`));
  const projectSelect = h("select", { name: "project_id" }, ...projectOptions);
  const mForm = h("form", { class: "form" });
  mForm.appendChild(h("label", {}, "项目", projectSelect));
  mForm.appendChild(h("label", {}, "实测日期", h("input", { type: "date", name: "date", required: true })));
  mForm.appendChild(h("label", {}, "电耗 kWh", h("input", { type: "number", step: "1", name: "electric_kwh" })));
  mForm.appendChild(h("label", {}, "燃气 m³", h("input", { type: "number", step: "1", name: "gas_m3" })));
  mForm.appendChild(h("label", {}, "采暖度日", h("input", { type: "number", step: "1", name: "degree_days" })));
  mForm.appendChild(h("label", {}, "舒适度满意率 %", h("input", { type: "number", step: "0.1", min: "0", max: "100", name: "comfort_sample_pct" })));
  mForm.appendChild(h("label", {}, "抽测人数", h("input", { type: "number", step: "1", min: "0", name: "sample_count" })));
  mForm.appendChild(h("label", { class: "wide" }, "说明", h("input", { type: "text", name: "note" })));
  mForm.appendChild(h("div", { class: "actions" }, h("button", { class: "primary", type: "submit" }, "保存实测记录")));
  mForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(mForm).entries());
    const projectId = data.project_id;
    delete data.project_id;
    mutate("/api/measurements", { project_id: projectId, measurement: data, actor: state.actor }, "实测记录已保存，结论已重算");
  });
  panel.appendChild(mForm);

  panel.appendChild(h("h3", {}, "登记完工验收"));
  const acceptForm = h("form", { class: "form" });
  acceptForm.appendChild(h("label", {}, "项目", h("select", { name: "project_id", required: true }, ...projectOptions)));
  acceptForm.appendChild(h("label", {}, "验收日期", h("input", { type: "date", name: "accepted_on", required: true })));
  acceptForm.appendChild(h("label", { class: "wide" }, "备注", h("input", { type: "text", name: "note" })));
  acceptForm.appendChild(h("div", { class: "actions" }, h("button", { class: "primary", type: "submit" }, "登记验收并比对原依据")));
  acceptForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(acceptForm).entries());
    mutate("/api/accept", { ...data, actor: state.actor, note: data.note || "完工验收" }, "已登记验收，结论已重算");
  });
  panel.appendChild(acceptForm);

  if (completed.length) {
    panel.appendChild(h("h3", {}, "已完成项目复核状态"));
    const table = h("div", { class: "table-wrap" });
    const node = h("table");
    node.appendChild(h("thead", {}, h("tr", {}, h("th", {}, "项目"), h("th", {}, "结论"), h("th", {}, "待办"), h("th", {}, "操作"))));
    const body = h("tbody");
    for (const project of completed) {
      body.appendChild(
        h(
          "tr",
          {},
          h("td", {}, project.project_id),
          h("td", {}, badge(project.decision)),
          h("td", {}, project.outstanding?.length ? h("ul", { class: "list" }, ...project.outstanding.map((o) => h("li", {}, o))) : "无"),
          h("td", {}, h("button", { onclick: () => openProject(project.project_id) }, "依据"))
        )
      );
    }
    node.appendChild(body);
    table.appendChild(node);
    panel.appendChild(table);
  }
  return panel;
}

// ---------------------------------------------------------------------------
// data / corrections
// ---------------------------------------------------------------------------

function renderData(analysis) {
  const panel = h("section", { class: "panel" });
  panel.appendChild(h("h2", {}, "资料与更正"));
  panel.appendChild(
    h(
      "p",
      { class: "sub" },
      "补录或更正后点击“重新复核”，系统会列出受影响的投资排序、未完成事项，并保留项目批准时的依据。"
    )
  );

  const importForm = h("form", { class: "form" });
  importForm.appendChild(h("label", { class: "wide" }, "CSV 目录路径", h("input", { type: "text", name: "directory", value: "data" })));
  importForm.appendChild(h("div", { class: "actions" }, h("button", { type: "submit", class: "primary" }, "导入 CSV 目录（替换现有记录）")));
  importForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(importForm).entries());
    try {
      const result = await api("/api/import", "POST", { directory: data.directory, replace: true });
      state.payload = result.payload;
      render();
      toast(`已导入 ${result.imported} 个项目记录`);
    } catch (error) {
      toast(error.message, true);
    }
  });
  panel.appendChild(importForm);

  panel.appendChild(h("h3", {}, "资料问题"));
  if (!analysis.data_issues.length) {
    panel.appendChild(h("div", { class: "notice good" }, "未发现资料结构问题。"));
  } else {
    const table = h("table");
    table.appendChild(h("thead", {}, h("tr", {}, h("th", {}, "级别"), h("th", {}, "资料"), h("th", {}, "记录"), h("th", {}, "说明"))));
    const body = h("tbody");
    for (const issue of analysis.data_issues) {
      body.appendChild(h("tr", {}, h("td", {}, issue.severity === "error" ? "错误" : "提醒"), h("td", {}, issue.dataset), h("td", {}, issue.row), h("td", {}, issue.message)));
    }
    table.appendChild(body);
    panel.appendChild(h("div", { class: "table-wrap" }, table));
  }

  panel.appendChild(h("h3", {}, "补录 / 更正（项目或能耗）"));
  const projectOptions = analysis.projects.map((p) => h("option", { value: p.project_id }, `${p.project_id} · ${p.name}`));
  const form = h("form", { class: "form" });
  form.appendChild(h("label", {}, "项目", h("select", { name: "project_id" }, h("option", { value: "" }, "（不指定）"), ...projectOptions)));
  form.appendChild(h("label", {}, "更正类型", h("select", { name: "kind" }, h("option", { value: "project" }, "项目验收/状态"), h("option", { value: "energy" }, "能耗记录"), h("option", { value: "comfort" }, "舒适度抽测"), h("option", { value: "event" }, "现场事件"))));
  const payloadArea = h("textarea", { rows: 6, class: "wide", name: "payload", placeholder: '{"accepted_on":"2026-05-01"} 或 CSV 风格的 JSON 数组，例如 {"energy":[{"building_id":"BL01","period":"2026-Q2","electric_kwh":"21000","gas_m3":"300","degree_days":"400","coverage_pct":"98"}]}' });
  form.appendChild(h("label", { class: "wide" }, "更正内容（JSON）", payloadArea));
  form.appendChild(h("label", { class: "wide" }, "更正说明", h("input", { type: "text", name: "note", placeholder: "如 补录 2026-Q2 电表读数" })));
  form.appendChild(h("div", { class: "actions" }, h("button", { class: "primary", type: "submit" }, "提交更正并重新复核")));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(form).entries());
    let parsed;
    try {
      parsed = JSON.parse(data.payload || "{}");
    } catch (error) {
      toast(`JSON 解析失败：${error.message}`, true);
      return;
    }
    const updates = {};
    if (data.kind === "project") {
      const projectRow = { project_id: data.project_id, ...parsed };
      updates.projects = [projectRow];
    } else if (data.kind === "energy") {
      updates.energy = Array.isArray(parsed) ? parsed : parsed.energy || [parsed];
    } else if (data.kind === "comfort") {
      updates.comfort = Array.isArray(parsed) ? parsed : parsed.comfort || [parsed];
    } else if (data.kind === "event") {
      updates.events = Array.isArray(parsed) ? parsed : parsed.events || [parsed];
    }
    try {
      const result = await api("/api/correction", "POST", {
        project_id: data.project_id || null,
        note: data.note,
        actor: state.actor,
        updates,
      });
      state.payload = result.payload;
      state.reauditResult = result.reaudit;
      render();
      toast(result.reaudit.affected.length ? `更正影响 ${result.reaudit.affected.length} 个已批准项目` : "已保存更正，未影响已批准结论");
    } catch (error) {
      toast(error.message, true);
    }
  });
  panel.appendChild(form);

  panel.appendChild(h("h3", {}, "现场事件登记（电表更换 / 租户变化 / 其他投运）"));
  const eventForm = h("form", { class: "form" });
  eventForm.appendChild(h("label", {}, "建筑", h("select", { name: "building_id" }, ...(state.payload.building_options || []).map((b) => h("option", { value: b.building_id }, b.building_id)))));
  eventForm.appendChild(h("label", {}, "类型", h("select", { name: "event_type" }, h("option", { value: "meter_replacement" }, "电表更换"), h("option", { value: "tenant_change" }, "租户/使用方变化"), h("option", { value: "equipment_commissioning" }, "其他设备投运调试"), h("option", { value: "use_change" }, "用途/开放安排变化"), h("option", { value: "other" }, "其他"))));
  eventForm.appendChild(h("label", {}, "开始", h("input", { type: "date", name: "start", required: true })));
  eventForm.appendChild(h("label", {}, "结束", h("input", { type: "date", name: "end" })));
  eventForm.appendChild(h("label", {}, "已核对", h("select", { name: "verified" }, h("option", { value: "" }, "否"), h("option", { value: "true" }, "是（读数已衔接）"))));
  eventForm.appendChild(h("label", { class: "wide" }, "说明", h("input", { type: "text", name: "note" })));
  eventForm.appendChild(h("div", { class: "actions" }, h("button", { type: "submit" }, "登记事件")));
  eventForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(eventForm).entries());
    data.actor = state.actor;
    data.verified = data.verified === "true";
    mutate("/api/events", data, "事件已登记，相关项目将重新评估");
  });
  panel.appendChild(eventForm);

  if (state.reauditResult) {
    panel.appendChild(h("h3", {}, "最近一次重算影响"));
    const report = state.reauditResult;
    if (!report.affected.length) {
      panel.appendChild(h("div", { class: "notice good" }, "本次更正未改变任何已批准项目的结论或未完成事项。"));
    } else {
      for (const item of report.affected) {
        const card = h("div", { class: "card" });
        card.appendChild(h("div", { class: "card-head" }, h("div", {}, h("h3", {}, item.name), h("div", { class: "meta" }, `${item.project_id} · 原结论 ${item.previous_label}`)), badge(item.current_decision)));
        const list = h("ul", { class: "list" });
        for (const change of item.changes) list.appendChild(h("li", {}, change));
        card.appendChild(list);
        card.appendChild(h("p", { class: "meta" }, "批准时的依据已保留，可在“审批与留痕”中对照。"));
        panel.appendChild(card);
      }
    }
  }
  return panel;
}

// ---------------------------------------------------------------------------
// audit
// ---------------------------------------------------------------------------

function renderAudit(analysis) {
  const panel = h("section", { class: "panel" });
  panel.appendChild(h("h2", {}, "审批与留痕"));
  panel.appendChild(h("p", { class: "sub" }, "批准时冻结当时的比较期、能耗、舒适度区间与理由；之后的资料补录只追加变化，不覆盖原始依据。"));

  const approvals = state.payload.approvals?.approvals || [];
  if (!approvals.length) {
    panel.appendChild(h("div", { class: "notice" }, "尚无批准记录。"));
  }
  for (const approval of approvals) {
    const card = h("div", { class: "card" });
    const current = approval.current;
    card.appendChild(
      h(
        "div",
        { class: "card-head" },
        h("div", {}, h("h3", {}, `${approval.project_id} · ${approval.basis.decision_label}`), h("div", { class: "meta" }, `批准人 ${approval.approved_by} · ${approval.approved_at} · 基于 ${approval.decided_on}`)),
        badge(current?.current_basis?.decision || approval.decision)
      )
    );
    card.appendChild(h("p", { class: "reason" }, approval.basis.explanation));
    if (approval.handoff) {
      card.appendChild(h("p", { class: "meta" }, `移交：${approval.handoff.team} · ${approval.handoff.at}`));
    }
    if (current?.affected) {
      card.appendChild(h("div", { class: "notice" }, "当前结论与批准依据存在差异："));
      const list = h("ul", { class: "list" });
      for (const change of current.changes) list.appendChild(h("li", {}, change));
      card.appendChild(list);
    } else {
      card.appendChild(h("div", { class: "notice good" }, "当前资料下，结论与批准时一致。"));
    }
    const details = h("details", { class: "disclosure" });
    details.appendChild(h("summary", {}, "查看批准时的完整依据"));
    const pre = h("pre", { style: "white-space:pre-wrap;font-size:12px;color:var(--text-secondary)" }, JSON.stringify(approval.basis, null, 2));
    details.appendChild(pre);
    card.appendChild(details);
    panel.appendChild(card);
  }

  panel.appendChild(h("h3", {}, "操作日志"));
  const log = state.payload.approval_log?.log || [];
  if (!log.length) {
    panel.appendChild(h("p", { class: "meta" }, "暂无操作记录。"));
  } else {
    const table = h("table");
    table.appendChild(h("thead", {}, h("tr", {}, h("th", {}, "时间"), h("th", {}, "操作"), h("th", {}, "项目"), h("th", {}, "操作人"), h("th", {}, "说明"))));
    const body = h("tbody");
    for (const entry of [...log].reverse()) {
      body.appendChild(h("tr", {}, h("td", {}, entry.at), h("td", {}, entry.action), h("td", {}, entry.project_id || "—"), h("td", {}, entry.actor), h("td", {}, entry.note)));
    }
    table.appendChild(body);
    panel.appendChild(h("div", { class: "table-wrap" }, table));
  }
  return panel;
}

// ---------------------------------------------------------------------------
// method
// ---------------------------------------------------------------------------

function renderMethod(analysis) {
  const panel = h("section", { class: "panel" });
  panel.appendChild(h("h2", {}, "口径说明"));
  panel.appendChild(
    h(
      "p",
      { class: "sub" },
      "本应用把“节能”限定为：在用途、开放服务与气象可比的前提下，设备效率带来的用能下降，并且室内环境质量不下降。"
    )
  );
  const sections = [
    ["比较期如何划分", [
      "施工期（改造施工）与设备投运/调试期分开，两者不合并。改造后比较期从投运期结束次日起算。",
      "基线优先取上年同期；上年同期资料不足时回退到改造前相邻等长期间，并在明细中标注基线类型。",
      "比较期长度取施工期与设定比较天数的较大者，且不超过已有资料范围。",
    ]],
    ["为何不能把用途/开放时间变化算作节能", [
      "用途类型在基线与改造后不一致时，项目直接判为“不可比”，不进入投资排序。",
      "开放小时或开放天数变化时，能耗按周开放小时折算后再比较，变化本身不计为设备效率；同时单独核对开放服务是否达到用途目标。",
      "计划停用时段必须落在施工期内，系统计算损失开放日/小时；停用占比超过上限时判为暂缓。",
    ]],
    ["气象与电表/租户等混杂因素", [
      "采暖度日相差较大时按度日比修正，并标注修正幅度；缺少度日时不修正而提示风险。",
      "电表更换、租户变化、其他设备投运等事件落在比较期内时，登记为混杂因素。未核对前项目降为“需补测”，不得把表面能耗差计为节能量。",
    ]],
    ["舒适度与服务保障", [
      "按用途取服务目标：最低开放天数、每日开放小时、最低舒适度满意率。",
      "抽测满意率按样本量加权，并给出 90% Wilson 置信区间。有效样本少于设定下限或覆盖率不足时，只展示区间与“证据不足”，不给单点结论。",
      "区间上界仍低于目标判为“服务不达标”，无论节能多少都不进入优先排序；区间跨越目标判为“证据不足”。",
    ]],
    ["排序与解释", [
      "已完成项目按实测年化节能量 × 证据强度排序，并可换算单位投资节能。",
      "候选措施按预计年化节能量、同楼已完成项目的业绩参照和投资强度排序，标注“待实测复核”。",
      "每个结论都有稳定的理由代码（如 verified_saving、use_type_change），便于审计与复现。",
    ]],
    ["补录与更正", [
      "资料补录/验收更正后重新计算，列出受影响的排序、结论变化与未完成事项。",
      "批准时的依据以快照冻结保存（比较期、能耗、舒适度区间、理由、待办）；之后只追加差异，不覆盖原依据。",
    ]],
  ];
  for (const [title, items] of sections) {
    panel.appendChild(h("h3", {}, title));
    const list = h("ul", { class: "list" });
    for (const item of items) list.appendChild(h("li", {}, item));
    panel.appendChild(list);
  }
  const config = analysis.config;
  panel.appendChild(h("h3", {}, "当前参数"));
  const table = h("table");
  const body = h("tbody");
  const labels = {
    baseline_mode: "基线选择",
    comparison_days: "最短比较期（天）",
    min_energy_coverage_pct: "能耗最低覆盖率（%）",
    min_post_days: "改造后最短稳定期（天）",
    gas_kwh_per_m3: "燃气折算（kWh/m³）",
    min_comfort_samples: "舒适度最低样本（人）",
    min_comfort_coverage_pct: "抽测覆盖率要求（%）",
    comfort_confidence: "区间置信水平",
    meaningful_saving_pct: "实质节能门槛（%）",
    schedule_tolerance_pct: "开放时间容忍差（%）",
    weather_sensitivity_pct: "气象敏感阈值（%）",
    max_closure_share_pct: "停用占施工期上限（%）",
  };
  for (const [key, label] of Object.entries(labels)) {
    body.appendChild(h("tr", {}, h("td", {}, label), h("td", { class: "num" }, String(config[key]))));
  }
  table.appendChild(body);
  panel.appendChild(table);
  return panel;
}

// ---------------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------------

function boot() {
  const root = $("#app");
  root.replaceChildren(h("p", { class: "meta" }, "正在载入…"));
  api("/api/analysis")
    .then((payload) => {
      state.payload = payload;
      render();
    })
    .catch((error) => {
      root.replaceChildren(
        h("div", { class: "notice error" }, `无法载入数据：${error.message}. 请确认服务已启动（python -m retrofit.server）。`)
      );
    });
  const themeButton = $("#theme-toggle");
  themeButton?.addEventListener("click", () => {
    const root = document.documentElement;
    const next = root.dataset.theme === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    themeButton.textContent = next === "dark" ? "浅色" : "深色";
    themeButton.setAttribute("aria-label", next === "dark" ? "切换到浅色模式" : "切换到深色模式");
  });
}

boot();
