#!/usr/bin/env python3
"""
Step 1 (vibe-coding) - Equivalent of
4_01_chat_intent_classification/1_prepare_context.py for the vibe-coding dataset
(IDE + CLI-agent), restricted to the 4 target languages (python/javascript/
typescript/java). Same head-tail truncation logic of 1000
characters/60-40 (Tang et al. sec. 3.2.2), same output format
(messages.csv + message_row_map.csv) so 2_classify.py (identical,
copied from 4_01_chat_intent_classification/) works without changes.

A "message" here is a USER turn whose response produced a clean code
candidate in one of the 4 languages (same candidates
already used by the adoption pipeline - camp.extract_snippet_clean/
ide.extract_diff_ide/extract_prose_ide). The "read" candidates
(last Read of a file, no real associated user turn - synthetic prompt
"(last read of X)") are EXCLUDED: they have no behavioral intent
to classify, they are not a user request.

Usage: python3 1_prepare_context.py
"""
import csv
import importlib
import json
import os
import re
import sys

PIPELINE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PIPELINE_DIR)
camp = importlib.import_module("lib_cli_agent_model_parser")
ide = importlib.import_module("lib_ide_session_parser")

csv.field_size_limit(50_000_000)

OUT_DIR = os.path.join(PIPELINE_DIR, "out_vibe_coding")
MARKDOWNS_CLI_DIR = os.path.join(PIPELINE_DIR, "1_07_agentic_scraper", "data", "markdowns_cli")
MARKDOWNS_IDE_DIR = os.path.join(PIPELINE_DIR, "1_07_agentic_scraper", "data", "markdowns")
PARSED_CLI_DIR = os.path.join(PIPELINE_DIR, "1_07_agentic_scraper", "data", "parsed_chats_simple_cli")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

LANGUAGES_TARGET = {"python", "javascript", "typescript", "java"}
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


_TOOL_INPUT_RE = re.compile(r"\*\*Tool:\*\*\s*(\w+)\s*\n\*\*Input:\*\*\s*```json\n(.*?)```", re.S)
_FENCE_PROSE_RE = re.compile(r"```([a-zA-Z0-9_+#.\-]*)\n(.*?)```", re.S)


def _candidates_formats_alternative(sha):
    """Candidates for the CLI-agent sessions in the "WORK SESSION" and
    "Claude Code Session Log" formats (previously not read: the extractor only knew
    SpecStory). Same candidate definition as the other formats: only
    code written by the agent - input of the Write (content) and Edit
    (new_string, unlike SpecStory preserved here) tools + fences in the
    agent's prose; NEVER output of Bash/Read/Grep/Glob/etc. (command
    results, not written code). The prompt is the last "real" user turn
    (not the tool "**Result:**" entries, which in these formats
    also have the user role). Reads the already parsed messages
    (parsed_chats_simple_cli, the same ones conversations.jsonl comes from
    for these formats) - do not import 3_parse_cli.py, it runs main() at
    module level and rewrites those files. By construction it changes
    nothing for the SpecStory/IDE formats (existing idx_c indexes unchanged)."""
    path = os.path.join(PARSED_CLI_DIR, sha + ".md.json")
    if not os.path.exists(path):
        return []
    detect_language = camp._detect_language_reliable()
    out = []
    ultimo_prompt = None
    for m in json.load(open(path, encoding="utf-8")).get("messages", []):
        role = (m.get("role") or "").strip().lower()
        text = m.get("content") or ""
        if role == "user":
            if text.strip() and not text.lstrip().startswith("**Result:**"):
                ultimo_prompt = text
            continue
        if not ultimo_prompt:
            continue
        tool = _TOOL_INPUT_RE.match(text.lstrip())
        if tool:
            nome = tool.group(1)
            if nome not in ("Write", "Edit"):
                continue
            try:
                inp = json.loads(tool.group(2))
            except ValueError:
                continue
            body = (inp.get("content") if nome == "Write" else inp.get("new_string")) or ""
            ext = os.path.splitext(inp.get("file_path") or "")[1].lstrip(".")
            candidates = [(ext, body)]
        elif text.lstrip().startswith("**Tool:**"):
            continue
        else:
            candidates = _FENCE_PROSE_RE.findall(text)
        for declared, body in candidates:
            body = body.strip()
            if not body:
                continue
            lang = detect_language(declared or None, body)
            if lang in camp._NOT_CODE or lang not in LANGUAGES_TARGET:
                continue
            out.append({"prompt": ultimo_prompt, "response_code": body, "language": lang})
    return out


