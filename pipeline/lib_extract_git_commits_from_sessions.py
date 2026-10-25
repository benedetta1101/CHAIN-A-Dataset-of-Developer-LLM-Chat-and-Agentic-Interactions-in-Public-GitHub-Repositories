#!/usr/bin/env python
"""Extracts, from the raw markdown of the CLI-agent sessions, the `git commit`
commands invoked by the agent in the Bash tool-uses - a direct signal
(first-hand, inside the transcript itself) that the session's work
was committed, without needing to search for the snippet text in the
repo history as the out_ago2026 pipeline does (designed for the copy-paste
model, not for an agent that writes directly into files).

Handles two observed styles:
  git commit -m "simple message"
  git commit -m "$(cat <<'EOF'
  message
  multi-line
  EOF
  )"
"""
import re

_HEREDOC_RE = re.compile(
    r"git commit\s+-m\s+\"\$\(cat\s*<<'?EOF'?\s*\n(.*?)\nEOF\s*\n?\)\"",
    re.S,
)
_SIMPLE_DQ_RE = re.compile(r'git commit\s+(?:[^\n]*?\s)?-m\s+"([^"]*)"')
_SIMPLE_SQ_RE = re.compile(r"git commit\s+(?:[^\n]*?\s)?-m\s+'([^']*)'")


def extract_messages_commit(text):
    """Returns the list of commit messages invoked in the text (raw,
    may appear several times per session - once for each commit made)."""
    found = []
    busy = []  # [start,end) intervals already consumed, to avoid recounting

    for m in _HEREDOC_RE.finditer(text):
        found.append((m.start(), m.group(1).strip()))
        busy.append((m.start(), m.end()))

    def overlapping(s, e):
        return any(s < oe and e > os for os, oe in busy)

    for rx in (_SIMPLE_DQ_RE, _SIMPLE_SQ_RE):
        for m in rx.finditer(text):
            if overlapping(m.start(), m.end()):
                continue
            msg = m.group(1).strip()
            if msg:
                found.append((m.start(), msg))
                busy.append((m.start(), m.end()))

    found.sort(key=lambda x: x[0])
    return [msg for _, msg in found]


if __name__ == "__main__":
    import os
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "1_07_agentic_scraper", "data", "markdowns_cli")
    n_files_with_commit = n_commit_tot = 0
    for fn in os.listdir(d)[:2000]:
        text = open(os.path.join(d, fn), encoding="utf-8").read()
        msgs = extract_messages_commit(text)
        if msgs:
            n_files_with_commit += 1
            n_commit_tot += len(msgs)
    print(f"Files with at least one commit extracted: {n_files_with_commit}")
    print(f"Total commit messages extracted: {n_commit_tot}")
