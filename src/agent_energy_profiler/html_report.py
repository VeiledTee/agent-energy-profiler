"""Self-contained, offline HTML report of one run.

    agent-energy-profiler report RUN --html report.html
    html_report.write("agent-energy-profiler-runs/my-run", "report.html")

One file: the view model (view_model.build) as an inert JSON block, plus a
small script that displays it. Nothing is fetched. The Content-Security-Policy
allows only this file's own script and stylesheet (by hash), so text from the
recording (labels, allowed attributes) can never run as code; the script
also only ever inserts text, never markup.

The script displays, sorts and filters. It never adds energy, compares
measured with estimated values or decides what is ranked: the view model has
done all of that, with the reports' own rules.
"""

import base64
import hashlib
import json

from . import view_model


def write(path, out, **build_kwargs):
	"""Build the view of the run at `path`, write the HTML to `out`, and
	return the view. `build_kwargs` go to view_model.build (attributes,
	all_attributes, max_instances, resolution, name)."""
	view = view_model.build(path, **build_kwargs)
	text = render(view)
	with open(out, "w", encoding="utf-8") as f:
		f.write(text)
	return view


def render(view):
	"""The HTML text of a view (validated again first)."""
	view_model.validate(view)
	csp = ("default-src 'none'; "
	       f"script-src '{_hash(_SCRIPT)}'; style-src '{_hash(_STYLE)}'; "
	       "img-src 'none'; connect-src 'none'; font-src 'none'; object-src 'none'; "
	       "base-uri 'none'; form-action 'none'")
	return (_PAGE.replace("@CSP@", csp).replace("@STYLE@", _STYLE)
	        .replace("@SCRIPT@", _SCRIPT).replace("@DATA@", _data(view)))


def _hash(text):
	return "sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()


