#!/usr/bin/env python3
"""
Standalone HTML browser (one per language) to explore
out_ago2026/chat_annotated_dataset.csv: chat prompt -> code pairs
(ChatGPT/Claude/Gemini), in the 4 target languages, with per-row adoption
("as is": exact copy in the repo) and the (macro/micro) intent of the prompt.

Navigation: conversations -> prompts (with intent) -> code pairs (with
adoption). Filters: adopted, method by which it was found, provider, year
(from the timestamp of the share URL, ~82% of ChatGPT conversations),
model, macro/micro intent, text search.

Writes new files in browser_dataset/ and SKIPS the ones that already exist (never
overwrites; opened in exclusive mode).

Usage:
    python pipeline/6_01_chat_html_annotated.py
    python pipeline/6_01_chat_html_annotated.py --languages java python
"""
import argparse
import copy
import csv
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_html_viewer_common as hv  # noqa: E402

csv.field_size_limit(50_000_000)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
IN_PATH = os.path.join(SCRIPT_DIR, "out_ago2026", "chat_annotated_dataset.csv")
CONV_PATH = os.path.join(SCRIPT_DIR, "out_ago2026", "conversations.jsonl")
OUT_DIR = os.path.join(SCRIPT_DIR, "browser_dataset_en")
LANGUAGES_DEFAULT = ["python", "javascript", "typescript", "java"]
MAX_PROMPT = 2000
MAX_CODE = 3000


def year_from_share(link):
    m = re.search(r"/share/([0-9a-f]{8})-[0-9a-f]{4}-8[0-9a-f]{3}", link or "")
    if not m:
        return None
    return datetime.datetime.fromtimestamp(int(m.group(1), 16), datetime.timezone.utc).year


