#!/usr/bin/env python3
"""
============================================================================
 DevGPT EXTENSION — full collection pipeline
============================================================================
Rebuilds, for 2024-2026, the kind of dataset DevGPT (Xiao et al., MSR'24)
collected for 2023: shared ChatGPT conversations referenced on GitHub,
linked to the artifact (issue / PR / commit / code file).

PIPELINE
  Stage A  Discover artifacts that reference a share link  (GitHub)
  Stage A* Pull artifact comments to catch links in replies (optional)
  Stage B  Fetch each conversation from chatgpt.com         (Playwright)
  Post     Normalize to a DevGPT-like schema, count tokens, write a dated
           snapshot, track link decay / duplicates.

AUTH (why you want a token)
  Unauthenticated : 60 core/hr, 10 search/min, NO code search.
  With GITHUB_TOKEN: 5000 core/hr, 30 search/min, code search ENABLED.
  A classic/fine-grained PAT with public read scope is enough.
    export GITHUB_TOKEN=ghp_xxx
  For a *very* large pull, prefer GH Archive on BigQuery for issues/PRs/
  comments (no 1,000-result ceiling, no rate limit); see notes at bottom.

USAGE
  export GITHUB_TOKEN=...                 # optional but strongly recommended
  python devgpt_extend.py --since 2024-01-01 --until 2024-03-31 \
         --sources issues prs code --comments --out snapshot

DEPENDENCIES
  Stage A : standard library only.
  Stage B : pip install playwright && playwright install chromium
  Tokens  : pip install tiktoken   (falls back to an estimate if absent)
============================================================================
"""

import os, re, sys, json, time, math, argparse, datetime as dt
import urllib.parse, urllib.request, urllib.error
import http.client
from collections import Counter, OrderedDict

# The Playwright Node driver runs as a single long-lived process for
# the whole Stage B (tens of thousands of pages) and its default V8 heap
# (~4GB) blew up in the middle of a real batch (FATAL ERROR:
# JavaScript heap out of memory around link ~9275/31658), making EVERY
# subsequent link fail with "Connection closed while reading from the driver"
# (not a provider-side block - the driver had simply died). setdefault so that
# it does not overwrite a NODE_OPTIONS already set by the user. It must come
# before any async_playwright()/sync_playwright(), which spawns the driver
# inheriting the environment of that moment.
os.environ.setdefault("NODE_OPTIONS", "--max-old-space-size=8192")

GITHUB_API = "https://api.github.com"

# Old + current share-link domains; share id is a UUID-like hex string.
# Share-link domains across providers. ChatGPT is fully supported in Stage B;
# Claude/Gemini extractors are provisional (their page structure must be tuned
# on a few real links — see fetch_conversation_async).
SHARE_RE = re.compile(
    r"https?://(?:"
    r"chatgpt\.com/share|chat\.openai\.com/share"          # ChatGPT
    r"|claude\.ai/share"                                     # Claude
    r"|gemini\.google\.com/share|g\.co/gemini/share"         # Gemini
    r")/[0-9a-zA-Z._-]+", re.I)


def provider_of(url):
    u = url.lower()
    if "claude.ai/share" in u:
        return "claude"
    if "gemini.google.com/share" in u or "g.co/gemini/share" in u:
        return "gemini"
    return "chatgpt"

BASE_HEADERS = {"Accept": "application/vnd.github+json",
                "User-Agent": "devgpt-extend/1.0",
                "X-GitHub-Api-Version": "2022-11-28"}


# --------------------------------------------------------------------------
# Token pool — rotate across N tokens to multiply the rate limit.
# NEVER hardcode tokens in source (they get committed and auto-revoked).
# Provide them via either:
#   export GITHUB_TOKENS="tok1,tok2,...,tok8"      (env, comma/space/newline)
#   or a file  tokens.txt  (one per line; ADD IT TO .gitignore)
# With 8 tokens you get ~8x throughput (the pool fails over automatically).
# --------------------------------------------------------------------------
def load_tokens():
    raw = os.environ.get("GITHUB_TOKENS") or os.environ.get("GITHUB_TOKEN") or ""
    toks = [t.strip() for t in re.split(r"[,\s]+", raw) if t.strip()]
    # Look for tokens.txt in the current dir AND next to this script.
    here = os.path.dirname(os.path.abspath(__file__))
    for path in ("tokens.txt", os.path.join(here, "tokens.txt")):
        if os.path.exists(path):
            with open(path) as f:
                toks += [l.strip() for l in f
                         if l.strip() and not l.lstrip().startswith("#")]
            break
    seen, out = set(), []
    for t in toks:
        if t not in seen:
            seen.add(t); out.append(t)
    return out


class TokenPool:
    def __init__(self, tokens):
        self.tokens = tokens or [None]      # [None] => unauthenticated
        self.i = 0
        self.blocked_until = {}             # token -> epoch when it frees up

    def has_auth(self):
        return any(t for t in self.tokens)

    def current(self):
        return self.tokens[self.i]

    def headers(self):
        h = dict(BASE_HEADERS)
        tok = self.current()
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        return h 

    def mark_blocked(self, until_epoch):
        self.blocked_until[self.current()] = until_epoch

    def next_available(self):
        """Rotate to a token whose cooldown has passed; return (token, wait).
        wait>0 means all tokens are cooling down and you must sleep `wait`s."""
        now = time.time()
        for _ in range(len(self.tokens)):
            self.i = (self.i + 1) % len(self.tokens)
            if self.blocked_until.get(self.current(), 0) <= now:
                return self.current(), 0
        # everyone is blocked -> shortest wait
        soonest = min(self.blocked_until.values())
        return self.current(), max(0, soonest - now) + 2


POOL = TokenPool(load_tokens())

SEARCH_CAP = 1000          # GitHub hard cap on retrievable search results
PER_PAGE = 100


# --------------------------------------------------------------------------
# HTTP layer with rate-limit awareness
# --------------------------------------------------------------------------
def _safe_url(url):
    """Percent-encode spaces / unsafe chars in the path & query so urllib does
    not reject URLs like '.../Episode 07 - Finding the Path/...'. Leaves already
    -encoded %XX and structural chars intact (no double-encoding)."""
    sp = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(sp.path, safe="/%:@")
    query = urllib.parse.quote(sp.query, safe="=&%:@/?+")
    return urllib.parse.urlunsplit((sp.scheme, sp.netloc, path, query, sp.fragment))


def _request(url):
    url = _safe_url(url)
    for attempt in range(8):
        req = urllib.request.Request(url, headers=POOL.headers())
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                body = json.load(r)
                rem = r.headers.get("X-RateLimit-Remaining")
                if rem is not None and int(rem) <= 1:
                    reset = int(r.headers.get("X-RateLimit-Reset", "0"))
                    # this token is dry: park it and switch to another one
                    POOL.mark_blocked(reset)
                    _, wait = POOL.next_available()
                    if wait > 0:
                        print(f"    [pool] all tokens cooling down, sleeping {wait:.0f}s")
                        time.sleep(wait)
                    else:
                        print(f"    [pool] token dry, rotating to #{POOL.i}")
                return body
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                reset = e.headers.get("X-RateLimit-Reset")
                retry_after = e.headers.get("Retry-After")
                if reset:
                    POOL.mark_blocked(int(reset))
                elif retry_after:
                    POOL.mark_blocked(time.time() + int(retry_after))
                else:
                    POOL.mark_blocked(time.time() + 30 * (attempt + 1))
                tok, wait = POOL.next_available()
                if wait > 0:
                    print(f"    [pool] {e.code}; all tokens limited, sleeping {wait:.0f}s")
                    time.sleep(wait)
                else:
                    print(f"    [pool] {e.code}; rotating to token #{POOL.i}")
                continue
            if e.code == 422:   # validation (e.g. code search w/o auth)
                raise
            print(f"    [http] {e.code} {e.reason}; retry in 5s")
            time.sleep(5)
        except (urllib.error.URLError, TimeoutError,
                http.client.HTTPException, ConnectionError,
                json.JSONDecodeError) as e:
            # IncompleteRead (connection truncated mid-response) and the like
            # are not URLError - without this except, a single network hiccup
            # on a deep windowed search used to crash the whole
            # discovery instead of just retrying (bug found live
            # on a real run, connection truncated during "pull requests")
            print(f"    [net] {type(e).__name__}: {e}; retry in 5s")
            time.sleep(5)
    raise RuntimeError(f"giving up on {url}")


