#!/usr/bin/env python
"""
Step 7 (optional, --track-snippets in run_chat_pipeline.sh) - What happens to the
snippet in the real repo.

Standalone: no imports from other scripts of the project. Usage:
    python pipeline/2_01_chat_track_snippets.py --limit 20
    python pipeline/2_01_chat_track_snippets.py                    # all traceable rows

Each row of snippet.csv has a list of artifacts (issue/PR/code file that
cite the link to the conversation - see 1_01_chat_discover_links.py). The type of
artifact determines how much can be verified, in order of reliability:

  GitHubCodeFile (repo+path+commit sha already known from the code search)
    1. the snippet is really in the file at that commit (the file cited the
       link, it does not necessarily contain the code too) -> snippet_in_file
    2. that commit is reachable from the default branch, i.e. merged
       -> in_main
    3. the file still exists today on the default branch, and if so whether
       the snippet is still there -> file_today / snippet_today

  GitHubPullRequest (repo+PR number)
    1. the files changed by the PR (via the API, not the diff text) are
       inspected to find the snippet -> snippet_in_file, path
    2. the PR API directly says whether it was merged -> in_main
    3. if merged, as above -> file_today / snippet_today

  GitHubIssue (discussion only, no known file/commit)
    No starting point - ONLY the whole-repo search is attempted (below).

In all 3 cases, if the snippet does not appear in the known place (or there is
no known place), the WHOLE repo is searched (default branch, via GitHub
code search) before concluding that it has disappeared - the known file/PR is
often just the one that cited the link, the code may have been
pasted elsewhere -> found_elsewhere_in_repo (paths found, "none",
or "not_verifiable" if the snippet has no suitable line to search or the
query fails; can be disabled with --no-search-repo). For GitHubIssue
rows this search IS the only signal available, so it is much
more frequent and the run becomes much slower.

Rows whose only artifact has no usable repo stay untraceable.

Writes out/snippet_provenance.csv (new file, does not touch snippet.csv).
"""
import argparse
import base64
import csv
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


# --------------------------------------------------------------------------
# Token pool - same minimal logic as 1_01_chat_discover_links.py, copied (not
# imported) because every script of the pipeline also runs standalone.
# --------------------------------------------------------------------------
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
    """Percent-encode characters that would break urlsplit/quote: a literal '#'
    in a file path (e.g. 'Laboratory#4.md') would be read by urlsplit as
    the start of a fragment, truncating path/query; an isolated '%'
    not followed by 2 hexadecimal digits produces an invalid URL."""
    url = url.replace("#", "%23")
    url = _PCT_RE.sub("%25", url)
    sp = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(sp.path, safe="/%:@")
    query = urllib.parse.quote(sp.query, safe="=&%:@/?+")
    return urllib.parse.urlunsplit((sp.scheme, sp.netloc, path, query, sp.fragment))


def _request(url, allow_404=False, allow_422=False):
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
            if e.code == 422 and allow_422:
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


