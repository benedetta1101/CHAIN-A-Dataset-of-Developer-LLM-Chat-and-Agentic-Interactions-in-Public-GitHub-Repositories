#!/usr/bin/env python
"""
Corrects harness/harness_mechanisms in conversations.jsonl to take into account
WHEN each harness file was actually created - without
this correction, the two fields reflect the CURRENT state of the repo and
are applied to ALL its sessions, including the ones that happened BEFORE
the file existed (verified live: on Neurotypic-ai/magus-mark,
301/301 sessions were tagged with AGENTS.md even though that file was
added after the last scraped session).

For each harness path found (harness_config.csv), it retrieves the date
of the FIRST commit that touches it (GET /repos/{repo}/commits?path=...,
pagination via Link header to jump directly to the last page =
the oldest commit, without downloading the whole history). A session
is tagged with that tool/mechanism ONLY if its timestamp is
later than (or equal to) the file creation date.

On-disk cache (harness_date_cache.pkl, same DiskCache as the rest of the
pipeline) - rerunning the script does not re-download the dates already known.

Safety: same pattern as 3_02_agentic_download_harness.py - timestamped backup
before rewriting conversations.jsonl, row count check, no overwriting
if something does not add up.

Usage:
    python pipeline/3_03_agentic_fix_harness_temporal.py
    python pipeline/3_03_agentic_fix_harness_temporal.py --limit 20   # test
"""
import argparse
import csv
import importlib
import json
import os
import re
import shutil
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_h1 = importlib.import_module("3_01_agentic_extract_harness")
POOL, _request, GITHUB_API = _h1.POOL, _h1._request, _h1.GITHUB_API
_h2 = importlib.import_module("3_02_agentic_download_harness")
tool_per_path, all_paths, MECH_COLS = _h2.tool_per_path, _h2.all_paths, _h2.MECH_COLS
from lib_github_cache import DiskCache

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AGENTIC_DIR = os.path.join(SCRIPT_DIR, "out_vibe_coding")

_LINK_LAST_RE = re.compile(r'[?&]page=(\d+)[^>]*>;\s*rel="last"')


def creation_date(repo, path):
    """Date (YYYY-MM-DD) of the OLDEST commit that touches this path, or
    None if it cannot be determined (no commit found / error)."""
    url = f"{GITHUB_API}/repos/{repo}/commits?path={urllib.parse.quote(path)}&per_page=1"
    body, headers = _request(url, allow_404=True, with_headers=True)
    if not body:
        return None
    link = headers.get("Link") or headers.get("link") or ""
    m = _LINK_LAST_RE.search(link)
    if not m:
        # a single page = a single commit in total - it is already the oldest
        data = body[0]["commit"]["author"]["date"]
        return data[:10]
    ultima_pagina = int(m.group(1))
    url_ultima = f"{GITHUB_API}/repos/{repo}/commits?path={urllib.parse.quote(path)}&per_page=1&page={ultima_pagina}"
    body2 = _request(url_ultima, allow_404=True)
    if not body2:
        return None
    return body2[0]["commit"]["author"]["date"][:10]


def load_harness_config():
    with open(os.path.join(AGENTIC_DIR, "harness_config.csv"), encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def load_rows_harness(all_rows, limit=None):
    rows = [r for r in all_rows if r["found"] == "yes" and int(r["n_mechanisms"]) > 0]
    if limit:
        rows = rows[:limit]
    return rows


def process_repo(row, cache):
    repo = row["repo"]
    out = []  # [(path, tool, mechanism, creation_date_or_None), ...]
    for col in MECH_COLS:
        for path in json.loads(row[col]):
            key = (repo, path)
            if key in cache:
                data = cache[key]
            else:
                data = creation_date(repo, path)
                cache[key] = data
            out.append((path, tool_per_path(path), col, data))
    return repo, out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None, help="limit to N repos (test)")
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()

    print(f"GitHub tokens available: {len(POOL.tokens) if POOL.has_auth() else 0}")
    all_rows = load_harness_config()
    reachable_per_repo = {r["repo"]: (r["found"] == "yes") for r in all_rows}
    rows = load_rows_harness(all_rows, args.limit)
    print(f"Repos with harness to verify temporally: {len(rows)}")

    cache = DiskCache(os.path.join(AGENTIC_DIR, "harness_date_cache.pkl"))
    info_per_repo = {}
    done = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {ex.submit(process_repo, r, cache): r["repo"] for r in rows}
        for fut in as_completed(futures):
            repo, out = fut.result()
            info_per_repo[repo] = out
            done += 1
            if done % 50 == 0:
                print(f"  [{done}/{len(rows)}]")
                cache.save_every(1, 0)
    cache.save()

    n_without_data = sum(1 for out in info_per_repo.values() for (_, _, _, d) in out if d is None)
    n_total = sum(len(out) for out in info_per_repo.values())
    print(f"\nPaths with determined creation date: {n_total - n_without_data}/{n_total}")

    if args.limit:
        print("--limit active: skipping the update of conversations.jsonl. Rerun without --limit.")
        return

    update_conversations_temporale(info_per_repo, reachable_per_repo)


def update_conversations_temporale(info_per_repo, reachable_per_repo):
    src = os.path.join(AGENTIC_DIR, "conversations.jsonl")
    tmp = src + ".tmp_harness_temporale"
    n_in, n_out = 0, 0
    n_with_harness_before, n_with_harness_after = 0, 0
    n_discarded_without_data = 0

    with open(src, encoding="utf-8") as fin, open(tmp, "w", encoding="utf-8") as fout:
        for line in fin:
            n_in += 1
            rec = json.loads(line)
            if rec.get("harness"):
                n_with_harness_before += 1

            ts = (rec.get("timestamp") or "")[:10]
            repos_session = [a["repo"] for a in (rec.get("artifacts") or []) if a.get("repo")]

            tools, mechanisms = set(), set()
            at_least_one_repo_reached = False
            for repo in repos_session:
                if reachable_per_repo.get(repo):
                    at_least_one_repo_reached = True
                out = info_per_repo.get(repo)
                if out is None:
                    continue
                for path, tool, mech, data in out:
                    if data is None:
                        n_discarded_without_data += 1
                        continue
                    if ts and data <= ts:
                        tools.add(tool)
                        mechanisms.add(mech)

            if tools:
                rec["harness"] = sorted(tools)
                n_with_harness_after += 1
            elif at_least_one_repo_reached:
                rec["harness"] = []  # repo reached (with or without harness), but not before this session
            else:
                rec["harness"] = None  # no known repo, or none reachable on GitHub
            rec["harness_mechanisms"] = sorted(mechanisms) if mechanisms else rec["harness"]

            fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n_out += 1

    if n_in != n_out:
        os.remove(tmp)
        raise RuntimeError(f"Row mismatch: read {n_in}, written {n_out} - ABORT, conversations.jsonl not touched")

    backup = src + f".bak-pre-harness-temporale-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(src, backup)
    os.replace(tmp, src)
    print(f"\nconversations.jsonl updated ({n_out} rows, unchanged in number).")
    print(f"Pre-modification backup: {backup}")
    print(f"Sessions with harness BEFORE the temporal correction: {n_with_harness_before}")
    print(f"Sessions with harness AFTER the temporal correction:     {n_with_harness_after}")
    print(f"(repo-file, session) combinations discarded because of unknown creation date: {n_discarded_without_data}")


if __name__ == "__main__":
    main()
