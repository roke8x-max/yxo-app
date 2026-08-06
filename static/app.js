/* YXO 订舱数据管理 · 前端逻辑 */
"use strict";

let META = { columns: [], users: [], company_field: "", groupable: [], followup_fields: [] };
let DATA = [];
let USER = "";
let STATE = {
  view: "grid",
  search: "",
  company: [],
  filters: {},      // 列名 -> [选项值]  (多选，列间 AND)
  contains: {},     // 列名 -> 文本 (包含，列间 AND)
  group_field: "",
  kanban_tabs: [],  // [{id,name,filters:{col:[val]}}]
  active_tab: null,
  sortRules: [],    // [{field, dir:'asc'|'desc'}]  多条件排序
  month: "",        // 当前激活的月份标签（YYYY-MM），""=全部
  trainType: "散舱",// 班列类型切换：散舱 / 专列 / 全部（专列与散舱分开展示）
  rowHeights: {},   // 行号(id) -> 行高px，仅当前用户可见
  hiddenFields: {}, // 字段名 -> true 表示在表格/看板中隐藏（仅当前用户）
  fieldOrder: [],   // 字段显示顺序（字段名数组；空=按配置默认顺序），仅当前用户
  frozen: 0,        // 冻结列数（从最左连续，含序号列）：0=不冻结，N=冻结最左 N 列
};
let _ctxRowId = null;   // 当前右键所在的行 id
let _saveTimer = null;
let _dataVersion = -1;  // 服务器数据版本号（多人协同：他人改动自动刷新）
let _newRowIds = new Set(); // 本次会话新增的行 id，在 loadRows/页面刷新后清除，避免被当前筛选条件立即隐藏
let _selectedRowIds = new Set();  // 序号列 Ctrl/Shift 多选的行 id 集合（行级，独立于单元格 _selection）
let _anchorRowId = null;          // Shift 连续选择的锚点行
let _activeCell = { id: null, field: null }; // 当前焦点单元格（网格粘贴锚点）
// —— Excel 式交互（2026-07-31）：选区模型 ——
let _selection = null;        // null | {r1,c1,r2,c2}（r/c = getView() 可见行列索引）
let _anchorCell = null;       // 选区锚点 {ri,ci}，Shift+单击从此扩展
let _multiSelection = new Set(); // Ctrl+单击多选：key=JSON.stringify([id,field])
let _dragging = false;
let _dragMoved = false;

function normSel(s){ return { r1:Math.min(s.r1,s.r2), c1:Math.min(s.c1,s.c2), r2:Math.max(s.r1,s.r2), c2:Math.max(s.c1,s.c2) }; }
function setSelection(r1,c1,r2,c2){ _selection = {r1,c1,r2,c2}; _multiSelection.clear(); }
function clearSelection(){ _selection = null; _anchorCell = null; _multiSelection.clear(); updateSelectionSum(); }

// 按当前 _selection 给 tbody 的 td 套 .cell-selected(锚点)/.cell-in-range(区域内) 高亮
function applyCellSelection() {
  const tbody = document.getElementById("body");
  if (!tbody) return;
  tbody.querySelectorAll("td.cell-selected, td.cell-in-range").forEach((td) => td.classList.remove("cell-selected","cell-in-range"));
  if (_selection) {
    const {r1,c1,r2,c2} = normSel(_selection);
    tbody.querySelectorAll("td.cell-edit[data-ri]").forEach((td) => {
      const ri = +td.dataset.ri, ci = +td.dataset.ci;
      if (ri>=r1 && ri<=r2 && ci>=c1 && ci<=c2) {
        const isAnchor = (td.dataset.id === String(_activeCell.id) && td.dataset.field === _activeCell.field);
        td.classList.add(isAnchor ? "cell-selected" : "cell-in-range");
      }
    });
  }
  // Ctrl+单击多选的高亮
  _multiSelection.forEach((key) => {
    try {
      const [id, field] = JSON.parse(key);
      const td = findCellTd(id, field);
      if (td) td.classList.add("cell-selected");
    } catch (_) {}
  });
  updateSelectionSum();
}

// 收集当前选区（矩形 + Ctrl 多选）的所有单元格，返回 [{id,field}]
function collectSelectedCells() {
  const map = new Map();
  const cols = userColumns();
  const view = getView();
  if (_selection) {
    const {r1,c1,r2,c2} = normSel(_selection);
    for (let ri = r1; ri <= r2; ri++) {
      const rec = view[ri];
      if (!rec) continue;
      for (let ci = c1; ci <= c2; ci++) {
        const c = cols[ci];
        if (c) map.set(JSON.stringify([rec.id, c.name]), { id: rec.id, field: c.name });
      }
    }
  }
  _multiSelection.forEach((key) => {
    try {
      const [id, field] = JSON.parse(key);
      map.set(key, { id, field });
    } catch (_) {}
  });
  return Array.from(map.values());
}

// 在底部状态栏显示选区数字求和（类似 Excel 状态栏）
function updateSelectionSum() {
  const bar = document.getElementById("pageBar");
  if (!bar) return;
  let span = document.getElementById("selectionSum");
  if (!span) {
    span = el("span", { class: "selection-sum", id: "selectionSum" });
    bar.appendChild(span);
  }
  const cells = collectSelectedCells();
  if (!cells.length) { span.textContent = ""; span.style.display = "none"; return; }
  let sum = 0, count = 0;
  cells.forEach(({ id, field }) => {
    const rec = DATA.find((x) => x.id === id);
    if (!rec) return;
    const v = String(rec[field] == null ? "" : rec[field]).replace(/,/g, "").trim();
    if (v === "") return;
    const n = Number(v);
    if (!isNaN(n) && isFinite(n)) { sum += n; count++; }
  });
  if (count) {
    span.textContent = `选中 ${cells.length} 个单元格 · 数字 ${count} 个 · 求和 ${sum.toLocaleString()}`;
  } else {
    span.textContent = `选中 ${cells.length} 个单元格`;
  }
  span.style.display = "";
}

// 按 (id, field) 找当前 DOM 里的数据单元格
function findCellTd(id, field) {
  const sel = '#body td.cell-edit[data-id="' + id + '"][data-field="' + (window.CSS ? CSS.escape(String(field)) : field) + '"]';
  return document.querySelector(sel);
}

// 打开单元格编辑器（双击 / 键入即编辑共用）。prefill：键入即编辑时预填的字符
function openCellEditor(r, c, td, prefill) {
  _activeCell = { id: r.id, field: c.name };
  const renderText = () => {
    const cur = r[c.name] == null ? "" : String(r[c.name]);
    td.innerHTML = "";
    const span = el("span", { class: "cell-text" }, cur);
    if (cur) span.title = cur;
    td.appendChild(span);
    if (c.name === "备注" && !String(r[c.name] || "").trim()) {
      td.appendChild(el("button", { class: "mini price-fab", text: "算价", onclick: (e) => { e.stopPropagation(); priceRow(r.id); } }));
    }
  };
  // §4.5：移除编辑器 paste 绑定，编辑中 Ctrl+V 只进当前格（原生）
  const editor = mkEditor(r, c);
  td.innerHTML = ""; td.appendChild(editor);
  if (editor.focus) { try { editor.focus(); } catch (_) {} }
  if (editor.select) { try { editor.select(); } catch (_) {} }
  if (prefill != null && editor.tagName === "INPUT") {
    editor.value = prefill;
    if (editor.setSelectionRange) { try { editor.setSelectionRange(prefill.length, prefill.length); } catch (_) {} }
    editor.dispatchEvent(new Event("input"));
  }
  editor.addEventListener("blur", () => setTimeout(renderText, 0));
  if (c.name === "备注") setTimeout(() => autoGrow(editor), 0);
}

// 选区 → TSV（回 Excel 可铺矩阵）。同时支持：①矩形 _selection ②Ctrl+单击多选 _multiSelection。
// 两者并存时取并集（外接网格），保证冻结列（position:sticky）单元格也能被正确复制。无选区返回 null。
function buildSelectionTSV() {
  const cols = userColumns();
  const view = getView();
  const cellVal = (ri, ci) => {
    const c = cols[ci]; const rec = view[ri];
    if (!c || !rec) return "";
    return String(rec[c.name] == null ? "" : rec[c.name]);
  };
  const pts = [];
  // ① 矩形选区 → 展开为全部格子坐标
  if (_selection) {
    const {r1,c1,r2,c2} = normSel(_selection);
    for (let ri = r1; ri <= r2; ri++)
      for (let ci = c1; ci <= c2; ci++) pts.push({ ri, ci });
  }
  // ② Ctrl+单击多选（散点）
  if (_multiSelection && _multiSelection.size) {
    _multiSelection.forEach((key) => {
      try {
        const [id, field] = JSON.parse(key);
        const ri = view.findIndex((x) => x.id === id);
        const ci = cols.findIndex((c) => c.name === field);
        if (ri >= 0 && ci >= 0) pts.push({ ri, ci });
      } catch (_) {}
    });
  }
  if (!pts.length) return null;
  const rmin = Math.min.apply(null, pts.map((p) => p.ri));
  const rmax = Math.max.apply(null, pts.map((p) => p.ri));
  const cmin = Math.min.apply(null, pts.map((p) => p.ci));
  const cmax = Math.max.apply(null, pts.map((p) => p.ci));
  const grid = {};
  pts.forEach((p) => { grid[p.ri + "_" + p.ci] = cellVal(p.ri, p.ci); });
  const rows = [];
  for (let r = rmin; r <= rmax; r++) {
    const line = [];
    for (let c = cmin; c <= cmax; c++) line.push(grid[r + "_" + c] != null ? grid[r + "_" + c] : "");
    rows.push(line.join("\t"));
  }
  return rows.join("\n");
}
let _loadingMore = false; // 无限滚动加载更多时的锁

/* ---------- 字段类型工具 ---------- */
function colDef(name) { return META.columns.find((c) => c.name === name); }
function d2input(v) {   // 任意 "2026/08/08"、"2026.08.08"、"2026\08\08" -> "2026-08-08"（input[type=date] 用）
  const s = String(v || "").trim().split(" ")[0];
  if (!s) return "";
  const m = s.replace(/[\\/]/g, "-").replace(/\./g, "-").match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
  return m ? m[1] + "-" + m[2].padStart(2, "0") + "-" + m[3].padStart(2, "0") : "";
}
function input2d(v) {   // 统一归一成 YYYY-MM-DD（库标准：横杠）再保存；手动输入的 / . \ 也一并归一
  if (!v) return "";
  const s = String(v).trim().replace(/[\\/]/g, "-").replace(/\./g, "-");
  const m = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
  return m ? m[1] + "-" + m[2].padStart(2, "0") + "-" + m[3].padStart(2, "0") : s;
}

/* 目的站等"可手输+筛选"的字段：用 datalist 组合框（既能选也能自由输入） */
function mkCombobox(r, c) {
  const cur = r[c.name] == null ? "" : String(r[c.name]);
  const dlId = "dl_" + c.name;
  let dl = document.getElementById(dlId);
  if (!dl) { dl = el("datalist", { id: dlId }); document.body.appendChild(dl); }
  dl.innerHTML = "";
  (c.options || []).forEach((o) => dl.appendChild(el("option", { value: o })));
  return el("input", {
    class: "combo-inp",
    list: dlId,
    value: cur,
    placeholder: "输入或选择",
    title: c.hint || "可直接输入，也可点右侧下拉从历史选项中筛选",
    onchange: (e) => saveCell(r.id, c.name, e.target.value),
  });
}

/* 日期字段：直接用原生 input[type=date]，浏览器自带日历按钮，避免自定义按钮换行/定位问题 */
function mkDateEditor(r, c) {
  const cur = r[c.name] == null ? "" : String(r[c.name]);
  const inp = el("input", {
    type: "date",
    class: "cell-date",
    value: d2input(cur),
    title: c.hint || "选择日期或手动输入",
    onchange: (e) => saveCell(r.id, c.name, input2d(e.target.value)),
  });
  return inp;
}

/* 按字段类型生成单元格编辑器（下拉/日期/数字/文本），表格和看板共用 */
function mkEditor(r, c) {
  const cur = r[c.name] == null ? "" : String(r[c.name]);
  if (c.type === "select") {
    if (c.name === "台账月份") {
      // 月份下拉要一次性显示全部月份（不受当前月份筛选影响），所以用普通 select
      const opts = monthsFromData();
      const sel = el("select", {
        class: "cell-sel" + (cur ? "" : " empty"),
        onchange: (e) => { saveCell(r.id, c.name, e.target.value); sel.classList.toggle("empty", !e.target.value); },
      });
      sel.appendChild(el("option", { value: "", text: "" }));
      opts.forEach((o) => sel.appendChild(el("option", { value: o, text: o })));
      if (cur && !opts.includes(cur)) sel.appendChild(el("option", { value: cur, text: cur + "（历史值）" }));
      sel.value = cur;
      return sel;
    }
    if (c.free_text) return mkCombobox(r, c);
    const sel = el("select", {
      class: "cell-sel" + (cur ? "" : " empty"),
      onchange: (e) => { saveCell(r.id, c.name, e.target.value); sel.classList.toggle("empty", !e.target.value); },
    });
    sel.appendChild(el("option", { value: "", text: "" }));
    (c.options || []).forEach((o) => sel.appendChild(el("option", { value: o, text: o })));
    if (cur && !(c.options || []).includes(cur)) sel.appendChild(el("option", { value: cur, text: cur + "（历史值）" }));
    sel.value = cur;
    return sel;
  }
  if (c.type === "date") return mkDateEditor(r, c);
  if (c.type === "number") {
    return el("input", {
      type: "number", step: "any", class: "cell-num", value: cur,
      onchange: (e) => saveCell(r.id, c.name, e.target.value),
      oninput: (e) => markPending(r.id, c.name, e.target.value),
    });
  }
  // 备注字段需要多行输入：Enter 手动换行，超出宽度自动换行
  if (c.name === "备注") {
    return el("textarea", {
      class: "cell-text cell-remark",
      value: cur, title: c.hint || "Enter 换行，自动保存",
      rows: 1,
      onchange: (e) => saveCell(r.id, c.name, e.target.value),
      oninput: (e) => { markPending(r.id, c.name, e.target.value); autoGrow(e.target); },
    });
  }
  return el("input", {
    value: cur, title: c.hint || "",
    onchange: (e) => saveCell(r.id, c.name, e.target.value),
    oninput: (e) => markPending(r.id, c.name, e.target.value),
  });
}
function autoGrow(ta) {
  ta.style.height = "auto";
  ta.style.height = Math.max(30, ta.scrollHeight) + "px";
}

