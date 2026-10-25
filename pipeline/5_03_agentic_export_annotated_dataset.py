#!/usr/bin/env python3
"""
ANNOTATED agentic dataset: agentic_candidates_dataset.csv (prompt ->
code pairs in the 4 languages, with macro/micro intent and adoption) + harness
information of the session, taken from conversations.jsonl (fields `harness` and
`harness_mechanisms`, already corrected by date: a harness added to the repo
AFTER the session does not count - see 3_03_agentic_fix_harness_temporal.py).

Columns added: harness_present (yes/no), harness_tools (e.g. "Cursor;Claude
Code"), harness_mechanisms (e.g. "context_files;rules"). The paths of the individual
harness files are in harness_config.csv / harness_files/ (per repo).

Adoption: `session_adopted` is at SESSION level (the code of that
session ended up in the repo), not of the single pair.

Read-only on the inputs; writes ONE new file, refuses if it exists.
Usage: python3 pipeline/5_03_agentic_export_annotated_dataset.py [working folder, default: the reference one]
"""
import csv, json, os, sys

csv.field_size_limit(50_000_000)
D = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "out_vibe_coding")
IN, OUT = os.path.join(D, "agentic_candidates_dataset.csv"), os.path.join(D, "agentic_annotated_dataset.csv")


def main():
    if os.path.exists(OUT):
        sys.exit(f"{OUT} already exists: not overwriting it.")
    harness = {}
    with open(os.path.join(D, "conversations.jsonl"), encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            harness[c["sha"]] = (c.get("harness") or [], c.get("harness_mechanisms") or [])
    n = n_h = 0
    tmp = OUT + ".tmp"
    with open(IN, encoding="utf-8", newline="") as fin, open(tmp, "w", encoding="utf-8", newline="") as fout:
        r = csv.DictReader(fin)
        w = csv.DictWriter(fout, fieldnames=r.fieldnames + ["harness_present", "harness_tools", "harness_mechanisms"])
        w.writeheader()
        for row in r:
            tools, mech = harness.get(row["sha"], ([], []))
            row["harness_present"] = "yes" if tools or mech else "no"
            row["harness_tools"] = ";".join(tools)
            row["harness_mechanisms"] = ";".join(mech)
            w.writerow(row)
            n += 1
            n_h += row["harness_present"] == "yes"
    os.rename(tmp, OUT)
    print(f"Rows: {n}; with harness: {n_h}\nWritten {OUT}")


if __name__ == "__main__":
    main()
