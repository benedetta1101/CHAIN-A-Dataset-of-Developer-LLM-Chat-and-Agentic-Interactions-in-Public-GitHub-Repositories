#!/bin/bash
# Orchestrator of the dataset construction pipeline (6 steps, 7 optional).
#
# Usage:
#   ./pipeline/run_chat_pipeline.sh                              # steps 1-6, all languages
#   ./pipeline/run_chat_pipeline.sh --languages python bash       # step 6 only on these languages
#   ./pipeline/run_chat_pipeline.sh --refetch                     # also reruns discovery+fetch (1-2)
#   ./pipeline/run_chat_pipeline.sh --refetch --discovery-timeout 300   # discovery cut at 300s
#   ./pipeline/run_chat_pipeline.sh --refetch --tag v2             # writes EVERYTHING under different names,
#                                                              # without touching the existing files
#   ./pipeline/run_chat_pipeline.sh --sources issues prs hn        # discovery without code search (quick test)
#   ./pipeline/run_chat_pipeline.sh --track-snippets               # additionally, runs "as-is" adoption (steps 7-12)
#   ./pipeline/run_chat_pipeline.sh --refetch --tag v2 --until 2026-10-12   # widens the discovery
#                                                              # window, separate outputs
#
# --since YYYY-MM-DD / --until YYYY-MM-DD: override the default discovery
# window (2023-05-01..2026-08-10, the same window used for
# out_ago2026). Useful to widen the
# search to a more recent period (e.g. beyond 2026-08-10 of out_ago2026)
# without touching the existing snapshot -
# requires --refetch (otherwise step 1 is skipped anyway if a
# file for that window already exists) and should almost always be used together with
# --tag (otherwise it rewrites dataset_<since>_<until>.json, not the
# "untagged" files which stay at the previous snapshot).
#
# --track-snippets: in addition to steps 1-6 (deterministic, minutes),
# runs the "as-is" adoption chain - steps 7-12 (2_01_chat_track_snippets,
# 2_02_chat_similarity_recheck, 2_03_chat_pr_commits_recheck, 2_04_chat_codefile_commits_recheck,
# 2_05_chat_reverify_wholerepo, 2_06_chat_timeline) - NOT the later hardening chain
# used for out_ago2026 (steps 16/18/46/47/54/55/92/96/97, incremental fixes
# found during that analysis, not yet ported here) - final result:
# out<_tag>/snippet_timeline.csv, the same "base" adoption already valid
# for a chat/agentic comparison. It makes many live API calls per row (some
# separately rate-limited for the whole-repo search) - on the whole dataset it can
# take several hours, which is why it is behind an explicit flag and not in the
# default behavior.
#
# --tag SUFFIX: makes the whole pipeline write under different names, so a
# new run does not overwrite the results already present:
#   dataset_<SUFFIX>_<since>_<until>.json   (instead of dataset_<since>_<until>.json)
#   cg_dataset_<SUFFIX>.jsonl etc.          (instead of cg_dataset.jsonl etc.)
#   pipeline/out_<SUFFIX>/                  (instead of pipeline/out/)
# Without --tag the names are the usual ones (behavior unchanged).
#
# --discovery-timeout: discovery (step 1) accumulates everything in memory and
# writes the snapshot ONLY at the end - if time runs out halfway, the process
# is interrupted (SIGTERM) and NO file is written. Useful for a quick
# test ("how long does it take to start, does the GitHub API respond, etc."), not
# for getting a usable partial result anyway: if the
# timeout fires the script stops with a clear message, it does not proceed to
# step 2 (the input would be missing).
# Steps 1-2 (discovery+fetch, live from GitHub/chatgpt.com/claude.ai/gemini) are NOT
# guaranteed to reproduce the same result over time (links that disappear,
# rate limits) - for this reason by default they are SKIPPED if their output already
# exists on disk (treated as a fixed snapshot), and rerun only with --refetch.
# Steps 3-6 are deterministic and always run, starting from that
# snapshot: they are the "100% reproducible" part of the pipeline.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
cd "$ROOT"

# ------------------------------------------------------------------
# Automatic check/installation of the Python dependencies.
# Maps "pip name":"import name" because they do not always coincide
# (e.g. beautifulsoup4 is imported as bs4).
# ------------------------------------------------------------------
check_and_install_deps() {
  echo "Checking Python dependencies..."
  local pip_names=(ijson pygments playwright beautifulsoup4 lxml requests langdetect lingua-language-detector)
  local import_names=(ijson pygments playwright bs4 lxml requests langdetect lingua)
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

  # playwright also needs the Chromium browser downloaded separately
  # (the installation is idempotent: if already present it downloads nothing)
  echo "Checking Playwright browser (Chromium)..."
  python -m playwright install chromium

  if [[ -z "$GITHUB_TOKEN" ]]; then
    echo
    echo "WARNING: GITHUB_TOKEN not set."
    echo "  Without it, discovery (step 1) is limited to 60 requests/hour and"
    echo "  the code file search is DISABLED entirely."
    echo "  Recommended: export GITHUB_TOKEN=ghp_..."
    echo
  fi
}

