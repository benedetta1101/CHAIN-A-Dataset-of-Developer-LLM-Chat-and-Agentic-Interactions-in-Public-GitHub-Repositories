#!/usr/bin/env python
"""
Step 2 - Fetch the conversations for all 3 providers (ChatGPT, Claude,
Gemini) starting from the discovery snapshot produced by step 1.

This is not a "unified" fetch in the sense of a single mechanism: ChatGPT
(scrape via Playwright, already included in 1_01_chat_discover_links.py as Stage B)
and Claude/Gemini (Claude API + Gemini scrape, in lib_fetch_claude_gemini_impl.py)
use different mechanisms out of necessity (different sites/APIs) - this
script is a single entry point that invokes both in sequence on the same
discovery snapshot, so from the point of view of use it is a single command.

Usage:
    python pipeline/1_02_chat_fetch_conversations.py --since 2023-05-01 --until 2026-06-04

Reads/updates the discovery snapshot (same file for both calls):
dataset_<since>_<until>.json
"""
import argparse
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default="2023-05-01")
    ap.add_argument("--until", default="2026-06-04")
    ap.add_argument("--out", default="dataset",
                    help="prefix of the discovery snapshot (same one used in step 1)")
    ap.add_argument("--fetch-limit", type=int, default=100000,
                    help="maximum number of ChatGPT conversations to fetch "
                         "(the default of the original script is 20, far too low)")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--cg-out", default="cg_dataset",
                    help="prefix of the Claude/Gemini output files (cg_dataset.jsonl etc.)")
    args = ap.parse_args()

    snapshot_path = os.path.join(ROOT, f"{args.out}_{args.since}_{args.until}.json")
    if not os.path.exists(snapshot_path):
        print(f"ERROR: discovery snapshot not found: {snapshot_path}")
        print("Run first: python pipeline/1_01_chat_discover_links.py")
        sys.exit(1)

    print("=== Fetch ChatGPT (Stage B, Playwright) ===")
    subprocess.run([
        sys.executable, os.path.join(SCRIPT_DIR, "1_01_chat_discover_links.py"),
        "--since", args.since, "--until", args.until,
        "--from-json", snapshot_path,
        "--fetch", "--fetch-limit", str(args.fetch_limit),
        "--concurrency", str(args.concurrency),
        "--out", args.out,
    ], cwd=ROOT, check=True)

    print("\n=== Fetch Claude + Gemini ===")
    cg_cmd = [
        sys.executable, os.path.join(SCRIPT_DIR, "lib_fetch_claude_gemini_impl.py"),
        "--from-json", snapshot_path,
        "--providers", "claude,gemini",
        "--per-block", "--by-language",
        "--out", args.cg_out,
    ]
    if args.fetch_limit < 100000:
        # the default (100000) is this script's "practically unlimited";
        # a lower explicit limit (e.g. for a quick test) must also be passed
        # here, otherwise Claude/Gemini ignore --fetch-limit altogether
        # and process ALL the links found (bug found empirically:
        # on a 20-link test it fetched 7,722 Claude+Gemini links, triggering
        # after ~800 requests a wall of errors on the Claude API side - no token
        # pool protects them from rate limiting as it does for GitHub).
        cg_cmd += ["--limit", str(args.fetch_limit)]
    subprocess.run(cg_cmd, cwd=ROOT, check=True)

    print("\nFetch completed for all 3 providers.")


if __name__ == "__main__":
    main()
