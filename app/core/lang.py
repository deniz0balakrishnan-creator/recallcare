"""Language helpers: deterministic message rendering, date/time formatting, script-based language checks."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Literal

from app.settings import clinic_config, messages_config, templates_config

Lang = Literal["en", "zh", "ms", "ta"]
LANGS: tuple[str, ...] = ("en", "zh", "ms", "ta")
LANG_NAMES = {"en": "English", "zh": "Mandarin (Simplified Chinese)", "ms": "Malay", "ta": "Tamil"}

_WEEKDAYS = {
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "zh": ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"],
    "ms": ["Isnin", "Selasa", "Rabu", "Khamis", "Jumaat", "Sabtu", "Ahad"],
    "ta": ["திங்கள்", "செவ்வாய்", "புதன்", "வியாழன்", "வெள்ளி", "சனி", "ஞாயிறு"],
}
_MONTHS = {
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    "ms": ["Jan", "Feb", "Mac", "Apr", "Mei", "Jun", "Jul", "Ogo", "Sep", "Okt", "Nov", "Dis"],
    "ta": ["ஜனவரி", "பிப்ரவரி", "மார்ச்", "ஏப்ரல்", "மே", "ஜூன்", "ஜூலை", "ஆகஸ்ட்", "செப்டம்பர்", "அக்டோபர்", "நவம்பர்", "டிசம்பர்"],
}


def fmt_time(dt: datetime, lang: str) -> str:
    h, m = dt.hour, dt.minute
    h12 = h % 12 or 12
    hm = f"{h12}:{m:02d}"
    if lang == "zh":
        part = "上午" if h < 12 else ("下午" if h < 18 else "晚上")
        return f"{part}{hm}"
    if lang == "ms":
        part = "pagi" if h < 12 else ("petang" if h < 19 else "malam")
        return f"{hm} {part}"
    if lang == "ta":
        part = "காலை" if h < 12 else ("மதியம்" if h < 16 else "மாலை")
        return f"{part} {hm}"
    return f"{hm}{'am' if h < 12 else 'pm'}"


def fmt_when(dt: datetime, lang: str) -> str:
    wd = _WEEKDAYS.get(lang, _WEEKDAYS["en"])[dt.weekday()]
    if lang == "zh":
        return f"{dt.month}月{dt.day}日（{wd}）{fmt_time(dt, lang)}"
    months = _MONTHS.get(lang, _MONTHS["en"])
    return f"{wd} {dt.day} {months[dt.month - 1]}, {fmt_time(dt, lang)}"


def render(key: str, lang: str, **kw: str) -> str:
    """Render a deterministic message from config/messages.yaml in `lang` (falls back to English)."""
    msgs = messages_config()[key]
    clinic = clinic_config()["clinic"]
    kw.setdefault("clinic", clinic["name"])
    kw.setdefault("phone", clinic["phone_display"])
    text = msgs.get(lang) or msgs["en"]
    return text.format(**kw)


def render_pair(key: str, lang: str, **kw: str) -> tuple[str, str]:
    """(text in patient's language, English gloss)."""
    return render(key, lang, **kw), render(key, "en", **kw)


def visit_label(visit_type: str, lang: str) -> str:
    labels = templates_config().get("visit_type_labels", {}).get(visit_type, {})
    return labels.get(lang) or labels.get("en") or visit_type.replace("_", " ")


# ---------------------------------------------------------------- language checks
_CJK = re.compile(r"[一-鿿]")
_TAMIL = re.compile(r"[஀-௿]")
_LATIN_WORD = re.compile(r"[A-Za-z]+")
_MALAY_MARKERS = {
    "anda", "saya", "untuk", "dengan", "boleh", "terima", "kasih", "sila", "temujanji", "ini", "itu", "tidak",
    "kami", "masa", "hari", "pagi", "petang", "malam", "klinik", "gigi", "ada", "akan", "lagi", "sudah",
    "mahu", "atau", "dan", "yang", "di", "ke", "pada", "jam", "hubungi", "selamat", "tempah", "berikut",
}
_EN_MARKERS = {
    "the", "you", "your", "and", "to", "for", "is", "are", "we", "our", "please", "thank", "with", "at",
    "this", "that", "can", "will", "time", "book", "appointment", "clinic", "hello", "hi", "have", "of",
}


def detect_lang(text: str) -> str:
    """Cheap script/stopword detector, good enough to validate our own outbound text."""
    if not text.strip():
        return "unknown"
    cjk, tamil = len(_CJK.findall(text)), len(_TAMIL.findall(text))
    latin_words = [w.lower() for w in _LATIN_WORD.findall(text)]
    if tamil >= 3 and tamil >= cjk:
        return "ta"
    if cjk >= 2:
        return "zh"
    ms = sum(w in _MALAY_MARKERS for w in latin_words)
    en = sum(w in _EN_MARKERS for w in latin_words)
    if ms == 0 and en == 0:
        return "unknown"
    return "ms" if ms > en else "en"


def lang_matches(text: str, expected: str) -> bool:
    got = detect_lang(text)
    if got == "unknown":
        # e.g. a message that is only a time/number/clinic name — not a mismatch
        return True
    return got == expected


def normalise_text(text: str) -> str:
    """Lower-case, NFKC, collapse whitespace, strip surrounding punctuation."""
    t = unicodedata.normalize("NFKC", text).lower().strip()
    t = re.sub(r"\s+", " ", t)
    return t.strip(" .!?。！？,，~…")


def is_emoji_or_symbols(text: str) -> bool:
    stripped = [ch for ch in text if not ch.isspace()]
    return bool(stripped) and all(unicodedata.category(ch)[0] in {"S", "P", "C"} or ord(ch) > 0x1F000 for ch in stripped)
