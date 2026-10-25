# Prompt vs. Agentic LLM Coding — Datasets and Replication Package

Two paired datasets of real-world LLM-assisted coding, built with related methodologies so
that chat-based and agentic coding can be studied side by side, plus the pipeline that
produced them.

| | **Chat** | **Agentic** |
|---|---|---|
| Source | ChatGPT / Claude / Gemini conversations whose share link is cited on GitHub  | session logs of IDE integrations (Cursor, Copilot, …) and CLI coding agents (Claude Code, …) committed to GitHub |
| Unit | a code snippet and the prompt that produced it | a prompt → code candidate written by the agent (diff added lines, written file, code in prose) |
| Size | 24,047 conversations · 56,600 snippets (27,962 in the 4 target languages) | 14,900 sessions · 106,936 candidates with an English prompt (25,291 in the 4 target languages) |
| Intent label | macro/micro category of the prompt (gpt-5-mini, taxonomy of Tang et al.) | same |
| Adoption | per snippet: found **as an exact copy** in the real repository | per session: the session's code reached the repository |
| Extra | — | AI-coding **harness** of the repo (context files, rules, MCP, skills, hooks, …): 474 of 1,464 repos |

Target languages: Python, JavaScript, TypeScript, Java.

## Where things are

- **This repository (GitHub):** the code and the documentation - `pipeline/`, `docs/`, `Dockerfile`, `run_all.sh`.
- **The data (Zenodo):** `[ZENODO DOI - to be filled in]`. It holds the datasets, the HTML browsers, the harness
  files and a copy of this repository's contents. Its `assemble.sh` rebuilds the folder layout used in the
  documentation (`datasets/chat/…`, `datasets/agentic/…`); put that `datasets/` folder next to `pipeline/`.

```
.
├── datasets/         (from Zenodo, not in this repository)
│   ├── chat/         conversations, all snippets, annotated 4-language dataset, HTML browsers
│   └── agentic/      conversations, annotated 4-language dataset and pairs for all languages (English
│                     prompts only), HTML browsers, harness
├── docs/
│   ├── DATASETS.md             every file and column
│   ├── METHODOLOGY.md          how the data was built and what each measure means
│   └── ETHICS_AND_PRIVACY.md   provenance, licensing, takedown
├── pipeline/         all the scripts that produced the data (pipeline/README.md explains the numbering and the order),
│                     including 1_07_agentic_scraper/ (GitHub discovery/fetch of the agentic sessions)
├── Dockerfile, run_all.sh      reproducible environment and one-command entry point

```

## Quick start

**Browse without running anything** — after downloading the data from Zenodo, open any self-contained HTML file in a browser:
`datasets/chat/html/`, `datasets/agentic/html/` (every conversation is shown in full, with message and token
counts and the model of each response; only conversations whose user messages are English),
`datasets/agentic/html/browser_harness.html` (repos × harness mechanisms).

**Load the data**

```python
import pandas as pd

chat = pd.read_csv("datasets/chat/annotated_4_languages.csv")
adopted = chat[chat.adopted == "yes"]                      # exact copies found in the repo

agentic = pd.read_csv("datasets/agentic/annotated_4_languages_english_only.csv")
by_intent = agentic.groupby("main_category").session_adopted.value_counts()
```

The full conversations are in `datasets/{chat,agentic}/conversations.jsonl` (one JSON object per line).
They also hold the **iteration and size information**: per conversation `n_user_messages`,
`n_llm_messages`, `n_total_messages` and the token counts `n_tokens_user`, `n_tokens_llm`,
`n_tokens_total`; per turn `n_tokens`, `message_index` (the i-th user message or model response) and,
for model responses, `after_user_message`. Agentic turns also carry the `model`, `sidechain` and
`timestamp` read from the session log, when the log has them.

The CSV files carry only the intent **classification** of each prompt (`main_category`,
`sub_category`, and `all_labels` with the same two fields for every label); the model's reasoning
behind it is not published.

Only English prompts: a prompt with any Chinese, Japanese or Korean text (or another non-Latin
script) is treated as non-English (`pipeline/lib_language_filter.py`); see `docs/DATASETS.md`.

Columns, values and keys are documented in [`docs/DATASETS.md`](docs/DATASETS.md).

**Rebuild a dataset**

```bash
docker build -t vibe-coding-pipeline .
docker run --rm -it \
  -v "$(pwd)/pipeline/tokens.txt:/app/pipeline/tokens.txt:ro" \
  vibe-coding-pipeline ./run_all.sh --chat-since 2024-09-01 --chat-until 2026-08-10
```

`tokens.txt` holds one GitHub personal access token per line (public read scope is enough);
without it discovery is capped at 60 requests/hour and code search is disabled. Credentials
are never baked into the image. `run_all.sh` flags (combinable): *(none)* construction +
assembly of `release/datasets/` (`--no-publish` to skip); `--track-snippets` chat adoption
check; `--adoption` agentic adoption check; `--harness` harness detection; `--classify`
**prepares** the intent-classification batches (free, local) — it never submits anything to
OpenAI; `--skip-chat` / `--skip-agentic`. Real runs make live API calls and can take hours.

Rebuilding gives the same **methodology and comparable scale**, not byte-identical data:
GitHub and the chat providers change over time, and the original discovery run of the chat
dataset used a script version that was not saved.

