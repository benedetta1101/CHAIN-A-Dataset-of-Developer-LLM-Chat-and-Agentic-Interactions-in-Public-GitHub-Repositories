#!/usr/bin/env python
"""
Extracts, for each "Agent" turn of the CLI-agent sessions (Claude Code) of the
new vibe-coding dataset, the SPECIFIC MODEL used - information
present in the raw markdown (markdowns_cli/*.md) but DISCARDED by the
original parser (3_parse_cli.py::normalize_role collapses everything to "Assistant",
throwing away the model name, the exact timestamp and the sidechain flag).

Headers observed in the "specstory" format (the only one of the 3 formats that carries
this information - claude_session and work_session only have
"### User"/"### Assistant" headers, no model):
  _**Agent (claude-opus-4-6 2026-02-26 06:54:43Z)**_
  _**Agent - sidechain (claude-haiku-4-5-20251001 2026-02-26 06:54:49Z)**_
  _**Agent (claude-opus-4-5-20251101) (sidechain)**_
  _**Agent (claude-opus-4-7 )**_          <- no timestamp in some files
  _**User (2026-03-10 08:58:50Z)**_       <- Users have no model
  _**Agent **_ / _**User **_              <- no model nor timestamp

The observed models also include non-Anthropic ones (e.g. "MiniMax-M2.7",
"gpt-5.6-sol") and a "<synthetic>" placeholder - a sign that Claude Code (or
the tool that generated these logs) can route to different models/providers
for sub-tasks (sidechain), not just Claude.

Use as a library:
    from lib_cli_agent_model_parser import parse_specstory_with_model
    turns = parse_specstory_with_model(raw_markdown_text)
    # -> [{"role": "user"/"assistant", "text": ..., "model": ..|None,
    #      "sidechain": bool, "timestamp": ..|None}, ...]
"""
import re

_HEADER_RE = re.compile(
    r"_\*\*(User|Agent)(\s*-\s*sidechain)?\s*\(([^)]*)\)(\s*\(sidechain\))?\s*\*\*_"
    r"\n+"
)
# two timestamp formats observed: "2026-02-26 06:54:43Z" (space, no ms)
# and "2026-01-13T09:17:13.650Z" (ISO with T and milliseconds)
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")
# fallback: if after removing the timestamp only punctuation/digits remain
# (no letters), it is not a valid model name - e.g. "(sidechain)"
# mistakenly captured in the model group instead of in the dedicated suffix
_ONLY_PUNCTUATION_RE = re.compile(r"^[\d\s:.\-TZ]*$")


def _clean_text(text):
    return re.sub(r"\n{3,}", "\n\n", text.strip())


def _is_inside_fence(text, pos):
    return text[:pos].count("```") % 2 == 1


def _parse_header(role_raw, sidechain_dash, paren_content, sidechain_suffix):
    role = "user" if role_raw == "User" else "assistant"
    sidechain = bool(sidechain_dash) or bool(sidechain_suffix)
    # "(sidechain)" sometimes appears as the ONLY parenthesis (not in the dedicated
    # suffix group) - it must be treated as a flag, not as a model name
    if paren_content.strip().lower() == "sidechain":
        sidechain = True
        paren_content = ""
    m = _TS_RE.search(paren_content)
    timestamp = m.group(0) if m else None
    model = (paren_content[:m.start()] + paren_content[m.end():] if m else paren_content).strip()
    if model and _ONLY_PUNCTUATION_RE.match(model):
        model = None  # timestamp in a format not yet covered by _TS_RE
    model = model or None
    return role, model, sidechain, timestamp


def parse_specstory_with_model(chat_text):
    """Like 3_parse_cli.py::parse_specstory_messages, but preserves model/
    sidechain/timestamp for each turn instead of discarding them."""
    turns = []
    matches = list(_HEADER_RE.finditer(chat_text))
    for i, m in enumerate(matches):
        if _is_inside_fence(chat_text, m.start()):
            continue
        start_body = m.end()
        end_body = matches[i + 1].start() if i + 1 < len(matches) else len(chat_text)
        body = chat_text[start_body:end_body]
        content = _clean_text(re.sub(r"\n\n---\n\n", "\n", body))
        if not content:
            continue
        role, model, sidechain, timestamp = _parse_header(*m.groups())
        turns.append({
            "role": role, "text": content,
            "model": model, "sidechain": sidechain, "timestamp": timestamp,
        })
    return turns


# Headers of the IDE sessions (Cursor / Copilot, SpecStory): same pattern 1_07_agentic_scraper/2_parse.py uses to
# split the messages, so that the i-th header here belongs to the i-th parsed message.
_IDE_HEADER_RE = re.compile(
    r"_\*\*((?:User|Assistant|Agent)[^\n]*?)\*\*_\n+(.*?)(?=(?:_\*\*(?:User|Assistant|Agent)[^\n]*?\*\*_\n+)|\Z)",
    re.DOTALL,
)
_IDE_HEAD_PARTS_RE = re.compile(r"^(?:User|Assistant|Agent)(\s*-\s*sidechain)?\s*(?:\(([^)]*)\))?(\s*\(sidechain\))?\s*$")
_MODEL_MODE_RE = re.compile(r"^model\s+(.+?)\s*(?:,\s*mode\b.*)?$")


