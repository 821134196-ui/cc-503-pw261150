/* Pointwise Report Builder — no-build vanilla JS SPA. */
"use strict";

const state = {
  user: "alice",
  catalog: { metrics: [], dimensions: [], fields: [], scopes: [] },
  selectedMetrics: new Map(),   // metric_key -> {versionId, label}
  selectedDims: new Set(),
  filters: [],
  report: null,                 // currently selected saved report
  preview: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      "X-Username": state.user,
      ...(opts.headers || {}),
    },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = body.error ? `${body.error.code}: ${body.error.message}` : `HTTP ${res.status}`;
    const err = new Error(msg);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}

function toast(msg, kind = "") {
  const el = $("toast");
  el.textContent = msg;
  el.className = `toast show ${kind}`;
  setTimeout(() => (el.className = "toast"), 3600);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function statusPill(status) {
  return `<span class="pill ${esc(status)}">${esc(status)}</span>`;
}

// ------------------------------------------------------------- navigation ---
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    $(`tab-${tab.dataset.tab}`).classList.add("active");
    if (tab.dataset.tab === "lineage") loadLineage();
    if (tab.dataset.tab === "version") loadVersionTab();
    if (tab.dataset.tab === "export") loadExportTab();
  });
});

// ---------------------------------------------------------------- bootstrap ---
async function init() {
  await loadUsers();
  await loadCatalog();
  await loadReports();
  bindBuilders();
}

async function loadUsers() {
  const users = await api("/api/admin/users");
  $("userSelect").innerHTML = users
    .map((u) => `<option value="${esc(u.username)}">${esc(u.display_name)}
      [${u.scopes.map((s) => s.key + (s.status === "revoked" ? "(已撤销)" : "")).join(", ")}]</option>`)
    .join("");
  $("userSelect").addEventListener("change", async (e) => {
    state.user = e.target.value;
    toast(`已切换用户：${state.user}`);
    await loadReports();
  });
}

async function loadCatalog() {
  const [metrics, dimensions, fields, scopes] = await Promise.all([
    api("/api/catalog/metrics"),
    api("/api/catalog/dimensions"),
    api("/api/catalog/fields"),
    api("/api/catalog/scopes"),
  ]);
  state.catalog = { metrics, dimensions, fields, scopes };

  $("metricList").innerHTML = metrics
    .map((m) => {
      const active = m.versions.find((v) => v.status === "active");
      return `<div class="pick" data-metric="${esc(m.key)}" data-vid="${active?.id || ""}">
        ${esc(m.name)} <span class="ver">${esc(m.key)} · active v${active?.version ?? "?"}</span></div>`;
    })
    .join("");
  $("metricList").querySelectorAll(".pick").forEach((el) =>
    el.addEventListener("click", () => {
      const key = el.dataset.metric;
      if (state.selectedMetrics.has(key)) {
        state.selectedMetrics.delete(key);
        el.classList.remove("selected");
      } else {
        state.selectedMetrics.set(key, { versionId: Number(el.dataset.vid) || null });
        el.classList.add("selected");
      }
    })
  );

  $("dimList").innerHTML = dimensions
    .map((d) => `<div class="pick" data-dim="${esc(d.key)}">${esc(d.name)}
      <span class="ver">${esc(d.key)}${d.status !== "active" ? ` · ${d.status}` : ""}</span></div>`)
    .join("");
  $("dimList").querySelectorAll(".pick").forEach((el) =>
    el.addEventListener("click", () => {
      const key = el.dataset.dim;
      if (state.selectedDims.has(key)) {
        state.selectedDims.delete(key);
        el.classList.remove("selected");
      } else {
        state.selectedDims.add(key);
        el.classList.add("selected");
      }
    })
  );

  $("filterField").innerHTML = fields
    .map((f) => `<option value="${esc(f.name)}">${esc(f.name)} (${esc(f.status)})</option>`)
    .join("");

  const scopeOpts = [`<option value="">（不使用范围）</option>`].concat(
    scopes.map((s) => `<option value="${esc(s.key)}">${esc(s.name)} [${esc(s.status)}]</option>`)
  );
  $("scopeSelect").innerHTML = scopeOpts.join("");
  $("switchScope").innerHTML = scopeOpts.join("");
}

function currentDefinition() {
  return {
    metrics: [...state.selectedMetrics.entries()].map(([k, v]) => ({
      metric_key: k,
      metric_version_id: v.versionId,
    })),
    dimensions: [...state.selectedDims],
    filters: state.filters,
    scope_key: $("scopeSelect").value || null,
  };
}

