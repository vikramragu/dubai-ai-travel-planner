"""Application settings, loaded from environment variables and an optional ``.env`` file.

See ``.env.example`` for every supported variable and ``docs/architecture.md`` §14.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

AgentRole = Literal["orchestrator", "destination", "logistics", "budget", "review"]


AGENT_ROLES: tuple[AgentRole, ...] = (
    "orchestrator",
    "destination",
    "logistics",
    "budget",
    "review",
)

# A provider's model when a role switches provider without naming one.
DEFAULT_MODELS = {
    "groq": "openai/gpt-oss-120b",
    "google_genai": "gemini-flash-latest",
    "anthropic": "claude-sonnet-5-5",
}


class DataMode(StrEnum):
    """Which data providers the tools may use."""

    LOCAL = "local"  # curated SQLite/JSON only — fully offline and reproducible
    HYBRID = "hybrid"  # free live sources first, local fallback
    LIVE = "live"  # free live sources preferred; local used only on failure


class Settings(BaseSettings):
    """Typed configuration for the whole application."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,  # `LLM_MODEL_REVIEW=` in .env means "not set"
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ LLM
    # Defaults (decision of 2026-11-17): Groq's gpt-oss-120b for the orchestrator, destination,
    # logistics and budget agents, and Gemini for the reviewer (a second model family checks the
    # plan). Any role can be moved to another provider with LLM_PROVIDER_<ROLE>/LLM_MODEL_<ROLE>.
    llm_provider: str = "groq"
    llm_model: str = "openai/gpt-oss-120b"
    llm_provider_orchestrator: str | None = None
    llm_provider_destination: str | None = None
    llm_provider_logistics: str | None = None
    llm_provider_budget: str | None = None
    llm_provider_review: str | None = "google_genai"
    llm_model_orchestrator: str | None = None
    llm_model_destination: str | None = None
    llm_model_logistics: str | None = None
    llm_model_budget: str | None = None
    llm_model_review: str | None = "gemini-flash-latest"
    groq_api_key: SecretStr | None = None
    google_api_key: SecretStr | None = None  # Gemini; GEMINI_API_KEY also works
    gemini_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None

    # ------------------------------------------------------------------ data
    data_mode: DataMode = DataMode.LOCAL
    db_path: Path = Path("data/dubai.sqlite")
    use_overpass: bool = True
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    use_osrm: bool = True
    osrm_base_url: str = "http://localhost:5000"
    use_open_meteo: bool = True
    open_meteo_url: str = "https://api.open-meteo.com/v1/forecast"
    amadeus_client_id: SecretStr | None = None
    amadeus_client_secret: SecretStr | None = None
    http_user_agent: str = "dubai-travel-planner/0.1 (educational demo)"
    http_timeout_s: float = Field(default=10.0, gt=0)

    # ------------------------------------------------------------------ planning
    max_revisions: int = Field(default=3, ge=0, le=10)
    contingency_pct: float = Field(default=10.0, ge=0, le=50)
    # AED per unit of foreign currency, for budgets given in another currency. USD and SAR
    # follow the official pegs; the others are approximate and should be updated before a demo.
    currency_rates: dict[str, float] = Field(
        default_factory=lambda: {
            "USD": 3.6725,  # AED is pegged to the US dollar
            "SAR": 0.9793,  # via the SAR/USD peg of 3.75
            "EUR": 4.0,  # approximate
            "GBP": 4.7,  # approximate
            "INR": 0.042,  # approximate
        }
    )

    # ------------------------------------------------------------------ misc
    cache_dir: Path = Path(".cache")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    def provider_for(self, role: AgentRole) -> str:
        """Provider for an agent role, falling back to ``llm_provider``."""
        override: str | None = getattr(self, f"llm_provider_{role}")
        return (override or self.llm_provider).lower()

    def model_for(self, role: AgentRole) -> str:
        """Model name for an agent role, falling back to ``llm_model``.

        A role on its own provider without its own model uses that provider's default model
        (``llm_model`` belongs to ``llm_provider``).
        """
        override: str | None = getattr(self, f"llm_model_{role}")
        if override:
            return override
        if self.provider_for(role) != self.llm_provider.lower():
            return DEFAULT_MODELS.get(self.provider_for(role), self.llm_model)
        return self.llm_model

    def chat_model_id(self, role: AgentRole) -> str:
        """``provider:model`` string for LangChain's ``init_chat_model``."""
        return f"{self.provider_for(role)}:{self.model_for(role)}"

    @property
    def amadeus_enabled(self) -> bool:
        return self.amadeus_client_id is not None and self.amadeus_client_secret is not None

    @property
    def live_data_enabled(self) -> bool:
        return self.data_mode is not DataMode.LOCAL


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton. Call ``get_settings.cache_clear()`` in tests."""
    return Settings()