def ide_message_headers(chat_text):
    """Header text ("Agent (gpt-5.4 2026-04-27 06:21:38Z)", "Assistant (gpt-5)", ...) of each message of an IDE
    session, in the order and with the same splitting used to build the parsed messages."""
    return [m.group(1).strip() for m in _IDE_HEADER_RE.finditer(chat_text)]


def parse_header_text(header):
    """(model, sidechain, timestamp) from a header text. Forms: "User", "User (2025-10-20 08:02Z)",
    "Agent (gpt-5.4 2026-04-27 06:21:38Z)", "Agent (copilot/claude-sonnet-4.5)", "Agent (model gpt-5, mode Agent)",
    "Agent - sidechain (claude-haiku-4-5 ...)". "default" (the IDE's automatic choice) is kept as the model name `default`."""
    m = _IDE_HEAD_PARTS_RE.match(header.strip())
    if not m:
        return None, False, None
    sidechain = bool(m.group(1)) or bool(m.group(3))
    inside = (m.group(2) or "").strip()
    if inside.lower() == "sidechain":
        return None, True, None
    ts = re.search(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?Z", inside)
    timestamp = ts.group(0) if ts else None
    model = (inside[:ts.start()] + inside[ts.end():] if ts else inside).strip()
    mm = _MODEL_MODE_RE.match(model)
    if mm:
        model = mm.group(1).strip()
    if model and model.lower() == "default":
        model = "default"
    elif not model or _ONLY_PUNCTUATION_RE.match(model):
        model = None
    return model, sidechain, timestamp


def detect_format(chat_text):
    if "WORK SESSION" in chat_text:
        return "work_session"
    if "Claude Code Session Log" in chat_text:
        return "claude_session"
    if "Generated by SpecStory" in chat_text or "_**User" in chat_text:
        return "specstory"
    return "unknown"


# ---------------------------------------------------------------------
# Tool-output cleanup ONLY for the adoption verification process - NOT
# used to build out_vibe_coding/conversations.jsonl (the dataset
# stays intact for the other analyses: intent classification, review
# by language, etc. - there the raw text, including tool output,
# is legitimate information). Here instead we need to isolate ONLY the code
# written by the agent, because otherwise the ``` fence extractor also picks up
# the literal output of commands like `git status` (seen in a
# Bash block, ```text format) - it is not code, and matching that
# text against the repo history is not evidence of adoption, only an
# artifact (file names in common).
#
# Rule: Write blocks carry content REALLY written by the agent
# (kept, unrolled as a normal fence); Bash/Read/Grep/Glob/
# BashOutput/etc. blocks are command output or exploration, never code
# written by the agent (discarded entirely); the text outside the tool-uses
# (the agent's prose, which may contain illustrative ``` blocks)
# stays as is.
_TOOL_USE_RE = re.compile(
    r'<tool-use data-tool-type="[^"]*" data-tool-name="([^"]*)">(.*?)</tool-use>',
    re.S,
)
# ALSO captures the declared language tag (previously it was not saved:
# the Write content was "unrolled" with a bare fence, losing the original
# tag - a ```md became a fence without a tag again, ended up detecting
# the language with the prose heuristic instead of the declared 'markdown',
# and sometimes matched another language by mistake)
_FENCE_IN_BLOCK_RE = re.compile(r"```([a-zA-Z0-9_+#.\-]*)\n(.*?)```", re.S)


def _extract_blocks_write(text):
    """(declared, content) for each Write block in the text - content
    REALLY written by the agent, with its own language tag
    preserved (not re-nested in another fence: if the content
    itself contains nested fences, e.g. a doc with code examples,
    re-nesting it would confuse a later scan)."""
    results = []
    for m in _TOOL_USE_RE.finditer(text):
        if (m.group(1) or "").strip().lower() != "write":
            continue
        fence = _FENCE_IN_BLOCK_RE.search(m.group(2))
        if fence:
            results.append((fence.group(1), fence.group(2)))
    return results


def _remove_tool_use(text):
    """Removes EVERY tool-use block (Write included - already extracted separately
    by _extract_blocks_write) - what remains is only the agent's prose,
    free of command output (git status, Read/Grep results, etc.) and with no
    risk of residual nested fences."""
    return _TOOL_USE_RE.sub("", text)


_NOT_CODE = {None, "markdown", "text"}  # docs/prose - excluded: the user wants to
                                            # review code, not README/comments
_e04 = None

_TRANSCRIPT_SPECSTORY_RE = re.compile(r"(^|/)\.specstory(\.\w+)?/", re.I)


def is_chat_transcript(path):
    """True if the path is INSIDE the .specstory/ folder (any
    subfolder) committed in the repo. SpecStory writes the
    conversation there as it proceeds, generated code included - so
    a match of the code extracted FROM a session against such a file
    is circular (the file IS the transcript or a bookkeeping variant of it,
    it trivially matches itself), it proves nothing
    about adoption in the project's real code. The name of the
    subfolder VARIES by version/project (history/, chat_history/,
    others/, cli/, debug/, references/, even ".specstory.wat/" - all
    observed empirically): the whole .specstory/ folder is excluded,
    not just "history/", otherwise the variants not explicitly listed
    remain circular false positives (concrete bug: 69 residual matches on
    .specstory/chat_history/*.md before this fix)."""
    return bool(_TRANSCRIPT_SPECSTORY_RE.search(path or ""))


def _detect_language_reliable():
    """Lazy import of 1_04_extract_and_filter (module name with a numeric
    prefix) - same language heuristic already validated in the rest of the
    pipeline, but ONLY the first two levels (declared tag -> heuristic
    regex): the third level (pygments fallback on code without a tag)
    is unreliable on short/ambiguous fragments - verified empirically
    here: it produces exotic and almost certainly wrong lexers (scdoc,
    gdscript, carbon, cbmbas, teratermmacro...) on content that is neither
    declared nor recognized by the regex. Better to discard a genuine
    candidate without a tag than to keep an invented label."""
    global _e04
    if _e04 is None:
        import importlib
        import os
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        _e04 = importlib.import_module("1_04_extract_and_filter")

    def detect(declared, code):
        lang = _e04._norm_lang(declared)
        if lang:
            return lang
        if not code or len(code.strip()) < 12:
            return None
        return _e04._heuristic_lang(code)  # None if not recognized - no pygments

    return detect


def extract_snippet_clean(chat_text):
    """For a session in 'specstory' format: rebuilds (prompt, code) pairs
    ONLY from code genuinely written by the agent
    (Write blocks + fences in the prose cleaned of tool-uses), excluding:
    - the output of Bash/Read/Grep/etc. tools (_remove_tool_use)
    - candidates detected as markdown/prose/unrecognized (the Write
      tool is also used to create READMEs/documentation/design docs, not
      just code - the user wants to review real code, not comments).
    Used only by the adoption verification scripts."""
    detect_language = _detect_language_reliable()
    turns = parse_specstory_with_model(chat_text)
    pairs = []
    ultimo_prompt = None
    for t in turns:
        if t["role"] == "user":
            ultimo_prompt = t["text"]
            continue
        if not ultimo_prompt:
            continue

        candidates = list(_extract_blocks_write(t["text"]))
        prose = _remove_tool_use(t["text"])
        candidates += re.findall(r"```([a-zA-Z0-9_+#.\-]*)\n(.*?)```", prose, re.S)

        for declared, body in candidates:
            body = body.strip()
            if not body:
                continue
            lang = detect_language(declared or None, body)
            if lang in _NOT_CODE:
                continue
            pairs.append({"prompt": ultimo_prompt, "response_code": body, "language": lang})
    return pairs


# ---------------------------------------------------------------------
# Reads (Read) - used ONLY for adoption verification, not for the
# dataset. The Edit tool is 4x more frequent than Write (4,474 vs 1,087 in our
# sample) but does not keep its own content/diff (only "file
# updated successfully" - see lib_cli_agent_model_parser docstring), so
# extract_snippet_clean() alone covers little of the real editing
# activity. After an Edit the agent often re-reads the file to check
# the result: that Read shows the UPDATED and REAL state of the file -
# it is not "written" by the agent in the strict sense, but it is exactly the
# state the agent left in the session, the right signal for
# "did it end up in the repo?". Only the LAST read for each path is kept
# (the closest, chronologically, to the final state of the session).
_READ_BLOCK_RE = re.compile(
    r'<tool-use data-tool-type="read" data-tool-name="Read">(.*?)</tool-use>',
    re.S,
)
_READ_PATH_RE = re.compile(r"`([^`\n]+)`")
_READ_FENCE_RE = re.compile(r"```text\n(.*?)```", re.S)
_LINE_NUM_RE = re.compile(r"^\s*\d+[→\t]", re.M)  # two observed formats:
                                                    # "   140→..." and "1\t..." (without padding)


def extract_reads_clean(chat_text):
    detect_language = _detect_language_reliable()
    ultimo_per_path = {}
    for m in _READ_BLOCK_RE.finditer(chat_text):
        body = m.group(1)
        path_m = _READ_PATH_RE.search(body)
        fence_m = _READ_FENCE_RE.search(body)
        if not path_m or not fence_m:
            continue
        path = path_m.group(1).strip()
        content = _LINE_NUM_RE.sub("", fence_m.group(1)).strip()
        if content:
            ultimo_per_path[path] = content  # a path read several times: the last one wins (document order)

    results = []
    for path, content in ultimo_per_path.items():
        nome_file = path.rsplit("/", 1)[-1]
        ext = nome_file.rsplit(".", 1)[-1].lower() if "." in nome_file else None
        lang = detect_language(ext, content)
        if lang in _NOT_CODE:
            continue
        results.append({"path": path, "response_code": content, "language": lang})
    return results
