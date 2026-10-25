#!/usr/bin/env python3
"""
Shared pieces of the HTML conversation browsers (chat, agentic, agentic other languages):

  * pack_conversation(): turns -> compact transcript (roles, ordinals, token counts, truncated text);
  * conversation_is_english(): conversation-level language check on the user messages;
  * pack_payload(): JSON -> gzip -> base64, read back in the browser with DecompressionStream;
  * VIEWER_CSS / VIEWER_JS: transcript rendering shared by the generators.

Token counts use tiktoken (o200k_base) when available - an approximation for non-OpenAI
models - and fall back to len(text)/4 otherwise (TOKEN_METHOD says which one was used).
"""
import base64
import gzip
import hashlib
import json
import os
import re

import lib_language_filter

CAP_TURN = 4000   # characters of each turn kept in the page (the full text is in conversations.jsonl)
HEAD_TURNS = 50   # long conversations: first HEAD_TURNS ...
TAIL_TURNS = 30   # ... last TAIL_TURNS turns and every exchange with pairs are shown, the rest is summarised
ROLE = {"user": "u", "assistant": "a", "agent": "a"}

_ENC = None
TOKEN_METHOD = None


def count_tokens(text):
    global _ENC, TOKEN_METHOD
    if _ENC is None:
        try:
            import tiktoken
            _ENC = tiktoken.get_encoding("o200k_base")
            TOKEN_METHOD = "tiktoken o200k_base"
        except Exception:
            _ENC = False
            TOKEN_METHOD = "estimate (characters / 4)"
    if _ENC:
        return len(_ENC.encode(text, disallowed_special=()))
    return (len(text) + 3) // 4


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip())


def text_key(s):
    """Whole-text key and 120-character-prefix key used to locate a prompt among the user turns."""
    n = _norm(s)
    return (hashlib.md5(n.encode("utf-8")).hexdigest()[:12],
            hashlib.md5(n[:120].encode("utf-8")).hexdigest()[:10])


def pack_conversation(turns):
    items, nu, nm, tu, tm = [], 0, 0, 0, 0
    for ti, t in enumerate(turns, 1):
        role = ROLE.get(t.get("role"), "o")
        text = t.get("text") or ""
        k = count_tokens(text)
        if role == "u":
            nu += 1
            i = nu
            tu += k
        elif role == "a":
            nm += 1
            i = nm
            tm += k
        else:
            i = 0
        it = {"r": role, "i": i, "ui": nu, "ti": ti, "k": k, "t": text[:CAP_TURN]}
        if len(text) > CAP_TURN:
            it["x"] = len(text) - CAP_TURN
        if t.get("model"):
            it["m"] = t["model"]
        if t.get("sidechain"):
            it["sc"] = 1
        if role == "u":
            it["h"], it["h2"] = text_key(text)
        items.append(it)
    return {"nu": nu, "nm": nm, "tu": tu, "tm": tm, "nt": len(turns), "turns": items}


def conversation_stats(turns):
    """Message and token counts of a conversation, with the same definitions as the HTML browsers.

    A *user message* is a turn with role user; a *model message* (response) is a turn with role
    assistant or agent; turns with another role (chat: unknown) are counted only in n_total_messages.
    Returns (summary, per_turn): summary has the conversation-level counts, per_turn one dict per
    turn with n_tokens, message_index (i-th user message / i-th model message) and, for model
    messages, after_user_message (the user message it answers)."""
    per_turn, nu, nm, tu, tm = [], 0, 0, 0, 0
    for t in turns:
        role = ROLE.get(t.get("role"), "o")
        k = count_tokens(t.get("text") or "")
        info = {"n_tokens": k}
        if role == "u":
            nu += 1
            tu += k
            info["message_index"] = nu
        elif role == "a":
            nm += 1
            tm += k
            info["message_index"] = nm
            info["after_user_message"] = nu
        per_turn.append(info)
    summary = {"n_user_messages": nu, "n_llm_messages": nm, "n_total_messages": len(turns),
               "n_tokens_user": tu, "n_tokens_llm": tm, "n_tokens_total": sum(p["n_tokens"] for p in per_turn)}
    return summary, per_turn


def enrich_conversations(src, dst):
    """Copy a conversations.jsonl adding conversation_stats to every conversation (conversation-level
    counts before "turns", per-turn counts inside each turn). Written to a temporary file and moved
    into place only when complete; src is never modified."""
    count_tokens("")
    n = 0
    tmp = dst + ".tmp"
    with open(src, encoding="utf-8") as fin, open(tmp, "w", encoding="utf-8") as fout:
        for line in fin:
            if not line.strip():
                continue
            conv = json.loads(line)
            summary, per_turn = conversation_stats(conv.get("turns") or [])
            out = {}
            for key, value in conv.items():
                if key == "turns":
                    out.update(summary)
                    out["token_count_method"] = TOKEN_METHOD
                if key in summary or key == "token_count_method":
                    continue
                out[key] = value
            out["turns"] = [dict(t, **p) for t, p in zip(conv.get("turns") or [], per_turn)]
            fout.write(json.dumps(out, ensure_ascii=False) + "\n")
            n += 1
    os.replace(tmp, dst)
    return n


