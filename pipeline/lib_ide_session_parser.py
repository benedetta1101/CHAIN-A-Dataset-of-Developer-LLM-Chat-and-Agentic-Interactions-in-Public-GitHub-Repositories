#!/usr/bin/env python
"""Clean extraction of code from the IDE sessions (Cursor/Copilot) of the
vibe-coding dataset - analogous to lib_cli_agent_model_parser.py for the
CLI-agent sessions, but for a much more fragmented format: verified
empirically (sample of 500 sessions) that there are AT LEAST TWO rendering
schemes of the tool-uses, not just one:

  NEW FORMAT (SpecStory "Markdown v2.1.0"+): explicit tag
    <tool-use data-tool-type="..." data-tool-name="...">...</tool-use>
  - read_file_v2: clean content in a JSON "contents" field - reliable
  - search_replace, apply_patch: real diff (```diff, +/- lines),
    HTML-escaped - reliable
  - copilot_readFile, edit_file_v2, textEditGroup, copilot_createFile,
    copilot_replaceString, replace_string_in_file, create_file, edit_file,
    read_file: no reliable content - only the path (fallback)
    (read_file is a TRAP discovered empirically: the "Result" field
    is sometimes the output of a terminal command cached in an internal
    VS Code scratch file, not the content of the file - discarded)

  OLD FORMAT (SpecStory without a version number): same kind of
  information but without <tool-use> tags, <details><summary>... blocks
    <summary>Edit file: {path}</summary> / <summary>Apply patch for
    {path}</summary>: SAME ```diff diff as the new counterparts -
    reliable, same extraction
    <summary>Read file: ...</summary> / Grep search / Listed .../
    Called ...: no reliable code content - path-only or
    discarded

  PROSE (both formats): ``` fence outside any
  <tool-use>/<details> block - the same principle as
  lib_cli_agent_model_parser.extract_snippet_clean (the agent sometimes proposes
  code in chat without going through an edit tool).

Together the two formats (tool-use + old) cover ~63% of a random
sample of 500 sessions; the rest are sessions truly without file
edits (Q&A only) or further formats not yet covered.

Use as a library:
    from lib_ide_session_parser import extract_reads_ide, extract_diff_ide, extract_path_touched_ide
"""
import html
import importlib
import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
camp = importlib.import_module("lib_cli_agent_model_parser")

_PATH_INTERNAL_RE = re.compile(
    r"workspaceStorage|chat-session-resources|\.vscode-server|"
    r"AppData[\\/]+Roaming[\\/]+Code|Library[\\/]+Application Support[\\/]+Code|"
    r"/tmp/|\\Temp\\|\.specstory(\.\w+)?[\\/]",
    re.I,
)


def is_internal_path(path):
    """Internal storage/cache path of VS Code/Cursor, or SpecStory export
    - not a repo file, must be discarded as a target or candidate
    (trap discovered empirically: a read_file on a path like
    this sometimes returns cached terminal output, not real
    code content; the SpecStory export is circular for the same
    reason already solved for CLI-agent - see camp.is_chat_transcript)."""
    return bool(_PATH_INTERNAL_RE.search(path or ""))


# ------------------------------------------------------------- new format
_TOOL_USE_RE = re.compile(
    r'<tool-use data-tool-type="[^"]*" data-tool-name="([^"]*)">(.*?)</tool-use>',
    re.S,
)
_TOOL_READING_CLEAN = {"read_file_v2"}
_TOOL_DIFF = {"search_replace", "apply_patch"}
_TOOL_PATH_ONLY = {
    "edit_file_v2", "edit_file", "textEditGroup", "copilot_readFile",
    "copilot_createFile", "copilot_replaceString", "replace_string_in_file",
    "create_file", "read_file",
}

_JSON_FENCE_RE = re.compile(r"```json\n(.*?)```", re.S)
_DIFF_FENCE_RE = re.compile(r"```diff\n(.*?)```", re.S)
_PATH_FIELDS = ("targetFile", "relativeWorkspacePath", "filePath", "file_path", "effectiveUri")

