#!/usr/bin/env python
"""
Generates a standalone HTML browser to manually inspect the harnesses
detected in the agentic repos (see 3_01_agentic_extract_harness.py and
3_02_agentic_download_harness.py) and how they are linked to the sessions - including
the actual content of the configuration files, and the
intent classification (main category + sub-category) of the
sessions' prompts, where available.

Three navigation levels:
  1. REPO list (those with at least one harness mechanism) - filters on
     tool, mechanism, year, aggregate adoption, intent.
  2. Repo detail: content of all the harness files found + list
     of the sessions linked to that repo.
  3. Session detail: transcript (prompt/response per turn, like the
     dataset browser), with intent badges (main category +
     sub-category) on the user turns that were classified.

Limits:
  - harness = CURRENT state of the repo (see 3_01_agentic_extract_harness.py).
  - intent per turn: available only for the turns that match exactly
    (textual match) a message classified in
    4_02_agentic_intent_classification/ - the classification covers only the
    prompts that produced a clean code candidate in the 4 target
    languages (see 1_prepare_context.py), not all the
    turns of the session.
  - to limit the file size, the sessions show at most
    the first 10 turns (truncated to 700 characters each) - not the whole
    conversation if longer.

Usage:
    python pipeline/6_04_agentic_html_harness.py
"""
import base64
import csv
import glob
import importlib
import json
import os
import re
import sys

from langdetect import DetectorFactory, detect
DetectorFactory.seed = 0  # deterministic results across runs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_h2 = importlib.import_module("3_02_agentic_download_harness")
tool_per_path, all_paths, repo_safe, MECH_COLS = _h2.tool_per_path, _h2.all_paths, _h2.repo_safe, _h2.MECH_COLS

csv.field_size_limit(50_000_000)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AGENTIC_DIR = os.path.join(SCRIPT_DIR, "out_vibe_coding")
HARNESS_FILES_DIR = os.path.join(AGENTIC_DIR, "harness_files")
INTENT_DIR = os.path.join(SCRIPT_DIR, "4_02_agentic_intent_classification", "data")
OUT_PATH = os.path.join(SCRIPT_DIR, "browser_dataset", "browser_harness.html")

MAIN = {"1": "Code Authoring", "2": "Failure Reporting", "3": "Inquiry",
        "4": "Context Specification", "5": "Validation", "6": "Delegation",
        "7": "Workflow Control", "8": "Others"}
MAX_TURNS = 10
MAX_TURN_CHARS = 700
MAX_FILE_CHARS = 6000