/* 列默认宽度：按字段类型/字段名给一个合适的初始宽度 */
function defaultColWidth(c) {
  if (c.name === "备注") return 170;
  if (c.name === "目的站") return 130;
  if (c.name === "箱号") return 120;
  if (c.name === "本地货源公司") return 120;
  const t = c.type;
  if (t === "date") return 120;
  if (t === "number") return 90;
  if (t === "select") return 82;
  return 120;
}
/* 列宽拖拽把手：拖动表头右缘调列宽，宽度只存当前用户（不影响他人） */
function makeResizer(th, name) {
  const rz = el("div", { class: "col-resizer", title: "拖动调整列宽（仅自己可见）" });
  rz.addEventListener("mousedown", (e) => {
    e.preventDefault(); e.stopPropagation();
    const startX = e.clientX;
    const startW = th.getBoundingClientRect().width;
    // 找到对应的 <col>，拖动时同步更新（colgroup 才是列宽的真正来源）
    const cols = [...document.querySelectorAll("#colgroup col")];
    const thSiblings = [...th.parentNode.children];
    const colIdx = thSiblings.indexOf(th);
    const col = cols[colIdx];
    document.body.style.cursor = "col-resize";
    function onMove(ev) {
      const w = Math.max(40, Math.round(startW + (ev.clientX - startX)));
      if (col) { col.style.width = w + "px"; col.style.minWidth = w + "px"; }
      th.style.width = w + "px";
    }
    function onUp() {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      document.body.style.cursor = "";
      const w = Math.round(th.getBoundingClientRect().width);
      STATE.colWidths = STATE.colWidths || {};
      STATE.colWidths[name] = w;
      saveStateSoon();
      // 列宽变化会影响所有冻结列的 left 偏移，重新渲染表头和 tbody
      buildStatic(); renderBody();
    }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
  return rz;
}

/* ---------- 工具 ---------- */
function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  attrs = attrs || {};
  for (const k in attrs) {
    if (k === "class") e.className = attrs[k];
    else if (k === "text") e.textContent = attrs[k];
    else if (k === "html") e.innerHTML = attrs[k];
    else if (k === "value") continue;  // value 在子节点挂载后用 DOM 属性赋值（textarea/select 用 setAttribute 无效）
    else if (k.startsWith("on") && typeof attrs[k] === "function") e.addEventListener(k.slice(2), attrs[k]);
    else if (k === "checked" || k === "disabled" || k === "selected") e[k] = !!attrs[k];  // 布尔属性必须用 DOM 属性赋值，setAttribute 会导致永远勾选
    else if (attrs[k] != null) e.setAttribute(k, attrs[k]);
  }
  kids.flat().forEach((c) => { if (c == null) return; e.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
  if ("value" in attrs && attrs.value != null) e.value = attrs.value;  // textarea/input/select 通用；select 需在 option 挂载后设置
  return e;
}
function distinct(col) {
  const s = new Set();
  DATA.forEach((r) => { const v = r[col]; if (v != null && String(v).trim() !== "") s.add(String(v)); });
  return [...s].sort();
}
function num(v) { return parseFloat(String(v).replace(/[^\d.\-]/g, "")) || 0; }
function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(t._t); t._t = setTimeout(() => t.classList.add("hidden"), 1800);
}
function activeTab() {
  if (!STATE.active_tab) return null;
  return STATE.kanban_tabs.find((t) => t.id === STATE.active_tab) || null;
}
/* "待跟进"：仅看报放单——已上传即算完成，其余状态均算待跟进（2026-07-29 用户拍板）。
   退舱记录不需要跟进（2026-07-29 补充：退舱直接排除）。 */
function isPending(r) {
  if (String(r["状态"] || "").trim() === "退舱") return false;
  return String(r["报放单"] || "").trim() !== "已上传";
}

/* 多条件排序：按 STATE.sortRules 排序；无规则时按服务端 order_idx（手动顺序） */
function applySort(rows) {
  console.log("[sort] applySort called, rules=", JSON.stringify(STATE.sortRules), "rows=", rows.length);
  if (!STATE.sortRules || !STATE.sortRules.length) return rows;
  const rules = STATE.sortRules;
  // 日期/月份正则：YYYY-MM-DD 或 YYYY-MM（ISO格式字符串字典序即可正确排序）
  const _reDate = /^\d{4}-\d{2}(-\d{2})?$/;
  return rows.slice().sort((a, b) => {
    for (const rule of rules) {
      let va = a[rule.field], vb = b[rule.field];
      const sa = String(va).trim(), sb = String(vb).trim();
      let cmp;
      // ① ISO日期：字符串字典序即可正确排序（YYYY-MM-DD）
      if (_reDate.test(sa) && _reDate.test(sb)) {
        cmp = sa.localeCompare(sb);
      } else {
        // ② 纯数值列：parseFloat 提取数字比较
        const na = parseFloat(sa.replace(/[^\d.\-]/g, ""));
        const nb = parseFloat(sb.replace(/[^\d.\-]/g, ""));
        if (!isNaN(na) && !isNaN(nb) && sa !== "" && sb !== "") {
          cmp = na - nb;
        } else {
          cmp = (sa || "").localeCompare(sb || "", "zh");
        }
      }
      if (cmp !== 0) return rule.dir === "desc" ? -cmp : cmp;
    }
    return 0;
  });
}

/* 取得当前视图行：先筛选，再排序 */
function getView() {
  let rows = DATA.filter(passFilter);
  // 班列类型切换：散舱 / 专列 / 全部（全部=不过滤）
  if (STATE.trainType && STATE.trainType !== "全部") {
    rows = rows.filter((r) => (r["班列类型"] || "散舱") === STATE.trainType);
  }
  return applySort(rows);
}

/* 列头点击排序：单击循环 无→升→降→无；Shift+单击 叠加多条件 */
function onHeaderSort(name, e) {
  const multi = e.shiftKey;
  const idx = STATE.sortRules.findIndex((r) => r.field === name);
  if (!multi) {
    if (idx === 0 && STATE.sortRules.length === 1) {
      STATE.sortRules = STATE.sortRules[0].dir === "asc" ? [{ field: name, dir: "desc" }] : [];
    } else {
      STATE.sortRules = [{ field: name, dir: "asc" }];
    }
  } else {
    if (idx === -1) STATE.sortRules.push({ field: name, dir: "asc" });
    else if (STATE.sortRules[idx].dir === "asc") STATE.sortRules[idx].dir = "desc";
    else STATE.sortRules.splice(idx, 1);
  }
  saveStateSoon(); refreshSortBadges(); renderActive();
}

/* 更新表头排序角标（↑/↓，多条件时显示序号） */
function refreshSortBadges() {
  document.querySelectorAll(".sort-badge").forEach((b) => {
    const name = b.getAttribute("data-col");
    const i = STATE.sortRules.findIndex((r) => r.field === name);
    if (i === -1) { b.textContent = ""; return; }
    const arrow = STATE.sortRules[i].dir === "asc" ? "↑" : "↓";
    b.textContent = STATE.sortRules.length > 1 ? (i + 1) + arrow : arrow;
  });
}

function monthOf(r) {
  // 优先用“台账月份”（可手动改归属），为空时回退到发班时间所在月
  const t = String(r["台账月份"] || "").trim();
  if (/^\d{4}-\d{2}$/.test(t)) return t;
  const s = String(r["发班时间"] || "").trim();
  const m = s.slice(0, 7).replace(/\//g, "-");
  return /^\d{4}-\d{2}$/.test(m) ? m : "";
}
/* 当前用户实际可见、且按 fieldOrder 排序后的字段列表（字段管理：隐藏 + 排序） */
function userColumns() {
  const order = STATE.fieldOrder || [];
  const hidden = STATE.hiddenFields || {};
  let cols;
  if (order && order.length) {
    const inOrder = order.filter((n) => META.columns.some((c) => c.name === n));
    const rest = META.columns.filter((c) => !inOrder.includes(c.name));
    cols = inOrder.map((n) => META.columns.find((c) => c.name === n)).concat(rest);
  } else {
    cols = META.columns.slice();
  }
  // 按班列类型过滤字段：带 trainTypes 的字段只在其列出的类型视图可见（"全部"=两种都显示）
  const tt = STATE.trainType || "全部";
  return cols.filter((c) => {
    if (hidden[c.name] || c.internal) return false;
    if (c.trainTypes && c.trainTypes.length) {
      return c.trainTypes.indexOf(tt) >= 0;
    }
    return true;
  });
}
function monthsFromData() {
  const s = new Set();
  DATA.forEach((r) => { const m = monthOf(r); if (m) s.add(m); });
  return [...s].sort().reverse();  // 降序：最新月份在前
}
let _selYear = "";   // 月份条当前选中的年份（跨年支持；空 = 自动取最新年/当前筛选月所在年）
function renderMonthTabs() {
  const bar = document.getElementById("monthBar");
  if (!bar) return;
  // #140：Dashboard 视图、以及专列表格视图 不展示全局月份条（专列改用 Dashboard 自带年份/月份下拉）
  const hideMonthBar = (STATE.view === "dash") || (STATE.view === "grid" && STATE.trainType === "专列");
  if (hideMonthBar) { bar.classList.add("hidden"); bar.innerHTML = ""; return; }
  bar.classList.remove("hidden");
  bar.innerHTML = "";
  const months = monthsFromData();
  const years = [...new Set(months.map((m) => m.slice(0, 4)))].sort().reverse();

  // 年份选项：数据里只有一个年份时不显示，跨年后自动出现
  if (years.length > 1) {
    if (!_selYear || !years.includes(_selYear)) {
      _selYear = (STATE.month && years.includes(STATE.month.slice(0, 4))) ? STATE.month.slice(0, 4) : years[0];
    }
    const ysel = el("select", { class: "year-sel", title: "选择年份" });
    years.forEach((y) => ysel.appendChild(el("option", { value: y, text: y + "年" })));
    ysel.value = _selYear;
    ysel.onchange = (e) => { _selYear = e.target.value; renderMonthTabs(); };
    bar.appendChild(ysel);
  } else {
    _selYear = years[0] || "";
  }

  const allBtn = el("span", { class: "month-chip" + (STATE.month ? "" : " active"), text: "全部" });
  allBtn.onclick = () => setMonth("");
  bar.appendChild(allBtn);
  months.filter((m) => !_selYear || m.slice(0, 4) === _selYear).forEach((m) => {
    const label = m.slice(5).replace(/^0/, "") + "月";
    const btn = el("span", { class: "month-chip" + (STATE.month === m ? " active" : ""), text: label });
    btn.title = m;
    btn.onclick = () => setMonth(m);
    bar.appendChild(btn);
  });
}
function setMonth(m) {
  STATE.month = m;
  STATE.page = 0;   // 月份切换后回到第一页
  saveStateSoon();
  renderMonthTabs();
  renderActive();
}

function passFilter(r) {
  // 本次会话新增的行：先让用户能看见并编辑，等下次 loadRows/页面刷新再受筛选条件约束
  if (_newRowIds.has(r.id)) return true;
  if (STATE.search) {
    const q = STATE.search.trim().toLowerCase();
    if (q && !Object.values(r).some((v) => v != null && String(v).toLowerCase().includes(q))) return false;
  }
  if (STATE.company.length && !STATE.company.includes(r[META.company_field] || "")) return false;
  if (STATE.month && monthOf(r) !== STATE.month) return false;
  for (const col in STATE.filters) {
    const arr = STATE.filters[col];
    if (arr && arr.length && !arr.includes(r[col] || "")) return false;
  }
  for (const col in STATE.contains) {
    const t = (STATE.contains[col] || "").trim();
    if (t && !String(r[col] || "").includes(t)) return false;
  }
  const tab = activeTab();
  if (tab) {
    if (tab.company && tab.company.length && !tab.company.includes(r[META.company_field] || "")) return false;
    for (const col in (tab.filters || {})) {
      const arr = tab.filters[col];
      if (arr && arr.length && !arr.includes(r[col] || "")) return false;
    }
  }
  return true;
}
/* 初始化默认月份：当前月有数据则用当前月，否则用最新月 */
function pickDefaultMonth() {
  const months = monthsFromData();
  if (!months.length) return "";
  const now = new Date();
  const cur = now.getFullYear() + "-" + String(now.getMonth() + 1).padStart(2, "0");
  return months.includes(cur) ? cur : months[0];
}

/* ---------- 状态存取 ---------- */
function saveStateSoon() {
  clearTimeout(_saveTimer);
  _saveTimer = setTimeout(saveState, 400);
}
function saveState() {
  fetch("api/state", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user: USER, state: STATE }),
  }).catch(() => {});
}

/* 取某列当前宽度（含用户拖拽后的值），seq 用 __seq__ */
function getColWidth(name, cols) {
  if (name === "__seq__") return (STATE.colWidths && STATE.colWidths["__seq__"]) || 42;
  const c = cols.find((x) => x.name === name);
  return (c && STATE.colWidths && STATE.colWidths[c.name]) || (c ? defaultColWidth(c) : 80);
}

/* ---------- 冻结列 ---------- */
/* 某显示列（0=序号，1..N=数据列）的 sticky left 偏移 = 它左边所有冻结列宽度之和 */
function frozenLeft(displayIdx, cols) {
  const names = ["__seq__"].concat(cols.map((c) => c.name));
  let left = 0;
  for (let k = 0; k < displayIdx; k++) left += getColWidth(names[k], cols);
  return left;
}
/* 给单元格（th/td）应用冻结样式：position:sticky + left 偏移 + frozen 类；最后一根冻结列加分隔边 */
function applyFrozen(elm, displayIdx, cols) {
  const frozen = STATE.frozen || 0;
  if (displayIdx < frozen) {
    elm.classList.add("frozen");
    elm.style.position = "sticky";
    elm.style.left = frozenLeft(displayIdx, cols) + "px";
    if (displayIdx === frozen - 1) elm.classList.add("frozen-last");
  }
}

/* ---------- 渲染：表头 / 筛选行（只建一次） ---------- */
function buildStatic() {
  const tbl = document.getElementById("tbl");
  const head = document.getElementById("head"); head.innerHTML = "";
  const fr = document.getElementById("filterRow"); fr.innerHTML = "";
  const cols = userColumns();

  // 用 <colgroup> 统一所有列宽：thead / filterRow / tbody 共享同一宽度，避免错位
  let cg = document.getElementById("colgroup");
  if (!cg) { cg = el("colgroup", { id: "colgroup" }); tbl.insertBefore(cg, tbl.firstChild); }
  cg.innerHTML = "";
  ["__seq__"].concat(cols.map((c) => c.name)).forEach((name) => {
    const w = getColWidth(name, cols);
    cg.appendChild(el("col", { style: "width:" + w + "px; min-width:" + w + "px;" }));
  });

  // 序号表头
  const seqTh = el("th", { class: "seq", text: "序号" });
  seqTh.setAttribute("data-display", "0");
  seqTh.appendChild(makeResizer(seqTh, "__seq__"));
  applyFrozen(seqTh, 0, cols);
  head.appendChild(seqTh);

  cols.forEach((c, ci) => {
    const th = el("th", { class: c.kind, "data-col": c.name });
    th.setAttribute("data-display", String(ci + 1));
    th.appendChild(el("span", {
      class: "colname", text: c.label || c.name,
      title: (c.hint ? c.hint + "。" : "") + "点击排序（按住 Shift 点击可叠加多条件）· 右键表头可冻结此列",
      onclick: (e) => { e.stopPropagation(); onHeaderSort(c.name, e); },
    }));
    th.appendChild(el("span", { class: "sort-badge", "data-col": c.name, text: "" }));
    th.appendChild(el("span", {
      class: "funnel", text: "⏷", title: "筛选 / 选项",
      onclick: (e) => { e.stopPropagation(); openFilter(c.name, e); },
    }));
    th.appendChild(makeResizer(th, c.name));
    applyFrozen(th, ci + 1, cols);
    head.appendChild(th);
  });

  const seqFilterTh = el("th", { class: "seq" });
  applyFrozen(seqFilterTh, 0, cols);
  fr.appendChild(seqFilterTh);
  cols.forEach((c, ci) => {
    const inp = el("input", {
      type: "text", placeholder: "包含", "data-col": c.name,
      value: STATE.contains[c.name] || "",
      oninput: (e) => { STATE.contains[c.name] = e.target.value; saveStateSoon(); renderActive(); },
      onblur: (e) => {
        const v = e.target.value.trim();
        if (v !== e.target.value) { e.target.value = v; STATE.contains[c.name] = v; saveStateSoon(); }
      },
    });
    const clr = el("span", {
      class: "fclear", text: "×", title: "清除本列筛选",
      onclick: (e) => {
        e.stopPropagation();
        STATE.contains[c.name] = "";
        delete STATE.filters[c.name];
        inp.value = "";
        STATE.page = 0;
        saveStateSoon(); renderActive();
      },
    });
    const fth = el("th", { "data-col": c.name }, inp, clr);
    applyFrozen(fth, ci + 1, cols);
    fr.appendChild(fth);
  });

  // 用户下拉
  const us = document.getElementById("userSel"); us.innerHTML = "";
  META.users.forEach((u) => us.appendChild(el("option", { value: u, text: u })));
  us.value = USER;
  us.onchange = () => { USER = us.value; localStorage.setItem("yxo_user", USER); loadUserState().then(renderAll); };

  // 公司弹层列表
  const cl = document.getElementById("companyList"); cl.innerHTML = "";
  distinct(META.company_field).forEach((c) => {
    const cb = el("input", {
      type: "checkbox", checked: STATE.company.includes(c),
      onchange: () => {
        const arr = STATE.company;
        if (cb.checked) { if (!arr.includes(c)) arr.push(c); }
        else STATE.company = arr.filter((x) => x !== c);
        saveStateSoon(); renderActive(); updateCompanyBtn();
      },
    });
    const onlyBtn = el("a", {
      href: "javascript:void(0)", text: "仅筛选此项", class: "only-this",
      onclick: (ev) => {
        ev.preventDefault(); ev.stopPropagation();
        STATE.company = [c];
        saveStateSoon(); buildStatic(); updateCompanyBtn(); renderActive();
      },
    });
    cl.appendChild(el("label", {}, cb, el("span", { class: "name", text: c }), onlyBtn));
  });

  // 看板分组字段
  const gf = document.getElementById("groupField"); gf.innerHTML = "";
  META.groupable.forEach((g) => { const gl = (META.columns.find((c) => c.name === g) || {}).label || g; gf.appendChild(el("option", { value: g, text: gl })); });
  gf.value = STATE.group_field || META.groupable[0];
  gf.onchange = () => { STATE.group_field = gf.value; saveStateSoon(); renderBoard(); };

  refreshSortBadges();

  // 把字段名行实际高度写入 CSS 变量，让搜索行精确粘在字段名行下方（避免两行重叠）
  const gridScroll = document.querySelector(".grid-scroll");
  if (gridScroll) gridScroll.style.setProperty("--head-height", (head.offsetHeight || 34) + "px");

  buildDatalists();
}

