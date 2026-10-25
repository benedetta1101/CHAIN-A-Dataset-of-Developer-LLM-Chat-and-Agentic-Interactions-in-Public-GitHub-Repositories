#!/usr/bin/env python
"""
Phase 2 of the harness extraction (after 3_01_agentic_extract_harness.py):
1. Downloads the CONTENT of all the artifacts identified in
   harness_config.csv (Contents API, one file at a time) and saves them
   locally in out_vibe_coding/harness_files/<repo>/<path>.
2. Infers, for each repo, WHICH TOOL generated each artifact
   (e.g. CLAUDE.md/.claude/* -> "Claude Code", .cursor/* -> "Cursor") and
   writes a "harness" column into the main agentic dataset
   (conversations.jsonl), joining via repo (artifacts[].repo field of
   each session).

The "harness" field is therefore a property of the REPO (which configuration
is present NOW on the default branch), not of the single session - a
session on a repo without a configured harness at the time but that has one
today will still have "harness" filled in, with the same limit already
reported in 3_01_agentic_extract_harness.py.

Safety on the main file: it does NOT overwrite conversations.jsonl without a
backup - it writes to a temporary file, verifies that the row count
matches, saves a timestamped backup copy, then replaces it
atomically.

Usage:
    python pipeline/3_02_agentic_download_harness.py
    python pipeline/3_02_agentic_download_harness.py --limit 20   # test
"""
import argparse
import base64
import csv
import importlib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_h1 = importlib.import_module("3_01_agentic_extract_harness")
POOL, _request, GITHUB_API = _h1.POOL, _h1._request, _h1.GITHUB_API

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AGENTIC_DIR = os.path.join(SCRIPT_DIR, "out_vibe_coding")
HARNESS_FILES_DIR = os.path.join(AGENTIC_DIR, "harness_files")
MECH_COLS = ("context_files", "skills", "subagents", "commands", "rules", "hooks", "mcp")


def tool_per_path(path):
    """Maps an artifact path to the tool that defines it, from Table 1 of
    Galster et al. 2026. AGENTS.md is tool-agnostic (Codex introduced
    it first but it is also used by Cursor/Copilot): if it is the
    ONLY artifact of the repo it stays "AGENTS.md (generic)", otherwise
    it still counts as a signal of that particular tool if in a
    specific directory (e.g. .codex/ or inside .cursor/)."""
    pl = path.lower()
    fname = pl.rsplit("/", 1)[-1]  # context files can be nested
                                     # (e.g. "apps/web/AGENTS.md"), not only
                                     # at the root of the repo - we need to look at
                                     # the file name, not the whole path
    if fname == "claude.md" or pl.startswith(".claude/") or pl == ".mcp.json":
        return "Claude Code"
    if fname == "gemini.md" or pl.startswith(".gemini/"):
        return "Gemini"
    if fname == ".cursorrules" or pl.startswith(".cursor/"):
        return "Cursor"
    if pl.startswith(".codex/"):
        return "Codex"
    if pl.startswith(".github/"):
        return "Copilot"
    if fname in ("agents.md", "agents.override.md"):
        return "AGENTS.md (generic)"
    return "Other"


def repo_safe(repo):
    return repo.replace("/", "__")


