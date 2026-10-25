#!/bin/bash
# Submits the classification batches ONE AT A TIME (waits for completion
# before submitting the next one) to stay under the limit of 5,000,000
# "enqueued" tokens for gpt-5-mini on the whole account - same constraint
# and same solution already validated in 4_01_chat_intent_classification/run_sequential.sh
# (out_ago2026), here adapted for the vibe-coding dataset (30 batches of 500
# requests, data/batch_inputs/ already prepared by 2_classify.py prepare).
#
# NO `set -e`: a failed status check due to a transient network hiccup must
# not be able to silently kill the whole loop - lesson learned in the
# out_ago2026 run (batch 57: completed server-side, just not downloaded
# because of a network gap) - before re-submitting a batch,
# check whether a batch_id already exists for that number.
cd "$(dirname "${BASH_SOURCE[0]}")"
export OPENAI_API_KEY=$(cat .openai_key | tr -d '[:space:]')

# computed from the files that 2_classify.py prepare actually wrote,
# no longer hand-written - so it does not get out of sync if messages.csv changes
# (e.g. after a dataset rebuild) and one forgets to update it
N_BATCHES=$(ls data/batch_inputs/batch_input_*.jsonl 2>/dev/null | wc -l | tr -d ' ')
if [[ "$N_BATCHES" -eq 0 ]]; then
    echo "ERROR: no data/batch_inputs/batch_input_*.jsonl found - first run 'python3 2_classify.py prepare'."
    exit 1
fi
echo "N_BATCHES detected: $N_BATCHES"

for n in $(seq 1 $N_BATCHES); do
    tag=$(printf '%04d' $n)
    parsed="data/parsed/classifications_${tag}.jsonl"
    batch_id_file="data/batch_ids/batch_id_${tag}.txt"
    if [ -f "$parsed" ]; then
        echo "batch $n already completed, skipping"
        continue
    fi

    if [ -f "$batch_id_file" ]; then
        echo "=== batch $n/$N_BATCHES: batch_id already exists, resuming without re-submitting ==="
    else
        echo "=== batch $n/$N_BATCHES: submitting ==="
        for attempt in 1 2 3 4 5; do
            if python3 2_classify.py submit $n; then
                break
            fi
            echo "  ! submit failed (attempt $attempt/5), retrying in 30s"
            sleep 30
        done
    fi

    while true; do
        s=$(python3 2_classify.py status $n 2>&1)
        ok=$?
        state=$(echo "$s" | grep "Status:" | awk '{print $2}')
        if [ $ok -ne 0 ] || [ -z "$state" ]; then
            echo "  $(date '+%H:%M:%S') batch $n: check failed, retrying in 30s ($s)"
            sleep 30
            continue
        fi
        echo "  $(date '+%H:%M:%S') batch $n: $state"
        if [ "$state" = "completed" ]; then
            for attempt in 1 2 3 4 5; do
                if python3 2_classify.py download $n; then
                    break
                fi
                echo "  ! download failed (attempt $attempt/5), retrying in 30s"
                sleep 30
            done
            break
        elif [ "$state" = "failed" ] || [ "$state" = "expired" ] || [ "$state" = "cancelled" ]; then
            echo "  ! batch $n failed (status=$state) - stopping, check errors $n"
            exit 1
        fi
        sleep 20
    done
done

echo "ALL $N_BATCHES BATCHES COMPLETED"