def attach_prompts(packed, prompts):
    """Marks, in packed['turns'], the user turn of each prompt (field 'pi' = index into prompts).
    Returns the number of prompts located."""
    by_h, by_h2 = {}, {}
    for t in packed["turns"]:
        if t["r"] == "u":
            by_h.setdefault(t["h"], []).append(t)
            by_h2.setdefault(t["h2"], []).append(t)
    found = 0
    for pi, text in enumerate(prompts):
        h, h2 = text_key(text)
        for index in (by_h.get(h, []), by_h2.get(h2, [])):
            free = [t for t in index if "pi" not in t]
            if free:
                free[0]["pi"] = pi
                found += 1
                break
    for t in packed["turns"]:
        t.pop("h", None)
        t.pop("h2", None)
    _trim(packed)
    return found


def _trim(packed):
    """Long conversations: keep the first HEAD_TURNS and the last TAIL_TURNS turns plus every exchange that
    carries pairs; the remaining stretches are replaced by an {'r': 'x', 'n': count} marker."""
    items = packed["turns"]
    if len(items) <= HEAD_TURNS + TAIL_TURNS:
        return
    keep = set(range(HEAD_TURNS)) | set(range(len(items) - TAIL_TURNS, len(items)))
    for i, t in enumerate(items):
        if t["r"] == "u" and "pi" in t:
            j = i
            keep.add(j)
            while j + 1 < len(items) and items[j + 1]["r"] != "u":
                j += 1
                keep.add(j)
    out, gap = [], 0
    for i, t in enumerate(items):
        if i in keep:
            if gap:
                out.append({"r": "x", "n": gap})
                gap = 0
            out.append(t)
        else:
            gap += 1
    packed["turns"] = out


def conversation_is_english(turns, min_chars=20, min_share=0.8):
    """Conversation-level English check on the user messages (see lib_language_filter.py): no message with
    a non-Latin script (Chinese, Japanese, Korean, Cyrillic, Arabic...) and at least `min_share` of the
    messages with enough prose detected as English."""
    return lib_language_filter.conversation_is_english(turns, min_chars, min_share)


def pack_payload(obj):
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(gzip.compress(raw, 6, mtime=0)).decode("ascii")


def load_agentic_conversations(path, shas, all_languages=False):
    """Streams conversations.jsonl and returns (packed transcripts by sha, (year, model) by sha, n excluded)."""
    packed, meta, excluded = {}, {}, 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            sha = c["sha"]
            if sha not in shas:
                continue
            if not all_languages and not conversation_is_english(c["turns"]):
                excluded += 1
                continue
            year = None
            if c.get("timestamp"):
                try:
                    year = int(c["timestamp"][:4])
                except ValueError:
                    pass
            models = c.get("models_used") or []
            meta[sha] = (year, models[0] if models else None)
            packed[sha] = pack_conversation(c["turns"])
    return packed, meta, excluded


VIEWER_CSS = r"""
  .tcard{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px 16px;margin-bottom:12px}
  .turn{border:1px solid var(--border);border-radius:8px;padding:8px 12px;margin:8px 0}
  .turn.u{background:#eef2ff;margin-right:6%}
  .turn.a{background:#fff;margin-left:6%}
  .turn.o{background:#f3f4f6}
  .tbody{max-height:560px;overflow-y:auto}
  .thead{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:11px;color:var(--muted);margin-bottom:5px;align-items:center}
  .thead b{color:var(--text)}
  .mdl{background:#ede9fe;color:#5b21b6;border-radius:10px;padding:1px 8px;font-size:10.5px}
  .txt{white-space:pre-wrap;word-break:break-word;font-size:13px;line-height:1.45}
  pre.code{background:#0d1117;color:#c9d1d9;border:0;border-radius:6px;padding:8px 10px;margin:6px 0;font-size:12px;white-space:pre-wrap;word-break:break-word;max-height:none;overflow:visible}
  pre.code .lang{display:block;color:#8b949e;font-size:10px;margin-bottom:3px;text-transform:uppercase}
  .trunc,.omit{font-size:11px;color:#b45309;background:#fef3c7;border-radius:5px;padding:3px 8px;margin-top:5px;display:inline-block}
  .omit{display:block;text-align:center;margin:8px 0}
  .pairs{margin:4px 0 10px 6%;border-left:3px solid var(--accent);padding-left:10px}
  .pairs details{margin-top:6px}
  .pairs pre{max-height:260px}
  .tstats{display:flex;flex-wrap:wrap;gap:6px 22px;font-size:12.5px;margin-top:8px}
  .tstats b{color:var(--muted);font-weight:600}
  .tnote{font-size:11px;color:var(--muted);margin-top:6px}
"""

