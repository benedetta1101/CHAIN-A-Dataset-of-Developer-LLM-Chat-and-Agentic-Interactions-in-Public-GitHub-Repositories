#!/usr/bin/env python
"""
Step 3 - Normalization + conversation metadata (all providers unified).

Standalone: no imports from other scripts of the project. Usage:
    python pipeline/1_03_chat_normalize.py

Reads the raw outputs of steps 1-2 (already present, not redone here):
  - ChatGPT:      dataset_2023-05-01_2026-06-04.json (discovery+fetch together,
                  field "links[].conversation.turns" + "links[].referenced_by[]")
  - Claude/Gemini: cg_dataset.jsonl (per-conversation metadata) +
                   cg_dataset.turns.jsonl (full turns)

Writes A SINGLE unified file, independent of the provider:
    pipeline/out/conversations.jsonl

Schema per line (one line = one conversation):
  conversation_url, provider, model_slug, n_user_messages, n_llm_messages,
  n_total_messages, artifacts (list - a conversation may be
  referenced by several GitHub artifacts, all kept here, not just
  the first), turns (full list of {role, text}).

KNOWN LIMIT: for Claude/Gemini we have no artifact_url/sha/number/
created_at field (no native discovery snapshot for these providers
is available in this project, only artifact_type) - those fields stay
None for rows with provider in (claude, gemini). For ChatGPT they are
instead all populated when available in the raw snapshot.

Inclusion filter: only conversations with turns actually available
(status="ok" and non-empty turns for ChatGPT; presence in cg_dataset.turns.jsonl
for Claude/Gemini) - without turns there is nothing to extract in the
following steps.
"""
import argparse
import json
import os
import re
from collections import defaultdict

import ijson

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_CHATGPT_RAW = os.path.join(ROOT, "dataset_2023-05-01_2026-06-04.json")
DEFAULT_CG_DATASET = os.path.join(ROOT, "cg_dataset.jsonl")
DEFAULT_CG_TURNS = os.path.join(ROOT, "cg_dataset.turns.jsonl")
DEFAULT_OUT_DIR = os.path.join(SCRIPT_DIR, "out")

ARTIFACT_RE = {
    "issue": re.compile(r"/issues/(\d+)"),
    "pull": re.compile(r"/pull/(\d+)"),
    "blob": re.compile(r"/blob/([0-9a-fA-F]{7,40})/(.+)"),
    "commit": re.compile(r"/commit/([0-9a-fA-F]{7,40})"),
}

def parse_artifact(source, url):
    ref, path = None, None
    if url:
        if source == "GitHubIssue":
            m = ARTIFACT_RE["issue"].search(url)
            ref = m.group(1) if m else None
        elif source == "GitHubPullRequest":
            m = ARTIFACT_RE["pull"].search(url)
            ref = m.group(1) if m else None
        elif source == "GitHubCodeFile":
            m = ARTIFACT_RE["blob"].search(url)
            if m:
                ref, path = m.group(1), m.group(2)
        else:
            m = ARTIFACT_RE["commit"].search(url)
            ref = m.group(1) if m else None
    return ref, path


def process_chatgpt(out_f, chatgpt_raw):
    n_in = n_out = 0
    with open(chatgpt_raw, "rb") as f:
        for item in ijson.items(f, "links.item"):
            n_in += 1
            conv = item.get("conversation") or {}
            turns = conv.get("turns")
            if conv.get("status") != "ok" or not turns:
                continue

            artifacts = []
            for ref in item.get("referenced_by", []):
                source = ref.get("source")
                url = ref.get("artifact_url")
                reference, path = parse_artifact(source, url)
                artifacts.append({
                    "type": source, "reference": reference, "path": path,
                    "url": url, "created_at": ref.get("created_at"),
                    "repo": ref.get("repo"),
                })

            record = {
                "conversation_url": item.get("share_url"),
                "provider": "chatgpt",
                "model_slug": conv.get("model_slug"),
                "n_user_messages": conv.get("n_user_messages"),
                "n_llm_messages": conv.get("n_assistant_messages"),
                "n_total_messages": conv.get("n_turns"),
                "artifacts": artifacts,
                "turns": turns,
            }
            out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
            n_out += 1
    return n_in, n_out


def process_claude_gemini(out_f, cg_dataset, cg_turns):
    turns_by_link = {}
    with open(cg_turns, encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            turns_by_link[o["chat_link"]] = o["turns"]

    n_in = n_out = 0
    with open(cg_dataset, encoding="utf-8") as f:
        for line in f:
            n_in += 1
            o = json.loads(line)
            link = o["chat_link"]
            turns = turns_by_link.get(link)
            if not turns:
                continue

            record = {
                "conversation_url": link,
                "provider": o.get("provider"),
                "model_slug": o.get("model_slug"),
                "n_user_messages": o.get("n_user_messages"),
                "n_llm_messages": o.get("n_llm_messages"),
                "n_total_messages": o.get("n_total_messages"),
                "artifacts": [{
                    "type": o.get("artifact_type"), "reference": None, "path": None,
                    "url": None, "created_at": None, "repo": o.get("repo_name"),
                }],
                "turns": turns,
            }
            out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
            n_out += 1
    return n_in, n_out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--chatgpt-raw", default=DEFAULT_CHATGPT_RAW,
                    help="raw ChatGPT snapshot (discovery+fetch)")
    ap.add_argument("--cg-dataset", default=DEFAULT_CG_DATASET,
                    help="per-conversation Claude/Gemini metadata")
    ap.add_argument("--cg-turns", default=DEFAULT_CG_TURNS,
                    help="full Claude/Gemini turns")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                    help="output folder (default: pipeline/out)")
    args = ap.parse_args()

    out_path = os.path.join(args.out_dir, "conversations.jsonl")
    os.makedirs(args.out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as out_f:
        print("Normalizing ChatGPT...")
        n_in_c, n_out_c = process_chatgpt(out_f, args.chatgpt_raw)
        print(f"  {n_out_c}/{n_in_c} conversations included (status=ok and turns available)")

        print("Normalizing Claude/Gemini...")
        n_in_g, n_out_g = process_claude_gemini(out_f, args.cg_dataset, args.cg_turns)
        print(f"  {n_out_g}/{n_in_g} conversations included (turns available)")

    print(f"\nWritten {out_path}")
    print(f"Total unified conversations: {n_out_c + n_out_g}")
    print(f"  chatgpt: {n_out_c}")
    print(f"  claude+gemini: {n_out_g}")


if __name__ == "__main__":
    main()
