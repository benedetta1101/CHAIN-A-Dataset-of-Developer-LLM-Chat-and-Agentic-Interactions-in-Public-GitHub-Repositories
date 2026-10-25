#!/usr/bin/env python
"""
Adoption for the IDE sessions (Cursor/Copilot) of the vibe-coding dataset:
PATH-based method, BASE coverage for (almost) all sessions -
analogous to 2_08_agentic_adoption_cli_agent_fallback.py, but here it is the primary method
(not a residual fallback) because IDE sessions almost never have
a strong direct in-session signal (no `git commit` in the transcript
- the developer commits outside the chat, not the assistant).

For each session:
  1. Extracts the PATHS touched by any read/edit tool
     (lib_ide_session_parser.extract_path_touched_ide - the Cursor/Copilot
     vocabulary is much more heterogeneous than Claude Code's, see that module).
  2. Converts the local absolute path into a repo-relative path (same
     heuristic as 2_08_agentic_adoption_cli_agent_fallback.py::path_relative, extended
     to also handle Windows separators '\\').
  3. Downloads the content of each converted path AT THE COMMIT embedded in the
     conversation_url itself (`/blob/{sha}/...` - the commit in which
     the transcript file existed at scraping time, a much more precise
     temporal anchor than the current HEAD, which may have changed a lot since),
     falling back to HEAD if the file no longer exists at that commit
     (renamed/moved). Checks whether the session's clean code (read_file_v2
     reads + search_replace/apply_patch diffs - NEVER the raw content of
     read_file, risk of reading command output instead of real code, see
     lib_ide_session_parser.py) appears in it.

Sessions already verified 'yes' by 2_10_agentic_adoption_ide_direct.py (authorship)
are skipped (redundant).

Writes out_vibe_coding/adoption_ide_fallback.csv.
"""
import csv
import importlib
import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
track = importlib.import_module("2_01_chat_track_snippets")  # _request
step9 = importlib.import_module("2_03_chat_pr_commits_recheck")  # snippet_in_text
camp = importlib.import_module("lib_cli_agent_model_parser")  # is_chat_transcript
ide = importlib.import_module("lib_ide_session_parser")
from lib_github_cache import DiskCache

csv.field_size_limit(50_000_000)

SCRAPER_DIR = os.path.join(SCRIPT_DIR, "1_07_agentic_scraper", "data")
MARKDOWNS_DIR = os.path.join(SCRAPER_DIR, "markdowns")
OUT_DIR = os.path.join(SCRIPT_DIR, "out_vibe_coding")
DIRECT_PATH = os.path.join(OUT_DIR, "adoption_ide_direct.csv")
OUT_PATH = os.path.join(OUT_DIR, "adoption_ide_fallback.csv")


def path_relative(path_absolute, repo_full_name):
    """Like 2_08_agentic_adoption_cli_agent_fallback.py::path_relative, but tolerant of
    both '/' (Cursor on Mac/Linux) and '\\' (Copilot on Windows, very
    common in this dataset - 'd:\\project\\...' seen empirically)."""
    repo_name = repo_full_name.split("/")[-1].lower()
    parts = re.split(r"[/\\]", path_absolute.rstrip("/\\"))
    for i, p in enumerate(parts):
        if p.lower() == repo_name:
            return "/".join(parts[i + 1:])
    return None


def load_repo_per_sha():
    with open(os.path.join(SCRAPER_DIR, "searches.json"), encoding="utf-8") as f:
        items = json.load(f)
    return {it["sha"]: it["repository"].get("full_name") for it in items}