VIEWER_JS = r"""
const CAP_TURN = __CAP_TURN__;
async function loadData(b64){
  if (typeof DecompressionStream === "undefined"){
    document.body.innerHTML = "<p style='padding:24px'>This browser cannot decompress the embedded data (needs DecompressionStream: Chrome/Edge 80+, Firefox 113+, Safari 16.4+).</p>";
    throw new Error("DecompressionStream not supported");
  }
  const bin = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const stream = new Blob([bin]).stream().pipeThrough(new DecompressionStream("gzip"));
  return JSON.parse(await new Response(stream).text());
}
const fmtN = n => n >= 10000 ? (n/1000).toFixed(1) + "k" : String(n);
function renderBody(text){
  const parts = String(text == null ? "" : text).split(/```([^\n`]*)\n([\s\S]*?)(?:```|$)/g);
  let out = "";
  for (let i = 0; i < parts.length; i += 3){
    if (parts[i] && parts[i].trim()) out += `<div class="txt">${esc(parts[i])}</div>`;
    if (parts[i+2] !== undefined) out += `<pre class="code"><span class="lang">${esc(parts[i+1] || "code")}</span>${esc(parts[i+2])}</pre>`;
  }
  return out || `<div class="txt" style="color:var(--muted)">(empty)</div>`;
}
function transcriptSummary(s, tokenNote){
  return `<div class="tstats">
    <div><b>user messages</b> ${s.nu}</div><div><b>model responses</b> ${s.nm}</div><div><b>turns</b> ${s.nt}</div>
    <div><b>user tokens</b> ${fmtN(s.tu)}</div><div><b>model tokens</b> ${fmtN(s.tm)}</div><div><b>total tokens</b> ${fmtN(s.tu + s.tm)}</div></div>
    <div class="tnote">Tokens: ${esc(tokenNote)}. Each turn is shown up to ${CAP_TURN} characters; the full text is in conversations.jsonl.</div>`;
}
// view: [{p, cs}] prompts (with their visible pairs); candHtml(c): html of one pair; promptBadges(p): html badges of a prompt
function renderTranscript(s, view, candHtml, promptBadges){
  const byPi = new Map();
  s.prompts.forEach((p, i) => { const v = view.find(x => x.p === p); if (v) byPi.set(i, v); });
  const placed = new Set();
  const T = s.turns;
  let html = "", cur = null;
  const flush = () => {
    if (cur != null && byPi.has(cur) && !placed.has(cur)){
      placed.add(cur);
      html += `<div class="pairs">${byPi.get(cur).cs.map(candHtml).join("")}</div>`;
    }
  };
  for (let k = 0; k < T.length; k++){
    const t = T[k];
    if (t.r === "x"){
      html += `<div class="omit">&hellip; ${t.n} turn${t.n===1?"":"s"} not shown in this view (long conversation; full text in conversations.jsonl) &hellip;</div>`;
      continue;
    }
    if (t.r === "u") cur = (t.pi != null) ? t.pi : null;
    const head = t.r === "u" ? `USER message ${t.i} / ${s.nu}`
               : t.r === "a" ? `MODEL response ${t.i} / ${s.nm} <span>(after user message ${t.ui} / ${s.nu})</span>` : "other message";
    const mdl = t.r === "a" ? `<span class="mdl">model: ${esc(t.m || s.m || "n/a")}</span>${t.sc ? '<span class="mdl">sub-agent</span>' : ""}` : "";
    const labels = (t.r === "u" && t.pi != null && promptBadges) ? promptBadges(s.prompts[t.pi]) : "";
    html += `<div class="turn ${t.r}"><div class="thead"><b>${head}</b><span>turn ${t.ti} / ${s.nt}</span><span>${fmtN(t.k)} tokens</span>${mdl}${labels}</div><div class="tbody">${renderBody(t.t)}</div>${t.x ? `<div class="trunc">&hellip; ${fmtN(t.x)} more characters not shown &hellip;</div>` : ""}</div>`;
    const next = T[k+1];
    if (!next || next.r === "u" || next.r === "x") flush();
  }
  const rest = [...byPi.keys()].filter(i => !placed.has(i));
  if (rest.length) html += `<div class="label" style="margin-top:12px">Pairs whose prompt is not in the part of the transcript shown above</div><div class="pairs">${rest.map(i => byPi.get(i).cs.map(candHtml).join("")).join("")}</div>`;
  return html;
}
"""
