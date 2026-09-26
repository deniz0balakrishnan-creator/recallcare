"""Deterministic output validator. Runs on EVERY outbound message, whoever wrote it, before any channel send."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from app import db
from app.core.lang import lang_matches
from app.settings import clinic_config, settings

# Clinical-advice patterns (medication, dosage, diagnosis). Deliberately specific so logistics text passes.
_ADVICE = [re.compile(p, re.I) for p in (
    r"\b(take|try|use|apply|swallow)\s+(some\s+|an?\s+)?(ibuprofen|paracetamol|panadol|nurofen|antibiotics?|amoxicillin|"
    r"painkillers?|pain\s?killers?|mouthwash|clove oil|aspirin)\b",
    r"\b\d+(\.\d+)?\s?mg\b",
    r"\b(you\s+(may|might|probably|likely|could)\s+have|it\s+(sounds|looks)\s+like\s+(an?\s+)?"
    r"(infection|abscess|gum disease|cavity|decay|periodontitis))",
    r"\b(rinse|gargle)\s+with\s+(warm\s+)?salt\s*water\b",
    r"\bno need to (see|visit) (a|the) dentist\b",
    r"(服用|吃点?|用)(止痛药|抗生素|布洛芬|扑热息痛|消炎药|阿莫西林)",
    r"(盐水漱口|毫克|你可能(有|患|是).{0,6}(感染|发炎|牙周病|蛀牙|脓肿))",
    r"(ambil|makan|telan|guna)\s+(ubat tahan sakit|panadol|antibiotik|ibuprofen|parasetamol)",
    r"(berkumur dengan air garam|anda mungkin (mengalami|ada|menghidapi) (jangkitan|penyakit gusi|abses))",
    r"(வலி நிவாரணி|ஆன்டிபயாடிக்|ஆண்டிபயாடிக்|பாராசிட்டமால்)\s*(மாத்திரை)?\s*(சாப்பிடுங்கள்|எடுத்துக்கொள்ளுங்கள்|சாப்பிடவும்)",
    r"உப்பு நீரில் வாய் கொப்பளி",
)]
_MONEY = re.compile(r"(?:S\$|SGD\s?|\$)\s?(\d{1,5})|(\d{1,5})\s?(?:新元|新币|元|dolar|வெள்ளி)")
_SYNTHETIC_PHONE = re.compile(r"^\+655550\d{4}$")


@dataclass
class Check:
    ok: bool
    reasons: list[str] = field(default_factory=list)


@lru_cache(maxsize=1)
def _published_amounts() -> frozenset[str]:
    info = " ".join(str(v) for v in clinic_config()["info_sheet"].values())
    return frozenset(m.group(1) or m.group(2) for m in _MONEY.finditer(info))


def _other_patient_identifiers(patient_id: int) -> list[str]:
    ids: list[str] = []
    for r in db.q("SELECT full_name, phone FROM patients WHERE id != ?", (patient_id,)):
        name = r["full_name"]
        ids.append(r["phone"])
        ids.append(re.sub(r"\s*\(.*?\)", "", name).strip().lower())
        m = re.search(r"\((.*?)\)", name)
        if m:
            ids.append(m.group(1))
    return [i for i in ids if len(i) >= 5]


def effective_phone(patient: dict) -> str:
    return settings.phone_overrides.get(patient["id"], patient["phone"])


def recipient_allowed(phone: str, channel: str) -> bool:
    if phone in settings.allowlist:
        return True
    # The simulator also accepts the synthetic +65 5550 xxxx range (never routable on the real channel).
    return channel == "simulator" and bool(_SYNTHETIC_PHONE.match(phone))


def validate_outbound(*, patient: dict, ctx_patient_id: int, text: str, lang: str, channel: str,
                      kind: str = "text", allow_opted_out: bool = False) -> Check:
    reasons: list[str] = []
    if patient["id"] != ctx_patient_id:
        reasons.append("recipient is not the current patient")
    if not recipient_allowed(effective_phone(patient), channel):
        reasons.append("recipient not on allowlist")
    if not patient["whatsapp_consent"]:
        reasons.append("no WhatsApp consent on record")
    if patient["opted_out"] and not allow_opted_out:
        reasons.append("patient has opted out")
    if len(text) > settings.max_outbound_chars:
        reasons.append(f"message longer than {settings.max_outbound_chars} chars")
    if kind == "text":
        lc = text.lower()
        for ident in _other_patient_identifiers(patient["id"]):
            if ident.lower() in lc or (ident.startswith("+") and ident[1:] in re.sub(r"\D", "", text)):
                reasons.append("contains another patient's identifier")
                break
        if any(p.search(text) for p in _ADVICE):
            reasons.append("clinical-advice pattern")
        amounts = {m.group(1) or m.group(2) for m in _MONEY.finditer(text)}
        if amounts - _published_amounts():
            reasons.append("price not in the published info sheet")
        if not lang_matches(text, lang):
            reasons.append(f"language does not match patient preference ({lang})")
    return Check(ok=not reasons, reasons=reasons)
