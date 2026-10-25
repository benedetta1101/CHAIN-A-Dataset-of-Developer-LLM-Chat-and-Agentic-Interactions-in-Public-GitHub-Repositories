#!/usr/bin/env python
"""
Adoption for the IDE sessions (Cursor/Copilot) of the vibe-coding dataset:
DIRECT method via commit message - analogous to
2_07_agentic_adoption_cli_agent_direct.py, NEVER applied before to IDE sessions because
of a wrong assumption (it was thought that Cursor/Copilot did not show git
commands in the transcript, since the developer commits outside the chat).
False: verified empirically (concrete case reported by the
user, jglaspey/eod-agentic-flow - the session literally shows
`git commit -m "Merge Claude Sonnet 4 branch..."` and `git push origin
main` in the transcript, and that commit really exists in the history of the
repo) - the user has access to the terminal inside the IDE too, so can
show the same git commands as the CLI-agent sessions. Sample:
10.1% of the IDE sessions (101/1000) have at least one extractable commit
message - a high-precision signal never exploited so far.

Extracts the `git commit -m "..."` messages from the transcript
(lib_extract_git_commits_from_sessions.py, same function as CLI-agent), searches
for them in the repo's REAL commit history (already downloaded, no API
call for this step), and verifies that the session's clean code
appears in the FULL content of each file touched by the found commit.

Writes out_vibe_coding/adoption_ide_commit_message.csv.
"""
import csv
import importlib
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
extract = importlib.import_module("lib_extract_git_commits_from_sessions")
track = importlib.import_module("2_01_chat_track_snippets")  # fetch_file_at_ref, _request
step9 = importlib.import_module("2_03_chat_pr_commits_recheck")  # snippet_in_text
camp = importlib.import_module("lib_cli_agent_model_parser")  # is_chat_transcript
ide = importlib.import_module("lib_ide_session_parser")
from lib_github_cache import DiskCache

csv.field_size_limit(50_000_000)

SCRAPER_DIR = os.path.join(SCRIPT_DIR, "1_07_agentic_scraper", "data")
MARKDOWNS_IDE_DIR = os.path.join(SCRAPER_DIR, "markdowns")
COMMITS_HISTORY_DIR = os.path.join(SCRAPER_DIR, "commits_history")
OUT_DIR = os.path.join(SCRIPT_DIR, "out_vibe_coding")
OUT_PATH = os.path.join(OUT_DIR, "adoption_ide_commit_message.csv")


def normalize(msg):
    import re
    return re.sub(r"\s+", " ", msg.strip())


def subject(msg):
    return normalize(msg.split("\n", 1)[0])


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


def find_match(msg_extracted, history):
    norm_target = normalize(msg_extracted)
    sub_target = subject(msg_extracted)
    match_subject = None
    for c in history:
        cm = (c.get("commit") or {}).get("message") or ""
        if normalize(cm) == norm_target:
            return c, "full_message"
        if match_subject is None and subject(cm) == sub_target:
            match_subject = c
    if match_subject:
        return match_subject, "subject_only"
    return None, None


def commit_files_full_content(repo, sha, cache):
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
    print("Loading repo per session...")
    repo_per_sha = load_repo_per_sha()
    shas_ide = set()
    with open(os.path.join(OUT_DIR, "conversations.jsonl"), encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            if c.get("is_cli_agent") == "no":
                shas_ide.add(c["sha"])

    cache = DiskCache(os.path.join(OUT_DIR, "github_cache_ide.pkl"))
    files = [fn for fn in os.listdir(MARKDOWNS_IDE_DIR) if fn.endswith(".md") and fn[:-3] in shas_ide]
    print(f"Total IDE sessions: {len(files)}")

    rows_out = []
    n_with_commit = n_matched = n_verified = 0
    history_cache = {}

    for i, fn in enumerate(files, 1):
        if i % 500 == 0:
            cache.save()
            print(f"  [{i}/{len(files)}]  with_commit={n_with_commit} matched={n_matched} verified={n_verified}")
        sha = fn[:-3]
        text = open(os.path.join(MARKDOWNS_IDE_DIR, fn), encoding="utf-8").read()
        messages = extract.extract_messages_commit(text)
        if not messages:
            continue
        n_with_commit += 1

        repo_id, repo_full_name = repo_per_sha.get(sha, (None, None))
        if not repo_id or not repo_full_name:
            for msg in messages:
                rows_out.append({
                    "sha": sha, "repo": repo_full_name or "", "commit_message": msg[:200],
                    "match_outcome": "repo_unknown", "found_commit_sha": "",
                    "commit_date": "", "code_verified": "n/a",
                })
            continue

        if repo_id not in history_cache:
            history_cache[repo_id] = load_commit_history(repo_id)
        history = history_cache[repo_id]

        snippet_session = code_for_verification(text)

        for msg in messages:
            match, type_match = find_match(msg, history)
            if not match:
                rows_out.append({
                    "sha": sha, "repo": repo_full_name, "commit_message": msg[:200],
                    "match_outcome": "no_match_in_history", "found_commit_sha": "",
                    "commit_date": "", "code_verified": "n/a",
                })
                continue
            n_matched += 1
            commit_sha = match["sha"]
            commit_date = (match.get("commit") or {}).get("author", {}).get("date", "")

            code_verified = "n/a_no_session_snippet"
            if snippet_session:
                contents = commit_files_full_content(repo_full_name, commit_sha, cache)
                found = sum(1 for cod in snippet_session
                              if any(step9.snippet_in_text(cod, c) for c in contents))
                code_verified = "yes" if found > 0 else "no"
                if code_verified == "yes":
                    n_verified += 1

            rows_out.append({
                "sha": sha, "repo": repo_full_name, "commit_message": msg[:200],
                "match_outcome": type_match, "found_commit_sha": commit_sha,
                "commit_date": commit_date, "code_verified": code_verified,
            })

    cache.save()
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
        w.writeheader()
        w.writerows(rows_out)

    print(f"\nSessions with at least one commit extracted: {n_with_commit}")
    print(f"Total messages extracted: {len(rows_out)}")
    print(f"Matches found in the commit history: {n_matched}")
    print(f"Verified (code confirmed in the commit): {n_verified}")
    print(f"Written {OUT_PATH}")


if __name__ == "__main__":
    main()
