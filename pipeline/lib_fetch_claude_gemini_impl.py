#!/usr/bin/env python3
"""
collect_cg.py — STANDALONE COLLECTION (Stage B only) of Claude and Gemini,
in the EXACT format of rich_dataset. No import of the pipeline or of other
modules: all the logic of the rich schema is copied in here.

It starts from the links already present in the dataset (.json snapshot OR rich_dataset.jsonl),
redoes only the fetch+parse of Claude/Gemini with the parsers that work, and produces
rows identical to the RICH_COLUMNS schema -> ready to be merged with the ChatGPT rows.

  Claude : direct GET to the chat_snapshots API (no browser), thread pool.
  Gemini : Playwright render with infinite-scroller scrolling, then DOM scraping
           with reconstruction of the markdown fences from the <code-block>s.
  English filter ACTIVE by default (is_english); disable it with --any-language.
  Snippets validated with looks_like_code() (pygments token density + ast/json).
  Resumable: on rerun it resumes from the links not done; errors are retried.

Usage:
  python collect_cg.py --from-json rich_dataset.jsonl --limit 20     # test
  python collect_cg.py --from-json rich_dataset.jsonl                # everything

Output (prefix --out, default cg_dataset):
  cg_dataset.jsonl           rich rows kept (Claude+Gemini, English)
  cg_dataset.csv             same rows in CSV (RICH_COLUMNS schema)
  cg_dataset.rejected.jsonl  discarded (non-English / empty)
  cg_dataset.errors.jsonl    failed fetches (retried on rerun)

Dependencies:  pip install beautifulsoup4 lxml requests langdetect pygments playwright
               playwright install chromium
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import re
import sys
import threading
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Same fix as 1_01_chat_discover_links.py: Playwright's Node driver is a
# long-lived process for the whole batch and its default V8 heap
# (~4GB) can blow up on large batches, making every subsequent fetch fail
# with "Connection closed while reading from the driver" (not
# a provider-side block). setdefault does not overwrite a NODE_OPTIONS
# already set by the user. It must come before async_playwright().
os.environ.setdefault("NODE_OPTIONS", "--max-old-space-size=8192")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

SHARE_RE = re.compile(
    r"https?://(?:claude\.ai/share/[A-Za-z0-9\-_]+"
    r"|gemini\.google\.com/share/[A-Za-z0-9\-_]+"
    r"|g\.co/gemini/share/[A-Za-z0-9\-_]+)")


def provider_of(url: str) -> str:
    u = (url or "").lower()
    if "claude.ai/share" in u:
        return "claude"
    if "gemini.google.com/share" in u or "g.co/gemini/share" in u:
        return "gemini"
    return "chatgpt"


# ==========================================================================
# ---- RICH SCHEMA (copied faithfully from collection_conv.py) ----------------
# ==========================================================================
CODE_FENCE = re.compile(r"```([a-zA-Z0-9_+#.\-]*)\n(.*?)```", re.S)

_LANG_ALIAS = {
    "py": "python", "py3": "python", "python3": "python",
    "js": "javascript", "node": "javascript", "jsx": "javascript",
    "ts": "typescript", "tsx": "typescript",
    "sh": "bash", "shell": "bash", "zsh": "bash", "console": "bash",
    "yml": "yaml", "c++": "cpp", "cs": "csharp", "rb": "ruby",
    "kt": "kotlin", "md": "markdown", "rs": "rust", "golang": "go",
    "plaintext": "text", "": None,
}


def _norm_lang(lang):
    if not lang:
        return None
    return _LANG_ALIAS.get(lang.strip().lower(), lang.strip().lower())


_PYGMENTS = None
_COMMON = {"python", "javascript", "typescript", "java", "c", "cpp", "csharp",
           "go", "ruby", "php", "rust", "bash", "sql", "html", "css", "json",
           "yaml", "xml", "kotlin", "swift", "scala", "r", "perl", "lua",
           "dart", "objective-c", "matlab", "markdown", "dockerfile", "text"}

_HEUR = [
    ("php",        re.compile(r"<\?php")),
    ("html",       re.compile(r"<(?:!doctype|html|head|body|div|span|p|a|script)\b", re.I)),
    ("sql",        re.compile(r"\bselect\b[\s\S]+?\bfrom\b|\binsert\s+into\b|\bcreate\s+table\b|\bupdate\b[\s\S]+?\bset\b|\bdelete\s+from\b", re.I)),
    ("python",     re.compile(r"(?m)^\s*(?:def |class |from \S+ import|@\w)|\bself\b|print\(|\belif\b|__name__")),
    ("rust",       re.compile(r"\bfn \w+|let mut |println!|::<")),
    ("go",         re.compile(r"\bpackage \w+|\bfunc \w+\(|fmt\.\w+")),
    ("typescript", re.compile(r"\binterface \w+|\: (?:string|number|boolean)\b|\bimport .* from ")),
    ("javascript", re.compile(r"\b(?:function|const|let|var)\b.*|=>|console\.log|document\.")),
    ("java",       re.compile(r"\b(?:public|private|protected)\s+(?:static\s+)?(?:class|void|int|String)\b|System\.out\.print")),
    ("cpp",        re.compile(r"#include\s*<[^>]+>.*(?:std::|cout|cin)|using namespace std", re.S)),
    ("c",          re.compile(r"#include\s*<[^>]+>|int\s+main\s*\(")),
    ("csharp",     re.compile(r"using System|Console\.Write|namespace \w+")),
    ("ruby",       re.compile(r"(?m)^\s*(?:def |end$|require )|puts |\.each do")),
    ("bash",       re.compile(r"(?m)^#!/.*sh\b|\b(?:echo|apt-get|sudo|chmod|export)\b")),
    ("yaml",       re.compile(r"(?m)^\s*[\w.-]+:\s+\S")),
]


def _heuristic_lang(code):
    s = code.strip()
    if s[:1] in "{[":
        try:
            json.loads(s)
            return "json"
        except Exception:
            pass
    for name, rx in _HEUR:
        if rx.search(code):
            return name
    return None


def detect_language(code):
    if not code or len(code.strip()) < 12:
        return None
    lang = _heuristic_lang(code)
    if lang:
        return lang
    global _PYGMENTS
    if _PYGMENTS is None:
        try:
            from pygments.lexers import guess_lexer
            _PYGMENTS = guess_lexer
        except Exception:
            _PYGMENTS = False
    if not _PYGMENTS:
        return None
    try:
        lexer = _PYGMENTS(code)
        name = _norm_lang(lexer.aliases[0] if lexer.aliases else lexer.name)
        return name if name in _COMMON else None
    except Exception:
        return None


def extract_code_blocks(turns):
    blocks = []
    last_user = None
    for t in turns:
        if t["role"] == "user":
            last_user = t["text"]
        elif t["role"] == "assistant":
            for lang, body in CODE_FENCE.findall(t["text"]):
                declared = _norm_lang(lang)
                detected = detect_language(body)
                blocks.append({
                    "language": declared or detected,
                    "language_declared": declared,
                    "language_detected": detected,
                    "prompt": last_user,
                    "code": body,
                })
    return blocks


# ---- code-validity: looks_like_code (pygments token density) -------------
_PYG_LEX = {}


def _code_density(code, declared=None):
    try:
        from pygments.lexers import get_lexer_by_name, guess_lexer
        from pygments.token import Token
    except Exception:
        return None
    lex = None
    if declared:
        if declared not in _PYG_LEX:
            try:
                _PYG_LEX[declared] = get_lexer_by_name(declared)
            except Exception:
                _PYG_LEX[declared] = False
        lex = _PYG_LEX[declared] or None
    if lex is None:
        try:
            lex = guess_lexer(code)
        except Exception:
            return (0.0, 1.0, 0)
    toks = [(tt, v) for tt, v in lex.get_tokens(code) if v.strip()]
    n = len(toks)
    if n == 0:
        return (0.0, 0.0, 0)
    codey = sum(1 for tt, _ in toks if tt in Token.Operator or tt in Token.Punctuation
                or tt in Token.Keyword or tt in Token.Literal.Number
                or tt in Token.Name.Function or tt in Token.Name.Class
                or tt in Token.Name.Tag or tt in Token.Name.Builtin
                or tt in Token.Literal.String)
    err = sum(1 for tt, _ in toks if tt in Token.Error)
    return (codey / n, err / n, n)


def _looks_like_code_regex(code):
    s = (code or "").strip()
    if not s:
        return False
    has_punct = bool(re.search(r"[{}()\[\];=]|::|=>|->|#include", s))
    lines = [l for l in s.splitlines() if l.strip()]
    arrows = len(re.findall(r"[\u2190-\u21FF\u2794\u27A1\u2022]|-->|==>", s))
    caps = sum(1 for l in lines if l.strip().isupper() and len(l.strip()) > 3)
    if arrows >= 2 or caps >= 2:
        return has_punct
    if has_punct:
        return True
    words = re.findall(r"[A-Za-z]+", s)
    if len(words) > 20 and len(lines) <= max(1, len(words) // 4):
        return False
    return True


def looks_like_code(code, declared=None):
    """True only for real source code (not diagrams/lists/prose).
    ast/json are definitive; otherwise the pygments token density decides."""
    s = (code or "").strip()
    if len(s) < 3:
        return False
    d = (declared or "").lower()
    if d in ("python", "py", "python3"):
        try:
            import ast
            ast.parse(s)
            return True
        except Exception:
            pass
    if d == "json":
        try:
            json.loads(s)
            return True
        except Exception:
            return False
    dens = _code_density(code, declared)
    if dens is None:
        return _looks_like_code_regex(code)
    codey, err, n = dens
    if n < 3:
        return False
    return codey >= 0.25 and err <= 0.20


def code_only(text):
    reals = [body for lang, body in CODE_FENCE.findall(text or "")
             if looks_like_code(body, _norm_lang(lang))]
    return "\n\n".join(b.strip("\n") for b in reals) if reals else None


def _msg_has_code(text):
    return any(looks_like_code(body, _norm_lang(lang))
               for lang, body in CODE_FENCE.findall(text or ""))


# ---- English filter -------------------------------------------------------
_EN_STOP = {"the", "and", "to", "of", "a", "is", "in", "for", "you", "this",
            "that", "it", "with", "on", "can", "how", "i", "we", "be", "are",
            "as", "at", "or", "an", "if", "not", "your", "my", "do", "does",
            "using", "use", "when", "what", "why", "please", "help", "code",
            "function", "error", "want", "need", "should", "would", "could",
            "here"}
_LANGDETECT = None


def is_english(turns):
    prose = " ".join(CODE_FENCE.sub(" ", t["text"] or "") for t in turns).strip()
    if len(prose) < 20:
        return True
    global _LANGDETECT
    if _LANGDETECT is None:
        try:
            from langdetect import detect, DetectorFactory
            DetectorFactory.seed = 0
            _LANGDETECT = detect
        except Exception:
            _LANGDETECT = False
    if _LANGDETECT:
        try:
            return _LANGDETECT(prose[:3000]) == "en"
        except Exception:
            pass
    words = re.findall(r"[a-zA-Z']+", prose.lower())
    return bool(words) and sum(1 for w in words if w in _EN_STOP) / len(words) >= 0.12


# ---- per-conversation fields ---------------------------------------------
def conversation_fields(turns):
    users = [t["text"] for t in turns if t["role"] == "user"]
    llms = [t["text"] for t in turns if t["role"] == "assistant"]
    llm_code = [t["text"] for t in turns
                if t["role"] == "assistant" and _msg_has_code(t["text"])]
    preceding, last_user = [], None
    for t in turns:
        if t["role"] == "user":
            last_user = t["text"]
        elif t["role"] == "assistant" and _msg_has_code(t["text"]):
            preceding.append(last_user)
    return {
        "first_user_message": users[0] if users else None,
        "first_llm_response": llms[0] if llms else None,
        "first_user_before_code": preceding[0] if preceding else None,
        "first_llm_with_code": llm_code[0] if llm_code else None,
        "first_llm_with_code_snippet": code_only(llm_code[0]) if llm_code else None,
        "n_total_messages": len(turns), "n_user_messages": len(users),
        "n_llm_messages": len(llms), "n_llm_messages_with_code": len(llm_code),
        "n_user_messages_with_code": sum(
            1 for t in turns if t["role"] == "user" and _msg_has_code(t["text"])),
        "last_user_before_code": preceding[-1] if preceding else None,
        "last_llm_with_code": llm_code[-1] if llm_code else None,
        "last_llm_with_code_snippet": code_only(llm_code[-1]) if llm_code else None}


# ---- user / provenance (GitHub optional) ------------------------------
GH_LOOKUP = False
_USER_CACHE = {}


def _gh_headers():
    tok = (os.environ.get("GITHUB_TOKEN")
           or (os.environ.get("GITHUB_TOKENS", "").split(",")[0].strip()))
    h = {"User-Agent": "collect-cg/1.0", "Accept": "application/vnd.github+json"}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def user_type(login, source):
    if not login:
        return None
    if login in _USER_CACHE:
        return _USER_CACHE[login]
    t = None
    if GH_LOOKUP:
        try:
            req = urllib.request.Request(
                f"https://api.github.com/users/{login}", headers=_gh_headers())
            with urllib.request.urlopen(req, timeout=20) as r:
                t = json.load(r).get("type")
        except Exception:
            t = None
    _USER_CACHE[login] = t
    return t


def profile_link(login, source):
    if not login:
        return None
    return f"https://github.com/{login}"


def _distinctive_line(code):
    c = [l.strip() for l in (code or "").splitlines() if 25 <= len(l.strip()) <= 120]
    c = [l for l in c if '"' not in l and "\\" not in l]
    c.sort(key=len, reverse=True)
    return c[0] if c else None


def find_snippet_file(repo, code):
    if not GH_LOOKUP:
        return None
    line = _distinctive_line(code)
    if not repo or not line:
        return None
    try:
        q = urllib.parse.quote(f'repo:{repo} "{line}"')
        req = urllib.request.Request(
            f"https://api.github.com/search/code?q={q}&per_page=1",
            headers=_gh_headers())
        with urllib.request.urlopen(req, timeout=25) as r:
            items = json.load(r).get("items", [])
        return items[0].get("html_url") if items else None
    except Exception:
        return None


RICH_COLUMNS = [
    ("first_user_message", "first user message"),
    ("first_llm_response", "first llm response"),
    ("first_user_before_code", "first user message before snippet"),
    ("first_llm_with_code", "first llm message with snippet"),
    ("first_llm_with_code_snippet", "first llm snippet (code only)"),
    ("n_total_messages", "total number of messages"),
    ("n_user_messages", "number of user messages"),
    ("n_llm_messages", "number of LLM messages"),
    ("n_llm_messages_with_code", "number of llm messages with snippet"),
    ("n_user_messages_with_code", "number of user messages with snippet"),
    ("last_user_before_code", "last user message before snippet"),
    ("last_llm_with_code", "last llm message with snippet"),
    ("last_llm_with_code_snippet", "last llm snippet (code only)"),
    ("repo_name", "repo name"), ("repo_link", "link repo"), ("chat_link", "link chat"),
    ("artifact_type", "type of metadata with chat link"),
    ("artifact_text", "text of metadata with chat link"),
    ("last_snippet_repo_file", "repo file of the last snippet (link)"),
    ("snippet_repo_files", "repo file with snippet of the conversation"),
    ("user_profile_link", "user profile link"), ("user_type", "user type"),
    ("language", "language (fence label)"),
    ("language_detected", "language recognized from the snippet"),
    ("provider", "provider"), ("model_slug", "model")]


def build_rich_row(conv, art, provenance=False):
    turns = conv.get("turns") or []
    blocks = extract_code_blocks(turns)
    labeled = [b for b in blocks
               if b["language_declared"] and looks_like_code(b["code"], b["language_declared"])]
    langs = [b["language_declared"] for b in labeled]
    detected = [b["language_detected"] for b in labeled if b["language_detected"]]
    repo = art.get("repo")
    login = art.get("author")
    source = art.get("source")
    row = conversation_fields(turns)
    row.update({
        "repo_name": repo,
        "repo_link": f"https://github.com/{repo}" if repo else None,
        "chat_link": conv.get("share_url"), "artifact_type": source,
        "artifact_text": art.get("artifact_text"),
        "user_profile_link": art.get("user_profile_link") or profile_link(login, source),
        "user_type": art.get("user_type") or user_type(login, source),
        "language": Counter(langs).most_common(1)[0][0] if langs else None,
        "language_detected": Counter(detected).most_common(1)[0][0] if detected else None,
        "provider": provider_of(conv.get("share_url", "")),
        "model_slug": conv.get("model_slug"),
        "last_snippet_repo_file": None, "snippet_repo_files": None})
    if provenance and repo and labeled:
        row["last_snippet_repo_file"] = find_snippet_file(repo, labeled[-1]["code"])
        files = []
        for b in labeled:
            u = find_snippet_file(repo, b["code"])
            if u and u not in files:
                files.append(u)
        row["snippet_repo_files"] = " | ".join(files) if files else None
    return row, langs


def write_rich_csv(rows, path):
    import csv
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([h for _, h in RICH_COLUMNS])
        for r in rows:
            w.writerow([r.get(k) for k, _ in RICH_COLUMNS])


# ---- ONE-PER-SNIPPET level (prompt -> block pairs) --------------------
BLOCK_COLUMNS = [
    ("chat_link", "link chat"),
    ("provider", "provider"),
    ("repo_name", "repo name"),
    ("repo_link", "link repo"),
    ("artifact_type", "metadata type"),
    ("artifact_text", "metadata text"),
    ("user_profile_link", "user profile link"),
    ("user_type", "user type"),
    ("snippet_index", "snippet index in the conversation"),
    ("prompt", "prompt that produced the snippet"),
    ("code", "snippet (code)"),
    ("language", "language"),
    ("language_declared", "language (fence label)"),
    ("language_detected", "language recognized from the snippet"),
    ("n_lines", "lines of code"),
    ("is_valid", "valid snippet"),
    ("snippet_repo_file", "repo file of the snippet (link)")]


def write_block_csv(rows, path):
    import csv
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([h for _, h in BLOCK_COLUMNS])
        for r in rows:
            w.writerow([r.get(k) for k, _ in BLOCK_COLUMNS])


def build_blocks(out, include_all=False, provenance=False, by_language=False):
    """(Re)builds the one-per-snippet dataset from the turns saved in
    <out>.turns.jsonl. Full rebuild: reads ALL the collected
    conversations (even across different runs), so there is no need to redo the fetch.
    By default it selects like build_rich_row: declared label AND valid."""
    args_all = include_all
    tp = Path(f"{out}.turns.jsonl")
    if not tp.exists():
        print(f"Missing {tp}: run the collection first.")
        return 0
    rows, seen = [], set()
    bad = 0
    for line in tp.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            bad += 1
            continue
        if not isinstance(rec, dict):
            bad += 1
            continue
        url = rec.get("chat_link")
        if not url or url in seen:
            continue
        seen.add(url)
        art = rec.get("art") or {}
        provider = rec.get("provider")
        turns = rec.get("turns") or []
        repo = art.get("repo")
        login = art.get("author")
        source = art.get("source")
        upl = art.get("user_profile_link") or profile_link(login, source)
        ut = art.get("user_type") or user_type(login, source)
        idx = 0
        for b in extract_code_blocks(turns):
            # Gemini has no native markdown fences: if the declared label is missing
            # we rely on the RECOGNIZED language (same algorithm as
            # ChatGPT), so that an unread header does not lose the snippet.
            # ChatGPT/Claude stay unchanged: they require the fence label.
            eff_lang = b["language_declared"] or (
                b["language_detected"] if provider == "gemini" else None)
            valid = looks_like_code(b["code"], eff_lang)
            labeled = bool(eff_lang) and valid
            if not args_all and not labeled:
                continue
            idx += 1
            rows.append({
                "chat_link": url, "provider": provider,
                "repo_name": repo,
                "repo_link": f"https://github.com/{repo}" if repo else None,
                "artifact_type": source, "artifact_text": art.get("artifact_text"),
                "user_profile_link": upl, "user_type": ut,
                "snippet_index": idx, "prompt": b["prompt"], "code": b["code"],
                "language": b["language_declared"] or b["language_detected"],
                "language_declared": b["language_declared"],
                "language_detected": b["language_detected"],
                "n_lines": b["code"].count("\n") + 1, "is_valid": valid,
                "snippet_repo_file": (find_snippet_file(repo, b["code"])
                                      if provenance else None)})
    with open(f"{out}.blocks.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    write_block_csv(rows, f"{out}.blocks.csv")

    # --- statistics per language (snippets and conversations) ---
    snip_lang = Counter(r["language"] for r in rows if r["language"])
    by_conv = {}
    for r in rows:
        by_conv.setdefault(r["chat_link"], set()).add(r["language"])
    conv_lang = Counter()
    for langs in by_conv.values():
        for lg in langs:
            if lg:
                conv_lang[lg] += 1
    stats = {
        "n_snippets": len(rows),
        "n_conversations": len(by_conv),
        "n_languages": len(snip_lang),
        "snippets_per_language": dict(snip_lang.most_common()),
        "conversations_per_language": dict(conv_lang.most_common()),
    }
    Path(f"{out}.blocks.stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2))

    # --- optional split: one file per language ---
    if by_language:
        d = Path(f"{out}_by_language")
        d.mkdir(exist_ok=True)
        groups = {}
        for r in rows:
            groups.setdefault(r["language"] or "unknown", []).append(r)
        for lg, grp in groups.items():
            safe = re.sub(r"[^a-z0-9]+", "_", lg.lower()).strip("_") or "unknown"
            with open(d / f"{safe}.jsonl", "w", encoding="utf-8") as f:
                for r in grp:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            write_block_csv(grp, str(d / f"{safe}.csv"))

    print(f"\n[per-block] {len(rows)} snippet"
          + (" (all blocks)" if args_all else " validated with label")
          + f" from {len(by_conv)} conversations -> {out}.blocks.jsonl / .csv")
    if bad:
        print(f"[per-block] WARNING: {bad} rows out of {out}.turns.jsonl "
              f"ignored because corrupted")
    print(f"[per-block] {len(snip_lang)} languages -> {out}.blocks.stats.json")
    if snip_lang:
        print("[per-block] snippets per language (top 15):")
        for lg, n in snip_lang.most_common(15):
            print(f"    {n:>6}  {lg}")
    if by_language:
        print(f"[per-block] split per language -> {out}_by_language/ "
              f"({len(snip_lang)} files)")
    return len(rows)


# ==========================================================================
# ---- PARSER CLAUDE (JSON chat_snapshots) ---------------------------------
# ==========================================================================
def fetch_claude_http(share_url, session=None):
    import requests
    # UUID from the path alone: any ?query or #fragment would break the API (400)
    uuid = urllib.parse.urlsplit(share_url).path.rstrip("/").split("/")[-1]
    api = (f"https://claude.ai/api/chat_snapshots/{uuid}"
           f"?rendering_mode=messages&render_all_tools=true")
    sess = session or requests.Session()
    r = sess.get(api, headers={"User-Agent": UA, "Accept": "application/json"},
                 timeout=30)
    r.raise_for_status()
    return r.json()


def claude_turns(data):
    """Turns {role,text} in markdown; Claude already has the code in the fences."""
    turns = []
    for m in data.get("chat_messages", []):
        role = "user" if m.get("sender") == "human" else "assistant"
        parts = [b["text"] for b in m.get("content", [])
                 if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
        text = "\n".join(parts).strip() or (m.get("text") or "").strip()
        turns.append({"role": role, "text": text})
    return turns


# ==========================================================================
# ---- GEMINI PARSER (rendered DOM -> markdown with fences) ---------------
# ==========================================================================
_GEMINI_USER_LABEL = re.compile(r"^\s*You said\s+", re.I)


def _clean(s):
    return re.sub(r"[ \t]*\n[ \t]*", "\n", re.sub(r"[ \t]+", " ", s or "")).strip()


def _light(s):
    return re.sub(r"\n{3,}", "\n\n", s or "").strip()


def _gemini_user_text(el):
    return _GEMINI_USER_LABEL.sub("", _clean(el.get_text(" ")))


def _codeblock_text(cb):
    pre = cb.find("pre")
    if pre is not None:
        code = pre.get_text("")
    else:
        code_el = cb.find("code") or cb
        code = code_el.get_text("\n")
    lang = ""
    dec = cb.find(class_=re.compile(r"(decoration|language|lang)", re.I))
    if dec:
        toks = _clean(dec.get_text(" ")).split()
        lang = toks[0].lower() if toks else ""
    return code, lang


def _gemini_response_markdown(container):
    from bs4 import BeautifulSoup, NavigableString
    cont = BeautifulSoup(str(container), "lxml")
    for cb in cont.find_all("code-block"):
        code, lang = _codeblock_text(cb)
        fence = f"\n```{lang}\n{code.rstrip(chr(10))}\n```\n"
        cb.replace_with(NavigableString(fence))
    return _light(cont.get_text("\n"))


def gemini_turns(html):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    turns = []
    viewers = soup.find_all("share-turn-viewer")
    if viewers:
        for v in viewers:
            uq = v.find(["user-query-content", "user-query"])
            if uq:
                turns.append({"role": "user", "text": _gemini_user_text(uq)})
            rc = v.find(["response-container", "response-element",
                         "structured-content-container"])
            if rc:
                turns.append({"role": "assistant",
                              "text": _gemini_response_markdown(rc)})
    else:
        for el in soup.find_all(["user-query", "response-container"]):
            if el.name == "user-query":
                turns.append({"role": "user", "text": _gemini_user_text(el)})
            else:
                turns.append({"role": "assistant",
                              "text": _gemini_response_markdown(el)})
    return turns


async def _scroll_html(page, url, max_scroll):
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_selector("user-query, share-turn-viewer", timeout=25000)
    except Exception:
        pass
    prev = -1
    for _ in range(max_scroll):
        c = await page.locator("user-query").count()
        await page.mouse.wheel(0, 25000)
        await page.wait_for_timeout(700)
        if c > 0 and c == prev:
            break
        prev = c
    await page.wait_for_timeout(1200)
    return await page.content()


# ==========================================================================
# ---- TARGET READING (.json snapshot OR rich_dataset.jsonl) ------------
# ==========================================================================
def load_targets(path):
    txt = path.read_text(encoding="utf-8", errors="ignore")
    targets, seen = [], set()

    snap = None
    try:
        obj = json.loads(txt)
        if isinstance(obj, dict) and "links" in obj:
            snap = obj
    except Exception:
        snap = None

    if snap:  # native snapshot with referenced_by
        for rec in snap.get("links", []):
            url = rec.get("share_url")
            if not url or url in seen or provider_of(url) not in ("claude", "gemini"):
                continue
            seen.add(url)
            ref = (rec.get("referenced_by") or [{}])[0]
            targets.append((provider_of(url), url, {
                "repo": ref.get("repo"), "author": ref.get("author"),
                "source": ref.get("source"),
                "artifact_text": ref.get("artifact_text"),
                "artifact_url": ref.get("artifact_url")}))
        return targets

    # rich_dataset.jsonl (flat rows): reuses the metadata already present
    for line in txt.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        url = row.get("chat_link")
        if not url:
            m = SHARE_RE.search(line)
            url = m.group(0) if m else None
        if not url or url in seen or provider_of(url) not in ("claude", "gemini"):
            continue
        seen.add(url)
        login = None
        m = re.search(r"github\.com/([^/]+)/?$", row.get("user_profile_link") or "")
        if m:
            login = m.group(1)
        targets.append((provider_of(url), url, {
            "repo": row.get("repo_name"), "author": login,
            "source": row.get("artifact_type"),
            "artifact_text": row.get("artifact_text"),
            "user_type": row.get("user_type"),
            "user_profile_link": row.get("user_profile_link")}))
    return targets


# ==========================================================================
# ---- OUTPUT RESUMABLE -----------------------------------------------------
# ==========================================================================
class Sink:
    def __init__(self, out, fresh):
        self.out = out
        mode = "w" if fresh else "a"
        self.lock = threading.Lock()
        self.f_keep = open(f"{out}.jsonl", mode, encoding="utf-8")
        self.f_rej = open(f"{out}.rejected.jsonl", mode, encoding="utf-8")
        self.f_err = open(f"{out}.errors.jsonl", mode, encoding="utf-8")
        self.f_turns = open(f"{out}.turns.jsonl", mode, encoding="utf-8")
        self.c = Counter()

    def _w(self, f, obj):
        with self.lock:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
            f.flush()

    def kept(self, row):
        self.c["kept"] += 1
        self._w(self.f_keep, row)

    def turns(self, url, provider, art, turns):
        # raw turns: basis to (re)build the one-per-snippet dataset
        self._w(self.f_turns, {"chat_link": url, "provider": provider,
                               "art": art, "turns": turns})

    def rejected(self, url, provider, reason):
        self.c[f"rej_{reason}"] += 1
        self._w(self.f_rej, {"chat_link": url, "provider": provider, "reason": reason})

    def error(self, url, provider, err):
        self.c["error"] += 1
        self._w(self.f_err, {"chat_link": url, "provider": provider, "error": err})

    def close(self):
        for f in (self.f_keep, self.f_rej, self.f_err, self.f_turns):
            f.close()


def load_done(out):
    done = set()
    for name in (f"{out}.jsonl", f"{out}.rejected.jsonl"):
        p = Path(name)
        if p.exists():
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                try:
                    done.add(json.loads(line)["chat_link"])
                except Exception:
                    pass
    return done


# ==========================================================================
# ---- PROCESSING + PHASES --------------------------------------------------
# ==========================================================================
def process(turns, url, provider, art, args, sink):
    if not turns:
        sink.rejected(url, provider, "empty")
        return
    if not args.any_language and not is_english(turns):
        sink.rejected(url, provider, "not_english")
        return
    conv = {"share_url": url, "turns": turns, "model_slug": None}
    row, _ = build_rich_row(conv, art, provenance=args.provenance)
    if not row.get("n_total_messages"):
        sink.rejected(url, provider, "empty")
        return
    sink.kept(row)
    sink.turns(url, provider, art, turns)


def run_claude(targets, args, sink):
    tl = threading.local()

    def sess():
        import requests
        if not getattr(tl, "s", None):
            tl.s = requests.Session()
        return tl.s

    total = len(targets)
    done = [0]

    def work(t):
        _, url, art = t
        try:
            process(claude_turns(fetch_claude_http(url, sess())),
                    url, "claude", art, args, sink)
        except Exception as e:
            sink.error(url, "claude", repr(e))
        done[0] += 1
        if done[0] % 50 == 0 or done[0] == total:
            print(f"  claude {done[0]}/{total} (kept {sink.c['kept']}, "
                  f"err {sink.c['error']})")

    print(f"[claude] {total} link, {args.claude_workers} thread")
    with ThreadPoolExecutor(max_workers=args.claude_workers) as ex:
        list(ex.map(work, targets))


async def run_gemini(targets, args, sink):
    from playwright.async_api import async_playwright
    total = len(targets)
    done = [0]
    sem = asyncio.Semaphore(args.gemini_concurrency)
    print(f"[gemini] {total} links, concurrency {args.gemini_concurrency}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA)

        async def work(t):
            _, url, art = t
            async with sem:
                page = await ctx.new_page()
                try:
                    html = await _scroll_html(page, url, args.gemini_scrolls)
                    process(gemini_turns(html), url, "gemini", art, args, sink)
                except Exception as e:
                    sink.error(url, "gemini", repr(e))
                finally:
                    await page.close()
            done[0] += 1
            if done[0] % 25 == 0 or done[0] == total:
                print(f"  gemini {done[0]}/{total} (kept {sink.c['kept']}, "
                      f"err {sink.c['error']})")

        await asyncio.gather(*(work(t) for t in targets))
        await browser.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-json", default="rich_dataset.jsonl", dest="src")
    ap.add_argument("--out", default="cg_dataset")
    ap.add_argument("--providers", default="claude,gemini")
    ap.add_argument("--limit", type=int, default=0, help="0 = all")
    ap.add_argument("--any-language", action="store_true",
                    help="disable the English filter (default: active)")
    ap.add_argument("--provenance", action="store_true",
                    help="locate the snippets in the repo files (code search, slow)")
    ap.add_argument("--per-block", action="store_true",
                    help="at the end of collection also generate the one-per-snippet dataset")
    ap.add_argument("--blocks-only", action="store_true",
                    help="does NOT collect: only regenerates the blocks from the already saved turns")
    ap.add_argument("--all-blocks", action="store_true",
                    help="per-block: also include the NOT valid snippets")
    ap.add_argument("--by-language", action="store_true",
                    help="per-block: also write one file for each language")
    ap.add_argument("--lookup-users", action="store_true",
                    help="query the GitHub API for user_type (needs GITHUB_TOKEN)")
    ap.add_argument("--fresh", action="store_true",
                    help="start from scratch (truncates the outputs). Default: resume")
    ap.add_argument("--claude-workers", type=int, default=8)
    ap.add_argument("--gemini-concurrency", type=int, default=3)
    ap.add_argument("--gemini-scrolls", type=int, default=30)
    args = ap.parse_args()

    global GH_LOOKUP
    GH_LOOKUP = args.lookup_users or args.provenance

    src = Path(args.src)

    # no-collection mode: (re)build only the blocks from the saved turns
    if args.blocks_only:
        build_blocks(args.out, include_all=args.all_blocks,
                     provenance=args.provenance, by_language=args.by_language)
        return

    if not src.exists():
        sys.exit(f"File not found: {src.resolve()}")

    wanted = {s.strip() for s in args.providers.split(",") if s.strip()}
    done = set() if args.fresh else load_done(args.out)

    all_t = load_targets(src)
    targets = [t for t in all_t if t[0] in wanted and t[1] not in done]
    if args.limit:
        targets = targets[: args.limit]

    n_cl = sum(1 for t in targets if t[0] == "claude")
    n_ge = sum(1 for t in targets if t[0] == "gemini")
    print(f"\nC/G links in the json: {len(all_t)} | already done: {len(done)} | "
          f"da fare: {len(targets)} (claude {n_cl}, gemini {n_ge})\n")
    if not targets:
        print("Nothing to do. (--fresh to start over)")
        return

    sink = Sink(args.out, args.fresh)
    try:
        if n_cl:
            run_claude([t for t in targets if t[0] == "claude"], args, sink)
        if n_ge:
            asyncio.run(run_gemini([t for t in targets if t[0] == "gemini"],
                                   args, sink))
    finally:
        sink.close()

    rows = []
    for line in Path(f"{args.out}.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    write_rich_csv(rows, f"{args.out}.csv")

    print("\n===== funnel =====")
    print(f"  to do in this run : {len(targets)}")
    print(f"  kept (English)        : {sink.c['kept']}")
    print(f"  discarded non-English  : {sink.c['rej_not_english']}")
    print(f"  discarded empty       : {sink.c['rej_empty']}")
    print(f"  errors (retryable)    : {sink.c['error']}")
    print(f"\nOutput: {args.out}.jsonl / {args.out}.csv  (schema RICH_COLUMNS)")

    if args.per_block:
        build_blocks(args.out, include_all=args.all_blocks,
                     provenance=args.provenance, by_language=args.by_language)


if __name__ == "__main__":
    main()