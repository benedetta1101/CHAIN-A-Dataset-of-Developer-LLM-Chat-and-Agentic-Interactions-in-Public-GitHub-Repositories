#!/usr/bin/env python
"""
Equivalent of 4_03_chat_intent_adoption.py for the agentic dataset (IDE + CLI) -
crosses the already classified intent labels (4_02_agentic_intent_classification/)
with the adoption outcome.

Two important methodological differences with respect to chat, to be reported
every time this number is cited in the paper:

1. Granularity of adoption. For out_ago2026 adoption is known at the level
   of the SINGLE ROW of snippet.csv (snippet_timeline.csv, steps 7-11: for
   each code block it is verified whether THAT BLOCK ended up in the repo).
   For the agentic dataset there is no equivalent - the 7 adoption_*.csv
   files (see 2_14_agentic_repo_characteristics.py::
   outcome_per_sha) give an outcome per SESSION (sha): "the session
   produced code that ended up in the repo", not "this specific
   candidate was adopted". Here the session outcome is propagated
   to all the classified messages of that session - a coarser signal,
   without the verbatim/modified split available for chat.

2. message_row_map.csv here does NOT map to snippet.csv::row_id (unlike
   chat) - it maps to "{sha}::{index_candidate}", the index in the list
   of candidates produced by 1_prepare_context.py::
   candidates_with_real_prompt(), a SEPARATE extraction from the one of
   1_04_extract_and_filter.py (same principle, different code - they are not
   guaranteed to produce identical blocks). For this reason, to recover the
   language of the candidate that same extraction has to be re-executed
   (local, no API calls, a few minutes on the whole dataset),
   a direct merge on a shared row_id is not possible.

Usage:
    python pipeline/4_04_agentic_intent_adoption.py --out-dir out_vibe_coding

Writes into out_vibe_coding/:
  - intent_adoption_main.csv, intent_adoption_sub.csv
  - intent_adoption_main_per_language.csv, intent_adoption_sub_per_language.csv
"""
import argparse
import csv
import glob
import importlib
import json
import os
import re
import sys

import pandas as pd

csv.field_size_limit(50_000_000)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INTENT_DIR = os.path.join(SCRIPT_DIR, "4_02_agentic_intent_classification", "data")
sys.path.insert(0, os.path.join(SCRIPT_DIR, "4_02_agentic_intent_classification"))
prep = importlib.import_module("1_prepare_context")

MAIN = {"1": "Code Authoring", "2": "Failure Reporting", "3": "Inquiry",
        "4": "Context Specification", "5": "Validation", "6": "Delegation",
        "7": "Workflow Control", "8": "Others"}
SUB = {"1.1": "New Implementation", "1.2": "Iterative Modification",
       "1.3": "Alignment Correction", "2.1": "Log Paste",
       "2.2": "Symptom Description", "2.3": "Error Persistence",
       "3.1": "Planning & Consultation", "3.2": "Project Comprehension",
       "3.3": "General Knowledge Query", "4.1": "Information Injection",
       "4.2": "Behavior Specification", "5.1": "Code Review",
       "5.2": "Runtime Inspection", "6.1": "Documentation",
       "6.2": "Toolchain Operation", "7.1": "Confirmation",
       "7.2": "Continuation", "7.3": "Deferred Debugging",
       "7.4": "Deferred Implementation", "7.5": "Sentiment Expression",
       "8.1": "Others"}
LANGUAGES_TARGET = ["python", "javascript", "typescript", "java"]


def primary_label():
    """Identical to 4_03_chat_intent_adoption.py::primary_label() - same logic
    (first label returned by the model, normalized to the number), only
    pointed at 4_02_agentic_intent_classification/data."""
    recs = []
    for path in sorted(glob.glob(os.path.join(INTENT_DIR, "parsed", "*.jsonl"))):
        for line in open(path, encoding="utf-8"):
            r = json.loads(line)
            main = sub = None
            for lab in r.get("labels") or []:
                m = re.match(r"\s*(\d)\.(\d)", lab.get("sub_category") or "")
                if m:
                    main, sub = m.group(1), f"{m.group(1)}.{m.group(2)}"
                    break
                m = re.match(r"\s*(\d)", lab.get("main_category") or "")
                if m:
                    main = m.group(1)
                    break
            recs.append({"message_id": r["message_id"],
                         "main_category": MAIN.get(main),
                         "sub_category": f"{sub} {SUB[sub]}" if sub in SUB else None})
    return pd.DataFrame(recs)