def gh(path, params):
    return _request(f"{GITHUB_API}{path}?{urllib.parse.urlencode(params)}")


# --------------------------------------------------------------------------
# Search with automatic date-window bisection (beats the 1,000 cap)
# --------------------------------------------------------------------------
def _search_issues_page(q, page):
    return gh("/search/issues", {"q": q, "per_page": PER_PAGE, "page": page,
                                 "sort": "created", "order": "asc"})


def search_windowed(base_q, since, until, depth=0):
    """Recursively bisect [since, until] until each window has <= 1000 hits,
    then page through it fully. `since`/`until` are date objects."""
    window = f"created:{since.isoformat()}..{until.isoformat()}"
    q = f"{base_q} {window}"
    first = _search_issues_page(q, 1)
    total = first.get("total_count", 0)
    pad = "  " * depth
    print(f"{pad}window {since}..{until}: {total} hits")
    if total == 0:
        return []
    if total > SEARCH_CAP and since < until:
        mid = since + (until - since) / 2
        mid = dt.date.fromordinal(int(mid.toordinal()))
        if mid <= since:
            print(f"{pad}  ! single day > {SEARCH_CAP}; truncating to first 1000")
        else:
            left = search_windowed(base_q, since, mid, depth + 1)
            right = search_windowed(base_q, mid + dt.timedelta(days=1), until, depth + 1)
            return left + right
    items = list(first.get("items", []))
    n_pages = min(math.ceil(min(total, SEARCH_CAP) / PER_PAGE), 10)
    for p in range(2, n_pages + 1):
        items.extend(_search_issues_page(q, p).get("items", []))
    return items


# --------------------------------------------------------------------------
# Stage A — collectors
# --------------------------------------------------------------------------
def _artifact_from_issue(it):
    title = it.get("title", "") or ""
    body = it.get("body", "") or ""
    text = f"{title}\n{body}"
    links = SHARE_RE.findall(text)
    return {
        "source": "GitHubPullRequest" if it.get("pull_request") else "GitHubIssue",
        "repo": it.get("repository_url", "").split("/repos/")[-1],
        "artifact_url": it.get("html_url"),
        "artifact_title": title,
        "artifact_text": body,            # e.g. the issue / PR body
        "author": (it.get("user") or {}).get("login"),
        "created_at": it.get("created_at"),
        "comments_url": (it.get("comments_url")
                         or (it.get("pull_request") or {}).get("url")),
        "share_links": sorted(set(links)),
        "links_in_body": len(links),
    }


SHARE_DOMAINS = ("chatgpt.com/share", "chat.openai.com/share",
                 "claude.ai/share", "gemini.google.com/share", "g.co/gemini/share")


def collect_issues(since, until):
    print("[A] issues …")
    out = []
    for dom in SHARE_DOMAINS:
        out += [_artifact_from_issue(i)
                for i in search_windowed(f'"{dom}" type:issue', since, until)]
    return out


def collect_prs(since, until):
    print("[A] pull requests …")
    out = []
    for dom in SHARE_DOMAINS:
        out += [_artifact_from_issue(i)
                for i in search_windowed(f'"{dom}" type:pr', since, until)]
    return out


def _search_code_page(q, page):
    return gh("/search/code", {"q": q, "per_page": PER_PAGE, "page": page})


def search_code_windowed(base_q, size_min=0, size_max=5_000_000, depth=0):
    """Like search_windowed(), but bisecting on size: instead of created:
    (code search does not support created:, but it supports size: - same
    technique, same reason: get past the 1000-result cap per query).
    Without this, beyond the first 1000 hits of a domain+language pair
    the remaining results are simply unreachable via the API, no matter
    how many times the query is re-run."""
    window = f"size:{size_min}..{size_max}"
    q = f"{base_q} {window}"
    try:
        first = _search_code_page(q, 1)
    except urllib.error.HTTPError as e:
        print(f"    {'  '*depth}size {size_min}..{size_max} failed: {e.code}")
        return []
    total = first.get("total_count", 0)
    pad = "  " * depth
    print(f"    {pad}size {size_min}..{size_max}: {total} hits")
    if total == 0:
        return []
    if total > SEARCH_CAP and size_max > size_min:
        mid = (size_min + size_max) // 2
        if mid <= size_min:
            print(f"    {pad}  ! window of 1 byte beyond {SEARCH_CAP}; truncated to the first 1000")
        else:
            left = search_code_windowed(base_q, size_min, mid, depth + 1)
            right = search_code_windowed(base_q, mid + 1, size_max, depth + 1)
            return left + right
    items = list(first.get("items", []))
    n_pages = min(math.ceil(min(total, SEARCH_CAP) / PER_PAGE), 10)
    for p in range(2, n_pages + 1):
        items.extend(_search_code_page(q, p).get("items", []))
    return items


def collect_code():
    """Code search: links committed into source files. Requires a token.

    MODIFIED with respect to the original (extraction_pairs_single_snippet.py)
    in two ways:
      1. it also searches claude.ai/share and gemini.google.com/share, not only
         chatgpt.com/share (issues/prs already looped over SHARE_DOMAINS
         in the original, only collect_code() was chatgpt-only) - 3x the queries
      2. it uses search_code_windowed() (bisection on size:) instead of simply
         taking the first 1000 results per domain+language pair -
         it captures results beyond the cap that would otherwise be lost
         forever, no matter how many times the search is re-run.
         More API calls when a window exceeds 1000 hits (each
         bisection costs one extra probe query).
    """
    if not POOL.has_auth():
        print("[A] code search SKIPPED (needs a token in GITHUB_TOKENS / tokens.txt)")
        return []
    print("[A] code files …")
    out = []
    domains = ("chatgpt.com/share", "claude.ai/share", "gemini.google.com/share")
    for domain in domains:
        for lang in ("python", "javascript", "typescript", "markdown", "shell"):
            q = f'"{domain}" language:{lang}'
            items = search_code_windowed(q)
            print(f"    code[{domain}][{lang}]: {len(items)} total hits (after bisection)")
            for it in items:
                out.append({
                    "source": "GitHubCodeFile",
                    "repo": (it.get("repository") or {}).get("full_name"),
                    "artifact_url": it.get("html_url"),
                    "artifact_title": it.get("path"),
                    "artifact_text": None,      # filled with file content by enrich
                    "author": None,
                    "created_at": None,
                    "raw_url": it.get("url"),   # contents API; fetch to extract links
                    "share_links": [],          # filled in by enrich_code_links()
                    "links_in_body": 0,
                })
    return out


def enrich_code_links(code_artifacts, limit=None):
    """For code hits, fetch the file and pull out the share links."""
    n = 0
    for a in code_artifacts:
        if limit and n >= limit:
            break
        try:
            meta = _request(a["raw_url"])
            import base64
            content = base64.b64decode(meta.get("content", "")).decode("utf-8", "ignore")
            a["share_links"] = sorted(set(SHARE_RE.findall(content)))
            a["links_in_body"] = len(a["share_links"])
            n += 1
        except Exception as e:
            print(f"    code enrich failed for {a['artifact_url']}: {e}")
    return code_artifacts


def enrich_comments(artifacts, limit=None):
    """Many links live in comments/replies, not the body. Fetch comments and
    add any share links found there. Costs ~1 core request per artifact."""
    print("[A*] fetching comments for links in replies …")
    n = 0
    for a in artifacts:
        if not a.get("comments_url"):
            continue
        if limit and n >= limit:
            break
        try:
            comments = _request(a["comments_url"])
            extra = set()
            for c in comments if isinstance(comments, list) else []:
                extra.update(SHARE_RE.findall(c.get("body", "") or ""))
            if extra:
                a["share_links"] = sorted(set(a["share_links"]) | extra)
            n += 1
        except Exception as e:
            print(f"    comment fetch failed for {a['artifact_url']}: {e}")
    return artifacts