function updateCompanyBtn() {
  const b = document.getElementById("companyBtn");
  b.textContent = STATE.company.length ? `公司(${STATE.company.length}) ▾` : "公司 ▾";
  b.classList.toggle("active", STATE.company.length > 0);
}

/* 高亮正在筛选的列：表头 + 筛选框都要明显，一眼看出哪几列在过滤。
   两类筛选都算：STATE.contains(表头输入框「包含」) 与 STATE.filters(漏斗勾选)。
   注意按 data-col 匹配，不能按表头文字——「开票子公司名称」显示为「负责公司」，文字匹配会失效。 */
function highlightFilteredCols() {
  let active = 0;
  const heads = document.querySelectorAll("#head th[data-col]");
  const fcells = document.querySelectorAll("#filterRow th[data-col]");
  heads.forEach((th) => th.classList.remove("filtered-col"));
  fcells.forEach((th) => th.classList.remove("filtered-col"));

  (META.columns || []).forEach((c) => {
    const hasText = String(STATE.contains[c.name] || "").trim() !== "";
    const hasPick = (STATE.filters[c.name] || []).length > 0;
    if (!hasText && !hasPick) return;
    active++;
    const th = document.querySelector('#head th[data-col="' + cssEsc(c.name) + '"]');
    if (th) {
      th.classList.add("filtered-col");
      const fn = th.querySelector(".funnel");
      if (fn) fn.title = hasPick ? "已选 " + STATE.filters[c.name].length + " 项（点击修改）" : "筛选 / 选项";
    }
    const fth = document.querySelector('#filterRow th[data-col="' + cssEsc(c.name) + '"]');
    if (fth) fth.classList.add("filtered-col");
  });

  // 工具栏上给个总数提示 + 一键清空
  const badge = document.getElementById("filterBadge");
  if (badge) {
    badge.textContent = active ? "筛选中 " + active + " 列 ✕" : "";
    badge.style.display = active ? "" : "none";
  }
}
function cssEsc(s) { return String(s).replace(/["\\]/g, "\\$&"); }

function clearAllColFilters() {
  STATE.contains = {};
  STATE.filters = {};
  STATE.page = 0;
  saveStateSoon();
  buildStatic();
  renderActive();
  toast("已清空所有列筛选");
}

/* ---------- 渲染：表格 ---------- */
function renderBody(reset = true) {
  highlightFilteredCols();

  const body = document.getElementById("body");
  const rows = getView();
  document.getElementById("total").textContent = rows.length; // 当前筛选条件下的行数
  document.getElementById("pending").textContent = rows.filter(isPending).length;
  const rh = STATE.rowHeights || {};

  // —— 分页：默认只渲染第 0 页；滚动到底自动追加下一页 ——
  const _PAGE_ALL = 900000;   // 哨兵：每页“全部”
  const ps = (STATE.pageSize && STATE.pageSize > 0 && STATE.pageSize < _PAGE_ALL) ? STATE.pageSize : _PAGE_ALL;
  const _all = (ps >= _PAGE_ALL) || (ps >= rows.length);
  const pageCount = _all ? 1 : Math.ceil(rows.length / ps);

  if (reset) {
    STATE.page = 0;
    body.innerHTML = "";
  }
  if (STATE.page > pageCount - 1) STATE.page = pageCount - 1;
  if (STATE.page < 0) STATE.page = 0;
  const start = STATE.page * ps;
  const pageRows = _all ? rows : rows.slice(start, start + ps);

  const cols = userColumns();
  pageRows.forEach((r, i) => {
    const tr = el("tr", { "data-id": r.id });
    if (rh[r.id]) tr.style.minHeight = rh[r.id] + "px";
    if (String(r["状态"] || "").trim() === "退舱") tr.classList.add("row-cancel");
    // 点击任意位置选中整行（高亮定位，便于左右滚动时跟踪）；点数据单元格则由单元格自身处理（选单元格，不整行）
    tr.addEventListener("click", (e) => { if (e.target.closest("td.cell-edit")) return; selectRow(r.id); });
    // 序号格 + 行高拖拽把手（拖动调整该行高度，仅自己可见）
    // 列宽统一由 thead 的 table-layout:fixed 控制，tbody 不再单独设 width
    const seqTd = el("td", { class: "seq" }, String(start + i + 1));
    applyFrozen(seqTd, 0, cols);
    const rz = el("div", { class: "row-resizer", title: "拖动调整行高（仅自己可见）" });
    rz.addEventListener("mousedown", (e) => {
      e.preventDefault(); e.stopPropagation();
      const startY = e.clientY;
      const startH = tr.getBoundingClientRect().height;
      document.body.style.cursor = "row-resize";
      const onMove = (ev) => { tr.style.minHeight = Math.max(28, Math.round(startH + (ev.clientY - startY))) + "px"; };
      const onUp = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        document.body.style.cursor = "";
        const h = Math.round(tr.getBoundingClientRect().height);
        STATE.rowHeights = STATE.rowHeights || {}; STATE.rowHeights[r.id] = h;
        saveStateSoon();
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });
    seqTd.appendChild(rz);
    seqTd.addEventListener("click", (e) => {
      if (e.target.closest(".row-resizer")) return;   // 拖拽行高把手不触发
      e.stopPropagation();                             // 不触发 tr 的 selectRow(单行)
      if (e.ctrlKey || e.metaKey) {
        if (_selectedRowIds.has(r.id)) _selectedRowIds.delete(r.id);
        else _selectedRowIds.add(r.id);
        _anchorRowId = r.id;
      } else if (e.shiftKey && _anchorRowId != null) {
        const view = getView();
        const a = view.findIndex((x) => x.id === _anchorRowId);
        const b = view.findIndex((x) => x.id === r.id);
        if (a >= 0 && b >= 0) {
          const [lo, hi] = a < b ? [a, b] : [b, a];
          for (let k = lo; k <= hi; k++) _selectedRowIds.add(view[k].id);
        }
      } else {
        _selectedRowIds.clear();
        _selectedRowIds.add(r.id);
        _anchorRowId = r.id;
      }
      applyRowSelection();
    });
    tr.appendChild(seqTd);
    // 数据单元格：点单元格任意位置都能聚焦编辑器
    cols.forEach((c, ci) => {
      const gidx = start + i;   // getView() 中的全局可见行索引（选区用）
      const tdCls = c.kind + " cell-edit" + (c.name === "备注" ? " td-remark" : "");
      const td = el("td", { class: tdCls, "data-id": r.id, "data-field": c.name, "data-ri": gidx, "data-ci": ci });
      // 平时显示纯文本；点击单元格才进入编辑态（不一直显示输入框）
      const renderText = () => {
        let cur = r[c.name] == null ? "" : String(r[c.name]);
        // 日期字段显示时统一归一为 YYYY-MM-DD（防止旧数据残留 \ / . 等格式）
        if (c.type === "date" && cur) cur = input2d(cur);
        td.innerHTML = "";
        const span = el("span", { class: "cell-text" }, cur);
        if (cur) span.title = cur;
        td.appendChild(span);
        if (c.name === "单箱价格" && !String(r[c.name] || "").trim()) {
          // 算价按钮悬浮在右上角，不撑高单元格（保证所有行行高一致）
          td.appendChild(el("button", { class: "mini price-fab", text: "算价", onclick: (e) => { e.stopPropagation(); priceRow(r.id); } }));
        }
      };
      renderText();
      // §4.1/§4.3：单击=仅选中（设定锚点+选区，不开编辑器）；双击/键入=编辑
      // Ctrl/Cmd+单击 = Excel 式多选（切换单个单元格）
      td.addEventListener("click", (e) => {
        if (td.querySelector("input,select,textarea")) return; // 编辑中→原生聚焦，不动选区
        if (_dragMoved) { _dragMoved = false; return; }         // 拖拽结束的附带 click 忽略
        const ri = +td.dataset.ri, ci = +td.dataset.ci;
        _activeCell = { id: r.id, field: c.name };
        if (e.ctrlKey || e.metaKey) {
          const key = JSON.stringify([r.id, c.name]);
          if (_multiSelection.has(key)) _multiSelection.delete(key);
          else _multiSelection.add(key);
          _anchorCell = { ri, ci };
          applyCellSelection();
          return;
        }
        if (e.shiftKey && _anchorCell) {
          setSelection(_anchorCell.ri, _anchorCell.ci, ri, ci);
        } else {
          _anchorCell = { ri, ci };
          setSelection(ri, ci, ri, ci);
        }
        applyCellSelection();
      });
      td.addEventListener("dblclick", () => {
        if (td.querySelector("input,select,textarea")) return;
        openCellEditor(r, c, td);
      });
      // 拖拽框选（§4.3）+ Shift 扩展 + 编辑态点其他格先提交退出
      td.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;                              // 仅左键
        if (e.ctrlKey || e.metaKey) return;                     // Ctrl/Cmd+单击多选交给 click 处理，不拖拽
        if (td.querySelector("input,select,textarea")) return;  // 编辑中→原生聚焦，不动选区
        // 编辑态下点击其他单元格：先提交并关闭当前编辑器（视为失焦）
        const openInput = document.querySelector(
          "#body td.cell-edit input, #body td.cell-edit select, #body td.cell-edit textarea"
        );
        if (openInput) {
          const openTd = openInput.closest("td.cell-edit");
          if (openTd && openTd !== td) { try { openInput.blur(); } catch (_) {} }
        }
        e.preventDefault();                                     // 防文本选中
        _dragging = true;
        const ri = +td.dataset.ri, ci = +td.dataset.ci;
        _activeCell = { id: r.id, field: c.name };
        let sri, sci;
        if (e.shiftKey) {                                       // 扩展：以既有锚点为起点
          const a = _anchorCell || { ri, ci };
          sri = a.ri; sci = a.ci;
        } else {                                                // 新选区：落点为锚点
          _anchorCell = { ri, ci };
          sri = ri; sci = ci;
        }
        setSelection(sri, sci, ri, ci);
        applyCellSelection();
        let moved = false;
        const onMove = (ev) => {
          if (!_dragging) return;
          const tgt = document.elementFromPoint(ev.clientX, ev.clientY);
          const cell = tgt && tgt.closest && tgt.closest("td.cell-edit[data-ri]");
          if (!cell) return;
          const cri = +cell.dataset.ri, cci = +cell.dataset.ci;
          if (cri !== ri || cci !== ci) moved = true;
          setSelection(sri, sci, cri, cci);
          applyCellSelection();
        };
        const onUp = () => {
          _dragging = false; _dragMoved = moved;
          document.removeEventListener("mousemove", onMove);
          document.removeEventListener("mouseup", onUp);
        };
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
      });
      applyFrozen(td, ci + 1, cols);
      tr.appendChild(td);
    });
    body.appendChild(tr);
  });

  // 重新应用行选中高亮（re-render 后保持选中状态，便于滚动追踪）
  if (_selectedRowId != null) {
    const sel = body.querySelector('tr[data-id="' + _selectedRowId + '"]');
    if (sel) sel.classList.add("row-selected");
  }
  // 重新应用单元格选区高亮（Excel 式交互）
  applyCellSelection();

  // 筛选后无数据时给出明确提示，避免误以为"没有数据"（仅在重置/首次渲染时显示）
  if (reset && !rows.length && DATA.length) {
    const tr = el("tr", {});
    const td = el("td", { colspan: String(userColumns().length + 1), class: "muted" });
    td.style.textAlign = "center"; td.style.padding = "24px";
    td.appendChild(el("span", { text: "当前筛选条件下没有匹配的记录（共 " + DATA.length + " 条数据）。" }));
    const a = el("a", { href: "javascript:void(0)", text: " 一键清除全部筛选" });
    a.style.color = "#0f6e56";
    a.onclick = () => {
      STATE.filters = {}; STATE.contains = {}; STATE.company = []; STATE.search = ""; STATE.month = "";
      document.getElementById("search") && (document.getElementById("search").value = "");
      STATE.active_tab = null;
      saveStateSoon(); buildStatic(); updateCompanyBtn(); renderTabs(); renderAll();
    };
    td.appendChild(a);
    tr.appendChild(td);
    body.appendChild(tr);
  }
  renderPageBar();   // 在表格底部渲染分页/加载控件
}

/* 无限滚动：追加下一页到当前表格 */
function loadMoreRows() {
  if (_loadingMore) return;
  const rows = getView();
  const _PAGE_ALL = 900000;
  const ps = (STATE.pageSize && STATE.pageSize > 0 && STATE.pageSize < _PAGE_ALL) ? STATE.pageSize : _PAGE_ALL;
  if (ps >= _PAGE_ALL || ps >= rows.length) return; // 全部模式或数据量少时不触发
  const pageCount = Math.ceil(rows.length / ps);
  if (STATE.page >= pageCount - 1) return;
  _loadingMore = true;
  STATE.page++;
  renderBody(false);
  setTimeout(() => { _loadingMore = false; }, 300);
}

/* ---------- 渲染：看板 ---------- */
function renderBoard() {
  const gf = STATE.group_field || META.groupable[0];
  const rows = getView();
  const groups = {};
  rows.forEach((r) => { const k = r[gf] || "(空)"; (groups[k] = groups[k] || []).push(r); });
  const board = document.getElementById("board"); board.innerHTML = "";
  if (!rows.length) {
    board.appendChild(el("div", { class: "muted", text: "当前筛选条件下没有记录。" }));
    return;
  }
  Object.keys(groups).sort().forEach((k) => {
    const col = el("div", { class: "bcol" });
    col.appendChild(el("h3", {}, el("span", { text: k }), el("span", { class: "pill", text: groups[k].length })));
    groups[k].forEach((r) => {
      const card = el("div", { class: "card", "data-id": r.id });
      // 标题：班列号 · 客户编码
      card.appendChild(el("div", { class: "c-title" }, el("b", { text: r["班列号"] || "—" }), " · " + (r["客户编码"] || "")));
      // 第二行：口岸 / 目的站 两个标签 + 跳转按钮（置于最右）
      const route = el("div", { class: "c-route" });
      route.appendChild(el("span", { class: "tag", text: r["口岸"] || "—" }));
      route.appendChild(el("span", { class: "tag tag-dest", text: r["目的站"] || "—" }));
      route.appendChild(el("button", { class: "jump-btn", type: "button", text: "↩ 跳到表格",
        title: "跳回表格对应行（不改变当前筛选）",
        onclick: (e) => { e.stopPropagation(); jumpToRow(r.id); } }));
      card.appendChild(route);
      // 箱号（只读显示，不提供编辑）
      card.appendChild(el("div", { class: "c-line muted", text: "箱号：" + (r["箱号"] || "—") }));
      // 分组字段（如状态）可直接修改
      card.appendChild(el("div", { class: "c-line" }, el("span", { class: "c-label", text: ((colDef(gf) || {}).label || gf) + " " }), mkInput(r, gf)));
      // 单箱价格 + 算价
      const priceLine = el("div", { class: "c-line" }, el("span", { class: "c-label", text: "单价 " }), el("b", { text: r["单箱价格"] || "—" }));
      if (!String(r["单箱价格"] || "").trim()) {
        priceLine.appendChild(el("button", { class: "mini", text: "算价", onclick: () => priceRow(r.id) }));
      }
      card.appendChild(priceLine);
      col.appendChild(card);
    });
    board.appendChild(col);
  });
}
/* 看板卡片“跳到表格”：切回表格视图（保留当前筛选），滚动定位到对应行并高亮 */
function jumpToRow(id) {
  setView("grid");
  setTimeout(() => {
    const tr = document.querySelector('#body tr[data-id="' + id + '"]');
    if (tr) {
      tr.scrollIntoView({ block: "center", behavior: "smooth" });
      tr.classList.remove("flash");
      void tr.offsetWidth;   // 重置动画
      tr.classList.add("flash");
      setTimeout(() => tr.classList.remove("flash"), 4000);
      toast("已定位到该行（橙色高亮 4 秒）");
    } else {
      toast("该行不在当前筛选范围内");
    }
  }, 60);
}
function mkInput(r, field) {
  const c = colDef(field);
  if (c) return mkEditor(r, c);
  return el("input", { value: r[field] || "", onchange: (e) => saveCell(r.id, field, e.target.value) });
}

/* ---------- 渲染：标签栏 ---------- */
function renderTabs() {
  const bar = document.getElementById("tabBar"); bar.innerHTML = "";
  STATE.kanban_tabs.forEach((t) => {
    const chip = el("span", { class: "tab" + (STATE.active_tab === t.id ? " active" : "") });
    chip.appendChild(el("span", {
      text: t.name,
      onclick: () => { STATE.active_tab = STATE.active_tab === t.id ? null : t.id; saveStateSoon(); renderAll(); },
    }));
    chip.appendChild(el("span", { class: "ed", text: "✎", title: "编辑", onclick: (e) => { e.stopPropagation(); openTabPop(t, e); } }));
    chip.appendChild(el("span", { class: "x", text: "×", title: "关闭标签", onclick: (e) => { e.stopPropagation(); closeTab(t.id); } }));
    bar.appendChild(chip);
  });
}
function closeTab(id) {
  if (!confirm("关闭该标签？")) return;
  STATE.kanban_tabs = STATE.kanban_tabs.filter((t) => t.id !== id);
  if (STATE.active_tab === id) STATE.active_tab = null;
  saveStateSoon(); renderTabs(); renderActive();
}

/* ---------- 渲染：统计 ---------- */
function renderStats() {
  const rows = DATA.filter(passFilter);
  const box = document.getElementById("stats"); box.innerHTML = "";
  const withPrice = rows.filter((r) => num(r["单箱价格"]) > 0);
  const sum = withPrice.reduce((a, r) => a + num(r["单箱价格"]), 0);
  const avg = withPrice.length ? sum / withPrice.length : 0;
  const byC = {}, byM = {};
  rows.forEach((r) => {
    const c = r[META.company_field] || "(空)";
    byC[c] = byC[c] || { n: 0, s: 0 }; byC[c].n++; if (num(r["单箱价格"]) > 0) byC[c].s += num(r["单箱价格"]);
    const m = String(r["发班时间"] || "").slice(0, 7) || "(空)";
    byM[m] = byM[m] || { n: 0, s: 0 }; byM[m].n++; if (num(r["单箱价格"]) > 0) byM[m].s += num(r["单箱价格"]);
  });
  box.appendChild(el("h2", { text: "统计（基于当前筛选结果 · " + rows.length + " 行）" }));
  const kpis = el("div", { class: "kpis" });
  kpis.appendChild(kpi("记录数", rows.length));
  kpis.appendChild(kpi("有单价", withPrice.length));
  kpis.appendChild(kpi("单价合计", sum.toFixed(2)));
  kpis.appendChild(kpi("单价平均", avg.toFixed(2)));
  box.appendChild(kpis);
  box.appendChild(tableFrom("按公司", byC));
  box.appendChild(tableFrom("按月(发班时间)", byM));
}
function kpi(k, v) { return el("div", { class: "kpi" }, el("div", { class: "v", text: String(v) }), el("div", { class: "k", text: k })); }
function tableFrom(title, obj) {
  const t = el("table");
  t.appendChild(el("tr", {}, el("th", { text: title }), el("th", { text: "箱量" }), el("th", { text: "单价合计" })));
  Object.keys(obj).sort().forEach((k) => {
    t.appendChild(el("tr", {}, el("td", { text: k }), el("td", { text: obj[k].n }), el("td", { text: obj[k].s.toFixed(2) })));
  });
  return el("div", {}, el("h2", { text: title }), t);
}

/* ---------- 统一渲染 ---------- */
function renderActive() {
  if (STATE.view === "grid") renderBody();
  else if (STATE.view === "board") renderBoard();
  else if (STATE.view === "dash") renderDashboard();
  else renderStats();
  renderMonthTabs();   // #140：每次渲染同步全局月份条可见性（Dashboard/专列表格隐藏）
  // 调试面板已移除
  // _renderFilterDiag();
}

// ---- 筛选诊断面板（已停用）----
/* let _diagEl = null;
function _renderFilterDiag() { ... 已移除 ... } */
function renderAll() {
  if (STATE.view === "dash") { renderDashboard(); return; }
  renderBody(); renderBoard(); renderTabs(); renderStats(); renderMonthTabs();
  document.getElementById("filterRow") && updateCompanyBtn();
  updateCompanyBtn();
  refreshSortBadges();
}

/* ---------- 保存单元格 / 算价 ---------- */
/* 待保存缓冲：编辑中的单元格先记到这里，页面关闭/刷新前用 sendBeacon 兜底发送，
   避免“输入后没失焦就刷新/关页”导致备注等字段丢失。 */
let _pending = {};
function pendingKey(id, field) { return id + "|" + field; }
function markPending(id, field, val) { _pending[pendingKey(id, field)] = val; }
function clearPending(id, field) { delete _pending[pendingKey(id, field)]; }
function flushPending() {
  const edits = Object.keys(_pending).map((k) => {
    const i = k.indexOf("|");
    return { id: parseInt(k.slice(0, i), 10), field: k.slice(i + 1), value: _pending[k] };
  });
  if (!edits.length) return;
  try {
    const blob = new Blob([JSON.stringify({ user: USER, edits })], { type: "application/json" });
    navigator.sendBeacon("api/cells", blob);
  } catch (_) { /* 兜底失败也无能为力，正常 blur 保存已覆盖绝大多数情况 */ }
}
window.addEventListener("beforeunload", flushPending);

function saveCell(id, field, val) {
  const rec = DATA.find((x) => x.id === id);
  const oldValue = rec ? (rec[field] == null ? "" : rec[field]) : "";
  pushUndo({ type: "cell", id, field, oldValue: String(oldValue), newValue: String(val) });
  markPending(id, field, val);
  fetch("api/row/" + id, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ field, value: val, user: USER }),
  }).then((r) => r.json()).then((j) => {
    if (j.ok) {
      clearPending(id, field);
      const rec = DATA.find((x) => x.id === id); if (rec) rec[field] = val;
      if (field === "班列类型") {
        // C/D：类型变更影响专列(DASH)与散舱(DATA)两个独立快照，必须双向强制刷新；
        // 并跳到「全部」视图，确保改完一定可见（修复"专列改散舱两边都不见"）。
        STATE.trainType = "全部";
        document.querySelectorAll("#trainTypeSeg .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.type === "全部"));
        if (STATE.view !== "grid") setView("grid");
        toast("已改为「" + val + "」：已切到「全部」视图，可在「全部」/「" + val + "」中查看");
        loadRows(true).then(() => { renderDashboard(); renderActive(); });
      } else if (field === "发班时间" && j.synced > 0) {
        // 发班时间是班列级属性：后端已把同班列其他记录一并改了，
        // 这里同步本地数据并重绘，否则界面上其他行还是旧时间，看着像没联动。
        syncDepartureLocal(id, String(oldValue), String(val));
        renderActive();
        toast("已保存，同班列另 " + j.synced + " 条发班时间已同步");
      } else {
        renderActive();
        toast("已保存");
      }
      syncVersion();
    } else toast("保存失败: " + j.msg);
  }).catch(() => toast("保存失败(网络)"));
}

