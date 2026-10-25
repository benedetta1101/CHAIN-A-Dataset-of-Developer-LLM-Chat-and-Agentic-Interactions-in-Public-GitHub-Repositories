#!/usr/bin/env python3
"""
Standalone HTML browser (one per language) to explore
out_vibe_coding/agentic_annotated_dataset.csv: the ANNOTATED agentic dataset (code candidates + intent + session adoption + harness) built
directly on the code candidates (diff / Write / prose), with the intent of the
prompt. Complementary to generate_html_browser_dataset.py (which shows snippet.csv)
- it does not modify it and does not touch its HTML files.

Navigation: sessions -> prompts (with intent) -> code candidates.
Filters: group (IDE/CLI), adoption (session), year, model, candidate
type, prompt language, non-trivial lines (>=5), intent (main and
sub-category), presence in snippet.csv, text search.

Writes new files in browser_dataset/ and SKIPS the ones that already exist (never
overwrites).

Usage:
    python pipeline/6_02_agentic_html_annotated.py
    python pipeline/6_02_agentic_html_annotated.py --languages java python
"""
import argparse
import copy
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_html_viewer_common as hv  # noqa: E402

csv.field_size_limit(50_000_000)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AGENTIC_DIR = os.path.join(SCRIPT_DIR, "out_vibe_coding")
IN_PATH = os.path.join(AGENTIC_DIR, "agentic_annotated_dataset.csv")
OUT_DIR = os.path.join(SCRIPT_DIR, "browser_dataset_en")
LANGUAGES_DEFAULT = ["python", "javascript", "typescript", "java"]
MAX_PROMPT = 2000
MAX_CODE = 3000


