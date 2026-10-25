#!/usr/bin/env python
"""
Extracts the harness configuration mechanisms (Context Files, Skills,
Subagents, Commands, Rules, Settings, Hooks, MCP) present in the repos of the
agentic sessions, using the file/directory heuristics of Table 1 of
Galster et al. 2026 ("Harness Engineering for Agentic AI Coding Tools").

For each distinct repo in out_vibe_coding/conversations.jsonl, downloads
ONLY ONCE the entire file tree of the default branch (Git Trees API,
recursive=1) and compares the paths with the heuristics - no need to clone the
repos nor to make a call for each possible file.

Known limits (prototype, not as detailed as the paper):
  - detects the PRESENCE of the path, not the content - does not distinguish (without
    reading the file) Hooks/MCP when they share the same Settings file
    (e.g. .claude/settings.json may contain hooks; .codex/
    config.toml and .gemini/settings.json may also contain MCP) -
    these cases are marked as "ambiguous" in settings_ambiguous, not
    forced into yes/no.
  - CURRENT state of the repo (default branch today), not the one at the time
    of the session - a repo may have added the harness afterwards.
  - repos no longer reachable (404/private/renamed) are marked
    found=no, consistent with the friction already seen in the other
    adoption chains.

Usage:
    python pipeline/3_01_agentic_extract_harness.py
    python pipeline/3_01_agentic_extract_harness.py --limit 50      # quick test
    python pipeline/3_01_agentic_extract_harness.py --concurrency 16
"""
import argparse
import csv
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib_github_cache import DiskCache

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AGENTIC_DIR = os.path.join(SCRIPT_DIR, "out_vibe_coding")
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


import threading

MAX_WAIT_S = 90  # safety cap: an X-RateLimit-Reset read on a 403 of a
                  # SECONDARY rate-limit (abuse detection, not the primary
                  # 5000/h limit) may still report the reset of the primary limit,
                  # up to an hour in the future - without this cap a single
                  # thread can stay blocked for an hour (seen live: process stopped,
                  # 0% CPU, no progress)


class TokenPool:
    def __init__(self, tokens):
        self.tokens = tokens or [None]
        self.i = 0
        self.blocked_until = {}
        self.lock = threading.Lock()

    def has_auth(self):
        return any(t for t in self.tokens)

    def pick(self):
        """Picks (thread-safely) the next unblocked token,
        rotating the shared index - unlike the original version
        (not thread-safe, used with a single worker in the other steps), here
        it runs with several threads in parallel."""
        with self.lock:
            now = time.time()
            for _ in range(len(self.tokens)):
                self.i = (self.i + 1) % len(self.tokens)
                tok = self.tokens[self.i]
                if self.blocked_until.get(tok, 0) <= now:
                    return tok
            soonest = min(self.blocked_until.values())
            return None, max(0, soonest - now)

    def mark_blocked(self, tok, until_epoch):
        with self.lock:
            self.blocked_until[tok] = min(until_epoch, time.time() + MAX_WAIT_S)


POOL = TokenPool(load_tokens())


def _headers_for(tok):
    h = dict(BASE_HEADERS)
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _request(url, allow_404=True, with_headers=False):
    for attempt in range(6):
        picked = POOL.pick()
        if isinstance(picked, tuple):
            # no free token at the moment - wait but with a cap
            time.sleep(min(picked[1], MAX_WAIT_S) + 1)
            continue
        tok = picked
        req = urllib.request.Request(url, headers=_headers_for(tok))
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                body = json.load(r)
                rem = r.headers.get("X-RateLimit-Remaining")
                if rem is not None and int(rem) <= 1:
                    reset = int(r.headers.get("X-RateLimit-Reset", "0"))
                    POOL.mark_blocked(tok, reset)
                return (body, dict(r.headers)) if with_headers else body
        except urllib.error.HTTPError as e:
            if e.code == 404 and allow_404:
                return (None, {}) if with_headers else None
            if e.code in (403, 429):
                # Retry-After before X-RateLimit-Reset: it is the right header
                # for the SECONDARY rate-limit (too many concurrent requests
                # to "expensive" endpoints like git/trees?recursive=1), which is
                # the one that triggers here - X-RateLimit-Reset instead reflects
                # the hourly primary limit and is misleading in this case.
                retry_after = e.headers.get("Retry-After")
                reset = e.headers.get("X-RateLimit-Reset")
                if retry_after:
                    POOL.mark_blocked(tok, time.time() + int(retry_after))
                elif reset:
                    POOL.mark_blocked(tok, int(reset))
                else:
                    POOL.mark_blocked(tok, time.time() + 20 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException, ConnectionError):
            # RemoteDisconnected (subclass of HTTPException) and similar are
            # transient network hiccups, not URL errors - seen live
            # at scale (thousands of requests): without this except
            # a single hiccup crashes the whole script instead of
            # retrying.
            time.sleep(5)
    raise RuntimeError(f"giving up on {url}")


