#!/usr/bin/env python3
"""
Step 1.09 (optional) - Recover the model label of Gemini shared conversations.

The Gemini share page prints, above the first turn, a line such as
    "Created with 2.5 Pro July 31, 2025 at 01:44 AM"
The Gemini parser of lib_fetch_claude_gemini_impl.py discarded it (model_slug was
fixed to None), so the released Gemini conversations have no model. This step
re-opens the Gemini links of the released conversations.jsonl with Playwright and
records the label ("2.5 Pro", "Flash", ...). It is a display name, not a technical
model identifier.

Result per link (`status`):
  ok              label found and parsed
  label_unparsed  a "Created with" line exists but did not match the expected pattern
  no_label        the conversation rendered (turns present) but there is no such line
  not_rendered    the page stayed on the landing screen / timed out (retried once)
  error           navigation failed

Read-only on the conversations file. Writes ONE new CSV; refuses to overwrite it
unless --resume is given (then only links not yet written are processed).

Usage:
    python3 1_09_chat_gemini_model_labels.py \\
        --conversations datasets/chat/conversations.jsonl \\
        --out chat_gemini_model_labels.csv [--limit 5] [--concurrency 6] [--resume]
"""
import argparse
import asyncio
import csv
import json
import os
import re
import sys

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
LABEL_RE = re.compile(rf"^Created with\s+(.+?)\s+((?:{MONTHS})\s+\d{{1,2}},\s+\d{{4}}.*)$", re.M)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
FIELDS = ["conversation_url", "model_label", "created_label_date", "status"]


async def read_label(page, url):
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_selector("share-turn-viewer, user-query", timeout=25000)
    except Exception:
        return "", "", "not_rendered"
    await page.wait_for_timeout(800)
    text = await page.evaluate("document.body.innerText.slice(0, 2000)")
    m = LABEL_RE.search(text)
    if m:
        return m.group(1).strip(), m.group(2).strip(), "ok"
    line = next((l for l in text.splitlines() if l.startswith("Created with")), "")
    if line:
        return line[:80], "", "label_unparsed"
    return "", "", "no_label"


async def worker(ctx, sem, url, out_rows):
    async with sem:
        result = ("", "", "error")
        for attempt in (1, 2):
            page = await ctx.new_page()
            try:
                result = await read_label(page, url)
            except Exception as e:
                result = ("", "", "error")
            finally:
                await page.close()
            if result[2] not in ("not_rendered", "error"):
                break
        out_rows.append({"conversation_url": url, "model_label": result[0],
                         "created_label_date": result[1], "status": result[2]})


async def run(urls, out, new_file, concurrency):
    from playwright.async_api import async_playwright
    counts, done = {}, 0
    sem = asyncio.Semaphore(concurrency)
    with open(out, "w" if new_file else "a", encoding="utf-8", newline="") as fout:
        w = csv.DictWriter(fout, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context(user_agent=UA, locale="en-US")
            batch = 60
            for i in range(0, len(urls), batch):
                rows = []
                await asyncio.gather(*(worker(ctx, sem, u, rows) for u in urls[i:i + batch]))
                for r in rows:
                    w.writerow(r)
                    counts[r["status"]] = counts.get(r["status"], 0) + 1
                fout.flush()
                done += len(rows)
                print(f"  [{done}/{len(urls)}] {counts}", flush=True)
            await browser.close()
    print(f"done: {counts}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conversations", required=True, help="chat conversations.jsonl")
    ap.add_argument("--out", required=True, help="NEW output CSV")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    if os.path.exists(args.out) and not args.resume:
        sys.exit(f"{args.out} already exists: not overwriting (use --resume to continue it)")
    done = set()
    if args.resume and os.path.exists(args.out):
        with open(args.out, encoding="utf-8", newline="") as f:
            done = {r["conversation_url"] for r in csv.DictReader(f)}
    urls = []
    with open(args.conversations, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if d["provider"] == "gemini" and d["conversation_url"] not in done:
                urls.append(d["conversation_url"])
    if args.limit:
        urls = urls[:args.limit]
    print(f"links to open: {len(urls)} (already done: {len(done)})")
    new_file = not (args.resume and os.path.exists(args.out))
    asyncio.run(run(urls, args.out, new_file, args.concurrency))


if __name__ == "__main__":
    main()
