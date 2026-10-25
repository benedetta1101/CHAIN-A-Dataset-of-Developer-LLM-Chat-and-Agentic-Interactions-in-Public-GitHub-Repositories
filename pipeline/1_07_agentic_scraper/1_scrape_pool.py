#!/usr/bin/env python
"""
Version of scrape.ipynb adapted to use a POOL of GitHub tokens
(instead of a single GITHUB_PAT) with concurrent requests, to drastically
reduce the collection time - the original script is single-token and
single-thread, hence limited by the 5,000 requests/hour cap of ONE
account.

Same output structure (data/searches, data/repositories.json,
data/languages.json, data/contributors/, data/readmes/, data/file_trees/,
data/contents/, data/commits_history/, data/commits_path/, data/commits/,
data/mapping.csv) - compatible with 2_parse.py/3_parse_cli.py as they are.

Fix with respect to the original: in the notebook, the phases that do NOT go
through make_request() (repositories, languages, readmes, file_trees,
contents, commits_path, commits) have a generic except that silently skips
the item even on a 403/rate limit, without retry - with a single token,
as soon as the hourly cap is exceeded ALL the following items in that phase
are lost. Here every phase goes through the same helper, with
retry + token rotation + wait for the reset.

Usage:
    python 1_scrape_pool.py --start-date 2026-07-01 --end-date 2026-09-28
    python 1_scrape_pool.py --start-date 2026-07-01 --end-date 2026-09-28 \
        --skip-searches --skip-commits-history   # resume, skipping phases already done
    python 1_scrape_pool.py --help
"""
import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.parse import quote

import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
API_BASE_URL = "https://api.github.com"
TOKENS_PATH = os.path.join(SCRIPT_DIR, "..", "..", "pipeline", "tokens.txt")


# ---------------------------------------------------------------- token pool
def load_tokens():
    env = os.environ.get("GITHUB_TOKENS") or os.environ.get("GITHUB_PAT") or ""
    toks = [t.strip() for t in env.replace(",", " ").split() if t.strip()]
    if os.path.exists(TOKENS_PATH):
        with open(TOKENS_PATH) as f:
            toks += [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]
    seen, out = set(), []
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


class TokenPool:
    """Thread-safe: checkout() returns an unblocked token (in rotation), and waits
    if ALL are blocked. mark_blocked() marks a single token as exhausted until
    the reset - the others stay available, unlike the original notebook, which
    has no such concept (a single token = nothing to rotate)."""

    def __init__(self, tokens):
        self.tokens = tokens or [None]
        self.lock = threading.Lock()
        self.blocked_until = {}
        self.rr = 0

    def has_auth(self):
        return any(self.tokens)

    def checkout(self):
        while True:
            with self.lock:
                now = time.time()
                for _ in range(len(self.tokens)):
                    tok = self.tokens[self.rr % len(self.tokens)]
                    self.rr += 1
                    if self.blocked_until.get(tok, 0) <= now:
                        return tok
                soonest_tok = min(self.blocked_until, key=self.blocked_until.get)
                wait = max(0.0, self.blocked_until[soonest_tok] - now) + 1
            print(f"  [token pool] all tokens blocked, waiting {wait:.0f}s...")
            time.sleep(wait)

    def mark_blocked(self, token, until_epoch):
        with self.lock:
            self.blocked_until[token] = until_epoch


POOL = TokenPool(load_tokens())


def make_request(url, max_retries=6, allow_404=True):
    """GET with retry + token rotation over the pool. Replaces both
    make_request() and the 'bare' requests.get() calls of the original
    notebook (see the note in the module docstring)."""
    for attempt in range(max_retries):
        token = POOL.checkout()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            resp = requests.get(url, headers=headers, timeout=40)
            if resp.status_code == 404 and allow_404:
                return None
            if resp.status_code in (403, 429):
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    until = time.time() + int(retry_after)
                else:
                    until = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
                POOL.mark_blocked(token, until)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            print(f"  [retry {attempt+1}/{max_retries}] error on {url}: {e}")
            time.sleep(5)
    print(f"  [ERROR] too many failed attempts, giving up: {url}")
    return None


def parallel_map(items, fn, workers, desc):
    """Runs fn(item) on several threads, with periodic progress. fn must do its
    own I/O (file writing) and not return anything aggregated - each item
    writes its own output independently."""
    total = len(items)
    if total == 0:
        print(f"{desc}: no items to process.")
        return
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fn, item): item for item in items}
        for fut in as_completed(futures):
            done += 1
            try:
                fut.result()
            except Exception as e:
                print(f"  [worker error] {futures[fut]}: {e}")
            if done % 100 == 0 or done == total:
                print(f"{desc}: [{done}/{total}]")


