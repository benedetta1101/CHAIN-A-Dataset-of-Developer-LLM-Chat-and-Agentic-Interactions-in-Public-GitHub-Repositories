#!/bin/bash
# Orchestrator of the intent classification (prompt/chat, out_ago2026) -
# equivalent of run_chat_pipeline.sh/run_agentic_pipeline.sh for this
# phase. It DELIBERATELY stops before submission (it costs real money,
# OpenAI Batch API) unless --submit is given explicitly.
#
# Usage:
#   ./pipeline/run_chat_classification.sh                    # prepare only (free), then stops
#   ./pipeline/run_chat_classification.sh --submit            # additionally, submits and downloads all the batches
#   ./pipeline/run_chat_classification.sh --submit --crosstab  # additionally, crosses with adoption after the download
#   ./pipeline/run_chat_classification.sh --out-dir pipeline/out  # classify another build (not out_ago2026)
#
# Chain:
#   1. 1_prepare_context.py      - reads snippet.csv and conversations.jsonl of --out-dir,
#                                 builds data/messages.csv (local, free)
#   2. 2_classify.py prepare     - builds data/batch_inputs/*.jsonl, chunk_size=500
#                                 (threshold that keeps each batch under the limit of
#                                 5,000,000 "enqueued" tokens for gpt-5-mini, see
#                                 run_sequential.sh) - local, free
#   3. (--submit) run_sequential.sh - submits/waits/downloads EVERY batch one at a
#                                 time, N_BATCHES computed from the files just written
#   4. (--crosstab) 4_03_chat_intent_adoption.py - crosses the labels with adoption,
#                                 on the same --out-dir as this run
#
# --out-dir: which pipeline/outXXX to classify (default: out_ago2026, the
# reference dataset).

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
cd "$ROOT"

SUBMIT=0
CROSSTAB=0
OUT_DIR_ARG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --submit)
      SUBMIT=1
      shift
      ;;
    --crosstab)
      CROSSTAB=1
      shift
      ;;
    --out-dir)
      OUT_DIR_ARG="$2"
      shift 2
      ;;
    *)
      echo "Unrecognized argument: $1"
      exit 1
      ;;
  esac
done

DEFAULT_OUT_DIR="$ROOT/pipeline/out_ago2026"
OUT_DIR_FLAG=()
[[ -n "$OUT_DIR_ARG" ]] && OUT_DIR_FLAG=(--out-dir "$OUT_DIR_ARG")

# 4_01_chat_intent_classification/data/ (messages.csv, batch_inputs/, etc.) is NOT
# isolated per --out-dir - it is a single shared folder
# (2_classify.py is not parameterized: it is also used for submit/download of the
# paid batch, too risky to touch it for this change). If it already contains
# data (e.g. the real prepare for out_ago2026, or worse already submitted)
# and a DIFFERENT --out-dir is being classified, stop instead of
# silently overwriting - the user must manually move/rename
# pipeline/4_01_chat_intent_classification/data/ before proceeding.
if [[ -n "$OUT_DIR_ARG" && "$OUT_DIR_ARG" != "$DEFAULT_OUT_DIR" && "$OUT_DIR_ARG" != "out_ago2026" && "$OUT_DIR_ARG" != "pipeline/out_ago2026" \
      && -f "$ROOT/pipeline/4_01_chat_intent_classification/data/messages.csv" ]]; then
  echo
  echo "ERROR: pipeline/4_01_chat_intent_classification/data/messages.csv already exists."
  echo "--out-dir '$OUT_DIR_ARG' is different from the reference dataset - rerunning"
  echo "here would overwrite the preparation (and the batches) already done for the real one."
  echo "Move/rename pipeline/4_01_chat_intent_classification/data/ before proceeding, e.g.:"
  echo "  mv pipeline/4_01_chat_intent_classification/data pipeline/4_01_chat_intent_classification/data_out_ago2026"
  exit 1
fi

echo
echo "=================================================================="
echo "STEP 1: Message preparation (1_prepare_context.py)"
echo "=================================================================="
python3 pipeline/4_01_chat_intent_classification/1_prepare_context.py "${OUT_DIR_FLAG[@]}"

echo
echo "=================================================================="
echo "STEP 2: Batch preparation for OpenAI Batch API (2_classify.py prepare)"
echo "=================================================================="
( cd pipeline/4_01_chat_intent_classification && python3 2_classify.py prepare 999999 500 )

N_BATCHES=$(ls pipeline/4_01_chat_intent_classification/data/batch_inputs/batch_input_*.jsonl 2>/dev/null | wc -l | tr -d ' ')
echo
echo "Batches prepared: $N_BATCHES (no API call made so far, all local/free)"

if [[ $SUBMIT -eq 0 ]]; then
    echo
    echo "Stopping here (no --submit). To estimate the cost before submitting:"
    echo "  sum the tokens of the files in pipeline/4_01_chat_intent_classification/data/batch_inputs/"
    echo "  (see the hand estimate made in the chat for out_ago2026/vibe_coding as an example)."
    echo "To really submit: ./pipeline/run_chat_classification.sh --submit"
    exit 0
fi

echo
echo "=================================================================="
echo "STEP 3: Sequential submit/wait/download ($N_BATCHES batches) - SPENDS MONEY"
echo "=================================================================="
./pipeline/4_01_chat_intent_classification/run_sequential.sh

if [[ $CROSSTAB -eq 1 ]]; then
    echo
    echo "=================================================================="
    echo "STEP 4: Intent x adoption crosstab (4_03_chat_intent_adoption.py)"
    echo "=================================================================="
    python3 pipeline/4_03_chat_intent_adoption.py --out-dir "${OUT_DIR_ARG:-out_ago2026}"
fi

echo
echo "=================================================================="
echo "Chat classification completed."
echo "=================================================================="
