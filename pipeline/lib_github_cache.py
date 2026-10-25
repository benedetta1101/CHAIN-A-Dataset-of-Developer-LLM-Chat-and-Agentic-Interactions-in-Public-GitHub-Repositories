#!/usr/bin/env python
"""
Shared on-disk cache for the GitHub calls used by several steps of the
adoption chain (9_pr_commits_recheck, 2_04_chat_codefile_commits_recheck, and
the future step for hunk-level checking) - different steps
often download the SAME commit history or the SAME file content
for identical (repo, path, sha); without a persistent cache, each script
requests it again from scratch.

Usage:
    from lib_github_cache import DiskCache
    cache = DiskCache(os.path.join(args.out_dir, "github_cache.pkl"))
    ... (used like a normal dict, which is what the functions of
         2_01_chat_track_snippets.py/2_04_chat_codefile_commits_recheck.py expect) ...
    cache.save()               # at the end of the run
    cache.save_every(500, i)    # periodic save inside a long loop,
                                 # so a crash halfway does not lose everything
"""
import os
import pickle
import tempfile


class DiskCache(dict):
    def __init__(self, path):
        super().__init__()
        self.path = path
        if os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    self.update(pickle.load(f))
                print(f"  [lib_github_cache] loaded {len(self)} entries from {path}")
            except Exception as e:
                print(f"  [lib_github_cache] existing cache unreadable ({e}), starting empty")

    def save(self):
        d = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                pickle.dump(dict(self), f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, self.path)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def save_every(self, every_n, i):
        """Call inside a long loop (e.g. every row of step 10) - saves
        only every `every_n` iterations, so as not to burden a loop that already makes
        many network calls with a disk save at every step."""
        if i % every_n == 0:
            self.save()
