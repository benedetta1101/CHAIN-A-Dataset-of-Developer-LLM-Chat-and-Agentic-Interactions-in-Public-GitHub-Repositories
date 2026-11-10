# Pipeline

## How the numbering works

Every script is named `<phase>_<step>_<side>_<what>.py`, so listing this folder shows the order in which the
scripts run:

```
2_07_agentic_adoption_cli_agent_direct.py
│ │  │       └─ what it does
│ │  └───────── side: chat | agentic (no side = shared by both)
│ └──────────── step: order inside the phase (01, 02, ...)
└────────────── phase: 1-7
```

| Phase | What happens | Needs |
|---|---|---|
| **1** construction | find the conversations / sessions, fetch them, extract the prompt→code pairs, apply the quality and English filters | GitHub tokens (discovery) |
| **2** adoption | check whether the code ended up in the real repository | GitHub tokens (many API calls) |
| **3** harness | detect the AI-coding configuration files of each repository | GitHub tokens |
| **4** intent classification | prepare the batches and label each prompt with `gpt-5-mini`; cross the labels with adoption | OpenAI key for the paid step |
| **5** annotated exports | assemble the annotated CSV files from everything above | outputs of phases 1-4 |
| **6** HTML browsers | build the browsable HTML pages | output of phase 5 |
| **7** merge and publish | merge both sides, assemble `datasets/` | everything above |

Rules:

- Run the scripts of a side in numeric order: phase first, then step. Steps of the same phase on the same side
  depend on the previous ones; the `chat` and `agentic` sides are independent (they only share
  `1_04_extract_and_filter`).
- Inside the subfolders (`1_07_agentic_scraper/`, `4_01_chat_intent_classification/`,
  `4_02_agentic_intent_classification/`) the scripts are numbered in the order they run (`1_`, `2_`, ...);
  `1b_` is a variant of step `1_`.
- `lib_*.py` are modules imported by the steps and never run directly.
- Output files keep plain names (`snippet.csv`, `adoption_ide_direct.csv`, ...): only the scripts carry the
  numbers.
- You rarely run the scripts one by one: `run_all.sh` (in the folder above) calls the orchestrators
  `run_chat_pipeline.sh`, `run_agentic_pipeline.sh`, `run_chat_classification.sh` and
  `run_agentic_classification.sh` in this folder, which run the steps in the right order.

## Script map

The folder is flat on purpose: scripts import each other from the same directory, and the paths are used by
`run_all.sh` and the Dockerfile. The subfolders are `1_07_agentic_scraper/` (discovery/fetch/parse of the
agentic sessions) and the two `4_0x_*_intent_classification/` folders.

| Phase | Chat | Agentic |
|---|---|---|
| **1. Construction** | `1_01_chat_discover_links` → `1_02_chat_fetch_conversations` (uses `lib_fetch_claude_gemini_impl`) → `1_03_chat_normalize` → `1_04_extract_and_filter` (shared) → `1_05_chat_add_context` → `1_06_chat_split_by_language` → `1_09_chat_gemini_model_labels` (optional: model label of Gemini shared pages) | `1_07_agentic_scraper/{1_scrape_pool, 2_parse, 3_parse_cli}` → `1_08_agentic_build_conversations` → `1_04_extract_and_filter` (`--out-dir out_vibe_coding`) |
| **2. Adoption** (flag `--track-snippets` / `--adoption`) | `2_01_chat_track_snippets` → `2_02_chat_similarity_recheck` → `2_03_chat_pr_commits_recheck` → `2_04_chat_codefile_commits_recheck` → `2_05_chat_reverify_wholerepo` → `2_06_chat_timeline` | `2_07`…`2_13_agentic_adoption_{cli_agent_direct, cli_agent_fallback, cli_agent_authorship, ide_direct, ide_commit_message, ide_fallback, whole_repo}`, `2_14_agentic_repo_characteristics` |
| **3. Harness** (flag `--harness`) | — | `3_01_agentic_extract_harness` → `3_02_agentic_download_harness` → `3_03_agentic_fix_harness_temporal` |
| **4. Intent classification** (flag `--classify`, prepare only) | `4_01_chat_intent_classification/{1_prepare_context, 2_classify}`, `4_03_chat_intent_adoption` | `4_02_agentic_intent_classification/{1_prepare_context, 1b_prepare_context_new_formats, 2_classify}`, `4_04_agentic_intent_adoption` |
| **5. Annotated exports** | `5_01_chat_export_annotated_dataset` | `5_02_agentic_export_candidates_dataset` → `5_03_agentic_export_annotated_dataset`; `5_04_agentic_export_pairs_all_languages` (all languages; labels for the 4 target ones); `5_05_agentic_export_classification` |
| **6. HTML browsers** (shared: `lib_html_viewer_common`, `lib_language_filter` - the English filter also used by step 1_04 and the exporters) | `6_01_chat_html_annotated` (HTML goes to `pipeline/browser_dataset_en/`) | `6_02_agentic_html_annotated`, `6_03_agentic_html_all_languages`, `6_04_agentic_html_harness` |
| **7. Merge and publish** (end of `run_all.sh`, `--no-publish` to skip) | `7_01_merge_datasets` (both sides in one table) · `7_02_publish_datasets` → `release/datasets/` | copies the pipeline outputs (already English-named) into `datasets/`; runs the `5_*` exporters if their inputs exist |

English-prompt-only variants of the agentic CSV files: `7_02_publish_datasets` writes the `*_english_only.csv`
files. The HTML generators keep only conversations with English user messages (`--all-languages` disables this).

Shared modules (imported, not run directly): `lib_github_cache`, `lib_cli_agent_model_parser`,
`lib_ide_session_parser`, `lib_extract_git_commits_from_sessions`, `lib_fetch_claude_gemini_impl`,
`lib_html_viewer_common`, `lib_language_filter`; inside the classification folders `lib_categories`.

Order matters for the annotated exports: adoption (phase 2), harness (phase 3) and
classification (phase 4, which needs an OpenAI key and a paid submission outside
`run_all.sh`) must exist before phase 5.

The methodology (what each step measures, known biases) is in [`../docs/METHODOLOGY.md`](../docs/METHODOLOGY.md).
