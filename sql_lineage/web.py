"""把血缘报告（可选加影响分析结果）渲染成一个自包含的 HTML 页面。

页面不依赖任何外部资源：对象树可搜索，点击字段后展示上游来源与下游
影响的可展开路径树，未解析语句和循环依赖有醒目标识。
"""
from __future__ import annotations

import json

_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SQL 血缘分析</title>
<style>
:root {
  --bg: #f6f7f9; --panel: #ffffff; --ink: #1f2430; --muted: #6b7280;
  --line: #e3e6ea; --accent: #2563eb; --warn-bg: #fef3c7; --warn-ink: #92400e;
  --bad-bg: #fee2e2; --bad-ink: #b91c1c; --ok-ink: #15803d;
}
* { box-sizing: border-box; }
body { margin: 0; font-family: system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
       background: var(--bg); color: var(--ink); font-size: 14px; }
header { background: var(--panel); border-bottom: 1px solid var(--line); padding: 12px 20px; }
header h1 { font-size: 17px; margin: 0 0 6px; }
.stats { color: var(--muted); font-size: 12.5px; display: flex; gap: 14px; flex-wrap: wrap; }
.banner { margin-top: 8px; padding: 8px 12px; border-radius: 6px; background: var(--warn-bg);
          color: var(--warn-ink); font-size: 12.5px; }
nav.tabs { display: flex; gap: 4px; padding: 8px 20px 0; background: var(--panel);
           border-bottom: 1px solid var(--line); }
nav.tabs button { border: none; background: none; padding: 8px 14px; cursor: pointer;
                  font-size: 13.5px; color: var(--muted); border-bottom: 2px solid transparent; }
nav.tabs button.active { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }
main { padding: 16px 20px; }
section.tab { display: none; }
section.tab.active { display: block; }
.layout { display: grid; grid-template-columns: 320px 1fr; gap: 16px; align-items: start; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
#search { width: 100%; padding: 8px 10px; border: 1px solid var(--line); border-radius: 6px;
          font-size: 13px; margin-bottom: 8px; }
aside.panel { padding: 10px; max-height: calc(100vh - 170px); overflow: auto; }
.obj { margin-bottom: 4px; }
.obj > .obj-head { font-weight: 600; padding: 4px 6px; cursor: pointer; border-radius: 5px; }
.obj > .obj-head:hover { background: var(--bg); }
.kind-badge { display: inline-block; font-size: 10.5px; padding: 1px 6px; border-radius: 8px;
              margin-right: 6px; vertical-align: 1px; }
