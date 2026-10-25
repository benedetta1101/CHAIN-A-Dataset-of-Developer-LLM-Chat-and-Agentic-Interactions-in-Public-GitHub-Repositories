#!/usr/bin/env python
"""
Adoption for the IDE sessions (Cursor/Copilot) of the vibe-coding dataset:
AUTHORSHIP method (bonus/priority) - searches the repo's commit history
(already downloaded by 1_scrape_pool.py into data/commits_history/{repo_id}.json,
no API call for this step) for a commit AUTHORED or CO-AUTHORED
by a recognizable AI bot, in the time window after the session.

Verified empirically (see conversation) on 43844 cached commits:
  - Copilot ("coding agent" autonomous mode, DIFFERENT from the
    chat/inline assistant that generates the Cursor/Copilot sessions of this dataset)
    leaves a recognizable bot author ("Copilot") or a trailer
    "Co-authored-by: Copilot..." - used here
  - Cursor leaves NO signature in the commits (it is always the human
    developer who commits) - so this method captures ONLY a minority of the
    sessions (the autonomous-agent Copilot ones), the majority remains
    covered only by 2_12_agentic_adoption_ide_fallback.py (path-based)
  - EXCLUDED: "claude"/"claude opus..." (already covered by the separate
    CLI-agent pipeline), "lovable-dev[bot]" (another AI tool, out of scope),
    "dependabot[bot]" (dependency bot, not AI-coding)

For each match: verifies that the session's code appears in the FULL
content of each file touched AT THE FOUND COMMIT (same logic
already corrected in 2_07_agentic_adoption_cli_agent_direct.py - not only the lines
added in the diff, which would underestimate if part of the code pre-existed).

Writes out_vibe_coding/adoption_ide_direct.csv.
"""
import csv
import datetime
import importlib
import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
track = importlib.import_module("2_01_chat_track_snippets")  # fetch_file_at_ref, _request
step9 = importlib.import_module("2_03_chat_pr_commits_recheck")  # snippet_in_text
camp = importlib.import_module("lib_cli_agent_model_parser")  # is_chat_transcript
ide = importlib.import_module("lib_ide_session_parser")
from lib_github_cache import DiskCache

csv.field_size_limit(50_000_000)

SCRAPER_DIR = os.path.join(SCRIPT_DIR, "1_07_agentic_scraper", "data")
MARKDOWNS_DIR = os.path.join(SCRAPER_DIR, "markdowns")
COMMITS_HISTORY_DIR = os.path.join(SCRAPER_DIR, "commits_history")
OUT_DIR = os.path.join(SCRIPT_DIR, "out_vibe_coding")
OUT_PATH = os.path.join(OUT_DIR, "adoption_ide_direct.csv")

WINDOW_DAYS = 30  # how far ahead in time to look for a bot-signed commit

# recognizable AND relevant AI bots (autonomous Copilot) - other
# tools/non-AI bots or ones already covered elsewhere are explicitly excluded
_BOT_LOGIN_RE = re.compile(r"^copilot$", re.I)
_BOT_TRAILER_RE = re.compile(r"co-authored-by:\s*copilot\b", re.I)


def e_commit_bot_copilot(commit):
    login = ((commit.get("author") or {}).get("login") or "")
    if _BOT_LOGIN_RE.match(login):
        return True
    msg = (commit.get("commit") or {}).get("message") or ""
    return bool(_BOT_TRAILER_RE.search(msg))


def load_repo_per_sha():
    with open(os.path.join(SCRAPER_DIR, "searches.json"), encoding="utf-8") as f:
        items = json.load(f)
    out = {}
    for it in items:
        repo = it.get("repository") or {}
        out[it["sha"]] = (repo.get("id"), repo.get("full_name"))
    return out


