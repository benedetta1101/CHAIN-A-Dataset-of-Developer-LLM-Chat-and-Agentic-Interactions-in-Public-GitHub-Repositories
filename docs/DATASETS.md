# Datasets — file and column reference

*4 languages* = Python, JavaScript, TypeScript, Java. **File names, column names
and categorical values are all in English.** The pipeline writes them natively
(`pipeline/out_*`), and its last step, `pipeline/7_02_publish_datasets.py` (run by
`run_all.sh`), assembles this folder.

## `datasets/chat/`

| File | Rows | What it is |
|---|---|---|
| `conversations.jsonl` | 24,047 | One conversation per line: `conversation_url`, `provider` (chatgpt/claude/gemini), `model_slug`, `n_user_messages`, `n_llm_messages`, `n_total_messages` (iterations of the conversation: user messages, model responses, all turns), `n_tokens_user`, `n_tokens_llm`, `n_tokens_total`, `token_count_method`, `artifacts` (GitHub artifacts referencing the link: `type`, `reference`, `path`, `url`, `created_at`, `repo`), `turns` (`role`, `text`; each turn also has `n_tokens`, `message_index` (i-th user message / i-th model response) and, for model responses, `after_user_message` (the user message it answers)). Roles are `user`, `assistant`, `unknown`; `unknown` turns count only in `n_total_messages`. |
| `snippets_all_languages.csv` | 56,600 | One code snippet per row, all languages. `row_id` (unique key), `conversation_url`, `provider`, `repo`, `artifacts`, `model`, `language`, `prompt`, `response` (full response), `response_code` (extracted code), `response_in_step_build` / `n_steps` / `step_snippets` (step-by-step builds), `alternative_responses` (independent alternatives, code joined with a blank line). |
| `annotated_4_languages.csv` | 27,962 | **The annotated dataset.** Snippets of the 4 languages with adoption and intent (below). Full response text: `snippets_all_languages.csv`, same `row_id`. |
| `html/browser_chat_annotated_<language>.html` | — | Browsable version of the annotated dataset, one file per language; see *HTML browsers* below. |

**`annotated_4_languages.csv` columns**

- `row_id`, `conversation_url`, `provider`, `repo`, `model`, `language`, `prompt`, `response_code`.
- `adopted`: `yes` = the code was found as an **exact copy** in the repository (as is); `no` = not found, or found only by similarity (modified snippets do not count); `not_verifiable` = the row cannot be traced to a repository (e.g. link found only on Hacker News).
- For adopted rows: `found_via` (`known_verbatim`, `file_history`, `whole_repo`, `intermediate_pr_commit`), `found_path`, `found_commit`, `found_date`, `days_elapsed`.
- `message_id`, `status` (`success` or a failure reason), `main_category`, `sub_category`: intent of the **prompt** (8 macro categories, 21 micro categories).

## `datasets/agentic/`

