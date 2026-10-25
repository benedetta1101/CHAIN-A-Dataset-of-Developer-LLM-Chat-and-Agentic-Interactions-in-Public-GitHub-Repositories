#!/usr/bin/env python
"""
Fallback for the CLI-agent sessions NOT already verified by
2_07_agentic_adoption_cli_agent_direct.py (no `git commit` in the transcript, or
found but without confirmation on the code): uses the Edit/Write tool-uses, which,
although they do not show the diff, report the ABSOLUTE PATH of the touched file
on the developer's host:
  "The file /Users/.../repo/pkg/foo.go has been updated successfully"
  "File created successfully at: /Users/.../repo/docs/x.md"

The path is local, it must be converted into a REPO-RELATIVE path by finding the
segment that matches the repo name (heuristic: last component
of 'owner/repo') and taking everything that follows - it fails if the repo
is cloned in a folder with a different name (~40% success, verified
empirically), but when it works it removes the need for the expensive
whole-repo search of the out_ago2026 pipeline: we already know EXACTLY
which file to check, the agent itself told us.

For each successfully converted file, downloads the CURRENT content (HEAD
of the default branch) and checks whether the session's code appears there
(snippet_in_text, same logic as the rest of the pipeline). The
code used for the verification is NOT the one already in
out_vibe_coding/snippet.csv (for specstory sessions it also includes
output of Bash/Read/Grep tools pasted in a fence, e.g. `git status` -
false positives discovered in manual review): here a dedicated clean
extraction is used (lib_cli_agent_model_parser.extract_snippet_clean), only for
this script - snippet.csv stays unchanged for the other analyses.
Honest limit: it checks only the state of TODAY, not a specific commit -
if the file was modified again afterwards, it may no longer find the
code even though it was adopted and then changed.

Writes out_vibe_coding/adoption_cli_agent_fallback.csv.
"""
import csv
import importlib
import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
track = importlib.import_module("2_01_chat_track_snippets")
step9 = importlib.import_module("2_03_chat_pr_commits_recheck")  # snippet_in_text
camp = importlib.import_module("lib_cli_agent_model_parser")  # clean code extraction
from lib_github_cache import DiskCache

csv.field_size_limit(50_000_000)

SCRAPER_DIR = os.path.join(SCRIPT_DIR, "1_07_agentic_scraper", "data")
MARKDOWNS_CLI_DIR = os.path.join(SCRAPER_DIR, "markdowns_cli")
OUT_DIR = os.path.join(SCRIPT_DIR, "out_vibe_coding")
DIRECT_PATH = os.path.join(OUT_DIR, "adoption_cli_agent_direct.csv")
OUT_PATH = os.path.join(OUT_DIR, "adoption_cli_agent_fallback.csv")

PATTERNS = [
    re.compile(r"The file (\S[^`\n]*?) has been updated successfully"),
    re.compile(r"File created successfully at: (\S[^`\n]*)"),
]


def path_relative(path_absolute, repo_full_name):
    repo_name = repo_full_name.split("/")[-1].lower()
    parts = path_absolute.rstrip("/").split("/")
    for i, p in enumerate(parts):
        if p.lower() == repo_name:
            return "/".join(parts[i + 1:])
    return None


def extract_path_touched(text, repo_full_name):
    path_seen = set()
    for rx in PATTERNS:
        for m in rx.finditer(text):
            rel = path_relative(m.group(1).strip(), repo_full_name)
            if rel:
                path_seen.add(rel)
    return path_seen


def load_repo_per_sha():
    with open(os.path.join(SCRAPER_DIR, "searches.json"), encoding="utf-8") as f:
        items = json.load(f)
    return {it["sha"]: (it["repository"].get("full_name"))
            for it in items}