# --------------------------------------------------------------------------
# Stage B — fetch the conversation (run on your own machine)
# --------------------------------------------------------------------------
def _turns_from_data(data):
    """Build conversation turns in correct order.

    `mapping` is the FULL conversation tree, not the linear thread the user
    saw: every edited message and every regenerated response is its own
    sibling node, still present in `mapping.values()`. Iterating all of them
    (the old approach) mixes abandoned branches into the output, and even
    sorting by create_time does not fix it - a regenerated assistant reply
    can carry a create_time earlier than the user message that triggered it
    (observed directly: verified against a live chatgpt.com share page,
    conversation 68d43c9d-4d60-8006-a1a7-14ae49475a5a, where the reply to
    "can you make only the necessary changes to both login and register
    pages..." has a create_time ~0.07s BEFORE that user message's own
    create_time). The only reliable order is the actual thread the share
    page renders: walk `current_node` back to the root via `parent`
    pointers, then reverse.
    """
    mapping = data.get("mapping", {}) or {}
    current_node = data.get("current_node")

    def _walk_active_branch():
        node_id = current_node
        chain = []
        seen = set()
        while node_id and node_id in mapping and node_id not in seen:
            seen.add(node_id)
            chain.append(node_id)
            node_id = mapping[node_id].get("parent")
        chain.reverse()
        rows = []
        for nid in chain:
            msg = mapping[nid].get("message")
            if not msg:
                continue
            role = (msg.get("author") or {}).get("role")
            if role not in ("user", "assistant"):
                continue
            parts = (msg.get("content") or {}).get("parts") or []
            text = "\n".join(p for p in parts if isinstance(p, str))
            if text:
                rows.append((role, text))
        return rows

    if current_node:
        rows = _walk_active_branch()
        if rows:
            return [{"role": r, "text": t} for r, t in rows]

    # Fallback (no current_node, or the walk produced nothing - e.g. a share
    # page shape we haven't seen): flatten every node and sort by
    # create_time. Known to occasionally mis-order branched conversations
    # (see docstring) - better than nothing when the tree walk is unusable.
    rows = []
    for node in mapping.values():
        msg = node.get("message")
        if not msg:
            continue
        role = (msg.get("author") or {}).get("role")
        if role not in ("user", "assistant"):
            continue
        parts = (msg.get("content") or {}).get("parts") or []
        text = "\n".join(p for p in parts if isinstance(p, str))
        if text:
            rows.append((msg.get("create_time") or 0, role, text))
    if all(ct for ct, _, _ in rows):          # all have timestamps -> sort
        rows.sort(key=lambda r: r[0])
    return [{"role": r, "text": t} for _, r, t in rows]


# Per-provider extraction. Each entry: (url-fixup, JS that returns the data blob
# or null, parser(blob)->(turns, model)). ChatGPT is verified; Claude & Gemini
# are PROVISIONAL scaffolds: the JS path/structure must be confirmed on a few
# real links (run --fetch on ~10 of each and inspect), because their pages were
# not testable here. The generic DOM fallback keeps them from crashing.
_CHATGPT_JS = ("() => (window.__reactRouterDataRouter?.state?.loaderData?."
               "['routes/share.$shareId.($action)']?.serverResponse?.data) ?? null")
# Generic: grab any large embedded JSON (__NEXT_DATA__ etc.) — best-effort.
_GENERIC_JS = ("() => { const s=document.getElementById('__NEXT_DATA__');"
               " if(s) { try { return JSON.parse(s.textContent); } catch(e){} }"
               " return null; }")


def _parse_chatgpt(blob):
    return _turns_from_data(blob), (blob.get("model_slug") or blob.get("default_model_slug"))


def _parse_generic(blob):
    # Placeholder parser for Claude/Gemini __NEXT_DATA__-style blobs.
    # Tune the path here once you've inspected a real shared page.
    return [], None


PROVIDERS = {
    "chatgpt": {"fix": lambda u: u.replace("chat.openai.com", "chatgpt.com"),
                "js": _CHATGPT_JS, "parse": _parse_chatgpt},
    "claude":  {"fix": lambda u: u, "js": _GENERIC_JS, "parse": _parse_generic},
    "gemini":  {"fix": lambda u: u, "js": _GENERIC_JS, "parse": _parse_generic},
}

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BLOB_JS = _CHATGPT_JS   # kept for backward compatibility


def _dom_text_fallback(page):
    """Last resort: read visible conversation text if no JSON blob was found."""
    try:
        txt = page.evaluate("() => document.body.innerText") or ""
    except Exception:
        return []
    txt = txt.strip()
    return [{"role": "unknown", "text": txt}] if len(txt) > 40 else []


def _build_result(share_url, status, turns, model):
    if not turns:
        return {"share_url": share_url, "status": status if status != "ok" else "unavailable"}
    return {"share_url": share_url, "status": "ok", "model_slug": model,
            "n_turns": len(turns), "turns": turns}


def fetch_conversation(share_url, timeout=45000):
    """Synchronous single-link fetch (kept for simple/debug use). Prefer the
    concurrent async path (fetch_conversations) for batches — it is far faster."""
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import TimeoutError as PWTimeout
    prov = PROVIDERS[provider_of(share_url)]
    url = prov["fix"](share_url)
    status, blob = "error", None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=_UA)
            ctx.route("**/*", lambda r: (
                r.abort() if r.request.resource_type in ("image", "media", "font")
                else r.continue_()))
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                try:
                    page.wait_for_function(prov["js"], timeout=timeout)
                except PWTimeout:
                    pass
                blob = page.evaluate(prov["js"])
                if blob:
                    turns, model = prov["parse"](blob)
                    status = "ok"
                else:
                    turns, model = _dom_text_fallback(page), None
                    status = "ok" if turns else "unavailable"
            finally:
                browser.close()
    except Exception as e:
        return {"share_url": share_url, "status": "error", "error": str(e)[:200]}
    return _build_result(share_url, status, turns, model)


# --------------------------------------------------------------------------
# FAST Stage B: one browser, N pages in parallel (async).
# --------------------------------------------------------------------------
async def _fetch_one(ctx, share_url, timeout, sem):
    from playwright.async_api import TimeoutError as PWTimeout
    prov = PROVIDERS[provider_of(share_url)]
    url = prov["fix"](share_url)
    async with sem:
        page = None
        try:
            # page = await ctx.new_page() used to be outside this try - if the browser
            # crashes in the middle of a huge batch (plausible over tens of thousands
            # of pages with a single long-lived browser instance), this call
            # raises an uncaught exception that bubbles up to
            # _fetch_all_async and loses the WHOLE `results` dict accumulated so far
            # (found after a real run: fetch_stats recorded
            # 8,377 "ok" but the final file had 0 usable conversations,
            # all "error" without a message - the cause was right here).
            page = await ctx.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            try:
                await page.wait_for_function(prov["js"], timeout=timeout)
            except PWTimeout:
                pass
            blob = await page.evaluate(prov["js"])
            if blob:
                turns, model = prov["parse"](blob)
                return _build_result(share_url, "ok", turns, model)
            txt = ""
            try:
                txt = (await page.evaluate("() => document.body.innerText") or "").strip()
            except Exception:
                pass
            turns = [{"role": "unknown", "text": txt}] if len(txt) > 40 else []
            return _build_result(share_url, "timeout", turns, None)
        except Exception as e:
            return {"share_url": share_url, "status": "error", "error": str(e)[:200]}
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass


async def _fetch_all_async(urls, concurrency, timeout, on_done=None):
    from playwright.async_api import async_playwright
    import asyncio
    results = {}
    sem = asyncio.Semaphore(concurrency)
    # The whole body is wrapped in a try/except: _fetch_one now catches its
    # own per-page errors (see the comment there), but this is a second
    # layer of defence against ANY unexpected exception (e.g. the
    # browser itself dying, async_playwright failing on close) -
    # it still returns what has already been accumulated in `results`
    # instead of losing it all, as used to happen (see README/step 1).
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context(user_agent=_UA)

            async def _route(route):
                if route.request.resource_type in ("image", "media", "font"):
                    await route.abort()
                else:
                    await route.continue_()
            await ctx.route("**/*", _route)
            tasks = [asyncio.ensure_future(_fetch_one(ctx, u, timeout, sem)) for u in urls]
            done = 0
            for fut in asyncio.as_completed(tasks):
                try:
                    res = await fut
                except Exception as e:
                    done += 1
                    print(f"    ! task failed with no result ({e}), continuing")
                    continue
                results[res["share_url"]] = res
                done += 1
                if on_done:
                    on_done(done, len(urls), res)
            await browser.close()
    except Exception as e:
        print(f"  ! fetch batch interrupted ({e}) - returning the {len(results)} results already obtained "
              f"instead of losing them all")
    return results


