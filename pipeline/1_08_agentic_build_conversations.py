#!/usr/bin/env python
"""
Adapts the new dataset collected with pipeline/1_07_agentic_scraper/1_scrape_pool.py
(standard IDE sessions via 2_parse.py + CLI-agent sessions via 3_parse_cli.py) to the
conversations.jsonl format of OUR pipeline, so that
1_04_extract_and_filter.py can be reused as is (same code block
extraction, same language detection, same English filter, same
step/alternative collapse - identical methodology used for out_ago2026, necessary
for a valid agent/model comparison).

The 'provider' column in the resulting snippet.csv carries the value of the
"platform" field of the original parser (e.g. "Cursor / Copilot" for the
standard sessions, "Claude Code 2" for the CLI-agent ones) - it is the column
to use for the ide/cli_agent split downstream.

For the CLI-agent sessions, each turn also carries the SPECIFIC MODEL
used (e.g. "claude-opus-4-6", "claude-haiku-4-5-20251001" for the
sidechains, and also non-Anthropic models like "gpt-5.6-sol") and the
sidechain flag (sub-agent) - information present in the raw markdown but
discarded by the original parser (3_parse_cli.py), extracted here with
lib_cli_agent_model_parser.py. Available only for the "specstory" format
(~62% of the CLI-agent sessions, the only one of the 3 formats that reports it) -
for claude_session/work_session the model stays None.

Usage:
    python pipeline/1_08_agentic_build_conversations.py
    python pipeline/1_04_extract_and_filter.py --out-dir out_vibe_coding
"""
import importlib
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
camp = importlib.import_module("lib_cli_agent_model_parser")

SCRAPER_DIR = os.path.join(SCRIPT_DIR, "1_07_agentic_scraper", "data")
MARKDOWNS_CLI_DIR = os.path.join(SCRAPER_DIR, "markdowns_cli")
MARKDOWNS_DIR = os.path.join(SCRAPER_DIR, "markdowns")
OUT_DIR = os.path.join(SCRIPT_DIR, "out_vibe_coding")
PARSED_DIRS = [
    (os.path.join(SCRAPER_DIR, "parsed_chats_simple"), "no"),
    (os.path.join(SCRAPER_DIR, "parsed_chats_simple_cli"), "yes"),
]


def turns_cli_with_model(sha, messages_fallback):
    """For a CLI-agent session, tries to re-extract the turns from the raw markdown
    with model/sidechain/timestamp; if the format is not
    'specstory' (no model available) or the raw file is missing,
    falls back to the already-parsed messages (role/content, model=None)."""
    raw_path = os.path.join(MARKDOWNS_CLI_DIR, sha + ".md")
    if os.path.exists(raw_path):
        with open(raw_path, encoding="utf-8") as f:
            raw_text = f.read()
        if camp.detect_format(raw_text) == "specstory":
            turns = camp.parse_specstory_with_model(raw_text)
            if turns:
                return turns
    return [
        {"role": (m.get("role") or "").strip().lower(), "text": m.get("content") or "",
         "model": None, "sidechain": False, "timestamp": None}
        for m in messages_fallback
    ]


def turns_ide_with_model(sha, messages):
    """For an IDE session, the parsed messages (role/content, unchanged) plus the model / sidechain / timestamp read
    from the header of each message in the raw markdown (e.g. "Agent (gpt-5.4 2026-04-27 06:21:38Z)"). The headers
    are split exactly like the messages were, so they are matched by position; if the numbers differ (or the raw
    file is missing) the model stays None."""
    turns = [{"role": (m.get("role") or "").strip().lower(), "text": m.get("content") or "",
              "model": None, "sidechain": False, "timestamp": None} for m in messages]
    raw_path = os.path.join(MARKDOWNS_DIR, sha + ".md")
    if not os.path.exists(raw_path):
        return turns
    with open(raw_path, encoding="utf-8") as f:
        headers = camp.ide_message_headers(f.read())
    if len(headers) != len(turns):
        return turns
    for turn, header in zip(turns, headers):
        turn["model"], turn["sidechain"], turn["timestamp"] = camp.parse_header_text(header)
    return turns


def load_sha_meta():
    """sha -> (html_url, repository_full_name) da searches.json."""
    with open(os.path.join(SCRAPER_DIR, "searches.json"), encoding="utf-8") as f:
        items = json.load(f)
    meta = {}
    for it in items:
        meta[it["sha"]] = (it.get("html_url") or it.get("url"),
                            it.get("repository", {}).get("full_name"))
    return meta


def main():
    sha_meta = load_sha_meta()
    print(f"Sha metadata loaded: {len(sha_meta)}")

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, "conversations.jsonl")

    n_written = n_saltate_meta = n_skipped_empty = 0
    with open(out_path, "w", encoding="utf-8") as fout:
        for parsed_dir, is_cli_agent in PARSED_DIRS:
            if not os.path.isdir(parsed_dir):
                print(f"  WARNING: {parsed_dir} does not exist, skipping.")
                continue
            # sorted() - os.listdir() does not guarantee a stable order across
            # different runs, especially after adding new files (real bug:
            # without it, the order of conversations.jsonl changes at every
            # rebuild, and with it ALL the message_ids assigned
            # downstream by 1_prepare_context.py - silently misaligns
            # the classifications already done with respect to the regenerated map,
            # without any visible error. Discovered on 14/10 while rebuilding
            # the dataset and testing the classification orchestrator.
            files = sorted(f for f in os.listdir(parsed_dir) if f.endswith(".md.json"))
            print(f"Processing {parsed_dir}: {len(files)} files...")
            for fn in files:
                sha = fn[: -len(".md.json")]
                html_url, repo = sha_meta.get(sha, (None, None))
                if not html_url or not repo:
                    n_saltate_meta += 1
                    continue

                with open(os.path.join(parsed_dir, fn), encoding="utf-8") as f:
                    chat = json.load(f)
                messages = chat.get("messages") or []
                if not messages:
                    n_skipped_empty += 1
                    continue

                if is_cli_agent == "yes":
                    turns = turns_cli_with_model(sha, messages)
                else:
                    turns = turns_ide_with_model(sha, messages)
                models_used = sorted({t["model"] for t in turns if t.get("model")})

                record = {
                    "conversation_url": html_url,
                    "provider": chat.get("platform") or ("cli-agent" if is_cli_agent == "yes" else "ide"),
                    "artifacts": [{"repo": repo}],
                    "model_slug": None,
                    "is_cli_agent": is_cli_agent,
                    "sha": sha,
                    "title": chat.get("title"),
                    "timestamp": chat.get("timestamp"),
                    "models_used": models_used,
                    "has_sidechain": any(t.get("sidechain") for t in turns),
                    "turns": turns,
                }
                fout.write(json.dumps(record, ensure_ascii=False) + "\n")
                n_written += 1

    print(f"\nConversations written: {n_written}")
    print(f"Skipped (missing sha metadata): {n_saltate_meta}")
    print(f"Skipped (no messages): {n_skipped_empty}")
    print(f"Written {out_path}")


if __name__ == "__main__":
    main()
