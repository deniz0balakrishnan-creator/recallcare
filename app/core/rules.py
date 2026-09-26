"""Deterministic language rules in 4 languages: guard pre-screen, short-reply classifier, time-preference parser.

These run before (or instead of) any LLM call. They are also what the mock LLM uses to behave plausibly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app import clock
from app.core.lang import is_emoji_or_symbols, normalise_text
from app.settings import clinic_config

SECURITY_CATS = ("injection", "impersonation", "other_recipient")
# Severity order when several categories hit (first wins as the primary category).
SEVERITY = ("injection", "impersonation", "other_recipient", "clinical", "complaint", "billing", "human_request",
            "abuse", "opt_out")

_LATIN = re.compile(r"^[\x00-\x7f]+$")
_PHONE = re.compile(r"(?:\+?\d[\d\s-]{6,}\d)")


def _term_hit(text_lc: str, term: str) -> bool:
    term = term.lower()
    if _LATIN.match(term):
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text_lc) is not None
    return term in text_lc


@dataclass
class Prescreen:
    hits: dict[str, list[str]] = field(default_factory=dict)
    opt_out_exact: bool = False
    too_long: bool = False
    emoji_only: bool = False
    empty: bool = False
    has_phone_number: bool = False
    short_reply: str | None = None      # option:N | yes | no | thanks | change | greeting | cancel

    @property
    def categories(self) -> list[str]:
        return [c for c in SEVERITY if c in self.hits]

    @property
    def primary(self) -> str | None:
        cats = self.categories
        return cats[0] if cats else None

    def to_dict(self) -> dict[str, Any]:
        return {"hits": self.hits, "opt_out_exact": self.opt_out_exact, "too_long": self.too_long,
                "emoji_only": self.emoji_only, "short_reply": self.short_reply,
                "has_phone_number": self.has_phone_number}


def prescreen(text: str, max_chars: int = 1500) -> Prescreen:
    ps = Prescreen()
    raw = text or ""
    if not raw.strip():
        ps.empty = True
        return ps
    if len(raw) > max_chars:
        ps.too_long = True
    lc = normalise_text(raw)
    cfg = clinic_config()["prescreen"]
    exact = {normalise_text(t) for terms in cfg["opt_out_exact"].values() for t in terms}
    if lc in exact:
        ps.opt_out_exact = True
        ps.hits["opt_out"] = [lc]
        return ps
    for cat in ("clinical", "human_request", "complaint", "billing", "injection", "impersonation", "other_recipient", "abuse"):
        found = [t for terms in cfg[cat].values() for t in terms if _term_hit(lc, t)]
        if found:
            ps.hits[cat] = sorted(set(found))[:6]
    oo = [t for terms in cfg["opt_out_contains"].values() for t in terms if _term_hit(lc, t)]
    if oo:
        ps.hits["opt_out"] = sorted(set(oo))[:4]
    ps.has_phone_number = bool(_PHONE.search(raw))
    if ps.has_phone_number and re.search(r"send|forward|text|message|whatsapp|发|转发|hantar|அனுப்ப", lc):
        ps.hits.setdefault("other_recipient", []).append("<phone number>")
    ps.emoji_only = is_emoji_or_symbols(raw)
    if not ps.hits:
        ps.short_reply = classify_short_reply(raw)
    return ps


# ---------------------------------------------------------------- short replies (no LLM needed)
_SHORT = {
    "yes": ["yes", "yes please", "ok", "okay", "ok sure", "sure", "can", "yup", "yeah", "yep", "alright", "good",
            "好", "好的", "可以", "行", "是", "是的", "好啊", "ya", "boleh", "baik", "ok boleh", "setuju", "ஆம்", "சரி", "சரி சரி"],
    "no": ["no", "no thanks", "no thank you", "nope", "not interested", "not now", "不用", "不用了", "不要", "不需要",
           "tidak", "tak", "tak nak", "tidak mahu", "tidak perlu", "tak perlu", "இல்லை", "வேண்டாம்", "தேவையில்லை"],
    "thanks": ["thanks", "thank you", "thank you so much", "tq", "thx", "ty", "谢谢", "多谢", "谢谢你", "terima kasih",
               "terima kasih banyak", "நன்றி", "மிக்க நன்றி"],
    "change": ["change", "reschedule", "change please", "更改", "改时间", "tukar", "ubah", "மாற்று", "மாற்றவும்"],
    "greeting": ["hi", "hello", "hey", "good morning", "你好", "您好", "hai", "helo", "selamat pagi", "வணக்கம்"],
    "cancel": ["cancel", "cancel appointment", "cancel my appointment", "取消", "取消预约", "batal", "batalkan", "ரத்து", "ரத்து செய்"],
}
_ORDINALS = {
    1: ["1", "one", "first", "the first", "option 1", "no 1", "第一", "第一个", "一", "pertama", "satu", "yang pertama", "முதல்", "முதலாவது", "ஒன்று"],
    2: ["2", "two", "second", "the second", "option 2", "no 2", "第二", "第二个", "二", "kedua", "dua", "yang kedua", "இரண்டாவது", "இரண்டு"],
    3: ["3", "three", "third", "the third", "option 3", "no 3", "第三", "第三个", "三", "ketiga", "tiga", "yang ketiga", "மூன்றாவது", "மூன்று"],
}


def classify_short_reply(text: str) -> str | None:
    t = normalise_text(text).replace("#", "").replace("no.", "no ")
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > 40:
        return None
    for n, forms in _ORDINALS.items():
        if t in forms or re.fullmatch(rf"(option|no|number|pilihan|选项|விருப்பம்)?\s*{n}", t):
            return f"option:{n}"
    for kind, forms in _SHORT.items():
        if t in forms:
            return kind
    return None


# ---------------------------------------------------------------- intent hints (used by the mock LLM + tests)
INFO_TOPICS: dict[str, list[str]] = {
    "hours": ["hours", "open", "opening", "close", "closing", "what time", "operating", "几点", "营业", "开门", "关门", "时间表",
              "buka", "tutup", "waktu operasi", "jam berapa", "திறந்திருக்கும்", "திறக்கும்", "நேரம் என்ன", "எத்தனை மணி"],
    "prices": ["price", "prices", "cost", "how much", "fee", "fees", "charges", "多少钱", "价格", "费用", "价钱", "harga", "berapa",
               "kos", "bayaran", "விலை", "எவ்வளவு", "செலவு"],
    "address": ["where", "address", "location", "located", "directions", "mrt", "在哪", "地址", "怎么去", "di mana", "alamat",
                "lokasi", "எங்கே", "முகவரி", "எப்படி வருவது"],
    "parking": ["park", "parking", "carpark", "停车", "letak kereta", "parkir", "tempat letak", "வாகன நிறுத்த", "பார்க்கிங்"],
    "subsidies": ["chas", "subsidy", "subsidies", "pioneer", "merdeka", "medisave", "津贴", "补贴", "subsidi", "மானியம்"],
    "payment": ["pay", "payment", "paynow", "nets", "credit card", "付款", "付钱", "bayar", "pembayaran", "பணம் செலுத்த", "கட்டண முறை"],
    "languages": ["speak mandarin", "speak malay", "speak tamil", "language", "语言", "bahasa", "மொழி"],
    "accessibility": ["wheelchair", "lift", "stairs", "轮椅", "电梯", "kerusi roda", "lif", "சக்கர நாற்காலி", "லிப்ட்"],
    "what_to_bring": ["bring", "带什么", "带", "bawa", "கொண்டு வர"],
    "children": ["child", "children", "kid", "son", "daughter", "孩子", "小孩", "anak", "குழந்தை"],
}

BOOKING_WORDS = ["book", "appointment", "come in", "come", "slot", "available", "schedule", "visit", "free",
                 "预约", "约", "来", "空", "tempah", "temujanji", "datang", "janji", "முன்பதிவு", "வர", "வருகிறேன்", "சந்திப்பு"]
DECLINE_WORDS = ["no thanks", "not interested", "i'll call", "i will call", "book myself", "not now", "maybe later",
                 "don't need", "do not need", "no need", "already went", "another clinic", "moved", "不用了", "不需要", "以后再说",
                 "不用", "别的诊所", "tak perlu", "tidak perlu", "tidak mahu", "tak nak", "nanti", "klinik lain",
                 "வேண்டாம்", "பிறகு", "தேவையில்லை", "வேறு மருத்துவமனை"]
RESCHEDULE_WORDS = ["change", "reschedule", "another time", "different time", "move my", "postpone", "改", "换时间", "更改", "改期",
                    "tukar", "ubah", "tangguh", "மாற்ற", "வேறு நேரம்"]
CANCEL_WORDS = ["cancel", "取消", "batal", "ரத்து"]
LIMIT_WORDS = ["20 slots", "20 appointments", "all slots", "every slot", "many appointments", "several appointments",
               "multiple appointments", "10 slots", "all the slots", "所有时间", "semua slot", "அனைத்து நேரங்களையும்"]


def detect_info_topic(text: str) -> str | None:
    lc = normalise_text(text)
    best, best_n = None, 0
    for topic, words in INFO_TOPICS.items():
        n = sum(_term_hit(lc, w) for w in words)
        if n > best_n:
            best, best_n = topic, n
    return best


def has_any(text: str, words: list[str]) -> bool:
    lc = normalise_text(text)
    return any(_term_hit(lc, w) for w in words)


# ---------------------------------------------------------------- time preference parser
_WEEKDAY_WORDS: list[tuple[int, list[str]]] = [
    (0, ["monday", "mon", "星期一", "周一", "礼拜一", "isnin", "திங்கள்"]),
    (1, ["tuesday", "tue", "tues", "星期二", "周二", "礼拜二", "selasa", "செவ்வாய்"]),
    (2, ["wednesday", "wed", "星期三", "周三", "礼拜三", "rabu", "புதன்"]),
    (3, ["thursday", "thu", "thur", "thurs", "星期四", "周四", "礼拜四", "khamis", "வியாழன்"]),
    (4, ["friday", "fri", "星期五", "周五", "礼拜五", "jumaat", "jumat", "வெள்ளி"]),
    (5, ["saturday", "sat", "星期六", "周六", "礼拜六", "sabtu", "சனி"]),
    (6, ["sunday", "sun", "星期日", "星期天", "周日", "礼拜天", "ahad", "ஞாயிறு"]),
]
_PARTS = {
    "morning": ["morning", "早上", "上午", "pagi", "காலை"],
    "afternoon": ["afternoon", "lunch", "下午", "中午", "petang", "tengah hari", "மதியம்", "பிற்பகல்"],
    "evening": ["evening", "night", "after work", "晚上", "傍晚", "malam", "lepas kerja", "மாலை", "இரவு"],
}
_TOMORROW = ["tomorrow", "tmr", "tmrw", "明天", "esok", "besok", "நாளை"]
_DAY_AFTER = ["day after tomorrow", "后天", "lusa", "நாளை மறுநாள்"]
_NEXT_WEEK = ["next week", "下周", "下星期", "下个星期", "下礼拜", "minggu depan", "அடுத்த வாரம்"]
_THIS_WEEK = ["this week", "这周", "这个星期", "本周", "minggu ini", "இந்த வாரம்"]
_WEEKEND = ["weekend", "周末", "hujung minggu", "வார இறுதி"]
_ASAP = ["asap", "soonest", "earliest", "as soon as possible", "尽快", "最早", "secepat mungkin", "segera", "விரைவில்"]
_MONTHS_EN = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_time_preference(text: str, today: date | None = None) -> dict[str, Any]:
    """Map free text to FindSlots args. Always returns something valid (defaults to the next 14 days, any time)."""
    today = today or clock.today()
    lc = normalise_text(text)
    horizon = clinic_config()["calendar"]["horizon_days"]
    earliest, latest = today + timedelta(days=1), today + timedelta(days=horizon)
    part = "any"
    weekdays: list[int] = []
    matched = False
    for p, words in _PARTS.items():
        if any(_term_hit(lc, w) for w in words):
            part, matched = p, True
            break
    clock_time = re.search(r"(\d{1,2})(?::\d{2})?\s*(am|pm)\b", lc)
    if clock_time and part == "any":
        hour = int(clock_time.group(1)) % 12 + (12 if clock_time.group(2) == "pm" else 0)
        part = "morning" if hour < 13 else ("afternoon" if hour < 18 else "evening")
        matched = True
    if any(_term_hit(lc, w) for w in _DAY_AFTER):
        earliest = latest = today + timedelta(days=2)
        matched = True
    elif any(_term_hit(lc, w) for w in _TOMORROW):
        earliest = latest = today + timedelta(days=1)
        matched = True
    next_week = any(_term_hit(lc, w) for w in _NEXT_WEEK)
    this_week = any(_term_hit(lc, w) for w in _THIS_WEEK)
    for wd, words in _WEEKDAY_WORDS:
        if any(_term_hit(lc, w) for w in words):
            weekdays.append(wd)
    if weekdays and earliest != latest:
        matched = True
        if len(weekdays) == 1:
            target = today + timedelta(days=((weekdays[0] - today.weekday()) % 7 or 7))
            if next_week and target.isocalendar()[1] == today.isocalendar()[1]:
                target += timedelta(days=7)      # "Tuesday next week" said on a Monday → the Tuesday of next week
            earliest = latest = target
            weekdays = []
    elif next_week:
        start = today + timedelta(days=(7 - today.weekday()))
        earliest, latest, matched = start, start + timedelta(days=6), True
    elif this_week:
        earliest, latest, matched = today + timedelta(days=1), today + timedelta(days=(6 - today.weekday())), True
    if any(_term_hit(lc, w) for w in _WEEKEND):
        weekdays, matched = [5], True
    m = re.search(r"(\d{1,2})\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", lc) or None
    if m:
        try:
            d = date(today.year, _MONTHS_EN[m.group(2)], int(m.group(1)))
            earliest = latest = d if d > today else date(today.year + 1, d.month, d.day)
            matched = True
        except ValueError:
            pass
    m2 = re.search(r"(\d{1,2})月(\d{1,2})[日号]", text)
    if m2:
        try:
            d = date(today.year, int(m2.group(1)), int(m2.group(2)))
            earliest = latest = d
            matched = True
        except ValueError:
            pass
    if any(_term_hit(lc, w) for w in _ASAP):
        matched = True
    earliest = max(earliest, today + timedelta(days=1)) if earliest <= today else earliest
    if latest < earliest:
        latest = earliest
    return {"earliest": earliest.isoformat(), "latest": latest.isoformat(), "part_of_day": part,
            "weekdays": weekdays or None, "_matched": matched}


def mentions_time(text: str) -> bool:
    return bool(parse_time_preference(text).get("_matched"))
