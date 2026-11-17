"""Phase 0 exit criteria: settings load correctly with and without a .env file."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.config import DataMode, Settings, get_settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove any real environment variables that would leak into Settings."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def load(env_file: Path | None = None) -> Settings:
    # `_env_file` is pydantic-settings' per-instance override; None disables .env loading.
    return Settings(_env_file=env_file)


def test_defaults_without_env_file() -> None:
    s = load()
    assert s.llm_provider == "groq"
    assert s.chat_model_id("orchestrator") == "groq:openai/gpt-oss-120b"
    assert s.chat_model_id("review") == "google_genai:gemini-flash-latest"
    assert s.data_mode is DataMode.LOCAL
    assert s.max_revisions == 3
    assert s.contingency_pct == 10.0
    assert s.anthropic_api_key is None
    assert not s.live_data_enabled
    assert not s.amadeus_enabled


def test_loads_values_from_env_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "LLM_PROVIDER=openai\nLLM_MODEL=gpt-test\nDATA_MODE=hybrid\nMAX_REVISIONS=5\n"
        "ANTHROPIC_API_KEY=sk-secret\n"
    )
    s = load(env)
    assert s.llm_provider == "openai"
    assert s.llm_model == "gpt-test"
    assert s.data_mode is DataMode.HYBRID
    assert s.live_data_enabled
    assert s.max_revisions == 5
    assert s.anthropic_api_key is not None
    assert s.anthropic_api_key.get_secret_value() == "sk-secret"


def test_environment_overrides_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text("MAX_REVISIONS=5\n")
    monkeypatch.setenv("MAX_REVISIONS", "1")
    assert load(env).max_revisions == 1


def test_empty_values_are_treated_as_unset(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("LLM_MODEL_BUDGET=\nANTHROPIC_API_KEY=\nAMADEUS_CLIENT_ID=\n")
    s = load(env)
    assert s.llm_model_budget is None
    assert s.anthropic_api_key is None
    assert s.amadeus_client_id is None


def test_env_example_is_loadable() -> None:
    example = Path(__file__).resolve().parents[2] / ".env.example"
    s = load(example)
    assert s.data_mode is DataMode.LOCAL
    assert s.chat_model_id("budget") == "groq:openai/gpt-oss-120b"
    assert s.chat_model_id("review") == "google_genai:gemini-flash-latest"


def test_per_role_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_MODEL", "base-model")
    monkeypatch.setenv("LLM_PROVIDER_REVIEW", "anthropic")
    monkeypatch.setenv("LLM_MODEL_REVIEW", "strong-model")
    s = load()
    assert s.model_for("review") == "strong-model"
    assert s.model_for("destination") == "base-model"
    assert s.chat_model_id("review") == "anthropic:strong-model"


def test_role_on_another_provider_uses_its_default_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER_BUDGET", "google_genai")
    s = load()
    assert s.chat_model_id("budget") == "google_genai:gemini-flash-latest"
    assert s.chat_model_id("logistics") == "groq:openai/gpt-oss-120b"


def test_gemini_key_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    from core.llm import role_configured

    assert role_configured("review", load())


def test_amadeus_enabled_needs_both_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMADEUS_CLIENT_ID", "id")
    assert not load().amadeus_enabled
    monkeypatch.setenv("AMADEUS_CLIENT_SECRET", "secret")
    assert load().amadeus_enabled


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DATA_MODE", "cloud"),
        ("MAX_REVISIONS", "-1"),
        ("MAX_REVISIONS", "11"),
        ("CONTINGENCY_PCT", "75"),
        ("LOG_LEVEL", "VERBOSE"),
    ],
)
def test_invalid_values_are_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        load()


def test_secrets_are_masked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-very-secret")
    assert "sk-very-secret" not in repr(load())


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()
