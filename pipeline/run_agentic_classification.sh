#!/bin/bash
# Orchestrator of the intent classification for the agentic dataset
# (IDE + CLI, out_vibe_coding) - equivalent of run_chat_classification.sh.
# It DELIBERATELY stops before submission (it costs real money, OpenAI
# Batch API) unless --submit is given explicitly.
#
# Usage:
#   ./pipeline/run_agentic_classification.sh                    # prepare only (free), then stops
#   ./pipeline/run_agentic_classification.sh --submit            # additionally, submits and downloads all the batches
#   ./pipeline/run_agentic_classification.sh --submit --crosstab  # additionally, crosses with adoption after the download
#
# Chain:
#   1. 1_prepare_context.py - builds data/messages.csv (local,
#      free, but re-extracts the candidates for every session: a few minutes)
#   2. 2_classify.py prepare            - builds data/batch_inputs/*.jsonl,
#      chunk_size=500 (local, free)
#   3. (--submit) run_sequential.sh   - submits/waits/downloads EVERY batch one
#      at a time, N_BATCHES computed from the files just written
#   4. (--crosstab) 4_04_agentic_intent_adoption.py - crosses the labels
#      with adoption (requires that run_agentic_pipeline.sh --adoption
#      has already been run at least once)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
cd "$ROOT"

SUBMIT=0
CROSSTAB=0
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
    *)
      echo "Unrecognized argument: $1"
      exit 1
      ;;
  esac
done

echo "=================================================================="
echo "STEP 1: Message preparation (1_prepare_context.py)"
echo "=================================================================="
python3 pipeline/4_02_agentic_intent_classification/1_prepare_context.py

echo
echo "=================================================================="
echo "STEP 2: Batch preparation for OpenAI Batch API (2_classify.py prepare)"
echo "=================================================================="
( cd pipeline/4_02_agentic_intent_classification && python3 2_classify.py prepare 999999 500 )

N_BATCHES=$(ls pipeline/4_02_agentic_intent_classification/data/batch_inputs/batch_input_*.jsonl 2>/dev/null | wc -l | tr -d ' ')
echo
echo "Batches prepared: $N_BATCHES (no API call made so far, all local/free)"

if [[ $SUBMIT -eq 0 ]]; then
    echo
    echo "Stopping here (no --submit). To really submit:"
    echo "  ./pipeline/run_agentic_classification.sh --submit"
    exit 0
fi

echo
echo "=================================================================="
echo "STEP 3: Sequential submit/wait/download ($N_BATCHES batches) - SPENDS MONEY"
echo "=================================================================="
./pipeline/4_02_agentic_intent_classification/run_sequential.sh

if [[ $CROSSTAB -eq 1 ]]; then
    echo
    echo "=================================================================="
    echo "STEP 4: Intent x adoption crosstab (4_04_agentic_intent_adoption.py)"
    echo "=================================================================="
    python3 pipeline/4_04_agentic_intent_adoption.py --out-dir out_vibe_coding
fi

echo
echo "=================================================================="
echo "Agentic classification completed."
echo "=================================================================="
