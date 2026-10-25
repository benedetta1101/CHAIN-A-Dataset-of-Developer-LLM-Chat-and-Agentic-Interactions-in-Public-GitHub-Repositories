#!/usr/bin/env python
"""
Merges the output of the two pipelines (prompt/chat + agentic IDE/CLI) into a
single set of files, with a 'dataset' column/field ('prompt'/'agentic') to
tell the provenance apart row by row.

The conversations.jsonl schemas are NOT identical (the agentic side has
'sha'/'is_cli_agent'/'models_used'/'has_sidechain' in addition, the chat one has
'n_user_messages'/'n_llm_messages'/'n_total_messages' in addition) - the merge is
by simple concatenation, each row carries only the fields of its own source
schema plus 'dataset'.

snippet.csv instead is already uniform (same script 1_04_extract_and_filter.py
for both datasets, same extraction methodology), except for the
fact that the chat side may have in addition the columns added by
1_05_chat_add_context.py (code_in_prompt/has_example/prompt_with_context/
full_prompt) if it was run - here we merge on the UNION of the
columns (those missing on one side stay empty), not on the intersection,
so as not to lose information when it is available on one side only.

Usage:
    python pipeline/7_01_merge_datasets.py --chat-dir out_ago2026 --agentic-dir out_vibe_coding --out-dir out_unified
    python pipeline/7_01_merge_datasets.py --chat-dir out_test100 --agentic-dir out_vibe_coding_test100 --out-dir out_unified_test100
"""
import argparse
import csv
import json
import os

csv.field_size_limit(50_000_000)


def merge_conversations(chat_dir, agentic_dir, out_dir):
    out_path = os.path.join(out_dir, "conversations.jsonl")
    n_chat = n_agentic = 0
    with open(out_path, "w", encoding="utf-8") as fout:
        for src_dir, label, counter in [(chat_dir, "prompt", "chat"), (agentic_dir, "agentic", "agentic")]:
            path = os.path.join(src_dir, "conversations.jsonl")
            if not os.path.exists(path):
                print(f"  WARNING: {path} does not exist, skipping.")
                continue
            n = 0
            with open(path, encoding="utf-8") as f:
                for line in f:
                    record = json.loads(line)
                    record["dataset"] = label
                    fout.write(json.dumps(record, ensure_ascii=False) + "\n")
                    n += 1
            if counter == "chat":
                n_chat = n
            else:
                n_agentic = n
    print(f"conversations.jsonl unified: {n_chat} prompt + {n_agentic} agentic = {n_chat + n_agentic} rows -> {out_path}")


def merge_snippet(chat_dir, agentic_dir, out_dir):
    """row_id of the two sources is NOT unique across them (both restart
    from 0, assigned independently by 1_04_extract_and_filter.py) - it must be
    renamed to row_id_origin (to trace back to the row in the source
    CSV) and replaced with a new row_id, unique over the whole unified
    file, same convention (progressive integer from 0) already used by
    1_04_extract_and_filter.py itself."""
    out_path = os.path.join(out_dir, "snippet.csv")
    rows = []
    fieldnames = ["dataset"]
    for src_dir, label in [(chat_dir, "prompt"), (agentic_dir, "agentic")]:
        path = os.path.join(src_dir, "snippet.csv")
        if not os.path.exists(path):
            print(f"  WARNING: {path} does not exist, skipping.")
            continue
        with open(path, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for col in reader.fieldnames:
                if col not in fieldnames:
                    fieldnames.append(col)
            for r in reader:
                r["dataset"] = label
                if "row_id" in r:
                    r["row_id_origin"] = r.pop("row_id")
                rows.append(r)

    # at this point fieldnames still contains the old "row_id" of the
    # sources (original header, not touched by the rename done row by row
    # above) - I remove it and put "row_id" (new, unique) + "row_id_origin"
    # at the front.
    fieldnames.remove("row_id")
    fieldnames = ["row_id", "row_id_origin"] + fieldnames

    for i, r in enumerate(rows):
        r["row_id"] = i

    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    n_prompt = sum(1 for r in rows if r["dataset"] == "prompt")
    n_agentic = sum(1 for r in rows if r["dataset"] == "agentic")
    print(f"snippet.csv unified: {n_prompt} prompt + {n_agentic} agentic = {len(rows)} rows, {len(fieldnames)} columns -> {out_path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chat-dir", required=True, help="out_* folder of the prompt/chat dataset (e.g. out_ago2026)")
    ap.add_argument("--agentic-dir", required=True, help="out_* folder of the agentic dataset (e.g. out_vibe_coding)")
    ap.add_argument("--out-dir", required=True, help="destination folder for the unified files")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    merge_conversations(args.chat_dir, args.agentic_dir, args.out_dir)
    merge_snippet(args.chat_dir, args.agentic_dir, args.out_dir)


if __name__ == "__main__":
    main()
