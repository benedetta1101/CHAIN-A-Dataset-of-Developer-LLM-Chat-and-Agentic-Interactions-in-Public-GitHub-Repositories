#!/usr/bin/env python
"""
Step 4 - Snippet extraction + step/alternative collapse + quality filters.

Standalone: no imports from other scripts of the project (the language
detection, quality filter and step/alternative collapse functions are
reimplemented here, same logic already validated in the rest of the project
but without cross dependencies). Usage:
    python pipeline/1_04_extract_and_filter.py

Reads pipeline/out/conversations.jsonl (step 3, all providers).

Internal pipeline, in order:
  1. Extraction of code blocks per assistant turn (``` fence), paired
     with the prompt (last user turn) and the full response (text of the
     assistant turn). Language: tag declared in the fence (normalized) ->
     regex heuristic -> pygments (fallbacks in this order, same strategy
     as the original pipeline).
  2. Quality filter PER BLOCK (before the collapse, so a trivial block does
     not "save" a group that would otherwise be discarded): non-empty prompt,
     English prompt (see below), block with >= MIN_ROWS_SNIPPET
     non-trivial lines (a line with at least one alphanumeric character after strip).

  Language filter (lib_language_filter.py): the "English only" filter applied at collection time
  (is_english() in 1_01_chat_discover_links.py / lib_fetch_claude_gemini_impl.py) detects
  the language on the WHOLE CONVERSATION, not line by line - verified
  empirically to let ~17-18% of non-English prompts through.
  Here the language is judged on the single PROMPT: it is rejected if it contains Chinese,
  Japanese or Korean text (or another non-Latin script) anywhere, or if lingua-py (restricted
  to 29 common languages, applied to the prose with code blocks and URLs removed) detects
  another language. Prose shorter than MIN_LEN_LANGUAGE characters is not judged and is kept
  by default, as the original filter did.
  3. Deduplication of identical blocks (same text) in the same conversation.
  4. Collapse per response: blocks sharing (link, prompt, response) are
     grouped. If more than one, the one that covers (textual superset,
     whitespace-insensitive) the most other blocks of the group is chosen
     (step-build); if none covers others, they are independent "alternatives"
     and all are kept (response_code = plain text, the
     blocks joined with \n\n; the full, distinct list stays in
     step_snippets, always a JSON list).

Output: pipeline/out/snippet.csv
"""
import csv
import argparse
import json
import os
import re

csv.field_size_limit(50_000_000)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT_DIR = os.path.join(SCRIPT_DIR, "out")

MIN_ROWS_SNIPPET = 5

import lib_language_filter

MIN_LEN_LANGUAGE = lib_language_filter.MIN_LEN_LANGUAGE


def is_english_prompt(prompt):
    """True if the prompt is English or too short / undetermined to judge (kept by default);
    False if it contains a non-Latin script (e.g. Japanese) or another language was detected.
    See lib_language_filter.py."""
    return lib_language_filter.is_english_text((prompt or "").strip())


CODE_FENCE = re.compile(r"```([a-zA-Z0-9_+#.\-]*)\n(.*?)```", re.S)

_LANG_ALIAS = {
    "py": "python", "js": "javascript", "ts": "typescript", "jsx": "javascript",
    "tsx": "typescript", "sh": "bash", "shell": "bash", "zsh": "bash",
    "c++": "cpp", "cxx": "cpp", "c#": "csharp", "cs": "csharp",
    "yml": "yaml", "html5": "html", "dockerfile": "dockerfile",
    "golang": "go", "rb": "ruby", "kt": "kotlin", "rs": "rust", "md": "markdown",
}

