#!/usr/bin/env python
"""
Step 11 (optional) - Re-verification of the whole-repo/file-history matches with
stricter criteria, without redoing the searches.

Standalone: no imports from other scripts of the project. Usage:
    python pipeline/2_05_chat_reverify_wholerepo.py

A manual audit of ~65 randomly sampled matches (see commit/discussion)
found that the whole-repo search (used by step 7 as a fallback for
GitHubCodeFile/GitHubPullRequest, and as the ONLY signal for GitHubIssue) has
a real false positive rate of about 40%, almost always for the
same reason: the searched line was generic boilerplate (a single
import line, a #define macro, a generic catch block, a standard file
format header) that recurs in the repo regardless of the specific
conversation - not evidence that the generated code was
adopted. The same problem, on a smaller scale, was seen in step 10.

This step does NOT redo any search (expensive, rate-limited): it takes up
the paths ALREADY FOUND by step 7 (whole-repo, including GitHubIssue rows) and
step 10 (file history), re-downloads their content and re-verifies them with the
updated matching logic (2_01_chat_track_snippets.py: generic lines
excluded, minimum length raised to 45 for the whole-repo fallback). If a
match no longer holds under the stricter criteria, it is downgraded.

Writes:
  out/snippet_provenance_corrected.csv (copy of snippet_provenance.csv
    with found_elsewhere_in_repo and, for GitHubIssue rows, also
    snippet_today/path re-corrected)
  out/snippet_codefile_commits_corrected.csv (same for found_in_history)
"""
import argparse
import base64
import csv
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from collections import Counter

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import importlib
track = importlib.import_module("2_01_chat_track_snippets")

csv.field_size_limit(50_000_000)
DEFAULT_OUT_DIR = os.path.join(SCRIPT_DIR, "out")
GITHUB_API = "https://api.github.com"


def fetch_file(repo, path, ref, cache):
    key = (repo, path, ref)
    if key in cache:
        return cache[key]
    try:
        meta = track._request(
            f"{GITHUB_API}/repos/{repo}/contents/{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref)}",
            allow_404=True)
        cache[key] = base64.b64decode(meta["content"]).decode("utf-8", "ignore") if meta and "content" in meta else None
    except Exception:
        cache[key] = None
    return cache[key]


def strict_match(code, text):
    """Like track.snippet_in_text but using the already-updated functions
    (generic lines excluded, min_len 30 for the fallback - here we use
    the same min_len as the known-file check, not the 45 of the search, because
    the path is already known: the genericity risk remains but is lower)."""
    return track.snippet_in_text(code, text)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    args = ap.parse_args()

    if not track.POOL.has_auth():
        print("WARNING: no GITHUB_TOKEN - 60 requests/hour.")

    snippet_path = os.path.join(args.out_dir, "snippet.csv")
    prov_path = os.path.join(args.out_dir, "snippet_provenance.csv")
    hist_path = os.path.join(args.out_dir, "snippet_codefile_commits.csv")

    with open(snippet_path, encoding="utf-8", newline="") as f:
        snip = {r["row_id"]: r for r in csv.DictReader(f)}

    cache = {}

    # ---- 1. snippet_provenance.csv: recheck found_elsewhere_in_repo
    # (whole-repo search: fallback for code_file/pull_request, only
    # signal for issue) ----
    with open(prov_path, encoding="utf-8", newline="") as f:
        prov_rows = list(csv.DictReader(f))

    n_downgraded_elsewhere = 0
    n_downgraded_issue_yes = 0
    for r in prov_rows:
        code = snip.get(r["row_id"], {}).get("response_code")
        if not code:
            continue

        raw = r.get("found_elsewhere_in_repo", "")
        if raw and raw not in ("none", "not_searched", "not_verifiable") and not raw.startswith("error"):
            p = raw.split(";")[0]
            text = fetch_file(r["repo"], p, r.get("default_branch") or "HEAD", cache)
            if not (text and strict_match(code, text)):
                r["found_elsewhere_in_repo"] = "downgraded_strict_criteria"
                n_downgraded_elsewhere += 1

        if r["artifact_type"] == "issue" and r.get("snippet_today") == "yes" and r.get("path"):
            text = fetch_file(r["repo"], r["path"], r.get("default_branch") or "HEAD", cache)
            if not (text and strict_match(code, text)):
                r["snippet_today"] = "downgraded_strict_criteria"
                n_downgraded_issue_yes += 1
                print(f"  [issue downgraded] {r['repo']}/{r['path']}")

    out_prov = os.path.join(args.out_dir, "snippet_provenance_corrected.csv")
    with open(out_prov, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(prov_rows[0].keys()))
        w.writeheader()
        w.writerows(prov_rows)
    print(f"\nWritten {out_prov}")
    print(f"  found_elsewhere_in_repo downgraded: {n_downgraded_elsewhere}")
    print(f"  issue rows (snippet_today='yes') downgraded: {n_downgraded_issue_yes}")

    # ---- 2. snippet_codefile_commits.csv: recheck found_in_history ----
    if os.path.exists(hist_path):
        with open(hist_path, encoding="utf-8", newline="") as f:
            hist_rows = list(csv.DictReader(f))

        n_downgraded_history = 0
        for r in hist_rows:
            if r.get("found_in_history") != "yes":
                continue
            code = snip.get(r["row_id"], {}).get("response_code")
            if not code:
                continue
            text = fetch_file(r["repo"], r["path"], r["commit_sha"], cache)
            if not (text and strict_match(code, text)):
                r["found_in_history"] = "downgraded_strict_criteria"
                n_downgraded_history += 1
                print(f"  [history downgraded] {r['repo']}/{r['path']} @ {r['commit_sha'][:8]}")

        # fixed fieldnames (no longer hist_rows[0].keys()) - hist_rows may
        # be empty (0 candidates at step 10, now a file always written
        # even if empty after the fix there) and [0] would raise IndexError - real
        # bug discovered while testing the 7-12 chain on a small sample,
        # masked until now only because the file previously did not exist at all
        # in that case (os.path.exists above returned False).
        out_hist = os.path.join(args.out_dir, "snippet_codefile_commits_corrected.csv")
        fieldnames = ["row_id", "conversation_url", "repo", "path", "link_date",
                      "n_commits_checked", "n_matches_discarded_before_link",
                      "found_in_history", "commit_sha", "found_date"]
        with open(out_hist, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(hist_rows)
        print(f"\nWritten {out_hist}")
        print(f"  found_in_history downgraded: {n_downgraded_history}")


if __name__ == "__main__":
    main()