function bindBuilders() {
  $("addFilter").addEventListener("click", () => {
    const field = $("filterField").value;
    const op = $("filterOp").value;
    const raw = $("filterValue").value.trim();
    if (!raw) return toast("请输入筛选值", "err");
    let value = raw;
    if (!Number.isNaN(Number(raw))) value = Number(raw);
    state.filters.push({ field, op, value });
    $("filterValue").value = "";
    renderFilters();
  });

  $("btnPreviewAdhoc").addEventListener("click", () => previewAdhoc());
  $("btnSave").addEventListener("click", saveReport);
  $("btnPreviewSnapshot").addEventListener("click", previewSelectedSnapshot);
  $("btnImpact").addEventListener("click", loadImpact);
  $("btnSwitch").addEventListener("click", switchVersion);
  $("btnSwitchScope").addEventListener("click", switchScope);
  $("btnExport").addEventListener("click", createExport);
  $("btnRefreshExports").addEventListener("click", loadExports);
  $("btnRefreshAudit").addEventListener("click", loadAudit);
  $("switchMetric").addEventListener("change", fillVersionOptions);
}

function renderFilters() {
  $("filterChips").innerHTML = state.filters
    .map((f, i) => `<li>${esc(f.field)} ${esc(f.op)} ${esc(JSON.stringify(f.value))}
      <button data-i="${i}">✕</button></li>`)
    .join("");
  $("filterChips").querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => {
      state.filters.splice(Number(b.dataset.i), 1);
      renderFilters();
    })
  );
}

// ------------------------------------------------------------------ reports ---
async function loadReports() {
  const reports = await api("/api/reports");
  $("reportTable").querySelector("tbody").innerHTML = reports
    .map((r) => `<tr>
      <td><a href="#" data-key="${esc(r.key)}" class="rep-link">${esc(r.key)}</a></td>
      <td>${esc(r.title)}</td>
      <td>${r.revision}</td>
      <td><span class="badge ${r.health || "ok"}">${r.health || "—"}</span></td>
      <td><button class="btn small" data-key="${esc(r.key)}">选择</button></td>
    </tr>`)
    .join("");
  $("reportTable").querySelectorAll("button, .rep-link").forEach((el) =>
    el.addEventListener("click", async (e) => {
      e.preventDefault();
      await selectReport(el.dataset.key);
    })
  );
  if (state.report && !reports.find((r) => r.key === state.report.key)) state.report = null;
}

async function selectReport(key) {
  state.report = await api(`/api/reports/${key}`);
  toast(`已选择报表 ${key}（revision ${state.report.revision}）`, "ok");
  await fillSnapshots();
}

async function fillSnapshots() {
  if (!state.report) return;
  const snaps = await api(`/api/reports/${state.report.key}/snapshots`);
  $("snapshotSelect").innerHTML = snaps
    .map((s) => `<option value="${s.id}">快照 #${s.snapshot_no} · revision ${s.revision}</option>`)
    .join("");
}

async function saveReport() {
  const key = $("repKey").value.trim();
  const title = $("repTitle").value.trim();
  if (!key || !title) return toast("请填写报表 Key 与标题", "err");
  if (!state.selectedMetrics.size) return toast("至少选择一个指标", "err");
  try {
    const existing = await api(`/api/reports/${key}`).catch(() => null);
    if (existing) {
      const saved = await api(`/api/reports/${key}`, {
        method: "PUT",
        body: JSON.stringify({
          title,
          definition: currentDefinition(),
          expected_revision: existing.revision,
        }),
      });
      toast(`已保存：revision ${saved.revision}，生成新快照`, "ok");
      await selectReport(key);
    } else {
      const saved = await api("/api/reports", {
        method: "POST",
        body: JSON.stringify({ key, title, definition: currentDefinition() }),
      });
      toast(`报表已创建：revision ${saved.revision}，健康度 ${saved.health}`, "ok");
      await selectReport(key);
    }
    await loadReports();
  } catch (e) {
    toast(e.message, "err");
  }
}

// ------------------------------------------------------------------ preview ---
async function previewAdhoc() {
  if (!state.selectedMetrics.size) return toast("至少选择一个指标", "err");
  try {
    state.preview = await api("/api/reports/preview", {
      method: "POST",
      body: JSON.stringify({ definition: currentDefinition() }),
    });
    renderPreview(state.preview);
    document.querySelector('.tab[data-tab="preview"]').click();
  } catch (e) {
    toast(e.message, "err");
  }
}

async function previewSelectedSnapshot() {
  const snapId = $("snapshotSelect").value;
  if (!snapId) return toast("请先选择报表", "err");
  try {
    state.preview = await api("/api/reports/preview", {
      method: "POST",
      body: JSON.stringify({ snapshot_id: Number(snapId) }),
    });
    renderPreview(state.preview);
  } catch (e) {
    toast(e.message, "err");
  }
}