def fetch_conversations(urls, concurrency=8, timeout=30000, on_done=None):
    """Fetch many share links concurrently with ONE browser. Returns
    {share_url: result}. Much faster than calling fetch_conversation per link."""
    import asyncio
    return asyncio.run(_fetch_all_async(list(urls), concurrency, timeout, on_done))


# --------------------------------------------------------------------------
# Tokens (tiktoken if available, else estimate) + code extraction
# --------------------------------------------------------------------------
_ENC = None
def count_tokens(text):
    global _ENC
    if _ENC is None:
        try:
            import tiktoken
            _ENC = tiktoken.get_encoding("o200k_base")  # gpt-4o/4.1 family
        except Exception:
            _ENC = False
    if _ENC:
        # disallowed_special=(): real text may contain literal special tokens
        # such as "<|endoftext|>" (e.g. pasted by a user, or
        # generated as an example) - without this tiktoken raises ValueError
        # and interrupts the whole normalize() mid-batch, losing hours of
        # fetching already completed (real bug, found after a crash at the end of the
        # fetch of 29,087 conversations).
        return len(_ENC.encode(text, disallowed_special=()))
    return max(1, round(len(text) / 4))   # ~4 chars/token fallback

# Capture both the language tag (group 1, may be empty) and the body (group 2).
CODE_FENCE = re.compile(r"```([a-zA-Z0-9_+#.\-]*)\n(.*?)```", re.S)

# Normalize common fence tags / detector names to a consistent label.
_LANG_ALIAS = {
    "py": "python", "py3": "python", "python3": "python",
    "js": "javascript", "node": "javascript", "jsx": "javascript",
    "ts": "typescript", "tsx": "typescript",
    "sh": "bash", "shell": "bash", "zsh": "bash", "console": "bash",
    "yml": "yaml", "c++": "cpp", "cs": "csharp", "rb": "ruby",
    "kt": "kotlin", "md": "markdown", "rs": "rust", "golang": "go",
    "plaintext": "text", "": None,
}
def _norm_lang(lang):
    if not lang:
        return None
    return _LANG_ALIAS.get(lang.strip().lower(), lang.strip().lower())


_PYGMENTS = None
# Languages we accept from the pygments fallback (avoids absurd guesses like
# "teratermmacro" on short snippets).
_COMMON = {"python", "javascript", "typescript", "java", "c", "cpp", "csharp",
           "go", "ruby", "php", "rust", "bash", "sql", "html", "css", "json",
           "yaml", "xml", "kotlin", "swift", "scala", "r", "perl", "lua",
           "dart", "objective-c", "matlab", "markdown", "dockerfile", "text"}

# Conservative regex heuristics for the most common languages (checked in order).
_HEUR = [
    ("php",        re.compile(r"<\?php")),
    ("html",       re.compile(r"<(?:!doctype|html|head|body|div|span|p|a|script)\b", re.I)),
    ("sql",        re.compile(r"\bselect\b[\s\S]+?\bfrom\b|\binsert\s+into\b|\bcreate\s+table\b|\bupdate\b[\s\S]+?\bset\b|\bdelete\s+from\b", re.I)),
    ("python",     re.compile(r"(?m)^\s*(?:def |class |from \S+ import|@\w)|\bself\b|print\(|\belif\b|__name__")),
    ("rust",       re.compile(r"\bfn \w+|let mut |println!|::<")),
    ("go",         re.compile(r"\bpackage \w+|\bfunc \w+\(|fmt\.\w+")),
    ("typescript", re.compile(r"\binterface \w+|\: (?:string|number|boolean)\b|\bimport .* from ")),
    ("javascript", re.compile(r"\b(?:function|const|let|var)\b.*|=>|console\.log|document\.")),
    ("java",       re.compile(r"\b(?:public|private|protected)\s+(?:static\s+)?(?:class|void|int|String)\b|System\.out\.print")),
    ("cpp",        re.compile(r"#include\s*<[^>]+>.*(?:std::|cout|cin)|using namespace std", re.S)),
    ("c",          re.compile(r"#include\s*<[^>]+>|int\s+main\s*\(")),
    ("csharp",     re.compile(r"using System|Console\.Write|namespace \w+")),
    ("ruby",       re.compile(r"(?m)^\s*(?:def |end$|require )|puts |\.each do")),
    ("bash",       re.compile(r"(?m)^#!/.*sh\b|\b(?:echo|apt-get|sudo|chmod|export)\b")),
    ("yaml",       re.compile(r"(?m)^\s*[\w.-]+:\s+\S")),
]


def _heuristic_lang(code):
    s = code.strip()
    # JSON: parses cleanly as JSON object/array
    if s[:1] in "{[":
        try:
            json.loads(s); return "json"
        except Exception:
            pass
    for name, rx in _HEUR:
        if rx.search(code):
            return name
    return None


def detect_language(code):
    """Best-effort language detection. Hybrid: regex heuristics for common
    languages first, then pygments (whitelisted) as a fallback. Returns a short
    name or None. pygments is optional:  pip install pygments"""
    if not code or len(code.strip()) < 12:
        return None
    lang = _heuristic_lang(code)
    if lang:
        return lang
    global _PYGMENTS
    if _PYGMENTS is None:
        try:
            from pygments.lexers import guess_lexer
            _PYGMENTS = guess_lexer
        except Exception:
            _PYGMENTS = False
    if not _PYGMENTS:
        return None
    try:
        lexer = _PYGMENTS(code)
        name = _norm_lang(lexer.aliases[0] if lexer.aliases else lexer.name)
        return name if name in _COMMON else None   # reject odd guesses
    except Exception:
        return None


def extract_code_blocks(turns):
    """Code blocks from assistant turns. For each block:
      language          -> best guess (declared tag, else detected)
      language_declared -> the tag written in the ``` fence (may be None)
      language_detected -> pygments/heuristic guess from the code (may be None)
      prompt            -> the user message right before this answer
                           (the "intermediate prompt" that produced the snippet)
      code              -> the snippet text (no surrounding prose)
    Requires turns in conversation order (they are, after fetch)."""
    blocks = []
    last_user = None
    for t in turns:
        if t["role"] == "user":
            last_user = t["text"]
        elif t["role"] == "assistant":
            for lang, body in CODE_FENCE.findall(t["text"]):
                declared = _norm_lang(lang)
                detected = detect_language(body)
                blocks.append({
                    "language": declared or detected,
                    "language_declared": declared,
                    "language_detected": detected,
                    "prompt": last_user,
                    "code": body,
                })
    return blocks


def extract_code(turns):
    """Just the code strings (kept for backward compatibility)."""
    return [b["code"] for b in extract_code_blocks(turns)]


def normalize(conv):
    """Turn a fetched conversation into DevGPT-compatible fields."""
    if conv.get("status") != "ok":
        return conv
    turns = conv["turns"]
    prompts = [t["text"] for t in turns if t["role"] == "user"]
    answers = [t["text"] for t in turns if t["role"] == "assistant"]
    code_blocks = extract_code_blocks(turns)
    code = [b["code"] for b in code_blocks]
    n_msgs_with_code = sum(1 for t in turns if CODE_FENCE.search(t["text"]))
    languages = sorted({b["language"] for b in code_blocks if b["language"]})
    return {
        **conv,
        "is_code_generation": bool(code_blocks),
        # --- message counts ---
        "n_user_messages": len(prompts),
        "n_assistant_messages": len(answers),
        "n_messages_with_code": n_msgs_with_code,
        "n_code_snippets": len(code_blocks),
        # --- first / last messages ---
        "first_user_message": prompts[0] if prompts else None,
        "first_assistant_message": answers[0] if answers else None,
        "last_user_message": prompts[-1] if prompts else None,
        "last_assistant_message": answers[-1] if answers else None,
        # --- tokens ---
        "prompt_tokens": sum(count_tokens(p) for p in prompts),
        "answer_tokens": sum(count_tokens(a) for a in answers),
        # --- code (snippet + language) ---
        "code_blocks": code_blocks,        # [{"language": "...", "code": "..."}]
        "code_languages": languages,       # distinct languages used
        "code_snippets": code,             # just the code strings (compat)
    }


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------
def dedup(artifacts):
    """Merge by share link; track which artifacts reference each link."""
    by_link = {}
    for a in artifacts:
        for link in a["share_links"]:
            rec = by_link.setdefault(link, {"share_url": link, "referenced_by": []})
            rec["referenced_by"].append(
                {"source": a["source"], "repo": a["repo"],
                 "artifact_url": a["artifact_url"],
                 "artifact_title": a.get("artifact_title"),
                 "artifact_text": a.get("artifact_text"),
                 "author": a.get("author"),
                 "created_at": a["created_at"]})
    return by_link


