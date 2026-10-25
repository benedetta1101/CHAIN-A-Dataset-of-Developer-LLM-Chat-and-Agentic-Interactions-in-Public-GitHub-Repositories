#!/usr/bin/env python3
"""
Agentic prompt -> code pairs for ALL languages, built on the
candidates of code written by the agent (diff, Write, prose), like
5_02_agentic_export_candidates_dataset.py but without the filter on the 4 target
languages (extended only in memory: no input file is modified).

Intent labels (macro/micro): ONLY for the 4 target languages
(python/javascript/typescript/java), taken from agentic_annotated_dataset.csv
(same (sha, index) -> same labels). For the other languages and for the
candidates of sessions not yet classified the `status` column says why
the label is missing (`not_classified_language_outside_target`,
`not_classified_session_not_processed`).

Columns: `idx` = position among ALL the candidates of the session (with all
languages); `idx_4lang` = position among the candidates in the 4 languages only (the
key used by message_row_map.csv / agentic_annotated_dataset.csv).
The language of diffs comes from the file extension; diffs without a
recognizable path stay `diff`.

Read-only on the inputs; writes ONE new file, refuses if it exists.
Usage: python3 pipeline/5_04_agentic_export_pairs_all_languages.py [working folder, default: the reference one]
"""
import csv
import importlib
import json
import os
import sys

PIPELINE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PIPELINE_DIR)
sys.path.insert(0, os.path.join(PIPELINE_DIR, "4_02_agentic_intent_classification"))
prep = importlib.import_module("1_prepare_context")
iav = importlib.import_module("4_04_agentic_intent_adoption")
e04 = importlib.import_module("1_04_extract_and_filter")

csv.field_size_limit(50_000_000)
OUT_DIR = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(PIPELINE_DIR, "out_vibe_coding")
OUT_PATH = os.path.join(OUT_DIR, "pairs_agentic_all_languages.csv")
T = {"python", "javascript", "typescript", "java"}


class _All:
    def __contains__(self, x):
        return True


def candidates_all(sha, source_group):
    """Candidates with type, all languages, same order as the extractor."""
    if source_group != "ide":
        return [{**c, "candidate_type": "cli_agent"} for c in prep.candidates_with_real_prompt(sha, source_group)]
    path = os.path.join(prep.MARKDOWNS_IDE_DIR, sha + ".md")
    if not os.path.exists(path):
        return []
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
                out.append({**c, "prompt": ultimo, "candidate_type": kind})
    return out


def main():
    if os.path.exists(OUT_PATH):
        sys.exit(f"{OUT_PATH} already exists: not overwriting it.")
    prep.LANGUAGES_TARGET = _All()  # in memory only

    labels = {}
    with open(os.path.join(OUT_DIR, "agentic_annotated_dataset.csv"), encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            labels[(r["sha"], int(r["candidate_index"]))] = (r["message_id"], r["status"], r["main_category"],
                                                    r["sub_category"], r["all_labels"])
    sessions_classified = {k[0] for k in labels}
    yes, no = iav.outcome_per_sha(OUT_DIR)

    english = {}
    fields = ["sha", "candidate_index", "candidate_index_4lang", "conversation_url", "repo", "source_group", "candidate_type",
             "language", "prompt", "code", "nontrivial_lines", "prompt_is_english", "message_id",
             "status", "main_category", "sub_category", "all_labels", "session_adopted",
             "harness_present", "harness_tools", "harness_mechanisms"]
    n = n_lab = n4 = n4_match = 0
    tmp = OUT_PATH + ".tmp"
    with open(os.path.join(OUT_DIR, "conversations.jsonl"), encoding="utf-8") as fin, \
         open(tmp, "w", encoding="utf-8", newline="") as fout:
        w = csv.DictWriter(fout, fieldnames=fields)
        w.writeheader()
        for line in fin:
            c = json.loads(line)
            sha = c["sha"]
            source_group = "cli_agent" if c.get("is_cli_agent") == "yes" else "ide"
            repo = json.dumps(sorted({a["repo"] for a in c["artifacts"] if a.get("repo")}), ensure_ascii=False)
            tools, mech = c.get("harness") or [], c.get("harness_mechanisms") or []
            idx4 = -1
            for idx, cand in enumerate(candidates_all(sha, source_group)):
                p = cand["prompt"]
                in4 = cand["language"] in T
                if in4:
                    idx4 += 1  # the official index also counts the candidates that are later discarded
                if not p or not p.strip():
                    continue
                if in4:
                    n4 += 1
                    lab = labels.get((sha, idx4)) if sha in sessions_classified else None
                    n4_match += lab is not None
                    if lab is None:
                        lab = ("", "not_classified_session_not_processed", "", "", "")
                else:
                    lab = ("", "not_classified_language_outside_target", "", "", "")
                if p not in english:
                    english[p] = "yes" if e04.is_english_prompt(p.strip()) else "no"
                n += 1
                n_lab += lab[1] == "success"
                w.writerow({
                    "sha": sha, "candidate_index": idx, "candidate_index_4lang": idx4 if in4 else "",
                    "conversation_url": c["conversation_url"], "repo": repo, "source_group": source_group,
                    "candidate_type": cand["candidate_type"], "language": cand["language"],
                    "prompt": p, "code": cand["response_code"],
                    "nontrivial_lines": e04.nontrivial_lines(cand["response_code"]),
                    "prompt_is_english": english[p], "message_id": lab[0], "status": lab[1],
                    "main_category": lab[2], "sub_category": lab[3], "all_labels": lab[4],
                    "session_adopted": "yes" if sha in yes else ("no" if sha in no else "unknown"),
                    "harness_present": "yes" if tools or mech else "no",
                    "harness_tools": ";".join(tools), "harness_mechanisms": ";".join(mech),
                })
    os.rename(tmp, OUT_PATH)
    print(f"Candidates written: {n}; with valid label: {n_lab}")
    print(f"Candidates in the 4 languages: {n4}; of which found in the annotated dataset: {n4_match} (expected {len(labels)})")
    print(f"Written {OUT_PATH}")


if __name__ == "__main__":
    main()