def download_file(repo, path):
    """Contents API: returns the decoded text, or None if binary/not
    decodable/not found (e.g. file removed after the scan)."""
    url = f"{GITHUB_API}/repos/{repo}/contents/{urllib.parse.quote(path)}"
    body = _request(url, allow_404=True)
    if body is None or not isinstance(body, dict):
        return None
    if body.get("encoding") != "base64" or not body.get("content"):
        return None
    try:
        return base64.b64decode(body["content"]).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def load_rows_harness(limit=None):
    with open(os.path.join(AGENTIC_DIR, "harness_config.csv"), encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if limit:
        rows = rows[:limit]
    return rows


def all_paths(row):
    out = []
    for col in MECH_COLS:
        out.extend(json.loads(row[col]))
    return out


def process_repo(row):
    repo = row["repo"]
    paths = all_paths(row)
    if not paths:
        return repo, [], 0
    tools = set()
    downloaded = 0
    for path in paths:
        tools.add(tool_per_path(path))
        text = download_file(repo, path)
        if text is None:
            continue
        dest = os.path.join(HARNESS_FILES_DIR, repo_safe(repo), path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(text)
        downloaded += 1
    return repo, sorted(tools), downloaded


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None, help="limit to N repos (test)")
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()

    print(f"GitHub tokens available: {len(POOL.tokens) if POOL.has_auth() else 0}")
    rows = load_rows_harness(args.limit)
    with_artifacts = [r for r in rows if all_paths(r)]
    print(f"Total repos: {len(rows)}, with at least one artifact to download: {len(with_artifacts)}")

    harness_per_repo = {}
    total_downloaded = 0
    done = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {ex.submit(process_repo, r): r["repo"] for r in with_artifacts}
        for fut in as_completed(futures):
            repo, tools, downloaded = fut.result()
            harness_per_repo[repo] = tools
            total_downloaded += downloaded
            done += 1
            if done % 50 == 0:
                print(f"  [{done}/{len(with_artifacts)}] files downloaded so far: {total_downloaded}")

    # repos reached but without artifacts -> harness = [] (none, not ND)
    for r in rows:
        if r["repo"] not in harness_per_repo:
            harness_per_repo[r["repo"]] = [] if r["found"] == "yes" else None  # None = repo not reachable

    print(f"\nFiles downloaded: {total_downloaded}")
    print(f"Repos with tagged harness: {sum(1 for v in harness_per_repo.values() if v)}")

    mechanisms_per_repo = {}
    for r in rows:
        paths_per_mech = {col: json.loads(r[col]) for col in MECH_COLS}
        found_mech = sorted(col for col, v in paths_per_mech.items() if v)
        mechanisms_per_repo[r["repo"]] = found_mech if r["found"] == "yes" else None

    if args.limit:
        print("--limit active: skipping the update of conversations.jsonl "
              "(it would touch the real file with partial data). Rerun without --limit.")
        return
    update_conversations(harness_per_repo, mechanisms_per_repo)


def update_conversations(harness_per_repo, mechanisms_per_repo):
    """Writes two columns into the agentic dataset: "harness" (which TOOL -
    Claude Code/Cursor/etc.) and "harness_mechanisms" (WHICH mechanism of
    Table 1 - context_files/skills/subagents/commands/rules/hooks/mcp),
    both derived from the session's repo(s)."""
    src = os.path.join(AGENTIC_DIR, "conversations.jsonl")
    tmp = src + ".tmp_harness"
    n_in, n_out, n_with_harness = 0, 0, 0

    with open(src, encoding="utf-8") as fin, open(tmp, "w", encoding="utf-8") as fout:
        for line in fin:
            n_in += 1
            rec = json.loads(line)
            repos_session = [a["repo"] for a in (rec.get("artifacts") or []) if a.get("repo")]

            tools = set()
            mechanisms = set()
            at_least_one_repo_reached = False
            for repo in repos_session:
                v_tool = harness_per_repo.get(repo)
                if v_tool is not None:
                    at_least_one_repo_reached = True
                    tools.update(v_tool)
                v_mech = mechanisms_per_repo.get(repo)
                if v_mech is not None:
                    mechanisms.update(v_mech)

            if tools:
                rec["harness"] = sorted(tools)
                n_with_harness += 1
            elif at_least_one_repo_reached:
                rec["harness"] = []  # repo reached, no mechanism found
            else:
                rec["harness"] = None  # no known repo, or not reachable on GitHub

            if mechanisms:
                rec["harness_mechanisms"] = sorted(mechanisms)
            elif at_least_one_repo_reached:
                rec["harness_mechanisms"] = []
            else:
                rec["harness_mechanisms"] = None

            fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n_out += 1

    if n_in != n_out:
        os.remove(tmp)
        raise RuntimeError(f"Row mismatch: read {n_in}, written {n_out} - ABORT, conversations.jsonl not touched")

    backup = src + f".bak-pre-harness-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(src, backup)
    os.replace(tmp, src)
    print(f"\nconversations.jsonl updated ({n_out} rows, unchanged in number).")
    print(f"Pre-modification backup saved in: {backup}")
    print(f"Sessions with non-empty 'harness' column: {n_with_harness}/{n_out}")


if __name__ == "__main__":
    main()