def load_commit_history(repo_id):
    path = os.path.join(COMMITS_HISTORY_DIR, f"{repo_id}.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _parse_data(s):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            continue
    return None


def find_commit_bot_nearby(history, timestamp_session):
    """The Copilot-bot commit closest to (but not before) the session
    timestamp, within WINDOW_DAYS - a second case of "almost certain"
    adoption analogous to the git commit message match for
    CLI-agent, but here the signal is WHO made the commit, not WHAT
    the message says (IDE sessions have no git commands in the
    transcript)."""
    ts = _parse_data(timestamp_session)
    if not ts:
        return None
    candidates = []
    for c in history:
        if not e_commit_bot_copilot(c):
            continue
        data_commit = _parse_data((c.get("commit") or {}).get("author", {}).get("date", ""))
        if not data_commit:
            continue
        delta = (data_commit - ts).total_seconds()
        if 0 <= delta <= WINDOW_DAYS * 86400:
            candidates.append((delta, c))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def commit_files_full_content(repo, sha, cache):
    """As in the CLI-agent adoption examples browser (not part of this package): FULL content
    of each file touched by the commit, not just the lines added in the
    diff - the same bug already fixed for CLI-agent would apply here."""
    key = ("commit_detail_full", repo, sha)
    detail = cache.get(key)
    if not detail:
        detail = track._request(f"https://api.github.com/repos/{repo}/commits/{sha}", allow_404=True)
        cache[key] = detail
    if not detail:
        return []
    out = []
    for fmeta in (detail.get("files") or []):
        filename = fmeta.get("filename") or ""
        if camp.is_chat_transcript(filename) or ide.is_internal_path(filename):
            continue
        if fmeta.get("status") == "removed":
            continue
        content = track.fetch_file_at_ref(repo, filename, sha, cache)
        if content:
            out.append(content)
    return out


def code_for_verification(text_raw):
    pairs = (ide.extract_reads_ide(text_raw) + ide.extract_diff_ide(text_raw)
              + ide.extract_prose_ide(text_raw))
    return [c["response_code"] for c in pairs]


def main():
    print("Loading repo per session, IDE session timestamps...")
    repo_per_sha = load_repo_per_sha()
    sessions_ide = {}
    with open(os.path.join(OUT_DIR, "conversations.jsonl"), encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            if c.get("is_cli_agent") == "no":
                sessions_ide[c["sha"]] = c.get("timestamp")

    cache = DiskCache(os.path.join(OUT_DIR, "github_cache_ide.pkl"))
    files = [fn for fn in os.listdir(MARKDOWNS_DIR) if fn.endswith(".md") and fn[:-3] in sessions_ide]
    print(f"Total IDE sessions: {len(files)}")

    rows_out = []
    n_with_bot = n_verified = 0
    history_cache = {}

    for i, fn in enumerate(files, 1):
        if i % 300 == 0:
            cache.save()
            print(f"  [{i}/{len(files)}]  with_bot={n_with_bot} verified={n_verified}")
        sha = fn[:-3]
        timestamp_session = sessions_ide.get(sha)
        repo_id, repo_full_name = repo_per_sha.get(sha, (None, None))
        if not repo_id or not repo_full_name or not timestamp_session:
            continue

        if repo_id not in history_cache:
            history_cache[repo_id] = load_commit_history(repo_id)
        history = history_cache[repo_id]

        commit_bot = find_commit_bot_nearby(history, timestamp_session)
        if not commit_bot:
            continue
        n_with_bot += 1
        commit_sha = commit_bot["sha"]
        commit_date = (commit_bot.get("commit") or {}).get("author", {}).get("date", "")

        text = open(os.path.join(MARKDOWNS_DIR, fn), encoding="utf-8").read()
        snippet_session = code_for_verification(text)

        code_verified = "n/a_no_session_snippet"
        if snippet_session:
            contents = commit_files_full_content(repo_full_name, commit_sha, cache)
            found = sum(1 for cod in snippet_session
                          if any(step9.snippet_in_text(cod, c) for c in contents))
            code_verified = "yes" if found > 0 else "no"
            if code_verified == "yes":
                n_verified += 1

        rows_out.append({
            "sha": sha, "repo": repo_full_name, "found_commit_sha": commit_sha,
            "commit_date": commit_date, "code_verified": code_verified,
        })

    cache.save()
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8", newline="") as f:
        fieldnames = ["sha", "repo", "found_commit_sha", "commit_date", "code_verified"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows_out)

    print(f"\nSessions with a nearby Copilot-bot commit: {n_with_bot}")
    print(f"Verified (code confirmed in the commit): {n_verified}")
    print(f"Written {OUT_PATH}")


if __name__ == "__main__":
    main()
