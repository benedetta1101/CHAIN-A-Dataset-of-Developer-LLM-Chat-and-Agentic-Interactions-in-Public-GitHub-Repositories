#!/usr/bin/env python
"""
Step 9 (optional) - Recheck of PRs on individual commits, not just the HEAD.

Standalone: no imports from other scripts of the project. Usage:
    python pipeline/2_03_chat_pr_commits_recheck.py --limit 50
    python pipeline/2_03_chat_pr_commits_recheck.py

Step 7, for rows with a GitHubPullRequest artifact, checks only the FINAL
state of the PR (the last commit, "head"). But a PR can have many
intermediate commits: if the snippet was pasted in one commit and then
cleaned up/replaced in a later commit OF THE SAME PR (before the
merge), the head no longer contains it - yet the generated code passed
through there. Verified with a live test on a random sample of 25 rows
"not found in the head": 7 (28%) were in an intermediate commit.

This step takes up the PR rows that step 7 did not find in the head, and
for each one downloads the list of the PR's commits and the diff of each,
searching for the snippet ONLY among the added lines ("+" prefix in the patch) -
never among removed or context lines, otherwise we would find deleted
code, not pasted code.

Writes out/snippet_pr_commits.csv (new file, does not touch the other outputs).
"""
import argparse
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_github_cache import DiskCache

csv.field_size_limit(50_000_000)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT_DIR = os.path.join(SCRIPT_DIR, "out")
GITHUB_API = "https://api.github.com"
BASE_HEADERS = {"Accept": "application/vnd.github+json",
                "User-Agent": "devgpt-extend-tracker/1.0",
                "X-GitHub-Api-Version": "2022-11-28"}


def load_tokens():
    raw = os.environ.get("GITHUB_TOKENS") or os.environ.get("GITHUB_TOKEN") or ""
    toks = [t.strip() for t in re.split(r"[,\s]+", raw) if t.strip()]
    here = os.path.dirname(os.path.abspath(__file__))
    for path in ("tokens.txt", os.path.join(here, "tokens.txt")):
        if os.path.exists(path):
            with open(path) as f:
                toks += [l.strip() for l in f
                         if l.strip() and not l.lstrip().startswith("#")]
            break
    seen, out = set(), []
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


class TokenPool:
    def __init__(self, tokens):
        self.tokens = tokens or [None]
        self.i = 0
        self.blocked_until = {}

    def has_auth(self):
        return any(t for t in self.tokens)

    def current(self):
        return self.tokens[self.i]

    def headers(self):
        h = dict(BASE_HEADERS)
        tok = self.current()
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        return h

    def mark_blocked(self, until_epoch):
        self.blocked_until[self.current()] = until_epoch

    def next_available(self):
        now = time.time()
        for _ in range(len(self.tokens)):
            self.i = (self.i + 1) % len(self.tokens)
            if self.blocked_until.get(self.current(), 0) <= now:
                return self.current(), 0
        soonest = min(self.blocked_until.values())
        return self.current(), max(0, soonest - now) + 2


POOL = TokenPool(load_tokens())
_PCT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")


def _safe_url(url):
    url = url.replace("#", "%23")
    url = _PCT_RE.sub("%25", url)
    sp = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(sp.path, safe="/%:@")
    query = urllib.parse.quote(sp.query, safe="=&%:@/?+")
    return urllib.parse.urlunsplit((sp.scheme, sp.netloc, path, query, sp.fragment))


def _request(url, allow_404=False):
    url = _safe_url(url)
    for attempt in range(6):
        req = urllib.request.Request(url, headers=POOL.headers())
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                body = json.load(r)
                rem = r.headers.get("X-RateLimit-Remaining")
                if rem is not None and int(rem) <= 1:
                    reset = int(r.headers.get("X-RateLimit-Reset", "0"))
                    POOL.mark_blocked(reset)
                    _, wait = POOL.next_available()
                    if wait > 0:
                        time.sleep(wait)
                return body
        except urllib.error.HTTPError as e:
            if e.code == 404 and allow_404:
                return None
            if e.code in (403, 429):
                reset = e.headers.get("X-RateLimit-Reset")
                retry_after = e.headers.get("Retry-After")
                if reset:
                    POOL.mark_blocked(int(reset))
                elif retry_after:
                    POOL.mark_blocked(time.time() + int(retry_after))
                else:
                    POOL.mark_blocked(time.time() + 30 * (attempt + 1))
                _, wait = POOL.next_available()
                if wait > 0:
                    time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(5)
    raise RuntimeError(f"giving up on {url}")


def _normalize(text):
    return re.sub(r"\s+", " ", text or "").strip()


def added_lines_only(patch):
    """Only the ADDED lines of a unified diff ('+' prefix, excluding the
    '+++' header). Removed ('-') or context (' ') lines do not
    count as "code pasted here"."""
    out = []
    for line in (patch or "").splitlines():
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            out.append(line[1:])
    return "\n".join(out)