/* 与后端 sync_departure 保持同一套规则：同班列号 + 同原年份(或原值为空) → 一起改。
   跨年同号（2026-WB123 ≠ 2027-WB123）按原年份锁定，不会互串。 */
function syncDepartureLocal(srcId, oldVal, newVal) {
  const src = DATA.find((x) => x.id === srcId);
  if (!src) return;
  const train = (src["班列号"] || "").trim();
  if (!train) return;
  const oldYear = (oldVal || "").slice(0, 4) || (newVal || "").slice(0, 4);
  DATA.forEach((r) => {
    if (r.id === srcId) return;
    if ((r["班列号"] || "").trim() !== train) return;
    const cur = r["发班时间"] || "";
    if (cur === newVal) return;
    if (cur === "" || cur.slice(0, 4) === oldYear) r["发班时间"] = newVal;
  });
}

/* ---------- 多人协同：轮询数据版本，他人改动自动刷新 ---------- */
function syncVersion() {
  return fetch("api/version").then((r) => r.json())
    .then((j) => { _dataVersion = j.version; }).catch(() => {});
}
function isEditingCell() {
  const ae = document.activeElement;
  if (!ae) return false;
  const tag = ae.tagName;
  if (tag !== "INPUT" && tag !== "SELECT" && tag !== "TEXTAREA") return false;
  return !!(ae.closest("#body") || ae.closest("#board") || ae.closest("#filterRow"));
}
function pollRemoteChanges() {
  fetch("api/version").then((r) => r.json()).then((j) => {
    if (_dataVersion === -1) { _dataVersion = j.version; return; }
    if (j.version !== _dataVersion) {
      if (isEditingCell()) return;           // 正在编辑，等空闲再刷新
      _dataVersion = j.version;
      loadRows(true).then(() => { renderActive(); toast("已同步他人改动"); });
    }
  }).catch(() => {});
}
function priceRow(id) {
  fetch("api/price/" + id, { method: "POST" }).then((r) => r.json()).then((j) => {
    if (j.ok) { const rec = DATA.find((x) => x.id === id); if (rec) rec["单箱价格"] = j.price; renderActive(); toast("算价: " + j.price); }
    else toast("算价失败: " + j.msg);
  }).catch(() => toast("算价失败(网络)"));
}

/* ---------- 列筛选弹层（漏斗：下拉选项 + 多选） ---------- */
function openFilter(col, evt) {
  const pop = document.getElementById("filterPop");
  pop.innerHTML = ""; pop.style.position = "fixed";
  pop.appendChild(el("div", { class: "pop-head" }, el("span", { text: col + " · 选项(可多选)" })));
  const q = el("input", { class: "q", type: "text", placeholder: "搜索选项…" });
  const list = el("div", { class: "pop-list" });
  pop.appendChild(q); pop.appendChild(list);
  const bar = el("div", { class: "bar" },
    el("a", { text: "全选", href: "#", onclick: (e) => { e.preventDefault(); STATE.filters[col] = distinct(col); STATE.page = 0; sync(); saveStateSoon(); renderActive(); } }),
    el("a", { text: "清空", href: "#", onclick: (e) => { e.preventDefault(); STATE.filters[col] = []; STATE.page = 0; sync(); saveStateSoon(); renderActive(); } }));
  pop.appendChild(bar);

  function sync() { renderList(); }
  function renderList() {
    list.innerHTML = "";
    const kw = q.value.trim().toLowerCase();
    distinct(col).filter((v) => v.toLowerCase().includes(kw)).forEach((v) => {
      const checked = (STATE.filters[col] || []).includes(v);
      const cb = el("input", {
        type: "checkbox", checked,
        onchange: () => {
          const arr = STATE.filters[col] || [];
          if (cb.checked) { if (!arr.includes(v)) arr.push(v); }
          else STATE.filters[col] = arr.filter((x) => x !== v);
          STATE.page = 0; saveStateSoon(); renderActive();
        },
      });
      list.appendChild(el("label", { class: "opt" }, cb, el("span", { text: v })));
    });
    if (!list.children.length) list.appendChild(el("div", { class: "muted", text: "(无选项)" }));
  }
  q.oninput = renderList;
  renderList();

  const rect = evt.target.getBoundingClientRect();
  pop.style.left = Math.min(rect.left, window.innerWidth - 260) + "px";
  pop.style.top = (rect.bottom + 4) + "px";
  pop.classList.remove("hidden");
}

/* ---------- 字段选项维护（增/删下拉选项，立即生效） ---------- */
function openOptPop() {
  const pop = document.getElementById("optPop");
  pop.style.position = "fixed";
  const btn = document.getElementById("optBtn");
  const rect = btn.getBoundingClientRect();
  pop.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - 540)) + "px";
  pop.style.top = Math.max(8, Math.min(rect.bottom + 6, window.innerHeight - 480)) + "px";
  pop.classList.remove("hidden");
  renderOptPop();
}
function renderOptPop() {
  const pop = document.getElementById("optPop");
  pop.innerHTML = "";
  pop.appendChild(el("div", { class: "pop-head" },
    el("span", { text: "字段选项维护（可直接增删，立即生效）" }),
    el("span", { class: "alink", text: "关闭", onclick: () => pop.classList.add("hidden") })));
  const body = el("div", { class: "opt-body" });
  META.columns.filter((c) => c.maintainable).forEach((c) => {
    const sec = el("div", { class: "opt-sec" });
    sec.appendChild(el("div", { class: "opt-title", text: c.name + (c.free_text ? "（可手输+筛选）" : "") }));
    const chips = el("div", { class: "chips" });
    (c.options || []).forEach((o) => {
      chips.appendChild(el("span", { class: "chip" },
        el("span", { text: o }),
        el("span", { class: "x", text: "×", title: "删除该选项",
          onclick: () => {
            c.options = c.options.filter((x) => x !== o);
            saveOptions(c.name, c.options); renderOptPop();
          } })));
    });
    sec.appendChild(chips);
    const inp = el("input", { class: "opt-add", type: "text", placeholder: "新增选项，回车添加" });
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && inp.value.trim()) {
        const v = inp.value.trim();
        if (c.options.includes(v)) toast("已存在");
        else { c.options = c.options.concat(v); saveOptions(c.name, c.options); renderOptPop(); }
      }
    });
    sec.appendChild(inp);
    body.appendChild(sec);
  });
  pop.appendChild(body);
}
function saveOptions(field, options) {
  fetch("api/field_options", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ field, options }),
  }).then((r) => r.json()).then((j) => {
    if (j.ok) {
      toast("选项已保存");
      loadMeta().then(() => { buildDatalists(); renderActive(); });
    } else toast("保存失败: " + j.msg);
  }).catch(() => toast("保存失败(网络)"));
}
/* 刷新 free_text 字段的 datalist（选项变更后立即反映到单元格下拉） */
function buildDatalists() {
  META.columns.filter((c) => c.free_text).forEach((c) => {
    const dlId = "dl_" + c.name;
    const dl = document.getElementById(dlId);
    if (!dl) return;
    dl.innerHTML = "";
    (c.options || []).forEach((o) => dl.appendChild(el("option", { value: o })));
  });
}

