#!/usr/bin/env python3
"""
English filter shared by the pipeline (1_04_extract_and_filter.py, the agentic exporters) and the
HTML browsers (lib_html_viewer_common.py).

A text is rejected as non-English when
  1. it contains characters of a non-Latin script: any Chinese / Japanese / Korean character
     (kana, kanji/hanzi, hangul), or at least 3 letters of Cyrillic, Arabic, Hebrew, Thai,
     Devanagari or Bengali (code, tracebacks and URLs do not exempt it: a prompt that contains
     Japanese text is a Japanese prompt, whatever surrounds it), or
  2. its prose (fenced code blocks and URLs removed) has at least MIN_LEN_LANGUAGE characters and
     lingua-py detects another language. Shorter prose is not judged and is kept.
lingua is restricted to 29 common languages: with all the languages it reports Latin, Welsh,
Yoruba... for technical English.
"""
import re

MIN_LEN_LANGUAGE = 20
_LANGS = ["ENGLISH", "SPANISH", "FRENCH", "GERMAN", "PORTUGUESE", "ITALIAN", "DUTCH", "RUSSIAN", "CHINESE",
          "JAPANESE", "KOREAN", "ARABIC", "TURKISH", "POLISH", "INDONESIAN", "VIETNAMESE", "THAI", "HINDI",
          "UKRAINIAN", "SWEDISH", "CZECH", "HEBREW", "PERSIAN", "GREEK", "ROMANIAN", "HUNGARIAN", "DANISH",
          "FINNISH", "BOKMAL"]
_FENCE = re.compile(r"```.*?```", re.S)
_URL = re.compile(r"https?://\S+")
_CJK = re.compile(r"[぀-ヿㇰ-ㇿ㐀-䶿一-鿿豈-﫿가-힯ｦ-ﾟ]")
_OTHER_SCRIPT = re.compile(r"[Ѐ-ӿ֐-׿؀-ۿݐ-ݿ฀-๿ऀ-ॿঀ-৿]")
_DETECTOR = None


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip())


def has_foreign_script(text):
    """Any CJK character, or >= 3 letters of another non-Latin script."""
    text = _URL.sub(" ", text or "")
    return bool(_CJK.search(text)) or len(_OTHER_SCRIPT.findall(text)) >= 3


def prose_of(text):
    return _norm(_URL.sub(" ", _FENCE.sub(" ", text or "")))


def _detector():
    global _DETECTOR
    if _DETECTOR is None:
        from lingua import Language, LanguageDetectorBuilder
        _DETECTOR = LanguageDetectorBuilder.from_languages(*[getattr(Language, n) for n in _LANGS]).build()
    return _DETECTOR


def is_english_text(text, min_len=MIN_LEN_LANGUAGE):
    """True if the text is English, or too short / undetermined to judge (kept by default)."""
    if has_foreign_script(text):
        return False
    prose = prose_of(text)
    if len(prose) < min_len:
        return True
    from lingua import Language
    lang = _detector().detect_language_of(prose[:2000])
    return lang is None or lang == Language.ENGLISH


def conversation_is_english(turns, min_len=MIN_LEN_LANGUAGE, min_share=0.8):
    """True if no user message contains a foreign script and at least `min_share` of the user
    messages with enough prose are detected as English. No such message -> True."""
    from lingua import Language
    n = n_en = 0
    for t in turns:
        if t.get("role") != "user":
            continue
        text = t.get("text") or ""
        if has_foreign_script(text):
            return False
        prose = prose_of(text)
        if len(prose) < min_len:
            continue
        lang = _detector().detect_language_of(prose[:1500])
        if lang is None:
            continue
        n += 1
        n_en += lang == Language.ENGLISH
    return n == 0 or n_en / n >= min_share
