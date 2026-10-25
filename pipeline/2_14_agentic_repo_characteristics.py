#!/usr/bin/env python
"""
Repo characteristics analysis for the vibe-coding dataset: descriptive
distribution (stars/size/age/contributors/language), "looks_like_personal_archive"
flag (same heuristic already validated for out_ago2026 in
104_metadata_repo_archive.py - description/topics), and correlation
between repo characteristics and adoption rate.

No new API calls: repositories.json and contributors/*.json were
already downloaded by the scraper.

Writes out_vibe_coding/repo_analysis.csv (one row per repo, with all the
metrics + n_sessions/n_adopted/adoption_pct) and prints a text report.
"""
import csv
import datetime
import json
import os
import re
import statistics
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
csv.field_size_limit(50_000_000)

SCRAPER_DIR = os.path.join(SCRIPT_DIR, "1_07_agentic_scraper", "data")
OUT_DIR = os.path.join(SCRIPT_DIR, "out_vibe_coding")
OUT_CSV = os.path.join(OUT_DIR, "repo_analysis.csv")

# same heuristic as 104_metadata_repo_archive.py, reused here
_WORDS_ARCHIVE = re.compile(
    r"\b(notes?|journal|diary|second-?brain|digital-?garden|zettelkasten|"
    r"knowledge-?base|brain-?dump|personal-?wiki|chatgpt-?log|ai-?convers|"
    r"llm-?log|gpt-?log|chat-?log|ai-?journal|prompt-?log)\b", re.I)


def outcome_per_sha():
    yes, no = set(), set()
    for path, campo, values_yes, excluded in [
        (f"{OUT_DIR}/adoption_cli_agent_direct.csv", "code_verified", ("yes",), ()),
        (f"{OUT_DIR}/adoption_cli_agent_fallback.csv", "found", ("yes",),
         ("n/a_no_session_snippet", "file_not_found_today", "n.d._transcript_chat")),
        (f"{OUT_DIR}/adoption_cli_agent_authorship.csv", "code_verified", ("yes",), ()),
        (f"{OUT_DIR}/adoption_ide_direct.csv", "code_verified", ("yes",), ()),
        (f"{OUT_DIR}/adoption_ide_commit_message.csv", "code_verified", ("yes",),
         ("n/a", "n/a_no_session_snippet")),
        (f"{OUT_DIR}/adoption_ide_fallback.csv", "found", ("yes",),
         ("n/a_no_session_snippet", "file_not_found_today")),
        (f"{OUT_DIR}/adoption_whole_repo.csv", "found", ("yes",), ()),
    ]:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r[campo] in excluded:
                    continue
                (yes if r[campo] in values_yes else no).add(r["sha"])
    return yes, no


