#!/usr/bin/env python
"""
Step 12 (optional) - Final table: where and when we find the code.

Standalone: no imports from other scripts of the project (except 07_track_
snippets.py for the HTTP/matching functions, already so for step 11).
Usage:
    python pipeline/2_06_chat_timeline.py --limit 50
    python pipeline/2_06_chat_timeline.py

Steps 7-11 each answer "was this snippet found?" with a different
piece of logic, in different files. This step consolidates everything
into a single row per traceable snippet, with two more questions beyond
yes/no:

  WHERE: found_via (mechanism: known_verbatim / whole_repo / similarity
    / intermediate_pr_commit / file_history / not_found), found_path,
    found_commit.

  WHEN: link_date (when the repo already contained the link to the chat -
    from the creation of the issue/PR, or from the known commit for a direct file)
    and found_date (date of the commit in which the code was found, if
    known). days_elapsed = found_date - link_date when both
    are known; days_elapsed_clipped zeroes the negatives (see below) for
    direct use in an aggregate "how long did it take" analysis.

Honest limit on found_date: for matches "in today's state" (not at
a specific already-known commit - whole-repo, similarity, file today) there
is no ONE precise commit to anchor the date to. We use the date of the LAST
commit that touched that file as a lower-bound approximation (the
code may have been there from before, not after) - explicitly marked with
found_date_type="last_file_commit" to distinguish it from a match at a
specific, known commit (found_date_type="exact_commit").

Honest limit also on link_date: for issues/PRs it uses created_at (opening
date, reliable). For GitHubCodeFile there is no direct created_at -
we use the date of the commit known from discovery, which however is an UPPER
bound (the link was already there at that point, but it may have appeared
earlier - the known commit is often just the HEAD seen by the code search
at the time of the scan, not the last commit that actually touched the
file, see 2_04_chat_codefile_commits_recheck.py). For this reason a negative
days_elapsed for a GitHubCodeFile row is not necessarily an
error: it may mean that the code was already there before the point we
used as a reference, not that time runs backwards.

Writes out/snippet_timeline.csv.
"""
import argparse
import csv
import json
import os
import sys
import urllib.parse
from collections import Counter
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import importlib
track = importlib.import_module("2_01_chat_track_snippets")

csv.field_size_limit(50_000_000)
DEFAULT_OUT_DIR = os.path.join(SCRIPT_DIR, "out")
GITHUB_API = "https://api.github.com"


def commit_date(repo, sha, cache):
    key = ("commit_date", repo, sha)
    if key in cache:
        return cache[key]
    date = None
    try:
        detail = track._request(f"{GITHUB_API}/repos/{repo}/commits/{sha}", allow_404=True)
        if detail:
            date = (detail.get("commit", {}).get("author", {}) or {}).get("date") \
                or (detail.get("commit", {}).get("committer", {}) or {}).get("date")
    except Exception:
        pass
    cache[key] = date
    return date


def latest_commit_date_for_path(repo, path, cache):
    """Date of the last commit that touched this file - approximation
    for the 'in today's state' matches without a specific known commit."""
    key = ("latest_for_path", repo, path)
    if key in cache:
        return cache[key]
    date = None
    try:
        data = track._request(
            f"{GITHUB_API}/repos/{repo}/commits?path={urllib.parse.quote(path)}&per_page=1",
            allow_404=True) or []
        if data:
            c = data[0]
            date = (c.get("commit", {}).get("author", {}) or {}).get("date") \
                or (c.get("commit", {}).get("committer", {}) or {}).get("date")
    except Exception:
        pass
    cache[key] = date
    return date


def find_artifact_created_at(artifacts_json, kind):
    try:
        arts = json.loads(artifacts_json)
    except Exception:
        return None
    for a in arts:
        if a.get("type") == {"code_file": "GitHubCodeFile", "pull_request": "GitHubPullRequest",
                              "issue": "GitHubIssue"}.get(kind):
            if a.get("created_at"):
                return a["created_at"]
    return None


def elsewhere_valid(v):
    return v not in ("none", "not_searched", "not_verifiable", "downgraded_strict_criteria") \
        and not v.startswith("error")


