#!/usr/bin/env python3
"""
Step 1 - Extracts the unique messages (conversation_url, prompt) from snippet.csv,
builds for each one the [prev_user]/[prev_assistant] context from
conversations.jsonl (head-tail truncation 60/40 at 1000 chars, as per the
Tang et al. 2026 paper sec. 3.2.2), and writes:
  - data/messages.csv       (message_id, conversation_url, context, truncated_content)
  - data/message_row_map.csv (message_id, row_id)  -- to carry the classification
    results over to ALL the rows of snippet.csv that share the
    same (conversation_url, prompt), not just the first.

Usage: python3 pipeline/4_01_chat_intent_classification/1_prepare_context.py [--out-dir pipeline/outXXX]

Reads snippet.csv and conversations.jsonl of --out-dir (default: out_ago2026).
"""
import argparse
import csv
import json
import os

csv.field_size_limit(50_000_000)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "out_ago2026")
DATA_DIR = os.path.join(SCRIPT_DIR, "data")

CTX_LIMIT = 1000
CTX_HEAD_FRAC = 0.6
CURRENT_LIMIT = 10000


def head_tail(s, limit, head_frac=CTX_HEAD_FRAC):
    s = s or ""
    if len(s) <= limit:
        return s
    h = int(limit * head_frac)
    t = limit - h
    return s[:h] + " ... " + s[-t:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                     help="pipeline/outXXX folder to read snippet.csv and conversations.jsonl from (default: out_ago2026)")
    args = ap.parse_args()
    conv_path = os.path.join(args.out_dir, "conversations.jsonl")

    os.makedirs(DATA_DIR, exist_ok=True)

    print("Reading snippet.csv...")
    with open(os.path.join(args.out_dir, "snippet.csv"), encoding="utf-8", newline="") as f:
        rows = [{"row_id": r["row_id"], "conversation_url": r["conversation_url"], "prompt": r["prompt"]}
                for r in csv.DictReader(f)]
    rows.sort(key=lambda r: int(r["row_id"]))
    print(f"  {len(rows)} total rows")

    # byte offset of each conversation (first occurrence of a link wins), to read the turns on demand
    # without loading the whole conversations.jsonl in memory
    print("Indexing conversations.jsonl...")
    offsets = {}
    offset = 0
    with open(conv_path, "rb") as f:
        for line in f:
            link = json.loads(line)["conversation_url"]
            offsets.setdefault(link, (offset, len(line)))
            offset += len(line)

    f_conv = open(conv_path, "rb")

    def turns_for_link(link):
        off_blen = offsets.get(link)
        if not off_blen:
            return None
        off, blen = off_blen
        f_conv.seek(off)
        line = f_conv.read(blen)
        return json.loads(line)["turns"]

    seen = {}  # (link, prompt) -> message_id
    messages = []  # rows for messages.csv
    map_rows = []  # rows for message_row_map.csv
    turns_cache = {}  # link -> turns, avoids rereading for conversations with several rows

    n_no_conv = n_no_match = 0
    for r in rows:
        link, prompt, row_id = r["conversation_url"], r["prompt"], r["row_id"]
        key = (link, prompt)
        if key in seen:
            map_rows.append((seen[key], row_id))
            continue

        if link not in turns_cache:
            turns_cache[link] = turns_for_link(link)
        turns = turns_cache[link]

        if not turns:
            n_no_conv += 1
            context = ""
        else:
            idx = None
            for i, t in enumerate(turns):
                if t["role"] == "user" and t["text"] == prompt:
                    idx = i
                    break
            if idx is None:
                n_no_match += 1
                context = ""
            else:
                prev_assistant = turns[idx - 1]["text"] if idx > 0 and turns[idx - 1]["role"] == "assistant" else ""
                prev_user = turns[idx - 2]["text"] if idx > 1 and turns[idx - 2]["role"] == "user" else ""
                if prev_user or prev_assistant:
                    context = (f"[prev_user]: {head_tail(prev_user, CTX_LIMIT)}\n"
                               f"[prev_assistant]: {head_tail(prev_assistant, CTX_LIMIT)}")
                else:
                    context = ""

        message_id = len(messages)
        seen[key] = message_id
        messages.append({
            "message_id": message_id,
            "conversation_url": link,
            "context": context,
            "truncated_content": head_tail(prompt, CURRENT_LIMIT),
        })
        map_rows.append((message_id, row_id))

    f_conv.close()

    print(f"  unique messages: {len(messages)}  (conversation not found: {n_no_conv}, "
          f"prompt not found in the conversation: {n_no_match})")

    with open(os.path.join(DATA_DIR, "messages.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["message_id", "conversation_url", "context", "truncated_content"])
        w.writeheader()
        w.writerows(messages)

    with open(os.path.join(DATA_DIR, "message_row_map.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["message_id", "row_id"])
        w.writerows(map_rows)

    print(f"Written {DATA_DIR}/messages.csv e message_row_map.csv")


if __name__ == "__main__":
    main()