def load_already_verified():
    """Sessions already confirmed 'yes' by 2_07_agentic_adoption_cli_agent_direct.py -
    for these the fallback is redundant, skip."""
    verificate = set()
    if not os.path.exists(DIRECT_PATH):
        return verificate
    with open(DIRECT_PATH, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r["code_verified"] == "yes":
                verificate.add(r["sha"])
    return verificate


def load_snippet_per_link():
    path = os.path.join(OUT_DIR, "snippet.csv")
    of = {}
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            of.setdefault(r["conversation_url"], []).append(r["response_code"])
    return of


def code_for_verification(text_raw, link, snippet_per_link):
    """See 2_07_agentic_adoption_cli_agent_direct.py::code_for_verification - same
    logic (no silent fallback to the dirty data for specstory
    sessions without clean candidates), duplicated here to keep the two
    scripts independent."""
    if camp.detect_format(text_raw) == "specstory":
        pairs = camp.extract_snippet_clean(text_raw) + camp.extract_reads_clean(text_raw)
        return [c["response_code"] for c in pairs]
    return snippet_per_link.get(link, []) if link else []


def fetch_file(repo, path, cache):
    key = ("content_file_head", repo, path)
    if key in cache:
        return cache[key]
    import base64
    meta = track._request(
        f"https://api.github.com/repos/{repo}/contents/{path}", allow_404=True)
    text = None
    if meta and "content" in meta:
        try:
            text = base64.b64decode(meta["content"]).decode("utf-8", "ignore")
        except Exception:
            text = None
    cache[key] = text
    return text


def main():
    print("Loading repo per session, already verified sessions, snippets, links...")
    repo_per_sha = load_repo_per_sha()
    already_verified = load_already_verified()
    snippet_per_link = load_snippet_per_link()

    link_per_sha = {}
    with open(os.path.join(OUT_DIR, "conversations.jsonl"), encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            if c.get("is_cli_agent") == "yes":
                link_per_sha[c["sha"]] = c["conversation_url"]

    cache = DiskCache(os.path.join(OUT_DIR, "github_cache_cli_agent.pkl"))

    files = [fn for fn in os.listdir(MARKDOWNS_CLI_DIR) if fn.endswith(".md")]
    to_process = [fn for fn in files if fn[:-3] not in already_verified]
    print(f"Total CLI-agent sessions: {len(files)}, already verified (skipped): {len(already_verified)}, "
          f"to process: {len(to_process)}")

    rows_out = []
    n_with_path = n_with_path_converted = n_verificate = 0

    for i, fn in enumerate(to_process, 1):
        if i % 300 == 0:
            cache.save()
            print(f"  [{i}/{len(to_process)}]  with_path={n_with_path} converted={n_with_path_converted} "
                  f"verificate={n_verificate}")
        sha = fn[:-3]
        repo = repo_per_sha.get(sha)
        if not repo:
            continue
        text = open(os.path.join(MARKDOWNS_CLI_DIR, fn), encoding="utf-8").read()
        path_touched = extract_path_touched(text, repo)
        if not path_touched:
            continue
        n_with_path += 1
        n_with_path_converted += 1

        link = link_per_sha.get(sha)
        snippet_session = code_for_verification(text, link, snippet_per_link)
        if not snippet_session:
            for p in path_touched:
                rows_out.append({"sha": sha, "repo": repo, "path": p,
                                   "found": "n/a_no_session_snippet"})
            continue

        session_verified = False
        for p in sorted(path_touched):
            if camp.is_chat_transcript(p):
                rows_out.append({"sha": sha, "repo": repo, "path": p,
                                   "found": "n.d._transcript_chat"})
                continue  # SpecStory export - circular match, it is not real code
            content = fetch_file(repo, p, cache)
            found = "no"
            if content:
                if any(step9.snippet_in_text(cod, content) for cod in snippet_session):
                    found = "yes"
                    session_verified = True
            else:
                found = "file_not_found_today"
            rows_out.append({"sha": sha, "repo": repo, "path": p, "found": found})
        if session_verified:
            n_verificate += 1

    cache.save()
    os.makedirs(OUT_DIR, exist_ok=True)
    if rows_out:
        with open(OUT_PATH, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
            w.writeheader()
            w.writerows(rows_out)

    print(f"\nSessions with at least one converted Edit/Write path: {n_with_path_converted}")
    print(f"Verified sessions (code found in at least one touched file): {n_verificate}")
    print(f"Written {OUT_PATH}")


if __name__ == "__main__":
    main()