# ------------------------------------------------------------- old format
_DETAILS_RE = re.compile(r"<details>\s*<summary>(.*?)</summary>(.*?)</details>", re.S)
_OLDEST_EDIT_RE = re.compile(r"^Edit file:\s*(.+)$")
_OLDEST_APPLYPATCH_RE = re.compile(r"^Apply patch for\s*(.+)$")
_OLDEST_PATH_ONLY_RE = re.compile(r"^(Read file|Grep search|Listed|Called)\b")


def _first_json(text):
    return [m.group(1) for m in _JSON_FENCE_RE.finditer(text)]


def _extract_path_from_block(body):
    """Looks for a path in one of the known JSON fields (first json block, usually
    the 'Parameters'), or in a 'filePath: `...`' line outside
    JSON, or in a markdown link '[](file:///...)'."""
    for block in _first_json(body):
        try:
            data = json.loads(block)
        except Exception:
            continue
        if isinstance(data, dict):
            for campo in _PATH_FIELDS:
                if data.get(campo):
                    return str(data[campo])
    m = re.search(r"(?:filePath|targetFile|relativeWorkspacePath|file_path)[:`]*\s*`([^`\n]+)`", body)
    if m:
        return m.group(1)
    m = re.search(r"\[\]\(file://([^)\s]+)\)", body)
    if m:
        return urllib.parse.unquote(m.group(1))
    return None


