#!/usr/bin/env python3
"""
ANNOTATED chat dataset: rows of out_ago2026/snippet.csv in the 4 target languages
(python/javascript/typescript/java) with
  - adoption (snippet_timeline.csv): `adopted` = yes ONLY if the code was
    found as an EXACT COPY in the repo ("as is": all mechanisms except the
    similarity of step 8); no = not found or found only by
    similarity (the 830 "modified" rows do NOT count as adopted);
    not_verifiable = adoption could not be traced (no file path or pull-request
    number is known for the cited link, as for Claude and Gemini rows). For adopted rows: also
    found_via, found_path, found_commit, found_date, days_elapsed.
  - classification of the PROMPT (classification_per_row_4lang.csv): macro and
    micro category, in the same format as the agentic dataset (without a numeric
    prefix in the macro category).
The full text of the response stays in snippet.csv (same row_id): here
only the extracted code, to keep the file manageable.

Read-only on the inputs; writes ONE new file, refuses if it exists.
Usage: python3 pipeline/5_01_chat_export_annotated_dataset.py [working folder, default: the reference one]
"""
import csv, os, re, sys

csv.field_size_limit(50_000_000)
P = os.path.dirname(os.path.abspath(__file__))
D = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(P, "out_ago2026")
OUT = os.path.join(D, "chat_annotated_dataset.csv")
TARGET = {"python", "javascript", "typescript", "java"}


def main():
    if os.path.exists(OUT):
        sys.exit(f"{OUT} already exists: not overwriting it.")
    tl = {}
    with open(os.path.join(D, "snippet_timeline.csv"), encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            tl[r["row_id"]] = r
    lab = {}
    with open(os.path.join(P, "4_01_chat_intent_classification", "data", "classification_per_row_4lang.csv"),
              encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            lab[r["row_id"]] = r
    fields = ["row_id", "conversation_url", "provider", "repo", "model", "language", "prompt",
             "response_code", "adopted", "found_via", "found_path", "found_commit",
             "found_date", "days_elapsed", "message_id", "status", "main_category", "sub_category"]
    n = 0
    tmp = OUT + ".tmp"
    with open(os.path.join(D, "snippet.csv"), encoding="utf-8", newline="") as fin, \
         open(tmp, "w", encoding="utf-8", newline="") as fout:
        w = csv.DictWriter(fout, fieldnames=fields)
        w.writeheader()
        for r in csv.DictReader(fin):
            if r["language"] not in TARGET:
                continue
            t = tl.get(r["row_id"])
            if t is None:
                adopt, t = "not_verifiable", {}
            elif t["found"] == "yes" and t["found_via"] != "similarity":
                adopt = "yes"
            else:
                adopt = "no"
            l = lab.get(r["row_id"], {})
            ok = l.get("status") == "success"
            w.writerow({
                "row_id": r["row_id"], "conversation_url": r["conversation_url"],
                "provider": r["provider"], "repo": r["repo"], "model": r["model"],
                "language": r["language"], "prompt": r["prompt"], "response_code": r["response_code"],
                "adopted": adopt,
                "found_via": t.get("found_via", "") if adopt == "yes" else "",
                "found_path": t.get("found_path", "") if adopt == "yes" else "", "found_commit": t.get("found_commit", "") if adopt == "yes" else "",
                "found_date": t.get("found_date", "") if adopt == "yes" else "", "days_elapsed": t.get("days_elapsed", "") if adopt == "yes" else "",
                "message_id": l.get("message_id", ""), "status": l.get("status", "no_result"),
                "main_category": re.sub(r"^\d+\.\s*", "", l["main_category"]) if ok else "",
                "sub_category": l["sub_category"] if ok else "",
            })
            n += 1
    os.rename(tmp, OUT)
    print(f"Rows: {n}\nWritten {OUT}")


if __name__ == "__main__":
    main()
