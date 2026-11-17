"""Provider-agnostic LLM layer (architecture.md §10).

- ``get_llm(role)`` builds a LangChain chat model with ``init_chat_model("provider:model")``,
  so switching provider or model is a configuration change (``LLM_PROVIDER``, ``LLM_MODEL``,
  and per role ``LLM_PROVIDER_<ROLE>`` / ``LLM_MODEL_<ROLE>``). Defaults: Groq gpt-oss-120b for
  every agent except the reviewer, which uses Gemini.
- Agents talk to an :class:`LLMClient`: one method, :meth:`LLMClient.structured`, that returns
  a validated Pydantic object. :class:`LangChainLLM` implements it with
  ``with_structured_output`` and one retry that sends back the validation error. Tests use a
  stub client with canned outputs.
- ``make_llm()`` returns ``None`` when no API key is configured; agents then use their
  deterministic fallbacks, so the app runs offline.
"""

from __future__ import annotations

import json
import os
import warnings
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError

from core.config import AGENT_ROLES, AgentRole, Settings, get_settings

M = TypeVar("M", bound=BaseModel)

# Some Gemini models use fixed sampling and ignore `temperature`; the warning is expected noise.
warnings.filterwarnings("ignore", message=r"Model '.*' uses fixed sampling defaults")

# Low temperature for parsing, budget and review; a little more for ranking; NARRATIVE for prose.
ROLE_TEMPERATURE: dict[AgentRole, float] = {
    "orchestrator": 0.0,
    "destination": 0.3,
    "logistics": 0.3,
    "budget": 0.2,
    "review": 0.2,
}
NARRATIVE_TEMPERATURE = 0.6

_KEY_ENV = {
    "groq": ("GROQ_API_KEY",),
    "google_genai": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
}
_KEY_FIELD = {
    "groq": ("groq_api_key",),
    "google_genai": ("google_api_key", "gemini_api_key"),
    "anthropic": ("anthropic_api_key",),
    "openai": ("openai_api_key",),
}
_KEYLESS = {"ollama"}  # local models
# ``with_structured_output`` method per provider. Groq's gpt-oss models support JSON-schema
# output (not strict: strict mode needs every field required and no free-form dicts); the
# others use their integration's default.
STRUCTURED_METHOD = {"groq": "json_schema"}


class LLMError(RuntimeError):
    """The LLM could not produce a usable answer; callers fall back to deterministic code."""


class LLMOutputError(LLMError):
    """The output still failed validation after the retry."""


class LLMUnavailableError(LLMError):
    """The provider call itself failed (network, auth, quota, timeout)."""


@dataclass
class LLMUsage:
    calls: int = 0
    failures: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


class LLMClient(Protocol):
    usage: LLMUsage

    def structured(
        self,
        role: AgentRole,
        schema: type[M],
        *,
        system: str,
        user: str,
        temperature: float | None = None,
    ) -> M:
        """Ask for an answer matching ``schema``. Raises :class:`LLMError` on failure."""
        ...


def _api_key(settings: Settings, provider: str) -> str | None:
    for field in _KEY_FIELD.get(provider, ()):
        secret = getattr(settings, field)
        if secret is not None:
            return str(secret.get_secret_value())
    for env in _KEY_ENV.get(provider, ()):
        if os.environ.get(env):
            return os.environ[env]
    return None


def role_configured(role: AgentRole, settings: Settings | None = None) -> bool:
    """True when the provider of ``role`` can be called (local Ollama needs no key)."""
    settings = settings or get_settings()
    provider = settings.provider_for(role)
    return provider in _KEYLESS or _api_key(settings, provider) is not None


def llm_configured(settings: Settings | None = None, *, every_role: bool = False) -> bool:
    """True when at least one agent role (or, with ``every_role``, all of them) has an LLM."""
    settings = settings or get_settings()
    check = all if every_role else any
    return check(role_configured(r, settings) for r in AGENT_ROLES)


def provider_kwargs(provider: str, model: str, settings: Settings) -> dict[str, Any]:
    """Client options per provider: retries and a timeout everywhere; for Groq's gpt-oss also
    an output cap and low reasoning effort (both count towards the tokens-per-minute limit)."""
    out: dict[str, Any] = {
        "max_retries": settings.llm_max_retries,
        "timeout": settings.llm_timeout_s,
    }
    if provider == "groq":
        out["max_tokens"] = settings.llm_max_tokens
        if "gpt-oss" in model:  # reasoning models; others reject the option
            out["reasoning_effort"] = settings.llm_reasoning_effort
    return out