/* ---------- 字段管理（隐藏 / 排序，按当前用户，互不干扰） ---------- */
function openFieldPop() {
  const pop = document.getElementById("fieldPop");
  pop.style.position = "fixed";
  const btn = document.getElementById("fieldBtn");
  const rect = btn.getBoundingClientRect();
  pop.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - 360)) + "px";
  pop.style.top = Math.max(8, Math.min(rect.bottom + 6, window.innerHeight - 520)) + "px";
  pop.classList.remove("hidden");
  renderFieldPop();
}
function fieldOrderList() {
  // 工作副本：当前顺序（已隐藏的也显示，便于恢复显示）
  const order = STATE.fieldOrder && STATE.fieldOrder.length ? STATE.fieldOrder.slice()
    : META.columns.map((c) => c.name);
  // 补齐配置中新增但 order 里没有的字段
  META.columns.forEach((c) => { if (!order.includes(c.name)) order.push(c.name); });
  return order;
}
function renderFieldPop() {
  const pop = document.getElementById("fieldPop");
  pop.innerHTML = "";
  pop.appendChild(el("div", { class: "pop-head" },
    el("span", { text: "字段管理（隐藏 / 拖动排序，仅你自己生效）" }),
    el("span", { class: "alink", text: "关闭", onclick: () => pop.classList.add("hidden") })));
  const body = el("div", { class: "field-body" });
  const hidden = STATE.hiddenFields || {};
  const list = fieldOrderList();
  const rowsWrap = el("div", { class: "field-rows" });
  list.forEach((name, idx) => {
    const c = META.columns.find((x) => x.name === name);
    if (!c) return;
    const rowEl = el("div", { class: "field-row" + (hidden[name] ? " hidden-field" : "") });
    rowEl.appendChild(el("span", { class: "fhandle", text: "⠿", title: "拖拽排序" }));
    const cb = el("input", { type: "checkbox", checked: !hidden[name],
      onchange: () => { STATE.hiddenFields = STATE.hiddenFields || {}; if (cb.checked) delete STATE.hiddenFields[name]; else STATE.hiddenFields[name] = true; saveStateSoon(); renderFieldPop(); buildStatic(); renderActive(); } });
    rowEl.appendChild(el("label", { class: "fshow" }, cb, el("span", { text: name })));
    const mv = el("span", { class: "fmove" });
    mv.appendChild(el("span", { class: "fup", text: "↑", title: "上移",
      onclick: () => { if (idx > 0) { list.splice(idx - 1, 0, list.splice(idx, 1)[0]); commitFieldOrder(list); } } }));
    mv.appendChild(el("span", { class: "fdown", text: "↓", title: "下移",
      onclick: () => { if (idx < list.length - 1) { list.splice(idx + 1, 0, list.splice(idx, 1)[0]); commitFieldOrder(list); } } }));
    rowEl.appendChild(mv);
    rowsWrap.appendChild(rowEl);
  });
  body.appendChild(rowsWrap);
  // 简易拖拽排序
  enableFieldDrag(rowsWrap, list);
  pop.appendChild(body);
}
function commitFieldOrder(list) {
  STATE.fieldOrder = list.slice();
  saveStateSoon();
  renderFieldPop(); buildStatic(); renderActive();
}
function enableFieldDrag(wrap, list) {
  let dragEl = null;
  wrap.querySelectorAll(".field-row").forEach((row) => {
    row.querySelector(".fhandle").addEventListener("mousedown", (e) => {
      e.preventDefault();
      dragEl = row;
      row.classList.add("dragging");
      const onMove = (ev) => {
        const after = [...wrap.querySelectorAll(".field-row")].find((r) => {
          const box = r.getBoundingClientRect();
          return ev.clientY < box.top + box.height / 2;
        });
        if (after == null) wrap.appendChild(row);
        else wrap.insertBefore(row, after);
      };
      const onUp = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        row.classList.remove("dragging");
        const newList = [...wrap.querySelectorAll(".field-row")].map((r) => r.querySelector(".fshow span").textContent);
        commitFieldOrder(newList);
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });
  });
}

/* ---------- 批量算价 + 导出 ---------- */
function batchPrice() {
  if (!confirm("对当前所有“单箱价格为空”的记录执行算价？已填价格的不会被覆盖。")) return;
  fetch("api/price/batch", { method: "POST" }).then((r) => r.json()).then((j) => {
    if (j.ok) { loadRows().then(() => { renderActive(); toast("已批量算价 " + j.priced + " 行"); }); }
    else toast("算价失败: " + (j.msg || ""));
  }).catch(() => toast("算价失败(网络)"));
}
function exportXlsx() {
  const cols = userColumns().map((c) => c.name);
  const rows = DATA.filter(passFilter);   // 当前筛选条件下的行
  toast("导出中…");
  fetch("api/export", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ columns: cols, rows: rows }),
  }).then((r) => {
    if (!r.ok) return r.json().then((j) => { throw new Error(j.msg || "导出失败"); });
    return r.blob();
  }).then((blob) => {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "订舱数据_导出.xlsx"; a.click();
    URL.revokeObjectURL(url);
    toast("已导出当前筛选结果（" + rows.length + " 行）");
  }).catch((e) => toast("导出失败: " + (e.message || "")));
}

/* ---------- 标签弹层（看板：新增/编辑） ---------- */
function openTabPop(existing, evt) {
  const pop = document.getElementById("tabPop");
  // 固定定位：弹在触发按钮附近（否则会掉到页面最底部）
  pop.style.position = "fixed";
  let left = 16, top = 96;
  const anchor = (evt && evt.target) || document.getElementById("newTabBtn");
  if (anchor && anchor.getBoundingClientRect) {
    const rect = anchor.getBoundingClientRect();
    left = Math.max(8, Math.min(rect.left, window.innerWidth - 300));
    top = Math.max(8, Math.min(rect.bottom + 6, window.innerHeight - 260));
  }
  pop.style.left = left + "px";
  pop.style.top = top + "px";
  document.getElementById("tabPopTitle").textContent = existing ? "编辑标签" : "新建标签";
  document.getElementById("tabName").value = existing ? existing.name : "";
  const cond = document.getElementById("tabCond"); cond.innerHTML = "";
  const fieldSel = el("select", {});
  META.columns.forEach((c) => fieldSel.appendChild(el("option", { value: c.name, text: c.name })));
  const valSel = el("select", {});
  function fillVals() { valSel.innerHTML = ""; distinct(fieldSel.value).forEach((v) => valSel.appendChild(el("option", { value: v, text: v }))); }
  fieldSel.onchange = fillVals; fillVals();
  if (existing) {
    const f = Object.keys(existing.filters || {})[0];
    const v = (existing.filters[f] || [])[0];
    if (f) { fieldSel.value = f; fillVals(); if (v) valSel.value = v; }
  }
  cond.appendChild(el("label", {}, "字段 ", fieldSel));
  cond.appendChild(el("label", {}, "值 ", valSel));
  pop.classList.remove("hidden");
  document.getElementById("tabSave").onclick = () => {
    const name = document.getElementById("tabName").value.trim() || "未命名";
    const f = fieldSel.value, v = valSel.value;
    const filters = { [f]: [v] };
    if (existing) { existing.name = name; existing.filters = filters; }
    else STATE.kanban_tabs.push({ id: "t" + Date.now(), name, filters });
    saveStateSoon(); pop.classList.add("hidden"); renderTabs(); renderActive();
  };
  document.getElementById("tabCancel").onclick = () => pop.classList.add("hidden");
}

/* ---------- 数据加载 ---------- */
function loadMeta() {
  return fetch("api/meta").then((r) => r.json()).then((m) => { META = m; STATE.group_field = m.groupable[0]; });
}
function loadRows(keepNew) {
  if (!keepNew) _newRowIds.clear();   // 重新加载数据后，新增行恢复受正常筛选规则约束；keepNew=true（自动同步他人改动）时保留待定行
  return fetch("api/rows").then((r) => r.json()).then((rows) => { DATA = rows; });
}
function loadUserState() {
  return fetch("api/state?user=" + encodeURIComponent(USER)).then((r) => r.json()).then((j) => {
    const s = j.state || {};
    STATE = Object.assign({
      view: "grid", search: "", company: [], filters: {}, contains: {},
      group_field: META.groupable[0], kanban_tabs: [], active_tab: null,       sortRules: [], month: "",
      colWidths: {}, rowHeights: {}, page: 0, pageSize: 100,
    }, s);
    if (!STATE.group_field) STATE.group_field = META.groupable[0];
  }).catch(() => {});
}

/* ---------- 新增 / 删除（右键 + 按钮） ---------- */
function insertRow(position, refId) {
  fetch("api/row/insert", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ position, ref_id: refId }),
  }).then((r) => r.json()).then((j) => {
    if (j.ok) {
      loadRows(true).then(() => {
        _newRowIds.add(j.id);   // 临时放行，让用户在当前筛选下也能看到并编辑
        // 跳转到新行所在页，方便一口气新增多行后逐格编辑
        const _vrows = getView();
        const _idx = _vrows.findIndex((r) => r.id === j.id);
        if (_idx >= 0) {
          const _ps = (STATE.pageSize && STATE.pageSize > 0 && STATE.pageSize < 900000) ? STATE.pageSize : 900000;
          STATE.page = (_ps >= 900000) ? 0 : Math.floor(_idx / _ps);
        }
        renderAll();
        // 把刚插入的新行设为当前选中单元格/粘贴锚点，
        // 使紧接着的 Ctrl+V 落在新行，而非残留旧锚点（原"放到第一行"现象）
        const _ncols = userColumns();
        const _af = (_ncols[0] || {}).name;
        _activeCell = { id: j.id, field: _af };
        const _nv = getView();
        const _nri = _nv.findIndex((r) => r.id === j.id);
        if (_nri >= 0) setSelection(_nri, 0, _nri, 0);
        applyCellSelection();
        const tr = document.querySelector('#body tr[data-id="' + j.id + '"]');
        if (tr) {
          tr.scrollIntoView({ block: "center", behavior: "smooth" });
          tr.classList.remove("flash");
          void tr.offsetWidth;
          tr.classList.add("flash");
          setTimeout(() => tr.classList.remove("flash"), 4000);
          toast("已插入新行 #" + j.seq);
        } else {
          toast("已插入新行 #" + j.seq);
        }
      });
    }
    else toast("插入失败");
  }).catch(() => toast("插入失败(网络)"));
}
function deleteRow(id) {
  if (!confirm("确定删除这条记录吗？\n删除后会进入回收站（系统管理 → 回收站），可随时恢复。")) return;
  fetch("api/row/" + id + "?user=" + encodeURIComponent(USER), { method: "DELETE" }).then((r) => r.json()).then((j) => {
    if (j.ok) { DATA = DATA.filter((x) => x.id !== id); renderAll(); toast("已移入回收站"); }
    else toast("删除失败");
  }).catch(() => toast("删除失败(网络)"));
}
async function deleteRows(ids) {
  ids = (ids || []).filter(Boolean);
  if (!ids.length) return;
  if (!confirm(`确定删除选中的 ${ids.length} 条记录吗？\n删除后进入回收站（系统管理 → 回收站），可随时恢复。`)) return;
  let ok = 0, fail = 0;
  await Promise.all(ids.map((id) =>
    fetch("api/row/" + id + "?user=" + encodeURIComponent(USER), { method: "DELETE" })
      .then((r) => r.json()).then((j) => { if (j.ok) { ok++; DATA = DATA.filter((x) => x.id !== id); } else fail++; })
      .catch(() => fail++)
  ));
  _selectedRowIds.clear();
  renderAll();
  toast(`已移入回收站 ${ok} 条` + (fail ? `，失败 ${fail} 条` : ""));
}
let _clipboardRow = null; // 右键“复制此行”的整行数据副本
let _systemClipboard = ""; // 兜底：监听页面 copy 事件捕获的纯文本（解决 http 下 navigator.clipboard 读不到的问题）
let _ctxField = null;      // 右键时所在的列（字段名），用于复制/粘贴单元格

/* 把文本写进系统剪贴板：优先 clipboard API；HTTP 下用临时 textarea + execCommand 兜底 */
function writeClipboard(text) {
  _systemClipboard = text;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).catch(() => execCopy(text));
  } else {
    execCopy(text);
  }
}
function execCopy(text) {
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed"; ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    document.body.removeChild(ta);
  } catch (_) {}
}

function openCtxMenu(e, rowId, field) {
  e.preventDefault();
  _ctxRowId = rowId;
  _ctxField = field || null;
  const menu = document.getElementById("ctxMenu");
  menu.style.position = "fixed";
  // 先显示以测量真实宽高，再做边界校正
  menu.classList.remove("hidden");
  const h = menu.offsetHeight || 260;
  const w = menu.offsetWidth || 190;
  let x = Math.min(e.clientX, Math.max(0, window.innerWidth - w));
  let y = e.clientY;
  if (y + h > window.innerHeight) {
    y = Math.max(0, window.innerHeight - h);
  }
  menu.style.left = x + "px";
  menu.style.top = y + "px";
}

/* 复制当前行（内存 + 系统剪贴板，便于 Excel 粘贴） */
function copyRow(id) {
  const rec = id != null && DATA.find((x) => x.id === id);
  if (!rec) { toast("没有可复制的行"); return; }
  const cols = userColumns();
  const vals = cols.map((c) => String(rec[c.name] || ""));
  const tsv = vals.join("\t");
  _clipboardRow = { rec, cols };
  writeClipboard(tsv);
  toast("已复制此行");
}

/* 复制单元格：优先复制编辑框里选中的部分，否则整格的值 */
function copyCell(id, field) {
  const rec = id != null && DATA.find((x) => x.id === id);
  if (!rec || !field) { toast("没有可复制的单元格"); return; }
  let text = String(rec[field] == null ? "" : rec[field]);
  // 如果焦点正在这个格子的编辑框里且有选中文本，只复制选中部分
  const active = document.activeElement;
  if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA")) {
    const sel = active.value.substring(active.selectionStart || 0, active.selectionEnd || 0);
    if (sel) text = sel;
  }
  _clipboardRow = null;   // 单元格复制后，行粘贴不再误用旧的整行副本
  writeClipboard(text);
  toast("已复制「" + field + "」：" + (text.length > 20 ? text.slice(0, 20) + "…" : (text || "(空)")));
}

/* 可靠读取系统剪贴板：
   - 优先 navigator.clipboard.readText（localhost / https 安全上下文）
   - 否则用 execCommand('paste') 注入隐藏 textarea 读取（HTTP 反代等非安全上下文也能读到在别的窗口复制的外部文本） */
async function readSystemClipboard() {
  let t = "";
  try {
    if (navigator.clipboard && navigator.clipboard.readText) t = await navigator.clipboard.readText();
  } catch (e) { t = ""; }
  if (t) return t;
  try {
    const ta = document.createElement("textarea");
    ta.style.position = "fixed"; ta.style.left = "-9999px"; ta.style.top = "0";
    document.body.appendChild(ta); ta.focus(); ta.select();
    const ok = document.execCommand("paste");
    t = ta.value; document.body.removeChild(ta);
    if (ok && t) return t;
  } catch (e) {}
  return "";
}

/* 统一保存多字段编辑并记一笔可撤销操作（批量粘贴 / 整行粘贴共用）。
   本地先更新 DATA 并即时渲染，再写服务端。 */
function applyEdits(edits, msg) {
  if (!edits.length) { toast("没有需要粘贴的内容"); return; }
  edits.forEach((e) => { const rec = DATA.find((x) => x.id === e.id); if (rec) rec[e.field] = e.value; });
  fetch("api/cells", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user: USER, edits }),
  }).then((r) => r.json()).then((j) => {
    if (j.ok) {
      renderActive();
      toast(msg);
      pushUndo({ type: "bulk-edit", edits: JSON.parse(JSON.stringify(edits)) });
    } else toast("保存失败: " + (j.msg || ""));
  }).catch(() => toast("保存失败(网络)"));
}

/* 粘贴到当前行（支持系统剪贴板多行 → 批量粘贴） */
async function pasteToRow(id) {
  if (id == null) { toast("请先右键选中一行"); return; }
  // 1) 优先读取真实系统剪贴板（即使文本是在别的窗口复制的也能读到）
  let raw = (await readSystemClipboard()).trim();
  if (!raw) raw = (_systemClipboard || "").trim();
  if (raw) {
    // 多行或多列 → 批量粘贴（以右键所点的列/首列为锚点向右下铺开）
    const lines = raw.split(/\r?\n/).filter((l) => l.trim() !== "");
    if (lines.length > 1 || raw.includes("\t")) {
      const f = (_ctxField && userColumns().find((c) => c.name === _ctxField)) ? _ctxField : (userColumns()[0] || {}).name;
      doGridPaste(id, f, raw);
      return;
    }
    // 单行纯文本：粘贴到右键所点的那个单元格（没有列信息时退回第一个可见列）
    const rec = DATA.find((x) => x.id === id);
    if (!rec) return;
    const cols = userColumns();
    const target = (_ctxField && cols.find((c) => c.name === _ctxField)) || cols[0];
    if (!target) return;
    if (String(rec[target.name] || "") === raw) { toast("内容相同，无需粘贴"); return; }
    let nv = raw;
    if (target.type === "date") nv = normalizeDate(nv);
    else if (target.type === "select") nv = normalizeSelect(target, nv);
    if (String(rec[target.name] || "") === nv) { toast("内容相同，无需粘贴"); return; }
    rec[target.name] = nv;
    saveCell(id, target.name, nv);   // saveCell 已记撤销
    renderActive();
    toast("已粘贴到「" + target.name + "」");
    return;
  }

  // 2) 系统剪贴板为空：仅当本页之前「复制此行」过，才作为兜底（需确认，避免误覆盖吓一跳）
  if (!_clipboardRow || !_clipboardRow.rec) { toast("剪贴板为空"); return; }
  const src = _clipboardRow.rec;
  const rec = DATA.find((x) => x.id === id);
  if (!rec) return;
  const cols = userColumns();
  const edits = [];
  for (const c of cols) {
    const v = src[c.name];
    if (v == null || v === "") continue;
    if (String(rec[c.name] || "") === String(v)) continue;
    edits.push({ id, field: c.name, value: String(v), oldValue: rec[c.name] == null ? "" : rec[c.name] });
  }
  if (!edits.length) { toast("没有需要粘贴的内容"); return; }
  if (!confirm("系统剪贴板为空，是否粘贴本页之前复制的整行（" + edits.length + " 个字段）到当前行？")) return;
  applyEdits(edits, "已粘贴 " + edits.length + " 个字段（来自本页复制的行）");
}

