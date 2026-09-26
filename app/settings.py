"""Runtime settings (env) and clinic configuration (YAML).

Env is read once at import via python-dotenv; tests override attributes directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    v = _env(name, "true" if default else "false").lower()
    return v in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


@dataclass
class Settings:
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "mock"))
    gateway_url: str = field(default_factory=lambda: _env("LLM_GATEWAY_URL", "https://api.softwaresystems.app"))
    gateway_api_key: str = field(default_factory=lambda: _env("LLM_GATEWAY_API_KEY"))
    gateway_model: str = field(default_factory=lambda: _env("LLM_MODEL", "global.anthropic.claude-sonnet-4-5-20250929-v1:0"))
    gateway_system_in_user: bool = field(default_factory=lambda: _env_bool("LLM_GATEWAY_SYSTEM_IN_USER"))
    llm_max_request_bytes: int = field(default_factory=lambda: _env_int("LLM_MAX_REQUEST_BYTES", 7500))
    llm_timeout_s: int = field(default_factory=lambda: _env_int("LLM_TIMEOUT_S", 60))
    llm_max_output_tokens: int = field(default_factory=lambda: _env_int("LLM_MAX_OUTPUT_TOKENS", 600))
    openrouter_api_key: str = field(default_factory=lambda: _env("OPENROUTER_API_KEY"))
    openrouter_model: str = field(default_factory=lambda: _env("OPENROUTER_MODEL", "anthropic/claude-haiku-4.5"))
    openrouter_base_url: str = field(default_factory=lambda: _env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"))

    channel: str = field(default_factory=lambda: _env("CHANNEL", "simulator"))
    whatsapp_allowlist: str = field(default_factory=lambda: _env("WHATSAPP_ALLOWLIST"))
    demo_phone_map: str = field(default_factory=lambda: _env("DEMO_PHONE_MAP"))
    wa_access_token: str = field(default_factory=lambda: _env("WHATSAPP_ACCESS_TOKEN"))
    wa_phone_number_id: str = field(default_factory=lambda: _env("WHATSAPP_PHONE_NUMBER_ID"))
    wa_business_account_id: str = field(default_factory=lambda: _env("WHATSAPP_BUSINESS_ACCOUNT_ID"))
    wa_graph_version: str = field(default_factory=lambda: _env("WHATSAPP_GRAPH_VERSION"))
    wa_app_secret: str = field(default_factory=lambda: _env("WHATSAPP_APP_SECRET"))
    wa_verify_token: str = field(default_factory=lambda: _env("WHATSAPP_VERIFY_TOKEN"))
    wa_template_name: str = field(default_factory=lambda: _env("WHATSAPP_TEMPLATE_NAME", "recall_reminder"))
    wa_template_approved: bool = field(default_factory=lambda: _env_bool("WHATSAPP_TEMPLATE_APPROVED"))

    dashboard_user: str = field(default_factory=lambda: _env("DASHBOARD_USER", "staff"))
    dashboard_password: str = field(default_factory=lambda: _env("DASHBOARD_PASSWORD"))
    session_secret: str = field(default_factory=lambda: _env("SESSION_SECRET"))
    database_path: str = field(default_factory=lambda: _env("DATABASE_PATH", "data/recallcare.db"))
    checkpoint_db_path: str = field(default_factory=lambda: _env("CHECKPOINT_DB_PATH", "data/checkpoints.db"))
    clinic_config: str = field(default_factory=lambda: _env("CLINIC_CONFIG", "config/clinics/dental.yaml"))
    as_of_date: str = field(default_factory=lambda: _env("AS_OF_DATE"))
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL"))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    inbound_rate_per_min: int = field(default_factory=lambda: _env_int("INBOUND_RATE_PER_MIN", 10))
    auto_jobs: bool = field(default_factory=lambda: _env_bool("AUTO_JOBS", True))

    # Hard caps (not env-tunable on purpose)
    max_steps_per_run: int = 8
    message_window: int = 8          # turns kept in short-term memory
    max_outbound_chars: int = 1000
    max_inbound_chars: int = 1500    # longer inbound messages are escalated, never sent to a model

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else ROOT / p

    @property
    def allowlist(self) -> set[str]:
        return {normalise_phone(x) for x in self.whatsapp_allowlist.split(",") if x.strip()}

    @property
    def phone_overrides(self) -> dict[int, str]:
        out: dict[int, str] = {}
        for pair in self.demo_phone_map.split(","):
            if ":" in pair:
                pid, phone = pair.split(":", 1)
                if pid.strip().isdigit():
                    out[int(pid.strip())] = normalise_phone(phone)
        return out


def normalise_phone(raw: str) -> str:
    """E.164-ish normalisation: keep leading + and digits only."""
    raw = raw.strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    return f"+{digits}" if digits else ""


settings = Settings()


@lru_cache(maxsize=8)
def _load_yaml(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def clinic_config(path: str | None = None) -> dict[str, Any]:
    return _load_yaml(str(settings.path(path or settings.clinic_config)))


def messages_config() -> dict[str, Any]:
    return _load_yaml(str(ROOT / "config" / "messages.yaml"))


def templates_config() -> dict[str, Any]:
    return _load_yaml(str(ROOT / "config" / "whatsapp_templates.yaml"))