def get_llm(
    role: AgentRole, *, temperature: float | None = None, settings: Settings | None = None
) -> BaseChatModel:
    """A chat model for an agent role (per-role model override, per-role temperature)."""
    from langchain.chat_models import init_chat_model

    settings = settings or get_settings()
    provider = settings.provider_for(role)
    kwargs: dict[str, Any] = {
        "temperature": ROLE_TEMPERATURE[role] if temperature is None else temperature,
        **provider_kwargs(provider, settings.model_for(role), settings),
    }
    key = _api_key(settings, provider)
    if key is not None:
        kwargs["api_key"] = key
    model: BaseChatModel = init_chat_model(settings.chat_model_id(role), **kwargs)
    return model


def _usage(raw: object) -> tuple[int, int]:
    meta = getattr(raw, "usage_metadata", None) or {}
    return int(meta.get("input_tokens", 0)), int(meta.get("output_tokens", 0))


class LangChainLLM:
    """:class:`LLMClient` backed by LangChain chat models."""

    def __init__(self, settings: Settings | None = None, *, retries: int = 1) -> None:
        self.settings = settings or get_settings()
        self.retries = retries
        self.usage = LLMUsage()
        self._models: dict[tuple[AgentRole, float | None], BaseChatModel] = {}

    def _model(self, role: AgentRole, temperature: float | None) -> BaseChatModel:
        key = (role, temperature)
        if key not in self._models:
            self._models[key] = get_llm(role, temperature=temperature, settings=self.settings)
        return self._models[key]

    def structured(
        self,
        role: AgentRole,
        schema: type[M],
        *,
        system: str,
        user: str,
        temperature: float | None = None,
    ) -> M:
        if not role_configured(role, self.settings):
            self.usage.failures += 1
            raise LLMUnavailableError(f"no API key for {self.settings.provider_for(role)} ({role})")
        kwargs: dict[str, Any] = {"include_raw": True}
        method = STRUCTURED_METHOD.get(self.settings.provider_for(role))
        if method is not None:
            kwargs["method"] = method
        runnable = self._model(role, temperature).with_structured_output(schema, **kwargs)
        prompt = user
        last_error = "unknown error"
        for _ in range(1 + self.retries):
            messages: list[BaseMessage] = [SystemMessage(system), HumanMessage(prompt)]
            try:
                out = runnable.invoke(messages)
            except Exception as exc:  # provider/network errors vary by package
                self.usage.failures += 1
                raise LLMUnavailableError(f"{type(exc).__name__}: {exc}") from exc
            self.usage.calls += 1
            tin, tout = _usage(out.get("raw") if isinstance(out, dict) else None)
            self.usage.tokens_in += tin
            self.usage.tokens_out += tout
            parsed = out.get("parsed") if isinstance(out, dict) else out
            error = out.get("parsing_error") if isinstance(out, dict) else None
            if parsed is not None and error is None:
                try:
                    return parsed if isinstance(parsed, schema) else schema.model_validate(parsed)
                except ValidationError as exc:
                    error = exc
            last_error = str(error)[:2000]
            # Retry once with the validation error appended (a fresh turn, so providers that
            # require tool results after tool calls are not confused).
            prompt = (
                f"{user}\n\nYour previous answer was rejected:\n{last_error}\n"
                "Answer again, following the schema exactly."
            )
        self.usage.failures += 1
        raise LLMOutputError(f"invalid {schema.__name__} after retry: {last_error[:300]}")


class ScriptedLLM:
    """An :class:`LLMClient` with canned answers keyed by schema class name (tests and demos).

    An answer may be a dict or model, a callable taking the decoded user message (JSON when it
    parses), or an exception to raise. A schema with no answer raises :class:`LLMOutputError`,
    so the agent falls back to its deterministic path.
    """

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.answers = answers or {}
        self.usage = LLMUsage()
        self.calls: list[tuple[str, str, str, str]] = []  # (role, schema, system, user)

    def structured(
        self,
        role: AgentRole,
        schema: type[M],
        *,
        system: str,
        user: str,
        temperature: float | None = None,
    ) -> M:
        self.calls.append((role, schema.__name__, system, user))
        self.usage.calls += 1
        answer = self.answers.get(schema.__name__)
        if answer is None:
            raise LLMOutputError(f"no canned answer for {schema.__name__}")
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            try:
                payload: Any = json.loads(user)
            except ValueError:
                payload = user
            answer = answer(payload)
        return answer if isinstance(answer, schema) else schema.model_validate(answer)


def make_llm(settings: Settings | None = None) -> LLMClient | None:
    """The configured LLM client, or ``None`` when no role has a provider key (offline mode).

    A role whose provider has no key raises :class:`LLMUnavailableError`, so that agent falls
    back to its deterministic path while the others still use their LLM.
    """
    settings = settings or get_settings()
    return LangChainLLM(settings) if llm_configured(settings) else None