/* 批量粘贴：从 startId 开始，按当前显示列顺序向下覆盖 */
function doBulkPaste(startId, raw) {
  const rec = DATA.find((x) => x.id === startId);
  if (!rec) { toast("找不到起始行"); return; }
  const startIdx = DATA.indexOf(rec);
  const rows = raw.split(/\r?\n/)
    .map((line) => line.split("\t"))
    .filter((arr) => arr.some((v) => String(v).trim() !== ""));
  if (!rows.length) { toast("没有可粘贴的内容"); return; }
  const cols = userColumns();
  const edits = [];
  const updated = [];
  let skipped = 0;
  rows.forEach((vals, i) => {
    const target = DATA[startIdx + i];
    if (!target) { skipped++; return; }
    vals.forEach((v, j) => {
      if (j >= cols.length) return;
      const c = cols[j];
      const nv = String(v).trim();
      if (target[c.name] === nv) return;
      edits.push({ id: target.id, field: c.name, value: nv, oldValue: target[c.name] });
      target[c.name] = nv;
      updated.push(target.id);
    });
  });
  if (!edits.length) { toast("没有需要粘贴的内容"); return; }
  if (skipped) toast(`超出 ${skipped} 行，已跳过（可先插入空行再粘贴）`);
  applyEdits(edits, `已批量粘贴 ${rows.length - skipped} 行，${edits.length} 个字段`);
}

/* ====================== 网格多格粘贴（按焦点单元格锚定，向右/下 2D 展开） ====================== */
/* 单元格级 Ctrl+V：以该格为锚点，把外部表格复制的矩阵（TSV）铺到对应矩形区域 */
function onCellPaste(e, id, field) {
  e.preventDefault();
  const raw = (e.clipboardData || window.clipboardData).getData("text");
  if (!raw) { toast("剪贴板为空"); return; }
  _systemClipboard = raw;
  doGridPaste(id, field, raw);
}

/* 把 Excel/Google 表格复制的文本（制表符分列、换行分行）按锚点格铺开 */
function doGridPaste(id, field, raw) {
  const viewRows = getView();
  const anchorIdx = viewRows.findIndex((r) => r.id === id);
  if (anchorIdx < 0) { toast("找不到起始行"); return; }
  const cols = userColumns();
  const anchorCol = cols.findIndex((c) => c.name === field);
  if (anchorCol < 0) { toast("找不到起始列"); return; }
  const matrix = raw.split(/\r?\n/).map((l) => l.split("\t"));
  // 去掉末尾全空行（剪贴板常带多余换行）
  while (matrix.length && matrix[matrix.length - 1].every((v) => String(v == null ? "" : v).trim() === "")) matrix.pop();
  if (!matrix.length) { toast("没有可粘贴的内容"); return; }
  const R = matrix.length;
  const C = Math.max.apply(null, matrix.map((a) => a.length));
  const edits = [];
  let skipped = 0;
  for (let dr = 0; dr < R; dr++) {
    const target = viewRows[anchorIdx + dr];
    if (!target) { skipped += (R - dr); break; }
    for (let dc = 0; dc < C; dc++) {
      const tc = cols[anchorCol + dc];
      if (!tc) continue;
      let nv = (matrix[dr][dc] != null ? String(matrix[dr][dc]) : "").trim();
      if (nv === "") continue;                 // 空值不覆盖（避免误清空目标格）
      if (tc.type === "date") nv = normalizeDate(nv);
      else if (tc.type === "select") nv = normalizeSelect(tc, nv);
      if (String(target[tc.name] == null ? "" : target[tc.name]) === nv) continue;
      edits.push({ id: target.id, field: tc.name, value: nv });
      target[tc.name] = nv;
    }
  }
  if (!edits.length) { toast("没有需要粘贴的内容"); return; }
  const note = skipped ? `（末尾 ${skipped} 行超出，已跳过）` : "";
  const msg = `已粘贴 ${R}×${C}${note}，从「${field}」开始向右/向下铺开`;
  applyEdits(edits, msg);
}

