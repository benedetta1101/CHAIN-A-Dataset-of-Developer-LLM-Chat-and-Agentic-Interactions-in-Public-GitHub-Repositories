#!/usr/bin/env python
"""
Step 6 - Splitting the final dataset by language.

Standalone: no imports from other scripts of the project. Usage:
    python pipeline/1_06_chat_split_by_language.py                 # all languages
    python pipeline/1_06_chat_split_by_language.py --languages python javascript bash

Reads pipeline/out/snippet.csv (step 5, already with prompt_with_context) and
writes one CSV per language in pipeline/out/per_language/<language>.csv.
With --languages ONLY the files for the indicated languages are written (useful
to avoid regenerating 150+ files when only a subset is needed); without the
option, all the languages present in the dataset.
"""
import argparse
import csv
import os
from collections import defaultdict

csv.field_size_limit(50_000_000)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT_DIR = os.path.join(SCRIPT_DIR, "out")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--languages", dest="languages", metavar="LANG", nargs="+", default=None,
                         help="Subset of languages to extract (default: all)")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                         help="folder with snippet.csv and where to write per_language/")
    args = parser.parse_args()
    in_path = os.path.join(args.out_dir, "snippet.csv")
    per_language_dir = os.path.join(args.out_dir, "per_language")

    with open(in_path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    print(f"Total rows: {len(rows)}")

    per_lang = defaultdict(list)
    for r in rows:
        per_lang[r["language"] or "unknown"].append(r)

    selection = set(args.languages) if args.languages else set(per_lang.keys())
    missing = selection - set(per_lang.keys())
    if missing:
        print(f"Warning: requested languages absent from the dataset: {sorted(missing)}")

    os.makedirs(per_language_dir, exist_ok=True)
    written = 0
    for lang in sorted(selection & set(per_lang.keys())):
        out_path = os.path.join(per_language_dir, f"{lang}.csv")
        with open(out_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(per_lang[lang])
        written += 1

    print(f"\nFiles written: {written} in {per_language_dir}/")
    print(f"Languages available in the dataset: {len(per_lang)}")
    if not args.languages:
        print("\ntop 15 by number of rows:")
        for lang, r in sorted(per_lang.items(), key=lambda x: -len(x[1]))[:15]:
            print(f"  {lang}: {len(r)}")


if __name__ == "__main__":
    main()