check_and_install_deps

REFETCH=0
DISCOVERY_TIMEOUT=""
TAG=""
TRACK_SNIPPETS=0
SINCE_ARG=""
UNTIL_ARG=""
FETCH_LIMIT_ARG=""
LANGUAGES_ARGS=()
SOURCES_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --refetch)
      REFETCH=1
      shift
      ;;
    --discovery-timeout)
      DISCOVERY_TIMEOUT="$2"
      shift 2
      ;;
    --tag)
      TAG="$2"
      shift 2
      ;;
    --track-snippets)
      TRACK_SNIPPETS=1
      shift
      ;;
    --since)
      SINCE_ARG="$2"
      shift 2
      ;;
    --until)
      UNTIL_ARG="$2"
      shift 2
      ;;
    --fetch-limit)
      FETCH_LIMIT_ARG="$2"
      shift 2
      ;;
    --sources)
      shift
      while [[ $# -gt 0 && "$1" != --* ]]; do
        SOURCES_ARGS+=("$1")
        shift
      done
      ;;
    --languages)
      shift
      while [[ $# -gt 0 && "$1" != --* ]]; do
        LANGUAGES_ARGS+=("$1")
        shift
      done
      ;;
    *)
      echo "Unrecognized argument: $1"
      exit 1
      ;;
  esac
done

TIMEOUT_CMD=()
if [[ -n "$DISCOVERY_TIMEOUT" ]]; then
  if command -v timeout &>/dev/null; then
    TIMEOUT_CMD=(timeout "${DISCOVERY_TIMEOUT}s")
  elif command -v gtimeout &>/dev/null; then
    TIMEOUT_CMD=(gtimeout "${DISCOVERY_TIMEOUT}s")
  else
    echo "WARNING: --discovery-timeout requested but neither 'timeout' nor 'gtimeout'"
    echo "is available (on macOS: brew install coreutils). Ignoring the limit."
  fi
fi

# --sources: which sources to search in discovery (default all). Useful for a
# quick test: "code" (code file search) does NOT respect the date window
# and can take hours, whereas issues/prs/hn stop at the window.
[[ ${#SOURCES_ARGS[@]} -eq 0 ]] && SOURCES_ARGS=(issues prs code hn)
SINCE="${SINCE_ARG:-2023-05-01}"
UNTIL="${UNTIL_ARG:-2026-08-10}"

if [[ -n "$TAG" ]]; then
  DATASET_OUT="dataset_${TAG}"
  CG_OUT="cg_dataset_${TAG}"
  PIPELINE_OUT_DIR="$SCRIPT_DIR/out_${TAG}"
  echo "Tag active: '$TAG' - this run writes under different names, it does not touch the existing files."
else
  DATASET_OUT="dataset"
  CG_OUT="cg_dataset"
  PIPELINE_OUT_DIR="$SCRIPT_DIR/out"
fi

CHATGPT_RAW="${DATASET_OUT}_${SINCE}_${UNTIL}.json"
CG_DATASET="${CG_OUT}.jsonl"
CG_TURNS="${CG_OUT}.turns.jsonl"

echo "=================================================================="
echo "STEP 1: Link discovery (issues/PR/code/HN, all 3 providers)"
echo "=================================================================="

if [[ -f "$CHATGPT_RAW" && $REFETCH -eq 0 ]]; then
  echo "$CHATGPT_RAW already present, skipping discovery (use --refetch to rerun)"
else
  if [[ -n "$DISCOVERY_TIMEOUT" ]]; then
    echo "Launching discovery (Stage A), time limit: ${DISCOVERY_TIMEOUT}s..."
  else
    echo "Launching discovery (Stage A, no conversation fetch)..."
  fi
  set +e
  "${TIMEOUT_CMD[@]}" python pipeline/1_01_chat_discover_links.py \
    --since "$SINCE" --until "$UNTIL" \
    --sources "${SOURCES_ARGS[@]}" \
    --out "$DATASET_OUT"
  DISCOVERY_EXIT=$?
  set -e

  if [[ $DISCOVERY_EXIT -eq 124 ]]; then
    echo
    echo "Discovery interrupted by timeout (${DISCOVERY_TIMEOUT}s) before completion."
    echo "No snapshot written (the script saves only at the end of collection) - the"
    echo "pipeline stops here: rerun without --discovery-timeout, or with a"
    echo "higher value, or restrict --sources for a quicker test."
    exit 1
  elif [[ $DISCOVERY_EXIT -ne 0 ]]; then
    echo "Discovery failed (exit code $DISCOVERY_EXIT)."
    exit $DISCOVERY_EXIT
  fi
fi

echo
echo "=================================================================="
echo "STEP 2: Fetch conversations (ChatGPT + Claude + Gemini)"
echo "=================================================================="

if [[ -f "$CG_DATASET" && -f "$CG_TURNS" && $REFETCH -eq 0 ]]; then
  echo "$CG_DATASET + $CG_TURNS already present, skipping fetch (use --refetch to rerun)"
  echo "(note: if you only need to re-fetch ChatGPT because cg_dataset is already fine,"
  echo " call python pipeline/1_02_chat_fetch_conversations.py directly)"
else
  echo "Launching fetch for all 3 providers (can take hours, automatic resume)..."
  FETCH_LIMIT_FLAG=()
  if [[ -n "$FETCH_LIMIT_ARG" ]]; then
    FETCH_LIMIT_FLAG=(--fetch-limit "$FETCH_LIMIT_ARG")
    echo "  (--fetch-limit $FETCH_LIMIT_ARG: stops after the first $FETCH_LIMIT_ARG ChatGPT conversations and the first $FETCH_LIMIT_ARG Claude+Gemini combined)"
  fi
  python pipeline/1_02_chat_fetch_conversations.py \
    --since "$SINCE" --until "$UNTIL" \
    --out "$DATASET_OUT" --cg-out "$CG_OUT" "${FETCH_LIMIT_FLAG[@]}"
fi

echo
echo "=================================================================="
echo "STEP 3: Normalization + conversation metadata (unified)"
echo "=================================================================="
python pipeline/1_03_chat_normalize.py \
  --chatgpt-raw "$CHATGPT_RAW" --cg-dataset "$CG_DATASET" --cg-turns "$CG_TURNS" \
  --out-dir "$PIPELINE_OUT_DIR"

echo
echo "=================================================================="
echo "STEP 4: Snippet extraction + step/alternative collapse + quality filters"
echo "=================================================================="
python pipeline/1_04_extract_and_filter.py --out-dir "$PIPELINE_OUT_DIR"

echo
echo "=================================================================="
echo "STEP 5: Adding prompt context"
echo "=================================================================="
python pipeline/1_05_chat_add_context.py --out-dir "$PIPELINE_OUT_DIR"

echo
echo "=================================================================="
echo "STEP 6: Split by language"
echo "=================================================================="
if [[ ${#LANGUAGES_ARGS[@]} -gt 0 ]]; then
  python pipeline/1_06_chat_split_by_language.py --out-dir "$PIPELINE_OUT_DIR" --languages "${LANGUAGES_ARGS[@]}"
else
  python pipeline/1_06_chat_split_by_language.py --out-dir "$PIPELINE_OUT_DIR"
fi

if [[ $TRACK_SNIPPETS -eq 1 ]]; then
  echo
  echo "=================================================================="
  echo "STEP 7: Snippet provenance (what happens to the code in the real repo)"
  echo "=================================================================="
  echo "Warning: makes many API calls per row, including separately"
  echo "rate-limited searches - on the whole dataset it can take several hours."
  python pipeline/2_01_chat_track_snippets.py --out-dir "$PIPELINE_OUT_DIR"

  echo
  echo "=================================================================="
  echo "STEP 8: Similarity recheck (not only exact substring)"
  echo "=================================================================="
  python pipeline/2_02_chat_similarity_recheck.py --out-dir "$PIPELINE_OUT_DIR"

  echo
  echo "=================================================================="
  echo "STEP 9: Recheck of intermediate PR commits (GitHubPullRequest rows)"
  echo "=================================================================="
  python pipeline/2_03_chat_pr_commits_recheck.py --out-dir "$PIPELINE_OUT_DIR"

  echo
  echo "=================================================================="
  echo "STEP 10: Commit history recheck (direct GitHubCodeFile rows)"
  echo "=================================================================="
  python pipeline/2_04_chat_codefile_commits_recheck.py --out-dir "$PIPELINE_OUT_DIR"

  echo
  echo "=================================================================="
  echo "STEP 11: Re-verification with stricter criteria (whole-repo + file history)"
  echo "=================================================================="
  python pipeline/2_05_chat_reverify_wholerepo.py --out-dir "$PIPELINE_OUT_DIR"

  echo
  echo "=================================================================="
  echo "STEP 12: Timeline consolidation (final snippet_timeline.csv)"
  echo "=================================================================="
  python pipeline/2_06_chat_timeline.py --out-dir "$PIPELINE_OUT_DIR"
fi

echo
echo "=================================================================="
echo "Pipeline completed. Final output: ${PIPELINE_OUT_DIR}/per_language/"
echo "=================================================================="
