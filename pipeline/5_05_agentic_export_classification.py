#!/usr/bin/env python3
"""
Exports the agentic dataset "classified prompt->response pairs": a row
of out_vibe_coding/snippet.csv (4 target languages) with the intent of its
PROMPT attached. Agentic equivalent of
4_01_chat_intent_classification/data/classification_per_row_4lang.csv (chat).

Row -> label match: same (sha, prompt) between the snippet.csv row and a
candidate of candidates_with_real_prompt() (the same extraction used to
classify), comparison with normalized whitespace. Rows whose prompt
is not among the classified candidates do NOT appear (about 30%: whole
sessions without candidates, plus an unexplained residue) - see
data/MANIFEST.md.

Read-only on the inputs. Writes ONE new file and refuses to start if it
already exists (never overwrites).

Usage: python3 pipeline/5_05_agentic_export_classification.py
"""
import csv
import glob
import importlib
import json
import os
import re
import sys

PIPELINE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PIPELINE_DIR)
sys.path.insert(0, os.path.join(PIPELINE_DIR, "4_02_agentic_intent_classification"))
prep = importlib.import_module("1_prepare_context")
iav = importlib.import_module("4_04_agentic_intent_adoption")

csv.field_size_limit(50_000_000)
OUT_DIR = os.path.join(PIPELINE_DIR, "out_vibe_coding")
OUT_PATH = os.path.join(OUT_DIR, "classification_per_pair_agentic.csv")
INTENT_DIR = os.path.join(PIPELINE_DIR, "4_02_agentic_intent_classification", "data")
TARGET = {"python", "javascript", "typescript", "java"}



def labels_only(labels):
    """Keep only the classification (main/sub category) of each label; the model's free-text
    reasoning is not published."""
    if isinstance(labels, list):
        return [{k: l.get(k) for k in ("main_category", "sub_category")} if isinstance(l, dict) else l
                for l in labels]
    return labels

def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip())


def main():
    if os.path.exists(OUT_PATH):
        sys.exit(f"{OUT_PATH} already exists: not overwriting it. Move/rename it if you want to regenerate it.")

    sha_per_link, group_per_sha = {}, {}
    with open(os.path.join(OUT_DIR, "conversations.jsonl"), encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            sha_per_link[c["conversation_url"]] = c["sha"]
            group_per_sha[c["sha"]] = "cli_agent" if c.get("is_cli_agent") == "yes" else "ide"

    msgid_per_key = {}
    with open(os.path.join(INTENT_DIR, "message_row_map.csv"), encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            msgid_per_key[r["row_id"]] = r["message_id"]

    labels = {}
    for path in sorted(glob.glob(os.path.join(INTENT_DIR, "parsed", "*.jsonl"))):
        for line in open(path, encoding="utf-8"):
            r = json.loads(line)
            if r.get("status") == "success":
                labels[str(r["message_id"])] = r
    lab = iav.primary_label().set_index("message_id")

    yes, no = iav.outcome_per_sha(OUT_DIR)

    cache = {}
    n_tot = n_written = 0
    tmp = OUT_PATH + ".tmp"
    fields = ["row_id", "sha", "conversation_url", "repo", "language", "prompt",
             "response_code", "message_id", "main_category", "sub_category",
             "all_labels", "session_adopted"]
    with open(os.path.join(OUT_DIR, "snippet.csv"), encoding="utf-8", newline="") as fin, \
         open(tmp, "w", encoding="utf-8", newline="") as fout:
        w = csv.DictWriter(fout, fieldnames=fields)
        w.writeheader()
        for r in csv.DictReader(fin):
            if r["language"] not in TARGET:
                continue
            n_tot += 1
            sha = sha_per_link.get(r["conversation_url"])
            if not sha:
                continue
            if sha not in cache:
                cache[sha] = prep.candidates_with_real_prompt(sha, group_per_sha.get(sha, "ide"))
            p = norm(r["prompt"])
            idx = next((i for i, c in enumerate(cache[sha]) if norm(c["prompt"]) == p), None)
            if idx is None:
                continue
            msgid = msgid_per_key.get(f"{sha}::{idx}")
            if msgid is None or msgid not in labels:
                continue
            rec = labels[msgid]
            mid = int(msgid)
            w.writerow({
                "row_id": r["row_id"], "sha": sha,
                "conversation_url": r["conversation_url"], "repo": r["repo"],
                "language": r["language"], "prompt": r["prompt"],
                "response_code": r["response_code"], "message_id": msgid,
                "main_category": lab.loc[mid, "main_category"] if mid in lab.index else "",
                "sub_category": lab.loc[mid, "sub_category"] if mid in lab.index else "",
                "all_labels": json.dumps(labels_only(rec.get("labels")), ensure_ascii=False),
                "session_adopted": "yes" if sha in yes else ("no" if sha in no else "unknown"),
            })
            n_written += 1
    os.rename(tmp, OUT_PATH)
    print(f"snippet.csv rows in the 4 languages: {n_tot}; with label: {n_written} ({100*n_written/n_tot:.1f}%)")
    print(f"Written {OUT_PATH}")


if __name__ == "__main__":
    main()