# ---------------------------------------------------------------- phase 1: search
def generate_daily_prefixes(start_date, end_date):
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
    if start_dt > end_dt:
        raise ValueError("start-date must precede end-date")
    total_days = (end_dt - start_dt).days + 1
    return [(start_dt + timedelta(days=o)).strftime("%Y-%m-%d") for o in range(total_days)]


def fetch_prefix(prefix):
    query = f".specstory/history/{prefix} in:path language:markdown"
    encoded_query = quote(query)
    all_items = []
    page = 1
    while True:
        url = f"{API_BASE_URL}/search/code?q={encoded_query}&per_page=100&page={page}"
        data = make_request(url)
        if not data:
            break
        items = data.get("items", [])
        if not items:
            break
        all_items.extend(items)
        if len(items) < 100 or len(all_items) >= 1100:
            break
        page += 1
        time.sleep(2)  # the search API has a much stricter limit than the core API

    out_path = os.path.join(DATA_DIR, "searches", f"{prefix}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_items, f, ensure_ascii=False, indent=2)


def run_searches(prefixes, workers):
    os.makedirs(os.path.join(DATA_DIR, "searches"), exist_ok=True)
    existing = {f[:-5] for f in os.listdir(os.path.join(DATA_DIR, "searches")) if f.endswith(".json")}
    from_fare = [p for p in prefixes if p not in existing]
    print(f"Search: {len(prefixes)} total days, {len(from_fare)} da fare "
          f"({len(existing)} already present).")
    parallel_map(from_fare, fetch_prefix, workers, "Search (per day)")


def load_all_items():
    search_dir = os.path.join(DATA_DIR, "searches")
    all_items = []
    for filename in sorted(os.listdir(search_dir)):
        if filename.endswith(".json"):
            with open(os.path.join(search_dir, filename)) as f:
                all_items.extend(json.load(f))
    with open(os.path.join(DATA_DIR, "searches.json"), "w") as f:
        json.dump(all_items, f, indent=2)
    return all_items


def build_df(all_items):
    import pandas as pd
    rows = [{
        "name": it.get("name"), "path": it.get("path"), "sha": it.get("sha"),
        "url": it.get("url"), "git_url": it.get("git_url"),
        "repository_full_name": it["repository"]["full_name"],
        "repository_url": it["repository"]["url"],
    } for it in all_items]
    df = pd.DataFrame(rows).sort_values(by="name").reset_index(drop=True)
    df = df.drop_duplicates(subset=["sha"]).reset_index(drop=True)
    print(f"Unique markdown files: {len(df)}")
    return df


# ---------------------------------------------------------------- phase 2: repository
def fetch_repository(repo_url):
    return make_request(repo_url)


def run_repositories(df, workers):
    repo_urls = df["repository_url"].value_counts().index.tolist()
    print(f"Unique repositories: {len(repo_urls)}")
    results = []
    lock = threading.Lock()

    def worker(url):
        data = fetch_repository(url)
        if data:
            with lock:
                results.append(data)

    parallel_map(repo_urls, worker, workers, "Repository")
    with open(os.path.join(DATA_DIR, "repositories.json"), "w") as f:
        json.dump(results, f, indent=2)
    print(f"Repositories obtained: {len(results)}/{len(repo_urls)}")


def build_df_repositories():
    import pandas as pd
    items = json.load(open(os.path.join(DATA_DIR, "repositories.json")))
    rows = [{
        "id": it.get("id"), "name": it.get("name"), "full_name": it.get("full_name"),
        "languages_url": it.get("languages_url"),
        "commits_url": (it.get("commits_url") or "").replace("{/sha}", ""),
    } for it in items]
    df_repo = pd.DataFrame(rows)
    print(f"Total repositories: {len(df_repo)}")
    return df_repo


def build_mapping(df, df_repo):
    df_mapping = df[["name", "sha", "repository_full_name"]].rename(
        columns={"name": "session_name", "sha": "session_sha"})
    df_mapping = df_mapping.merge(
        df_repo[["id", "full_name"]].rename(columns={"id": "repository_id"}),
        left_on="repository_full_name", right_on="full_name", how="left")
    df_mapping = df_mapping.drop(columns=["full_name", "session_name", "repository_full_name"])
    df_mapping = df_mapping.sort_values(by=["repository_id"]).reset_index(drop=True)
    df_mapping.to_csv(os.path.join(DATA_DIR, "mapping.csv"), index=False)
    print(f"Session-repository mapping: {len(df_mapping)}")


# ---------------------------------------------------------------- phase 3: languages
def run_languages(df_repo, workers):
    results = []
    lock = threading.Lock()

    def worker(row):
        data = make_request(row.languages_url)
        if data is not None:
            with lock:
                results.append({"full_name": row.full_name, "languages": data})

    parallel_map(list(df_repo.itertuples()), worker, workers, "Languages")
    with open(os.path.join(DATA_DIR, "languages.json"), "w") as f:
        json.dump(results, f, indent=2)
    print(f"Languages obtained: {len(results)}/{len(df_repo)}")


# ---------------------------------------------------------------- phase 4: contributors
def run_contributors(df_repo, workers):
    out_dir = os.path.join(DATA_DIR, "contributors")
    os.makedirs(out_dir, exist_ok=True)
    existing = {f[:-5] for f in os.listdir(out_dir) if f.endswith(".json")}
    from_fare = [r for r in df_repo.itertuples() if str(r.id) not in existing]
    print(f"Contributor: {len(df_repo)} total repos, {len(from_fare)} da fare.")

    def worker(row):
        all_contributors, page = [], 1
        while True:
            url = f"{API_BASE_URL}/repos/{row.full_name}/contributors?per_page=100&page={page}"
            data = make_request(url)
            if not data:
                break
            all_contributors.extend(data)
            if len(data) < 100:
                break
            page += 1
        with open(os.path.join(out_dir, f"{row.id}.json"), "w", encoding="utf-8") as f:
            json.dump(all_contributors, f, ensure_ascii=False, indent=2)

    parallel_map(from_fare, worker, workers, "Contributor")


# ---------------------------------------------------------------- phase 5: readmes
def run_readmes(df_repo, workers):
    out_dir = os.path.join(DATA_DIR, "readmes")
    os.makedirs(out_dir, exist_ok=True)
    existing = {f[:-3] for f in os.listdir(out_dir) if f.endswith(".md")}
    from_fare = [r for r in df_repo.itertuples() if str(r.id) not in existing]
    print(f"README: {len(df_repo)} total repos, {len(from_fare)} da fare.")

    def worker(row):
        import base64
        data = make_request(f"{API_BASE_URL}/repos/{row.full_name}/readme")
        decoded = ""
        if data and data.get("content"):
            try:
                decoded = base64.b64decode(data["content"]).decode("utf-8", "ignore")
            except Exception:
                pass
        with open(os.path.join(out_dir, f"{row.id}.md"), "w", encoding="utf-8") as f:
            f.write(decoded)

    parallel_map(from_fare, worker, workers, "README")


# ---------------------------------------------------------------- phase 6: file trees
def run_file_trees(df_repo, workers):
    out_dir = os.path.join(DATA_DIR, "file_trees")
    os.makedirs(out_dir, exist_ok=True)
    existing = {f[:-5] for f in os.listdir(out_dir) if f.endswith(".json")}
    from_fare = [r for r in df_repo.itertuples() if str(r.id) not in existing]
    print(f"File tree: {len(df_repo)} total repos, {len(from_fare)} da fare.")

    def worker(row):
        repo_meta = make_request(f"{API_BASE_URL}/repos/{row.full_name}")
        if not repo_meta or not repo_meta.get("default_branch"):
            return
        branch = repo_meta["default_branch"]
        tree = make_request(f"{API_BASE_URL}/repos/{row.full_name}/git/trees/{branch}?recursive=1")
        if not tree:
            return
        paths = [item["path"] for item in tree.get("tree", []) if "path" in item]
        with open(os.path.join(out_dir, f"{row.id}.json"), "w", encoding="utf-8") as f:
            json.dump(paths, f, ensure_ascii=False, indent=2)

    parallel_map(from_fare, worker, workers, "File tree")


# ---------------------------------------------------------------- phase 7: contents
def run_contents(df, workers):
    out_dir = os.path.join(DATA_DIR, "contents")
    os.makedirs(out_dir, exist_ok=True)
    existing = set(os.listdir(out_dir))
    df_to_fetch = df[~df["sha"].isin(existing)].reset_index(drop=True)
    print(f"Chat contents: {len(df)} total files, {len(df_to_fetch)} da fare.")

    def worker(git_url):
        data = make_request(git_url)
        if data and "content" in data and "sha" in data:
            with open(os.path.join(out_dir, data["sha"]), "w") as f:
                f.write(data["content"])

    parallel_map(df_to_fetch["git_url"].to_list(), worker, workers, "Chat contents")


def build_markdowns():
    import base64
    contents_dir = os.path.join(DATA_DIR, "contents")
    out_dir = os.path.join(DATA_DIR, "markdowns")
    os.makedirs(out_dir, exist_ok=True)
    n = 0
    for filename in os.listdir(contents_dir):
        with open(os.path.join(contents_dir, filename)) as f:
            encoded = f.read()
        decoded = base64.b64decode(encoded).decode("utf-8", "ignore")
        with open(os.path.join(out_dir, filename + ".md"), "w", encoding="utf-8") as f:
            f.write(decoded)
        n += 1
    print(f"Decoded markdown: {n}")


# ---------------------------------------------------------------- phase 8: commits_history
def run_commits_history(df_repo, workers):
    out_dir = os.path.join(DATA_DIR, "commits_history")
    os.makedirs(out_dir, exist_ok=True)
    existing = {f[:-5] for f in os.listdir(out_dir) if f.endswith(".json")}
    from_fare = [r for r in df_repo.itertuples() if str(r.id) not in existing]
    print(f"Commit history: {len(df_repo)} total repos, {len(from_fare)} da fare "
          f"(no page limit, can be slow for very active repos).")

    def worker(row):
        all_commits, page = [], 1
        while True:
            data = make_request(f"{row.commits_url}?per_page=100&page={page}")
            if not data:
                break
            all_commits.extend(data)
            if len(data) < 100:
                break
            page += 1
        with open(os.path.join(out_dir, f"{row.id}.json"), "w") as f:
            json.dump(all_commits, f, indent=2)

    parallel_map(from_fare, worker, workers, "Commit history")


# ---------------------------------------------------------------- phase 9: commits_path
def run_commits_path(df, workers):
    out_dir = os.path.join(DATA_DIR, "commits_path")
    os.makedirs(out_dir, exist_ok=True)
    existing = set(os.listdir(out_dir))
    df_to_fetch = df[~df["sha"].isin(existing)].reset_index(drop=True)
    print(f"Commits per file: {len(df)} total files, {len(df_to_fetch)} da fare.")

    def worker(row):
        url = f"{API_BASE_URL}/repos/{row.repository_full_name}/commits?path={quote(row.path)}&per_page=100"
        data = make_request(url)
        if data is not None:
            with open(os.path.join(out_dir, row.sha), "w") as f:
                json.dump(data, f, indent=2)

    parallel_map(list(df_to_fetch.itertuples()), worker, workers, "Commits per file")


# ---------------------------------------------------------------- phase 10: commits (detail)
def run_commits(workers):
    import pandas as pd
    commits_path_dir = os.path.join(DATA_DIR, "commits_path")
    commits_history = []
    for sha in os.listdir(commits_path_dir):
        with open(os.path.join(commits_path_dir, sha)) as f:
            data = json.load(f)
        for commit in data:
            commits_history.append({
                "sha": sha, "commit_sha": commit.get("sha"), "url": commit.get("url"),
            })
    df_ch = pd.DataFrame(commits_history)
    unique_commits = df_ch.drop_duplicates(subset=["commit_sha"]).reset_index(drop=True)

    out_dir = os.path.join(DATA_DIR, "commits")
    os.makedirs(out_dir, exist_ok=True)
    existing = set(os.listdir(out_dir))
    df_to_fetch = unique_commits[~unique_commits["commit_sha"].isin(existing)].reset_index(drop=True)
    print(f"Commit detail: {len(unique_commits)} unique, {len(df_to_fetch)} da fare.")

    def worker(row):
        data = make_request(row.url)
        if data is not None:
            with open(os.path.join(out_dir, row.commit_sha), "w") as f:
                json.dump(data, f, indent=2)

    parallel_map(list(df_to_fetch.itertuples()), worker, workers, "Commit detail")


# ---------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end-date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--workers", type=int, default=None,
                     help="concurrent threads (default: 3x the number of tokens)")
    for phase in ["searches", "repositories", "languages", "contributors", "readmes",
                 "file-trees", "contents", "markdowns", "commits-history",
                 "commits-path", "commits"]:
        ap.add_argument(f"--skip-{phase}", action="store_true")
    args = ap.parse_args()

    if not POOL.has_auth():
        print("ERROR: no token found (neither GITHUB_TOKENS/GITHUB_PAT nor pipeline/tokens.txt).")
        sys.exit(1)
    workers = args.workers or len(POOL.tokens) * 3
    print(f"Tokens in the pool: {len(POOL.tokens)}  |  concurrent workers: {workers}")

    os.makedirs(DATA_DIR, exist_ok=True)

    prefixes = generate_daily_prefixes(args.start_date, args.end_date)
    if not args.skip_searches:
        run_searches(prefixes, workers)
    all_items = load_all_items()
    df = build_df(all_items)

    if not args.skip_repositories:
        run_repositories(df, workers)
    df_repo = build_df_repositories()
    build_mapping(df, df_repo)

    if not args.skip_languages:
        run_languages(df_repo, workers)
    if not args.skip_contributors:
        run_contributors(df_repo, workers)
    if not args.skip_readmes:
        run_readmes(df_repo, workers)
    if not args.skip_file_trees:
        run_file_trees(df_repo, workers)
    if not args.skip_contents:
        run_contents(df, workers)
    if not args.skip_markdowns:
        build_markdowns()
    if not args.skip_commits_history:
        run_commits_history(df_repo, workers)
    if not args.skip_commits_path:
        run_commits_path(df, workers)
    if not args.skip_commits:
        run_commits(workers)

    print("\nDone.")


if __name__ == "__main__":
    main()