def days_between(link_date, found_date):
    if not link_date or not found_date:
        return ""
    try:
        d1 = datetime.fromisoformat(link_date.replace("Z", "+00:00"))
        d2 = datetime.fromisoformat(found_date.replace("Z", "+00:00"))
        return (d2 - d1).days
    except Exception:
        return ""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    if not track.POOL.has_auth():
        print("WARNING: no GITHUB_TOKEN - 60 requests/hour.")

    snippet_path = os.path.join(args.out_dir, "snippet.csv")
    prov_path = os.path.join(args.out_dir, "snippet_provenance_corrected.csv")
    sim_path = os.path.join(args.out_dir, "snippet_similarity.csv")
    pr9_path = os.path.join(args.out_dir, "snippet_pr_commits.csv")
    hist10_path = os.path.join(args.out_dir, "snippet_codefile_commits_corrected.csv")
    out_path = os.path.join(args.out_dir, "snippet_timeline.csv")

    with open(snippet_path, encoding="utf-8", newline="") as f:
        snip = {r["row_id"]: r for r in csv.DictReader(f)}
    with open(prov_path, encoding="utf-8", newline="") as f:
        prov_rows = list(csv.DictReader(f))
    with open(sim_path, encoding="utf-8", newline="") as f:
        sim = {r["row_id"]: r for r in csv.DictReader(f) if r["verdict"] == "probable_yes"}
    with open(pr9_path, encoding="utf-8", newline="") as f:
        pr9 = {r["row_id"]: r for r in csv.DictReader(f) if r["found_in_intermediate_commit"] == "yes"}
    hist10 = {}
    if os.path.exists(hist10_path):
        with open(hist10_path, encoding="utf-8", newline="") as f:
            hist10 = {r["row_id"]: r for r in csv.DictReader(f) if r["found_in_history"] == "yes"}

    if args.limit:
        prov_rows = prov_rows[:args.limit]

    cache = {}
    results = []
    for i, r in enumerate(prov_rows, 1):
        link = r["conversation_url"]
        rid = r["row_id"]
        snip_row = snip.get(rid)
        kind = r["artifact_type"]

        dove, found_path, found_commit, found_date_type = "not_found", "", "", ""

        if r["snippet_today"] == "yes":
            found_path = r["path"]
            if kind == "issue":
                # for issue rows the only mechanism is always the whole-repo
                # search, even when the field is snippet_today
                dove, found_date_type = "whole_repo", "last_file_commit"
            elif r.get("snippet_in_file") == "yes" and r.get("commit_sha"):
                dove, found_commit, found_date_type = "known_verbatim", r["commit_sha"], "exact_commit"
            else:
                dove, found_date_type = "verbatim_today", "last_file_commit"
        elif elsewhere_valid(r["found_elsewhere_in_repo"]):
            dove = "whole_repo"
            found_path = r["found_elsewhere_in_repo"].split(";")[0]
            found_date_type = "last_file_commit"
        elif rid in sim:
            dove, found_date_type = "similarity", "last_file_commit"
            found_path = sim[rid]["path"]
        elif rid in pr9:
            dove, found_date_type = "intermediate_pr_commit", "exact_commit"
            found_path, found_commit = pr9[rid]["path"], pr9[rid]["commit_sha"]
        elif rid in hist10:
            dove, found_date_type = "file_history", "exact_commit"
            found_path, found_commit = hist10[rid]["path"], hist10[rid]["commit_sha"]

        # link_date: for PRs and issues we use created_at from the artifact (opening
        # date, already available, no extra calls) - NOT the date of the
        # last commit of the PR (head), which would be later than
        # any intermediate commit found by step 9, systematically producing
        # negative days_elapsed (bug discovered while testing).
        # For code_file the known commit remains: it is an upper bound (the
        # link was ALREADY there, but it may have appeared earlier) not
        # an exact value, there is no direct created_at to use.
        link_date = None
        if kind == "code_file" and r.get("commit_sha"):
            link_date = commit_date(r["repo"], r["commit_sha"], cache)
        elif kind == "pull_request" and snip_row:
            link_date = find_artifact_created_at(snip_row["artifacts"], "pull_request")
        elif kind == "issue" and snip_row:
            link_date = find_artifact_created_at(snip_row["artifacts"], "issue")

        # found_date
        found_date = ""
        if dove == "file_history":
            found_date = hist10[rid].get("found_date") or ""
        elif found_date_type == "exact_commit" and found_commit:
            found_date = commit_date(r["repo"], found_commit, cache) or ""
        elif found_date_type == "last_file_commit" and found_path:
            found_date = latest_commit_date_for_path(r["repo"], found_path, cache) or ""

        days = days_between(link_date, found_date)
        out = {
            "row_id": rid,
            "conversation_url": link,
            "repo": r["repo"],
            "artifact_type": kind,
            "found": "yes" if dove != "not_found" else "no",
            "found_via": dove,
            "found_path": found_path,
            "found_commit": found_commit,
            "link_date": link_date or "",
            "found_date": found_date,
            "found_date_type": found_date_type,
            "days_elapsed": days,
            # negative almost always = imprecise "link_date" (upper bound,
            # not the exact moment) or a local commit shortly before the opening
            # of the PR - not time running backwards. For an aggregate "how long
            # did it take" analysis, treat the negative as "immediate/concurrent" (0)
            # instead of a meaningless number; for a more rigorous analysis, filter on
            # found_date_type="exact_commit" and use days_elapsed (not clip) directly.
            "days_elapsed_clipped": max(0, days) if days != "" else "",
        }
        results.append(out)
        if i % 500 == 0:
            print(f"  [{i}/{len(prov_rows)}]")

    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)

    print(f"\nWritten {out_path} ({len(results)} rows)")
    print(f"found: {dict(Counter(r['found'] for r in results))}")
    print(f"found_via: {dict(Counter(r['found_via'] for r in results))}")
    with_days = [r["days_elapsed"] for r in results if r["days_elapsed"] != ""]
    print(f"rows with computable days_elapsed: {len(with_days)}")


if __name__ == "__main__":
    main()