function renderPreview(p) {
  $("previewBasis").textContent = `执行依据：${p.basis}`;
  $("previewChecksum").textContent = p.checksum;
  $("previewWarnings").innerHTML = (p.warnings || [])
    .map((w) => `<div class="w">⚠️ ${esc(w)}</div>`)
    .join("");
  $("previewTable").querySelector("thead").innerHTML =
    `<tr>${p.columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr>`;
  $("previewTable").querySelector("tbody").innerHTML = p.rows
    .map((row) => `<tr>${row.map((v) => `<td>${esc(v)}</td>`).join("")}</tr>`)
    .join("");
}

// ------------------------------------------------------------------ lineage ---
async function loadLineage() {
  if (!state.report) {
    $("lineageGraph").innerHTML = `<p class="hint">请先在「构建」页选择一张报表。</p>`;
    return;
  }
  const data = await api(`/api/reports/${state.report.key}/lineage`);
  const groups = { metric_version: [], dimension: [], data_source: [], scope: [] };
  data.nodes.forEach((n) => groups[n.kind]?.push(n));

  const metricHtml = groups.metric_version.map((n) => `
    <div class="lg-node">
      <b>${esc(n.metric_name)} ${esc(n.version_label)} ${statusPill(n.status)}</b>
      <div class="meta">表达式：${esc(n.expression)}</div>
      <div class="meta">依赖字段：${n.fields.map((f) =>
        `${esc(f.name)} → ${esc(f.physical_column)} ${statusPill(f.status)}`).join("；") || "（无）"}</div>
    </div>`).join("");

  const dimHtml = groups.dimension.map((n) => `
    <div class="lg-node">
      <b>${esc(n.name)} (${esc(n.key)}) ${statusPill(n.status)}</b>
      <div class="meta">物理字段：${esc(n.field.physical_column)} · 数据源：${esc(n.field.data_source)}</div>
    </div>`).join("");

  const dsHtml = groups.data_source.map((n) => `
    <div class="lg-node"><b>${esc(n.name)}</b>
      <div class="meta">物理表：${esc(n.table)} · 版本标识 ${esc(n.version)}</div></div>`).join("");

  const scopeHtml = groups.scope.map((n) => `
    <div class="lg-node"><b>${esc(n.name)} (${esc(n.key)}) ${statusPill(n.status)}</b>
      <div class="meta">版本标识 ${esc(n.version)}</div></div>`).join("")
    || `<div class="lg-node"><div class="meta">未绑定权限范围</div></div>`;

  $("lineageGraph").innerHTML = `
    <p class="hint">报表 ${esc(data.report_key)} · revision ${data.revision} · 快照 #${data.snapshot_id}
       （血缘在快照生成时冻结，含每个上游的精确版本）</p>
    <div class="lg-row">
      <div class="lg-col"><h4>指标版本（含字段血缘）</h4>${metricHtml}</div>
      <div class="lg-col"><h4>维度</h4>${dimHtml}</div>
      <div class="lg-col"><h4>数据源 / 权限范围</h4>${dsHtml}${scopeHtml}</div>
    </div>`;
}

// ------------------------------------------------------------------ version ---
async function loadVersionTab() {
  if (!state.report) {
    $("impactBox").innerHTML = `<p class="hint">请先选择报表。</p>`;
    return;
  }
  await loadImpact();
  await loadSnapshotsTable();
  $("switchMetric").innerHTML = state.catalog.metrics
    .map((m) => `<option value="${esc(m.key)}">${esc(m.name)}</option>`).join("");
  fillVersionOptions();
}

function fillVersionOptions() {
  const key = $("switchMetric").value;
  const m = state.catalog.metrics.find((x) => x.key === key);
  if (!m) return;
  $("switchVersion").innerHTML = m.versions
    .filter((v) => v.status !== "draft")
    .map((v) => `<option value="${v.id}">v${v.version} [${v.status}] — ${esc(v.change_note || v.expression)}</option>`)
    .join("");
}

async function loadImpact() {
  if (!state.report) return;
  const data = await api(`/api/reports/${state.report.key}/impact`);
  state.report = await api(`/api/reports/${state.report.key}`);
  const issues = data.issues.length
    ? data.issues.map((i) => `
        <div class="issue ${i.severity}">
          <div class="t">${i.severity === "broken" ? "⛔" : "⚠️"} ${esc(i.type)}</div>
          <div class="m">${esc(i.message)}</div>
        </div>`).join("")
    : `<div class="issue ok"><div class="t">✅ 所有上游均为 active，无版本/字段影响</div></div>`;

  const switches = data.switch_options.map((g) => `
    <div class="issue impacted">
      <div class="t">指标 ${esc(g.metric_key)}：当前 v${g.current_version}，可切换到：</div>
      <div class="m">${g.options.map((o) =>
        `v${o.version} [${o.status}]（${esc(o.change_note || o.expression)}）`).join(" ｜ ")}</div>
    </div>`).join("");

  $("impactBox").innerHTML = `
    <p>健康度：<span class="badge ${data.health}">${data.health}</span> · revision ${data.revision}</p>
    ${issues}${switches}`;
  await loadReports();
}