.kind-table { background: #e0e7ff; color: #3730a3; }
.kind-view { background: #dcfce7; color: #166534; }
ul.cols { list-style: none; margin: 2px 0 6px; padding-left: 14px; }
ul.cols li { padding: 2px 6px; border-radius: 5px; cursor: pointer; color: #374151; }
ul.cols li:hover { background: #eef2ff; }
ul.cols li.sel { background: #dbeafe; color: #1d4ed8; font-weight: 600; }
.col-type { color: var(--muted); font-size: 11px; margin-left: 6px; }
.detail { padding: 14px 16px; min-height: 300px; }
.detail h2 { font-size: 15px; margin: 0 0 4px; }
.detail .sub { color: var(--muted); font-size: 12px; margin-bottom: 12px; }
.detail h3 { font-size: 13.5px; margin: 16px 0 6px; border-top: 1px solid var(--line); padding-top: 12px; }
.hint { color: var(--muted); }
ul.tree { list-style: none; padding-left: 18px; margin: 4px 0; border-left: 1px dashed var(--line); }
ul.tree.root { border-left: none; padding-left: 0; }
ul.tree li { margin: 3px 0; }
details > summary { cursor: pointer; list-style: none; }
details > summary::before { content: "▸ "; color: var(--muted); }
details[open] > summary::before { content: "▾ "; }
.node-label { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12.5px; }
.node-eph { color: #7c3aed; }
.edge-info { color: var(--muted); font-size: 11.5px; margin-left: 6px; }
.edge-info code { background: var(--bg); padding: 1px 5px; border-radius: 4px;
                  font-size: 11px; word-break: break-all; }
.tf { display: inline-block; font-size: 10px; padding: 0 5px; border-radius: 7px; margin-right: 4px; }
.tf-direct { background: #e5e7eb; color: #374151; }
.tf-expression { background: #dbeafe; color: #1e40af; }
.tf-aggregate { background: #fce7f3; color: #9d174d; }
.tf-window { background: #ede9fe; color: #6d28d9; }
.tf-setop { background: #fef9c3; color: #854d0e; }
.kind-usage { color: #b45309; }
.cycle-mark { color: var(--bad-ink); font-size: 12px; }
table.stmts { width: 100%; border-collapse: collapse; font-size: 12.5px; }
table.stmts th, table.stmts td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line);
                                 vertical-align: top; }
table.stmts th { color: var(--muted); font-weight: 600; }
.st { display: inline-block; font-size: 11px; padding: 1px 8px; border-radius: 9px; }
.st-ok { background: #dcfce7; color: var(--ok-ink); }
.st-partial { background: var(--warn-bg); color: var(--warn-ink); }
.st-unresolved { background: var(--bad-bg); color: var(--bad-ink); }
.st-ignored { background: #e5e7eb; color: var(--muted); }
.warn-list { margin: 4px 0 0; padding-left: 16px; color: var(--warn-ink); font-size: 12px; }
.reason { color: var(--bad-ink); font-size: 12px; }
.cycle-box { background: var(--bad-bg); color: var(--bad-ink); border-radius: 6px;
             padding: 8px 12px; margin-bottom: 10px; font-size: 12.5px; }
.change-card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
               padding: 12px 16px; margin-bottom: 14px; }
.change-card h3 { margin: 0 0 6px; font-size: 14px; }
.change-tag { display: inline-block; font-size: 11px; padding: 1px 8px; border-radius: 9px; margin-left: 8px; }
.tag-drop { background: var(--bad-bg); color: var(--bad-ink); }
.tag-rename { background: var(--warn-bg); color: var(--warn-ink); }
.tag-type { background: #dbeafe; color: #1e40af; }
.caveat { color: var(--warn-ink); font-size: 12px; margin: 4px 0; }
.aff-group { margin: 8px 0; }
.aff-obj { font-weight: 600; font-size: 13px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.loc { color: var(--muted); font-size: 11px; }
</style>
</head>
<body>
<header>
  <h1>SQL 血缘分析 <span class="loc">%%META%%</span></h1>
  <div class="stats" id="stats"></div>
  <div class="banner" id="banner" hidden></div>
</header>
<nav class="tabs">
  <button data-tab="lineage" class="active">血缘浏览</button>
  <button data-tab="impact" id="tabbtn-impact">影响分析</button>
  <button data-tab="statements">语句与问题</button>
</nav>
<main>
  <section class="tab active" id="tab-lineage">
    <div class="layout">
      <aside class="panel">
        <input id="search" placeholder="搜索表 / 视图 / 字段…">
        <div id="obj-list"></div>
      </aside>
      <div class="panel detail" id="detail">
        <p class="hint">点击左侧字段，查看它的上游来源与下游影响路径。<br>
        紫色节点是语句内部的中间层（CTE / 子查询），可逐级展开还原完整链路。</p>
      </div>
    </div>
  </section>
  <section class="tab" id="tab-impact"><div id="impact-body"></div></section>
  <section class="tab" id="tab-statements"><div id="stmts-body"></div></section>
</main>
<script>
const REPORT = %%REPORT%%;
const IMPACT = %%IMPACT%%;

const NODES = REPORT.nodes, OBJECTS = REPORT.objects, EDGES = REPORT.edges;
const outEdges = {}, inEdges = {};
for (const e of EDGES) {
  (outEdges[e.src] = outEdges[e.src] || []).push(e);
  (inEdges[e.dst] = inEdges[e.dst] || []).push(e);
}
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

const TF_LABEL = {direct:"直接", expression:"表达式", aggregate:"聚合", window:"窗口", setop:"集合"};
const KIND_LABEL = {value:"值", usage:"引用"};

function nodeLabel(id) {
  const n = NODES[id];
  if (!n) return `<span class="node-label">${esc(id)}</span>`;
  if (n.kind === "ephemeral")
    return `<span class="node-label node-eph" title="语句 ${esc(n.statement)} 的中间层">⚙ ${esc(id)}</span>`;
  return `<span class="node-label"><b>${esc(n.object)}</b>.${esc(n.column)}</span>`;
}
function edgeInfo(e) {
  const tf = `<span class="tf tf-${esc(e.transform)}">${TF_LABEL[e.transform] || esc(e.transform)}</span>`;
  const kind = e.kind === "usage" ? `<span class="kind-usage">[引用]</span>` : "";
  const loc = e.location ? `<span class="loc">${esc(e.location.file)}:${esc(e.location.line)}</span>` : "";
  const expr = e.expression ? `<code>${esc(e.expression)}</code>` : "";
  const amb = e.ambiguous ? `<span class="cycle-mark">[歧义]</span>` : "";
  return `<span class="edge-info">${tf}${kind}${expr}${loc}${amb}</span>`;
}
function treeHtml(rootId, dir) {
  const adj = dir === "down" ? outEdges : inEdges;
  function build(id, ancestors, depth) {
    const edges = adj[id] || [];
    if (!edges.length || depth > 15) return "";
    let html = "<ul class='tree'>";
    for (const e of edges) {
      const next = dir === "down" ? e.dst : e.src;
      const info = edgeInfo(e);
      if (ancestors.has(next)) {
        html += `<li><span class="cycle-mark">↩ 循环:</span> ${nodeLabel(next)} ${info}</li>`;
        continue;
      }
      const sub = build(next, new Set([...ancestors, next]), depth + 1);
      if (sub)
        html += `<li><details><summary>${nodeLabel(next)} ${info}</summary>${sub}</details></li>`;
      else
        html += `<li>${nodeLabel(next)} ${info}</li>`;
    }
    return html + "</ul>";
  }
  return build(rootId, new Set([rootId]), 0) || "<p class='hint'>（无）</p>";
}

// ---------------------------------------------------------------- 对象列表
function renderObjList(filter) {
  const el = document.getElementById("obj-list");
  const f = (filter || "").toLowerCase();
  let html = "";
  for (const [name, obj] of Object.entries(OBJECTS)) {
    const cols = obj.columns.filter(c => !f || name.includes(f) || c.name.includes(f));
    if (f && !cols.length) continue;
    const badge = `<span class="kind-badge kind-${obj.kind}">${obj.kind === "view" ? "视图" : "表"}</span>`;
    html += `<div class="obj"><div class="obj-head">${badge}${esc(name)}
             <span class="loc">${obj.source === "manifest" ? "清单" : "SQL"}</span></div><ul class="cols">`;
    for (const c of cols)
      html += `<li data-node="${esc(name)}.${esc(c.name)}">${esc(c.name)}
               <span class="col-type">${esc(c.type || "")}</span></li>`;
    html += "</ul></div>";
  }
  el.innerHTML = html || "<p class='hint'>无匹配对象</p>";
  el.querySelectorAll("li[data-node]").forEach(li =>
    li.addEventListener("click", () => selectColumn(li.dataset.node, li)));
}

let selected = null;
function selectColumn(id, li) {
  if (selected) selected.classList.remove("sel");
  selected = li; li.classList.add("sel");
  const n = NODES[id];
  const obj = OBJECTS[n ? n.object : ""];
  const col = obj && n ? obj.columns.find(c => c.name === n.column) : null;
  const det = document.getElementById("detail");
  det.innerHTML = `
    <h2 class="mono">${esc(id)}</h2>
    <div class="sub">${obj ? (obj.kind === "view" ? "视图" : "表") + " · " : ""}${col && col.type ? esc(col.type) : ""}</div>
    <h3>下游影响（该字段被谁使用）</h3>${treeHtml(id, "down")}
    <h3>上游来源（该字段来自哪里）</h3>${treeHtml(id, "up")}`;
}

// ---------------------------------------------------------------- 语句与问题
function renderStatements() {
  const el = document.getElementById("stmts-body");
  let html = "";
  for (const cyc of REPORT.cycles)
    html += `<div class="cycle-box">⚠ 循环依赖：${cyc.path.map(esc).join(" → ")}
             （涉及语句 ${cyc.statements.map(esc).join(", ")}）</div>`;
  html += `<table class="stmts"><tr><th>ID</th><th>位置</th><th>类型</th><th>目标</th>
           <th>状态</th><th>说明 / 原因</th></tr>`;
  for (const s of REPORT.statements) {
    if (s.kind === "meta") continue;
    const st = `<span class="st st-${s.status}">${s.status}</span>`;
    let note = "";
    for (const r of s.reasons) note += `<div class="reason">${esc(r)}</div>`;
    if (s.warnings.length)
      note += `<ul class="warn-list">${s.warnings.map(w => `<li>${esc(w.msg)}</li>`).join("")}</ul>`;
    if (!note) note = `<span class="loc">${esc(s.summary)}</span>`;
    html += `<tr><td class="mono">${esc(s.id)}</td><td class="loc">${esc(s.file)}:${esc(s.line)}</td>
             <td>${esc(s.kind)}</td><td class="mono">${esc(s.target)}</td><td>${st}</td><td>${note}</td></tr>`;
  }
  el.innerHTML = html + "</table>";
}

// ---------------------------------------------------------------- 影响分析
function renderImpact() {
  const el = document.getElementById("impact-body");
  if (!IMPACT) { el.innerHTML = "<p class='hint'>未提供影响分析结果（运行 impact 命令生成）。</p>"; return; }
  let html = "";
  for (const ch of IMPACT.changes) {
    const tag = `<span class="change-tag tag-${esc(ch.change)}">${esc(ch.change_label)}</span>`;
    const extra = ch.new_name ? ` → <b>${esc(ch.new_name)}</b>` : (ch.new_type ? ` → <b>${esc(ch.new_type)}</b>` : "");
    html += `<div class="change-card"><h3 class="mono">${esc(ch.table)}.${esc(ch.column)}${tag}${extra}</h3>`;
    if (!ch.found) html += `<div class="caveat">⚠ 该字段不在已知血缘中</div>`;
    for (const cav of ch.caveats) html += `<div class="caveat">⚠ ${esc(cav)}</div>`;
    const groups = {};
    for (const a of ch.affected) {
      if (a.truncated) { html += `<div class="caveat">⚠ ${esc(a.note)}</div>`; continue; }
      (groups[a.object] = groups[a.object] || []).push(a);
    }
    const n = Object.values(groups).reduce((s, g) => s + g.length, 0);
    html += `<div class="sub">受影响下游位置：${n} 个</div>`;
    for (const [obj, items] of Object.entries(groups)) {
      html += `<div class="aff-group"><div class="aff-obj mono">${esc(obj)}</div>`;
      for (const a of items) {
        const path = a.via.map(v => esc(v)).join(" → ");
        html += `<details><summary><span class="mono">${esc(a.column)}</span>
                 <span class="edge-info">[${a.edge_kind === "usage" ? "引用" : "值"}] ${esc(a.transform)}</span></summary>
                 <div style="padding:4px 0 4px 18px">
                 <div class="loc">${esc(a.location.file || "")}:${esc(a.location.line || "")}</div>
                 ${a.expression ? `<code>${esc(a.expression)}</code>` : ""}
                 ${a.direct_expression && a.direct_expression !== a.expression
                     ? `<div class="loc">被改字段的直接引用：<code>${esc(a.direct_expression)}</code></div>` : ""}
                 <div class="loc">路径：${path}</div></div></details>`;
      }
      html += "</div>";
    }
    html += "</div>";
  }
  el.innerHTML = html || "<p class='hint'>无变更</p>";
}

// ---------------------------------------------------------------- 初始化
function init() {
  const st = REPORT.stats;
  document.getElementById("stats").innerHTML =
    [`对象 ${st.objects}`, `节点 ${st.nodes}`, `边 ${st.edges}`,
     `语句 ${st.statements}（正常 ${st.ok} / 部分 ${st.partial} / 未解析 ${st.unresolved}）`,
     `循环 ${st.cycles}`].map(s => `<span>${s}</span>`).join("");
  if (!st.complete) {
    const b = document.getElementById("banner");
    b.hidden = false;
    b.textContent = `⚠ 血缘不完整：${st.unresolved} 条语句未解析、${st.partial} 条部分解析` +
      (st.cycles ? `、${st.cycles} 处循环依赖` : "") + "。下游影响分析可能遗漏，详见“语句与问题”。";
  }
  renderObjList("");
  document.getElementById("search").addEventListener("input", e => renderObjList(e.target.value));
  renderStatements();
  renderImpact();
  if (!IMPACT) document.getElementById("tabbtn-impact").hidden = true;
  document.querySelectorAll("nav.tabs button").forEach(btn =>
    btn.addEventListener("click", () => {
      document.querySelectorAll("nav.tabs button").forEach(b => b.classList.remove("active"));
      document.querySelectorAll("section.tab").forEach(s => s.classList.remove("active"));
      btn.classList.add("active");
      document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
    }));
}
init();
</script>
</body>
</html>
"""


def _safe_json(data) -> str:
    """嵌入 <script> 的 JSON：转义 </ 防止提前闭合标签。"""
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


def render_html(report: dict, impact: dict | None = None) -> str:
    meta = report.get("meta", {})
    meta_text = f"{meta.get('dialect', '')} · {meta.get('generated_at', '')}"
    return (_TEMPLATE
            .replace("%%META%%", meta_text)
            .replace("%%REPORT%%", _safe_json(report))
            .replace("%%IMPACT%%", _safe_json(impact) if impact else "null"))