def parse_data(s):
    if not s:
        return None
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def main():
    print("Loading repositories.json, contributors/, conversations.jsonl...")
    repos = json.load(open(os.path.join(SCRAPER_DIR, "repositories.json"), encoding="utf-8"))
    repo_per_nome = {r["full_name"]: r for r in repos}

    contrib_dir = os.path.join(SCRAPER_DIR, "contributors")
    n_contrib_per_id = {}
    for fn in os.listdir(contrib_dir):
        try:
            with open(os.path.join(contrib_dir, fn), encoding="utf-8") as f:
                n_contrib_per_id[fn[:-5]] = len(json.load(f))
        except Exception:
            continue

    yes_total, no_total = outcome_per_sha()

    def outcome(sha):
        if sha in yes_total:
            return "yes"
        if sha in no_total:
            return "no"
        return "nv"

    sessions_per_repo = {}
    with open(os.path.join(OUT_DIR, "conversations.jsonl"), encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            repo = (c.get("artifacts") or [{}])[0].get("repo", "")
            if not repo:
                continue
            sessions_per_repo.setdefault(repo, []).append(c["sha"])

    today = datetime.datetime.utcnow()
    rows = []
    for repo_nome, shas in sessions_per_repo.items():
        meta = repo_per_nome.get(repo_nome)
        if not meta:
            continue
        repo_id = str(meta["id"])
        created = parse_data(meta.get("created_at"))
        pushed = parse_data(meta.get("pushed_at"))
        age_days = (today - created).days if created else None
        activity_days = (pushed - created).days if (created and pushed) else None

        text_topics = " ".join(meta.get("topics") or [])
        text_descr = meta.get("description") or ""
        archive = bool(_WORDS_ARCHIVE.search(text_topics + " " + text_descr))

        outcomes = [outcome(sha) for sha in shas]
        n_yes = outcomes.count("yes")
        n_no = outcomes.count("no")
        n_searched = n_yes + n_no

        rows.append({
            "repo": repo_nome, "repo_id": repo_id,
            "stars": meta.get("stargazers_count", 0),
            "forks": meta.get("forks_count", 0),
            "size_kb": meta.get("size", 0),
            "primary_language": meta.get("language") or "",
            "n_contributors": n_contrib_per_id.get(repo_id, None),
            "age_days": age_days,
            "activity_days": activity_days,
            "open_issues": meta.get("open_issues_count", 0),
            "is_fork": meta.get("fork", False),
            "github_archived": meta.get("archived", False),
            "looks_like_personal_archive": archive,
            "n_sessions": len(shas),
            "n_searched": n_searched,
            "n_adopted": n_yes,
            "adoption_pct": round(100 * n_yes / n_searched, 1) if n_searched else None,
        })

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"Written {OUT_CSV} ({len(rows)} repo)")

    # ---------------------------------------------------------- report
    print()
    print("=" * 70)
    print(f"REPOS ANALYZED: {len(rows)} (su {len(sessions_per_repo)} distinct repos in the sessions)")
    print("=" * 70)

    def stat(campo, filter_fn=None):
        vals = [r[campo] for r in rows if r[campo] is not None and (filter_fn is None or filter_fn(r))]
        if not vals:
            return "n/a"
        return (f"median={statistics.median(vals):.0f}  mean={statistics.mean(vals):.0f}  "
                f"min={min(vals)}  max={max(vals)}")

    print("\n--- Descriptive distribution ---")
    print("Stars:          ", stat("stars"))
    print("Forks:           ", stat("forks"))
    print("Size (KB):      ", stat("size_kb"))
    print("Age (days):   ", stat("age_days"))
    print("Contributors:   ", stat("n_contributors"))
    print("Open issues:    ", stat("open_issues"))

    import collections
    cnt_lang = collections.Counter(r["primary_language"] for r in rows if r["primary_language"])
    print("\nTop 10 primary languages of the repos:")
    for l, n in cnt_lang.most_common(10):
        print(f"  {l}: {n}")

    n_fork = sum(1 for r in rows if r["is_fork"])
    n_archived = sum(1 for r in rows if r["github_archived"])
    n_personal = sum(1 for r in rows if r["looks_like_personal_archive"])
    print(f"\nForks of another repo: {n_fork} ({100*n_fork/len(rows):.1f}%)")
    print(f"Archived on GitHub (read-only): {n_archived} ({100*n_archived/len(rows):.1f}%)")
    print(f"Looks like a personal archive (topics/description): {n_personal} ({100*n_personal/len(rows):.1f}%)")

    # share of SESSIONS (not repos) involved in archive repos
    sessions_in_archive = sum(r["n_sessions"] for r in rows if r["looks_like_personal_archive"])
    tot_sessions = sum(r["n_sessions"] for r in rows)
    print(f"Sessions in archive repos: {sessions_in_archive}/{tot_sessions} "
          f"({100*sessions_in_archive/tot_sessions:.1f}%)")

    print("\n--- Correlation repo characteristics <-> adoption ---")

    def adoption_pct_bracket(filter_fn):
        relevant = [r for r in rows if filter_fn(r) and r["n_searched"] > 0]
        tot_yes = sum(r["n_adopted"] for r in relevant)
        tot_searched = sum(r["n_searched"] for r in relevant)
        n_repo = len(relevant)
        return tot_yes, tot_searched, n_repo

    print("\nBy star bracket (aggregated sessions, not repos):")
    brackets_stars = [("0", lambda r: r["stars"] == 0), ("1-5", lambda r: 1 <= r["stars"] <= 5),
                     ("6-20", lambda r: 6 <= r["stars"] <= 20), ("21-100", lambda r: 21 <= r["stars"] <= 100),
                     (">100", lambda r: r["stars"] > 100)]
    for nome, filter_fn in brackets_stars:
        yes, searched, n_repo = adoption_pct_bracket(filter_fn)
        print(f"  {nome:>8} stars ({n_repo:>4} repos): {yes}/{searched} = "
              f"{100*yes/searched:.1f}%" if searched else f"  {nome:>8} stars: n/a")

    print("\nBy number of contributors:")
    brackets_contrib = [("1 (alone)", lambda r: r["n_contributors"] == 1),
                      ("2-3", lambda r: r["n_contributors"] is not None and 2 <= r["n_contributors"] <= 3),
                      ("4-10", lambda r: r["n_contributors"] is not None and 4 <= r["n_contributors"] <= 10),
                      (">10", lambda r: r["n_contributors"] is not None and r["n_contributors"] > 10)]
    for nome, filter_fn in brackets_contrib:
        yes, searched, n_repo = adoption_pct_bracket(filter_fn)
        print(f"  {nome:>10} ({n_repo:>4} repo): {yes}/{searched} = {100*yes/searched:.1f}%" if searched else f"  {nome}: n.d.")

    print("\nPersonal archive vs normal project:")
    for nome, filter_fn in [("personal archive", lambda r: r["looks_like_personal_archive"]),
                          ("normal project", lambda r: not r["looks_like_personal_archive"])]:
        yes, searched, n_repo = adoption_pct_bracket(filter_fn)
        print(f"  {nome:>20} ({n_repo:>4} repo): {yes}/{searched} = {100*yes/searched:.1f}%" if searched else f"  {nome}: n.d.")

    print("\nFork vs original repo:")
    for nome, filter_fn in [("fork", lambda r: r["is_fork"]),
                          ("original", lambda r: not r["is_fork"])]:
        yes, searched, n_repo = adoption_pct_bracket(filter_fn)
        print(f"  {nome:>12} ({n_repo:>4} repo): {yes}/{searched} = {100*yes/searched:.1f}%" if searched else f"  {nome}: n.d.")


if __name__ == "__main__":
    main()