HTML_TMPL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Agentic annotated browser - __LING__</title>
<style>
  :root{ --bg:#f5f6f8; --panel:#fff; --border:#dfe3e8; --text:#1c2128; --muted:#6b7280; --accent:#2563eb; }
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;background:var(--bg);color:var(--text);font-size:14px}
  #app{display:grid;grid-template-columns:380px minmax(0,1fr);grid-template-rows:minmax(0,1fr);height:100vh}
  #sidebar{min-height:0;border-right:1px solid var(--border);display:flex;flex-direction:column;background:var(--panel)}
  #topbar{padding:12px;border-bottom:1px solid var(--border);overflow-y:auto;max-height:62vh}
  #topbar h2{margin:0 0 4px;font-size:15px}
  .note{font-size:10.5px;color:#b45309;background:#fef3c7;padding:4px 6px;border-radius:5px;margin-bottom:8px}
  #count{font-size:12.5px;color:var(--muted);margin-bottom:8px}
  .filter-label{font-size:11px;color:var(--muted);margin:8px 0 3px;text-transform:uppercase}
  input[type=text], select{width:100%;padding:6px 8px;border:1px solid var(--border);border-radius:6px;font-size:12.5px}
  #list{overflow-y:auto;flex:1}
  .item{padding:9px 12px;border-bottom:1px solid var(--border);cursor:pointer;font-size:12px}
  .item:hover{background:#eaf1fe}
  .item.active{background:#eaf1fe;border-left:3px solid var(--accent)}
  .item .repo{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11.5px}
  .item .summary{color:var(--muted);margin-top:2px}
  .badge{display:inline-block;padding:1px 6px;border-radius:10px;font-size:10px;background:#eee;margin-top:4px;margin-right:4px}
  .badge.ide{background:#e0e7ff;color:#3730a3}
  .badge.cli_agent{background:#ede9fe;color:#5b21b6}
  .badge.yes{background:#dcfce7;color:#166534}
  .badge.no{background:#fee2e2;color:#991b1b}
  .badge.unknown{background:#f3f4f6;color:#6b7280}
  .badge.diff{background:#fef9c3;color:#854d0e}
  .badge.prose{background:#e0f2fe;color:#075985}
  #main{min-height:0;overflow-y:auto;padding:24px}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px 20px;margin-bottom:16px}
  h3{margin:0 0 8px;font-size:14.5px}
  pre{background:#f6f8fa;border:1px solid var(--border);color:#1c2128;padding:10px;border-radius:6px;overflow-x:auto;font-size:12px;white-space:pre-wrap;word-break:break-word;max-height:260px;overflow-y:auto;margin:6px 0 0}
  .kv{display:flex;flex-wrap:wrap;gap:8px 20px;font-size:12.5px}
  .kv div{display:flex;gap:6px}
  .kv b{color:var(--muted);font-weight:600}
  a{color:var(--accent)}
  .prompt{border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:12px}
  .label{color:var(--muted);font-size:10.5px;text-transform:uppercase;margin:6px 0 2px}
  .ptext{white-space:pre-wrap;word-break:break-word;max-height:170px;overflow-y:auto;background:#f6f8fa;border:1px solid var(--border);border-radius:6px;padding:8px;font-size:12px}
  details{margin-top:6px}
  summary{cursor:pointer;font-size:12px}
__VIEWER_CSS__
</style>
</head>
<body>
<div id="app">
  <div id="sidebar">
    <div id="topbar">
      <h2>Agentic annotated - __LING__ (__N__ candidates)</h2>
      <div class="note">Built directly on code candidates (diff added lines, Write content, code blocks in prose) - no snippet.csv quality filters. Use the filters to narrow down (e.g. English prompt + &ge;5 lines reproduces the snippet.csv criteria). Adoption is session-level (not per pair). Harness = AI-coding configuration present in the repo at the time of the session. Intent is the label of the PROMPT. Each session is shown in full (all turns, with and without code) with message and token counts; only sessions whose user messages are English are included.</div>
      <div id="count"></div>
      <input id="filterBox" type="text" placeholder="Filter by repo/prompt...">
      <div class="filter-label">Source</div>
      <select id="fGroup"><option value="">All</option><option value="ide">IDE</option><option value="cli_agent">CLI agent</option></select>
      <div class="filter-label">Adoption (session)</div>
      <select id="fAd"><option value="">All</option><option value="yes">Adopted</option><option value="no">Not adopted</option><option value="unknown">Unknown</option></select>
      <div class="filter-label">Harness present</div>
      <select id="fHp"><option value="">All</option><option value="yes">Yes</option><option value="no">No</option></select>
      <div class="filter-label">Harness tool</div><select id="fHt"><option value="">All</option></select>
      <div class="filter-label">Harness mechanism</div><select id="fHm"><option value="">All</option></select>
      <div class="filter-label">Year</div><select id="fYear"><option value="">All</option></select>
      <div class="filter-label">Model</div><select id="fModel"><option value="">All</option></select>
      <div class="filter-label">Candidate type</div>
      <select id="fType"><option value="">All</option><option value="diff">diff (added lines)</option><option value="prose">code in prose</option><option value="cli_agent">CLI agent</option></select>
      <div class="filter-label">Prompt language</div>
      <select id="fEn"><option value="">All</option><option value="yes">English</option><option value="no">Not English</option></select>
      <div class="filter-label">Non-trivial lines</div>
      <select id="fRows"><option value="">All</option><option value="ge5">&ge; 5</option><option value="lt5">&lt; 5</option></select>
      <div class="filter-label">Prompt also in snippet.csv</div>
      <select id="fSnip"><option value="">All</option><option value="yes">Yes</option><option value="no">No</option></select>
      <div class="filter-label">Intent (main category)</div><select id="fMain"><option value="">All</option></select>
      <div class="filter-label">Intent (sub-category)</div><select id="fSub"><option value="">All</option></select>
    </div>
    <div id="list"></div>
  </div>
  <div id="main">
    <div id="placeholder" style="color:var(--muted);text-align:center;margin-top:80px">Select a session from the list on the left.</div>
    <div id="detail" style="display:none"></div>
  </div>
</div>
<script>
__VIEWER_JS__
const esc = s => (s==null?"":String(s)).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
loadData("__DATA_B64__").then(DATA => {
const TOKEN_NOTE = "__TOKEN_NOTE__";
const F = {txt:"",hp:"",ht:"",hm:"",source_group:"",ad:"",year:"",model:"",type:"",en:"",rows:"",snip:"",main:"",sub:""};
let current = null;
const adLabel = k => k==="yes" ? "adopted" : (k==="no" ? "not adopted" : "unknown");
const nCand = s => s.prompts.reduce((a,p)=>a+p.cands.length,0);

function candOk(c){
  if (F.type && c.t !== F.type) return false;
  if (F.rows==="ge5" && c.n < 5) return false;
  if (F.rows==="lt5" && c.n >= 5) return false;
  return true;
}
function promptOk(p){
  if (F.en && p.en !== F.en) return false;
  if (F.snip && p.sn !== F.snip) return false;
  if (F.main && p.main !== F.main) return false;
  if (F.sub && p.sub !== F.sub) return false;
  return true;
}
function view(s){
  const out = [];
  for (const p of s.prompts){
    if (!promptOk(p)) continue;
    const cs = p.cands.filter(candOk);
    if (cs.length) out.push({p, cs});
  }
  return out;
}
function sessionOk(s){
  if (F.source_group && s.g !== F.source_group) return false;
  if (F.ad && s.ad !== F.ad) return false;
  if (F.hp && s.hp !== F.hp) return false;
  if (F.ht && !s.ht.includes(F.ht)) return false;
  if (F.hm && !s.hm.includes(F.hm)) return false;
  if (F.year && String(s.y) !== F.year) return false;
  if (F.model && s.m !== F.model) return false;
  if (F.txt){
    const f = F.txt.toLowerCase();
    if (!(s.r||"").toLowerCase().includes(f) && !s.prompts.some(p => (p.p||"").toLowerCase().includes(f))) return false;
  }
  return true;
}
function fill(id, vals){
  const sel = document.getElementById(id);
  [...new Set(vals)].filter(v=>v!=null&&v!=="").sort().forEach(v=>{const o=document.createElement("option");o.value=v;o.textContent=v;sel.appendChild(o);});
}
fill("fHt", DATA.flatMap(s=>s.ht)); fill("fHm", DATA.flatMap(s=>s.hm)); fill("fYear", DATA.map(s=>s.y)); fill("fModel", DATA.map(s=>s.m));
fill("fMain", DATA.flatMap(s=>s.prompts.map(p=>p.main))); fill("fSub", DATA.flatMap(s=>s.prompts.map(p=>p.sub)));

function renderList(){
  const list = document.getElementById("list"); list.innerHTML = "";
  let ns = 0, nc = 0;
  for (const s of DATA){
    if (!sessionOk(s)) continue;
    const v = view(s); if (!v.length) continue;
    ns++; const k = v.reduce((a,x)=>a+x.cs.length,0); nc += k;
    const d = document.createElement("div");
    d.className = "item" + (current === s.sha ? " active" : "");
    d.innerHTML = `<div class="repo">${esc(s.r||"(unknown repo)")}</div>
      <div class="summary">${s.nu} user &middot; ${s.nm} model message${s.nm===1?"":"s"} &middot; ${fmtN(s.tu+s.tm)} tokens &middot; ${k} candidate${k===1?"":"s"}</div>
      <span class="badge ${s.g}">${s.g==="ide"?"IDE":"CLI agent"}</span>
      <span class="badge ${s.ad}">${adLabel(s.ad)}</span>${s.ht.map(t=>`<span class="badge diff">${esc(t)}</span>`).join("")}${s.y?`<span class="badge unknown">${s.y}</span>`:""}`;
    d.onclick = () => select(s.sha); list.appendChild(d);
  }
  document.getElementById("count").innerHTML = `<b>${ns}</b> / ${DATA.length} sessions &middot; <b>${nc}</b> candidates shown`;
}
function select(sha){ current = sha; renderList(); showDetail(); }
function candHtml(c){
  return `<details><summary><span class="badge ${c.t}">${c.t==="diff"?"diff":(c.t==="prose"?"code in prose":"CLI agent")}</span><span class="badge unknown">${esc(c.l)}</span><span class="badge unknown">${c.n} non-trivial lines</span>${c.trunc?'<span class="badge unknown">truncated</span>':""} candidate code</summary><pre>${esc(c.c)}</pre></details>`;
}
function promptBadges(p){
  return `${p.main?`<span class="badge unknown">${esc(p.main)}</span>`:""}${p.sub?`<span class="badge unknown">${esc(p.sub)}</span>`:`<span class="badge unknown">no intent label (${esc(p.st)})</span>`}<span class="badge ${p.en==="yes"?"yes":"no"}">${p.en==="yes"?"English":"not English"}</span>${p.sn==="yes"?`<span class="badge yes">in snippet.csv</span>`:""}`;
}
function showDetail(){
  const s = DATA.find(x=>x.sha===current), v = view(s);
  document.getElementById("placeholder").style.display = "none";
  const det = document.getElementById("detail"); det.style.display = "block";
  det.innerHTML = `<div class="card"><h3>${esc(s.r||"(unknown repo)")}</h3>
    <div class="kv"><div><b>source</b> ${s.g==="ide"?"IDE":"CLI agent"}</div><div><b>adoption (session)</b> ${adLabel(s.ad)}</div>
    <div><b>harness</b> ${s.hp==="yes"?esc(s.ht.join(", ")||"yes"):"none"}</div><div><b>harness mechanisms</b> ${esc(s.hm.join(", "))||"N/A"}</div><div><b>year</b> ${s.y||"N/A"}</div><div><b>model</b> ${esc(s.m)||"N/A"}</div></div>
    ${s.l?`<div style="margin-top:8px"><a href="${esc(s.l)}" target="_blank" rel="noopener">${esc(s.l)}</a></div>`:""}
    ${transcriptSummary(s, TOKEN_NOTE)}</div>
    <div class="card"><h3>Session transcript</h3>${renderTranscript(s, v, candHtml, promptBadges)}</div>`;
}
const bind = (id,k) => document.getElementById(id).addEventListener(id==="filterBox"?"input":"change", e => {F[k]=e.target.value.trim(); renderList(); if(current) showDetail();});
bind("filterBox","txt");bind("fHp","hp");bind("fHt","ht");bind("fHm","hm");bind("fGroup","source_group");bind("fAd","ad");bind("fYear","year");bind("fModel","model");bind("fType","type");
bind("fEn","en");bind("fRows","rows");bind("fSnip","snip");bind("fMain","main");bind("fSub","sub");
renderList();
});
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--languages", dest="languages", metavar="LANG", nargs="+", default=LANGUAGES_DEFAULT)
    ap.add_argument("--all-languages", action="store_true",
                    help="also include sessions whose user messages are not English (default: English only)")
    args = ap.parse_args()
    languages = set(args.languages)
    from_fare = [l for l in sorted(languages)
               if not os.path.exists(os.path.join(OUT_DIR, f"browser_agentic_annotated_{l}.html"))]
    for l in sorted(languages - set(from_fare)):
        print(f"{l}: browser_agentic_annotated_{l}.html already exists, not overwriting it.")
    if not from_fare:
        sys.exit(0)

    per_language = {l: {} for l in from_fare}
    with open(IN_PATH, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r["language"] not in per_language:
                continue
            sess = per_language[r["language"]].setdefault(r["sha"], {
                "sha": r["sha"], "l": r["conversation_url"],
                "r": (json.loads(r["repo"]) or [None])[0], "g": r["source_group"],
                "ad": r["session_adopted"], "hp": r["harness_present"],
                "ht": [t for t in r["harness_tools"].split(";") if t],
                "hm": [t for t in r["harness_mechanisms"].split(";") if t], "prompts": {}})
            p = sess["prompts"].setdefault(r["prompt"], {
                "p": r["prompt"][:MAX_PROMPT], "trunc": len(r["prompt"]) > MAX_PROMPT,
                "en": r["prompt_is_english"], "sn": r["prompt_in_snippet_csv"],
                "main": r["main_category"] or None, "sub": r["sub_category"] or None,
                "st": r["status"], "cands": []})
            p["cands"].append({"t": r["candidate_type"], "l": r["language"], "n": int(r["nontrivial_lines"]),
                               "c": r["code"][:MAX_CODE], "trunc": len(r["code"]) > MAX_CODE})
    everything = {sha for d in per_language.values() for sha in d}
    packed, meta, excluded = hv.load_agentic_conversations(os.path.join(AGENTIC_DIR, "conversations.jsonl"),
                                                           everything, args.all_languages)
    print(f"Sessions: {len(packed)} kept, {excluded} excluded (user messages not in English); tokens: {hv.TOKEN_METHOD}")
    os.makedirs(OUT_DIR, exist_ok=True)
    for l in from_fare:
        sessions, n_cand, located, total = [], 0, 0, 0
        for sha, s in per_language[l].items():
            if sha not in packed:
                continue
            conv = copy.deepcopy(packed[sha])
            located += hv.attach_prompts(conv, list(s["prompts"]))
            total += len(s["prompts"])
            s.update(conv)
            s["y"], s["m"] = meta.get(sha, (None, None))
            s["prompts"] = list(s["prompts"].values())
            n_cand += sum(len(p["cands"]) for p in s["prompts"])
            sessions.append(s)
        b64 = hv.pack_payload(sessions)
        html = (HTML_TMPL.replace("__VIEWER_CSS__", hv.VIEWER_CSS)
                .replace("__VIEWER_JS__", hv.VIEWER_JS.replace("__CAP_TURN__", str(hv.CAP_TURN)))
                .replace("__TOKEN_NOTE__", hv.TOKEN_METHOD)
                .replace("__DATA_B64__", b64).replace("__LING__", l).replace("__N__", str(n_cand)))
        out = os.path.join(OUT_DIR, f"browser_agentic_annotated_{l}.html")
        with open(out, "x", encoding="utf-8") as f:
            f.write(html)
        print(f"Written {out} ({len(sessions)} sessions, {n_cand} candidates, prompts located in transcript {located}/{total}, {os.path.getsize(out)//1_000_000} MB)")

if __name__ == "__main__":
    main()