def outcome_per_sha(out_dir):
    """Copied verbatim from 2_14_agentic_repo_characteristics.py -
    union of the 7 adoption verification methods into two sets of sha
    (adopted yes/no). See that file for the details of each method."""
    yes, no = set(), set()
    for path, campo, values_yes, excluded in [
        (f"{out_dir}/adoption_cli_agent_direct.csv", "code_verified", ("yes",), ()),
        (f"{out_dir}/adoption_cli_agent_fallback.csv", "found", ("yes",),
         ("n/a_no_session_snippet", "file_not_found_today", "n.d._transcript_chat")),
        (f"{out_dir}/adoption_cli_agent_authorship.csv", "code_verified", ("yes",), ()),
        (f"{out_dir}/adoption_ide_direct.csv", "code_verified", ("yes",), ()),
        (f"{out_dir}/adoption_ide_commit_message.csv", "code_verified", ("yes",),
         ("n/a", "n/a_no_session_snippet")),
        (f"{out_dir}/adoption_ide_fallback.csv", "found", ("yes",),
         ("n/a_no_session_snippet", "file_not_found_today")),
        (f"{out_dir}/adoption_whole_repo.csv", "found", ("yes",), ()),
    ]:
        if not os.path.exists(path):
            print(f"  WARNING: {path} does not exist, skipping (run run_agentic_pipeline.sh --adoption first)")
            continue
        with open(path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if r[campo] in excluded:
                    continue
                (yes if r[campo] in values_yes else no).add(r["sha"])
    return yes, no


def tabella(df, keys):
    g = df.groupby(keys).agg(n=("adopted", "size"), n_adopted=("adopted", "sum"))
    g["adoption_pct"] = (g.n_adopted / g.n * 100).round(1)
    return g.reset_index().sort_values(keys[:-1] + ["n"], ascending=[True] * (len(keys) - 1) + [False])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="out_vibe_coding")
    args = ap.parse_args()
    out_dir_name = args.out_dir
    out_dir = os.path.join(SCRIPT_DIR, out_dir_name)

    print("Loading message_row_map.csv + conversations.jsonl...")
    mapping = pd.read_csv(os.path.join(INTENT_DIR, "message_row_map.csv"))
    split = mapping["row_id"].astype(str).str.split("::", n=1, expand=True)
    mapping["sha"] = split[0]
    mapping["idx_c"] = split[1].astype(int)

    conv_per_sha = {}
    with open(os.path.join(out_dir, "conversations.jsonl"), encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            conv_per_sha[c["sha"]] = c

    print(f"Re-extracting candidates for {mapping['sha'].nunique()} unique sessions "
          f"(same logic as 1_prepare_context.py, local, no API calls)...")
    language_per_key = {}
    for i, sha in enumerate(mapping["sha"].unique(), 1):
        if i % 2000 == 0:
            print(f"  [{i}/{mapping['sha'].nunique()}]")
        c = conv_per_sha.get(sha)
        if not c:
            continue
        source_group = "cli_agent" if c.get("is_cli_agent") == "yes" else "ide"
        candidates = prep.candidates_with_real_prompt(sha, source_group)
        for idx_c, cand in enumerate(candidates):
            language_per_key[(sha, idx_c)] = cand["language"]

    mapping["language"] = [language_per_key.get((r.sha, r.idx_c)) for r in mapping.itertuples()]
    mapping["repo"] = [(conv_per_sha[r.sha].get("artifacts") or [{}])[0].get("repo")
                      if r.sha in conv_per_sha else None for r in mapping.itertuples()]

    labels = mapping.merge(primary_label(), on="message_id")
    labels = labels[labels.language.notna()]

    archive = set()
    repo_csv = os.path.join(out_dir, "repo_analysis.csv")
    if os.path.exists(repo_csv):
        meta = pd.read_csv(repo_csv)
        archive = set(meta.loc[meta.looks_like_personal_archive.astype(str).str.lower() == "true", "repo"])
    else:
        print(f"  WARNING: {repo_csv} does not exist (run 2_14_agentic_repo_characteristics.py first) - no archive-repo exclusion applied")

    yes, no = outcome_per_sha(out_dir)
    if not yes and not no:
        print("No adoption CSV found - first run 'run_agentic_pipeline.sh --adoption'.")
        return
    outcome = pd.DataFrame({"sha": list(yes) + list(no), "adopted": [True] * len(yes) + [False] * len(no)})

    df = labels[~labels.repo.isin(archive)].merge(outcome, on="sha", how="inner")
    print(f"classified messages with known session outcome, without archive: {len(df)}, "
          f"adopted (at session level) {df.adopted.sum()} ({df.adopted.mean() * 100:.1f}%)")

    df["language_group"] = df.language.where(df.language.isin(LANGUAGES_TARGET), "other")
    for level, col in [("main", "main_category"), ("sub", "sub_category")]:
        tabella(df, [col]).to_csv(os.path.join(out_dir, f"intent_adoption_{level}.csv"), index=False)
        tabella(df, ["language_group", col]).to_csv(
            os.path.join(out_dir, f"intent_adoption_{level}_per_language.csv"), index=False)
    print(f"wrote 4 CSV intent_adoption_*.csv in {out_dir}")


if __name__ == "__main__":
    main()
