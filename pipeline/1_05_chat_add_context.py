#!/usr/bin/env python
"""
Step 5 - Adding prompt context.

Standalone: no imports from other scripts of the project. Usage:
    python pipeline/1_05_chat_add_context.py

Reads pipeline/out/snippet.csv (step 4) and pipeline/out/conversations.jsonl
(step 3, same source - the turns are ALWAYS available here by construction,
unlike the previous pipeline where a join with a separate external file was
needed and turn coverage was ~86-95%).

For each row: finds the index of the assistant turn that matches
"response" (exact match), then checks whether all the previous turns
are free of code:
  - prompt_with_context="yes" if previous history exists (index>0, excluding
    the possible placeholder "Original custom instructions no longer
    available" injected by ChatGPT as a fake first turn, see
    real_start()) AND no previous turn contains code
  - "no" if it is the first real turn, or if there is already code before
  - "not_available" only if the exact match response<->turn fails
    (rare, since the turns come from the same source)

full_prompt: when "yes", previous turns (labelled by role) +
original prompt at the end. Otherwise identical to "prompt".

Besides the context, this step also classifies the prompt itself (not the
previous turns) on two additional properties, with the same
regex heuristic already used for the context:
  code_in_prompt -> "yes"/"no": the prompt already has a ``` fence or a
    bare-code pattern (reuses has_code())
  has_example -> "yes"/"no": the prompt contains an example of expected
    behavior ("for example", "e.g.", "input:...output:...", "expected
    output", "test case")

Updates snippet.csv IN PLACE (adds the four columns: context x2 +
prompt classification x2).
"""
import argparse
import csv
import json
import os
import re

csv.field_size_limit(50_000_000)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT_DIR = os.path.join(SCRIPT_DIR, "out")

CODE_FENCE = re.compile(r"```")
CUSTOM_INSTRUCTIONS_PLACEHOLDER = "Original custom instructions no longer available"

_HEUR_CODE = re.compile(
    r"^\s*(?:def |class |from \S+ import|@\w)|\bself\b|print\(|"
    r"\bfn \w+|let mut |\bpackage \w+|\bfunc \w+\(|"
    r"#include\s*<[^>]+>|int\s+main\s*\(|using System|"
    r"^#!/.*sh\b|\b(?:echo|apt-get|sudo|chmod)\b|"
    # Extension (validated on real false negatives - see 109_prompting_techniques.py):
    # import/export JS-TS-JSX, Java-style functions, PHP, const/let/var assignments
    r"\bimport\s+[\w{}, ]+\s+from\s+['\"]|\brequire\(['\"]\S+['\"]\)|"
    r"\bexport\s+(?:default\s+)?(?:function|const|class|interface)\b|"
    r"\bmodule\.exports\b|=>\s*\{|\)\s*=>\s*\S|"
    r"<[A-Z]\w*(?:\s+\w+=[{\"][^>]*)?>|"
    r"\bpublic\s+(?:static\s+)?(?:void|class|final)\b|"
    r"<\?php\b|\bfunction\s+\w+\s*\([^)]*\)\s*\{|"
    r"\b(?:const|let|var)\s+\w+\s*[:=]\s*\S|"
    # lowercase HTML/XML tags (<div id="...">, <string name="...">, <meta .../>)
    r"<[a-zA-Z][\w\-]*(?:\s[^<>]{0,200})?/?>|"
    # pasted terminal sessions (bash/PowerShell prompts, CLI commands with flags)
    r"\b\w[\w.\-]*@[\w.\-]+:[~/][^\s$#]*[$#]\s|"
    r"\bPS [A-Z]:\\[^>]*>\s|"
    r"\b(?:npm|pip3?|python3?|node|git|docker|kubectl|cargo|yarn|composer|flask|go|"
    r"cc|gcc|g\+\+|clang|make|ld|javac|tsc|cmake|ninja|mvn|gradle|rustc|dotnet)"
    r"\s+(?:[\w.\-]+\s+)?--?[\w\-]+\b|"
    # program output with syntax indistinguishable from the code that
    # builds it (e.g. "tensor(...)" is both how a tensor is printed and how
    # it is built: torch.tensor(...)) - treated as code,
    # not as a separate category (see discussion with the user)
    r"\btensor\s*\(|\barray\s*\(\s*\[|\bDataFrame\b|\bdtype\s*=|\bshape\s*=\s*\(|"
    r"^\s*\{\s*[\"']\w+[\"']\s*:",
    re.M
)


