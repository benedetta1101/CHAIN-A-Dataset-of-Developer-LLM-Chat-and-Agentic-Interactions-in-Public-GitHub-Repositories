#!/usr/bin/env python3
"""
Step 2 - Classification of behavioral intent via OpenAI Batch API,
adapted from empirical-conversational-programming/4_01_chat_intent_classification/
classification_batch.py (Tang et al., ASE '26) to read data/messages.csv
(produced by 1_prepare_context.py) instead of their CSV.

Usage:
  python3 2_classify.py prepare [limit] [chunk_size]   # writes data/batch_inputs/*.jsonl
  python3 2_classify.py submit <batch_no>               # submits a batch (SPENDS MONEY - requires confirmation)
  python3 2_classify.py status <batch_no>
  python3 2_classify.py download <batch_no>              # downloads + parses
  python3 2_classify.py errors <batch_no>
  python3 2_classify.py peek <batch_no>

Requires OPENAI_API_KEY in the environment.
"""
import json
import os
from pathlib import Path

import pandas as pd

from lib_categories import VALID_LABELS

SCRIPT_DIR = Path(__file__).parent
CODEBOOK = open(SCRIPT_DIR / "codebook.txt").read()

DATA_DIR = SCRIPT_DIR / "data"
BATCH_INPUT_DIR = DATA_DIR / "batch_inputs"
RAW_OUTPUT_DIR = DATA_DIR / "raw_output"
BATCH_ID_DIR = DATA_DIR / "batch_ids"
PARSED_OUTPUT_DIR = DATA_DIR / "parsed"

MODEL = "gpt-5-mini"

RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "classification",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "labels": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "reasoning": {"type": "string"},
                            "main_category": {"type": "string"},
                            "sub_category": {"type": "string"},
                        },
                        "required": ["reasoning", "main_category", "sub_category"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["labels"],
            "additionalProperties": False,
        },
    },
}


def build_user_prompt(row):
    context = str(row.get("context", "")) if not pd.isna(row.get("context", "")) else ""
    current = str(row.get("truncated_content", ""))
    parts = []
    if context:
        parts.append(context)
    parts.append(f"[current]: {current}")
    return "\n".join(parts)


def ensure_dirs():
    for d in (BATCH_INPUT_DIR, RAW_OUTPUT_DIR, BATCH_ID_DIR, PARSED_OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)


def get_batch_tag(batch_no):
    return f"{int(batch_no):04d}"


def get_batch_input_path(batch_no):
    return BATCH_INPUT_DIR / f"batch_input_{get_batch_tag(batch_no)}.jsonl"


def get_batch_id_path(batch_no):
    return BATCH_ID_DIR / f"batch_id_{get_batch_tag(batch_no)}.txt"


def get_parsed_output_path(batch_no):
    return PARSED_OUTPUT_DIR / f"classifications_{get_batch_tag(batch_no)}.jsonl"


# -- prepare -------------------------------------------------------------
def prepare_batch_files(df, chunk_size=10000, limit=None):
    ensure_dirs()
    if limit:
        df = df.head(limit)
    total = len(df)
    if total == 0:
        print("No rows - no file written.")
        return []

    written = []
    for batch_index, start in enumerate(range(0, total, chunk_size), start=1):
        end = min(start + chunk_size, total)
        chunk = df.iloc[start:end]
        out_path = get_batch_input_path(batch_index)
        with open(out_path, "w", encoding="utf-8") as f:
            for _, row in chunk.iterrows():
                request = {
                    "custom_id": str(int(row["message_id"])),
                    "method": "POST",
                    "url": "/v1/chat/completions",
                    "body": {
                        "model": MODEL,
                        "messages": [
                            {"role": "system", "content": CODEBOOK},
                            {"role": "user", "content": build_user_prompt(row)},
                        ],
                        "response_format": RESPONSE_SCHEMA,
                        "max_completion_tokens": 4096,
                    },
                }
                f.write(json.dumps(request, ensure_ascii=False) + "\n")
        written.append(out_path)
        print(f"Written {out_path} ({end - start} requests, message_id {start}..{end - 1})")

    print(f"\nPrepared {len(written)} batch files for {total} messages (chunk_size={chunk_size}).")
    return written


# -- submit / status / download (identical to the original pipeline) -------
def _client():
    from openai import OpenAI
    return OpenAI()