| File | Rows | What it is |
|---|---|---|
| `conversations.jsonl` | 14,900 | One session per line: `sha`, `conversation_url`, `is_cli_agent` (`yes` = CLI agent, otherwise IDE), `provider`, `title`, `timestamp`, `models_used`, `has_sidechain`, `n_user_messages`, `n_llm_messages`, `n_total_messages` (iterations of the conversation: user messages, model responses, all turns), `n_tokens_user`, `n_tokens_llm`, `n_tokens_total`, `token_count_method`, `artifacts`, `turns` (`role`, `text`, `model`, `sidechain`, `timestamp`; each turn also has `n_tokens`, `message_index` (i-th user message / i-th model response) and, for model responses, `after_user_message` (the user message it answers); roles are `user`, `assistant`, `agent`, the last two count as model responses; `model`, `sidechain` and `timestamp` are read from the header of each message in the session log, so they are empty when the log does not give them. A header that says `default` (the IDE's automatic choice) is kept as `model` = `default`), `harness` / `harness_mechanisms` (see below). |
| `annotated_4_languages_english_only.csv` | 25,291 | **The annotated dataset** (English prompts only, 2,980 sessions). One row per prompt→code candidate (written by the agent) in the 4 languages; 25,239 rows have an intent label. Derived from the 41,649 annotated candidates of the 4 languages by keeping `prompt_is_english = yes` (see *English filter* below). |
| `pairs_all_languages_english_only.csv` | 106,936 | Prompt→code candidates (written by the agent) in **all languages**, English prompts only (6,351 sessions): 28,369 in the 4 target languages and 78,567 in others (mostly bash, yaml, html, json, and `diff` = edits whose file type is unknown). Same columns as the annotated file, plus `candidate_index_4lang`. Intent labels only for the 4 target languages; `status` says why a row has no label (`not_classified_language_outside_target`, or `not_classified_session_not_processed` for 3,078 candidates of two CLI log formats not yet classified). |
| `html/browser_agentic_annotated_<language>.html` | — | Browsable version of the annotated dataset, one file per language; see *HTML browsers* below. |
| `html/other_languages/` | — | Browsers for the pairs in languages **outside** the 4 target languages (no intent labels; sessions with English user messages only): one file per language (large ones such as `diff` and `bash` split in parts; rare languages grouped in `other`) and `browser_agentic_other_languages_index.html` as the entry page. The 4 target languages are in `browser_agentic_annotated_*`. |
| `html/browser_harness.html` | — | Repos × harness mechanisms, linked to sessions. |
| `harness/harness_config.csv`, `harness/harness_files/` | 1,464 repos checked, 474 with at least one mechanism | Harness mechanisms detected per repo (state of the default branch at check time) and the downloaded files (460 repos). Columns: `repo`, `n_sessions`, `found`, `default_branch`, `n_files`, `truncated`, `n_mechanisms`, one column per mechanism (`context_files`, `skills`, `subagents`, `commands`, `rules`, `hooks`, `mcp`) listing the files, `settings_ambiguous`. |

**`annotated_4_languages_english_only.csv` columns**

- Key: `sha` (session) + `candidate_index` (index within the session, over the 4-language candidates). `conversation_url`, `repo`, `source_group` (`ide` / `cli_agent`).
- `candidate_type`: `diff` (added lines of an edit), `prose` (code block in the agent's prose), `cli_agent` (written file or block, CLI agents). `language`, `prompt`, `code`.
- `nontrivial_lines`, `prompt_is_english` (`yes`/`no`), `prompt_in_snippet_csv`: for filtering. Example: English prompt and ≥5 non-trivial lines reproduces the chat-style criteria (13,070 rows).
- `message_id`, `status`, `main_category`, `sub_category`, `all_labels`: intent of the **prompt** (25,239 rows labelled). `main_category` / `sub_category` are the classification; `all_labels` is the JSON list of every label of the prompt (`main_category`, `sub_category` only - the model's reasoning is not published).
- `session_adopted`: `yes` / `no` / `unknown` — adoption of the **session**, not of the pair.
- `harness_present`, `harness_tools` (Cursor, Claude Code, Copilot, Gemini, AGENTS.md (generic)), `harness_mechanisms` (context_files, rules, mcp, commands, subagents, skills, hooks): harness present in the repo **at the time of the session**.

`pairs_all_languages_english_only.csv` has the same columns except `prompt_in_snippet_csv`, plus `candidate_index_4lang`; there `candidate_index` counts the candidates of **all** languages in the session.

**English filter.** One rule (`pipeline/lib_language_filter.py`) decides whether a prompt is English: it is **not
English** if it contains any Chinese, Japanese or Korean character (or at least 3 letters of Cyrillic, Arabic, Hebrew,
Thai, Devanagari or Bengali) anywhere in its text - code, tracebacks and URLs do not exempt a prompt that also
contains such text - or if its prose (code blocks and URLs removed) has at least 20 characters and lingua-py
(restricted to 29 common languages) detects another language. Shorter prose is not judged and is kept. The agentic
data are multilingual: 39.3% of the 41,649 annotated candidates (and 42.1% of the 184,780 candidates of all languages)
have a non-English prompt. **This package ships only the English-prompt rows** (`prompt_is_english` is `yes` in every
row of the two agentic CSVs); `7_02_publish_datasets.py` also writes the complete files (`annotated_4_languages.csv`,
`pairs_all_languages.csv`), which are not part of this package. The chat dataset applies the same rule at construction, so its snippet files
only contain English prompts. `conversations.jsonl` is not filtered (a conversation can contain messages in several
languages). Only the prompt is checked: code, comments and model responses can contain other languages.

## HTML browsers

The browsers in `chat/html/`, `agentic/html/` and `agentic/html/other_languages/` are self-contained pages
(open them in a recent Chrome/Edge/Firefox/Safari; the data is gzip-compressed inside the page and
decompressed by the browser).

- **Full conversations.** Every turn of each conversation/session is shown in order - user messages and model
  responses, with or without code - and the response text is shown in full, with code blocks rendered as
  code. The extracted code pairs (adoption, intent label, candidate type) appear under the exchange they
  belong to. For the intent label of a prompt see the badge on its user message.
- **Counts.** Per conversation: number of user messages, model responses, turns, and tokens (user, model,
  total). Per turn: its position ("model response 3 / 8, after user message 3 / 8") and its token count.
  Tokens are counted with `tiktoken` `o200k_base`, an approximation for models that use another tokenizer.
  The same counts (with the same definitions) are stored in `conversations.jsonl`, see the fields above.
- **Truncation.** To keep the pages light, each turn is cut at 4,000 characters and very long conversations
  show their first 50 turns, last 30 turns and every exchange that carries pairs; the cut parts are marked.
  The complete text is in `conversations.jsonl`.
- **Language.** Only conversations whose user messages are English are included: at least 80% of the user
  messages with at least 20 characters of prose (code blocks and URLs removed) must be detected as English
  with lingua-py (restricted to 29 common languages), and **no** user message may contain a Chinese,
  Japanese, Korean (or other non-Latin-script) text, wherever it appears in the message. Excluded: 702 of
  6,130 chat conversations and 1,978 of 4,516 agentic sessions in the 4 target languages. The CSV files are filtered per prompt, not by
  this conversation-level rule; `--all-languages` in the generators disables it.

## Categorical values

Values are in English: `yes` / `no` / `unknown` / `not_verifiable` / `n/a` / `error`;
`found_via`: `known_verbatim`, `file_history`, `whole_repo`, `intermediate_pr_commit`,
`not_found` (`similarity` matches are not counted as adopted); `candidate_type`: `diff`, `prose`,
`cli_agent`; `status`: `success`, `no_result`, `not_classified_language_outside_target`,
`not_classified_session_not_processed`, plus the model's failure reasons
(`failure: empty content`, `invalid_category`).

## Intent taxonomy

8 macro categories (Code Authoring, Failure Reporting, Inquiry, Context Specification, Validation, Delegation, Workflow Control, Others) and 21 micro categories, from Tang et al. Labels come from the **prompt** (plus the previous turn as context); the model never saw the code.