async function loadSnapshotsTable() {
  const snaps = await api(`/api/reports/${state.report.key}/snapshots`);
  $("snapshotTable").querySelector("tbody").innerHTML = snaps
    .map((s) => {
      const mvs = s.lineage.filter((n) => n.kind === "metric_version")
        .map((n) => `${esc(n.metric_key)} ${esc(n.version_label)}`).join(", ");
      return `<tr><td>${s.snapshot_no}</td><td>${s.revision}</td>
        <td>${esc(s.created_by)}</td><td>${new Date(s.created_at).toLocaleString()}</td>
        <td>${mvs}</td></tr>`;
    })
    .join("");
}

async function switchVersion() {
  try {
    const saved = await api(`/api/reports/${state.report.key}/switch-version`, {
      method: "POST",
      body: JSON.stringify({
        metric_key: $("switchMetric").value,
        target_version_id: Number($("switchVersion").value),
        expected_revision: state.report.revision,
      }),
    });
    toast(`版本已切换 → revision ${saved.revision}`, "ok");
    await selectReport(state.report.key);
    await loadVersionTab();
  } catch (e) {
    toast(e.status === 409 ? `并发冲突：${e.message}` : e.message, "err");
  }
}

async function switchScope() {
  try {
    const saved = await api(`/api/reports/${state.report.key}/switch-scope`, {
      method: "POST",
      body: JSON.stringify({
        scope_key: $("switchScope").value || null,
        expected_revision: state.report.revision,
      }),
    });
    toast(`权限范围已切换 → revision ${saved.revision}`, "ok");
    await selectReport(state.report.key);
    await loadVersionTab();
  } catch (e) {
    toast(e.message, "err");
  }
}

// ------------------------------------------------------------------- export ---
async function loadExportTab() {
  if (!state.report) return;
  await fillSnapshots();
  await loadExports();
  await loadAudit();
}

async function createExport() {
  if (!state.report) return toast("请先选择报表", "err");
  try {
    const ex = await api(`/api/reports/${state.report.key}/exports`, { method: "POST" });
    toast(`导出已冻结：${ex.row_count} 行，校验和 ${ex.checksum.slice(0, 12)}…`, "ok");
    await loadExports();
    await loadAudit();
  } catch (e) {
    toast(e.message, "err");
  }
}

async function loadExports() {
  if (!state.report) return;
  const records = await api(`/api/reports/${state.report.key}/exports`);
  $("exportTable").querySelector("tbody").innerHTML = records
    .map((r) => `<tr>
      <td>${r.export_id}</td><td>${r.snapshot_id}</td><td>${r.row_count}</td>
      <td><code>${esc(r.checksum.slice(0, 16))}…</code></td>
      <td>${esc(r.requested_by)}</td><td>${new Date(r.created_at).toLocaleString()}</td>
      <td>
        <a href="/api/reports/${state.report.key}/exports/${r.export_id}/download"
           download target="_blank">下载</a> ·
        <a href="#" class="verify-link" data-id="${r.export_id}">校验</a>
      </td></tr>`)
    .join("");
  $("exportTable").querySelectorAll(".verify-link").forEach((a) =>
    a.addEventListener("click", async (e) => {
      e.preventDefault();
      const v = await api(`/api/reports/${state.report.key}/exports/${a.dataset.id}/verify`);
      toast(v.consistent
        ? `✅ 校验一致：文件 sha256 与冻结记录匹配（${v.row_count} 行）`
        : "❌ 校验失败：文件与快照不一致", v.consistent ? "ok" : "err");
    })
  );
}

async function loadAudit() {
  const events = await api(`/api/admin/audit?limit=150`);
  $("auditTable").querySelector("tbody").innerHTML = events
    .map((e) => `<tr>
      <td>${new Date(e.at).toLocaleString()}</td>
      <td>${esc(e.actor_name)}</td>
      <td>${esc(e.action)}</td>
      <td>${esc(e.target_type)} ${esc(e.target_id)}</td>
      <td><span class="badge ${e.result === "success" ? "ok" : "broken"}">${esc(e.result)}</span></td>
      <td><code>${esc(JSON.stringify(e.detail || {}).slice(0, 120))}</code></td>
    </tr>`)
    .join("");
}

init().catch((e) => toast(`初始化失败：${e.message}`, "err"));
