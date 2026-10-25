#!/usr/bin/env python
"""
Step 8 (optional) - Similarity recheck of the "not found" snippets.

Standalone: no imports from other scripts of the project. Usage:
    python pipeline/2_02_chat_similarity_recheck.py --limit 50
    python pipeline/2_02_chat_similarity_recheck.py

Step 7 (2_01_chat_track_snippets.py) looks for the snippet VERBATIM (exact substring
after whitespace normalization). If the code was integrated with
modifications - renamed variables, reformatted lines, extended with other
code around it - exact matching does not find it, even if the content is
largely the same.

This step takes up ONLY the rows that:
  - have a GitHubCodeFile or GitHubPullRequest artifact (hence a known
    file, no whole-repo search to redo - unlike step 7 there is no
    tight throttling here, only core API requests);
  - step 7 marked as not found verbatim today (snippet_today
    "no" or "modified") even though the file is still present.

For each one, it re-downloads today's file content (once per file,
cached) and computes a similarity score with difflib.SequenceMatcher:
not the symmetric similarity of the whole file, but how much of the
original SNIPPET is found in blocks in common with the file (containment
ratio) - tolerant to code added/removed around it, less tolerant to
widespread variable renames (those would require semantic matching, not
just textual - see note in docs/METHODOLOGY.md).

Writes out/snippet_similarity.csv (new file, does not touch the other outputs).
"""
import argparse
import base64
import csv
import difflib
import json
import os
import re
import time
import urllib.parse
import urllib.request
import urllib.error
from collections import Counter

csv.field_size_limit(50_000_000)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT_DIR = os.path.join(SCRIPT_DIR, "out")
GITHUB_API = "https://api.github.com"
BASE_HEADERS = {"Accept": "application/vnd.github+json",
                "User-Agent": "devgpt-extend-tracker/1.0",
                "X-GitHub-Api-Version": "2022-11-28"}

THRESHOLD_PROBABLE = 0.55   # ratio >= this -> "probable_yes"
THRESHOLD_WEAK = 0.3       # ratio >= this (and < probable) -> "weak"


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


def fetch_file(repo, path, ref, cache):
    key = (repo, path, ref)
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


def containment_ratio(snippet, text):
    """How much of the snippet (in characters, after whitespace normalization)
    is found in ordered common blocks with the file content - not the
    symmetric similarity of the whole file (which would almost always be low,
    the snippet is much shorter than the file)."""
    s, t = _normalize(snippet), _normalize(text)
    if not s or not t:
        return 0.0
    sm = difflib.SequenceMatcher(None, s, t, autojunk=True)
    matched = sum(block.size for block in sm.get_matching_blocks())
    return matched / len(s)


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
    out_path = os.path.join(args.out_dir, "snippet_similarity.csv")

    with open(snippet_path, encoding="utf-8", newline="") as f:
        snippet_rows = {r["row_id"]: r for r in csv.DictReader(f)}
    with open(prov_path, encoding="utf-8", newline="") as f:
        prov_rows = list(csv.DictReader(f))
    print(f"Rows in snippet_provenance.csv: {len(prov_rows)}")

    candidates = [r for r in prov_rows
                 if r["artifact_type"] in ("code_file", "pull_request")
                 and r["snippet_today"] in ("no", "modified")
                 and r["file_today"] == "yes" and r["path"]]
    print(f"Candidates for similarity recheck: {len(candidates)}")
    if args.limit:
        candidates = candidates[:args.limit]
        print(f"(limited to {len(candidates)} for --limit)")

    cache = {}
    results = []
    for i, r in enumerate(candidates, 1):
        row = snippet_rows.get(r["row_id"])
        if row is None:
            continue
        ref = r["commit_sha"] if r["artifact_type"] == "code_file" else r["default_branch"]
        text = fetch_file(r["repo"], r["path"], ref, cache)
        ratio = containment_ratio(row["response_code"], text) if text else 0.0
        verdict = ("probable_yes" if ratio >= THRESHOLD_PROBABLE
                    else "weak" if ratio >= THRESHOLD_WEAK else "no")
        out = {
            "row_id": r["row_id"],
            "conversation_url": r["conversation_url"],
            "repo": r["repo"],
            "path": r["path"],
            "snippet_today_step7": r["snippet_today"],
            "similarity_ratio": round(ratio, 3),
            "verdict": verdict,
        }
        results.append(out)
        print(f"  [{i}/{len(candidates)}] {r['repo']}/{r['path']} ratio={ratio:.2f} -> {verdict}")

    # ALWAYS writes the file, even if empty (header only) - 0 candidates is a
    # legitimate outcome (e.g. on a small sample, or a dataset with no
    # "not found" code_file/pull_request rows), not an error: a downstream
    # script (2_04_chat_codefile_commits_recheck.py) opens this file
    # unconditionally and would crash if it did not exist at all -
    # real bug discovered while testing the 7-12 chain on a sample of 140 rows.
    fieldnames = ["row_id", "conversation_url", "repo", "path",
                  "snippet_today_step7", "similarity_ratio", "verdict"]
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(results)

    if not results:
        print("No results (file written anyway, header only).")

    print(f"\nWritten {out_path} ({len(results)} rows)")
    print(f"verdict: {dict(Counter(r['verdict'] for r in results))}")


if __name__ == "__main__":
    main()