# Columns requested for the flat table.
CSV_COLUMNS = [
    "share_url", "repo", "artifact_url", "artifact_text",
    "n_user_messages", "n_assistant_messages", "n_messages_with_code",
    "n_code_snippets", "code_languages",
    "first_user_message", "first_assistant_message",
    "last_user_message", "last_assistant_message",
    "model_slug", "status",
]


def flatten_rows(links):
    """One flat dict per share link (mirrors CSV_COLUMNS). Used for CSV and
    for the flat-JSON export."""
    rows = []
    for rec in links.values():
        ref = (rec.get("referenced_by") or [{}])[0]
        conv = rec.get("conversation") or {}
        rows.append({
            "share_url": rec.get("share_url"),
            "repo": ref.get("repo"),
            "artifact_url": ref.get("artifact_url"),
            "artifact_text": ref.get("artifact_text"),
            "n_user_messages": conv.get("n_user_messages"),
            "n_assistant_messages": conv.get("n_assistant_messages"),
            "n_messages_with_code": conv.get("n_messages_with_code"),
            "n_code_snippets": conv.get("n_code_snippets"),
            "code_languages": ", ".join(conv.get("code_languages") or []),
            "first_user_message": conv.get("first_user_message"),
            "first_assistant_message": conv.get("first_assistant_message"),
            "last_user_message": conv.get("last_user_message"),
            "last_assistant_message": conv.get("last_assistant_message"),
            "model_slug": conv.get("model_slug"),
            "status": conv.get("status", "not_fetched"),
        })
    return rows


def write_csv(links, path):
    """Flat CSV table (one row per share link)."""
    import csv
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for row in flatten_rows(links):
            w.writerow(row)


def write_flat_json(links, path):
    """Same flat records as the CSV, but as JSON (one object per share link)."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(flatten_rows(links), f, indent=2, ensure_ascii=False)


def write_codegen_jsonl(links, path):
    """One line per CODE-GENERATION conversation (full enriched record).
    Only includes successfully fetched conversations classified code_generation."""
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for rec in links.values():
            conv = rec.get("conversation") or {}
            if conv.get("status") == "ok" and conv.get("is_code_generation"):
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
    return n


# ==========================================================================
# RICH PER-CONVERSATION DATASET  (+ per-language TEST mode)
# Reuses Stage A/B above. Adds: snippet-only fields, English-only filter,
# code-validity check (rejects diagrams/prose in a fence), snippet provenance
# in repo files, user profile/type. Discovery here has NO date limits.
# ==========================================================================

# ---- code-validity: is a fenced block really source code? -----------------
_PYG_LEX = {}
def _code_density(code, declared=None):
    """(codey_ratio, err_ratio, n) via the declared language's lexer."""
    try:
        from pygments.lexers import get_lexer_by_name, guess_lexer
        from pygments.token import Token
    except Exception:
        return None
    lex = None
    if declared:
        if declared not in _PYG_LEX:
            try: _PYG_LEX[declared] = get_lexer_by_name(declared)
            except Exception: _PYG_LEX[declared] = False
        lex = _PYG_LEX[declared] or None
    if lex is None:
        try: lex = guess_lexer(code)
        except Exception: return (0.0, 1.0, 0)
    toks = [(tt, v) for tt, v in lex.get_tokens(code) if v.strip()]
    n = len(toks)
    if n == 0:
        return (0.0, 0.0, 0)
    codey = sum(1 for tt, _ in toks if tt in Token.Operator or tt in Token.Punctuation
                or tt in Token.Keyword or tt in Token.Literal.Number
                or tt in Token.Name.Function or tt in Token.Name.Class
                or tt in Token.Name.Tag or tt in Token.Name.Builtin
                or tt in Token.Literal.String)
    err = sum(1 for tt, _ in toks if tt in Token.Error)
    return (codey / n, err / n, n)