_HEUR = [
    ("php", re.compile(r"<\?php")),
    ("html", re.compile(r"<(?:!doctype|html|head|body|div|span|p|a|script)\b", re.I)),
    ("sql", re.compile(r"\bselect\b[\s\S]+?\bfrom\b|\binsert\s+into\b|\bcreate\s+table\b", re.I)),
    ("python", re.compile(r"(?m)^\s*(?:def |class |from \S+ import|@\w)|\bself\b|print\(|\belif\b|__name__")),
    ("rust", re.compile(r"\bfn \w+|let mut |println!|::<")),
    ("go", re.compile(r"\bpackage \w+|\bfunc \w+\(|fmt\.\w+")),
    ("typescript", re.compile(r"\binterface \w+|\: (?:string|number|boolean)\b|\bimport .* from ")),
    ("javascript", re.compile(r"\b(?:function|const|let|var)\b.*|=>|console\.log|document\.")),
    ("java", re.compile(r"\b(?:public|private|protected)\s+(?:static\s+)?(?:class|void|int|String)\b|System\.out\.print")),
    ("cpp", re.compile(r"#include\s*<[^>]+>.*(?:std::|cout|cin)|using namespace std", re.S)),
    ("c", re.compile(r"#include\s*<[^>]+>|int\s+main\s*\(")),
    ("csharp", re.compile(r"using System|Console\.Write|namespace \w+")),
    ("ruby", re.compile(r"(?m)^\s*(?:def |end$|require )|puts |\.each do")),
    ("bash", re.compile(r"(?m)^#!/.*sh\b|\b(?:echo|apt-get|sudo|chmod|export)\b")),
    ("yaml", re.compile(r"(?m)^\s*[\w.-]+:\s+\S")),
]

_pygments_guess = None


def _norm_lang(tag):
    if not tag:
        return None
    t = tag.strip().lower()
    return _LANG_ALIAS.get(t, t) or None


def _heuristic_lang(code):
    s = code.strip()
    if s[:1] in "{[":
        try:
            json.loads(s)
            return "json"
        except Exception:
            pass
    for name, rx in _HEUR:
        if rx.search(code):
            return name
    return None


def detect_language(declared, code):
    lang = _norm_lang(declared)
    if lang:
        return lang
    if not code or len(code.strip()) < 12:
        return None
    lang = _heuristic_lang(code)
    if lang:
        return lang
    global _pygments_guess
    if _pygments_guess is None:
        try:
            from pygments.lexers import guess_lexer
            _pygments_guess = guess_lexer
        except Exception:
            _pygments_guess = False
    if not _pygments_guess:
        return None
    try:
        lexer = _pygments_guess(code)
        name = (lexer.aliases[0] if lexer.aliases else lexer.name).lower()
        return _LANG_ALIAS.get(name, name)
    except Exception:
        return None


def nontrivial_lines(code):
    return sum(1 for l in (code or "").splitlines() if re.search(r"[A-Za-z0-9_]", l))


def extract_blocks(conv):
    turns = conv["turns"]
    blocks = []
    last_user = None
    for t in turns:
        if t["role"] == "user":
            last_user = t["text"]
        elif t["role"] in ("assistant", "agent"):
            # "agent": turns of IDE sessions in Agent mode (Cursor/Copilot),
            # 29% of the agentic sessions - previously ignored (no row in snippet.csv)
            for declared, body in CODE_FENCE.findall(t["text"]):
                blocks.append({
                    "prompt": last_user, "response": t["text"], "code": body,
                    "language": detect_language(declared, body),
                })
    return blocks


