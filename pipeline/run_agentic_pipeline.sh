#!/bin/bash
# Orchestrator of the construction pipeline of the AGENTIC dataset
# (IDE + CLI-agent, "vibe-coding") - equivalent of run_chat_pipeline.sh for the
# prompt/chat dataset, but for the other half of the unified dataset.
#
# Usage:
#   ./pipeline/run_agentic_pipeline.sh                                  # full build, default window
#   ./pipeline/run_agentic_pipeline.sh --since 2025-06-01 --until 2026-09-28
#   ./pipeline/run_agentic_pipeline.sh --workers 24                     # more concurrent workers on scraping
#   ./pipeline/run_agentic_pipeline.sh --adoption                       # additionally, runs the adoption verification
#   ./pipeline/run_agentic_pipeline.sh --harness                        # additionally, extracts the harness mechanisms per repo
#
# Chain (equivalent to steps 1-6 of the chat pipeline):
#   1. 1_scrape_pool.py   (pipeline/1_07_agentic_scraper) - GitHub discovery+fetch, incremental/resumable on its own
#   2. 2_parse.py         (pipeline/1_07_agentic_scraper) - raw markdown -> parsed IDE + CLI-agent split
#   3. 3_parse_cli.py     (pipeline/1_07_agentic_scraper) - CLI-agent markdown -> parsed
#   4. 1_08_agentic_build_conversations.py - converts the parser output into our conversations.jsonl schema
#   5. 1_04_extract_and_filter.py --out-dir out_vibe_coding - reuses the SAME extraction/filter of the chat pipeline
#
# --adoption (optional, NOT in the default): additionally runs the 7
# adoption verifications (direct/fallback/authorship for CLI-agent, direct/commit_message/
# fallback for IDE, whole_repo as a last resort - fixed order, each one may
# depend on the output of the previous one) + the repo characteristics
# analysis. It makes live GitHub API calls (same token pool as the chat
# pipeline, pipeline/tokens.txt) and can take hours on the whole dataset - for
# this reason it is behind an explicit flag, like --track-snippets in the other pipeline.
#
# --harness (optional, NOT in the default, independent of --adoption - it depends
# only on conversations.jsonl, not on snippet.csv/adoption): detects the
# harness configuration mechanisms (Context Files, Skills,
# Subagents, Commands, Rules, Hooks, MCP, Settings) present in the repos of the
# sessions, according to the heuristics of Galster et al. 2026 - 3 phases in
# fixed order:
#   a. 3_01_agentic_extract_harness.py     -> out_vibe_coding/harness_config.csv
#      (one detection per repo, via GitHub Trees API)
#   b. 3_02_agentic_download_harness.py    -> out_vibe_coding/harness_files/, and
#      adds the "harness" column (which tool generated it) to
#      conversations.jsonl
#   c. 3_03_agentic_fix_harness_temporal.py  -> corrects "harness"/"harness_mechanisms"
#      by file creation date (a session must not be tagged with a
#      harness added to the repo AFTER that session)
# Phases b and c rewrite conversations.jsonl - both with the same safety
# pattern as the rest of the pipeline (temporary file, row count check,
# timestamped backup, atomic replacement - NEVER a direct
# overwrite). It makes live GitHub API calls, can take hours
# on the whole dataset.
#
# Unlike the discovery of the chat pipeline (atomic snapshot, all or
# nothing), 1_scrape_pool.py is ALREADY incremental internally (each phase
# checks what already exists on disk) - so here it is always called,
# without the need for a separate --refetch flag: rerunning the script is
# safe and cheap if there is nothing new to download.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
SCRAPER_DIR="$ROOT/pipeline/1_07_agentic_scraper"
cd "$ROOT"

