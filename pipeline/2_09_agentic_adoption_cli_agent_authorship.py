#!/usr/bin/env python
"""
Adoption for the CLI-agent sessions (Claude Code) of the vibe-coding dataset:
AUTHORSHIP method (bonus, complementary to 2_07_agentic_adoption_cli_agent_direct.py) -
searches the repo's commit history (already downloaded by 1_scrape_pool.py into
data/commits_history/{repo_id}.json, no API call for this
step) for a commit CO-AUTHORED by Claude ("Co-Authored-By: Claude ..."),
in the time window after the session - same logic as
2_10_agentic_adoption_ide_direct.py for Copilot, but applied to Claude.

Why it is needed IN ADDITION to the commit message matching already done by
2_07_agentic_adoption_cli_agent_direct.py: that method works ONLY if the agent
invoked `git commit -m "..."` INSIDE THE SESSION ITSELF. But a session
may not write/commit anything directly (e.g. a REVIEW-only session -
"Review this change for security vulnerabilities", only Read/Grep tools)
while the code it is reviewing IS STILL the work of Claude Code, committed
in a previous or different session - the "Co-Authored-By: Claude"
trailer in the commit confirms it independently of the content of THIS
specific transcript (concrete case found by the user: gregce/deadreckon,
review-only session, commit ccb0e06 with "Co-Authored-By: Claude Fable 5
<noreply@anthropic.com>", never seen by 2_07_agentic_adoption_cli_agent_direct.py because
there is no `git commit` in the transcript).

Sessions already verified 'yes' by 2_07_agentic_adoption_cli_agent_direct.py are
skipped (redundant).

For each match: verifies that the session's code appears in the FULL
content of each file touched AT THE FOUND COMMIT (same logic
already corrected in 2_07_agentic_adoption_cli_agent_direct.py - not only the lines
added in the diff).

Writes out_vibe_coding/adoption_cli_agent_authorship.csv.
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
camp = importlib.import_module("lib_cli_agent_model_parser")  # clean code extraction, is_chat_transcript
from lib_github_cache import DiskCache

csv.field_size_limit(50_000_000)

SCRAPER_DIR = os.path.join(SCRIPT_DIR, "1_07_agentic_scraper", "data")
MARKDOWNS_CLI_DIR = os.path.join(SCRAPER_DIR, "markdowns_cli")
COMMITS_HISTORY_DIR = os.path.join(SCRAPER_DIR, "commits_history")
OUT_DIR = os.path.join(SCRIPT_DIR, "out_vibe_coding")
DIRECT_PATH = os.path.join(OUT_DIR, "adoption_cli_agent_direct.csv")
OUT_PATH = os.path.join(OUT_DIR, "adoption_cli_agent_authorship.csv")

WINDOW_DAYS = 30  # how far ahead in time to look for a co-authored commit

_TRAILER_CLAUDE_RE = re.compile(r"co-authored-by:\s*claude\b", re.I)


def e_commit_claude(commit):
    msg = (commit.get("commit") or {}).get("message") or ""
    return bool(_TRAILER_CLAUDE_RE.search(msg))


def load_repo_per_sha():
    with open(os.path.join(SCRAPER_DIR, "searches.json"), encoding="utf-8") as f:
        items = json.load(f)
    out = {}
    for it in items:
        repo = it.get("repository") or {}
        out[it["sha"]] = (repo.get("id"), repo.get("full_name"))
    return out


def load_already_verified():
    """Sessions already confirmed 'yes' by 2_07_agentic_adoption_cli_agent_direct.py -
    for these authorship is redundant, skip."""
    verificate = set()
    if not os.path.exists(DIRECT_PATH):
        return verificate
    with open(DIRECT_PATH, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r["code_verified"] == "yes":
                verificate.add(r["sha"])
    return verificate


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


def find_commit_claude_nearby(history, timestamp_session):
    """The Claude co-authored commit closest to (but not before) the
    session timestamp, within WINDOW_DAYS - see
    2_10_agentic_adoption_ide_direct.py::find_commit_bot_nearby, same logic."""
    ts = _parse_data(timestamp_session)
    if not ts:
        return None
    candidates = []
    for c in history:
        if not e_commit_claude(c):
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
    """As in the CLI-agent adoption examples browser (not part of this package) / 2_10_agentic_adoption_ide_direct.py:
    FULL content of each file touched by the commit, not just the lines
    added in the diff."""
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
        if camp.is_chat_transcript(filename):
            continue
        if fmeta.get("status") == "removed":
            continue
        content = track.fetch_file_at_ref(repo, filename, sha, cache)
        if content:
            out.append(content)
    return out


def code_for_verification(text_raw):
    if camp.detect_format(text_raw) != "specstory":
        return []
    pairs = camp.extract_snippet_clean(text_raw) + camp.extract_reads_clean(text_raw)
    return [c["response_code"] for c in pairs]


def main():
    print("Loading repo per session, already verified sessions (direct), session timestamps...")
    repo_per_sha = load_repo_per_sha()
    already_verified = load_already_verified()
    sessions_cli = {}
    with open(os.path.join(OUT_DIR, "conversations.jsonl"), encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            if c.get("is_cli_agent") == "yes":
                sessions_cli[c["sha"]] = c.get("timestamp")

    cache = DiskCache(os.path.join(OUT_DIR, "github_cache_cli_agent.pkl"))
    files = [fn for fn in os.listdir(MARKDOWNS_CLI_DIR) if fn.endswith(".md") and fn[:-3] in sessions_cli]
    to_process = [fn for fn in files if fn[:-3] not in already_verified]
    print(f"Total CLI-agent sessions: {len(files)}, already verified (direct, skipped): {len(already_verified)}, "
          f"to process: {len(to_process)}")

    rows_out = []
    n_with_claude = n_verified = 0
    history_cache = {}

    for i, fn in enumerate(to_process, 1):
        if i % 300 == 0:
            cache.save()
            print(f"  [{i}/{len(to_process)}]  with_claude={n_with_claude} verified={n_verified}")
        sha = fn[:-3]
        timestamp_session = sessions_cli.get(sha)
        repo_id, repo_full_name = repo_per_sha.get(sha, (None, None))
        if not repo_id or not repo_full_name or not timestamp_session:
            continue

        if repo_id not in history_cache:
            history_cache[repo_id] = load_commit_history(repo_id)
        history = history_cache[repo_id]

        commit_claude = find_commit_claude_nearby(history, timestamp_session)
        if not commit_claude:
            continue
        n_with_claude += 1
        commit_sha = commit_claude["sha"]
        commit_date = (commit_claude.get("commit") or {}).get("author", {}).get("date", "")

        text = open(os.path.join(MARKDOWNS_CLI_DIR, fn), encoding="utf-8").read()
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

    print(f"\nSessions with a nearby Claude co-authored commit: {n_with_claude}")
    print(f"Verified (code confirmed in the commit): {n_verified}")
    print(f"Written {OUT_PATH}")


if __name__ == "__main__":
    main()
