#!/usr/bin/env python3
"""
Prepares (free, local, WITHOUT sending anything) the messages to classify for the
CLI-agent sessions in the "WORK SESSION" / "Claude Code Session Log" formats,
made readable by 1_prepare_context._candidates_formats_alternative.

Writes into a SEPARATE folder (data_new_formats/), not into data/: the
message_ids continue after the maximum already used in data/message_row_map.csv,
so the new messages do not collide with the labels already paid for in data/parsed/
and can be appended/merged after classification without renumbering anything.
Considers only sessions that do NOT already appear in data/message_row_map.csv.
Does not overwrite: stops if the output folder already exists.

NOTE: 2_classify.py uses a fixed DATA_DIR (data/) - to send these messages
an explicit and deliberate step is needed (paid, OpenAI Batch API), not
done here.

Usage: python3 pipeline/4_02_agentic_intent_classification/1b_prepare_context_new_formats.py
"""
import csv
import importlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
prep = importlib.import_module("1_prepare_context")

OUT = os.path.join(HERE, "data_new_formats")


def main():
    if os.path.exists(OUT):
        sys.exit(f"{OUT} already exists: not overwriting it.")

    already = set()
    max_id = -1
    with open(os.path.join(prep.DATA_DIR, "message_row_map.csv"), encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            already.add(r["row_id"].split("::")[0])
            max_id = max(max_id, int(r["message_id"]))
    print(f"Sessions already classified: {len(already)}; last message_id used: {max_id}")

    seen, messages, map_rows = {}, [], []
    with open(os.path.join(prep.OUT_DIR, "conversations.jsonl"), encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            sha = c["sha"]
            if sha in already or c.get("is_cli_agent") != "yes":
                continue
            candidates = prep.candidates_with_real_prompt(sha, "cli_agent")
            turns = c.get("turns", [])
            for idx_c, cand in enumerate(candidates):
                prompt = cand.get("prompt", "")
                if not prompt.strip():
                    continue
                key = (sha, prompt)
                if key in seen:
                    map_rows.append((seen[key], f"{sha}::{idx_c}"))
                    continue
                idx = next((i for i, t in enumerate(turns) if t["role"] == "user" and t["text"] == prompt), None)
                context = ""
                if idx is not None:
                    prev_a = turns[idx - 1]["text"] if idx > 0 and turns[idx - 1]["role"] in ("assistant", "agent") else ""
                    prev_u = turns[idx - 2]["text"] if idx > 1 and turns[idx - 2]["role"] == "user" else ""
                    if prev_u or prev_a:
                        context = (f"[prev_user]: {prep.head_tail(prev_u, prep.CTX_LIMIT)}\n"
                                   f"[prev_assistant]: {prep.head_tail(prev_a, prep.CTX_LIMIT)}")
                mid = max_id + 1 + len(messages)
                seen[key] = mid
                messages.append({"message_id": mid, "conversation_url": c.get("conversation_url", ""),
                                 "context": context,
                                 "truncated_content": prep.head_tail(prompt, prep.CURRENT_LIMIT)})
                map_rows.append((mid, f"{sha}::{idx_c}"))

    os.makedirs(OUT)
    with open(os.path.join(OUT, "messages.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["message_id", "conversation_url", "context", "truncated_content"])
        w.writeheader()
        w.writerows(messages)
    with open(os.path.join(OUT, "message_row_map.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["message_id", "row_id"])
        w.writerows(map_rows)
    print(f"New unique messages: {len(messages)}; candidates mapped: {len(map_rows)}")
    print(f"Written {OUT}/ (message_id da {max_id + 1} a {max_id + len(messages)})")


if __name__ == "__main__":
    main()