# --------------------------------------------------------------------------
# Content comparison: normalizes whitespace before searching for the snippet
# inside the file, so it does not fail because of different indentation/newlines.
# --------------------------------------------------------------------------
def code_joined(raw):
    """In the rebuilt ago2026 dataset `response_code` is sometimes a serialized
    JSON list (the blocks of a step response, like step_snippets)
    instead of plain code - searched that way, with JSON escapes (\\n, \\"),
    it never matches a file. Joins the blocks into plain text."""
    raw = (raw or "").strip()
    if raw.startswith('["') or raw.startswith("['"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return "\n\n".join(str(x) for x in parsed if isinstance(x, str))
        except Exception:
            pass
    return raw


def _normalize(text):
    return re.sub(r"\s+", " ", text).strip()


def containment_ratio(snippet, text):
    """How much of the snippet is found (in blocks) in today's text -
    same metric as step 8, used here to choose the MOST
    PLAUSIBLE file among the files changed by a PR when none contains
    the snippet verbatim (see track_row_pr)."""
    import difflib
    sa, sb = _normalize(snippet or ""), _normalize(text or "")
    if not sa or not sb:
        return 0.0
    sm = difflib.SequenceMatcher(None, sa, sb, autojunk=True)
    matched = sum(bl.size for bl in sm.get_matching_blocks())
    return matched / min(len(sa), len(sb))


def _fragment(code, n_chars=200):
    """A central fragment of the snippet, long enough to be
    specific but short enough to tolerate small changes elsewhere
    in the file."""
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
    """Lines that recur everywhere regardless of the specific content
    (single imports, macros, include headers, generic catch blocks) -
    a match ONLY on one of these is not evidence that the generated code
    was adopted, it is boilerplate present anyway in the file/repo."""
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
    # Fallback: if the central fragment does not match because another
    # part of the snippet was touched, it is enough that ONE sufficiently
    # specific line of the original code is still present verbatim.
    return any(line in text_n for line in _lines_normalized(code))


def snippet_in_text_not_from_prompt(code, text, prompt):
    """Like snippet_in_text, but a fragment/line already present in the prompt does
    not count: if the user pasted their own file, those lines
    match the file because they were already there, not because the response
    was adopted (verified case: row 49 of out_ago2026, a Java class
    pasted into the prompt and found 'verbatim' in the repo)."""
    if not code or not text:
        return False
    text_n, prompt_n = _normalize(text), _normalize(prompt or "")
    frag = _fragment(code)
    if frag in text_n and frag not in prompt_n:
        return True
    return any(line in text_n and line not in prompt_n for line in _lines_normalized(code))


def without_rows_of_prompt(code, prompt):
    """The code without the lines that already appear in the prompt - for a
    containment ratio that measures only the part generated by the model."""
    prompt_n = _normalize(prompt or "")
    return "\n".join(r for r in (code or "").splitlines()
                     if not _normalize(r) or _normalize(r) not in prompt_n)


SEARCH_API = f"{GITHUB_API}/search/code"
_LAST_SEARCH_AT = [0.0]


def pick_search_line(code, min_len=45, max_len=90):
    """The longest possible substring, within a single line, that
    contains no quotes/backslashes (GitHub search treats the text
    between quotes as an exact phrase) and is not generic boilerplate (see
    is_generic_line - a common macro/import/catch recurs throughout the
    repo regardless of the conversation, and is an almost guaranteed false
    positive for a whole-repo search). min_len raised to 45 (from the
    original 20): the audit sample showed that shorter lines
    match too often by coincidence even if they are not generic."""
    best = None
    for raw in (code or "").splitlines():
        line = raw.strip()
        if not line or is_generic_line(line):
            continue
        for part in re.split(r'["\\]', line):
            part = part.strip()
            if len(part) >= min_len and (best is None or len(part) > len(best)):
                best = part
    if best is None:
        return None
    return best[:max_len]


def search_repo_for_snippet(repo, code):
    """Searches for the snippet in the WHOLE repo (default branch, via GitHub's
    code search index). Returns the list of paths where it was
    found, [] if the search succeeded but found nothing, or None if
    it was not possible (unsuitable line, or the query is not valid for
    the code search API)."""
    line = pick_search_line(code)
    if not line:
        return None
    q = f'"{line}" repo:{repo}'
    url = f"{SEARCH_API}?{urllib.parse.urlencode({'q': q, 'per_page': 10})}"

    # code search has a much tighter limit (~30/min with a token) than
    # the core one (5000/h) - a short pause between one search and the next
    # avoids constantly running into it
    wait = 2.2 - (time.time() - _LAST_SEARCH_AT[0])
    if wait > 0:
        time.sleep(wait)
    _LAST_SEARCH_AT[0] = time.time()

    res = _request(url, allow_422=True)
    if res is None:
        return None
    return sorted({it["path"] for it in res.get("items", [])})


def get_default_branch(repo):
    meta = _request(f"{GITHUB_API}/repos/{repo}")
    return meta["default_branch"]


def cached_default_branch(repo, cache):
    key = ("repo_default_branch", repo)
    if key not in cache:
        try:
            cache[key] = get_default_branch(repo)
        except Exception as e:
            cache[key] = f"error: {e}"
    return cache[key]


def sha_in_default_branch(repo, sha, default_branch):
    """True if `sha` is an ancestor of the default branch (i.e. it was
    merged, or it is the default branch itself)."""
    cmp = _request(f"{GITHUB_API}/repos/{repo}/compare/{default_branch}...{sha}",
                    allow_404=True)
    if cmp is None:
        return "error"
    status = cmp.get("status")
    if status in ("identical", "behind"):
        return "yes"
    return "no"


def fetch_file_at_ref(repo, path, ref, cache):
    """Content of a file at a specific ref (sha or branch name), cached
    per (repo, path, ref) - many different rows often share the
    same file or the same PR."""
    key = ("file_at_ref", repo, path, ref)
    if key in cache:
        return cache[key]
    try:
        meta = _request(
            f"{GITHUB_API}/repos/{repo}/contents/{path}?ref={urllib.parse.quote(str(ref))}",
            allow_404=True)
        if meta is None or "content" not in meta:
            cache[key] = None
        else:
            cache[key] = base64.b64decode(meta["content"]).decode("utf-8", "ignore")
    except Exception:
        cache[key] = None
    return cache[key]


def search_fallback(repo, code, cache, search_repo, already_found):
    """Whole-repo search, common to all 3 artifact types: if the
    snippet has not already been found at the known place (already_found),
    search the whole repo before concluding that it has disappeared."""
    if already_found:
        return "not_searched"
    if not search_repo:
        return "not_searched"
    try:
        found = search_repo_for_snippet(repo, code)
    except Exception as e:
        return f"error: {e}"
    if found is None:
        return "not_verifiable"
    return ";".join(found) if found else "none"


# --------------------------------------------------------------------------
# Case 1: GitHubCodeFile artifact (repo+path+commit sha already known)
# --------------------------------------------------------------------------
def file_level_facts(repo, path, sha, cache):
    """Facts that depend only on (repo, path, sha), not on the snippet
    of the single row - cached to avoid repeated requests when several
    rows share the same artifact file (e.g. a README listing
    dozens of different conversations)."""
    key = ("codefile_facts", repo, path, sha)
    if key in cache:
        return cache[key]

    facts = {"text_at_sha": fetch_file_at_ref(repo, path, sha, cache)}

    default_branch = cached_default_branch(repo, cache)
    facts["default_branch"] = default_branch
    if isinstance(default_branch, str) and default_branch.startswith("error"):
        facts["in_main"] = "error"
        facts["text_today"] = None
        cache[key] = facts
        return facts

    try:
        facts["in_main"] = sha_in_default_branch(repo, sha, default_branch)
    except Exception as e:
        facts["in_main"] = f"error: {e}"

    facts["text_today"] = fetch_file_at_ref(repo, path, default_branch, cache)

    cache[key] = facts
    return facts


def track_row_codefile(row, art, cache, search_repo=True):
    repo, path, sha = art["repo"], art["path"], art["reference"]
    out = {
        "row_id": row["row_id"],
        "conversation_url": row["conversation_url"],
        "repo": repo,
        "artifact_type": "code_file",
        "path": path,
        "commit_sha": sha,
        "artifact_url": art["url"],
    }

    facts = file_level_facts(repo, path, sha, cache)

    text_at_sha = facts.get("text_at_sha")
    if text_at_sha is None:
        out["snippet_in_file"] = "error"
    else:
        out["snippet_in_file"] = "yes" if snippet_in_text(row["response_code"], text_at_sha) else "no"

    out["default_branch"] = facts.get("default_branch")
    out["in_main"] = facts.get("in_main")

    text_today = facts.get("text_today")
    if text_today is None:
        out["file_today"] = "no"
        out["snippet_today"] = "file_missing"
    else:
        out["file_today"] = "yes"
        if snippet_in_text(row["response_code"], text_today):
            out["snippet_today"] = "yes"
        elif _normalize(text_today) == _normalize(text_at_sha or ""):
            out["snippet_today"] = "no"
        else:
            out["snippet_today"] = "modified"

    out["found_elsewhere_in_repo"] = search_fallback(
        repo, row["response_code"], cache, search_repo,
        already_found=(out["snippet_today"] == "yes"))

    return out


# --------------------------------------------------------------------------
# Case 2: GitHubPullRequest artifact (repo+number, no known path/sha -
# they must be retrieved from the PR API, which also says whether it was merged
# without needing the compare/... bisection used for isolated commits)
# --------------------------------------------------------------------------
def pr_meta_and_files(repo, number, cache):
    key = ("pr_meta", repo, number)
    if key in cache:
        return cache[key]

    facts = {}
    try:
        pr = _request(f"{GITHUB_API}/repos/{repo}/pulls/{number}", allow_404=True)
        if pr is None:
            facts["error"] = "PR not found (invalid number or renamed repo)"
        else:
            facts["merged"] = bool(pr.get("merged"))
            facts["head_sha"] = pr["head"]["sha"]
            base_repo = (pr.get("base") or {}).get("repo") or {}
            facts["default_branch"] = base_repo.get("default_branch")

            files, page = [], 1
            while page <= 3:  # up to 300 files: beyond that is a rare/anomalous case
                batch = _request(
                    f"{GITHUB_API}/repos/{repo}/pulls/{number}/files?per_page=100&page={page}",
                    allow_404=True) or []
                files.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
            facts["files"] = files
    except Exception as e:
        facts["error"] = str(e)

    cache[key] = facts
    return facts


def track_row_pr(row, art, cache, search_repo=True):
    repo, number = art["repo"], art["reference"]
    out = {
        "row_id": row["row_id"],
        "conversation_url": row["conversation_url"],
        "repo": repo,
        "artifact_type": "pull_request",
        "path": None,
        "commit_sha": None,
        "artifact_url": art["url"],
    }

    facts = pr_meta_and_files(repo, number, cache)
    if facts.get("error"):
        out.update(default_branch=None, in_main="error", snippet_in_file="error",
                    file_today="error", snippet_today="error",
                    found_elsewhere_in_repo="not_verifiable")
        return out

    out["commit_sha"] = facts["head_sha"]
    out["default_branch"] = facts.get("default_branch")
    out["in_main"] = "yes" if facts["merged"] else "no"

    # FIX (before: if no file of the PR contained the snippet verbatim, the path
    # was assigned to the FIRST file in the list, regardless of any
    # relation with the snippet - a bug discovered by manually reading a sample
    # at step 91: it compared e.g. a prompt about a React component with a
    # CI workflow file. Now: if there is no verbatim match, we choose the file
    # with the highest similarity (containment ratio, same metric as step 8)
    # among ALL the files of the PR - if even the best one is below the threshold,
    # the path stays None (an honest "we don't know") instead of a random file.
    THRESHOLD_SIMILARITY_PLAUSIBLE = 0.25  # raised from 0.15 after a borderline case (ratio 0.18)
    # found weak/uncertain in the precision test on 30 rows (same family of
    # conversations about the MQTT library already flagged as noisy at step 80)
    match_path = None
    method_match = None
    best_path, best_ratio = None, 0.0
    for fmeta in facts.get("files", []):
        path = fmeta.get("filename")
        if not path:
            continue
        text = fetch_file_at_ref(repo, path, facts["head_sha"], cache)
        if not text:
            continue
        if snippet_in_text(row["response_code"], text):
            match_path = path
            method_match = "verbatim"
            break
        ratio = containment_ratio(row["response_code"], text)
        if ratio > best_ratio:
            best_ratio, best_path = ratio, path

    if not match_path and best_ratio >= THRESHOLD_SIMILARITY_PLAUSIBLE:
        match_path = best_path
        method_match = f"similarity_{best_ratio:.2f}"

    out["path"] = match_path  # None if no file is plausible - no longer an arbitrary fallback
    out["path_method"] = method_match or ""
    out["snippet_in_file"] = "yes" if method_match == "verbatim" else "no"

    if facts["merged"] and out["default_branch"] and match_path:
        text_today = fetch_file_at_ref(repo, match_path, out["default_branch"], cache)
        if text_today is None:
            out["file_today"] = "no"
            out["snippet_today"] = "file_missing"
        else:
            out["file_today"] = "yes"
            out["snippet_today"] = "yes" if snippet_in_text(row["response_code"], text_today) else "modified"
    else:
        # PR not merged (or merged but without a direct match): "today" does not
        # apply in the same way, the code is not necessarily in the default branch
        out["file_today"] = "n/a"
        out["snippet_today"] = "n/a"

    out["found_elsewhere_in_repo"] = search_fallback(
        repo, row["response_code"], cache, search_repo,
        already_found=(out["snippet_today"] == "yes" or out["snippet_in_file"] == "yes"))

    return out


# --------------------------------------------------------------------------
# Case 3: GitHubIssue-only artifact - no known file/commit/PR, the only
# possible signal is the direct whole-repo search.
# --------------------------------------------------------------------------
def track_row_issue(row, art, cache, search_repo=True):
    repo = art["repo"]
    out = {
        "row_id": row["row_id"],
        "conversation_url": row["conversation_url"],
        "repo": repo,
        "artifact_type": "issue",
        "path": None,
        "commit_sha": None,
        "artifact_url": art["url"],
        "snippet_in_file": "n/a",
        "default_branch": cached_default_branch(repo, cache),
        "in_main": "n/a",
    }

    found_raw = search_fallback(repo, row["response_code"], cache, search_repo,
                                   already_found=False)
    out["found_elsewhere_in_repo"] = found_raw
    if found_raw not in ("none", "not_searched", "not_verifiable") and not found_raw.startswith("error"):
        out["path"] = found_raw.split(";")[0]
        out["file_today"] = "yes"
        out["snippet_today"] = "yes"
    elif found_raw == "none":
        out["file_today"] = "n/a"
        out["snippet_today"] = "no"
    else:
        out["file_today"] = "n/a"
        out["snippet_today"] = "n/a"

    return out


# --------------------------------------------------------------------------
# Dispatcher: prefers the strongest signal available for each row
# (CodeFile > PullRequest > Issue).
# --------------------------------------------------------------------------
def track_row(row, cache, search_repo=True):
    try:
        artifacts = json.loads(row["artifacts"])
    except Exception:
        return None

    by_type = {}
    for a in artifacts:
        by_type.setdefault(a.get("type"), []).append(a)

    code_arts = [a for a in by_type.get("GitHubCodeFile", [])
                 if a.get("path") and a.get("reference") and a.get("repo")]
    if code_arts:
        return track_row_codefile(row, code_arts[0], cache, search_repo)

    pr_arts = [a for a in by_type.get("GitHubPullRequest", [])
               if a.get("reference") and a.get("repo")]
    if pr_arts:
        return track_row_pr(row, pr_arts[0], cache, search_repo)

    issue_arts = [a for a in by_type.get("GitHubIssue", []) if a.get("repo")]
    if issue_arts:
        return track_row_issue(row, issue_arts[0], cache, search_repo)

    return None  # no usable repo


FIELDNAMES = ["row_id", "conversation_url", "repo", "artifact_type", "path", "path_method", "commit_sha",
              "artifact_url", "snippet_in_file", "default_branch", "in_main",
              "file_today", "snippet_today", "found_elsewhere_in_repo"]


def write_results(out_path, results):
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        w.writerows(results)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    ap.add_argument("--limit", type=int, default=None,
                     help="track only the first N traceable rows (useful for quick tests)")
    ap.add_argument("--no-search-repo", action="store_true",
                     help="skip the whole-repo search when the snippet is not at the known place "
                          "(much faster, but GitHubIssue rows stay completely untraceable)")
    ap.add_argument("--save-every", type=int, default=1000,
                     help="write a partial save every N NEW traced rows (protection against "
                          "a crash/shutdown during a long run) - default 1000")
    args = ap.parse_args()

    if not POOL.has_auth():
        print("WARNING: no GITHUB_TOKEN - 60 requests/hour, this step consumes them quickly.")

    snippet_path = os.path.join(args.out_dir, "snippet.csv")
    out_path = os.path.join(args.out_dir, "snippet_provenance.csv")

    with open(snippet_path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    print(f"Total rows in snippet.csv: {len(rows)}")

    # Resume from a previous partial save (crash/shutdown halfway
    # through): rows already present in out_path (by row_id) are not
    # traced again. Without this, an interruption in a 36+ hour run
    # would lose all the work done up to that point - here instead we
    # restart from where we left off.
    results = []
    already_done = set()
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8", newline="") as f:
            results = list(csv.DictReader(f))
        already_done = {r["row_id"] for r in results if r.get("row_id")}
        print(f"Resumed from partial save: {len(results)} rows already traced, skipped.")

    tracked_new = 0
    cache = {}
    for row in rows:
        if row.get("row_id") in already_done:
            continue
        if args.limit and tracked_new >= args.limit:
            break
        r = track_row(row, cache, search_repo=not args.no_search_repo)
        if r is None:
            continue
        tracked_new += 1
        results.append(r)
        print(f"  [{len(results)}] ({r['artifact_type']}) {r['repo']}/{r.get('path')} "
              f"snippet_in_file={r.get('snippet_in_file')} "
              f"in_main={r.get('in_main')} "
              f"file_today={r.get('file_today')} "
              f"snippet_today={r.get('snippet_today')} "
              f"found_elsewhere={r.get('found_elsewhere_in_repo')}")
        if args.save_every and tracked_new % args.save_every == 0:
            write_results(out_path, results)

    if not results:
        print("No traceable rows.")
        return

    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        w.writerows(results)

    print(f"\nWritten {out_path} ({len(results)} rows)")
    print(f"by artifact type: {dict(Counter(r['artifact_type'] for r in results))}")
    for field in ("snippet_in_file", "in_main", "file_today", "snippet_today", "found_elsewhere_in_repo"):
        c = Counter(r.get(field) for r in results)
        print(f"{field}: {dict(c)}")


if __name__ == "__main__":
    main()
