#!/usr/bin/env python
"""
Adoption for the CLI-agent sessions: direct HYBRID method instead of the
similarity search used for out_ago2026 (designed for the copy-paste
model, not for an agent that writes directly into files).

For each CLI-agent session:
  1. Extracts from the raw markdown the `git commit -m "..."` commands invoked
     by the agent in the Bash tool-uses (lib_extract_git_commits_from_sessions.py) -
     a first-hand signal, inside the transcript itself.
  2. Searches the repo's REAL commit history (already downloaded by
     1_scrape_pool.py into data/commits_history/{repo_id}.json, no API
     call needed for this step) for a commit with an identical message
     or the same subject (first line) - a match is almost certain given
     the specificity of the messages (often with the signature
     "Generated with Claude Code").
  3. For each match: VERIFIES that the session's code is really in the
     commit found (we do not trust the message match alone) -
     downloads the commit (local cache if already in data/commits/,
     otherwise via API) and checks that the session's code appears in the
     FULL content of each file touched AT THE FOUND COMMIT (not only
     in the lines added in the diff of that commit: that underestimated the match
     when part of the code pre-existed from an earlier commit of the
     same session - requires an API call for each file not yet in
     cache). The code used for the verification is NOT the one
     already in out_vibe_coding/snippet.csv (which for CLI-agent sessions in
     specstory format also includes output of Bash/Read/Grep tools, e.g.
     `git status`, pasted in a fence - false positives discovered in
     manual review): here a dedicated clean extraction is used
     (lib_cli_agent_model_parser.extract_snippet_clean), ONLY for this
     script - out_vibe_coding/snippet.csv stays unchanged for the other
     analyses.

Sessions with no commit detected in-session are NOT covered by this
script (the agent may have modified files without committing itself, or
the user may have committed manually afterwards) - they remain to be
handled with a step 7-12 style fallback, not yet built for this
dataset.

Writes out_vibe_coding/adoption_cli_agent_direct.csv.
"""
import csv
import importlib
import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
extract = importlib.import_module("lib_extract_git_commits_from_sessions")
track = importlib.import_module("2_01_chat_track_snippets")  # fetch_file_at_ref, _request
step9 = importlib.import_module("2_03_chat_pr_commits_recheck")  # snippet_in_text
camp = importlib.import_module("lib_cli_agent_model_parser")  # clean code extraction
from lib_github_cache import DiskCache

csv.field_size_limit(50_000_000)

SCRAPER_DIR = os.path.join(SCRIPT_DIR, "1_07_agentic_scraper", "data")
MARKDOWNS_CLI_DIR = os.path.join(SCRAPER_DIR, "markdowns_cli")
COMMITS_HISTORY_DIR = os.path.join(SCRAPER_DIR, "commits_history")
COMMITS_DETAIL_DIR = os.path.join(SCRAPER_DIR, "commits")
OUT_DIR = os.path.join(SCRIPT_DIR, "out_vibe_coding")
OUT_PATH = os.path.join(OUT_DIR, "adoption_cli_agent_direct.csv")


def normalize(msg):
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
    """Searches the repo's commit history for an exact match on the full
    message, then on the subject alone (first line) as a fallback."""
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


def load_snippet_per_link():
    path = os.path.join(OUT_DIR, "snippet.csv")
    of = {}
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            of.setdefault(r["conversation_url"], []).append(r["response_code"])
    return of


def code_for_verification(text_raw, link, snippet_per_link):
    """Code to use ONLY for verifying adoption (does not touch
    out_vibe_coding/snippet.csv): for the specstory format it combines two
    clean sources - Write content + prose (extract_snippet_clean) and
    the last Read of each path (extract_reads_clean, captures the state
    of the file after an Edit, which alone does not keep its own diff) -
    both filtered by language. If the result is empty, the
    session is left without candidates (we do NOT fall back to the response_code
    already in snippet.csv, which for specstory also includes noise such as
    `git status`/directory listings: a silent fallback would defeat
    the cleanup). The fallback to snippet.csv remains only for the other two
    formats (claude_session/work_session), which do not have the same
    tool-use marking to clean up."""
    if camp.detect_format(text_raw) == "specstory":
        pairs = camp.extract_snippet_clean(text_raw) + camp.extract_reads_clean(text_raw)
        return [c["response_code"] for c in pairs]
    return snippet_per_link.get(link, []) if link else []


def fetch_commit_detail(repo, sha, cache):
    local_path = os.path.join(COMMITS_DETAIL_DIR, sha)
    if os.path.exists(local_path):
        with open(local_path, encoding="utf-8") as f:
            return json.load(f)
    key = ("commit_detail_full", repo, sha)
    if key in cache:
        return cache[key]
    detail = track._request(f"https://api.github.com/repos/{repo}/commits/{sha}", allow_404=True)
    cache[key] = detail
    return detail


def main():
    print("Loading repo per session...")
    repo_per_sha = load_repo_per_sha()
    print("Loading snippets per conversation_url...")
    snippet_per_link = load_snippet_per_link()
    print("Loading session metadata (conversation_url per sha) from out_vibe_coding/conversations.jsonl...")
    link_per_sha = {}
    with open(os.path.join(OUT_DIR, "conversations.jsonl"), encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            if c.get("is_cli_agent") == "yes":
                link_per_sha[c["sha"]] = c["conversation_url"]

    cache = DiskCache(os.path.join(OUT_DIR, "github_cache_cli_agent.pkl"))

    files = [fn for fn in os.listdir(MARKDOWNS_CLI_DIR) if fn.endswith(".md")]
    print(f"Total CLI-agent sessions: {len(files)}")

    rows_out = []
    n_with_commit = n_matched = n_verified = 0
    history_cache = {}

    for i, fn in enumerate(files, 1):
        if i % 300 == 0:
            cache.save()
            print(f"  [{i}/{len(files)}]  with_commit={n_with_commit} matched={n_matched} verified={n_verified}")
        sha = fn[:-3]
        text = open(os.path.join(MARKDOWNS_CLI_DIR, fn), encoding="utf-8").read()
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

        link = link_per_sha.get(sha)
        snippet_session = code_for_verification(text, link, snippet_per_link)

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
                detail = fetch_commit_detail(repo_full_name, commit_sha, cache)
                # FULL content of each file touched AT THE FOUND COMMIT,
                # not just the lines added in the diff of THAT commit: the
                # diff does not include lines that the file already had from an
                # earlier commit of the same session (imports/setup
                # written earlier, functions added in the found commit) -
                # comparing only against the diff underestimated the match
                # (concrete case: 340/813 lines of a candidate were a
                # preamble already present, not in the diff of that commit).
                contents = []
                if detail:
                    for fmeta in (detail.get("files") or []):
                        filename = fmeta.get("filename") or ""
                        if camp.is_chat_transcript(filename):
                            continue  # SpecStory export - circular match, it is not real code
                        if fmeta.get("status") == "removed":
                            continue
                        content = track.fetch_file_at_ref(repo_full_name, filename, commit_sha, cache)
                        if content:
                            contents.append(content)
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