def load_already_verified():
    verificate = set()
    if not os.path.exists(DIRECT_PATH):
        return verificate
    with open(DIRECT_PATH, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r["code_verified"] == "yes":
                verificate.add(r["sha"])
    return verificate


def fetch_file(repo, path, cache):
    key = ("content_file_head", repo, path)
    if key in cache:
        return cache[key]
    import base64
    try:
        meta = track._request(f"https://api.github.com/repos/{repo}/contents/{path}", allow_404=True)
    except RuntimeError:
        # transient network error after all the attempts of
        # 2_01_chat_track_snippets._request - not fatal, a permanent negative
        # outcome is not cached because of a network blip: it is retried on the
        # next run (a single lost row must not interrupt hours
        # of work on the other thousands)
        return None
    text = None
    if meta and "content" in meta:
        try:
            text = base64.b64decode(meta["content"]).decode("utf-8", "ignore")
        except Exception:
            text = None
    cache[key] = text
    return text


_COMMIT_IN_LINK_RE = re.compile(r"/blob/([0-9a-f]{40})/")


def load_commit_link_per_sha():
    """The commit embedded in conversation_url (/blob/{sha}/...) - the
    commit in which the transcript file existed at scraping time,
    a much more precise temporal anchor than the current HEAD."""
    out = {}
    with open(os.path.join(OUT_DIR, "conversations.jsonl"), encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            if c.get("is_cli_agent") != "no":
                continue
            m = _COMMIT_IN_LINK_RE.search(c.get("conversation_url") or "")
            if m:
                out[c["sha"]] = m.group(1)
    return out


def fetch_file_al_commit_o_head(repo, path, commit_ref, cache):
    if commit_ref:
        content = track.fetch_file_at_ref(repo, path, commit_ref, cache)
        if content:
            return content
    return fetch_file(repo, path, cache)


def code_for_verification(text_raw):
    """Clean reads (read_file_v2) + diffs (search_replace/apply_patch,
    new and old format) + prose - NEVER read_file (risk of cached
    command output) nor path-only tools without content."""
    pairs = (ide.extract_reads_ide(text_raw) + ide.extract_diff_ide(text_raw)
              + ide.extract_prose_ide(text_raw))
    return [c["response_code"] for c in pairs]


def main():
    print("Loading repo per session, already verified sessions (authorship), commits from links...")
    repo_per_sha = load_repo_per_sha()
    already_verified = load_already_verified()
    commit_link_per_sha = load_commit_link_per_sha()
    cache = DiskCache(os.path.join(OUT_DIR, "github_cache_ide.pkl"))

    shas_ide = set()
    with open(os.path.join(OUT_DIR, "conversations.jsonl"), encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            if c.get("is_cli_agent") == "no":
                shas_ide.add(c["sha"])

    files = [fn for fn in os.listdir(MARKDOWNS_DIR) if fn.endswith(".md") and fn[:-3] in shas_ide]
    to_process = [fn for fn in files if fn[:-3] not in already_verified]
    print(f"Total IDE sessions: {len(files)}, already verified (authorship, skipped): {len(already_verified)}, "
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
        text = open(os.path.join(MARKDOWNS_DIR, fn), encoding="utf-8").read()
        path_touched_absolute = ide.extract_path_touched_ide(text)
        if not path_touched_absolute:
            continue
        n_with_path += 1

        path_converted = set()
        for p in path_touched_absolute:
            rel = path_relative(p, repo)
            if rel and not camp.is_chat_transcript(rel):
                path_converted.add(rel)
        if not path_converted:
            continue
        n_with_path_converted += 1

        snippet_session = code_for_verification(text)
        if not snippet_session:
            for p in path_converted:
                rows_out.append({"sha": sha, "repo": repo, "path": p,
                                   "found": "n/a_no_session_snippet"})
            continue

        commit_ref = commit_link_per_sha.get(sha)
        session_verified = False
        for p in sorted(path_converted):
            content = fetch_file_al_commit_o_head(repo, p, commit_ref, cache)
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

    print(f"\nSessions with at least one IDE path extracted: {n_with_path}")
    print(f"Sessions with at least one path converted to relative: {n_with_path_converted}")
    print(f"Verified sessions (code found in at least one touched file): {n_verificate}")
    print(f"Written {OUT_PATH}")


if __name__ == "__main__":
    main()