# ------------------------------------------------ heuristics (Table 1)
CONTEXT_FILENAMES = {"claude.md", "agents.md", "agents.override.md", "gemini.md", ".cursorrules"}
CONTEXT_EXTRA_RE = re.compile(r"^\.github/(copilot-instructions\.md|instructions/.*\.md)$", re.I)

SKILLS_PREFIXES = (".claude/skills/", ".github/skills/", ".codex/skills/", ".cursor/skills/", ".gemini/skills/")
SUBAGENTS_PREFIXES = (".claude/agents/", ".github/agents/", ".cursor/agents/")
COMMANDS_PREFIXES = (".claude/commands/", ".cursor/commands/", ".gemini/commands/")
RULES_PREFIXES = (".codex/rules/", ".cursor/rules/")

# Unambiguous files (only one possible mechanism given the path)
HOOKS_UNAMBIGUOUS = re.compile(r"^(\.github/hooks/.*\.json|\.cursor/hooks\.json)$", re.I)
MCP_UNAMBIGUOUS = {".mcp.json", ".cursor/mcp.json"}

# "Container" files where Settings/Hooks/MCP can coexist in the same
# file - the presence alone is not enough to distinguish them (the
# content would have to be read, out of scope for this prototype)
SETTINGS_AMBIGUOUS = {
    ".claude/settings.json", ".claude/settings.local.json",
    ".codex/config.toml", ".cursor/cli.json",
    ".gemini/settings.json", ".gemini/config.yaml",
}


def classify(paths):
    out = {"context_files": [], "skills": set(), "subagents": set(), "commands": set(),
           "rules": set(), "hooks": [], "mcp": [], "settings_ambiguous": []}
    for p in paths:
        pl = p.lower()
        fname = pl.rsplit("/", 1)[-1]
        if fname in CONTEXT_FILENAMES or CONTEXT_EXTRA_RE.match(pl):
            out["context_files"].append(p)
        for pref in SKILLS_PREFIXES:
            if pl.startswith(pref):
                rest = p[len(pref):].split("/", 1)[0]
                out["skills"].add(pref + rest)
                break
        if pl.startswith(SUBAGENTS_PREFIXES) and pl.endswith(".md"):
            out["subagents"].add(p)
        if pl.startswith(COMMANDS_PREFIXES):
            out["commands"].add(p)
        if pl.startswith(RULES_PREFIXES):
            out["rules"].add(p)
        if HOOKS_UNAMBIGUOUS.match(pl):
            out["hooks"].append(p)
        if pl in MCP_UNAMBIGUOUS:
            out["mcp"].append(p)
        if pl in SETTINGS_AMBIGUOUS:
            out["settings_ambiguous"].append(p)
    out["skills"] = sorted(out["skills"])
    out["subagents"] = sorted(out["subagents"])
    out["commands"] = sorted(out["commands"])
    out["rules"] = sorted(out["rules"])
    return out


