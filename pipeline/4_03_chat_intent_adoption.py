#!/usr/bin/env python
"""
Step 122 (optional) - Adoption by category of the intent classification
(gpt-5-mini, Tang et al. taxonomy, 4_01_chat_intent_classification/) - main and
subcategory, overall and split by language, with the
verbatim/modified split.

Label used = the FIRST one returned by the model (multi-label messages,
~23%, count only once). Anomalous labels (Chinese translations, double
spaces) are normalized to the number ("1.2", "3"). A message shared
by several rows of snippet.csv (same prompt, several blocks) propagates the
label to all of them, via message_row_map.csv.

Denominator = traceable rows of snippet_timeline.csv (HackerNews
excluded), minus the archive repos (repo_metadata_archive.csv,
looks_like_personal_archive=yes) and the translated tamnd/brain-* forks (not
marked as archive but automatic copies of tamnd/brain).

modified = found_via 'similarity' (containment ratio >= 0.55, step 8);
all the other mechanisms find only exact copies -> verbatim.

Usage:
    python pipeline/4_03_chat_intent_adoption.py --out-dir out_ago2026

Writes into out<_ago2026>/:
  - intent_adoption_main.csv, intent_adoption_sub.csv
  - intent_adoption_main_per_language.csv, intent_adoption_sub_per_language.csv
"""
import argparse
import glob
import json
import os
import re

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INTENT_DIR = os.path.join(SCRIPT_DIR, "4_01_chat_intent_classification", "data")

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
FORK_TRANSLATED = {"tamnd/brain-ja", "tamnd/brain-vi", "tamnd/brain-zh-cn"}
LANGUAGES_TARGET = ["python", "javascript", "typescript", "java"]


def primary_label():
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


def tabella(df, keys):
    g = df.groupby(keys).agg(n=("adopted", "size"), n_adopted=("adopted", "sum"),
                               verbatim=("verbatim", "sum"), modified=("modified", "sum"))
    g["adoption_pct"] = (g.n_adopted / g.n * 100).round(1)
    g["pct_modified_of_adopted"] = (g.modified / g.n_adopted.where(g.n_adopted > 0) * 100).round(1)
    return g.reset_index().sort_values(keys[:-1] + ["n"], ascending=[True] * (len(keys) - 1) + [False])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="out_ago2026")
    args = ap.parse_args()
    out_dir = os.path.join(SCRIPT_DIR, args.out_dir)

    mapping = pd.read_csv(os.path.join(INTENT_DIR, "message_row_map.csv"))
    labels = mapping.merge(primary_label(), on="message_id")
    snip = pd.read_csv(os.path.join(out_dir, "snippet.csv"),
                       usecols=["row_id", "language"])
    tl = pd.read_csv(os.path.join(out_dir, "snippet_timeline.csv"),
                     usecols=["row_id", "repo", "found", "found_via"])
    meta = pd.read_csv(os.path.join(out_dir, "repo_metadata_archive.csv"))
    archive = set(meta.loc[meta.looks_like_personal_archive == "yes", "repo"]) | FORK_TRANSLATED

    df = tl[~tl.repo.isin(archive)].merge(labels, on="row_id").merge(snip, on="row_id")
    df["adopted"] = df.found.eq("yes")
    df["modified"] = df.found_via.eq("similarity")
    df["verbatim"] = df.adopted & ~df.modified
    print(f"traceable rows without archive: {len(df)}, adopted {df.adopted.sum()} "
          f"({df.adopted.mean() * 100:.1f}%), of which modified {df.modified.sum()}")

    df["language_group"] = df.language.where(df.language.isin(LANGUAGES_TARGET), "other")
    for level, col in [("main", "main_category"), ("sub", "sub_category")]:
        tabella(df, [col]).to_csv(os.path.join(out_dir, f"intent_adoption_{level}.csv"), index=False)
        tabella(df, ["language_group", col]).to_csv(
            os.path.join(out_dir, f"intent_adoption_{level}_per_language.csv"), index=False)
    print(f"wrote 4 CSV intent_adoption_*.csv in {out_dir}")


if __name__ == "__main__":
    main()