def _looks_like_code_regex(code):
    s = (code or "").strip()
    if not s: return False
    has_punct = bool(re.search(r"[{}()\[\];=]|::|=>|->|#include", s))
    lines = [l for l in s.splitlines() if l.strip()]
    arrows = len(re.findall(r"[\u2190-\u21FF\u2794\u27A1\u2022]|-->|==>", s))
    caps = sum(1 for l in lines if l.strip().isupper() and len(l.strip()) > 3)
    if arrows >= 2 or caps >= 2: return has_punct
    if has_punct: return True
    words = re.findall(r"[A-Za-z]+", s)
    if len(words) > 20 and len(lines) <= max(1, len(words) // 4): return False
    return True


def looks_like_code(code, declared=None):
    """True only for genuine source code (not diagrams / lists / prose).
    ast/json are definitive; otherwise pygments token density decides."""
    s = (code or "").strip()
    if len(s) < 3: return False
    d = (declared or "").lower()
    if d in ("python", "py", "python3"):
        try:
            import ast; ast.parse(s); return True
        except Exception:
            pass
    if d == "json":
        try:
            import json as _j; _j.loads(s); return True
        except Exception:
            return False
    dens = _code_density(code, declared)
    if dens is None:
        return _looks_like_code_regex(code)
    codey, err, n = dens
    if n < 3: return False
    return codey >= 0.25 and err <= 0.20


def code_only(text):
    """Just the fenced CODE inside a message (joined), skipping non-code fences."""
    reals = [body for lang, body in CODE_FENCE.findall(text or "")
             if looks_like_code(body, _norm_lang(lang))]
    return "\n\n".join(b.strip("\n") for b in reals) if reals else None


def _msg_has_code(text):
    return any(looks_like_code(body, _norm_lang(lang))
               for lang, body in CODE_FENCE.findall(text or ""))


def _response_lang(text):
    """Dominant fence-labeled language among the genuine code blocks of a msg."""
    reals = [(_norm_lang(lg), body) for lg, body in CODE_FENCE.findall(text or "")
             if looks_like_code(body, _norm_lang(lg))]
    langs = [lg for lg, _ in reals if lg]
    return Counter(langs).most_common(1)[0][0] if langs else None


# ---- English-only detection ----------------------------------------------
_EN_STOP = {"the","and","to","of","a","is","in","for","you","this","that","it",
  "with","on","can","how","i","we","be","are","as","at","or","an","if","not",
  "your","my","do","does","using","use","when","what","why","please","help",
  "code","function","error","want","need","should","would","could","here"}
_LANGDETECT = None
def is_english(turns):
    prose = " ".join(CODE_FENCE.sub(" ", t["text"] or "") for t in turns).strip()
    if len(prose) < 20: return True
    global _LANGDETECT
    if _LANGDETECT is None:
        try:
            from langdetect import detect, DetectorFactory
            DetectorFactory.seed = 0; _LANGDETECT = detect
        except Exception:
            _LANGDETECT = False
    if _LANGDETECT:
        try: return _LANGDETECT(prose[:3000]) == "en"
        except Exception: pass
    words = re.findall(r"[a-zA-Z']+", prose.lower())
    return bool(words) and sum(1 for w in words if w in _EN_STOP) / len(words) >= 0.12


# ---- per-conversation fields ---------------------------------------------
def conversation_fields(turns):
    users = [t["text"] for t in turns if t["role"] == "user"]
    llms = [t["text"] for t in turns if t["role"] == "assistant"]
    llm_code = [t["text"] for t in turns if t["role"] == "assistant" and _msg_has_code(t["text"])]
    preceding, last_user = [], None
    for t in turns:
        if t["role"] == "user": last_user = t["text"]
        elif t["role"] == "assistant" and _msg_has_code(t["text"]): preceding.append(last_user)
    return {
        "first_user_message": users[0] if users else None,
        "first_llm_response": llms[0] if llms else None,
        "first_user_before_code": preceding[0] if preceding else None,
        "first_llm_with_code": llm_code[0] if llm_code else None,
        "first_llm_with_code_snippet": code_only(llm_code[0]) if llm_code else None,
        "n_total_messages": len(turns), "n_user_messages": len(users),
        "n_llm_messages": len(llms), "n_llm_messages_with_code": len(llm_code),
        "n_user_messages_with_code": sum(1 for t in turns if t["role"] == "user" and _msg_has_code(t["text"])),
        "last_user_before_code": preceding[-1] if preceding else None,
        "last_llm_with_code": llm_code[-1] if llm_code else None,
        "last_llm_with_code_snippet": code_only(llm_code[-1]) if llm_code else None}


_USER_CACHE = {}
def user_type(login, source):
    if not login: return None
    if login in _USER_CACHE: return _USER_CACHE[login]
    try: t = gh(f"/users/{login}", {}).get("type")
    except Exception: t = None
    _USER_CACHE[login] = t; return t

def profile_link(login, source):
    if not login: return None
    return f"https://github.com/{login}"

def _distinctive_line(code):
    c = [l.strip() for l in (code or "").splitlines() if 25 <= len(l.strip()) <= 120]
    c = [l for l in c if '"' not in l and "\\" not in l]; c.sort(key=len, reverse=True)
    return c[0] if c else None

def find_snippet_file(repo, code):
    line = _distinctive_line(code)
    if not repo or not line: return None
    try:
        items = gh("/search/code", {"q": f'repo:{repo} "{line}"', "per_page": 1}).get("items", [])
        return items[0].get("html_url") if items else None
    except Exception:
        return None


RICH_COLUMNS = [
 ("first_user_message","first user message"),
 ("first_llm_response","first LLM response"),
 ("first_user_before_code","first user message before snippet"),
 ("first_llm_with_code","first LLM message with snippet"),
 ("first_llm_with_code_snippet","first LLM snippet (code only)"),
 ("n_total_messages","total number of messages"),
 ("n_user_messages","number of user messages"),
 ("n_llm_messages","number of LLM messages"),
 ("n_llm_messages_with_code","number of LLM messages with snippet"),
 ("n_user_messages_with_code","number of user messages with snippet"),
 ("last_user_before_code","last user message before snippet"),
 ("last_llm_with_code","last LLM message with snippet"),
 ("last_llm_with_code_snippet","last LLM snippet (code only)"),
 ("repo_name","repo name"),("repo_link","link repo"),("chat_link","link chat"),
 ("artifact_type","type of the metadata containing the chat link"),("artifact_text","text of the metadata containing the chat link"),
 ("last_snippet_repo_file","repo file of the last snippet (link)"),
 ("snippet_repo_files","repo file with the conversation's snippet"),
 ("user_profile_link","user profile link"),("user_type","user type"),
 ("language","language (fence label)"),
 ("language_detected","language recognized from the snippet"),
 ("provider","provider"),("model_slug","model")]


def build_rich_row(conv, art, provenance=False):
    turns = conv.get("turns") or []
    blocks = extract_code_blocks(turns)
    # keep only fence-labeled AND genuinely code blocks
    labeled = [b for b in blocks
               if b["language_declared"] and looks_like_code(b["code"], b["language_declared"])]
    langs = [b["language_declared"] for b in labeled]
    detected = [b["language_detected"] for b in labeled if b["language_detected"]]
    repo = art.get("repo"); login = art.get("author"); source = art.get("source")
    row = conversation_fields(turns)
    row.update({
        "repo_name": repo, "repo_link": f"https://github.com/{repo}" if repo else None,
        "chat_link": conv.get("share_url"), "artifact_type": source,
        "artifact_text": art.get("artifact_text"),
        "user_profile_link": profile_link(login, source),
        "user_type": user_type(login, source),
        "language": Counter(langs).most_common(1)[0][0] if langs else None,
        "language_detected": Counter(detected).most_common(1)[0][0] if detected else None,
        "provider": provider_of(conv.get("share_url", "")),
        "model_slug": conv.get("model_slug"),
        "last_snippet_repo_file": None, "snippet_repo_files": None})
    if provenance and repo and labeled:
        row["last_snippet_repo_file"] = find_snippet_file(repo, labeled[-1]["code"])
        files = []
        for b in labeled:
            u = find_snippet_file(repo, b["code"])
            if u and u not in files: files.append(u)
        row["snippet_repo_files"] = " | ".join(files) if files else None
    return row, langs


def write_rich_csv(rows, path):
    import csv
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow([h for _, h in RICH_COLUMNS])
        for r in rows: w.writerow([r.get(k) for k, _ in RICH_COLUMNS])


def discover_sample(max_pages=10, sources=("issue", "pr")):
    """Stage A WITHOUT date windows: a sample across all history, keeps author."""
    arts = []
    for dom in SHARE_DOMAINS:
        for kind in sources:
            for page in range(1, max_pages + 1):
                try:
                    res = gh("/search/issues", {"q": f'"{dom}" type:{kind}',
                             "per_page": PER_PAGE, "page": page,
                             "sort": "created", "order": "desc"})
                except Exception as e:
                    print(f"  search {dom}/{kind} p{page}: {e}"); break
                items = res.get("items", [])
                if not items: break
                arts += [_artifact_from_issue(it) for it in items]
                if len(items) < PER_PAGE: break
    return arts


def run_rich(args):
    """Build the rich dataset (or the per-language test CSVs)."""
    print(f"Auth: {sum(1 for t in POOL.tokens if t)} token(s) in pool")
    by_link = {}
    if args.from_json:
        path = args.from_json
        if path.endswith(".jsonl"):
            # rich_dataset.jsonl / codegen.jsonl: reuse the links we already have
            for line in open(path, encoding="utf-8"):
                line = line.strip()
                if not line: continue
                row = json.loads(line)
                conv = row.get("conversation") if isinstance(row.get("conversation"), dict) else {}
                chat = (row.get("chat_link") or row.get("share_url")
                        or conv.get("share_url"))
                if not chat: continue
                refm = (row.get("referenced_by") or [{}])[0]
                by_link.setdefault(chat, {
                    "repo": row.get("repo_name") or row.get("repo") or refm.get("repo"),
                    "author": refm.get("author"),
                    "source": row.get("artifact_type") or refm.get("source"),
                    "artifact_text": row.get("artifact_text") or refm.get("artifact_text"),
                    "share_links": [chat]})
        else:
            with open(path, encoding="utf-8") as f:
                prev = json.load(f)
            for rec in prev.get("links", []):
                ref = (rec.get("referenced_by") or [{}])[0]
                by_link[rec["share_url"]] = {
                    "repo": ref.get("repo"), "author": ref.get("author"),
                "source": ref.get("source"), "artifact_text": ref.get("artifact_text"),
                "artifact_url": ref.get("artifact_url"),
                "share_links": [rec["share_url"]]}
        print(f"Loaded {len(by_link)} links from {args.from_json}")
    elif args.test:
        print("Stage A — discovery (no date limits) …")
        for a in discover_sample(max_pages=args.max_pages):
            for l in a.get("share_links", []): by_link.setdefault(l, a)
    else:
        since = dt.date.fromisoformat(args.since); until = dt.date.fromisoformat(args.until)
        print(f"Stage A — windowed discovery {since} .. {until}  sources={args.sources} …")
        arts = []
        if "issues" in args.sources: arts += collect_issues(since, until)
        if "prs" in args.sources:    arts += collect_prs(since, until)
        if "code" in args.sources:   arts += enrich_code_links(collect_code())
        if args.comments:            enrich_comments(arts)
        for a in arts:
            for l in a.get("share_links", []): by_link.setdefault(l, a)
    urls = list(by_link)[:args.max_fetch] if args.max_fetch else list(by_link)
    print(f"  {len(by_link)} unique links; fetching {len(urls)}")
    if not urls:
        print("  nothing to fetch (no tokens? no links?)"); return

    print(f"Stage B — fetching (concurrency={args.concurrency}) …")
    st = Counter()
    def prog(d, t, res):
        st[res.get("status", "error")] += 1
        if d % 25 == 0 or d == t:
            print(f"    [{d}/{t}] " + " ".join(f"{k}={v}" for k, v in st.items()))
    try:
        results = fetch_conversations(urls, concurrency=args.concurrency,
                                      timeout=args.timeout * 1000, on_done=prog)
    except Exception as e:
        print(f"  ! fetch failed ({e}); pip install playwright && playwright install chromium")
        return

    def keep(conv):
        return (conv.get("status") == "ok"
                and (args.any_language or is_english(conv.get("turns") or [])))
    if not args.any_language:
        n = sum(1 for c in results.values()
                if c.get("status") == "ok" and not is_english(c.get("turns") or []))
        print(f"  English filter: {n} non-English conversations skipped")

    if getattr(args, "pairs", False):
        import csv as _csv
        by_lang = OrderedDict(); dedup = set(); n_pairs = 0; n_conv = 0
        for url, conv in results.items():
            if not keep(conv): continue
            n_conv += 1
            art = by_link[url]; repo = art.get("repo")
            chat = conv.get("share_url"); prov = provider_of(chat or "")
            model = conv.get("model_slug"); last_user = None
            for t in conv.get("turns") or []:
                if t["role"] == "user":
                    last_user = t["text"]
                elif t["role"] == "assistant" and _msg_has_code(t["text"]):
                    if getattr(args, "per_block", False):
                        # ONE ROW PER CODE BLOCK (each snippet, its own language)
                        for lg, body in CODE_FENCE.findall(t["text"]):
                            d = _norm_lang(lg)
                            if not looks_like_code(body, d):
                                continue
                            code = body.strip("\n")
                            if code.count("\n") + 1 < args.min_code_lines:
                                continue
                            lang = d
                            if not lang:
                                if not args.include_unknown: continue
                                lang = "unknown"
                            key = (chat, hash(code))
                            if key in dedup: continue
                            dedup.add(key)
                            by_lang.setdefault(lang, []).append({
                                "conversation_url": chat, "prompt": last_user,
                                "response": t["text"], "response_code": code,
                                "language": lang, "provider": prov,
                                "model": model, "repo": repo})
                            n_pairs += 1
                        continue
                    code = code_only(t["text"])
                    if not code or code.count("\n") + 1 < args.min_code_lines:
                        continue
                    lang = _response_lang(t["text"])
                    if not lang:
                        if not args.include_unknown: continue
                        lang = "unknown"
                    key = (chat, hash(code))
                    if key in dedup: continue
                    dedup.add(key)
                    by_lang.setdefault(lang, []).append({
                        "conversation_url": chat, "prompt": last_user,
                        "response": t["text"], "response_code": code,
                        "language": lang, "provider": prov,
                        "model": model, "repo": repo})
                    n_pairs += 1
        os.makedirs(args.out_dir, exist_ok=True)
        cols = ["conversation_url", "prompt", "response", "response_code",
                "language", "provider", "model", "repo"]
        for lang, rows in by_lang.items():
            p = os.path.join(args.out_dir, f"{re.sub('[^a-z0-9]+','_',lang.lower())}.csv")
            with open(p, "w", newline="", encoding="utf-8") as f:
                w = _csv.DictWriter(f, fieldnames=cols); w.writeheader()
                for r in rows: w.writerow(r)
        per_lang = {lg: len(rs) for lg, rs in sorted(by_lang.items(), key=lambda kv: -len(kv[1]))}
        avg = round(sum(r["response_code"].count("\n") + 1
                        for rs in by_lang.values() for r in rs) / n_pairs, 1) if n_pairs else 0
        stats = {"conversations": n_conv, "total_pairs": n_pairs,
                 "languages": len(by_lang), "avg_code_lines_per_response": avg,
                 "pairs_per_language": per_lang}
        json.dump(stats, open(os.path.join(args.out_dir, "pairs_stats.json"), "w"),
                  indent=2, ensure_ascii=False)
        print("\n============= STATISTICS (prompt-response pairs) =============")
        print(f"Conversations used:     {n_conv}")
        print(f"Prompt-response pairs:  {n_pairs}")
        print(f"Languages:               {len(by_lang)}")
        print(f"Average lines of code:   {avg}")
        print("Pairs per language:")
        for lg, c in per_lang.items():
            print(f"    {c:>6}  {lg}")
        print("===============================================================")
        print(f"CSV -> {args.out_dir}/<language>.csv  |  stats -> {args.out_dir}/pairs_stats.json")
        return

    if args.test:
        per = OrderedDict()
        for url, conv in results.items():
            if not keep(conv): continue
            blocks = extract_code_blocks(conv.get("turns") or [])
            langs = [b["language_declared"] for b in blocks
                     if b["language_declared"] and looks_like_code(b["code"], b["language_declared"])]
            new = [lg for lg in langs if lg and lg not in per]
            if not new: continue
            row, _ = build_rich_row(conv, by_link[url], provenance=True)
            for lg in new:
                r = dict(row); r["language"] = lg; per[lg] = r
        os.makedirs(args.out_dir, exist_ok=True)
        for lg, r in per.items():
            write_rich_csv([r], os.path.join(args.out_dir,
                           f"test_{re.sub('[^a-z0-9]+','_',lg.lower())}.csv"))
        print(f"\nTEST: {len(per)} languages -> {args.out_dir}/")
        for lg in per: print(f"  test_{re.sub('[^a-z0-9]+','_',lg.lower())}.csv")
        return

    rows = []
    conv_langs = []
    for url, conv in results.items():
        if not keep(conv): continue
        row, langs = build_rich_row(conv, by_link[url], provenance=args.provenance)
        rows.append(row)
        conv_langs.append(sorted(set(langs)))
    write_rich_csv(rows, f"{args.out}.csv")
    with open(f"{args.out}.jsonl", "w", encoding="utf-8") as f:
        for r in rows: f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # ---- statistics ----
    n_conv = len(rows)
    avg_msgs = round(sum(r["n_total_messages"] for r in rows) / n_conv, 2) if n_conv else 0
    avg_user = round(sum(r["n_user_messages"] for r in rows) / n_conv, 2) if n_conv else 0
    avg_snip = round(sum(r["n_llm_messages_with_code"] for r in rows) / n_conv, 2) if n_conv else 0
    per_lang = Counter()
    for lgs in conv_langs:
        for lg in lgs: per_lang[lg] += 1        # a conversation counts once per language it contains
    stats = {
        "n_conversations": n_conv,
        "avg_messages_per_conversation": avg_msgs,
        "avg_user_messages_per_conversation": avg_user,
        "avg_code_snippets_per_conversation": avg_snip,
        "conversations_per_language": dict(per_lang.most_common()),
    }
    with open(f"{args.out}.stats.json", "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)

    print("\n================= STATISTICS =================")
    print(f"Conversations found:                   {n_conv}")
    print(f"Average messages per conversation:     {avg_msgs}")
    print(f"  of which user messages (average):    {avg_user}")
    print(f"Average code snippets per conversation: {avg_snip}")
    print("Conversations per language (one conversation may appear in several):")
    for lg, c in per_lang.most_common():
        print(f"    {c:>5}  {lg}")
    print("==============================================")
    print(f"\n{n_conv} conversations -> {args.out}.csv  &  {args.out}.jsonl"
          f"  &  {args.out}.stats.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2024-01-01")
    ap.add_argument("--until", default="2024-01-31")
    ap.add_argument("--sources", nargs="+",
                    default=["issues", "prs"],
                    choices=["issues", "prs", "code"])
    ap.add_argument("--comments", action="store_true",
                    help="also fetch artifact comments (catches links in replies)")
    ap.add_argument("--fetch", action="store_true",
                    help="Stage B: fetch each conversation (needs Playwright + net)")
    ap.add_argument("--fetch-limit", type=int, default=20)
    ap.add_argument("--concurrency", type=int, default=8,
                    help="Stage B: conversations fetched in parallel (try 5-12)")
    ap.add_argument("--timeout", type=int, default=30,
                    help="Stage B: per-link timeout in seconds")
    ap.add_argument("--csv", action="store_true",
                    help="also write a flat CSV table (one row per share link)")
    ap.add_argument("--out", default="snapshot")
    ap.add_argument("--from-json", default=None,
                    help="skip Stage A: load links from a previously saved "
                         "snapshot .json and run only Stage B / exports")
    # ---- rich dataset / test mode (no date limits) ----
    ap.add_argument("--dataset", action="store_true",
                    help="build the RICH per-conversation dataset (CSV + JSONL)")
    ap.add_argument("--pairs", action="store_true",
                    help="build per-language prompt->response pairs (only answers "
                         "with code) from the fetched conversations")
    ap.add_argument("--min-code-lines", type=int, default=1,
                    help="pairs: drop responses whose code is shorter than N lines")
    ap.add_argument("--include-unknown", action="store_true",
                    help="pairs: also keep responses whose code has no language label")
    ap.add_argument("--per-block", action="store_true",
                    help="pairs: one row per CODE BLOCK (each snippet, its own "
                         "language) instead of one row per response")
    ap.add_argument("--test", action="store_true",
                    help="rich TEST mode: one CSV per language, a single row each")
    ap.add_argument("--out-dir", default="per_language_test")
    ap.add_argument("--max-pages", type=int, default=10,
                    help="rich mode: search pages per domain/kind (no date limit)")
    ap.add_argument("--max-fetch", type=int, default=300,
                    help="rich mode: cap conversations to fetch")
    ap.add_argument("--provenance", action="store_true",
                    help="rich mode: locate snippets in repo files (slow code search)")
    ap.add_argument("--any-language", action="store_true",
                    help="rich mode: keep non-English conversations too")
    args = ap.parse_args()

    if args.test or args.dataset or args.pairs:
        if args.test:
            args.provenance = True
            args.max_fetch = min(args.max_fetch, 200)
        run_rich(args)
        return

    n_tok = sum(1 for t in POOL.tokens if t)
    print(f"Auth: {n_tok} token(s) in pool "
          f"({'~%d/hr core, ~%d/min search, code search ON' % (5000*max(n_tok,1), 30*max(n_tok,1)) if n_tok else 'UNAUTH 60/hr, 10/min, no code search'})")

    if args.from_json:
        # ---- resume mode: reuse a saved Stage-A snapshot ----
        with open(args.from_json, encoding="utf-8") as f:
            prev = json.load(f)
        links = {rec["share_url"]: rec for rec in prev.get("links", [])}
        # don't re-fetch links already fetched OK on a previous run
        for rec in links.values():
            if (rec.get("conversation") or {}).get("status") != "ok":
                rec.pop("conversation", None)
        artifacts = []
        win = prev.get("window", {})
        since = dt.date.fromisoformat(win.get("since", args.since))
        until = dt.date.fromisoformat(win.get("until", args.until))
        snapshot = dict(prev)
        snapshot["n_unique_links"] = len(links)
        print(f"Resuming from {args.from_json}: {len(links)} links loaded\n")
    else:
        since = dt.date.fromisoformat(args.since)
        until = dt.date.fromisoformat(args.until)
        print(f"Window: {since} .. {until}   sources={args.sources}\n")

        artifacts = []
        if "issues" in args.sources: artifacts += collect_issues(since, until)
        if "prs" in args.sources:    artifacts += collect_prs(since, until)
        if "code" in args.sources:   artifacts += enrich_code_links(collect_code())
        if args.comments:            enrich_comments(artifacts)

        links = dedup(artifacts)
        snapshot = {
            "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "window": {"since": args.since, "until": args.until},
            "sources": args.sources,
            "n_artifacts": len(artifacts),
            "n_unique_links": len(links),
            "links": list(links.values()),
        }

    # Stage B (optional) — concurrent: one browser, many pages in parallel
    if args.fetch:
        targets = list(links.values())[:args.fetch_limit]
        print(f"\n[B] fetching {len(targets)} conversations "
              f"(concurrency={args.concurrency}) …")
        urls = [rec["share_url"] for rec in targets]
        status_counts = Counter()

        def _progress(done, total, res):
            status_counts[res.get("status", "error")] += 1
            if done % 25 == 0 or done == total:
                print(f"    [{done}/{total}] " +
                      " ".join(f"{k}={v}" for k, v in status_counts.items()), flush=True)

        try:
            results = fetch_conversations(urls, concurrency=args.concurrency,
                                          timeout=args.timeout * 1000,
                                          on_done=_progress)
        except Exception as e:
            print(f"  ! concurrent fetch failed ({e}); is Playwright installed?")
            results = {}
        n_normalize_failed = 0
        for rec in targets:
            conv = results.get(rec["share_url"]) or {"share_url": rec["share_url"],
                                                      "status": "error"}
            try:
                rec["conversation"] = normalize(conv)
            except Exception as e:
                # A normalization error on ONE record (e.g. text with literal special
                # tokens that tiktoken rejects) must not be able to make us lose the
                # whole batch already fetched - see the comment on count_tokens().
                n_normalize_failed += 1
                rec["conversation"] = {"share_url": rec["share_url"], "status": "error",
                                        "error": f"normalize failed: {e}"[:200]}
        if n_normalize_failed:
            print(f"  ! normalize() failed for {n_normalize_failed} record "
                  f"(saved as status=error, they did not block the rest)")
        n_codegen_ok = sum(1 for rec in targets
                           if rec.get("conversation", {}).get("status") == "ok"
                           and rec["conversation"].get("is_code_generation"))
        snapshot["fetch_stats"] = {
            "ok": status_counts.get("ok", 0),
            "by_status": dict(status_counts),
            "code_generation": n_codegen_ok}

    fn = f"{args.out}_{since}_{until}.json"
    with open(fn, "w") as f:
        json.dump(snapshot, f, indent=2)
    csv_fn = flat_fn = None
    if args.csv:
        csv_fn = f"{args.out}_{since}_{until}.csv"
        write_csv(links, csv_fn)
        flat_fn = f"{args.out}_{since}_{until}.flat.json"
        write_flat_json(links, flat_fn)
    # JSONL with ONLY code-generation conversations (needs --fetch to classify)
    codegen_fn = n_codegen = None
    if args.fetch:
        codegen_fn = f"{args.out}_{since}_{until}.codegen.jsonl"
        n_codegen = write_codegen_jsonl(links, codegen_fn)

    # Summary
    print("\n================= SUMMARY =================")
    print(f"Artifacts:            {snapshot['n_artifacts']}")
    print(f"Unique share links:   {snapshot['n_unique_links']}")
    print(f"By source: {dict(Counter(a['source'] for a in artifacts))}")
    multi = sum(1 for v in links.values() if len(v['referenced_by']) > 1)
    print(f"Links referenced in >1 place: {multi}")
    if args.fetch:
        print(f"Stage B: {snapshot['fetch_stats']}")
    print(f"Saved -> {fn}")
    if csv_fn:
        print(f"Saved -> {csv_fn}")
    if flat_fn:
        print(f"Saved -> {flat_fn}")
    if codegen_fn:
        print(f"Saved -> {codegen_fn}  ({n_codegen} code-generation conversations)")
    print("==========================================")


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# SCALE NOTE — GH Archive on BigQuery
# ---------------------------------------------------------------------------
# For issues/PRs/comments at large scale, the GitHub Search API's 1,000-result
# cap and 30/min limit make full coverage slow. GH Archive mirrors every public
# GitHub event and is a public BigQuery dataset. One query finds all links in,
# e.g., issue comments for a month with no cap and no rate limit:
#
#   SELECT repo.name, actor.login, created_at,
#          JSON_EXTRACT_SCALAR(payload,'$.comment.html_url') AS artifact_url,
#          REGEXP_EXTRACT_ALL(payload,
#            r'https?://(?:chatgpt\.com|chat\.openai\.com)/share/[0-9a-f-]+') AS links
#   FROM `githubarchive.month.202504`
#   WHERE type='IssueCommentEvent'
#     AND REGEXP_CONTAINS(payload, r'(?:chatgpt\.com|chat\.openai\.com)/share/');
#
# Use the API path (this script) for code files and for convenience; use
# BigQuery when you need exhaustive coverage. Stage B (fetch) is identical
# either way.