def _fragment(code, n_chars=200):
    norm = _normalize(code)
    if len(norm) <= n_chars:
        return norm
    start = max(0, (len(norm) - n_chars) // 2)
    return norm[start:start + n_chars]


_ROW_SEPARATOR_RE = re.compile(r"^[\s#/*\-=~_.]+$")


def _row_trivial(row_normalized):
    """Lines made only of repeated separator/comment characters
    (e.g. "# ----------" or "// ==========") - they appear almost
    identical in a huge number of files by pure style convention
    (section separators), so a match ONLY on a line like that
    is not evidence of adopted code, only of a common style (false
    positive reported by the user with a concrete screenshot: a match on
    a section separator between two unrelated Python files)."""
    return bool(_ROW_SEPARATOR_RE.match(row_normalized))


def _lines_normalized(code, min_len=30):
    return [n for n in (_normalize(l) for l in (code or "").splitlines())
            if len(n) >= min_len and not _row_trivial(n)]


def snippet_in_text(code, text):
    if not code or not text:
        return False
    text_n = _normalize(text)
    if _fragment(code) in text_n:
        return True
    return any(line in text_n for line in _lines_normalized(code))


def pr_commit_added_lines(repo, number, cache):
    """For each commit of the PR, the lines added in each file it
    touched. Cached per (repo, number) - a PR is often referenced by
    several rows (several snippets from the same conversation/PR)."""
    key = ("pr_commits", repo, number)
    if key in cache:
        return cache[key]

    out = []
    try:
        commits = _request(
            f"{GITHUB_API}/repos/{repo}/pulls/{number}/commits?per_page=100", allow_404=True) or []
        for c in commits:
            sha = c.get("sha")
            if not sha:
                continue
            ckey = ("commit_detail", repo, sha)
            if ckey not in cache:
                try:
                    cache[ckey] = _request(f"{GITHUB_API}/repos/{repo}/commits/{sha}", allow_404=True)
                except Exception:
                    cache[ckey] = None
            detail = cache[ckey]
            if not detail:
                continue
            for f in detail.get("files", []):
                patch = f.get("patch")
                if patch:
                    out.append((sha, f.get("filename"), added_lines_only(patch)))
    except Exception:
        pass

    cache[key] = out
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    ap.add_argument("--limit", type=int, default=None,
                     help="recheck only the first N candidate rows (useful for quick tests)")
    args = ap.parse_args()

    if not POOL.has_auth():
        print("WARNING: no GITHUB_TOKEN - 60 requests/hour.")

    snippet_path = os.path.join(args.out_dir, "snippet.csv")
    prov_path = os.path.join(args.out_dir, "snippet_provenance.csv")
    out_path = os.path.join(args.out_dir, "snippet_pr_commits.csv")

    with open(snippet_path, encoding="utf-8", newline="") as f:
        snippet_rows = {r["row_id"]: r for r in csv.DictReader(f)}
    with open(prov_path, encoding="utf-8", newline="") as f:
        prov_rows = list(csv.DictReader(f))

    candidates = [r for r in prov_rows
                 if r["artifact_type"] == "pull_request" and r["snippet_in_file"] == "no"]
    print(f"Candidates (PRs not found in the head): {len(candidates)}")
    if args.limit:
        candidates = candidates[:args.limit]
        print(f"(limited to {len(candidates)} for --limit)")

    cache = DiskCache(os.path.join(args.out_dir, "github_cache.pkl"))
    results = []
    for i, r in enumerate(candidates, 1):
        cache.save_every(500, i)
        row = snippet_rows.get(r["row_id"])
        if row is None:
            continue
        num = r["artifact_url"].rstrip("/").split("/")[-1]
        entries = pr_commit_added_lines(r["repo"], num, cache)

        found_sha, found_path = None, None
        for sha, path, added in entries:
            if snippet_in_text(row["response_code"], added):
                found_sha, found_path = sha, path
                break

        out = {
            "row_id": r["row_id"],
            "conversation_url": r["conversation_url"],
            "repo": r["repo"],
            "pr": num,
            "found_in_intermediate_commit": "yes" if found_sha else "no",
            "commit_sha": found_sha or "",
            "path": found_path or "",
        }
        results.append(out)
        print(f"  [{i}/{len(candidates)}] {r['repo']} PR#{num} -> {out['found_in_intermediate_commit']}"
              + (f" ({found_path} @ {found_sha[:8]})" if found_sha else ""))

    cache.save()
    # ALWAYS writes the file, even if empty (header only) - 0 candidates is a
    # legitimate outcome, not an error: downstream scripts (2_05_chat_reverify_wholerepo.py,
    # 2_06_chat_timeline.py) can open it unconditionally - same real bug
    # already fixed in 2_02_chat_similarity_recheck.py, found while testing the
    # 7-12 chain on a small sample.
    fieldnames = ["row_id", "conversation_url", "repo", "pr",
                  "found_in_intermediate_commit", "commit_sha", "path"]
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(results)

    print(f"\nWritten {out_path} ({len(results)} rows)")
    print(f"found_in_intermediate_commit: {dict(Counter(r['found_in_intermediate_commit'] for r in results))}")


if __name__ == "__main__":
    main()
