#!/usr/bin/env python
"""
Step 10 (optional) - Commit history for GitHubCodeFile rows.

Standalone: no imports from other scripts of the project. Usage:
    python pipeline/2_04_chat_codefile_commits_recheck.py --limit 50
    python pipeline/2_04_chat_codefile_commits_recheck.py

Steps 7-8, for rows with a GitHubCodeFile artifact, check only
TWO points in time: the commit known from discovery and the current state of
the file. As with PRs (step 9), the code may have passed through an
intermediate commit that was never checked - pasted and then modified/removed before
today, or even before the known commit.

This step takes up the GitHubCodeFile rows that neither step 7 nor step
8 found, and for each one downloads the history of the commits that
touched that file (up to --max-commits, default 100, the most recent).
For each commit it checks the content of the file AT THAT COMMIT (not the
diff) - unlike step 9 there is no "PR" diff from which to isolate only the
added lines, so the snippet is compared with the entire state of the file at
that moment.

We do NOT anchor the search with `since=<date of the known commit>`: that date
is often just the ref/HEAD seen by the code search at the time of the
scan (step 1), not the last commit that actually touched the file -
it may be more recent than the real last modification. Verified with a
concrete case: known commit dated June, last real modification to the file
in April - using June as `since` would have lost all the real
history. We still record link_date (date of the known commit
from the artifact) and found_date (date of the commit in which the snippet was
found, if found) in the output, so the "when" stays
precisely documented even without a temporal anchor in the query.

Causality filter - TRIED AND WITHDRAWN: the idea (a commit earlier than
link_date cannot be adoption, because the link did not exist yet)
is correct in principle and is already applied correctly
elsewhere (created_at of issues/PRs, a reliable datum). But for
GitHubCodeFile rows, commit_sha (and hence link_date) is NOT "the commit in which
the link appeared in the file" - it is only the HEAD of the default branch at
the time of the original scan, disconnected from the true history of that
specific path (same root cause as the bug discovered at step 97 - a
commit_sha that turned out to have modified a completely different file). Verified by
implementing the filter: it discarded 100% of the history in a test on
15 rows - the premise does not hold for this type of artifact. No
date filter here (an honest limit remains, not an arbitrary exclusion).

Writes out/snippet_codefile_commits.csv (new file, does not touch the other
outputs).
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
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_github_cache import DiskCache


def _parse_iso(date_str):
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception:
        return None

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


def _fragment(code, n_chars=200):
    norm = _normalize(code)
    if len(norm) <= n_chars:
        return norm
    start = max(0, (len(norm) - n_chars) // 2)
    return norm[start:start + n_chars]


_GENERIC_LINE_RE = re.compile(
    r'^\s*(#\s*include\b|#\s*define\b|import\s|from\s+\S+\s+import\b|'
    r'using\s+namespace\b|package\s+[\w.]+;?\s*$|use\s+\S+;?\s*$|'
    r'\}?\s*catch\s*\([^)]*\)\s*\{?\s*$)',
)


def is_generic_line(line):
    return bool(_GENERIC_LINE_RE.match(line.strip()))


def _lines_normalized(code, min_len=30):
    out = []
    for l in (code or "").splitlines():
        if is_generic_line(l):
            continue
        n = _normalize(l)
        if len(n) >= min_len:
            out.append(n)
    return out


def snippet_in_text(code, text):
    if not code or not text:
        return False
    text_n = _normalize(text)
    if _fragment(code) in text_n:
        return True
    return any(line in text_n for line in _lines_normalized(code))


def commit_date(repo, sha, cache):
    """Date of the commit (author date, ISO 8601) - used as 'link_date':
    the moment when the file already contained the link to the chat, the starting
    point for the history (the code cannot have been adopted
    BEFORE the link itself existed)."""
    key = ("commit_date", repo, sha)
    if key in cache:
        return cache[key]
    date = None
    try:
        detail = _request(f"{GITHUB_API}/repos/{repo}/commits/{sha}", allow_404=True)
        if detail:
            date = (detail.get("commit", {}).get("author", {}) or {}).get("date") \
                or (detail.get("commit", {}).get("committer", {}) or {}).get("date")
    except Exception:
        pass
    cache[key] = date
    return date


def file_commit_history(repo, path, cache, max_commits):
    """The last `max_commits` commits that touched this file (most
    recent first), with the date of each. We do NOT filter with `since=`
    on the date of the artifact's known commit: that date is often just
    the ref/HEAD seen by the code search at the time of the scan, not the
    last commit that actually touched the file - it may be more
    recent than the real last modification, and using it as `since` would lose real
    history (verified: a case with last real modification in April
    but known commit dated June). We still record the date of each
    checked commit, so the "when" stays documented
    precisely even without a temporal anchor in the query."""
    key = ("history", repo, path, max_commits)
    if key in cache:
        return cache[key]
    out = []
    try:
        data = _request(
            f"{GITHUB_API}/repos/{repo}/commits?path={urllib.parse.quote(path)}&per_page={min(max_commits, 100)}",
            allow_404=True) or []
        for c in data[:max_commits]:
            sha = c.get("sha")
            if not sha:
                continue
            date = (c.get("commit", {}).get("author", {}) or {}).get("date") \
                or (c.get("commit", {}).get("committer", {}) or {}).get("date")
            out.append((sha, date))
    except Exception:
        pass
    cache[key] = out
    return out


def fetch_file_at_ref(repo, path, ref, cache):
    key = ("file_at_ref", repo, path, ref)
    if key in cache:
        return cache[key]
    try:
        meta = _request(
            f"{GITHUB_API}/repos/{repo}/contents/{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref)}",
            allow_404=True)
        cache[key] = base64.b64decode(meta["content"]).decode("utf-8", "ignore") if meta and "content" in meta else None
    except Exception:
        cache[key] = None
    return cache[key]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    ap.add_argument("--limit", type=int, default=None,
                     help="recheck only the first N candidate rows (useful for quick tests)")
    ap.add_argument("--max-commits", type=int, default=100,
                     help="maximum number of commits checked per file since the link appeared (default 100)")
    args = ap.parse_args()

    if not POOL.has_auth():
        print("WARNING: no GITHUB_TOKEN - 60 requests/hour.")

    snippet_path = os.path.join(args.out_dir, "snippet.csv")
    prov_path = os.path.join(args.out_dir, "snippet_provenance.csv")
    sim_path = os.path.join(args.out_dir, "snippet_similarity.csv")
    out_path = os.path.join(args.out_dir, "snippet_codefile_commits.csv")

    with open(snippet_path, encoding="utf-8", newline="") as f:
        snippet_rows = {r["row_id"]: r for r in csv.DictReader(f)}
    with open(prov_path, encoding="utf-8", newline="") as f:
        prov_rows = list(csv.DictReader(f))
    with open(sim_path, encoding="utf-8", newline="") as f:
        sim_rows = {r["row_id"]: r for r in csv.DictReader(f)}

    candidates = []
    for r in prov_rows:
        if r["artifact_type"] != "code_file":
            continue
        if r["snippet_today"] == "yes":
            continue
        s = sim_rows.get(r["row_id"])
        if s and s["verdict"] == "probable_yes":
            continue
        candidates.append(r)
    print(f"Candidates (code_file not found by step 7 nor 8): {len(candidates)}")
    if args.limit:
        candidates = candidates[:args.limit]
        print(f"(limited to {len(candidates)} for --limit)")

    cache = DiskCache(os.path.join(args.out_dir, "github_cache.pkl"))
    results = []
    for i, r in enumerate(candidates, 1):
        cache.save_every(500, i)  # periodic save - a 37h run must not lose everything because of a crash
        row = snippet_rows.get(r["row_id"])
        if row is None:
            continue
        repo, path = r["repo"], r["path"]
        link_date = commit_date(repo, r["commit_sha"], cache) if r.get("commit_sha") else None
        history = file_commit_history(repo, path, cache, args.max_commits)
        link_chat = r["conversation_url"]

        # Causality filter (v2, replaces a first attempt by date that was
        # withdrawn - see below): the link to the chat is a verifiable fact in the
        # content of the file, not an approximate date. But it is not enough to
        # require the link and the snippet to be in the SAME commit: a
        # developer may paste the code first and add the link elsewhere in the file
        # only in a later commit (or vice versa) - that is fine anyway, as long as the link appeared
        # BY that point in the history or after. We walk the history only once (from the most
        # recent to the oldest) keeping track of both signals:
        # - candidates_snippet: every commit where the text contains the snippet
        # - oldest_commit_with_link: the oldest commit (going down)
        #   in which the link is still present in the file - the causality
        #   threshold (no commit earlier than this can be
        #   linked to the conversation)
        # The valid match is the most recent snippet candidate whose date
        # is not earlier than that threshold. No extra network calls:
        # the content is downloaded anyway for the snippet check.
        #
        # [withdrawn] a first attempt discarded commits with date <
        # link_date (the date of the known commit_sha) - it turned out that commit_sha
        # is only the HEAD of the branch at the time of the original scan,
        # disconnected from the true history of the path (same root cause as the bug
        # discovered at step 97): it discarded 100% of the history in a
        # test on 15 rows.
        candidates_snippet = []
        oldest_commit_with_link = None
        for sha, date in history:
            text = fetch_file_at_ref(repo, path, sha, cache)
            if not text:
                continue
            if link_chat and link_chat in text:
                oldest_commit_with_link = (sha, date)  # updated going down - the last one is the oldest
            if snippet_in_text(row["response_code"], text):
                candidates_snippet.append((sha, date))

        threshold = _parse_iso(oldest_commit_with_link[1]) if oldest_commit_with_link else None
        found_sha, found_data = None, None
        n_discarded_causality = 0
        for sha, date in candidates_snippet:  # already ordered from the most recent
            d = _parse_iso(date)
            if threshold is None or d is None or d >= threshold:
                found_sha, found_data = sha, date
                break
            n_discarded_causality += 1

        out = {
            "row_id": r["row_id"],
            "conversation_url": r["conversation_url"],
            "repo": repo,
            "path": path,
            "link_date": link_date or "",
            "n_commits_checked": len(history),
            "n_matches_discarded_before_link": n_discarded_causality,
            "found_in_history": "yes" if found_sha else "no",
            "commit_sha": found_sha or "",
            "found_date": found_data or "",
        }
        results.append(out)
        print(f"  [{i}/{len(candidates)}] {repo}/{path} ({len(history)} commit, "
              f"{n_discarded_causality} matches discarded for causality) -> {out['found_in_history']}"
              + (f" @ {found_sha[:8]} ({found_data})" if found_sha else ""))

    cache.save()
    # ALWAYS writes the file, even if empty (header only) - 0 candidates is a
    # legitimate outcome, not an error: downstream scripts (11/12) can open it
    # unconditionally - same real bug already fixed in
    # 2_02_chat_similarity_recheck.py/2_03_chat_pr_commits_recheck.py, found while testing
    # the 7-12 chain on a small sample.
    fieldnames = ["row_id", "conversation_url", "repo", "path", "link_date",
                  "n_commits_checked", "n_matches_discarded_before_link",
                  "found_in_history", "commit_sha", "found_date"]
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(results)

    print(f"\nWritten {out_path} ({len(results)} rows)")
    print(f"found_in_history: {dict(Counter(r['found_in_history'] for r in results))}")


if __name__ == "__main__":
    main()
