"use strict";
const $ = id => document.getElementById(id);
const names = { aws: "AWS", vercel: "Vercel", cloudflare: "Cloudflare", aliyun: "阿里云", alibabacloud: "Alibaba Cloud" };
const colors = { total: "#b4f5d1", aws: "#f4b564", vercel: "#c5cddb", cloudflare: "#f59469", aliyun: "#9d9af2", alibabacloud: "#72bad8" };
const symbols = { aws: "aws", vercel: "▲", cloudflare: "☁", aliyun: "◈", alibabacloud: "ALI" };
const viewInfo = {
  overview: ["费用概览", "一处掌握所有云平台的当月累计费用。"],
  providers: ["云平台", "查看各平台的累计费用、独立预算与采集状态。"],
  history: ["费用快照", "每一次采集，都留下可追溯的费用记录。"],
  alerts: ["报警记录", "查看预算超限事件、通知投递状态与采集错误。"],
  settings: ["预算与汇率", "当前生效的预算与固定汇率配置。"]
};
const state = { data: null, snapshots: [], alerts: [], collections: [], currency: "USD", series: "total", token: sessionStorage.getItem("cloudcost-token") || "", loading: false, sequence: 0 };
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function cash(amount, currency = "USD") {
  if (amount == null || !Number.isFinite(Number(amount))) return "—";
  return new Intl.NumberFormat("en-US", { style: "currency", currency, minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Number(amount));
}
function displayAmount(usd) { return Number(usd) * Number(state.data?.fx?.[state.currency] || 1); }
function stamp(value) {
  if (!value) return "尚未采集";
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "UTC" }).format(new Date(value)) + " UTC";
}
function basisLabel(basis) { return ({unbilled_mtd:"当月未出账", calendar_mtd:"自然月累计", mixed_mtd:"月累计 · 混合口径"})[basis] || "等待费用数据"; }
function label(p) { return names[p.kind] || p.id; }
function logo(p) { return `<span class="provider-logo ${esc(p.kind)}">${esc(symbols[p.kind] || "☁")}</span>`; }
function badge(p) {
  const status = { missing: ["未接入数据", "warning"], stale: ["数据已过期", "warning"], partial: ["覆盖不完整", "warning"], currency_mismatch: ["币种不匹配", "danger"] };
  if (p.status !== "ok") return `<span class="pill ${status[p.status]?.[1] || "warning"}">${esc(status[p.status]?.[0] || p.status)}</span>`;
  const exceeded = p.threshold !== null && Number(p.amount) / Number(state.data.fx[p.currency]) * Number(state.data.fx[p.threshold_currency]) > Number(p.threshold);
  return `<span class="pill ${exceeded ? "danger" : "good"}"><span class="dot"></span>${exceeded ? "预算超限" : "数据完整"}</span>`;
}
function progress(p) {
  if (p.budget_percent == null) return p.threshold != null && Number(p.threshold) === 0 ? '<span class="timestamp">预算为 0</span>' : '<span class="timestamp">未设置预算</span>';
  const percent = Math.max(0, Number(p.budget_percent));
  return `<div class="provider-progress ${percent > 100 ? "over" : ""}"><div class="track"><i class="bar" data-percent="${Math.min(100, percent)}"></i></div><span>${percent.toFixed(1)}%</span></div>`;
}
function applyBars() { document.querySelectorAll("[data-percent]").forEach(el => { el.style.width = `${Number(el.dataset.percent)}%`; }); }
function empty(title, description = "") { return `<div class="empty-state"><strong>${esc(title)}</strong>${esc(description)}</div>`; }
function view() {
  const requested = location.hash.slice(1) || "overview";
  const current = viewInfo[requested] ? requested : "overview";
  document.querySelectorAll("nav a").forEach(a => a.classList.toggle("active", a.dataset.view === current));
  document.querySelectorAll(".view").forEach(el => { el.hidden = el.id !== `view-${current}`; });
  $("breadcrumb-title").textContent = $("page-title").textContent = viewInfo[current][0];
  $("page-description").textContent = viewInfo[current][1];
}
async function api(path) {
  const response = await fetch(path, { headers: state.token ? { Authorization: `Bearer ${state.token}` } : {}, cache: "no-store" });
  if (response.status === 401) {
    if (!$("auth-dialog").open) $("auth-dialog").showModal();
    throw new Error("需要有效的 API 访问令牌");
  }
  if (!response.ok) throw new Error(`查询失败（HTTP ${response.status}）`);
  return response.json();
}
async function load(init = false) {
  const sequence = ++state.sequence;
  $("refresh").disabled = true;
  $("refresh").classList.add("spin");
  try {
    if (init || !$("month").value) {
      const months = await api("/api/months");
      if (sequence !== state.sequence) return;
      const chosen = $("month").value || months.current_month;
      $("month").innerHTML = months.months.map(m => `<option value="${esc(m)}">${esc(m.replace("-", " 年 "))} 月${m === months.current_month ? " · 当月" : ""}</option>`).join("");
      $("month").value = months.months.includes(chosen) ? chosen : months.current_month;
    }
    const q = `month=${encodeURIComponent($("month").value)}`;
    const [data, history, alerts, collections] = await Promise.all([api(`/api/summary?${q}`), api(`/api/history?${q}&limit=5000`), api(`/api/alerts?${q}`), api("/api/collections")]);
    if (sequence !== state.sequence) return;
    state.data = data; state.snapshots = history.snapshots; state.alerts = alerts.alerts; state.collections = collections.collections;
    if ($("auth-dialog").open) $("auth-dialog").close();
    render();
    $("last-updated").textContent = `查询更新于 ${stamp(data.as_of)} · 每 60 秒刷新`;
  } catch (e) {
    if (sequence !== state.sequence) return;
    $("notice").hidden = false; $("notice").className = "notice";
    $("notice").textContent = `${e.message}。${state.data ? "页面保留上次查询结果。" : "请确认服务与配置已就绪。"}`;
    if ($("auth-dialog").open) $("auth-error").textContent = e.message;
  } finally {
    if (sequence === state.sequence) { $("refresh").disabled = false; $("refresh").classList.remove("spin"); }
  }
}
function render() {
  const d = state.data, providers = d.providers, available = providers.filter(p => p.amount !== null);
  const total = displayAmount(d.total_usd), budget = d.total_threshold_usd === null ? null : displayAmount(d.total_threshold_usd);
  $("total-value").textContent = available.length ? cash(total, state.currency) : "—";
  document.querySelectorAll(".display-unit").forEach(el => { el.textContent = state.currency; });
  $("budget-value").textContent = cash(budget, state.currency);
  const over = budget !== null && total > budget;
  $("total-status").textContent = !d.complete ? "合计不完整" : over ? "预算超限" : "数据完整";
  $("total-status").className = `pill ${!d.complete ? "warning" : over ? "danger" : "good"}`;
  $("budget-remaining").textContent = !d.complete ? "覆盖不完整，暂停总预算判断" : budget === null ? "在配置文件中设置预算" : `${over ? "已超出预算" : "剩余预算"} ${cash(Math.abs(budget - total), state.currency)}`;
  $("budget-remaining").classList.toggle("error-text", over && d.complete);
  $("budget-percent").textContent = d.budget_percent !== null && available.length ? `${d.complete ? "" : "≈"}${Number(d.budget_percent).toFixed(1)}%` : "—";
  $("budget-progress").style.width = `${Math.max(0, Math.min(100, Number(d.budget_percent || 0)))}%`;
  $("budget-progress").parentElement.classList.toggle("danger", over);
  $("provider-count").textContent = providers.length;
  $("nav-provider-count").textContent = providers.length;
  $("provider-status").textContent = `${providers.filter(p => p.status === "ok").length} 个数据完整 · ${providers.filter(p => p.status !== "ok").length} 个待检查`;
  const historical = d.month !== new Date().toISOString().slice(0, 7);
  $("notice").hidden = d.complete && !d.demo && !historical;
  $("notice").className = `notice${d.demo ? " demo" : ""}`;
  $("notice").textContent = (d.demo ? "演示模式 · 当前展示模拟的月累计费用，外部通知已禁用。" : "") + (historical ? "历史月份展示当时保存的费用快照，不代表最终账单。" : !d.complete ? " 部分平台数据缺失、过期或覆盖不完整；已知合计仅供参考，总预算判断已暂停。" : " 云平台费用数据可能延迟更新。");
  $("footer-fx").textContent = `1 USD = ${Number(d.fx.CNY).toFixed(2)} CNY · 固定汇率`;
  $("footer-basis").textContent = basisLabel(d.basis);
  renderProviders(); renderChart(); renderAllocation(); renderHistory(); renderAlerts(); renderSettings();
  applyBars();
}
function providerTable() {
  if (!state.data.providers.length) return empty("尚未配置云平台", "在 TOML 中添加 providers 配置。");
  return `<table><thead><tr><th>云平台</th><th>当月累计费用</th><th>独立预算</th><th>预算使用率</th><th>状态</th><th>数据时间</th></tr></thead><tbody>${state.data.providers.map(p => `<tr><td><div class="provider-cell">${logo(p)}<div><div class="provider-name">${esc(label(p))}</div><div class="provider-id">${esc(p.id)} · ${esc(basisLabel(p.basis))}${p.source?.includes(":configured-fixed-fees") ? " · 含固定费估计" : ""}</div></div></div></td><td><span class="money-text">${cash(p.amount, p.currency)}</span><span class="secondary-money">${p.amount_usd !== null ? `≈ ${cash(displayAmount(p.amount_usd), state.currency)}` : "等待当月费用数据"}</span></td><td>${cash(p.threshold, p.threshold_currency)}<span class="secondary-money">${esc(p.threshold_currency)} / 月</span></td><td>${progress(p)}</td><td>${badge(p)}</td><td class="timestamp">${stamp(p.captured_at)}</td></tr>`).join("")}</tbody></table>`;
}
function renderProviders() {
  $("overview-provider-table").innerHTML = $("all-provider-table").innerHTML = providerTable();
  $("provider-cards").innerHTML = state.data.providers.map(p => `<article class="panel provider-card"><div class="provider-card-header">${logo(p)}<div><h3>${esc(label(p))}</h3><span class="provider-id">${esc(p.id)}</span></div>${badge(p)}</div><span class="label">${esc(basisLabel(p.basis))}${p.source?.includes(":configured-fixed-fees") ? " · 含固定费估计" : ""}</span><div class="card-cost">${cash(p.amount, p.currency)}</div><span class="label">独立预算 ${cash(p.threshold, p.threshold_currency)}</span>${progress(p)}<div class="card-footer"><span>${esc(p.mode === "native" ? "原生费用 API" : "月累计费用 feed")}</span><span>${stamp(p.captured_at)}</span></div></article>`).join("");
  const old = $("history-provider").value;
  $("history-provider").innerHTML = `<option value="">全部平台</option>${state.data.providers.map(p => `<option value="${esc(p.id)}">${esc(label(p))} · ${esc(p.id)}</option>`).join("")}`;
  if (state.data.providers.some(p => p.id === old)) $("history-provider").value = old;
}
function dailySeries() {
  const providers = new Map(state.data.providers.map(p => [p.id, p]));
  const sorted = state.snapshots.filter(r => providers.has(r.provider)).slice().sort((a,b) => a.captured_at.localeCompare(b.captured_at) || a.id - b.id);
  const days = new Map();
  for (const row of sorted) {
    const day = row.captured_at.slice(0,10);
    if (!days.has(day)) days.set(day, []);
    days.get(day).push(row);
  }
  const last = new Map(), points = [];
  for (const [day, rows] of days) {
    for (const row of rows) last.set(row.provider, Number(row.amount_usd));
    let amount = 0;
    for (const [id, usd] of last) if (state.series === "total" || id === state.series) amount += usd;
    points.push({ day, amount: displayAmount(amount), coverage: last.size });
  }
  return points;
}
function renderChart() {
  const providers = state.data.providers;
  if (state.series !== "total" && !providers.some(p => p.id === state.series)) state.series = "total";
  $("chart-legend").innerHTML = [{ id: "total", kind: "total", title: "全部平台" }, ...providers.map(p => ({ ...p, title: label(p) }))].map(p => `<button class="legend-button ${p.id === state.series ? "selected" : ""}" data-series="${esc(p.id)}"><span class="dot ${esc(p.kind)}"></span>${esc(p.title)}</button>`).join("");
  $("chart-legend").querySelectorAll("button").forEach(button => button.addEventListener("click", () => { state.series = button.dataset.series; renderChart(); }));
  const points = dailySeries();
  if (!points.length) { $("trend-chart").innerHTML = empty("暂无采集快照", "运行 cloudcost collect 后查看累计费用趋势。"); return; }
  const w = Math.max(260, $("trend-chart").clientWidth - 23), h = 210, left = 57, right = 14, top = 16, bottom = 31;
  const maximum = Math.max(1, ...points.map(p => p.amount)), minimum = Math.min(0, ...points.map(p => p.amount));
  const span = maximum - minimum, step = Math.pow(10, Math.floor(Math.log10(span))) / 2;
  const max = Math.ceil(maximum / step) * step, min = Math.floor(minimum / step) * step;
  const first = Date.parse(points[0].day), last = Date.parse(points[points.length - 1].day);
  const x = p => left + (last === first ? .5 : (Date.parse(p.day) - first)/(last-first)) * (w-left-right);
  const y = amount => top + (max-amount)/(max-min) * (h-top-bottom);
  const path = points.map((p,i) => `${i === 0 ? "M" : "L"}${x(p).toFixed(2)},${y(p.amount).toFixed(2)}`).join(" ");
  const color = state.series === "total" ? colors.total : colors[providers.find(p => p.id === state.series)?.kind] || colors.total;
  let grid = "";
  for (let i = 0; i <= 4; i++) {
    const value = min + (max-min)*i/4, pos = y(value);
    const text = (state.currency === "USD" ? "$" : "¥") + (Math.abs(value) >= 1000 ? (value/1000).toFixed(1)+"k" : value.toFixed(0));
    grid += `<line x1="${left}" y1="${pos}" x2="${w-right}" y2="${pos}" stroke="#2a323f" stroke-dasharray="3 5"/><text x="${left-12}" y="${pos+3}" text-anchor="end" fill="#78869b" font-size="9">${esc(text)}</text>`;
  }
  const tickCount = Math.min(5, points.length), indices = new Set();
  for (let i=0;i<tickCount;i++) indices.add(Math.round(i*(points.length-1)/Math.max(1,tickCount-1)));
  const ticks = [...indices].map(i => `<text x="${x(points[i])}" y="${h-8}" text-anchor="middle" fill="#78869b" font-size="9">${esc(points[i].day.slice(5).replace("-", "/"))}</text>`).join("");
  const dots = points.map(p => `<circle cx="${x(p)}" cy="${y(p.amount)}" r="3" fill="${color}" opacity=".8"><title>${esc(p.day)}：${cash(p.amount,state.currency)}（覆盖 ${p.coverage}/${providers.length} 平台）</title></circle>`).join("");
  $("trend-chart").innerHTML = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(state.series === "total" ? "全部平台" : state.series)}累计费用趋势"><defs><linearGradient id="area" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${color}" stop-opacity=".15"/><stop offset="100%" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>${grid}<path d="${path} L${x(points.at(-1))},${y(min)} L${x(points[0])},${y(min)} Z" fill="url(#area)"/><path d="${path}" fill="none" stroke="${color}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>${dots}${ticks}</svg>`;
  $("chart-caption-date").textContent = `${points[0].day.slice(5)} — ${points.at(-1).day.slice(5)} · UTC`;
}
function renderAllocation() {
  const providers = state.data.providers.filter(p => p.amount_usd !== null && Number(p.amount_usd)>0);
  const total = providers.reduce((sum,p) => sum + Number(p.amount_usd),0);
  if (!total) { $("allocation-chart").innerHTML = empty("暂无费用分布"); $("allocation-legend").innerHTML = ""; return; }
  let offset = 0;
  const circumference = 2*Math.PI*60;
  const arcs = providers.map(p => {
    const ratio = Number(p.amount_usd)/total, start = offset;
    offset += ratio;
    return `<circle cx="82" cy="82" r="60" fill="none" stroke="${colors[p.kind]}" stroke-width="15" stroke-dasharray="${Math.max(0,ratio*circumference-3)} ${circumference}" stroke-dashoffset="${-start*circumference}" transform="rotate(-90 82 82)"><title>${esc(label(p))} ${cash(displayAmount(p.amount_usd),state.currency)}</title></circle>`;
  }).join("");
  $("allocation-chart").innerHTML = `<svg viewBox="0 0 164 164" role="img" aria-label="各平台费用占比"><circle cx="82" cy="82" r="60" fill="none" stroke="#2a3341" stroke-width="15"/>${arcs}</svg><div class="donut-center"><small>已知费用合计</small><strong>${cash(displayAmount(state.data.total_usd),state.currency)}</strong><small>${state.currency}</small></div>`;
  $("allocation-legend").innerHTML = providers.map(p => `<div class="allocation-row"><span class="dot ${esc(p.kind)}"></span>${esc(label(p))}<strong>${(Number(p.amount_usd)/total*100).toFixed(1)}%</strong></div>`).join("");
}
function renderHistory() {
  const selected = $("history-provider").value;
  const rows = state.snapshots.filter(row => !selected || row.provider === selected).slice(0,500);
  if (!rows.length) { $("history-table").innerHTML = empty("暂无费用快照", "运行采集命令后，记录会出现在这里。"); return; }
  $("history-table").innerHTML = `<table><thead><tr><th>数据时间 (UTC)</th><th>云平台 / 账号</th><th>月度累计金额</th><th>折算 ${esc(state.currency)}</th><th>数据来源 / 口径</th><th>覆盖范围</th></tr></thead><tbody>${rows.map(r => `<tr><td class="timestamp">${stamp(r.captured_at)}</td><td>${esc(r.provider)}</td><td class="money-text">${cash(r.amount,r.currency)}</td><td>${cash(displayAmount(r.amount_usd),state.currency)}</td><td class="timestamp">${esc(r.source)}<span class="secondary-money">${esc(basisLabel(r.basis))}</span></td><td><span class="pill ${r.complete ? "good" : "warning"}">${r.complete ? "完整" : "部分"}</span></td></tr>`).join("")}</tbody></table>`;
}
function renderAlerts() {
  $("nav-alert-count").textContent = state.alerts.length;
  $("alerts-count").textContent = `${state.alerts.length} 条记录`;
  const statuses = {sent:["已发送","good"],failed:["投递失败","danger"],pending:["待发送","warning"],sending:["投递中","warning"]};
  $("alert-list").innerHTML = state.alerts.length ? state.alerts.map(a => `<article class="alert-row"><div class="alert-row-title"><span class="pill danger">预算超限</span><strong>${esc(a.scope === "total" ? "全部平台总预算" : a.scope)}</strong><span class="timestamp">${stamp(a.created_at)}</span></div><p>${cash(a.amount,a.currency)} 超过阈值 ${cash(a.threshold,a.currency)} · ${esc(a.month)} · ${esc(basisLabel(a.basis))}</p><div class="delivery-tags">${a.deliveries.length ? a.deliveries.map(c => `<span class="pill ${statuses[c.status]?.[1] || "warning"}" title="${esc(c.error || "")}">${esc(c.channel)} · ${esc(statuses[c.status]?.[0] || c.status)} · ${c.attempts} 次尝试</span>`).join("") : "未配置通知渠道"}</div></article>`).join("") : empty("这个月还没有报警记录", "预算判断由 CLI / 监控进程执行，查询页面不会触发通知。");
  $("collection-table").innerHTML = state.collections.length ? `<table><thead><tr><th>时间 (UTC)</th><th>平台</th><th>月份</th><th>采集状态</th><th>说明</th></tr></thead><tbody>${state.collections.map(r => `<tr><td class="timestamp">${stamp(r.collected_at)}</td><td>${esc(r.provider)}</td><td>${esc(r.month)}</td><td><span class="pill ${r.ok ? "good" : "danger"}">${r.ok ? "成功" : "失败"}</span></td><td class="error-text">${esc(r.error || "—")}</td></tr>`).join("")}</tbody></table>` : empty("尚无采集日志", "演示快照在初始化时生成。运行 collect 可生成采集记录。");
}
function renderSettings() {
  const d = state.data;
  $("settings-budget").innerHTML = `<div class="settings-row"><span>总和月度预算</span><strong>${cash(d.total_threshold,d.total_threshold_currency)}</strong></div>${d.providers.map(p => `<div class="settings-row"><span>${esc(label(p))} · ${esc(p.id)}</span><strong>${cash(p.threshold,p.threshold_currency)}</strong></div>`).join("")}`;
  $("settings-fx").innerHTML = `<span class="timestamp">当前固定汇率</span><div class="fx-value">$1 = ¥${Number(d.fx.CNY).toFixed(2)}</div><div class="settings-row"><span>总金额显示币种</span><strong>${state.currency}</strong></div><div class="settings-row"><span>报警比较规则</span><strong>累计金额严格大于阈值</strong></div>`;
}
function exportCSV() {
  const selected = $("history-provider").value;
  const rows = state.snapshots.filter(r => !selected || r.provider === selected);
  const fields = ["provider","month","amount","currency","amount_usd","captured_at","source","complete","basis"];
  const cell = value => { let s = String(value ?? ""); if (/^[=+@-]/.test(s) && !/^-?\d+(\.\d+)?$/.test(s)) s="'"+s; return '"'+s.replace(/"/g,'""')+'"'; };
  const text = "\ufeff" + [fields.map(cell).join(","), ...rows.map(r => fields.map(f => cell(r[f])).join(","))].join("\r\n");
  const url = URL.createObjectURL(new Blob([text],{type:"text/csv;charset=utf-8"}));
  const a = document.createElement("a"); a.href = url; a.download = `cloudcost-${state.data.month}${selected ? "-"+selected : ""}.csv`; a.click();
  setTimeout(() => URL.revokeObjectURL(url),1000);
}
window.addEventListener("hashchange", view);
window.addEventListener("resize", () => { if (state.data) renderChart(); });
$("refresh").addEventListener("click", () => load(true));
$("month").addEventListener("change", () => load());
$("display-currency").addEventListener("change", e => { state.currency=e.target.value; if (state.data) render(); });
$("history-provider").addEventListener("change", renderHistory);
$("export").addEventListener("click", () => { if (state.data) exportCSV(); });
$("auth-form").addEventListener("submit", e => { e.preventDefault(); state.token=$("api-token").value; sessionStorage.setItem("cloudcost-token",state.token); $("auth-error").textContent=""; load(true); });
$("auth-dialog").addEventListener("cancel", e => e.preventDefault());
view(); load(true);
setInterval(() => { if (!document.hidden && !$("auth-dialog").open && !$("refresh").disabled) load(true); },60000);
