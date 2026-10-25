#!/bin/bash
# Runs both halves of the unified dataset from scratch: prompt/chat
# (run_chat_pipeline.sh) and agentic IDE+CLI (run_agentic_pipeline.sh), in
# sequence - they share the same GitHub token pool (pipeline/tokens.txt),
# running them in parallel would make them compete for the same rate limit.
#
# Usage:
#   ./run_all.sh                                        # both datasets, default windows
#   ./run_all.sh --chat-since 2023-05-01 --chat-until 2026-06-04 \
#                --agentic-since 2025-01-01 --agentic-until 2026-09-28
#   ./run_all.sh --skip-chat                             # agentic dataset only
#   ./run_all.sh --skip-agentic                          # prompt/chat dataset only
#   ./run_all.sh --track-snippets --adoption              # additionally run the adoption/provenance checks (slow)
#   ./run_all.sh --harness                                 # additionally (agentic only), extract the harness mechanisms per repo
#   ./run_all.sh --no-publish                              # do not write release/datasets/ (English names) at the end of the run
#   ./run_all.sh --classify                              # additionally PREPARE (free) the intent classification, without submitting it
#
# --classify: runs run_chat_classification.sh / run_agentic_classification.sh
# WITHOUT --submit (it stops at the point where the batches are ready on disk,
# no API call / no cost). If pipeline/4_01_chat_intent_classification/data/ already contains a
# real preparation (e.g. for out_ago2026), the chat step stops with an
# error instead of overwriting it - see the comment in run_chat_classification.sh.
#
# Every option specific to one or the other pipeline (--refetch, --tag,
# --discovery-timeout, --languages for the chat one; --workers for the
# agentic one) must be passed directly to the corresponding script, not here -
# this is only a higher-level orchestrator.

set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

SKIP_CHAT=0
SKIP_AGENTIC=0
CHAT_SINCE=""
CHAT_UNTIL=""
AGENTIC_SINCE=""
AGENTIC_UNTIL=""
TRACK_SNIPPETS=0
ADOPTION=0
HARNESS=0
NO_MERGE=0
CLASSIFY=0
NO_PUBLISH=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-chat)
      SKIP_CHAT=1
      shift
      ;;
    --skip-agentic)
      SKIP_AGENTIC=1
      shift
      ;;
    --chat-since)
      CHAT_SINCE="$2"
      shift 2
      ;;
    --chat-until)
      CHAT_UNTIL="$2"
      shift 2
      ;;
    --agentic-since)
      AGENTIC_SINCE="$2"
      shift 2
      ;;
    --agentic-until)
      AGENTIC_UNTIL="$2"
      shift 2
      ;;
    --track-snippets)
      TRACK_SNIPPETS=1
      shift
      ;;
    --adoption)
      ADOPTION=1
      shift
      ;;
    --harness)
      HARNESS=1
      shift
      ;;
    --no-merge)
      NO_MERGE=1
      shift
      ;;
    --no-publish)
      NO_PUBLISH=1
      shift
      ;;
    --classify)
      CLASSIFY=1
      shift
      ;;
    *)
      echo "Unrecognized argument: $1"
      echo "(options specific to a single pipeline: run directly"
      echo " pipeline/run_chat_pipeline.sh or pipeline/run_agentic_pipeline.sh)"
      exit 1
      ;;
  esac
done

if [[ $SKIP_CHAT -eq 0 ]]; then
  echo "######################################################################"
  echo "# DATASET 1/2: PROMPT (chat) - run_chat_pipeline.sh"
  echo "######################################################################"
  CHAT_ARGS=()
  [[ -n "$CHAT_SINCE" ]] && CHAT_ARGS+=(--since "$CHAT_SINCE")
  [[ -n "$CHAT_UNTIL" ]] && CHAT_ARGS+=(--until "$CHAT_UNTIL")
  [[ $TRACK_SNIPPETS -eq 1 ]] && CHAT_ARGS+=(--track-snippets)
  ./pipeline/run_chat_pipeline.sh "${CHAT_ARGS[@]}"
else
  echo "Prompt (chat) dataset skipped (--skip-chat)."
fi

echo
if [[ $SKIP_AGENTIC -eq 0 ]]; then
  echo "######################################################################"
  echo "# DATASET 2/2: AGENTIC (IDE + CLI) - run_agentic_pipeline.sh"
  echo "######################################################################"
  AGENTIC_ARGS=()
  [[ -n "$AGENTIC_SINCE" ]] && AGENTIC_ARGS+=(--since "$AGENTIC_SINCE")
  [[ -n "$AGENTIC_UNTIL" ]] && AGENTIC_ARGS+=(--until "$AGENTIC_UNTIL")
  [[ $ADOPTION -eq 1 ]] && AGENTIC_ARGS+=(--adoption)
  [[ $HARNESS -eq 1 ]] && AGENTIC_ARGS+=(--harness)
  ./pipeline/run_agentic_pipeline.sh "${AGENTIC_ARGS[@]}"
else
  echo "Agentic (IDE+CLI) dataset skipped (--skip-agentic)."
fi

if [[ $SKIP_CHAT -eq 0 && $SKIP_AGENTIC -eq 0 && $NO_MERGE -eq 0 ]]; then
  echo
  echo "######################################################################"
  echo "# MERGE: single dataset (prompt + agentic), 'dataset' column"
  echo "######################################################################"
  python pipeline/7_01_merge_datasets.py \
    --chat-dir pipeline/out --agentic-dir pipeline/out_vibe_coding \
    --out-dir pipeline/out_unified
fi

if [[ $CLASSIFY -eq 1 ]]; then
  if [[ $SKIP_CHAT -eq 0 ]]; then
    echo
    echo "######################################################################"
    echo "# INTENT CLASSIFICATION (chat) - prepare only, no submission/cost"
    echo "######################################################################"
    ./pipeline/run_chat_classification.sh --out-dir "$ROOT/pipeline/out"
  fi
  if [[ $SKIP_AGENTIC -eq 0 ]]; then
    echo
    echo "######################################################################"
    echo "# INTENT CLASSIFICATION (agentic) - prepare only, no submission/cost"
    echo "######################################################################"
    ./pipeline/run_agentic_classification.sh
  fi
fi

if [[ $NO_PUBLISH -eq 0 ]]; then
  echo
  echo "######################################################################"
  echo "# PUBLISH: final datasets with English file/column/value names -> release/datasets/"
  echo "######################################################################"
  PUB_ARGS=(--out-dir release)
  [[ $SKIP_CHAT -eq 0 ]] && PUB_ARGS+=(--chat-dir "$ROOT/pipeline/out")
  python pipeline/7_02_publish_datasets.py "${PUB_ARGS[@]}"
fi

echo
echo "######################################################################"
echo "# DONE"
echo "######################################################################"
[[ $SKIP_CHAT -eq 0 ]] && echo "Prompt dataset:   pipeline/out/ (or pipeline/out_<tag>/)"
[[ $SKIP_AGENTIC -eq 0 ]] && echo "Agentic dataset:  pipeline/out_vibe_coding/"
[[ $SKIP_CHAT -eq 0 && $SKIP_AGENTIC -eq 0 && $NO_MERGE -eq 0 ]] && echo "Unified dataset: pipeline/out_unified/"
[[ $CLASSIFY -eq 1 ]] && echo "Classification (prepare): pipeline/4_01_chat_intent_classification*/data/batch_inputs/ (not submitted - see --submit)"