def submit_batch(batch_no):
    ensure_dirs()
    client = _client()
    batch_input_path = get_batch_input_path(batch_no)
    with open(batch_input_path, "rb") as f:
        uploaded = client.files.create(file=f, purpose="batch")
    batch = client.batches.create(
        input_file_id=uploaded.id,
        endpoint="/v1/chat/completions",
        completion_window="24h",
        metadata={"description": "ago2026 intent classification"},
    )
    with open(get_batch_id_path(batch_no), "w") as f:
        f.write(batch.id)
    print(f"Batch submitted: {batch.id}  status={batch.status}")
    return batch.id


def check_status(batch_no):
    client = _client()
    batch_id = open(get_batch_id_path(batch_no)).read().strip()
    batch = client.batches.retrieve(batch_id)
    print(f"Batch ID: {batch.id}")
    print(f"Status:   {batch.status}")
    print(f"Progress: {batch.request_counts.completed} / {batch.request_counts.total}")
    return batch


def download_and_parse(batch_no):
    client = _client()
    batch_id = open(get_batch_id_path(batch_no)).read().strip()
    batch = client.batches.retrieve(batch_id)
    if batch.status != "completed":
        print(f"Batch not ready yet. Status: {batch.status}")
        return

    ensure_dirs()
    raw_path = RAW_OUTPUT_DIR / f"{batch_id}.jsonl"
    content = client.files.content(batch.output_file_id).text
    with open(raw_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"Raw output saved: {raw_path}")
    parse_results(raw_path, get_parsed_output_path(batch_no))


def parse_results(raw_path, output_path):
    results = []
    invalid = failure = 0
    with open(raw_path, encoding="utf-8") as f:
        lines = f.read().strip().split("\n")

    for line in lines:
        record = json.loads(line)
        idx = int(record["custom_id"])
        if record.get("error"):
            results.append({"message_id": idx, "status": f"failure: {record['error']}", "labels": []})
            failure += 1
            continue
        body = record["response"]["body"]
        content_str = body["choices"][0]["message"]["content"]
        if not content_str:
            results.append({"message_id": idx, "status": "failure: empty content", "labels": []})
            failure += 1
            continue
        parsed = json.loads(content_str)
        labels = parsed.get("labels", [])
        if not labels:
            results.append({"message_id": idx, "status": "failure: empty labels", "labels": []})
            failure += 1
            continue
        all_valid = all((l.get("main_category", ""), l.get("sub_category", "")) in VALID_LABELS for l in labels)
        if not all_valid:
            invalid += 1
        results.append({
            "message_id": idx,
            "status": "success" if all_valid else "invalid_category",
            "labels": labels,
        })

    results.sort(key=lambda r: r["message_id"])
    with open(output_path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\n-- Summary --")
    print(f"  Total:           {len(results)}")
    print(f"  Success:         {len(results) - failure - invalid}")
    print(f"  Invalid category: {invalid}")
    print(f"  Failures:        {failure}")
    print(f"Classifications saved in {output_path}")


if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "prepare"

    if cmd == "prepare":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
        chunk_size = int(sys.argv[3]) if len(sys.argv) > 3 else 10000
        df = pd.read_csv(DATA_DIR / "messages.csv")
        prepare_batch_files(df, chunk_size=chunk_size, limit=limit)
    elif cmd == "submit":
        submit_batch(sys.argv[2])
    elif cmd == "status":
        check_status(sys.argv[2])
    elif cmd == "download":
        download_and_parse(sys.argv[2])
    elif cmd == "errors":
        client = _client()
        batch_id = open(get_batch_id_path(sys.argv[2])).read().strip()
        batch = client.batches.retrieve(batch_id)
        print(f"Error file ID: {batch.error_file_id}")
        if batch.error_file_id:
            content = client.files.content(batch.error_file_id).text
            for line in content.strip().split("\n")[:3]:
                print(json.dumps(json.loads(line), indent=2, ensure_ascii=False))
    elif cmd == "peek":
        client = _client()
        batch_id = open(get_batch_id_path(sys.argv[2])).read().strip()
        batch = client.batches.retrieve(batch_id)
        content = client.files.content(batch.output_file_id).text
        print(json.dumps(json.loads(content.strip().split("\n")[0]), indent=2, ensure_ascii=False))
    else:
        print("Usage: python3 2_classify.py [prepare [limit] [chunk_size]|submit N|status N|download N|errors N|peek N]")