# ------------------------------------------------------------------
# Python dependencies: those of the chat pipeline (reused by
# 1_04_extract_and_filter.py: pygments for the language heuristic,
# langdetect/lingua for the English filter) + those specific to the
# scraper (requests/pandas/tqdm, from pipeline/1_07_agentic_scraper/requirements.txt).
# ------------------------------------------------------------------
check_and_install_deps() {
  echo "Checking Python dependencies..."
  local pip_names=(ijson pygments beautifulsoup4 lxml requests langdetect lingua-language-detector pandas tqdm)
  local import_names=(ijson pygments bs4 lxml requests langdetect lingua pandas tqdm)
  local missing=()

  for i in "${!pip_names[@]}"; do
    if ! python -c "import ${import_names[$i]}" 2>/dev/null; then
      missing+=("${pip_names[$i]}")
    fi
  done

  if [[ ${#missing[@]} -gt 0 ]]; then
    echo "Missing: ${missing[*]} - installing with pip..."
    python -m pip install --quiet "${missing[@]}"
    echo "Installation completed."
  else
    echo "All Python packages already present."
  fi

  if [[ ! -f "$ROOT/pipeline/tokens.txt" && -z "$GITHUB_TOKENS" && -z "$GITHUB_PAT" ]]; then
    echo
    echo "WARNING: no GitHub token found (neither pipeline/tokens.txt nor"
    echo "  GITHUB_TOKENS/GITHUB_PAT in the environment). 1_scrape_pool.py will stop immediately."
    echo
  fi
}

check_and_install_deps

SINCE_ARG=""
UNTIL_ARG=""
WORKERS_ARG=""
ADOPTION=0
HARNESS=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --since)
      SINCE_ARG="$2"
      shift 2
      ;;
    --until)
      UNTIL_ARG="$2"
      shift 2
      ;;
    --workers)
      WORKERS_ARG="$2"
      shift 2
      ;;
    --adoption)
      ADOPTION=1
      shift
      ;;
    --harness)
      HARNESS=1
      shift
      ;;
    *)
      echo "Unrecognized argument: $1"
      exit 1
      ;;
  esac
done

SINCE="${SINCE_ARG:-2024-09-01}"
UNTIL="${UNTIL_ARG:-$(date +%Y-%m-%d)}"

echo "=================================================================="
echo "STEP 1: GitHub discovery + fetch (1_scrape_pool.py), window $SINCE..$UNTIL"
echo "=================================================================="
WORKERS_FLAG=()
if [[ -n "$WORKERS_ARG" ]]; then
  WORKERS_FLAG=(--workers "$WORKERS_ARG")
fi
( cd "$SCRAPER_DIR" && python 1_scrape_pool.py --start-date "$SINCE" --end-date "$UNTIL" "${WORKERS_FLAG[@]}" )

echo
echo "=================================================================="
echo "STEP 2: Parsing IDE sessions + CLI-agent split (2_parse.py)"
echo "=================================================================="
( cd "$SCRAPER_DIR" && python 2_parse.py )

echo
echo "=================================================================="
echo "STEP 3: Parsing CLI-agent sessions (3_parse_cli.py)"
echo "=================================================================="
( cd "$SCRAPER_DIR" && python 3_parse_cli.py )

echo
echo "=================================================================="
echo "STEP 4: Conversion into the unified schema (1_08_agentic_build_conversations.py)"
echo "=================================================================="
python pipeline/1_08_agentic_build_conversations.py

echo
echo "=================================================================="
echo "STEP 5: Snippet extraction + quality filters (reuses 1_04_extract_and_filter.py)"
echo "=================================================================="
python pipeline/1_04_extract_and_filter.py --out-dir "$SCRIPT_DIR/out_vibe_coding"

if [[ $ADOPTION -eq 1 ]]; then
  echo
  echo "=================================================================="
  echo "STEP 6 (optional): Adoption verification (7 methods, fixed order)"
  echo "=================================================================="
  echo "Warning: live GitHub API calls, can take hours."
  python pipeline/2_07_agentic_adoption_cli_agent_direct.py
  python pipeline/2_08_agentic_adoption_cli_agent_fallback.py
  python pipeline/2_09_agentic_adoption_cli_agent_authorship.py
  python pipeline/2_10_agentic_adoption_ide_direct.py
  python pipeline/2_11_agentic_adoption_ide_commit_message.py
  python pipeline/2_12_agentic_adoption_ide_fallback.py
  python pipeline/2_13_agentic_adoption_whole_repo.py

  echo
  echo "=================================================================="
  echo "STEP 7 (optional): Repo characteristics + correlation with adoption"
  echo "=================================================================="
  python pipeline/2_14_agentic_repo_characteristics.py
fi

if [[ $HARNESS -eq 1 ]]; then
  echo
  echo "=================================================================="
  echo "STEP (optional): Harness extraction - phase a: per-repo detection"
  echo "=================================================================="
  echo "Warning: live GitHub API calls, can take hours."
  python pipeline/3_01_agentic_extract_harness.py

  echo
  echo "=================================================================="
  echo "STEP (optional): Harness extraction - phase b: download + tool attribution"
  echo "=================================================================="
  python pipeline/3_02_agentic_download_harness.py

  echo
  echo "=================================================================="
  echo "STEP (optional): Harness extraction - phase c: temporal correction"
  echo "=================================================================="
  python pipeline/3_03_agentic_fix_harness_temporal.py
fi

echo
echo "=================================================================="
echo "Agentic dataset ready in pipeline/out_vibe_coding/"
echo "=================================================================="