def _data(view):
	"""JSON that cannot end its <script> block or open markup."""
	text = json.dumps(view, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
	for char, escaped in (("&", "\\u0026"), ("<", "\\u003c"), (">", "\\u003e"),
	                      (" ", "\\u2028"), (" ", "\\u2029")):
		text = text.replace(char, escaped)
	return text


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="@CSP@">
<meta name="referrer" content="no-referrer">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Energy profile</title>
<style>@STYLE@</style>
</head>
<body>
<noscript>This report needs JavaScript to display. Its data is the JSON block named aep-data in this file.</noscript>
<div id="app"></div>
<script type="application/json" id="aep-data">@DATA@</script>
<script>@SCRIPT@</script>
</body>
</html>
"""

_STYLE = """
:root{--bg:#ffffff;--fg:#1d2329;--muted:#5d6874;--line:#e1e5e9;--panel:#f6f8fa;
--accent:#2a62c9;--measured:#1f7a4d;--estimated:#9a5b00;--missing:#6b7480;--invalid:#b3261e;
--shared:#6d4bb3;--bar-time:#b4c3d3;--bar-energy:#2a62c9;--track:#eef1f4;--chip:#eef1f4;--guide:#d3d9df}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#15191d;--fg:#e3e7eb;
--muted:#9aa5b1;--line:#2a3138;--panel:#1b2025;--accent:#79a6f2;--measured:#5cc58f;
--estimated:#e3a64a;--missing:#8b96a2;--invalid:#f2867d;--shared:#b39af0;--bar-time:#55687c;
--bar-energy:#79a6f2;--track:#232930;--chip:#242a31;--guide:#36404a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1200px;margin:0 auto;padding:24px 20px 48px}
.title{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 12px}
h1{font-size:20px;margin:0}
h2{font-size:16px;margin:24px 0 8px}
h3{font-size:12px;margin:20px 0 6px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.muted{color:var(--muted)}
.small{font-size:12px}
.chips{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin:10px 0 0}
.chip{background:var(--chip);border-radius:10px;padding:2px 9px;font-size:12px}
.chip-label{font-size:12px;color:var(--muted);margin-right:2px}
.caveat{font-size:12px;color:var(--muted);margin:10px 0 0}
.banner{border-left:3px solid var(--estimated);background:var(--panel);padding:8px 12px;margin:14px 0 0}
.basis{font-size:13px;margin:14px 0 0}
nav.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin-top:18px}
nav.tabs button{background:none;border:0;border-bottom:2px solid transparent;color:var(--muted);
padding:8px 12px;font:inherit;cursor:pointer}
nav.tabs button[aria-selected="true"]{color:var(--fg);border-bottom-color:var(--accent);font-weight:600}
.toolbar{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;margin:14px 0 8px}
.toolbar label{font-size:12px;color:var(--muted)}
.toolbar .hint{margin-left:auto;font-size:12px;color:var(--muted)}
select,input[type=search]{font:inherit;font-size:13px;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:4px;padding:3px 6px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
th{font-size:12px;color:var(--muted);font-weight:500;white-space:nowrap}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
tbody tr:hover td{background:var(--panel)}
tr.lvl0 td{font-weight:600}
tr.lvl0 td .note,tr.lvl0 td .sub{font-weight:400}
tr.inst td{font-size:12px;padding-top:4px;padding-bottom:4px;color:var(--muted)}
tr.sel td{background:var(--chip)}tr.sel td:first-child{box-shadow:inset 3px 0 0 var(--accent)}
td.name{white-space:nowrap;width:36%}
.guide{display:inline-block;width:1.25em;align-self:stretch;border-left:1px solid var(--guide);margin-left:.45em;height:1.5em;vertical-align:middle}
button.link{background:none;border:0;padding:0;color:var(--accent);font:inherit;cursor:pointer;text-align:left}
button.op{background:none;border:0;padding:0;color:var(--fg);font:inherit;cursor:pointer;text-align:left}
button.op:hover,button.op:focus-visible{color:var(--accent);text-decoration:underline}
button.tog{background:none;border:0;width:1.4em;padding:0;color:var(--muted);font:inherit;cursor:pointer}
.tog-ph{display:inline-block;width:1.4em}
.sub{font-size:11px;color:var(--muted);font-weight:400}
.k-measured{color:var(--measured)}.k-estimated{color:var(--estimated)}.k-missing{color:var(--missing)}
.k-invalid{color:var(--invalid)}.k-shared{color:var(--shared)}
.note{display:inline-block;background:var(--chip);border-radius:8px;padding:0 6px;margin:1px 2px 1px 0;font-size:11px;color:var(--muted)}
.meter{width:96px;height:4px;border-radius:2px;background:var(--track);margin:4px 0 0 auto}
.meter .fill{height:100%;border-radius:2px}
.fill.time{background:var(--bar-time)}.fill.energy{background:var(--bar-energy)}
aside#panel{position:fixed;top:0;right:0;width:min(460px,100%);height:100%;overflow:auto;
background:var(--bg);border-left:1px solid var(--line);box-shadow:-4px 0 16px rgba(0,0,0,.15);padding:20px 22px;display:none}
aside#panel.open{display:block}
aside#panel .close{float:right}
aside#panel h2{margin:0 0 2px;font-size:17px}
dl.kv{display:grid;grid-template-columns:max-content 1fr;gap:3px 14px;margin:6px 0}
dl.kv dt{color:var(--muted)}dl.kv dd{margin:0;word-break:break-word}
.ev{display:grid;grid-template-columns:110px 1fr;gap:2px 12px;padding:8px 0;border-bottom:1px solid var(--line)}
.ev-val{font-size:16px;font-weight:600;font-variant-numeric:tabular-nums;text-align:right}
.na{font-size:12px;margin:4px 0}
.crumb{font-size:12px;color:var(--muted);word-break:break-word}
.legend{font-size:12px;color:var(--muted);margin:10px 0 0}
.hidden{display:none}
@media (max-width:700px){main{padding:16px}th.hide-s,td.hide-s{display:none}.toolbar .hint{display:none}}
"""

_SCRIPT = r"""
(function () {
"use strict";
var V = JSON.parse(document.getElementById("aep-data").textContent);
var KIND = {
  measured: ["●", "measured", "k-measured"],
  estimated: ["~", "estimated", "k-estimated"],
  unavailable: ["—", "n/a", "k-missing"],
  invalid: ["✕", "invalid", "k-invalid"],
  not_defensible: ["⧉", "not separable", "k-shared"]
};
var DOMAIN = {gpu: "GPU", cpu_package: "CPU package", dram: "DRAM"};
var SCOPE = {
  inclusive: "inclusive: the span's own window, children included",
  exclusive: "exclusive: children removed (a parent's own share)",
  window: "estimate over the span's window (inclusive)",
  whole_run: "whole run"
};
var SCOPE_SHORT = {inclusive: "inclusive", exclusive: "exclusive", window: "window estimate",
                   whole_run: "whole run"};
var QUALIFIER = {
  below_resolution: "≈ below the counter's resolution: quantised, may not represent the span",
  resolution_unknown: "counter resolution unknown for this run",
  diagnostic_only: "cross-check only: a counter measurement exists for this span"
};
var state = {tab: "explorer", sort: "energy", groupBy: "path", filter: "",
             open: {}, inst: {}, more: {}, selected: null};

function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = String(text);
  return e;
}
function add(parent, child) { parent.appendChild(child); return child; }
// a value is there only if it is neither null nor undefined (0 is a value)
function has(v) { return v !== null && v !== undefined; }
function fmtJ(v) {
  return v === null || v === undefined ? "n/a" :
    Number(v).toLocaleString("en-US", {minimumFractionDigits: 3, maximumFractionDigits: 3});
}
function fmtS(v) {
  if (v === null || v === undefined) return "n/a";
  if (v < 1e-3) return Math.round(v * 1e6) + " µs";
  if (v < 1) return (v * 1000).toFixed(1) + " ms";
  return v.toFixed(2) + " s";
}
function fmtShare(s) {
  if (s === null || s === undefined) return "";
  if (s > 0 && s < 0.001) return "<0.1%";
  return (s * 100).toFixed(1) + "%";
}
function noteTag(note) { return String(note).split(" ")[0]; }
function reasonNode(code) {
  var text = (V.reasons || {})[code];
  var s = el("span", "muted", " (" + (text || code) + ")");
  if (text) s.title = code;
  return s;
}
function counts(obj) {
  return Object.keys(obj || {}).map(function (k) {
    return ((V.reasons || {})[k] ? V.reasons[k] + " [" + k + "]" : k) + ": " + obj[k];
  }).join("; ");
}
function basisLabel() {
  var b = V.basis;
  if (!b) return null;
  return (b.domain === "gpu" ? "GPU J" : "CPU pkg J") + (b.kind === "estimated" ? " est." : "");
}
function estimated() { return V.basis && V.basis.kind === "estimated"; }

function kindSpan(kind) {
  var k = KIND[kind] || KIND.unavailable;
  var s = el("span", k[2], k[0] + " " + k[1]);
  return s;
}
function cellNode(cell) {
  var wrap = el("span");
  if (!cell) { add(wrap, el("span", "k-missing", "not recorded")); return wrap; }
  add(wrap, kindSpan(cell.kind));
  if (has(cell.value_j) && (cell.kind === "measured" || cell.kind === "estimated")) {
    add(wrap, el("span", null, " " + (cell.kind === "estimated" ? "~" : "") + fmtJ(cell.value_j) + " J"));
  }
  if (cell.reason) add(wrap, reasonNode(cell.reason));
  (cell.qualifiers || []).forEach(function (q) {
    add(wrap, el("div", "small muted", QUALIFIER[q] || q));
  });
  // how a value was obtained; for a missing value it would only add noise
  if (cell.method && has(cell.value_j)) add(wrap, el("div", "small muted", "method: " + cell.method));
  return wrap;
}
function rowEnergyNode(r) {
  if (!V.basis) return el("span", "muted", "");
  if (!has(r.energy_j)) return el("span", "k-missing", "n/a");
  var s = el("span", estimated() ? "k-estimated" : "k-measured",
             (estimated() ? "~" : "") + fmtJ(r.energy_j));
  if (!r.ranked) {
    var w = el("span");
    add(w, s);
    add(w, el("div", "sub", "not ranked"));
    return w;
  }
  return s;
}
function notesNode(notes) {
  var w = el("span");
  (notes || []).forEach(function (n) {
    var c = el("span", "note", n);
    var legend = V.legend[noteTag(n)];
    if (legend) c.title = legend;
    add(w, c);
  });
  return w;
}
function legendFor(rows) {
  var used = {};
  rows.forEach(function (r) { (r.notes || []).forEach(function (n) { used[noteTag(n)] = 1; }); });
  var box = el("div", "legend");
  Object.keys(V.legend).forEach(function (tag) {
    if (used[tag]) add(box, el("div", null, V.legend[tag]));
  });
  return box;
}

// ---------------------------------------------------------------- header
function header(root) {
  var h = add(root, el("header"));
  var t = add(h, el("div", "title"));
  add(t, el("h1", null, "Energy profile"));
  add(t, el("span", "muted", V.run.name));
  document.title = "Energy profile · " + V.run.name;
  var notOk = 0;
  Object.keys(V.run.status || {}).forEach(function (k) { if (k !== "ok" && k !== "null") notOk += V.run.status[k]; });
  var chips = add(h, el("div", "chips"));
  add(chips, chip(fmtS(V.run.wall_s), "first span start to last span end"));
  add(chips, chip(V.run.n_spans + " spans"));
  add(chips, chip(V.run.n_operations + " operations · " + V.run.n_paths + " paths",
    "distinct labels · distinct places in the hierarchy"));
  if (notOk) add(chips, chip(notOk + " spans not ok", null, "k-invalid"));
  if (V.mode) add(chips, chip("mode " + V.mode.mode, "requested " + V.mode.requested_mode +
    (V.mode.mode_source ? " (" + V.mode.mode_source + ")" : "")));
  else add(chips, chip(V.run.schema === 1 ? "schema v1" : "mode not recorded"));
  var w = add(h, el("div", "chips"));
  add(w, el("span", "chip-label", "Whole run, device-level:"));
  V.whole_run.forEach(function (c) {
    var ch = add(w, chip(DOMAIN[c.domain] + " "));
    if (has(c.value_j) && c.kind === "measured") {
      add(ch, el("span", "k-measured", fmtJ(c.value_j) + " J"));
      ch.title = "measured: " + c.method;
    } else {
      add(ch, el("span", "k-missing", "n/a"));
      ch.title = (V.reasons || {})[c.reason] || c.reason || "";
    }
  });
  add(h, el("p", "caveat", "Whole-device energy, assigned to operations by time window: " +
    "where energy was used while an operation ran, not what it caused."));
  var tabs = add(h, el("nav", "tabs"));
  tabs.setAttribute("role", "tablist");
  [["explorer", "Workflow explorer"], ["operations", "Operation breakdown"],
   ["provenance", "Provenance (" + V.warnings.length + " warnings)"]].forEach(function (t) {
    var b = add(tabs, el("button", null, t[1]));
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", state.tab === t[0] ? "true" : "false");
    b.addEventListener("click", function () { state.tab = t[0]; render(); });
  });
}
function chip(text, title, cls) {
  var c = el("span", "chip" + (cls ? " " + cls : ""), text);
  if (title) c.title = title;
  return c;
}

function basisSummary() {
  var b = V.basis;
  if (b.scope === "window") return "estimated GPU energy (~) of leaf spans that nothing overlapped";
  if (b.scope === "exclusive") return "measured " + DOMAIN[b.domain] + " energy, exclusive of children";
  return b.text;
}
function basisLine(box) {
  if (V.fallback_reason) {
    var b = add(box, el("div", "banner"));
    add(b, el("strong", null, "Not ranked by energy. "));
    add(b, el("span", null, V.fallback_reason + "."));
  }
  if (V.basis) {
    var p = add(box, el("div", "basis"));
    p.title = V.basis.text;
    add(p, el("strong", null, "Energy: "));
    add(p, el("span", null, basisSummary() + ". Share is of the ranked rows. "));
    var link = add(p, el("button", "link", "How was this measured?"));
    link.addEventListener("click", function () { state.tab = "provenance"; render(); });
  }
}

// ---------------------------------------------------------------- explorer
function sorter(mode) {
  return function (a, b) {
    if (mode === "name") return String(a.name).localeCompare(String(b.name));
    if (mode === "energy") {
      if (a.ranked !== b.ranked) return a.ranked ? -1 : 1;
      if (a.ranked) return b.energy_j - a.energy_j;
    }
    return (b.duration_total_s || 0) - (a.duration_total_s || 0);
  };
}
function maxOf(rows, key, onlyRanked) {
  var m = 0;
  rows.forEach(function (r) { if ((!onlyRanked || r.ranked) && r[key] > m) m = r[key]; });
  return m;
}
function meter(frac, cls) {
  var m = el("div", "meter");
  var f = add(m, el("div", "fill " + cls));
  f.style.width = Math.max(1, Math.min(100, 100 * frac)) + "%";
  return m;
}
function timeCell(r, maxT) {
  var td = el("td", "num");
  add(td, el("div", null, fmtS(r.duration_total_s)));
  if (maxT > 0) add(td, meter((r.duration_total_s || 0) / maxT, "time"));
  return td;
}
function energyCell(r, maxE) {
  var td = el("td", "num");
  add(td, rowEnergyNode(r));
  if (r.ranked && maxE > 0) add(td, meter(r.energy_j / maxE, "energy"));
  return td;
}
function pctCell(r) {
  return el("td", "num hide-s muted small", fmtS(r.duration_p50_s) + " / " + fmtS(r.duration_p95_s));
}
function instancesByPath() {
  var by = {};
  V.instances.forEach(function (s) { (by[s.path] = by[s.path] || []).push(s); });
  return by;
}
function bestCell(s) {
  var cells = s.cells || [];
  function find(scope, domain) {
    for (var i = 0; i < cells.length; i++) if (cells[i].scope === scope && cells[i].domain === domain) return cells[i];
    return null;
  }
  var d = V.basis ? V.basis.domain : "gpu";
  if (V.basis && V.basis.kind === "estimated") return find("window", d);
  return find(V.run.schema === 1 ? "inclusive" : "exclusive", d) || find("inclusive", d);
}

function compactCell(c) {
  if (!c) return el("span", "k-missing", "—");
  if (has(c.value_j)) return el("span", KIND[c.kind][2], (c.kind === "estimated" ? "~" : "") + fmtJ(c.value_j));
  var k = KIND[c.kind] || KIND.unavailable;
  var n = el("span", k[2], k[0] + " " + k[1]);
  n.title = (V.reasons || {})[c.reason] || c.reason || "";
  return n;
}

function explorer(box) {
  basisLine(box);
  var tb = add(box, el("div", "toolbar"));
  var lab = add(tb, el("label", null, "Order "));
  var sel = add(lab, el("select"));
  [["energy", "ranked energy, then time"], ["time", "total time"], ["name", "name"]].forEach(function (o) {
    var op = add(sel, el("option", null, o[1])); op.value = o[0]; if (state.sort === o[0]) op.selected = true;
  });
  sel.addEventListener("change", function () { state.sort = sel.value; render(); });
  var ex = add(tb, el("button", "link small", "expand all"));
  ex.addEventListener("click", function () { V.paths.forEach(function (p) { state.open[p.id] = true; }); render(); });
  var co = add(tb, el("button", "link small", "collapse all"));
  co.addEventListener("click", function () { state.open = {}; render(); });
  add(tb, el("span", "hint", "Click an operation for its details, “spans” for its individual spans"));
  if (V.instances_info.shown < V.instances_info.total) {
    add(box, el("div", "small muted", "Individual spans: " + V.instances_info.shown + " of " +
      V.instances_info.total + " listed (" + V.instances_info.policy + ")."));
  }
  var wrap = add(box, el("div", "scroll"));
  var table = add(wrap, el("table"));
  var head = add(add(table, el("thead")), el("tr"));
  var cols = [["operation", ""], ["spans", "num"], ["time", "num"], ["p50 / p95", "num hide-s"]];
  if (V.basis) cols.push([basisLabel(), "num"], ["share", "num"]);
  cols.push(["notes", ""]);
  cols.forEach(function (c) { add(head, el("th", c[1], c[0])); });
  var body = add(table, el("tbody"));
  var children = {};
  V.paths.forEach(function (p) {
    var k = p.parent === null || p.parent === undefined ? "root" : p.parent;
    (children[k] = children[k] || []).push(p);
  });
  var many = V.paths.length > 60;
  var maxT = maxOf(V.paths, "duration_total_s", false);
  var maxE = maxOf(V.paths, "energy_j", true);
  var inst = instancesByPath();
  function nameCell(tr, depth) {
    var td = add(tr, el("td", "name"));
    for (var i = 0; i < depth; i++) add(td, el("span", "guide"));
    return td;
  }
  function walk(key) {
    (children[key] || []).slice().sort(sorter(state.sort)).forEach(function (p) {
      var isOpen = state.open[p.id] !== undefined ? state.open[p.id] : (!many || p.depth === 0);
      var tr = add(body, el("tr", (p.depth === 0 ? "lvl0" : "") + (state.selected === "p" + p.id ? " sel" : "")));
      var td = nameCell(tr, p.depth);
      if (children[p.id]) {
        var t = add(td, el("button", "tog", isOpen ? "▾" : "▸"));
        t.setAttribute("aria-expanded", isOpen ? "true" : "false");
        t.setAttribute("aria-label", (isOpen ? "collapse " : "expand ") + p.name);
        t.addEventListener("click", function () { state.open[p.id] = !isOpen; render(); });
      } else {
        add(td, el("span", "tog-ph"));
      }
      var nm = add(td, el("button", "op", p.name));
      nm.addEventListener("click", function () { state.selected = "p" + p.id; showPath(p); });
      var nTd = add(tr, el("td", "num"));
      add(nTd, el("span", null, "×" + p.n));
      if (inst[p.id]) {
        add(nTd, el("span", "sub", " · "));
        var sh = add(nTd, el("button", "link sub", state.inst[p.id] ? "hide spans" : "spans"));
        sh.addEventListener("click", function () { state.inst[p.id] = !state.inst[p.id]; render(); });
      }
      add(tr, timeCell(p, maxT));
      add(tr, pctCell(p));
      if (V.basis) {
        add(tr, energyCell(p, maxE));
        add(tr, el("td", "num", fmtShare(p.share)));
      }
      add(add(tr, el("td")), notesNode(p.notes));
      if (state.inst[p.id] && inst[p.id]) instanceRows(p, inst[p.id]);
      if (isOpen) walk(p.id);
    });
  }
  function instanceRows(p, list) {
    var limit = state.more[p.id] ? list.length : Math.min(list.length, 100);
    for (var i = 0; i < limit; i++) {
      (function (s) {
        var tr = add(body, el("tr", "inst" + (state.selected === "s" + s.n ? " sel" : "")));
        var td = nameCell(tr, p.depth + 1);
        add(td, el("span", "tog-ph"));
        var b = add(td, el("button", "link", "span #" + s.n));
        b.addEventListener("click", function () { state.selected = "s" + s.n; showSpan(s); });
        add(td, el("span", null, "  at +" + fmtS(s.start_s)));
        add(tr, el("td", "num", s.status === "ok" ? "" : s.status));
        add(tr, el("td", "num", fmtS(s.duration_s)));
        add(tr, el("td", "num hide-s", ""));
        var c = bestCell(s);
        if (V.basis) {
          var e = add(tr, el("td", "num"));
          add(e, compactCell(c));
          if (c) e.title = SCOPE[c.scope];
          add(tr, el("td", "num", ""));
        }
        add(tr, el("td", "small", s.attribution === "shared" ? "shared" : ""));
      })(list[i]);
    }
    if (list.length > limit) {
      var tr = add(body, el("tr", "inst"));
      var td = add(tr, el("td"));
      td.colSpan = cols.length;
      var more = add(td, el("button", "link", "show all " + list.length + " listed spans of this path"));
      more.addEventListener("click", function () { state.more[p.id] = true; render(); });
    }
  }
  walk("root");
  add(box, legendFor(V.paths));
}

// ---------------------------------------------------------------- side panel
function panel() {
  var p = document.getElementById("panel");
  while (p.firstChild) p.removeChild(p.firstChild);
  p.className = "open";
  var close = add(p, el("button", "link close", "close ✕"));
  close.addEventListener("click", function () { p.className = ""; state.selected = null; render(); });
  return p;
}
function kv(box, pairs) {
  var dl = add(box, el("dl", "kv"));
  pairs.forEach(function (pair) {
    if (pair[1] === undefined || pair[1] === null || pair[1] === "") return;
    add(dl, el("dt", null, pair[0]));
    var dd = add(dl, el("dd"));
    if (pair[1] instanceof Node) add(dd, pair[1]); else dd.textContent = String(pair[1]);
  });
  return dl;
}
function showPath(r) {
  render();
  var p = panel();
  add(p, el("h2", null, r.name));
  add(p, el("div", "crumb", r.path.join(" › ")));
  kv(p, [["spans", r.n], ["not ok", r.n_not_ok || ""], ["total time", fmtS(r.duration_total_s) +
    (r.depth === 0 ? "" : " (inclusive of its own children)")],
    ["p50 / p95", fmtS(r.duration_p50_s) + " / " + fmtS(r.duration_p95_s)],
    [basisLabel() || "energy", V.basis ? rowEnergyNode(r) : "not ranked by energy"],
    ["share of ranked", fmtShare(r.share)],
    ["CPU package (measured, exclusive)", r.cpu_package_j !== null && r.cpu_package_j !== undefined ? fmtJ(r.cpu_package_j) + " J (shown, not ranked)" : ""],
    ["spans listed here", r.n_instances_shown + " of " + r.n]]);
  if (V.basis) add(p, el("p", "small muted", V.basis.text + "."));
  if ((r.notes || []).length) {
    add(p, el("h3", null, "Notes"));
    r.notes.forEach(function (n) {
      var d = add(p, el("div", "small"));
      add(d, el("span", "note", n));
      add(d, el("span", "muted", " " + (V.legend[noteTag(n)] || "")));
    });
  }
}
function showSpan(s) {
  render();
  var p = panel();
  var path = V.paths.filter(function (x) { return x.id === s.path; })[0];
  add(p, el("h2", null, s.name));
  add(p, el("div", "crumb", (path ? path.path.join(" › ") + "  ·  " : "") + "span #" + s.n));
  kv(p, [["duration", fmtS(s.duration_s)], ["start", "+" + fmtS(s.start_s) + " after the first span"],
    ["status", s.status + (s.error_type ? " (" + s.error_type + ")" : "")],
    ["parent", s.parent !== undefined ? "span #" + s.parent : "none (top level)"],
    ["child spans", s.n_children],
    ["attribution", s.attribution === "shared" ? "shared: overlapped " + (s.concurrent_spans || 0) +
      " other spans; device energy cannot be split" : (s.attribution || "")],
    ["children overlap", s.children_overlap ? "yes: no exclusive value" : ""],
    ["clock", s.clock_anomaly ? "the wall clock stepped during this span" : ""],
    ["other sessions", s.other_sessions_active ? "another Session in this process was active" : ""],
    ["tokens in / out", (s.input_tokens !== undefined || s.output_tokens !== undefined) ?
      (s.input_tokens !== undefined ? s.input_tokens : "?") + " / " + (s.output_tokens !== undefined ? s.output_tokens : "?") : ""]]);

  add(p, el("h3", null, "Energy"));
  var cells = s.cells || [];
  var valued = cells.filter(function (c) { return has(c.value_j); });
  if (!valued.length) add(p, el("div", "muted small", "No energy value for this span."));
  valued.forEach(function (c) {
    var row = add(p, el("div", "ev"));
    add(row, el("div", "ev-val " + KIND[c.kind][2], (c.kind === "estimated" ? "~" : "") + fmtJ(c.value_j) + " J"));
    var d = add(row, el("div"));
    var line = add(d, el("div"));
    add(line, el("span", null, DOMAIN[c.domain] + " · " + SCOPE_SHORT[c.scope] + " · "));
    add(line, kindSpan(c.kind));
    if (c.method) add(d, el("div", "small muted", "method: " + c.method));
    (c.qualifiers || []).forEach(function (q) { add(d, el("div", "small muted", QUALIFIER[q] || q)); });
  });
  // values that are missing, grouped by kind and reason
  var groups = {}, order = [];
  cells.filter(function (c) { return !has(c.value_j); }).forEach(function (c) {
    var key = c.kind + "|" + (c.reason || "");
    if (!groups[key]) { groups[key] = {kind: c.kind, reason: c.reason, where: []}; order.push(key); }
    groups[key].where.push(DOMAIN[c.domain] + " " + SCOPE_SHORT[c.scope]);
  });
  var absent = ["gpu", "cpu_package", "dram"].filter(function (d) {
    return !cells.some(function (c) { return c.domain === d; });
  });
  if (order.length || absent.length) {
    var na = add(p, el("div"));
    na.style.marginTop = "10px";
    order.forEach(function (key) {
      var g = groups[key];
      var line = add(na, el("div", "na"));
      add(line, kindSpan(g.kind));
      add(line, el("span", null, "  " + g.where.join(", ")));
      if (g.reason) add(line, reasonNode(g.reason));
    });
    if (absent.length) add(na, el("div", "na muted", "not recorded: " +
      absent.map(function (d) { return DOMAIN[d]; }).join(", ")));
  }
  add(p, el("p", "small muted", "Inclusive includes the span's children; exclusive is what its " +
    "children do not cover; a window estimate is sampled power over the span, never added to " +
    "measured values."));

  var attrs = s.attributes || {};
  var keys = Object.keys(attrs);
  if (keys.length) {
    add(p, el("h3", null, "Attributes"));
    kv(p, keys.map(function (k) {
      var v = attrs[k];
      return [k, typeof v === "string" ? v : JSON.stringify(v)];
    }));
  }
  add(p, el("div", "small muted", s.n_attributes_omitted ? s.n_attributes_omitted +
    " attribute(s) left out of this file (privacy; see Provenance)" : (keys.length ? "" : "No attributes in this file.")));
}

// ---------------------------------------------------------------- operations
function operations(box) {
  basisLine(box);
  var tb = add(box, el("div", "toolbar"));
  var lab = add(tb, el("label", null, "Group by "));
  var sel = add(lab, el("select"));
  [["path", "hierarchy path"], ["label", "operation label"]].forEach(function (o) {
    var op = add(sel, el("option", null, o[1])); op.value = o[0]; if (state.groupBy === o[0]) op.selected = true;
  });
  sel.addEventListener("change", function () { state.groupBy = sel.value; render(); });
  var flab = add(tb, el("label", null, "Filter "));
  var f = add(flab, el("input"));
  f.type = "search"; f.value = state.filter; f.placeholder = "name contains";
  f.addEventListener("change", function () { state.filter = f.value; render(); });
  var rows = (state.groupBy === "path" ? V.paths : V.operations).filter(function (r) {
    var name = state.groupBy === "path" ? r.path.join(" › ") : String(r.name);
    return !state.filter || name.toLowerCase().indexOf(state.filter.toLowerCase()) >= 0;
  });
  rows = rows.slice().sort(sorter("energy"));
  var maxT = maxOf(rows, "duration_total_s", false);
  var maxE = maxOf(rows, "energy_j", true);
  var wrap = add(box, el("div", "scroll"));
  var table = add(wrap, el("table"));
  var head = add(add(table, el("thead")), el("tr"));
  var hasCpu = rows.some(function (r) { return r.cpu_package_j !== null && r.cpu_package_j !== undefined; });
  var cols = [["#", "num"], [state.groupBy === "path" ? "hierarchy path" : "operation", ""], ["spans", "num"],
    ["not ok", "num hide-s"], ["time", "num"], ["p50 / p95", "num hide-s"]];
  if (V.basis) cols.push([basisLabel(), "num"], ["share", "num"]);
  if (hasCpu) cols.push(["CPU pkg J (shown, not ranked)", "num hide-s"]);
  cols.push(["notes", ""]);
  cols.forEach(function (c) { add(head, el("th", c[1], c[0])); });
  var body = add(table, el("tbody"));
  var rank = 0;
  rows.forEach(function (r) {
    var tr = add(body, el("tr"));
    add(tr, el("td", "num", r.ranked ? String(++rank) : "–"));
    add(tr, el("td", null, state.groupBy === "path" ? r.path.join(" › ") : r.name));
    tr.className = r.ranked ? "" : "muted";
    add(tr, el("td", "num", r.n));
    add(tr, el("td", "num hide-s", r.n_not_ok || ""));
    add(tr, timeCell(r, maxT));
    add(tr, pctCell(r));
    if (V.basis) {
      add(tr, energyCell(r, maxE));
      add(tr, el("td", "num", fmtShare(r.share)));
    }
    if (hasCpu) add(tr, el("td", "num hide-s", r.cpu_package_j !== null && r.cpu_package_j !== undefined ? fmtJ(r.cpu_package_j) : "n/a"));
    add(add(tr, el("td")), notesNode(r.notes));
  });
  add(box, el("p", "small muted", state.groupBy === "path" ?
    "Each span belongs to exactly one hierarchy path, so ranked path values never count a span twice." :
    "Grouping by label puts every span with the same name together, wherever it ran in the hierarchy."));
  add(box, legendFor(rows));
}

// ---------------------------------------------------------------- provenance
function provenance(box) {
  var P = V.provenance;
  add(box, el("h2", null, "What each kind of number is"));
  var l = add(box, el("div"));
  [["measured", "a hardware energy counter's difference between two readings"],
   ["estimated", "sampled average power integrated over a time window; never added to measured values"],
   ["unavailable", "no defensible value; the reason says why (never shown as 0)"],
   ["invalid", "the counter readings could not be used (for example the counter went backwards)"],
   ["not_defensible", "the span overlapped other recorded work, so the device's energy cannot be split"]
  ].forEach(function (k) {
    var d = add(l, el("div", "small"));
    add(d, kindSpan(k[0]));
    add(d, el("span", "muted", ": " + k[1]));
  });
  Object.keys(QUALIFIER).forEach(function (q) { add(l, el("div", "small muted", QUALIFIER[q])); });
  if (V.basis) add(box, el("p", "small", "Ranking basis: " + V.basis.text + "."));

  add(box, el("h2", null, "Measurement"));
  kv(box, [["schema", "v" + P.schema + " (" + P.kind + (P.attributed_on_the_fly ? ", attributed for this report without a timeline" : "") + ")"],
    ["recorded by", P.profiler_version_recorded], ["report by", P.generated_by],
    ["mode", P.measurement_recorded ? P.mode + " (requested " + P.requested_mode + ", " + P.mode_source + ")" : "not recorded: " + P.measurement_note],
    ["why this mode", P.mode_reason],
    ["per-span GPU counter reads", P.span_counter_reads === undefined || P.span_counter_reads === null ? "" : (P.span_counter_reads ? "on" : "off")],
    ["per-span GPU energy", (P.semantics || {}).gpu_span_energy],
    ["whole-run GPU energy", (P.semantics || {}).gpu_run_energy],
    ["sampler", P.sampler && P.sampler.power_hz ? "power at " + P.sampler.power_hz + " Hz (" + P.sampler.power_api + "); GPU counter " + P.sampler.counter_policy + " (~" + P.sampler.counter_hz_effective + " Hz)" : ""],
    ["counter disturbance", P.counter_disturbance ? P.counter_disturbance.verdict + (P.counter_disturbance.source ? " (from " + P.counter_disturbance.source + ")" : " (no test result)") : ""],
    ["GPU counter resolution", P.resolution && P.resolution.gpu_update_interval_s ? "~" + (P.resolution.gpu_update_interval_s * 1000).toFixed(1) + " ms (" + P.resolution.basis + (P.resolution.source ? ", " + P.resolution.source : "") + ")" : "unknown (not characterised)"],
    ["GPU", P.gpu ? ((P.gpu.devices || []).map(function (d) { return d.name; }).join(", ") || ("not read: " + (P.gpu.unavailable_reason || P.gpu.reader))) + (P.gpu.driver_version ? ", driver " + P.gpu.driver_version : "") : ""],
    ["energy scope", P.energy_scope]]);

  add(box, el("h2", null, "Whole-run measurements"));
  var wr = add(box, el("div"));
  V.whole_run.forEach(function (c) {
    var d = add(wr, el("div", "small"));
    add(d, el("strong", null, DOMAIN[c.domain] + ": "));
    add(d, cellNode(c));
  });
  if (P.run_counter) kv(box, [["GPU run counter", P.run_counter.status === "measured" ?
    fmtJ(P.run_counter.energy_j) + " J over " + fmtS(P.run_counter.duration_s) + ", " + P.run_counter.n_readings +
    " readings; brackets all spans: " + P.run_counter.brackets_all_spans : "n/a (" + P.run_counter.status + ": " + P.run_counter.reason + ")"]]);
  if (P.counter_intervals && P.counter_intervals.counter_over_estimate_median !== null && P.counter_intervals.counter_over_estimate_median !== undefined) {
    kv(box, [["counter / integrated power", "median " + P.counter_intervals.counter_over_estimate_median.toFixed(3) +
      " over " + P.counter_intervals.n_intervals + " intervals (diagnostic)"]]);
  }
  if (P.window) {
    if (P.window.available) {
      var parts = Object.keys(P.window.domains || {}).map(function (d) {
        var v = P.window.domains[d];
        return d + " " + (v.energy_j === null || v.energy_j === undefined ? "n/a (" + (v.reason || v.status) + ")" : fmtJ(v.energy_j) + " J");
      });
      kv(box, [["independent window", P.window.basis + ": " + parts.join(", ")]]);
    } else {
      kv(box, [["independent window", "n/a (" + P.window.reason + ")"]]);
    }
  }
  if (P.totals) {
    add(box, el("h3", null, "Sums over top-level spans (" + P.totals.basis + ")"));
    var rows = [];
    ["gpu", "cpu_package", "dram", "total"].forEach(function (d) {
      var t = P.totals.domains[d];
      if (!t) return;
      rows.push([d === "total" ? "gpu+cpu_package+dram (measured)" : (DOMAIN[d] + " (measured)"),
        t.total_j !== null && t.total_j !== undefined ? fmtJ(t.total_j) + " J" : "n/a" + (t.reason ? " (" + t.reason + ")" : "")]);
    });
    var e = P.totals.estimated_gpu;
    if (e) rows.push(["GPU estimate (separate)", e.sum_j !== null && e.sum_j !== undefined ? "~" + fmtJ(e.sum_j) + " J over " + e.n_spans + " of " + e.n_top_level + " top-level spans, never added to the above" : (e.n_spans ? "no sum (" + e.reason + ")" : "none")]);
    kv(box, rows);
  }

  add(box, el("h2", null, "Coverage per span"));
  V.coverage.forEach(function (c) { add(box, el("div", "small", DOMAIN[c.domain] + ": " + c.text)); });
  if (P.coverage) {
    var t2 = add(add(box, el("div", "scroll")), el("table"));
    var hr = add(add(t2, el("thead")), el("tr"));
    ["domain", "states", "reasons"].forEach(function (h) { add(hr, el("th", null, h)); });
    var tb = add(t2, el("tbody"));
    Object.keys(P.coverage).forEach(function (d) {
      var tr = add(tb, el("tr"));
      add(tr, el("td", null, d));
      add(tr, el("td", "small", counts(P.coverage[d].states)));
      add(tr, el("td", "small", counts(P.coverage[d].reasons)));
    });
  }

  add(box, el("h2", null, "Warnings"));
  if (!V.warnings.length) add(box, el("div", "muted", "none"));
  V.warnings.forEach(function (w) {
    var d = add(box, el("div", "small"));
    add(d, el("strong", null, "[" + w.code + "] "));
    add(d, el("span", null, w.message));
  });

  add(box, el("h2", null, "Limitations"));
  ["Device-level: NVML and RAPL report whole devices, including idle draw and other processes.",
   "Time-associated energy is not causal: a span's value is what the device used while it was open.",
   "Spans that overlap other recorded work are shared: their energy is not split between them.",
   "Accuracy has not been established against an external power meter."].forEach(function (t) {
    add(box, el("div", "small", "• " + t));
  });

  add(box, el("h2", null, "Session attributes"));
  var sa = V.run.attributes || {};
  if (Object.keys(sa).length) kv(box, Object.keys(sa).map(function (k) {
    return [k, typeof sa[k] === "string" ? sa[k] : JSON.stringify(sa[k])];
  }));
  add(box, el("div", "small muted", V.run.n_attributes_omitted ? V.run.n_attributes_omitted +
    " omitted from this file" : (Object.keys(sa).length ? "" : "none")));

  add(box, el("h2", null, "This file"));
  var pr = V.privacy;
  kv(box, [["attributes", pr.attributes === "all" ? "all span and session attributes are included" :
      pr.attributes === "allowlist" ? "only these, by name: " + pr.allowlist.join(", ") : "none included"],
    ["operation labels", pr.labels === "redacted" ? "replaced by \u201coperation N\u201d" :
      "included as written" + (pr.n_free_text_labels ? " (" + pr.n_free_text_labels +
      " look like free text)" : "")],
    ["left out", pr.omitted.join("; ")],
    ["individual spans", V.instances_info.shown + " of " + V.instances_info.total + " listed (" + V.instances_info.policy + ")"],
    ["view model", "version " + V.view_version + ", " + V.generated_by]]);
}

// ---------------------------------------------------------------- page
function render() {
  var app = document.getElementById("app");
  while (app.firstChild) app.removeChild(app.firstChild);
  var main = add(app, el("main"));
  header(main);
  var box = add(main, el("section"));
  if (state.tab === "explorer") explorer(box);
  else if (state.tab === "operations") operations(box);
  else provenance(box);
  if (!document.getElementById("panel")) {
    var aside = el("aside");
    aside.id = "panel";
    document.body.appendChild(aside);
  }
}
document.addEventListener("keydown", function (ev) {
  if (ev.key === "Escape") { var p = document.getElementById("panel"); if (p) p.className = ""; }
});
// #tab=operations, #path=3 or #span=12 open that view (read once, on load)
var hash = {};
String(location.hash || "").replace(/^#/, "").split("&").forEach(function (part) {
  var kv2 = part.split("=");
  if (kv2.length === 2) hash[kv2[0]] = decodeURIComponent(kv2[1]);
});
if (["explorer", "operations", "provenance"].indexOf(hash.tab) >= 0) state.tab = hash.tab;
render();
if (hash.path !== undefined) {
  var pth = V.paths.filter(function (x) { return String(x.id) === hash.path; })[0];
  if (pth) { state.selected = "p" + pth.id; showPath(pth); }
}
if (hash.span !== undefined) {
  var spn = V.instances.filter(function (x) { return String(x.n) === hash.span; })[0];
  if (spn) { state.selected = "s" + spn.n; state.inst[spn.path] = true; showSpan(spn); }
}
})();
"""