/* Excel 日期序列号 / 各种 yyyy/m/d 文本 → 统一 YYYY-MM-DD */
function normalizeDate(v) {
  v = (v || "").trim();
  if (!v) return "";
  if (/^\d{4,5}$/.test(v)) {
    const s = parseInt(v, 10);
    if (s > 20000 && s < 60000) {     // Excel 1900 日期序列号
      const d = new Date((s - 25569) * 86400000);
      const p = (n) => String(n).padStart(2, "0");
      return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}`;
    }
  }
  const m = v.match(/^(\d{4})[/-](\d{1,2})[/-](\d{1,2})/);
  if (m) {
    const p = (n) => String(n).padStart(2, "0");
    return `${m[1]}-${p(m[2])}-${p(m[3])}`;
  }
  return v;
}

/* 粘贴到下拉字段：大小写不敏感匹配选项；自由文本/可维护字段保留原值；不匹配且非自由文本也保留（入库存储） */
function normalizeSelect(c, v) {
  v = (v || "").trim();
  if (!v) return "";
  const opts = c.options || [];
  let hit = opts.find((o) => o === v);
  if (hit) return hit;
  hit = opts.find((o) => String(o).toLowerCase() === v.toLowerCase());
  if (hit) return hit;
  return v;
}

/* ====================== 分页/加载条 ====================== */
function renderPageBar() {
  const bar = document.getElementById("pageBar");
  if (!bar) return;
  const _PAGE_ALL = 900000;
  const rows = getView();
  const total = rows.length;
  const ps = (STATE.pageSize && STATE.pageSize > 0 && STATE.pageSize < _PAGE_ALL) ? STATE.pageSize : _PAGE_ALL;
  const _all = (ps >= _PAGE_ALL) || (ps >= total);
  const pageCount = _all ? 1 : Math.ceil(total / ps);
  if (STATE.page > pageCount - 1) STATE.page = pageCount - 1;
  if (STATE.page < 0) STATE.page = 0;
  bar.innerHTML = "";
  const loaded = _all ? total : Math.min(total, (STATE.page + 1) * ps);
  const info = el("span", { class: "page-info", text: `共 ${total} 行 · 已加载 ${loaded} 行` });
  bar.appendChild(info);
  if (_newRowIds.size) {
    bar.appendChild(el("span", { class: "page-hint", text: `（含 ${_newRowIds.size} 行新增待定，点「刷新」后纳入筛选）` }));
  }
}

/* ---------- 表头右键：冻结列 ---------- */
let _headCtxIdx = -1;   // 当前右键所在的表头显示列（0=序号）
function closeHeadCtx() { document.getElementById("headCtxMenu").classList.add("hidden"); }
function setFrozen(n) {
  STATE.frozen = Math.max(0, n | 0);
  saveStateSoon();
  buildStatic(); renderBody();
}
function openHeadCtxMenu(e, displayIdx) {
  e.preventDefault();
  document.getElementById("ctxMenu").classList.add("hidden");   // 避免两个菜单同时出现
  _headCtxIdx = parseInt(displayIdx, 10);
  const menu = document.getElementById("headCtxMenu");
  const cols = userColumns();
  const names = ["序号"].concat(cols.map((c) => c.name));
  const colName = names[_headCtxIdx] || "?";
  const cur = STATE.frozen || 0;
  menu.innerHTML = "";
  const freezeTo = _headCtxIdx + 1;     // 冻结 0..displayIdx（含当前列，从最左连续）
  if (freezeTo !== cur) {
    menu.appendChild(el("div", {
      class: "ctx-item", text: "📌 冻结到「" + colName + "」（含左侧所有列）",
      onclick: () => { setFrozen(freezeTo); closeHeadCtx(); },
    }));
  }
  if (cur > 0) {
    menu.appendChild(el("div", {
      class: "ctx-item", text: "◀ 仅冻结序号列",
      onclick: () => { setFrozen(1); closeHeadCtx(); },
    }));
    menu.appendChild(el("div", {
      class: "ctx-item", text: "↔ 取消冻结（全部可左右滚动）",
      onclick: () => { setFrozen(0); closeHeadCtx(); },
    }));
  }
  if (!menu.children.length) {
    menu.appendChild(el("div", { class: "ctx-item muted", text: "（当前已冻结到此列）" }));
  }
  menu.style.position = "fixed";
  menu.style.left = Math.min(e.clientX, window.innerWidth - 210) + "px";
  menu.style.top = Math.min(e.clientY, window.innerHeight - 170) + "px";
  menu.classList.remove("hidden");
}

/* ---------- 行选中高亮（点击某行后保持高亮，便于左右滚动时定位） ---------- */
let _selectedRowId = null;
function selectRow(id) {
  _selectedRowIds.clear();          // 单行手势视为放弃多选
  _selectedRowId = id;
  document.querySelectorAll('#body tr[data-id]').forEach((tr) => {
    tr.classList.toggle("row-selected", tr.getAttribute("data-id") === String(id));
  });
}
function applyRowSelection() {
  document.querySelectorAll('#body tr[data-id]').forEach((tr) => {
    const on = _selectedRowIds.has(Number(tr.getAttribute("data-id")));
    tr.classList.toggle("row-selected", on);
  });
}

/* ---------- 撤销栈 ---------- */
let _undoStack = [];
function pushUndo(op) { _undoStack.push(op); }
function undo() {
  const op = _undoStack.pop();
  if (!op) { toast("没有可撤销的操作"); return; }
  if (op.type === "bulk-edit") {
    const edits = op.edits.map((e) => ({ id: e.id, field: e.field, value: e.oldValue }));
    fetch("api/cells", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user: USER, edits }),
    }).then((r) => r.json()).then((j) => {
      if (j.ok) {
        edits.forEach((e) => {
          const rec = DATA.find((x) => x.id === e.id);
          if (rec) rec[e.field] = e.oldValue;
        });
        renderActive();
        toast("已撤销批量粘贴");
      }
    });
  } else if (op.type === "cell") {
    // 单行保存 / 单行粘贴 / 整行粘贴的撤销：把该格恢复为旧值
    fetch("api/row/" + op.id, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ field: op.field, value: op.oldValue, user: USER }),
    }).then((r) => r.json()).then((j) => {
      if (j.ok) {
        const rec = DATA.find((x) => x.id === op.id);
        if (rec) rec[op.field] = op.oldValue;
        renderActive();
        toast("已撤销");
      } else toast("撤销失败: " + (j.msg || ""));
    }).catch(() => toast("撤销失败(网络)"));
  } else if (op.type === "delete") {
    // 删除撤销需要后端支持恢复，当前仅提示
    toast("删除操作无法撤销，请手动重新录入");
  } else {
    toast("已撤销");
  }
}

/* ---------- 排序面板 ---------- */
function openSortPop() {
  renderSortRules();
  const pop = document.getElementById("sortPop");
  const btn = document.getElementById("sortBtn");
  const rect = btn.getBoundingClientRect();
  pop.style.position = "fixed";
  pop.style.left = Math.min(rect.left, window.innerWidth - 290) + "px";
  pop.style.top = (rect.bottom + 4) + "px";
  pop.classList.remove("hidden");
}
function renderSortRules() {
  const list = document.getElementById("sortList");
  list.innerHTML = "";
  if (!STATE.sortRules.length) {
    list.appendChild(el("div", { class: "muted", text: "（暂未设置排序，默认按手动顺序）" }));
  }
  STATE.sortRules.forEach((rule, i) => {
    const fs = el("select", { class: "sr-field" });
    META.columns.forEach((c) => fs.appendChild(el("option", { value: c.name, text: c.name })));
    fs.value = rule.field;
    fs.onchange = () => { rule.field = fs.value; saveStateSoon(); };
    const ds = el("select", { class: "sr-dir" });
    ds.appendChild(el("option", { value: "asc", text: "升序 ↑" }));
    ds.appendChild(el("option", { value: "desc", text: "降序 ↓" }));
    ds.value = rule.dir;
    ds.onchange = () => { rule.dir = ds.value; saveStateSoon(); };
    list.appendChild(el("div", { class: "sr-row" },
      el("span", { class: "sr-idx", text: String(i + 1) }), fs, ds,
      el("span", { class: "sr-del", text: "×", title: "移除",
        onclick: () => { STATE.sortRules.splice(i, 1); saveStateSoon(); renderSortRules(); refreshSortBadges(); renderActive(); } })
    ));
  });
}

/* ---------- 视图切换 ---------- */
function setView(v) {
  STATE.view = v; saveStateSoon();
  document.querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  document.querySelectorAll(".view").forEach((s) => s.classList.toggle("active", s.id === "view-" + v));
  renderActive();
}

/* ---------- 班列类型切换（散舱 / 专列 分开展示） ---------- */
function syncMidViewBtn() {
  const mid = document.getElementById("midViewBtn");
  if (!mid) return;
  if (STATE.trainType === "专列") { mid.textContent = "Dashboard"; mid.dataset.view = "dash"; }
  else { mid.textContent = "看板"; mid.dataset.view = "board"; }
}
// 班列类型切换时，列筛选/包含筛选按类型隔离（专列/散舱/全部各自独立）
let _filterSnapshots = {};
function setTrainType(t) {
  const old = STATE.trainType || "全部";
  _filterSnapshots[old] = { filters: Object.assign({}, STATE.filters), contains: Object.assign({}, STATE.contains) };
  STATE.trainType = t; STATE.page = 0; saveStateSoon();
  const snap = _filterSnapshots[t];
  STATE.filters = snap ? Object.assign({}, snap.filters) : {};
  STATE.contains = snap ? Object.assign({}, snap.contains) : {};
  document.querySelectorAll("#trainTypeSeg .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.type === t));
  syncMidViewBtn();
  if (t === "专列") {
    setView("dash");   // 专列 → Dashboard v0.2；setView 内部 renderActive 已触发 renderDashboard
  } else {
    if (STATE.view !== "grid") setView("grid");
    else renderActive();
  }
  toggleBulkBar();
}

/* ---------- 专列 Dashboard v0.2 ---------- */
let DASH = [];
let DASH_CHIP = "全部";
let DASH_YEAR = "";
let DASH_MONTH = "";
/* HTML 转义（innerHTML 拼接时必须用） */
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function renderDashboard() {
  fetch("api/train_summary")
    .then((r) => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then((list) => { DASH = list || []; })
    .catch((err) => { console.error("[dashboard] 加载失败", err); toast("专列看板加载失败：" + err.message); DASH = []; })
    .then(() => {
      // 渲染单独兜底：渲染报错不再伪装成"加载失败"
      try { populateDashDateFilters(); renderDash(); }
      catch (err) { console.error("[dashboard] 渲染失败", err); toast("专列看板渲染出错：" + err.message); }
    });
}
function getMonthFromDate(dateStr) {
  if (!dateStr) return null;
  const m = String(dateStr).match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (m) return m[2];
  const d = new Date(dateStr);
  if (!isNaN(d.getTime())) return String(d.getMonth() + 1).padStart(2, "0");
  return null;
}
function populateDashDateFilters() {
  const ysel = document.getElementById("dashYearSel");
  const msel = document.getElementById("dashMonthSel");
  if (ysel) {
    const ys = Array.from(new Set(DASH.map((t) => t.year).filter(Boolean))).sort((a, b) => String(b).localeCompare(String(a)));
    const curY = DASH_YEAR;
    ysel.innerHTML = '<option value="">全部年份</option>' + ys.map((y) => '<option value="' + esc(y) + '">' + esc(y) + ' 年</option>').join("");
    ysel.value = ys.indexOf(curY) >= 0 ? curY : "";
  }
  if (msel) {
    const months = Array.from(new Set(DASH.map((t) => getMonthFromDate(t.发班时间)).filter(Boolean))).sort();
    const curM = DASH_MONTH;
    msel.innerHTML = '<option value="">全部月份</option>' + months.map((m) => '<option value="' + esc(m) + '">' + esc(m) + ' 月</option>').join("");
    msel.value = months.indexOf(curM) >= 0 ? curM : "";
  }
}
function renderDash() {
  let list = DASH.filter((t) => {
    if (DASH_CHIP === "未发运") return t.status === "未发运";
    if (DASH_CHIP === "已发运") return t.status === "已发运";
    if (DASH_CHIP === "有异常箱") return t.有异常 && t.status !== "已完成";  // 仅未完成
    return true;
  });
  if (DASH_YEAR) list = list.filter((t) => t.year === DASH_YEAR);
  if (DASH_MONTH) list = list.filter((t) => getMonthFromDate(t.发班时间) === DASH_MONTH);

  // 顶部总览卡（按当前筛选结果统计）
  const sum = { notDep: 0, issue: 0, box: 0, completed: 0 };
  list.forEach((t) => {
    if (t.status === "未发运") sum.notDep++;
    if (t.status === "已完成") sum.completed++;
    if (t.status !== "已完成") sum.issue += t.异常数 || 0;  // 箱号异常仅显示未完成
    sum.box += t.total || 0;
  });
  const setStat = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
  setStat("st-notdeparted", sum.notDep);
  setStat("st-issue", sum.issue);
  setStat("st-completed", sum.completed);
  setStat("st-box", sum.box);

  const cnt = document.getElementById("dashCount");
  if (cnt) cnt.textContent = "共 " + list.length + " 班专列";
  const listEl = document.getElementById("dashList");
  if (!listEl) return;
  listEl.innerHTML = "";
  const yb = document.getElementById("dashYearBar");
  if (!list.length) {
    listEl.innerHTML = '<div class="dash-empty">当前筛选下没有专列。</div>';
    if (yb) yb.innerHTML = "";
    return;
  }
  // #110 按发班年份分组（年份倒序，"未知"排最后）
  const years = {};
  list.forEach((t) => { (years[t.year] = years[t.year] || []).push(t); });
  const yk = Object.keys(years).sort((a, b) => {
    if (a === "未知") return 1;
    if (b === "未知") return -1;
    return b.localeCompare(a);
  });
  if (yb) yb.innerHTML = yk.map((y) => '<span class="year-chip">' + esc(y) + " 年 · " + years[y].length + " 班</span>").join("");
  yk.forEach((y) => {
    const sec = el("div", { class: "dash-year-sec" });
    sec.appendChild(el("div", { class: "year-head" },
      el("span", { class: "yh-year", text: y === "未知" ? "未知年份" : y + " 年" }),
      el("span", { class: "yh-count", text: years[y].length + " 班专列" })));
    const wrap = el("div", { class: "dash-cards" });
    years[y].forEach((t) => wrap.appendChild(buildTrainCard(t)));
    sec.appendChild(wrap);
    listEl.appendChild(sec);
  });
}

function buildTrainCard(t) {
  const bad = !!t.有异常;
  const card = el("div", { class: "train " + (bad ? "abnormal" : "normal"), "data-key": t.train + "|" + t.year });

  /* ---- 头部（点任意处展开异常箱） ---- */
  const head = el("div", { class: "head", title: "点击展开 / 收起异常箱" });

  head.appendChild(el("div", { class: "bl", text: t.train + (t.year && t.year !== "未知" ? " · " + t.year : "") }));

  const meta = el("div", { class: "meta" });
  const metaItem = (k, v) => el("div", {}, el("span", { class: "mut", text: k }), el("b", { text: v || "—" }));
  meta.appendChild(metaItem("口岸", t.口岸));
  meta.appendChild(metaItem("发班", t.发班时间));
  meta.appendChild(metaItem("负责公司", t.负责公司));
  // 箱数 + 悬停显示 SOC/COC
  const bc = el("div", { class: "box-count" },
    el("span", { class: "num", text: t.total + " 箱" }),
    el("span", { class: "soc-coc-tip", text: "SOC：" + t.soc + " 箱　COC：" + t.coc + " 箱" + (t.退舱 ? "　（另有退舱 " + t.退舱 + " 箱，未计入）" : "") }));
  meta.appendChild(bc);
  meta.appendChild(el("span", { class: "status-tag " + (bad ? "err" : "ok"), text: bad ? "有异常 " + t.异常数 : "正常" }));
  // 手动班列状态（未发运/已发运/已完成）
  const stWrap = el("div", { class: "train-status-wrap" });
  stWrap.appendChild(el("span", { class: "mut", text: "状态" }));
  const stSel = el("select", { class: "train-status-sel", title: "手动维护班列状态" });
  stSel.dataset.prev = t.status || "未发运";
  ["未发运", "已发运", "已完成"].forEach((s) => {
    const o = el("option", { value: s, text: s });
    if (s === (t.status || "未发运")) o.selected = true;
    stSel.appendChild(o);
  });
  stSel.addEventListener("click", (e) => e.stopPropagation());  // 不触发手风琴展开
  stSel.addEventListener("change", (e) => {
    e.stopPropagation();
    setTrainStatus(t.train, t.year, stSel.value, stSel);
  });
  stWrap.appendChild(stSel);
  meta.appendChild(stWrap);
  head.appendChild(meta);

  const prog = el("div", { class: "prog" });
  prog.appendChild(mkProg("箱号", t.box_done, t.box_total, false));
  prog.appendChild(mkProg("报放", t.fang_done, t.fang_total, true));
  head.appendChild(prog);

  const acts = el("div", { class: "head-actions" });
  acts.appendChild(el("button", {
    class: "btn-detail", text: "查看详细",
    onclick: (e) => { e.stopPropagation(); dashViewDetail(t.train, t.year); },
  }));
  acts.appendChild(el("div", { class: "arrow", text: "›" }));
  head.appendChild(acts);

  card.appendChild(head);

  /* ---- 异常箱面板 ---- */
  const panel = el("div", { class: "issue-panel" });
  panel.appendChild(el("h4", {},
    el("span", { text: "⚠ 非正常箱子" }),
    el("span", { class: "count", text: String(t.异常数 || 0) })));
  const ilist = el("div", { class: "issue-list" });
  if (t.anomalies && t.anomalies.length) {
    t.anomalies.forEach((a) => {
      const row = el("div", { class: "issue-item" });
      row.appendChild(el("span", { class: "ibox", text: a.箱号 || "（空箱号）" }));
      if (a.箱号状态 === "未确认") row.appendChild(el("span", { class: "ist caodan-wait", text: "箱号未填" }));
      if (a.报放状态 !== "已上传") row.appendChild(el("span", { class: "ist bf-wait", text: "报放:" + (a.报放状态 || "未出") }));
      if (a.记录状态 && a.记录状态 !== "正常") row.appendChild(el("span", { class: "ist st-other", text: a.记录状态 }));
      row.appendChild(el("span", { class: "iremark" + (a.备注 ? " has" : ""), title: a.备注 || "", text: a.备注 || "—" }));
      ilist.appendChild(row);
    });
  } else {
    ilist.appendChild(el("div", { class: "issue-empty", text: "✅ 本列暂无非正常箱子" }));
  }
  panel.appendChild(ilist);
  card.appendChild(panel);

  /* ---- 交互：手风琴展开（点头部或进度条都行；「查看详细」除外） ---- */
  const toggle = (e) => {
    if (e) e.stopPropagation();
    const open = card.classList.contains("show-issues");
    document.querySelectorAll(".train.show-issues").forEach((o) => o.classList.remove("show-issues"));
    if (!open) card.classList.add("show-issues");
  };
  head.addEventListener("click", (e) => {
    if (e.target.closest(".btn-detail")) return;
    toggle(e);
  });
  return card;
}

function mkProg(label, done, total, amber) {
  const pct = total ? Math.round((done / total) * 100) : 0;
  const wrap = el("div", { class: "pbar" });
  wrap.appendChild(el("span", { class: "name", text: label }));
  const track = el("span", { class: "track" });
  // 满进度一律绿色；未满时报放条用琥珀色以示待跟进
  const cls = "fill" + (pct >= 100 ? "" : amber ? " amber" : "");
  track.appendChild(el("span", { class: cls, style: "width:" + pct + "%" }));
  wrap.appendChild(track);
  wrap.appendChild(el("span", { class: "num", text: done + "/" + total }));
  return wrap;
}
function setTrainStatus(train, year, status, sel) {
  fetch("api/train_status", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ train, year, status }),
  })
    .then((r) => r.json())
    .then((j) => {
      if (j.ok) { toast("班列状态已更新：" + status); renderDashboard(); }
      else { toast("保存失败：" + (j.msg || "")); if (sel) sel.value = sel.dataset.prev || "未发运"; }
    })
    .catch((e) => { toast("保存失败：" + e.message); if (sel) sel.value = sel.dataset.prev || "未发运"; });
}
function dashViewDetail(train, year) {
  // 跳转到表格视图，按 班列号(精确) + 发班年份(包含) 过滤
  STATE.trainType = "专列";
  document.querySelectorAll("#trainTypeSeg .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.type === "专列"));
  STATE.month = "";
  STATE.company = [];
  STATE.search = "";
  STATE.filters = STATE.filters || {};
  STATE.filters["班列号"] = [train];
  STATE.contains = STATE.contains || {};
  STATE.contains["发班时间"] = year;
  STATE.active_tab = null;
  setView("grid");
  buildStatic();
  renderActive();
  const t = DASH.find((x) => x.train === train && x.year === year);
  toast("已定位 " + train + "（" + year + "）共 " + (t ? t.total : "") + " 箱（班列号+年份筛选已生效）");
}
/* 专列模式才显示批量设置条；散舱/全部隐藏 */
function toggleBulkBar() {
  const bar = document.getElementById("bulkBar");
  if (!bar) return;
  if (STATE.trainType === "专列") { bar.classList.remove("hidden"); renderBulkBar(); }
  else bar.classList.add("hidden");
}
/* 专列批量设置条：箱号/封号逐条双击改；其余字段一键全部修改（复用 api/cells） */
function renderBulkBar() {
  const bar = document.getElementById("bulkBar");
  if (!bar) return;
  bar.style.cssText = "display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:6px 10px;background:#f3f7f6;border-bottom:1px solid #d8e3e0;font-size:13px;";
  bar.innerHTML = "";
  const sel = "padding:3px 6px;border:1px solid #c8d6d2;border-radius:5px;background:#fff;max-width:160px;";
  bar.appendChild(el("span", { class: "bulk-title", text: "专列批量设置：", style: "font-weight:600;color:#0f6e56;" }));
  // 字段选择（排除 班列类型/箱号/封号：箱号封号专列里逐条改）
  const fsel = el("select", { id: "bulkField", style: sel });
  META.columns.filter((c) => c.name !== "班列类型" && c.name !== "箱号" && c.name !== "封号")
    .forEach((c) => fsel.appendChild(el("option", { value: c.name, text: c.name })));
  bar.appendChild(fsel);
  // 值控件容器（依字段类型动态切换）
  const valWrap = el("span", { id: "bulkValWrap" });
  bar.appendChild(valWrap);
  // 写入模式：覆盖全部 / 仅补空
  const mode = el("select", { id: "bulkMode", style: sel },
    el("option", { value: "overwrite", text: "覆盖全部" }),
    el("option", { value: "fill", text: "仅补空" }));
  bar.appendChild(mode);
  // 范围：全部专列 / 当前结果
  const scope = el("select", { id: "bulkScope", style: sel },
    el("option", { value: "all", text: "全部专列" }),
    el("option", { value: "current", text: "当前结果" }));
  bar.appendChild(scope);
  bar.appendChild(el("button", { class: "tbtn", text: "应用到整批", onclick: applyBulkSet }));
  bar.appendChild(el("span", { class: "bulk-hint", text: "箱号/封号请逐条双击修改", style: "color:#8a9a96;" }));
  fsel.onchange = () => renderBulkVal(fsel.value);
  renderBulkVal(fsel.value);
}
function renderBulkVal(field) {
  const wrap = document.getElementById("bulkValWrap");
  if (!wrap) return;
  wrap.innerHTML = "";
  const c = colDef(field);
  const inp = "padding:3px 6px;border:1px solid #c8d6d2;border-radius:5px;background:#fff;min-width:120px;";
  if (c && c.type === "select") {
    const s = el("select", { class: "bulk-inp", style: inp });
    s.appendChild(el("option", { value: "", text: "（清空）" }));
    (c.options || []).forEach((o) => s.appendChild(el("option", { value: o, text: o })));
    wrap.appendChild(s);
  } else if (c && c.type === "date") {
    wrap.appendChild(el("input", { type: "date", class: "bulk-inp", style: inp }));
  } else {
    wrap.appendChild(el("input", { type: "text", class: "bulk-inp", style: inp, placeholder: "输入值" }));
  }
}
function applyBulkSet() {
  const field = document.getElementById("bulkField").value;
  const c = colDef(field);
  const valEl = document.querySelector("#bulkValWrap select, #bulkValWrap input");
  let value = valEl ? valEl.value : "";
  if (c && c.type === "date") value = input2d(value);
  const scope = document.getElementById("bulkScope").value;
  const mode = document.getElementById("bulkMode").value;
  const targets = scope === "all"
    ? DATA.filter((r) => (r["班列类型"] || "散舱") === "专列")
    : getView();
  if (!targets.length) { toast("没有可更新的专列行"); return; }
  const edits = [];
  targets.forEach((r) => {
    const cur = r[field] == null ? "" : String(r[field]);
    if (mode === "fill" && cur.trim() !== "") return;   // 仅补空：跳过非空
    edits.push({ id: r.id, field, value });
  });
  if (!edits.length) { toast("没有需要补写的空值行"); return; }
  if (!confirm("确认把【" + edits.length + "】条专列的「" + field + "」设为「" + (value || "空") + "」？\n模式：" + (mode === "fill" ? "仅补空" : "覆盖全部"))) return;
  fetch("api/cells", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user: USER, edits }),
  }).then((r) => r.json()).then((j) => {
    if (j.ok) {
      toast("已批量更新 " + edits.length + " 行「" + field + "」");
      syncVersion(); loadRows().then(renderAll);
    } else toast("批量更新失败: " + (j.msg || ""));
  }).catch(() => toast("批量更新失败(网络)"));
}

/* ---------- 表格线（按用户独立开关，存 localStorage，仅本人可见） ---------- */
function applyGridLines() {
  const on = localStorage.getItem("yxo_gridlines_" + USER) === "1";
  const gs = document.querySelector(".grid-scroll");
  if (gs) gs.classList.toggle("show-gridlines", on);
  const btn = document.getElementById("gridLinesBtn");
  if (btn) btn.classList.toggle("active", on);
}
function toggleGridLines() {
  const on = localStorage.getItem("yxo_gridlines_" + USER) === "1";
  localStorage.setItem("yxo_gridlines_" + USER, on ? "0" : "1");
  applyGridLines();
  toast((!on ? "已显示" : "已隐藏") + "表格线（仅你自己可见）");
}

/* ---------- 启动 ---------- */
function enterApp() {
  loadUserState().then(() => {
    if (!STATE.month && STATE.month !== "") STATE.month = pickDefaultMonth();
    buildStatic(); updateCompanyBtn(); setView(STATE.view || "grid"); renderAll();
    applyGridLines();
    // 班列类型切换初始化（active 状态 + 专列批量条显隐）
    document.querySelectorAll("#trainTypeSeg .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.type === STATE.trainType));
    toggleBulkBar();
    syncVersion();
    // 系统管理入口：毛骁洋 全权；杨雅雯/冯茜/韩文豪 受限（仅价格维护/选项维护/回收站）。服务端接口另有权限校验，前端只是隐藏入口。
    const LIMITED_ADMINS = ["杨雅雯","冯茜","韩文豪"];
    document.getElementById("adminBtn").classList.toggle("hidden", !(USER === "毛骁洋" || LIMITED_ADMINS.includes(USER)));
    // 托书生成入口：独立分组，仅毛骁洋/杨雅雯（服务端 config.TUOSHU_ADMINS 另有校验，前端只隐藏入口）
    const TUOSHU_ADMINS = ["毛骁洋","杨雅雯"];
    const tsBtn = document.getElementById("tuoshuBtn");
    if (tsBtn) tsBtn.classList.toggle("hidden", !TUOSHU_ADMINS.includes(USER));
    // 舱单导入入口：仅 config.MANIFEST_ADMIN（毛骁洋），服务端另有校验，前端只隐藏入口
    const MANIFEST_ADMINS = (META && META.manifest_admins) || ["毛骁洋"];
    const mfBtn = document.getElementById("manifestBtn");
    if (mfBtn) mfBtn.classList.toggle("hidden", !MANIFEST_ADMINS.includes(USER));
    setInterval(pollRemoteChanges, 4000);   // 每 4 秒检查他人改动（同 WPS 在线协同）
  });
}
/* 账户选择界面：首次进入 / 无 user 时显示；也可通过 ?user=姓名 无感进入 */
function showUserGate() {
  const gate = document.getElementById("userGate");
  const wrap = document.getElementById("gateCards"); wrap.innerHTML = "";
  META.users.forEach((u) => {
    const card = el("div", { class: "gate-card" },
      el("div", { class: "gate-avatar", text: u.slice(0, 1) }),
      el("div", { class: "gate-name", text: u }),
      el("div", { class: "gate-sub", text: "点击以该身份进入" }));
    card.onclick = () => {
      USER = u; localStorage.setItem("yxo_user", u);
      gate.classList.add("hidden");
      enterApp();
    };
    wrap.appendChild(card);
  });
  gate.classList.remove("hidden");
}
function init() {
  loadMeta().then(loadRows).then(() => {
    const urlUser = new URLSearchParams(location.search).get("user");
    // 1) URL ?user=xxx 无感进入；2) 否则用 localStorage 记住的上次选择；3) 都没有则显示选择界面
    if (urlUser && META.users.includes(urlUser)) {
      USER = urlUser; localStorage.setItem("yxo_user", USER); enterApp();
    } else {
      USER = localStorage.getItem("yxo_user") || "";
      if (USER && META.users.includes(USER)) enterApp();
      else showUserGate();
    }
  });

  // 工具条
  document.querySelectorAll(".seg-btn[data-view]").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
  // 班列类型切换（散舱 / 专列 / 全部）
  document.querySelectorAll("#trainTypeSeg .seg-btn").forEach((b) => b.addEventListener("click", () => setTrainType(b.dataset.type)));
  document.querySelectorAll("#dashChips .chip").forEach((b) => b.addEventListener("click", () => {
    DASH_CHIP = b.dataset.chip;
    document.querySelectorAll("#dashChips .chip").forEach((x) => x.classList.toggle("on", x === b));
    renderDash();
  }));
  const dys = document.getElementById("dashYearSel");
  if (dys) dys.addEventListener("change", () => { DASH_YEAR = dys.value; renderDash(); });
  const dms = document.getElementById("dashMonthSel");
  if (dms) dms.addEventListener("change", () => { DASH_MONTH = dms.value; renderDash(); });
  syncMidViewBtn();
  document.getElementById("search").addEventListener("input", (e) => { STATE.search = e.target.value; STATE.page = 0; saveStateSoon(); renderActive(); });
  document.getElementById("search").addEventListener("blur", (e) => {
    const v = e.target.value.trim();
    if (v !== e.target.value) { e.target.value = v; STATE.search = v; saveStateSoon(); renderActive(); }
  });
  const fbadge = document.getElementById("filterBadge");
  if (fbadge) fbadge.addEventListener("click", clearAllColFilters);
  document.getElementById("companyBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    const pop = document.getElementById("companyPop");
    if (pop.classList.contains("hidden")) {
      const rect = e.target.getBoundingClientRect();
      pop.style.position = "fixed"; pop.style.left = rect.left + "px"; pop.style.top = (rect.bottom + 4) + "px";
    }
    pop.classList.toggle("hidden");
  });
  document.getElementById("companyAll").addEventListener("click", (e) => {
    e.preventDefault(); STATE.company = distinct(META.company_field); STATE.page = 0; saveStateSoon(); buildStatic(); updateCompanyBtn(); renderActive();
  });
  document.getElementById("companyClear").addEventListener("click", (e) => {
    e.preventDefault(); STATE.company = []; STATE.page = 0; saveStateSoon(); buildStatic(); updateCompanyBtn(); renderActive();
  });
  document.getElementById("addRowBtn").addEventListener("click", () => {
    insertRow("end", null);   // 与右键“末尾新增”一致：写入后加入 _newRowIds，暂不受筛选约束，可一口气加多行逐格编辑
  });
  const importFile = document.getElementById("importFile");
  document.getElementById("importBtn").addEventListener("click", () => importFile.click());
  importFile.addEventListener("change", () => {
    const f = importFile.files[0];
    if (!f) return;
    // 选择散舱 / 专列（确定=散舱，取消=专列，再确认一次防误触）
    let trainType;
    if (confirm("从「" + f.name + "」导入。\n\n本表是【散舱】吗？\n\n【确定】= 散舱     【取消】= 专列\n（已存在的行只刷新基础字段，保留你填的操作字段）")) {
      trainType = "散舱";
    } else {
      if (!confirm("按【专列】导入「" + f.name + "」，确认吗？\n（点取消则放弃本次导入）")) {
        importFile.value = ""; return;
      }
      trainType = "专列";
    }
    const doUpload = (force) => {
      const fd = new FormData();
      fd.append("file", f);
      fd.append("train_type", trainType);
      if (force) fd.append("force", "1");
      toast("导入中…");
      fetch("api/import_upload", { method: "POST", body: fd })
        .then((r) => r.json()).then((j) => {
          if (j.ok) {
            importFile.value = "";
            loadRows().then(() => { buildStatic(); renderAll(); toast("按【" + (j.train_type || trainType) + "】导入 " + j.imported + " 行"); });
          } else if (j.need_confirm) {
            if (confirm("⚠ " + j.msg)) { doUpload(true); }
            else { importFile.value = ""; toast("已取消导入，请重新点导入并选对类型"); }
          } else {
            importFile.value = "";
            toast("导入失败: " + (j.msg || ""));
          }
        })
        .catch(() => { importFile.value = ""; toast("导入失败(网络)"); });
    };
    doUpload(false);
  });
  document.getElementById("helpBtn").addEventListener("click", () => {
    alert(
      "使用说明\n" +
      "1. 表格：点列头右侧 ⏷ 可下拉多选（选项随数据自动更新）；列下方“包含”框可输入关键字。多列条件同时生效。\n" +
      "2. 排序：点列名可升/降序切换；按住 Shift 点多个列可叠加多条件（如先时间升序、再客编升序）。也可点“⇅ 排序”按钮在面板里增删条件。\n" +
      "3. 新增/删除：表格里右键某一行，可选“在上方/下方插入”“在末尾新增”“删除此行”；也可用工具栏“＋ 新增行”。\n" +
      "4. 公司：点“公司”可多选，满足一人看多家客户。\n" +
      "5. 个人：右上“我是”选自己，筛选/看板标签只属于你，不影响他人。\n" +
      "6. 看板：选分组字段生成列；点“＋新建标签”把某个条件存成标签栏，可✎编辑、×关闭。\n" +
      "7. 月份标签：工具条下方自动按“发班时间”分月，默认打开当前月，点“全部”看全部。\n" +
      "8. 统计：按当前筛选汇总单价（合计/平均/按公司/按月）。\n" +
      "9. 单元格直接点击修改，单箱价格空时点“算价”自动填；目的站可直接输入或从下拉筛选。\n" +
      "10. 点“选项维护”可增删下拉字段的选项（无权限限制，4 人都能改，同 WPS）。\n" +
      "11. 列宽：把鼠标移到列头右缘（变 🔄 光标）拖动即可调宽窄；宽度只存在你自己这边，不影响其他人。\n" +
      "12. 冻结列：在任意【列名】上点右键，选“冻结到 XX（含左侧所有列）”，该列及左侧列会固定不动，方便左右滚动时对照；可“取消冻结”或“仅冻结序号列”。只能从最左连续冻结，避免冻结中间列错位。"
    );
  });
  document.getElementById("newTabBtn").addEventListener("click", (e) => openTabPop(null, e));
  document.getElementById("fieldBtn").addEventListener("click", (e) => { e.stopPropagation(); openFieldPop(); });
  const gridBtn = document.getElementById("gridLinesBtn");
  if (gridBtn) gridBtn.addEventListener("click", (e) => { e.stopPropagation(); toggleGridLines(); });
  document.getElementById("exportBtn").addEventListener("click", () => exportXlsx());
  document.getElementById("refreshBtn").addEventListener("click", () => {
    _selectedRowIds.clear();
    loadRows().then(() => { renderAll(); toast("已刷新，新增待定行已纳入筛选"); });
  });

  // 无限滚动：接近表格底部时自动追加下一页
  document.querySelector(".grid-scroll").addEventListener("scroll", (e) => {
    const gs = e.target;
    if (gs.scrollTop + gs.clientHeight >= gs.scrollHeight - 60) {
      loadMoreRows();
    }
  });

  // 排序按钮 + 排序面板
  document.getElementById("sortBtn").addEventListener("click", (e) => { e.stopPropagation(); openSortPop(); });
  document.getElementById("sortAdd").addEventListener("click", () => {
    STATE.sortRules.push({ field: META.columns[0].name, dir: "asc" });
    saveStateSoon(); renderSortRules();
  });
  document.getElementById("sortApply").addEventListener("click", () => {
    document.getElementById("sortPop").classList.add("hidden");
    refreshSortBadges(); renderActive();
  });
  document.getElementById("sortClear").addEventListener("click", () => {
    STATE.sortRules = []; saveStateSoon(); refreshSortBadges(); renderActive();
    renderSortRules(); document.getElementById("sortPop").classList.add("hidden");
  });

  // 右键菜单（表格区域：表头=冻结列；数据行=新增/删除/算价）
  document.querySelector(".grid-scroll").addEventListener("contextmenu", (e) => {
    e.preventDefault();
    const th = e.target.closest("th");
    if (th && th.hasAttribute("data-display")) {
      openHeadCtxMenu(e, th.getAttribute("data-display"));
      return;
    }
    const tr = e.target.closest("tr[data-id]");
    // 算出右键所在的列（字段名）：td.cellIndex 0 是序号列，数据列从 1 开始
    let field = null;
    const td = e.target.closest("td");
    if (td && tr && td.cellIndex > 0) {
      const cols = userColumns();
      const c = cols[td.cellIndex - 1];
      if (c) field = c.name;
    }
    openCtxMenu(e, tr ? parseInt(tr.getAttribute("data-id"), 10) : null, field);
  });
  document.getElementById("ctxMenu").addEventListener("click", (e) => {
    const item = e.target.closest(".ctx-item");
    if (!item) return;
    const act = item.getAttribute("data-act");
    document.getElementById("ctxMenu").classList.add("hidden");
    if (act === "delete") {
      if (_selectedRowIds.size) deleteRows([..._selectedRowIds]);
      else if (_ctxRowId != null) deleteRow(_ctxRowId);
    }
    else if (act === "before") insertRow("before", _ctxRowId);
    else if (act === "after") insertRow("after", _ctxRowId);
    else if (act === "price") { if (_ctxRowId != null) priceRow(_ctxRowId); }
    else if (act === "priceall") { batchPrice(); }
    else if (act === "copycell") { copyCell(_ctxRowId, _ctxField); }
    else if (act === "copy") { copyRow(_ctxRowId); }
    else if (act === "paste") { pasteToRow(_ctxRowId); }
    else insertRow("end", null);
  });

  // 页面 copy 事件：把用户复制的文本捕获到内存兜底变量
  // 优先读浏览器剪贴板数据；再读 input/textarea 选中文本；最后 fallback 到页面选择。
  document.addEventListener("copy", (e) => {
    const active = document.activeElement;
    // 编辑中或焦点在输入框 → 让浏览器原生复制选中文本，不拦截
    if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA")) return;
    // 网格有选区（矩形 / Ctrl+多选）→ 写 TSV 矩阵（回 Excel 可铺开）
    const tsv = buildSelectionTSV();
    if (tsv != null) {
      let n = 0;
      tsv.split("\n").forEach((ln) => { n += ln.split("\t").length; });
      e.preventDefault();
      if (e.clipboardData) e.clipboardData.setData("text", tsv);
      _systemClipboard = tsv;
      writeClipboard(tsv);   // HTTP 下 execCommand 兜底，Excel 仍能识别制表符矩阵
      toast("已复制选中区域（" + n + " 个单元格）");
      return;
    }
    // 否则走原逻辑：读页面选中文本进 _systemClipboard
    let text = "";
    if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA")) {
      text = active.value.substring(active.selectionStart || 0, active.selectionEnd || 0);
    }
    if (!text && window.getSelection) {
      text = window.getSelection().toString();
    }
    if (text) _systemClipboard = text;
  });
  // cut 也一并捕获（剪切搜索框文字再粘贴的场景）
  document.addEventListener("cut", () => {
    const active = document.activeElement;
    if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA")) {
      const t = active.value.substring(active.selectionStart || 0, active.selectionEnd || 0);
      if (t) _systemClipboard = t;
    }
  });

  // 全局粘贴（§4.4）：焦点在输入框/下拉→原生；否则以选中单元格为锚点铺开
  document.addEventListener("paste", (e) => {
    const active = document.activeElement;
    // 焦点在任何输入框/下拉（搜索框、表头筛选框、单元格编辑器内）→ 原生粘贴，网格不抢
    if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA" || active.tagName === "SELECT")) return;
    if (!_activeCell.id) return;             // 没选中任何单元格 → 放行
    e.preventDefault();
    let raw = (e.clipboardData || window.clipboardData).getData("text");
    if (raw) _systemClipboard = raw;
    if (!raw) raw = _systemClipboard || "";
    if (!raw) { toast("剪贴板为空"); return; }
    const cols = userColumns();
    const anchorField = (_activeCell.field && cols.find((c) => c.name === _activeCell.field)) ? _activeCell.field : (cols[0] || {}).name;
    if (!anchorField) { toast("没有可粘贴的列"); return; }
    doGridPaste(_activeCell.id, anchorField, raw);   // 未编辑态 → 铺开（保留原能力）
  });

  // 全局撤销：Ctrl+Z
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey && (e.key === "z" || e.key === "Z") && !e.shiftKey && !e.altKey && !e.metaKey) {
      const active = document.activeElement;
      if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA" || active.tagName === "SELECT")) return;
      e.preventDefault();
      undo();
    }
  });

  // 键入即编辑（§4.2）：已选中某单元格且未处于编辑态时，敲单字符直接开编辑器并预填
  document.addEventListener("keydown", (e) => {
    const active = document.activeElement;
    if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA" || active.tagName === "SELECT")) return; // 编辑中→原生
    if (e.key === "Escape") {
      if (_selectedRowIds.size) { _selectedRowIds.clear(); applyRowSelection(); }
      return;
    }
    if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA" || active.tagName === "SELECT")) return; // 编辑中→原生
    if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA" || active.tagName === "SELECT")) return; // 编辑中→原生
    if (!_activeCell.id) return;                  // 没选中单元格 → 不拦截
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key == null || e.key.length !== 1) return;   // 单字符（方向键等 length>1 跳过）
    if (e.key === "Enter" || e.key === "Tab" || e.key === "Escape") return;
    const td = findCellTd(_activeCell.id, _activeCell.field);
    if (!td || td.querySelector("input,select,textarea")) return;
    e.preventDefault();
    const r = DATA.find((x) => x.id === _activeCell.id);
    const c = userColumns().find((cc) => cc.name === _activeCell.field);
    if (!r || !c) return;
    openCellEditor(r, c, td, e.key);
  });

  // 点击空白处关闭弹层
  document.addEventListener("click", (e) => {
    const fp = document.getElementById("filterPop");
    if (!fp.classList.contains("hidden") && !fp.contains(e.target) && !e.target.closest(".funnel")) fp.classList.add("hidden");
    const cp = document.getElementById("companyPop");
    if (!cp.classList.contains("hidden") && !cp.contains(e.target) && e.target.id !== "companyBtn" && !e.target.closest("#companyBtn")) cp.classList.add("hidden");
    const tp = document.getElementById("tabPop");
    if (!tp.classList.contains("hidden") && !tp.contains(e.target) && e.target.id !== "newTabBtn" && !e.target.closest("#newTabBtn")) tp.classList.add("hidden");
    const cm = document.getElementById("ctxMenu");
    if (!cm.classList.contains("hidden") && !cm.contains(e.target)) cm.classList.add("hidden");
    const hm = document.getElementById("headCtxMenu");
    if (!hm.classList.contains("hidden") && !hm.contains(e.target)) hm.classList.add("hidden");
    const sp = document.getElementById("sortPop");
    if (!sp.classList.contains("hidden") && !sp.contains(e.target) && e.target.id !== "sortBtn" && !e.target.closest("#sortBtn")) sp.classList.add("hidden");
    const fp2 = document.getElementById("fieldPop");
    if (!fp2.classList.contains("hidden") && !fp2.contains(e.target) && e.target.id !== "fieldBtn" && !e.target.closest("#fieldBtn")) fp2.classList.add("hidden");
  });
}

document.addEventListener("DOMContentLoaded", init);