def load_repo_sessions():
    """repo -> number of agentic sessions that reference it."""
    c = Counter()
    with open(os.path.join(AGENTIC_DIR, "conversations.jsonl"), encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            for a in (rec.get("artifacts") or []):
                if a.get("repo"):
                    c[a["repo"]] += 1
    return c


def process_repo(repo, cache):
    if repo in cache:
        return cache[repo]

    meta = _request(f"{GITHUB_API}/repos/{repo}")
    if meta is None:
        result = {"found": "no", "default_branch": None, "n_files": 0, "truncated": "no",
                     "context_files": [], "skills": [], "subagents": [], "commands": [],
                     "rules": [], "hooks": [], "mcp": [], "settings_ambiguous": []}
        cache[repo] = result
        return result

    branch = meta.get("default_branch") or "main"
    tree = _request(f"{GITHUB_API}/repos/{repo}/git/trees/{urllib.parse.quote(branch)}?recursive=1")
    if tree is None or "tree" not in tree:
        result = {"found": "yes_empty", "default_branch": branch, "n_files": 0, "truncated": "no",
                     "context_files": [], "skills": [], "subagents": [], "commands": [],
                     "rules": [], "hooks": [], "mcp": [], "settings_ambiguous": []}
        cache[repo] = result
        return result

    paths = [e["path"] for e in tree["tree"] if e.get("type") == "blob"]
    classified = classify(paths)
    result = {"found": "yes", "default_branch": branch, "n_files": len(paths),
                 "truncated": "yes" if tree.get("truncated") else "no", **classified}
    cache[repo] = result
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None, help="limit to N repos (test)")
    ap.add_argument("--concurrency", type=int, default=5,
                     help="git/trees?recursive=1 is an 'expensive' endpoint for GitHub - "
                          "too much concurrency triggers the secondary rate-limit")
    ap.add_argument("--out", default=os.path.join(AGENTIC_DIR, "harness_config.csv"))
    args = ap.parse_args()

    print(f"GitHub tokens available: {len(POOL.tokens) if POOL.has_auth() else 0}")
    sessions_per_repo = load_repo_sessions()
    repos = sorted(sessions_per_repo, key=lambda r: -sessions_per_repo[r])
    if args.limit:
        repos = repos[:args.limit]
    print(f"Distinct repos to analyze: {len(repos)}")

    cache = DiskCache(os.path.join(AGENTIC_DIR, "harness_cache.pkl"))
    rows = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {ex.submit(process_repo, repo, cache): repo for repo in repos}
        for fut in as_completed(futures):
            repo = futures[fut]
            r = fut.result()
            n_mech = sum(1 for k in ("context_files", "skills", "subagents", "commands",
                                      "rules", "hooks", "mcp") if r[k])
            rows.append({
                "repo": repo, "n_sessions": sessions_per_repo[repo],
                "found": r["found"], "default_branch": r["default_branch"],
                "n_files": r["n_files"], "truncated": r["truncated"],
                "n_mechanisms": n_mech,
                "context_files": json.dumps(r["context_files"], ensure_ascii=False),
                "skills": json.dumps(r["skills"], ensure_ascii=False),
                "subagents": json.dumps(r["subagents"], ensure_ascii=False),
                "commands": json.dumps(r["commands"], ensure_ascii=False),
                "rules": json.dumps(r["rules"], ensure_ascii=False),
                "hooks": json.dumps(r["hooks"], ensure_ascii=False),
                "mcp": json.dumps(r["mcp"], ensure_ascii=False),
                "settings_ambiguous": json.dumps(r["settings_ambiguous"], ensure_ascii=False),
            })
            done += 1
            if done % 100 == 0:
                print(f"  [{done}/{len(repos)}]")
                cache.save_every(1, 0)

    cache.save()
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
        w.writeheader()
        for r in rows:
            w.writerow(r)

    found = [r for r in rows if r["found"] == "yes"]
    with_mech = [r for r in found if r["n_mechanisms"] > 0]
    print(f"\nWritten {args.out}")
    print(f"Reachable repos: {len(found)}/{len(rows)}")
    print(f"Repos with at least one harness mechanism: {len(with_mech)}")
    for mech in ("context_files", "skills", "subagents", "commands", "rules", "hooks", "mcp"):
        n = sum(1 for r in found if json.loads(r[mech]))
        print(f"  {mech}: {n} repo")
    n_settings_ambiguous = sum(1 for r in found if json.loads(r["settings_ambiguous"]))
    print(f"  settings (ambiguous, file shared with hooks/mcp): {n_settings_ambiguous} repo")


if __name__ == "__main__":
    main()