def has_code(text):
    if not text:
        return False
    return bool(CODE_FENCE.search(text) or _HEUR_CODE.search(text))


EXAMPLE_RE = re.compile(
    r"\bfor example\b|\be\.g\.\b|\bexample:|\binput:.*output:|"
    r"\bexpected output\b|\btest case\b",
    re.I | re.S,
)


def has_example(text):
    if not text:
        return False
    return bool(EXAMPLE_RE.search(text))


def real_start(turns):
    if turns and turns[0]["role"] == "user" and \
            turns[0]["text"].strip() == CUSTOM_INSTRUCTIONS_PLACEHOLDER:
        return 1
    return 0


def find_prompt_turn_index(turns, prompt, response):
    """Returns the index of the USER turn that is the prompt of this exchange
    (not the index of the assistant response!). The response is searched, then
    we go back to the immediately preceding user turn and check that its
    text matches the prompt - otherwise turns[:j] would wrongly include the
    prompt's own turn in the "previous context" (bug
    found and fixed during the testing of this script)."""
    for i, t in enumerate(turns):
        if t["role"] == "assistant" and t["text"] == response:
            j = i - 1
            while j >= 0 and turns[j]["role"] != "user":
                j -= 1
            if j >= 0 and turns[j]["text"] == prompt:
                return j
            return None
    return None


def format_context(turns, upto_idx):
    lines = ["[Previous conversation context]"]
    for t in turns[:upto_idx]:
        label = "User" if t["role"] == "user" else "Assistant"
        lines.append(f"{label}: {t['text']}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                    help="folder with snippet.csv and conversations.jsonl")
    args = ap.parse_args()
    snippet_path = os.path.join(args.out_dir, "snippet.csv")
    conv_path = os.path.join(args.out_dir, "conversations.jsonl")

    print("Loading turns per conversation...")
    turns_by_link = {}
    with open(conv_path, encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            turns_by_link[o["conversation_url"]] = o["turns"]
    print(f"  {len(turns_by_link)} conversations")

    with open(snippet_path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    print(f"Rows to process: {len(rows)}")

    n_yes = n_no = n_unknown = 0
    for row in rows:
        row["code_in_prompt"] = "yes" if has_code(row["prompt"]) else "no"
        row["has_example"] = "yes" if has_example(row["prompt"]) else "no"

        turns = turns_by_link.get(row["conversation_url"])
        if not turns:
            row["prompt_with_context"] = "not_available"
            row["full_prompt"] = row["prompt"]
            n_unknown += 1
            continue

        j = find_prompt_turn_index(turns, row["prompt"], row["response"])
        if j is None:
            row["prompt_with_context"] = "not_available"
            row["full_prompt"] = row["prompt"]
            n_unknown += 1
            continue

        start = real_start(turns)
        if j <= start:
            row["prompt_with_context"] = "no"
            row["full_prompt"] = row["prompt"]
            n_no += 1
            continue

        context_has_code = any(has_code(t["text"]) for t in turns[start:j])
        if context_has_code:
            row["prompt_with_context"] = "no"
            row["full_prompt"] = row["prompt"]
            n_no += 1
        else:
            row["prompt_with_context"] = "yes"
            row["full_prompt"] = (format_context(turns[start:], j - start)
                                        + "\n\n[Prompt]\n" + row["prompt"])
            n_yes += 1

    with open(snippet_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    n_code = sum(1 for r in rows if r["code_in_prompt"] == "yes")
    n_example = sum(1 for r in rows if r["has_example"] == "yes")

    print(f"\nUpdated {snippet_path}")
    print(f"prompt_with_context: yes={n_yes} ({n_yes/len(rows):.1%})  "
          f"no={n_no} ({n_no/len(rows):.1%})  "
          f"not_available={n_unknown} ({n_unknown/len(rows):.1%})")
    print(f"code_in_prompt: yes={n_code} ({n_code/len(rows):.1%})")
    print(f"has_example: yes={n_example} ({n_example/len(rows):.1%})")


if __name__ == "__main__":
    main()