HTML_TMPL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Chat annotated browser - __LING__</title>
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
  .badge.yes{background:#dcfce7;color:#166534}
  .badge.no{background:#fee2e2;color:#991b1b}
  .badge.not_verifiable{background:#f3f4f6;color:#6b7280}
  .badge.prov{background:#e0e7ff;color:#3730a3}
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
      <h2>Chat annotated - __LING__ (__N__ pairs)</h2>
      <div class="note">Chat prompt&rarr;code pairs with per-row adoption and the intent of the PROMPT. "Adopted" = the code was found as an EXACT copy in the repository (as is); modified/similar matches do not count. "Not verifiable" = the row cannot be traced to a repository (e.g. link found only on Hacker News). Each conversation is shown in full (all turns, with and without code), with the message counts and token counts; only conversations whose user messages are English are included.</div>
      <div id="count"></div>
      <input id="filterBox" type="text" placeholder="Filter by repo/prompt...">
      <div class="filter-label">Provider</div><select id="fProv"><option value="">All</option></select>
      <div class="filter-label">Adopted as is</div>
      <select id="fAd"><option value="">All</option><option value="yes">Yes</option><option value="no">No</option><option value="not_verifiable">Not verifiable</option></select>
      <div class="filter-label">Found via</div><select id="fDove"><option value="">All</option></select>
      <div class="filter-label">Year</div><select id="fYear"><option value="">All</option></select>
      <div class="filter-label">Model</div><select id="fModel"><option value="">All</option></select>
      <div class="filter-label">Intent (main category)</div><select id="fMain"><option value="">All</option></select>
      <div class="filter-label">Intent (sub-category)</div><select id="fSub"><option value="">All</option></select>
    </div>
    <div id="list"></div>
  </div>
  <div id="main">
    <div id="placeholder" style="color:var(--muted);text-align:center;margin-top:80px">Select a conversation from the list on the left.</div>
    <div id="detail" style="display:none"></div>
  </div>
</div>
<script>
__VIEWER_JS__
const esc = s => (s==null?"":String(s)).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
loadData("__DATA_B64__").then(DATA => {
const TOKEN_NOTE = "__TOKEN_NOTE__";
const F = {txt:"",prov:"",ad:"",dove:"",year:"",model:"",main:"",sub:""};
let current = null;
const adLabel = k => k==="yes" ? "adopted as is" : (k==="no" ? "not adopted" : "not verifiable");
function pairOk(c){ return (!F.ad || c.a===F.ad) && (!F.dove || c.d===F.dove); }
function promptOk(p){ return (!F.main || p.main===F.main) && (!F.sub || p.sub===F.sub); }
function view(s){
  const out = [];
  for (const p of s.prompts){
    if (!promptOk(p)) continue;
    const cs = p.cands.filter(pairOk);
    if (cs.length) out.push({p, cs});
  }
  return out;
}
function sessionOk(s){
  if (F.prov && s.v !== F.prov) return false;
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
fill("fProv", DATA.map(s=>s.v)); fill("fYear", DATA.map(s=>s.y)); fill("fModel", DATA.map(s=>s.m));
fill("fDove", DATA.flatMap(s=>s.prompts.flatMap(p=>p.cands.map(c=>c.d))));
fill("fMain", DATA.flatMap(s=>s.prompts.map(p=>p.main))); fill("fSub", DATA.flatMap(s=>s.prompts.map(p=>p.sub)));
function convAd(v){ const a=v.flatMap(x=>x.cs.map(c=>c.a)); return a.includes("yes")?"yes":(a.includes("no")?"no":"not_verifiable"); }
function renderList(){
  const list = document.getElementById("list"); list.innerHTML = "";
  let ns = 0, nc = 0;
  for (const s of DATA){
    if (!sessionOk(s)) continue;
    const v = view(s); if (!v.length) continue;
    const k = v.reduce((a,x)=>a+x.cs.length,0); ns++; nc += k;
    const d = document.createElement("div");
    d.className = "item" + (current === s.l ? " active" : "");
    d.innerHTML = `<div class="repo">${esc(s.r||"(unknown repo)")}</div>
      <div class="summary">${s.nu} user &middot; ${s.nm} model message${s.nm===1?"":"s"} &middot; ${fmtN(s.tu+s.tm)} tokens &middot; ${k} pair${k===1?"":"s"}</div>
      <span class="badge prov">${esc(s.v)}</span><span class="badge ${convAd(v)}">${adLabel(convAd(v))}</span>${s.y?`<span class="badge not_verifiable">${s.y}</span>`:""}`;
    d.onclick = () => select(s.l); list.appendChild(d);
  }
  document.getElementById("count").innerHTML = `<b>${ns}</b> / ${DATA.length} conversations &middot; <b>${nc}</b> pairs shown`;
}
function select(l){ current = l; renderList(); showDetail(); }
function candHtml(c){
  return `<details><summary><span class="badge ${c.a}">${adLabel(c.a)}</span>${c.d?`<span class="badge not_verifiable">found via ${esc(c.d)}</span>`:""}<span class="badge not_verifiable">row_id ${esc(c.id)}</span>${c.trunc?'<span class="badge not_verifiable">truncated</span>':""} extracted code</summary>${c.path?`<div class="label">${esc(c.path)}</div>`:""}<pre>${esc(c.c)}</pre></details>`;
}
function promptBadges(p){
  return `${p.main?`<span class="badge not_verifiable">${esc(p.main)}</span>`:""}${p.sub?`<span class="badge not_verifiable">${esc(p.sub)}</span>`:`<span class="badge not_verifiable">no intent label (${esc(p.st)})</span>`}`;
}
function showDetail(){
  const s = DATA.find(x=>x.l===current), v = view(s);
  document.getElementById("placeholder").style.display = "none";
  const det = document.getElementById("detail"); det.style.display = "block";
  det.innerHTML = `<div class="card"><h3>${esc(s.r||"(unknown repo)")}</h3>
    <div class="kv"><div><b>provider</b> ${esc(s.v)}</div><div><b>year</b> ${s.y||"N/A"}</div><div><b>model</b> ${esc(s.m)||"N/A"}</div></div>
    <div style="margin-top:8px"><a href="${esc(s.l)}" target="_blank" rel="noopener">${esc(s.l)}</a></div>
    ${transcriptSummary(s, TOKEN_NOTE)}</div>
    <div class="card"><h3>Conversation</h3>${renderTranscript(s, v, candHtml, promptBadges)}</div>`;
}
const bind = (id,k) => document.getElementById(id).addEventListener(id==="filterBox"?"input":"change", e => {F[k]=e.target.value.trim(); renderList(); if(current) showDetail();});
bind("filterBox","txt");bind("fProv","prov");bind("fAd","ad");bind("fDove","dove");bind("fYear","year");bind("fModel","model");bind("fMain","main");bind("fSub","sub");
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
                    help="also include conversations whose user messages are not English (default: English only)")
    args = ap.parse_args()
    from_fare = [l for l in sorted(set(args.languages))
               if not os.path.exists(os.path.join(OUT_DIR, f"browser_chat_annotated_{l}.html"))]
    for l in sorted(set(args.languages) - set(from_fare)):
        print(f"{l}: browser_chat_annotated_{l}.html already exists, not overwriting it.")
    if not from_fare:
        sys.exit(0)

    per_language = {l: {} for l in from_fare}
    with open(IN_PATH, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r["language"] not in per_language:
                continue
            link = r["conversation_url"]
            s = per_language[r["language"]].setdefault(link, {
                "l": link, "r": (json.loads(r["repo"]) or [None])[0] if r["repo"] else None,
                "v": r["provider"], "y": year_from_share(link), "m": r["model"] or None, "prompts": {}})
            p = s["prompts"].setdefault(r["prompt"], {
                "p": r["prompt"][:MAX_PROMPT], "trunc": len(r["prompt"]) > MAX_PROMPT,
                "main": r["main_category"] or None, "sub": r["sub_category"] or None,
                "st": r["status"], "cands": []})
            p["cands"].append({"id": r["row_id"], "a": r["adopted"], "d": r["found_via"] or None,
                               "path": r["found_path"] or None, "c": r["response_code"][:MAX_CODE],
                               "trunc": len(r["response_code"]) > MAX_CODE})
    needed = {url for d in per_language.values() for url in d}
    packed, excluded = {}, 0
    with open(CONV_PATH, encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            url = c["conversation_url"]
            if url not in needed:
                continue
            if not args.all_languages and not hv.conversation_is_english(c["turns"]):
                excluded += 1
                continue
            packed[url] = hv.pack_conversation(c["turns"])
    print(f"Conversations: {len(packed)} kept, {excluded} excluded (user messages not in English); tokens: {hv.TOKEN_METHOD}")
    os.makedirs(OUT_DIR, exist_ok=True)
    for l in from_fare:
        sessions, n, located, total = [], 0, 0, 0
        for url, s in per_language[l].items():
            if url not in packed:
                continue
            prompts = list(s["prompts"])
            conv = copy.deepcopy(packed[url])
            located += hv.attach_prompts(conv, prompts)
            total += len(prompts)
            s.update(conv)
            s["prompts"] = list(s["prompts"].values())
            n += sum(len(p["cands"]) for p in s["prompts"])
            sessions.append(s)
        b64 = hv.pack_payload(sessions)
        html = (HTML_TMPL.replace("__VIEWER_CSS__", hv.VIEWER_CSS)
                .replace("__VIEWER_JS__", hv.VIEWER_JS.replace("__CAP_TURN__", str(hv.CAP_TURN)))
                .replace("__TOKEN_NOTE__", hv.TOKEN_METHOD)
                .replace("__DATA_B64__", b64).replace("__LING__", l).replace("__N__", str(n)))
        out = os.path.join(OUT_DIR, f"browser_chat_annotated_{l}.html")
        with open(out, "x", encoding="utf-8") as f:
            f.write(html)
        print(f"Written {out} ({len(sessions)} conversations, {n} pairs, prompts located in transcript {located}/{total}, {os.path.getsize(out)//1_000_000} MB)")

if __name__ == "__main__":
    main()
