#!/usr/bin/env python
"""
Adoption (CLI-agent + IDE) for the sessions WITHOUT a commit/path extractable
by the methods already built (direct/fallback/authorship): last resort,
WHOLE-REPO search via the GitHub code search API
(2_01_chat_track_snippets.py::search_repo_for_snippet, same technique as the
old out_ago2026 dataset) - searches whether the code proposed by the agent
appears SOMEWHERE in the default branch of the repo, without needing
to already know the path or the commit.

Rate limit MUCH tighter than the core API (~27 searches/minute,
shared, not multipliable per token) - covers only the sessions with
AT LEAST ONE clean code candidate extracted (otherwise there is nothing
to search), about 7900 of the 12794 out-of-scope sessions.

IMPORTANT (bug found and fixed during a test on a 500 sample):
the search results must be filtered to exclude other
.specstory/history/*.md files in the same repo - a match there is circular
(the SAME idea/code discussed in ANOTHER session, never in real
source code) and inflated the apparent success rate from 85% to
24% once removed. Verified empirically: without this filter the
success rate was completely unreliable.

One search (maximum 3 attempts on different candidates) per session.

Writes out_vibe_coding/adoption_whole_repo.csv.
"""
import csv
import importlib
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
track = importlib.import_module("2_01_chat_track_snippets")
step9 = importlib.import_module("2_03_chat_pr_commits_recheck")
camp = importlib.import_module("lib_cli_agent_model_parser")
ide = importlib.import_module("lib_ide_session_parser")
from lib_github_cache import DiskCache

csv.field_size_limit(50_000_000)

OUT_DIR = os.path.join(SCRIPT_DIR, "out_vibe_coding")
MARKDOWNS_CLI_DIR = os.path.join(SCRIPT_DIR, "1_07_agentic_scraper", "data", "markdowns_cli")
MARKDOWNS_IDE_DIR = os.path.join(SCRIPT_DIR, "1_07_agentic_scraper", "data", "markdowns")
OUT_PATH = os.path.join(OUT_DIR, "adoption_whole_repo.csv")


def count(path):
    shas = set()
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            shas.add(r["sha"])
    return shas


def candidates_cli(sha):
    path = os.path.join(MARKDOWNS_CLI_DIR, sha + ".md")
    if not os.path.exists(path):
        return None
    text = open(path, encoding="utf-8").read()
    if camp.detect_format(text) != "specstory":
        return None
    return [c["response_code"] for c in camp.extract_snippet_clean(text) + camp.extract_reads_clean(text)]


def candidates_ide(sha):
    path = os.path.join(MARKDOWNS_IDE_DIR, sha + ".md")
    if not os.path.exists(path):
        return None
    text = open(path, encoding="utf-8").read()
    return [c["response_code"] for c in
            ide.extract_reads_ide(text) + ide.extract_diff_ide(text) + ide.extract_prose_ide(text)]


def main():
    cli_covered = (count(f"{OUT_DIR}/adoption_cli_agent_direct.csv")
                   | count(f"{OUT_DIR}/adoption_cli_agent_fallback.csv")
                   | count(f"{OUT_DIR}/adoption_cli_agent_authorship.csv"))
    ide_covered = count(f"{OUT_DIR}/adoption_ide_direct.csv") | count(f"{OUT_DIR}/adoption_ide_fallback.csv")

    repo_per_sha = {}
    to_process = []  # (type, sha)
    with open(f"{OUT_DIR}/conversations.jsonl", encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            sha = c["sha"]
            repo_per_sha[sha] = (c.get("artifacts") or [{}])[0].get("repo", "")
            if c.get("is_cli_agent") == "yes" and sha not in cli_covered:
                to_process.append(("cli", sha))
            elif c.get("is_cli_agent") == "no" and sha not in ide_covered:
                to_process.append(("ide", sha))

    cache = DiskCache(os.path.join(OUT_DIR, "github_cache_whole_repo.pkl"))
    rows_out = []
    n_with_candidates = n_searched = n_found_path = n_verificate = 0

    for i, (kind, sha) in enumerate(to_process, 1):
        if i % 100 == 0:
            cache.save()
            print(f"  [{i}/{len(to_process)}]  with_candidates={n_with_candidates} searched={n_searched} "
                  f"found_path={n_found_path} verificate={n_verificate}")
        repo = repo_per_sha.get(sha, "")
        if not repo:
            continue
        candidates = candidates_cli(sha) if kind == "cli" else candidates_ide(sha)
        if not candidates:
            continue
        n_with_candidates += 1

        result_path, candidate_used = None, None
        for cod in candidates[:3]:
            linea = track.pick_search_line(cod)
            if not linea:
                continue
            key = ("whole_repo_search", repo, linea)
            if key in cache:
                result = cache[key]
            else:
                try:
                    result = track.search_repo_for_snippet(repo, cod)
                except Exception:
                    # transient network error (e.g. RemoteDisconnected,
                    # not caught by the internal retry of _request) - not
                    # fatal, a permanent negative outcome is not cached
                    # because of a network blip: it is retried on the next run,
                    # a single lost search must not interrupt the
                    # other thousands already in the queue
                    result = None
                else:
                    cache[key] = result
                n_searched += 1
            if result:
                # excludes other SpecStory transcript files in the same
                # repo (circular false positive - see docstring)
                real = [p for p in result if not camp.is_chat_transcript(p)]
                if real:
                    result_path, candidate_used = real, cod
                    break

        if result_path:
            n_found_path += 1
            found = "no"
            path_confirmed = ""
            for p in result_path[:3]:
                content = track.fetch_file_at_ref(repo, p, "HEAD", cache)
                if content and step9.snippet_in_text(candidate_used, content):
                    found = "yes"
                    path_confirmed = p
                    n_verificate += 1
                    break
            rows_out.append({"type": kind, "sha": sha, "repo": repo,
                               "path": path_confirmed or result_path[0], "found": found})
        else:
            rows_out.append({"type": kind, "sha": sha, "repo": repo, "path": "",
                               "found": "none"})

    cache.save()
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["type", "sha", "repo", "path", "found"])
        w.writeheader()
        w.writerows(rows_out)

    print(f"\nSessions to process (with candidates): {n_with_candidates}")
    print(f"Effective whole-repo searches: {n_searched}")
    print(f"Sessions with at least one real path found: {n_found_path}")
    print(f"Verified sessions: {n_verificate}")
    print(f"Written {OUT_PATH}")


if __name__ == "__main__":
    main()
