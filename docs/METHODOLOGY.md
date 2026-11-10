# Methodology

How the two datasets were built, what each measurement means, and where it can
mislead. For files and columns see [`DATASETS.md`](DATASETS.md); for the scripts
behind each step see [`../pipeline/README.md`](../pipeline/README.md).

Everything in the pipeline (script names, intermediate and published file names, column
names, categorical values, identifiers) is in English.

## 1. Overview

| | Chat dataset | Agentic dataset |
|---|---|---|
| Source | ChatGPT / Claude / Gemini conversations whose public share link is cited on GitHub (issues, PRs, code files) | Session logs of IDE integrations (Cursor, Copilot, …) and CLI coding agents (Claude Code, …) committed to public GitHub repos |
| Unit | one code snippet (a fenced block of an assistant response) with the prompt that produced it | one candidate: code written by the agent (diff added lines, written file, code in prose) with the user prompt that preceded it |
| Adoption | per snippet: found **as an exact copy** in the repo | per **session**: code of the session reached the repo |
| Intent | label of the prompt (Tang et al. taxonomy, gpt-5-mini) | same |
| Extra | — | AI-coding harness present in the repo (context files, rules, MCP, skills, …) |

Target languages for intent labels and annotated datasets: Python, JavaScript,
TypeScript, Java.

## 2. Chat dataset construction (steps 1–6)

1. **Discovery** (`1_01_chat_discover_links.py`). Searches GitHub issues, pull requests, code
   files for share links of the three providers. Issues and PRs are
   bisected on `created:` and code search on `size:` when a query exceeds the
   1,000-result ceiling of the GitHub search API. Code search ignores date windows and
   can take hours; `--sources` restricts the sources. Uses a token pool
   (`pipeline/tokens.txt`, or `GITHUB_TOKENS` / `GITHUB_PAT`): without tokens discovery
   is capped at 60 requests/hour and code search is disabled.
2. **Fetch** (`1_02_chat_fetch_conversations.py`, `lib_fetch_claude_gemini_impl.py`). Downloads
   every turn of each discovered conversation: Playwright for ChatGPT and Gemini,
   direct HTTP for Claude. Resumable; failed links are retried on rerun. Steps 1–2 hit
   live services and are **not reproducible over time** (links disappear), so
   `run_chat_pipeline.sh` skips them when their output exists, unless `--refetch`.
3. **Normalisation** (`1_03_chat_normalize.py`). One record per conversation, one schema for
   all providers: counters, model, the GitHub artifacts that
   cite the link (all kept, not only the first), full turns.
4. **Snippet extraction and filters** (`1_04_extract_and_filter.py`). Extracts fenced code
   blocks from assistant turns and pairs each with its prompt. Language: declared fence
   tag → regex heuristic → pygments. Quality filters per block: non-empty prompt;
   **English prompt** (`lib_language_filter.py`: no Chinese/Japanese/Korean or other non-Latin-script
   text anywhere in the prompt, and lingua-py on its prose; prose shorter than 20
   characters is kept); at least **5 non-trivial lines**. Identical blocks are
   deduplicated; blocks sharing (conversation, prompt, response) are collapsed: if one
   block covers the others it is a *step-build* (`response_in_step = yes`), otherwise they
   are independent *alternatives* (`alternative_responses`).
5. **Context** (`1_05_chat_add_context.py`). For prompts that depend on earlier code-free turns,
   adds the previous context (`prompt_with_context`); flags code or an example already
   present in the prompt.
6. **Split by language** (`1_06_chat_split_by_language.py`).



## 3. Chat adoption (steps 7–12, `--track-snippets`)

*Adopted* = the snippet's code is found **verbatim** (after whitespace normalisation) in the
real repository. The evidence depends on the artifact that cites the link, strongest first:

| Artifact | Known | What is checked |
|---|---|---|
| code file | repo, path, commit | snippet is in that file at that commit; commit reachable from the default branch; file/snippet still there today |
| pull request | repo, PR number | snippet is in a file changed by the PR; PR merged; still there today |
| issue only | repo | only a whole-repo code search |
| Claude / Gemini link | repo only (no path, commit or PR number) | not traceable (`not_verifiable`) |

If the snippet is not at the known place, GitHub code search looks in the whole repo before
concluding it disappeared. Later steps add evidence and make the first pass stricter:

- **Step 8, similarity.** `difflib` containment ratio (share of the snippet found in common
  blocks with today's file). ≥ 0.55 is a probable *modified* adoption, 0.3–0.55 weak.
  **The published `adopted = yes` counts exact copies only**; modified snippets are `no`.
- **Step 9, PR commits.** Checks every intermediate PR commit, added lines only (code
  pasted and cleaned before merge).
- **Step 10, file history.** Content of the file at each of its last commits, with a
  causality check on the content (the chat link must already be in the file).
- **Step 11, re-verification.** A manual audit of about 65 matches found that the
  whole-repo search had a false-positive rate near 40%, almost always generic boilerplate
  (single imports, macros, bare `catch`). Generic lines are now excluded and the minimum
  search length raised to 45 characters; existing matches were re-verified without
  repeating searches.
- **Step 12, timeline.** One row per traceable snippet: `found_via`, `found_path`,
  `found_commit`, `link_date`, `found_date`, `days_elapsed`. For matches "in today's state"
  the date is that of the last commit touching the file (a lower bound). Negative
  `days_elapsed` mostly means an imprecise reference date, not time running backwards.

Reading the numbers: adoption is a **conservative lower bound** (heavily rewritten code,
removed repos and issue-only rows are missed); false positives were deliberately
traded for false negatives.

## 4. Agentic dataset construction

1. **Scraping** (`pipeline/1_07_agentic_scraper/1_scrape_pool.py`, phases 1–10): finds
   committed session logs (SpecStory-style markdown and CLI-agent logs), repository
   metadata, contributors, languages, README, file trees, commits. Incremental: each
   phase checks what is already on disk.
2. **Parsing** (`2_parse.py`, `3_parse_cli.py`): IDE sessions and CLI-agent sessions. For the
   SpecStory CLI format `lib_cli_agent_model_parser.py` also recovers, per turn, the exact
   model (including non-Anthropic models and `<synthetic>`), the sidechain flag and the
   timestamp, which the original parser discarded.
3. **Conversations** (`1_08_agentic_build_conversations.py`): our `conversations`
   schema. File listing is sorted: an unstable order once silently shifted all
   `message_id`s and misaligned classifications.
4. **Candidates** (`1_prepare_context.py`, `lib_ide_session_parser.py`,
   `lib_cli_agent_model_parser.py`). A candidate is code **written by the agent**, never tool
   output: added lines of a diff (IDE `search_replace` / `apply_patch` / edit-file blocks),
   content of a `Write`, `Edit` new strings, and fenced code in the agent's prose.
   Output of Bash/Read/Grep/Glob is discarded (a chat-style fence extraction would pick up
   `git status` output as "code"). Candidates in markdown/prose/unrecognised languages are
   dropped; the prompt is the preceding real user turn. Candidates are indexed by
   `sha::index` (index among the 4-language candidates of the session).
   IDE sessions in "Agent" mode (turn role `agent`) are handled.

The published agentic dataset is built on these candidates, not on the chat-style
`snippet.csv`, because the chat-style extraction is unreliable on agentic sessions (see
README,). Columns allow filtering after the fact (English prompt,
non-trivial lines, candidate type). English-prompt-only variants (`prompt_is_english = yes`) of the agentic CSV files are produced by
`7_02_publish_datasets.py`; the chat dataset is English-filtered by prompt at construction with the same rule. The HTML browsers apply a
stricter, conversation-level rule (see *HTML browsers* in `DATASETS.md`).

## 5. Agentic adoption (`--adoption`)

Seven methods, all **per session**, merged into a yes/no/unknown:

| Method | Signal |
|---|---|
| CLI direct | `git commit -m …` commands in the transcript matched to the repo's commit history (same message or subject), then the session code is checked against the full content of the files touched at that commit |
| CLI fallback | paths of files the agent edited/wrote, converted to repo-relative, checked in today's file |
| CLI authorship | a commit co-authored by Claude (`Co-Authored-By`) shortly after the session |
| IDE direct | commit authored or co-authored by a recognisable AI bot (Copilot coding agent) shortly after the session |
| IDE commit message | `git commit` commands visible in IDE transcripts (about 10% of IDE sessions), matched like CLI direct |
| IDE fallback | paths touched by IDE tools, checked at the commit embedded in the log URL, then HEAD |
| whole repo | last resort: GitHub code search for the session's code |

Rules that matter: the code is checked against the **full file content at the commit**, not
only the diff (the diff misses lines written earlier in the session); matches inside the
repo's own `.specstory/` folder are excluded (circular: the file *is* the transcript);
verification code comes from a clean extraction, not from tool output.

## 6. Harness (`--harness`)

Detects, per repo, which AI-coding configuration mechanisms exist (context files, skills,
subagents, commands, rules, hooks, MCP, settings) using the file/directory heuristics of
Table 1 of Galster et al. 2026:

1. `3_01_agentic_extract_harness.py` — one Git Trees API call per repo (default branch);
2. `3_02_agentic_download_harness.py` — downloads the files and attributes each to a tool
   (Claude Code, Cursor, Copilot, Gemini, generic `AGENTS.md`);
3. `3_03_agentic_fix_harness_temporal.py` — keeps a mechanism for a session only if the file's
   first commit is not later than the session.

Detection is by **path**, not content: hooks and MCP that share a settings file are marked
ambiguous. Files are the state of the default branch at check time.

## 7. Intent classification (`--classify`)

Each user prompt is labelled with the taxonomy of Tang et al. (8 macro and 21 micro
categories) by `gpt-5-mini` through the OpenAI Batch API, using the previous user and
assistant turns as context (head–tail truncation, 60/40 over 1,000 characters). The model
sees **the prompt and its context, never the code**. Messages with several labels use the
first (dominant) one as `main_category` / `sub_category`; all labels are kept in
`all_labels` (classification only, without the model's reasoning).

Batches are submitted **one at a time** (account limit of 5,000,000 enqueued tokens), with
resume logic that checks for an existing batch id before resubmitting. `run_all.sh
--classify` only **prepares** messages and batch files (free, local) and never submits;
submission is a deliberate, paid step (`run_chat_classification*.sh --submit`).
Chat messages map back to `row_id`; agentic messages map to `candidate_key` (`sha::index`).

## 8. Annotated datasets and publication

`export_*` scripts join the pieces into the annotated files; `generate_html_*` scripts
build the browsers; `7_02_publish_datasets.py` (last step of `run_all.sh`) writes everything to
`datasets/` (the pipeline already writes English names). Free text is never altered and
no existing file is overwritten.



## 9. Dependencies

Python 3.13 (the Docker image), packages in `pipeline/requirements.txt`
(`ijson`, `pygments`, `playwright` + Chromium, `beautifulsoup4`, `lxml`, `requests`,
`langdetect`, `lingua-language-detector`, `pandas`, `tqdm`). A GitHub token pool is
strongly recommended; an OpenAI key is needed only to submit classification batches.
The Docker image bundles everything except credentials.