def pick_final(snippets):
    """Returns (chosen_index, max_coverage). coverage=0 -> no
    snippet is a superset of another (group of independent alternatives)."""
    if len(snippets) == 1:
        return 0, 0
    norm = lambda s: re.sub(r"\s+", " ", s.strip())
    normed = [norm(s) for s in snippets]
    best_idx, best_key = 0, None
    for i, cand in enumerate(normed):
        coverage = sum(1 for j, other in enumerate(normed)
                        if j != i and other and other in cand)
        key = (coverage, len(cand), i)
        if best_key is None or key > best_key:
            best_key, best_idx = key, i
    return best_idx, best_key[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                    help="folder with conversations.jsonl and where to write snippet.csv")
    args = ap.parse_args()
    in_path = os.path.join(args.out_dir, "conversations.jsonl")
    out_path = os.path.join(args.out_dir, "snippet.csv")

    n_conv = n_blocks_raw = 0
    reasons = {"excluded: empty prompt": 0, "excluded: prompt not in English": 0,
               "excluded: snippet < 5 non-trivial lines": 0,
               "inclusa": 0, "duplicate_removed": 0}

    rows_out = []
    with open(in_path, encoding="utf-8") as f:
        for line in f:
            conv = json.loads(line)
            n_conv += 1
            blocks = extract_blocks(conv)
            n_blocks_raw += len(blocks)

            filtered = []
            seen = set()
            for b in blocks:
                prompt = (b["prompt"] or "").strip()
                if not prompt:
                    reasons["excluded: empty prompt"] += 1
                    continue
                if not is_english_prompt(prompt):
                    reasons["excluded: prompt not in English"] += 1
                    continue
                if nontrivial_lines(b["code"]) < MIN_ROWS_SNIPPET:
                    reasons["excluded: snippet < 5 non-trivial lines"] += 1
                    continue
                key = (b["response"], b["code"])
                if key in seen:
                    reasons["duplicate_removed"] += 1
                    continue
                seen.add(key)
                reasons["inclusa"] += 1
                filtered.append(b)

            groups = {}
            for b in filtered:
                groups.setdefault((b["prompt"], b["response"]), []).append(b)

            repo_distinct = sorted({a["repo"] for a in conv["artifacts"] if a.get("repo")})

            for (prompt, response), items in groups.items():
                snippets = [it["code"] for it in items]
                idx, coverage = pick_final(snippets)
                is_alt = len(items) > 1 and coverage == 0
                # response_code is ALWAYS plain text, even for independent
                # alternatives (joined with \n\n, no longer a serialized JSON list
                # as in a previous version) - a downstream consumer that
                # searches this text in a real file of the repo finds it that way,
                # whereas before a JSON list (with escapes \n, \") NEVER matched
                # a real file, artificially depressing the adoption
                # measured for these rows (real bug: 4.0% vs 29.3% of the
                # normal rows - see docs/METHODOLOGY.md and code_joined() in
                # 2_01_chat_track_snippets.py, from which this fix was brought here
                # to the source). step_snippets below still keeps the full
                # and distinct list for whoever needs the single blocks.
                response_code = "\n\n".join(snippets) if is_alt else snippets[idx]
                rows_out.append({
                    "conversation_url": conv["conversation_url"],
                    "provider": conv["provider"],
                    "repo": json.dumps(repo_distinct, ensure_ascii=False),
                    "artifacts": json.dumps(conv["artifacts"], ensure_ascii=False),
                    "model": conv.get("model_slug"),
                    "language": items[idx]["language"],
                    "prompt": prompt,
                    "response": response,
                    "response_code": response_code,
                    # "yes" only for the true step-build (one block covers others,
                    # response_code is a valid block) - it must be kept MUTUALLY
                    # EXCLUSIVE with alternative_responses, not just "len(items)>1"
                    # (with that condition, 97.3% of the "step" rows were
                    # actually alternatives, found by analyzing why the adoption
                    # of these rows seemed to collapse suspiciously - see
                    # docs/METHODOLOGY.md)
                    "response_in_step_build": "yes" if (len(items) > 1 and not is_alt) else "no",
                    "n_steps": len(items),
                    "step_snippets": json.dumps(snippets, ensure_ascii=False),
                    "alternative_responses": "yes" if is_alt else "no",
                })

    os.makedirs(args.out_dir, exist_ok=True)
    if rows_out:
        # row_id: unique row id (conversation_url alone is NOT -
        # a conversation with several steps/alternatives produces several rows with
        # the same conversation_url but different response_code; downstream scripts
        # that did lookups by conversation_url risked
        # taking the wrong response_code - real bug found and
        # fixed in steps 8-12, see docs/METHODOLOGY.md)
        for i, row in enumerate(rows_out):
            row["row_id"] = i
        fieldnames = ["row_id"] + [k for k in rows_out[0].keys() if k != "row_id"]
        with open(out_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows_out)

    print(f"Conversations processed: {n_conv}")
    print(f"Raw code blocks extracted: {n_blocks_raw}")
    print("\nExclusion/inclusion reasons (at block level):")
    for k, v in reasons.items():
        print(f"  {k}: {v}")
    print(f"\nFinal rows (after step/alternative collapse): {len(rows_out)}")
    print(f"Written {out_path}")

    from collections import Counter
    print("\nper provider:", Counter(r["provider"] for r in rows_out))
    print("\ntop 10 languages:", Counter(r["language"] for r in rows_out).most_common(10))
    print("\nresponse_in_step_build:", Counter(r["response_in_step_build"] for r in rows_out))
    print("alternative_responses:", Counter(r["alternative_responses"] for r in rows_out))


if __name__ == "__main__":
    main()