def candidates_with_real_prompt(sha, source_group):
    """Only the candidates with a real associated user turn (written for
    CLI-agent by camp.extract_snippet_clean, diff/prose for IDE associated
    here to the immediately preceding user turn via ide.parse_ide_turns -
    NEVER the "read" ones (extract_reads_ide, no associated user turn,
    no intent to classify - not called)."""
    if source_group == "cli_agent":
        path = os.path.join(MARKDOWNS_CLI_DIR, sha + ".md")
        if not os.path.exists(path):
            return []
        text = open(path, encoding="utf-8").read()
        fmt = camp.detect_format(text)
        if fmt in ("work_session", "claude_session"):
            return _candidates_formats_alternative(sha)
        if fmt != "specstory":
            return []
        return [c for c in camp.extract_snippet_clean(text) if c["language"] in LANGUAGES_TARGET]
    path = os.path.join(MARKDOWNS_IDE_DIR, sha + ".md")
    if not os.path.exists(path):
        return []
    text = open(path, encoding="utf-8").read()
    turns = ide.parse_ide_turns(text)
    out = []
    ultimo_prompt = None
    for t in turns:
        if t["role"] == "user":
            ultimo_prompt = t["text"]
            continue
        if not ultimo_prompt:
            continue
        cand = ide.extract_diff_ide(t["text"]) + ide.extract_prose_ide(t["text"])
        for c in cand:
            if c["language"] not in LANGUAGES_TARGET:
                continue
            out.append({**c, "prompt": ultimo_prompt})
    return out


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    print("Loading conversations.jsonl...")
    conv_per_sha = {}
    with open(os.path.join(OUT_DIR, "conversations.jsonl"), encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            conv_per_sha[c["sha"]] = c

    print(f"Total sessions: {len(conv_per_sha)}")

    seen = {}  # (sha, prompt) -> message_id
    messages = []
    map_rows = []
    n_no_turn = 0

    for i, (sha, c) in enumerate(conv_per_sha.items(), 1):
        if i % 2000 == 0:
            print(f"  [{i}/{len(conv_per_sha)}]  messages so far: {len(messages)}")
        source_group = "cli_agent" if c.get("is_cli_agent") == "yes" else "ide"
        candidates = candidates_with_real_prompt(sha, source_group)
        if not candidates:
            continue

        turns = c.get("turns", [])
        for idx_c, cand in enumerate(candidates):
            prompt = cand.get("prompt", "")
            if not prompt.strip():
                continue
            key = (sha, prompt)
            if key in seen:
                map_rows.append((seen[key], f"{sha}::{idx_c}"))
                continue

            idx = None
            for i_t, t in enumerate(turns):
                if t["role"] == "user" and t["text"] == prompt:
                    idx = i_t
                    break
            if idx is None:
                n_no_turn += 1
                context = ""
            else:
                prev_assistant = turns[idx - 1]["text"] if idx > 0 and turns[idx - 1]["role"] in ("assistant", "agent") else ""
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
                "conversation_url": c.get("conversation_url", ""),
                "context": context,
                "truncated_content": head_tail(prompt, CURRENT_LIMIT),
            })
            map_rows.append((message_id, f"{sha}::{idx_c}"))

    print(f"\nUnique messages (real prompt, 4 languages): {len(messages)}")
    print(f"Total candidates mapped: {len(map_rows)}")
    print(f"User turn not found (empty context): {n_no_turn}")

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