def _language_from_path(detect_language, path, content):
    nome_file = (path or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    ext = nome_file.rsplit(".", 1)[-1].lower() if "." in nome_file else None
    return detect_language(ext, content)


def _diff_added(body_html_escaped):
    diff_m = _DIFF_FENCE_RE.search(body_html_escaped)
    if not diff_m:
        return None
    diff_text = html.unescape(diff_m.group(1))
    added = "\n".join(
        line[1:] for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ).strip()
    return added or None


def extract_reads_ide(chat_text):
    """Only read_file_v2 (content in JSON, reliable) - read_file
    (new and old format) and copilot_readFile DISCARDED (see module
    docstring)."""
    detect_language = camp._detect_language_reliable()
    ultimo_per_path = {}
    for m in _TOOL_USE_RE.finditer(chat_text):
        if (m.group(1) or "").strip() not in _TOOL_READING_CLEAN:
            continue
        blocks_json = _first_json(m.group(2))
        if not blocks_json:
            continue
        try:
            parameters = json.loads(blocks_json[0])
        except Exception:
            parameters = {}
        path = next((str(parameters[c]) for c in _PATH_FIELDS if parameters.get(c)), None)
        if not path or is_internal_path(path):
            continue
        try:
            result = json.loads(blocks_json[-1])
        except Exception:
            continue
        content = result.get("contents") if isinstance(result, dict) else None
        if content and content.strip():
            ultimo_per_path[path] = content  # last read wins (most recent state)

    out = []
    for path, content in ultimo_per_path.items():
        lang = _language_from_path(detect_language, path, content)
        if lang in camp._NOT_CODE:
            continue
        out.append({"path": path, "response_code": content, "language": lang})
    return out


def extract_diff_ide(chat_text):
    """search_replace/apply_patch (new format, <tool-use> tag) + Edit
    file/Apply patch for (old format, <details><summary>) - same
    ```diff diff, +/- lines, HTML-escaped. Only the ADDED lines
    are kept (like added_lines_only in the rest of the pipeline)."""
    detect_language = camp._detect_language_reliable()
    out = []

    for m in _TOOL_USE_RE.finditer(chat_text):
        if (m.group(1) or "").strip() not in _TOOL_DIFF:
            continue
        body = m.group(2)
        added = _diff_added(body)
        if not added:
            continue
        path = _extract_path_from_block(body)
        lang = _language_from_path(detect_language, path, added)
        if lang in camp._NOT_CODE:
            continue
        out.append({"path": path or "", "response_code": added, "language": lang})

    for m in _DETAILS_RE.finditer(chat_text):
        title, body = m.group(1).strip(), m.group(2)
        me = _OLDEST_EDIT_RE.match(title)
        mp = _OLDEST_APPLYPATCH_RE.match(title)
        path = (me or mp).group(1).strip() if (me or mp) else None
        if not path:
            continue
        added = _diff_added(body)
        if not added:
            continue
        lang = _language_from_path(detect_language, path, added)
        if lang in camp._NOT_CODE:
            continue
        out.append({"path": path, "response_code": added, "language": lang})

    return out


def _remove_blocks_known(chat_text):
    """Removes tool-use (new format) and details (old format, including
    <think><details>Thought Process</details></think>) - what remains
    is the agent's prose, where proposed code can appear without
    going through an edit tool (same principle as
    lib_cli_agent_model_parser._remove_tool_use)."""
    without_tooluse = _TOOL_USE_RE.sub("", chat_text)
    return _DETAILS_RE.sub("", without_tooluse)


_FENCE_PROSE_RE = re.compile(r"```([a-zA-Z0-9_+#.\-]*)\n(.*?)```", re.S)


def extract_prose_ide(chat_text):
    """``` fences in the agent's prose, outside any tool-use/details
    block - code proposed in chat without an explicit edit
    tool (e.g. Ask-mode sessions, or inline suggestions)."""
    detect_language = camp._detect_language_reliable()
    prose = _remove_blocks_known(chat_text)
    out = []
    for declared, body in _FENCE_PROSE_RE.findall(prose):
        body = body.strip()
        if not body:
            continue
        lang = detect_language(declared or None, body)
        if lang in camp._NOT_CODE:
            continue
        out.append({"path": "", "response_code": body, "language": lang})
    return out


# ------------------------------------------------------------- turns (per real prompt)
# IDE headers observed (sample of 200 sessions): bare "_**User**_" / "_**Assistant**_",
# or "_**Agent (model-name)**_" / "_**Agent (model copilot/gpt-5, mode
# Agent)**_" - MUCH more heterogeneous formats than the CLI-agent ones (no
# structured timestamp, "Assistant" in addition to "Agent", parenthesis absent or
# with free text) - camp._HEADER_RE (ALWAYS requires a parenthesis, only
# User|Agent) does not cover them. Here only the turn boundary and the role are needed,
# not the model name.
_IDE_HEADER_RE = re.compile(r"_\*\*(User|Assistant|Agent)\b[^\n]*?\*\*_\n+")


def parse_ide_turns(chat_text):
    """Turns (role/text) for IDE sessions - same principle as
    camp.parse_specstory_with_model, but with the IDE header (see
    _IDE_HEADER_RE). Used to trace back to the real user prompt that
    produced a diff/prose candidate (extract_diff_ide/extract_prose_ide
    work on the whole text and do not know WHICH user turn generated a
    given block - here the same is applied per turn)."""
    turns = []
    matches = list(_IDE_HEADER_RE.finditer(chat_text))
    for i, m in enumerate(matches):
        if camp._is_inside_fence(chat_text, m.start()):
            continue
        start_body = m.end()
        end_body = matches[i + 1].start() if i + 1 < len(matches) else len(chat_text)
        body = chat_text[start_body:end_body]
        content = camp._clean_text(re.sub(r"\n\n---\n\n", "\n", body))
        if not content:
            continue
        role = "user" if m.group(1) == "User" else "assistant"
        turns.append({"role": role, "text": content})
    return turns


def extract_path_touched_ide(chat_text):
    """Only the PATHS touched by edit/read tools (any schema,
    new or old format), for the fallback method: we know WHICH file,
    and verify the content against the available candidates (reads +
    diffs), not against the text of the tool itself."""
    path_seen = set()
    for m in _TOOL_USE_RE.finditer(chat_text):
        if (m.group(1) or "").strip() not in (_TOOL_READING_CLEAN | _TOOL_DIFF | _TOOL_PATH_ONLY):
            continue
        path = _extract_path_from_block(m.group(2))
        if path and not is_internal_path(path):
            path_seen.add(path)

    for m in _DETAILS_RE.finditer(chat_text):
        title = m.group(1).strip()
        me = _OLDEST_EDIT_RE.match(title)
        mp = _OLDEST_APPLYPATCH_RE.match(title)
        if me or mp:
            path = (me or mp).group(1).strip()
            if path and not is_internal_path(path):
                path_seen.add(path)
        elif _OLDEST_PATH_ONLY_RE.match(title):
            path = _extract_path_from_block(m.group(2))
            if path and not is_internal_path(path):
                path_seen.add(path)

    return path_seen
