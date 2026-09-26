import pytest

from app import clock
from app.llm import factory
from app.llm.mock import MockLLM
from app.settings import settings


@pytest.fixture(autouse=True)
def fresh_env(tmp_path, monkeypatch):
    """Every test: its own SQLite files, mock LLM, simulator channel, frozen Saturday-morning clock."""
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings, "checkpoint_db_path", str(tmp_path / "cp.db"))
    monkeypatch.setattr(settings, "llm_usage_db", str(tmp_path / "usage.db"))
    monkeypatch.setattr(settings, "llm_min_interval_ms", 0)
    monkeypatch.setattr(settings, "whatsapp_allowlist", "+6590000001")
    monkeypatch.setattr(settings, "demo_phone_map", "")
    monkeypatch.setattr(settings, "llm_provider", "mock")
    monkeypatch.setattr(settings, "channel", "simulator")
    factory.set_provider(None)
    MockLLM.fail_next = 0
    clock.freeze("2026-09-26T10:00:00")
    yield
    clock.reset()


@pytest.fixture
def seeded():
    from app.seed import seed
    return seed()