def load_sha_language_target():
    """sha of the sessions with at least one candidate in the 4 target languages -
    via messages.csv (the candidates_with_real_prompt() extraction used to
    prepare the classification, already filtered on the 4 languages by
    construction - see 1_prepare_context.py), NOT via
    out_vibe_coding/snippet.csv (DIFFERENT extraction by
    1_04_extract_and_filter.py: overlap only 1,493/1,757, smaller
    population - using that one would have been a step backwards, not forwards).
    ~100% of the messages of this population have already been
    classified (14,634/14,654), so it coincides almost exactly with
    "classified sessions"."""
    link_to_sha = {}
    with open(os.path.join(AGENTIC_DIR, "conversations.jsonl"), encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            link_to_sha[rec["conversation_url"]] = rec["sha"]
    sha_target = set()
    with open(os.path.join(SCRIPT_DIR, "4_02_agentic_intent_classification", "data", "messages.csv"),
              encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            sha = link_to_sha.get(r["conversation_url"])
            if sha:
                sha_target.add(sha)
    return sha_target


def load_harness_config():
    with open(os.path.join(AGENTIC_DIR, "harness_config.csv"), encoding="utf-8", newline="") as f:
        return {r["repo"]: r for r in csv.DictReader(f)}


def load_intent_per_sha():
    """sha -> {exact_prompt_text: (main_category, sub_category)} - the
    match is by exact text of the user turn (see
    1_prepare_context.py: truncated_content = head_tail(prompt,
    10000), hence identical to the turn for prompts under 10k characters,
    the vast majority)."""
    msg_to_link = {}
    with open(os.path.join(INTENT_DIR, "messages.csv"), encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            msg_to_link[r["message_id"]] = (r["conversation_url"], r["truncated_content"])

    link_to_sha = {}
    with open(os.path.join(AGENTIC_DIR, "conversations.jsonl"), encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            link_to_sha[rec["conversation_url"]] = rec["sha"]

    out = {}
    for path in sorted(glob.glob(os.path.join(INTENT_DIR, "parsed", "*.jsonl"))):
        for line in open(path, encoding="utf-8"):
            rec = json.loads(line)
            main, sub = None, None
            for lab in rec.get("labels") or []:
                m = re.match(r"\s*(\d)\.(\d+)", lab.get("sub_category") or "")
                if m:
                    main, sub = MAIN.get(m.group(1)), lab["sub_category"].strip()
                    break
                m = re.match(r"\s*(\d)", lab.get("main_category") or "")
                if m:
                    main = MAIN.get(m.group(1))
                    break
            if not main:
                continue
            link, prompt_txt = msg_to_link.get(str(rec["message_id"]), (None, None))
            sha = link_to_sha.get(link)
            if not sha or not prompt_txt:
                continue
            out.setdefault(sha, {})[prompt_txt] = (main, sub)
    return out


def outcome_per_sha_agentic():
    yes, no = set(), set()
    for path, campo, values_yes, excluded in [
        (f"{AGENTIC_DIR}/adoption_cli_agent_direct.csv", "code_verified", ("yes",), ()),
        (f"{AGENTIC_DIR}/adoption_cli_agent_fallback.csv", "found", ("yes",),
         ("n/a_no_session_snippet", "file_not_found_today", "n.d._transcript_chat")),
        (f"{AGENTIC_DIR}/adoption_cli_agent_authorship.csv", "code_verified", ("yes",), ()),
        (f"{AGENTIC_DIR}/adoption_ide_direct.csv", "code_verified", ("yes",), ()),
        (f"{AGENTIC_DIR}/adoption_ide_commit_message.csv", "code_verified", ("yes",),
         ("n/a", "n/a_no_session_snippet")),
        (f"{AGENTIC_DIR}/adoption_ide_fallback.csv", "found", ("yes",),
         ("n/a_no_session_snippet", "file_not_found_today")),
        (f"{AGENTIC_DIR}/adoption_whole_repo.csv", "found", ("yes",), ()),
    ]:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r[campo] in excluded:
                    continue
                (yes if r[campo] in values_yes else no).add(r["sha"])
    return yes, no


def termini_per_file(path):
    """Text search terms to verify whether a file/skill/
    subagent is explicitly mentioned in the transcript of a session -
    a WEAK but verifiable signal of actual use (temporal presence
    alone only says that the file EXISTED, not that the session
    used it). It includes both the full file name (e.g. "claude.md",
    more specific) and the "logical" name without extension (e.g.
    "code-reviewer" from ".claude/agents/code-reviewer.md") - the latter
    may give false positives for generic names (e.g. "claude" from
    CLAUDE.md), see the note in the HTML."""
    basename = path.rsplit("/", 1)[-1].lower()
    termini = {basename}
    nome = basename.rsplit(".", 1)[0] if "." in basename else basename
    if nome and len(nome) >= 4:  # avoid terms that are too short/noisy
        termini.add(nome)
    return termini


def first_prompt_user(turns_raw):
    for t in turns_raw:
        if (t.get("role") or "") == "user":
            text = t.get("text") or ""
            if len(text) > 20:
                return text[:300]
    return None


def is_english(turns_raw):
    """Natural-language detection (not the programming language)
    of the first user prompt, via langdetect - used to limit the
    browser to English sessions only. Text that is too short (<20
    characters) or failed detection -> excluded to be safe (not
    verifiable as English)."""
    text = first_prompt_user(turns_raw)
    if not text:
        return False
    try:
        return detect(text) == "en"
    except Exception:
        return False


def read_file_locale(repo, path):
    dest = os.path.join(HARNESS_FILES_DIR, repo_safe(repo), path)
    if not os.path.isfile(dest):
        # include the case of a "skills" entry (DIRECTORY placeholder,
        # not downloadable as a file - see classify() in
        # 3_01_agentic_extract_harness.py) which by coincidence is also an
        # ancestor, on the local filesystem, of a real file nested further
        # down (e.g. a skill on "langchain" that includes inside it
        # a reference "agents.md" file - different real path, same
        # prefix)
        return None
    try:
        with open(dest, encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        return None


def main():
    print("Loading harness_config.csv...")
    harness_cfg = load_harness_config()
    repos_with_harness = [r for r, row in harness_cfg.items()
                          if row["found"] == "yes" and int(row["n_mechanisms"]) > 0]
    print(f"  repos with harness: {len(repos_with_harness)}")

    print("Loading intent per sha (agentic classification)...")
    intent_per_sha = load_intent_per_sha()
    print(f"  sessions with at least one classified turn: {len(intent_per_sha)}")

    print("Loading sessions with snippets in the 4 target languages...")
    sha_target = load_sha_language_target()
    print(f"  sessions with a candidate in the 4 target languages: {len(sha_target)}")

    print("Loading adoption outcome...")
    yes, no = outcome_per_sha_agentic()
    print(f"  sessions with known outcome: {len(yes) + len(no)}")

    print("Loading creation dates of the harness files (temporal correction)...")
    data_cache = {}
    cache_path = os.path.join(AGENTIC_DIR, "harness_date_cache.pkl")
    if os.path.exists(cache_path):
        import pickle
        with open(cache_path, "rb") as f:
            data_cache = pickle.load(f)

    print("Building repo -> sessions (4 target languages only, English prompts only, harness already temporally corrected)...")
    sessions_per_repo = {}
    n_discarded_language_prog, n_discarded_without_harness, n_discarded_language_natural = 0, 0, 0
    with open(os.path.join(AGENTIC_DIR, "conversations.jsonl"), encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            sha = rec["sha"]
            if sha not in sha_target:  # no python/javascript/typescript/java snippets
                n_discarded_language_prog += 1
                continue
            if not rec.get("harness"):  # harness already temporally corrected in conversations.jsonl
                n_discarded_without_harness += 1
                continue
            if not is_english(rec.get("turns") or []):
                n_discarded_language_natural += 1
                continue
            repos_sess = [a["repo"] for a in (rec.get("artifacts") or []) if a.get("repo")]
            for repo in repos_sess:
                if repo not in harness_cfg:
                    continue
                sessions_per_repo.setdefault(repo, []).append(rec)
    print(f"  discarded (no snippet in the 4 target languages): {n_discarded_language_prog}")
    print(f"  discarded (language ok but harness not yet present at the time): {n_discarded_without_harness}")
    print(f"  discarded (not in English / not detectable): {n_discarded_language_natural}")

    print("Assembling repo data...")
    repos_out = []
    for repo, sessions_rec in sessions_per_repo.items():
        row = harness_cfg[repo]
        paths = all_paths(row)

        files_out = []
        for p in paths:
            content = read_file_locale(repo, p)
            if content is None:
                continue
            truncated = len(content) > MAX_FILE_CHARS
            files_out.append({"path": p, "tool": tool_per_path(p),
                               "creation_date": data_cache.get((repo, p)),
                               "termini": termini_per_file(p), "mentions": [],
                               "content": content[:MAX_FILE_CHARS], "truncated": truncated})

        sessions_out = []
        tools_repo, mechanisms_repo = set(), set()
        for rec in sessions_rec:
            sha = rec["sha"]
            turns_raw = rec.get("turns") or []
            intent_map = intent_per_sha.get(sha, {})

            # Prompt->response pairing (as in the dataset browser,
            # see generate_html_browser_dataset.py) - here the raw turns of
            # conversations.jsonl are not already paired like the rows of
            # snippet.csv, so we pair them ourselves: each user turn with
            # the immediately following turn if it is a response.
            pairs = []
            i = 0
            while i < len(turns_raw) and len(pairs) < MAX_TURNS:
                t = turns_raw[i]
                if (t.get("role") or "") != "user":
                    i += 1
                    continue
                prompt_text = t.get("text") or ""
                response_text = ""
                if i + 1 < len(turns_raw) and (turns_raw[i + 1].get("role") or "") in ("assistant", "agent"):
                    response_text = turns_raw[i + 1].get("text") or ""
                    i += 2
                else:
                    i += 1
                pairs.append((prompt_text, response_text))

            # Harness<->session link: weak but verifiable signal -
            # is the file/skill/subagent explicitly mentioned in THIS
            # prompt/response pair (only the turns actually shown,
            # so that the "go to turn" link always points to something
            # visible)? It does not prove use, but it is stronger than mere
            # temporal coexistence.
            session_idx = len(sessions_out)
            turns_out = []
            mentioned_session = set()
            for turn_idx, (prompt_text, response_text) in enumerate(pairs):
                main, sub = intent_map.get(prompt_text, (None, None))
                text_pair = (prompt_text + " " + response_text).lower()
                mentioned_turn = []
                for f in files_out:
                    if any(term in text_pair for term in f["termini"]):
                        mentioned_turn.append(f["path"])
                        mentioned_session.add(f["path"])
                        f.setdefault("mentions", []).append({"session_idx": session_idx, "turn_idx": turn_idx})
                turns_out.append({
                    "prompt": prompt_text[:MAX_TURN_CHARS], "prompt_truncated": len(prompt_text) > MAX_TURN_CHARS,
                    "response": response_text[:MAX_TURN_CHARS], "response_truncated": len(response_text) > MAX_TURN_CHARS,
                    "intent": main, "intent_sub": sub, "mentioned": mentioned_turn,
                })

            models = rec.get("models_used") or []
            year = None
            if rec.get("timestamp"):
                try:
                    year = int(rec["timestamp"][:4])
                except Exception:
                    pass
            harness_session = rec.get("harness") or []
            mechanisms_session = rec.get("harness_mechanisms") or []
            tools_repo.update(harness_session)
            mechanisms_repo.update(mechanisms_session)

            sessions_out.append({
                "sha": sha, "link": rec.get("conversation_url"),
                "year": year, "model": models[0] if models else None,
                "harness": harness_session, "harness_mechanisms": mechanisms_session,
                "harness_mentioned": sorted(mentioned_session),
                "n_turns_total": len(turns_raw),
                "n_turns_shown": len(turns_out),
                "adopted": "yes" if sha in yes else ("no" if sha in no else None),
                "n_classified": sum(1 for t in turns_out if t["intent"]),
                "turns": turns_out,
            })

        for f in files_out:
            del f["termini"]  # only for the calculation above, not needed client-side

        repos_out.append({
            "repo": repo, "tools": sorted(tools_repo), "mechanisms": sorted(mechanisms_repo),
            "n_files": len(files_out), "files": files_out,
            "n_sessions": len(sessions_out), "sessions": sessions_out,
        })

    print(f"Repos assembled: {len(repos_out)}")
    write_html(repos_out)


HTML_TMPL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Harness browser</title>
<style>
  :root{ --bg:#f5f6f8; --panel:#fff; --border:#dfe3e8; --text:#1c2128; --muted:#6b7280; --accent:#2563eb; }
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;background:var(--bg);color:var(--text);font-size:14px}
  #app{display:grid;grid-template-columns:380px 1fr;height:100vh}
  #sidebar{border-right:1px solid var(--border);display:flex;flex-direction:column;background:var(--panel)}
  #topbar{padding:12px;border-bottom:1px solid var(--border)}
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
  .badge.tool{background:#e0e7ff;color:#3730a3}
  .badge.mech{background:#ede9fe;color:#5b21b6}
  .badge.yes{background:#dcfce7;color:#166534}
  .badge.no{background:#fee2e2;color:#991b1b}
  .badge.unknown{background:#f3f4f6;color:#6b7280}
  #main{overflow-y:auto;padding:24px}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px 20px;margin-bottom:16px}
  h3{margin:0 0 8px;font-size:14.5px}
  pre{background:#f6f8fa;border:1px solid var(--border);color:#1c2128;padding:12px;border-radius:6px;overflow-x:auto;font-size:12px;white-space:pre-wrap;word-break:break-word}
  .kv{display:flex;flex-wrap:wrap;gap:8px 20px;font-size:12.5px}
  .kv div{display:flex;gap:6px}
  .kv b{color:var(--muted);font-weight:600}
  a{color:var(--accent)}
  .backlink{display:inline-block;margin-bottom:12px;cursor:pointer;color:var(--accent);font-size:12.5px}
  .file-block{border:1px solid var(--border);border-radius:8px;margin-bottom:10px;overflow:hidden}
  .file-block .fname{background:#f6f8fa;padding:7px 12px;font-size:12px;font-weight:600;cursor:pointer}
  .file-block .fname:hover{background:#eaf1fe}
  .file-block pre{margin:0;border:none;border-radius:0;max-height:260px}
  .sess-row{border:1px solid var(--border);border-radius:8px;padding:9px 12px;margin-bottom:8px;cursor:pointer;font-size:12.5px}
  .sess-row:hover{background:#eaf1fe}
  .turn{border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:10px;font-size:12.5px}
  .turn.highlighted{border:2px solid var(--accent);box-shadow:0 0 0 3px rgba(37,99,235,0.15)}
  .turn .idx{color:var(--muted);font-size:11px;margin-bottom:5px}
  .turn .label{color:var(--muted);font-size:10.5px;text-transform:uppercase;margin:6px 0 2px}
  .turn .text{white-space:pre-wrap;word-break:break-word;max-height:160px;overflow-y:auto;background:#f6f8fa;border:1px solid var(--border);border-radius:6px;padding:8px;font-size:12px}
  .mention-links{padding:6px 12px;display:flex;flex-wrap:wrap;gap:6px;border-bottom:1px solid var(--border)}
  .mention-btn{font-size:11px;padding:3px 8px;border-radius:6px;border:1px solid var(--border);background:#eaf1fe;color:var(--accent);cursor:pointer}
  .mention-btn:hover{background:var(--accent);color:#fff}
</style>
</head>
<body>
<div id="app">
  <div id="sidebar">
    <div id="topbar">
      <h2>Harness browser (__N__ repo)</h2>
      <div class="note">Shows ENGLISH-language sessions (langdetect on the first user prompt) with a code candidate in one of the 4 target languages (python/javascript/typescript/java), almost all of which have been classified, whose harness was already present at the time of that session (file's first commit date &le; session date). "Mentioned in transcript" is a WEAK proxy for actual harness use: a case-insensitive text search for the file/skill/subagent name anywhere in the session's full transcript - presence only proves the file existed, this proves it was at least referenced by name, neither proves the agent actually followed it. Generic names (e.g. "claude" from CLAUDE.md) can false-positive.</div>
      <div id="count"></div>
      <input id="filterBox" type="text" placeholder="Filter by repo...">
      <div class="filter-label">Harness tool</div>
      <select id="filterTool"><option value="">All</option></select>
      <div class="filter-label">Mechanism</div>
      <select id="filterMech"><option value="">All</option></select>
      <div class="filter-label">Adoption (any session)</div>
      <select id="filterAdopted"><option value="">All</option><option value="yes">At least one adopted</option><option value="no">None adopted</option><option value="unknown">Unknown</option></select>
      <div class="filter-label">Intent (any session)</div>
      <select id="filterIntent"><option value="">All</option></select>
    </div>
    <div id="list"></div>
  </div>
  <div id="main">
    <div id="placeholder" style="color:var(--muted);text-align:center;margin-top:80px">Select a repo from the list on the left.</div>
    <div id="detail" style="display:none"></div>
  </div>
</div>
<script>
const DATA_B64 = "__DATA_B64__";
const REPOS = JSON.parse(decodeURIComponent(escape(atob(DATA_B64))));
let currentRepo = null;
let filter = "", fTool = "", fMech = "", fAdopted = "", fIntent = "";

function escapeHtml(s){ return (s==null?"":String(s)).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
function adKey(s){ return s.adopted === "yes" ? "yes" : (s.adopted === "no" ? "no" : "unknown"); }
function adLabel(k){ return k === "unknown" ? "unknown" : (k === "yes" ? "adopted" : "not adopted"); }
function repoAdAgg(r){
  const keys = r.sessions.map(adKey);
  if (keys.includes("yes")) return "yes";
  if (keys.includes("no")) return "no";
  return "unknown";
}
function repoIntents(r){
  const s = new Set();
  for (const sess of r.sessions) for (const t of sess.turns) if (t.intent) s.add(t.intent);
  return s;
}

function fillFilters(){
  const fill = (id, values) => {
    const sel = document.getElementById(id);
    for (const v of values){ if (v==null || v==="") continue; const o = document.createElement("option"); o.value=v; o.textContent=v; sel.appendChild(o); }
  };
  fill("filterTool", [...new Set(REPOS.flatMap(r => r.tools))].sort());
  fill("filterMech", [...new Set(REPOS.flatMap(r => r.mechanisms))].sort());
  fill("filterIntent", [...new Set(REPOS.flatMap(r => [...repoIntents(r)]))].sort());
}

function renderList(){
  const list = document.getElementById("list");
  list.innerHTML = "";
  let shown = 0;
  const f = filter.toLowerCase();
  for (const r of REPOS){
    if (f && !r.repo.toLowerCase().includes(f)) continue;
    if (fTool && !r.tools.includes(fTool)) continue;
    if (fMech && !r.mechanisms.includes(fMech)) continue;
    if (fAdopted && repoAdAgg(r) !== fAdopted) continue;
    if (fIntent && !repoIntents(r).has(fIntent)) continue;
    shown++;
    const div = document.createElement("div");
    div.className = "item" + (currentRepo === r.repo ? " active" : "");
    div.innerHTML = `
      <div class="repo">${escapeHtml(r.repo)}</div>
      <div class="summary">${r.n_files} file &middot; ${r.n_sessions} session${r.n_sessions===1?"":"s"}</div>
      ${r.tools.map(t => `<span class="badge tool">${escapeHtml(t)}</span>`).join("")}
      <span class="badge ${repoAdAgg(r)}">${adLabel(repoAdAgg(r))}</span>`;
    div.onclick = () => selectRepo(r.repo);
    list.appendChild(div);
  }
  document.getElementById("count").innerHTML = `<b>${shown}</b> / ${REPOS.length} repos shown`;
}

function selectRepo(repo){
  currentRepo = repo;
  renderList();
  const r = REPOS.find(x => x.repo === repo);
  document.getElementById("placeholder").style.display = "none";
  const detail = document.getElementById("detail");
  detail.style.display = "block";

  const filesHtml = r.files.map((f, i) => `
    <div class="file-block">
      <div class="fname">${escapeHtml(f.path)} <span class="badge tool">${escapeHtml(f.tool)}</span>${f.creation_date ? ` <span class="badge unknown">created ${escapeHtml(f.creation_date)}</span>` : ""} <span class="badge ${f.mentions.length ? "yes" : "unknown"}">mentioned ${f.mentions.length} time(s)</span></div>
      ${f.mentions.length ? `<div class="mention-links">${f.mentions.map(m => `<button class="mention-btn" onclick="selectSession('${repo.replace(/'/g,"\\'")}', ${m.session_idx}, ${m.turn_idx})">&rarr; ${escapeHtml(r.sessions[m.session_idx].sha.slice(0,8))} / turn ${m.turn_idx + 1}</button>`).join("")}</div>` : ""}
      <pre>${escapeHtml(f.content)}${f.truncated ? "\\n\\n[... truncated ...]" : ""}</pre>
    </div>`).join("") || "<p style='color:var(--muted)'>No file content available.</p>";

  const sessHtml = r.sessions.map((s, i) => `
    <div class="sess-row" onclick="selectSession('${repo.replace(/'/g,"\\'")}', ${i})">
      <b>${escapeHtml(s.sha.slice(0,10))}</b> &middot; ${s.n_turns_total} turns (showing ${s.n_turns_shown} prompt&rarr;response pairs) &middot; ${s.n_classified} classified
      <span class="badge ${adKey(s)}">${adLabel(adKey(s))}</span>
      ${s.year ? `<span class="badge unknown">${s.year}</span>` : ""}
      ${s.model ? `<span class="badge unknown">${escapeHtml(s.model)}</span>` : ""}
      <br>${s.harness.map(t => `<span class="badge tool">${escapeHtml(t)}</span>`).join("")}
      <span class="badge ${s.harness_mentioned.length ? "yes" : "unknown"}">${s.harness_mentioned.length ? s.harness_mentioned.length + " harness item(s) mentioned in text" : "no harness mention in text"}</span>
    </div>`).join("") || "<p style='color:var(--muted)'>No linked sessions.</p>";

  detail.innerHTML = `
    <div class="card">
      <h3>${escapeHtml(r.repo)}</h3>
      <div class="kv">
        <div><b>tools</b> ${r.tools.map(escapeHtml).join(", ") || "N/A"}</div>
        <div><b>mechanisms</b> ${r.mechanisms.map(escapeHtml).join(", ") || "N/A"}</div>
        <div><b>sessions</b> ${r.n_sessions}</div>
      </div>
      <div style="margin-top:8px"><a href="https://github.com/${escapeHtml(r.repo)}" target="_blank" rel="noopener">github.com/${escapeHtml(r.repo)}</a></div>
    </div>
    <div class="card">
      <h3>Harness files (${r.n_files})</h3>
      ${filesHtml}
    </div>
    <div class="card">
      <h3>Sessions (click for full transcript)</h3>
      ${sessHtml}
    </div>
  `;
}

function selectSession(repo, idx, highlightTurnIdx){
  const r = REPOS.find(x => x.repo === repo);
  const s = r.sessions[idx];
  const detail = document.getElementById("detail");
  document.getElementById("placeholder").style.display = "none";
  detail.style.display = "block";
  const turnsHtml = s.turns.map((t, i) => `
    <div class="turn${i === highlightTurnIdx ? " highlighted" : ""}" id="turn-${i}">
      <div class="idx">Turn ${i+1} / ${s.n_turns_shown}${s.n_turns_total > s.n_turns_shown * 2 ? ` (of ${s.n_turns_total} raw turns total)` : ""}</div>
      ${t.intent ? `<span class="badge mech">${escapeHtml(t.intent)}</span>` : ""}
      ${t.intent_sub ? `<span class="badge mech">${escapeHtml(t.intent_sub)}</span>` : ""}
      ${t.mentioned.length ? `<span class="badge yes">mentions: ${t.mentioned.map(escapeHtml).join(", ")}</span>` : ""}
      <div class="label">Prompt</div>
      <div class="text">${escapeHtml(t.prompt)}${t.prompt_truncated ? " [...]" : ""}</div>
      <div class="label">Response</div>
      <div class="text">${escapeHtml(t.response) || "(no response turn found)"}${t.response_truncated ? " [...]" : ""}</div>
    </div>`).join("");
  detail.innerHTML = `
    <div class="backlink" onclick="selectRepo('${repo.replace(/'/g,"\\'")}')">&larr; Back to ${escapeHtml(repo)}</div>
    <div class="card">
      <h3>Session ${escapeHtml(s.sha.slice(0,16))}</h3>
      <div class="kv">
        <div><b>adoption</b> ${adLabel(adKey(s))}</div>
        <div><b>year</b> ${s.year || "N/A"}</div>
        <div><b>model</b> ${escapeHtml(s.model) || "N/A"}</div>
        <div><b>classified turns</b> ${s.n_classified} / ${s.n_turns_shown}</div>
        <div><b>harness (as of this session)</b> ${s.harness.map(escapeHtml).join(", ") || "N/A"}</div>
        <div><b>mechanisms (as of this session)</b> ${s.harness_mechanisms.map(escapeHtml).join(", ") || "N/A"}</div>
        <div><b>mentioned in transcript</b> ${s.harness_mentioned.map(escapeHtml).join(", ") || "none"}</div>
      </div>
      ${s.link ? `<div style="margin-top:8px"><a href="${escapeHtml(s.link)}" target="_blank" rel="noopener">${escapeHtml(s.link)}</a></div>` : ""}
    </div>
    <div class="card">
      <h3>Transcript</h3>
      ${turnsHtml}
    </div>
  `;
  if (highlightTurnIdx !== undefined) {
    const el = document.getElementById("turn-" + highlightTurnIdx);
    if (el) el.scrollIntoView({behavior: "smooth", block: "center"});
  }
}

document.getElementById("filterBox").addEventListener("input", e => { filter = e.target.value.trim(); renderList(); });
document.getElementById("filterTool").addEventListener("change", e => { fTool = e.target.value; renderList(); });
document.getElementById("filterMech").addEventListener("change", e => { fMech = e.target.value; renderList(); });
document.getElementById("filterAdopted").addEventListener("change", e => { fAdopted = e.target.value; renderList(); });
document.getElementById("filterIntent").addEventListener("change", e => { fIntent = e.target.value; renderList(); });

fillFilters();
renderList();
</script>
</body>
</html>
"""


def write_html(repos_out):
    data_json = json.dumps(repos_out, ensure_ascii=False)
    data_b64 = base64.b64encode(data_json.encode("utf-8")).decode("ascii")
    html_out = HTML_TMPL.replace("__DATA_B64__", data_b64).replace("__N__", str(len(repos_out)))
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(html_out)
    print(f"Written {OUT_PATH}")


if __name__ == "__main__":
    main()
