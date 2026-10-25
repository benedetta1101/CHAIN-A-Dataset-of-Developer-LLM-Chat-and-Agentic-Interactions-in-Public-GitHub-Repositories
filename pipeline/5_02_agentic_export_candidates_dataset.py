#!/usr/bin/env python3
"""
Agentic dataset built directly on the code CANDIDATES (same
definition used by the intent classification and the adoption
verification), instead of on snippet.csv (which by construction applies the filters
designed for chat: ``` blocks, English prompt, >= 5 non-trivial lines, 4
languages, step/alternative collapse).

One row = one candidate (session, index) = prompt -> code pair written
by the agent (added lines of a diff, content of a Write, or code block
in the prose), in the 4 target languages, with the intent of the PROMPT. It covers
only the sessions already classified (4,516): the candidates of the sessions in the
WORK SESSION / Claude Code Session Log formats do not have a label yet and
do not appear here.

No quality filter applied: the columns `prompt_is_english`,
`nontrivial_lines` and `candidate_type` allow trimming afterwards
(e.g. replicating the snippet.csv filter: prompt_is_english=yes and nontrivial_lines>=5).

Read-only on the inputs. Writes ONE new file and refuses to start if it
already exists (never overwrites).

Usage: python3 pipeline/5_02_agentic_export_candidates_dataset.py [working folder, default: the reference one]
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
e04 = importlib.import_module("1_04_extract_and_filter")

csv.field_size_limit(50_000_000)
OUT_DIR = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(PIPELINE_DIR, "out_vibe_coding")
OUT_PATH = os.path.join(OUT_DIR, "agentic_candidates_dataset.csv")
INTENT_DIR = os.path.join(PIPELINE_DIR, "4_02_agentic_intent_classification", "data")
TARGET = prep.LANGUAGES_TARGET



def labels_only(labels):
    """Keep only the classification (main/sub category) of each label; the model's free-text
    reasoning is not published."""
    if isinstance(labels, list):
        return [{k: l.get(k) for k in ("main_category", "sub_category")} if isinstance(l, dict) else l
                for l in labels]
    return labels

def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip())


def candidates_typed(sha, source_group):
    """Same candidates and same order as prep.candidates_with_real_prompt
    (on which the sha::idx indexes depend), plus the type."""
    base = prep.candidates_with_real_prompt(sha, source_group)
    if source_group != "ide":
        return [{**c, "candidate_type": "cli_agent"} for c in base]
    path = os.path.join(prep.MARKDOWNS_IDE_DIR, sha + ".md")
    ide = prep.ide
    out, ultimo = [], None
    for t in ide.parse_ide_turns(open(path, encoding="utf-8").read()):
        if t["role"] == "user":
            ultimo = t["text"]
            continue
        if not ultimo:
            continue
        for kind, items in (("diff", ide.extract_diff_ide(t["text"])), ("prose", ide.extract_prose_ide(t["text"]))):
            for c in items:
                if c["language"] in TARGET:
                    out.append({**c, "prompt": ultimo, "candidate_type": kind})
    # consistency check with the official extractor
    if [c["response_code"] for c in out] != [c["response_code"] for c in base]:
        return [{**c, "candidate_type": ""} for c in base]
    return out


def main():
    if os.path.exists(OUT_PATH):
        sys.exit(f"{OUT_PATH} already exists: not overwriting it. Move/rename it if you want to regenerate it.")

    msgid_per_key = {}
    with open(os.path.join(INTENT_DIR, "message_row_map.csv"), encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            msgid_per_key[r["row_id"]] = r["message_id"]
    sessions = {k.split("::")[0] for k in msgid_per_key}

    labels = {}
    for path in sorted(glob.glob(os.path.join(INTENT_DIR, "parsed", "*.jsonl"))):
        for line in open(path, encoding="utf-8"):
            r = json.loads(line)
            labels[str(r["message_id"])] = r
    lab = iav.primary_label().set_index("message_id")
    yes, no = iav.outcome_per_sha(OUT_DIR)

    snippet_prompt = {}
    with open(os.path.join(OUT_DIR, "snippet.csv"), encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r["language"] in TARGET:
                snippet_prompt.setdefault(r["conversation_url"], set()).add(norm(r["prompt"]))

    english = {}
    fields = ["sha", "candidate_index", "conversation_url", "repo", "source_group", "candidate_type", "language",
             "prompt", "code", "nontrivial_lines", "prompt_is_english", "message_id", "status",
             "main_category", "sub_category", "all_labels", "session_adopted",
             "prompt_in_snippet_csv"]
    tmp = OUT_PATH + ".tmp"
    n = n_lab = n_type_ko = 0
    with open(os.path.join(OUT_DIR, "conversations.jsonl"), encoding="utf-8") as fin, \
         open(tmp, "w", encoding="utf-8", newline="") as fout:
        w = csv.DictWriter(fout, fieldnames=fields)
        w.writeheader()
        for line in fin:
            c = json.loads(line)
            sha = c["sha"]
            if sha not in sessions:
                continue
            source_group = "cli_agent" if c.get("is_cli_agent") == "yes" else "ide"
            link = c["conversation_url"]
            repo = json.dumps(sorted({a["repo"] for a in c["artifacts"] if a.get("repo")}), ensure_ascii=False)
            for idx, cand in enumerate(candidates_typed(sha, source_group)):
                msgid = msgid_per_key.get(f"{sha}::{idx}")
                if msgid is None:
                    continue
                p = cand["prompt"]
                if p not in english:
                    english[p] = "yes" if e04.is_english_prompt(p.strip()) else "no"
                rec = labels.get(msgid)
                ok = rec is not None and rec.get("status") == "success"
                mid = int(msgid)
                n += 1
                n_lab += ok
                n_type_ko += cand["candidate_type"] == ""
                w.writerow({
                    "sha": sha, "candidate_index": idx, "conversation_url": link, "repo": repo,
                    "source_group": source_group, "candidate_type": cand["candidate_type"],
                    "language": cand["language"], "prompt": p, "code": cand["response_code"],
                    "nontrivial_lines": e04.nontrivial_lines(cand["response_code"]),
                    "prompt_is_english": english[p], "message_id": msgid,
                    "status": rec.get("status") if rec else "no_result",
                    "main_category": lab.loc[mid, "main_category"] if ok and mid in lab.index else "",
                    "sub_category": lab.loc[mid, "sub_category"] if ok and mid in lab.index else "",
                    "all_labels": json.dumps(labels_only(rec.get("labels")), ensure_ascii=False) if ok else "",
                    "session_adopted": "yes" if sha in yes else ("no" if sha in no else "unknown"),
                    "prompt_in_snippet_csv": "yes" if norm(p) in snippet_prompt.get(link, ()) else "no",
                })
    os.rename(tmp, OUT_PATH)
    print(f"Candidates written: {n}; with valid label: {n_lab}; undetermined type: {n_type_ko}")
    print(f"Written {OUT_PATH}")


if __name__ == "__main__":
    main()
