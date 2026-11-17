"""LLM layer: model factory, structured output with one retry, offline detection."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel, SecretStr

import core.llm as llm_mod
from core.config import Settings
from core.llm import (
    LangChainLLM,
    LLMOutputError,
    LLMUnavailableError,
    get_llm,
    llm_configured,
    make_llm,
    role_configured,
)


class Answer(BaseModel):
    value: int


class _Runnable:
    def __init__(self, outputs: list[Any]) -> None:
        self.outputs = outputs
        self.prompts: list[str] = []

    def invoke(self, messages: list[Any]) -> Any:
        self.prompts.append(messages[-1].content)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


class _Model:
    def __init__(self, runnable: _Runnable) -> None:
        self.runnable = runnable

    def with_structured_output(
        self, schema: type, include_raw: bool = False, **kwargs: Any
    ) -> _Runnable:
        assert include_raw
        self.kwargs = kwargs
        return self.runnable


def _client(outputs: list[Any]) -> tuple[LangChainLLM, _Runnable]:
    runnable = _Runnable(outputs)
    client = LangChainLLM(Settings(_env_file=None, groq_api_key=SecretStr("test-key")))
    client._models[("budget", None)] = _Model(runnable)  # type: ignore[assignment]
    return client, runnable


def _raw(tin: int = 10, tout: int = 5) -> AIMessage:
    return AIMessage(
        content="",
        usage_metadata={"input_tokens": tin, "output_tokens": tout, "total_tokens": tin + tout},
    )


def test_structured_success_counts_tokens() -> None:
    client, _ = _client([{"parsed": Answer(value=3), "parsing_error": None, "raw": _raw()}])
    assert client.structured("budget", Answer, system="s", user="u") == Answer(value=3)
    assert (client.usage.calls, client.usage.tokens_in, client.usage.tokens_out) == (1, 10, 5)


def test_retry_sends_the_error_back() -> None:
    client, runnable = _client(
        [
            {"parsed": None, "parsing_error": ValueError("value missing"), "raw": _raw()},
            {"parsed": {"value": 7}, "parsing_error": None, "raw": _raw()},
        ]
    )
    assert client.structured("budget", Answer, system="s", user="question").value == 7
    assert client.usage.calls == 2
    assert "value missing" in runnable.prompts[1] and runnable.prompts[1].startswith("question")


def test_gives_up_after_one_retry() -> None:
    bad = {"parsed": None, "parsing_error": ValueError("nope"), "raw": _raw()}
    client, _ = _client([bad, dict(bad)])
    with pytest.raises(LLMOutputError, match="Answer"):
        client.structured("budget", Answer, system="s", user="u")
    assert client.usage.failures == 1


def test_invalid_parsed_dict_is_retried() -> None:
    client, _ = _client(
        [
            {"parsed": {"value": "not a number"}, "parsing_error": None, "raw": _raw()},
            {"parsed": {"value": 1}, "parsing_error": None, "raw": _raw()},
        ]
    )
    assert client.structured("budget", Answer, system="s", user="u").value == 1


def test_provider_errors_become_unavailable() -> None:
    client, _ = _client([ConnectionError("down")])
    with pytest.raises(LLMUnavailableError, match="ConnectionError"):
        client.structured("budget", Answer, system="s", user="u")


def test_get_llm_uses_role_model_and_temperature(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_init(model_id: str, **kwargs: Any) -> str:
        seen.update(model_id=model_id, **kwargs)
        return "model"

    import langchain.chat_models

    monkeypatch.setattr(langchain.chat_models, "init_chat_model", fake_init)
    s = Settings(
        _env_file=None,
        llm_provider="anthropic",
        llm_model="base-model",
        llm_provider_review=None,
        llm_model_review="review-model",
        anthropic_api_key=SecretStr("k"),
    )
    assert get_llm("review", settings=s) == "model"  # type: ignore[comparison-overlap]
    assert seen["model_id"] == "anthropic:review-model"
    assert seen["temperature"] == llm_mod.ROLE_TEMPERATURE["review"]
    assert seen["api_key"] == "k"
    get_llm("orchestrator", temperature=0.6, settings=s)
    assert seen["model_id"] == "anthropic:base-model" and seen["temperature"] == 0.6


KEY_VARS = ("GROQ_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY")


def test_offline_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None)
    assert not llm_configured(s)
    assert make_llm(s) is None
    assert llm_configured(Settings(_env_file=None, llm_provider="ollama"))
    assert isinstance(make_llm(Settings(_env_file=None, groq_api_key=SecretStr("k"))), LangChainLLM)
    monkeypatch.setenv("GROQ_API_KEY", "from-env")
    assert llm_configured(Settings(_env_file=None))


def test_roles_are_configured_per_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None, groq_api_key=SecretStr("k"))
    assert role_configured("budget", s) and not role_configured("review", s)
    assert llm_configured(s) and not llm_configured(s, every_role=True)
    both = s.model_copy(update={"google_api_key": SecretStr("g")})
    assert llm_configured(both, every_role=True)


def test_role_without_a_key_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    client = LangChainLLM(Settings(_env_file=None, groq_api_key=SecretStr("k")))
    with pytest.raises(LLMUnavailableError, match="google_genai"):
        client.structured("review", Answer, system="s", user="u")


def test_groq_uses_json_schema_output() -> None:
    client, _ = _client([{"parsed": Answer(value=1), "parsing_error": None, "raw": _raw()}])
    client.structured("budget", Answer, system="s", user="u")
    model = client._models[("budget", None)]
    assert model.kwargs == {"method": "json_schema"}  # type: ignore[attr-defined]


def test_real_model_objects_are_built() -> None:
    """The Groq and Gemini integrations are installed and accept our settings (no network)."""
    s = Settings(_env_file=None, groq_api_key=SecretStr("k"), google_api_key=SecretStr("g"))
    groq = get_llm("budget", settings=s)
    gemini = get_llm("review", settings=s)
    assert type(groq).__name__ == "ChatGroq" and "gpt-oss-120b" in groq.model_name  # type: ignore[attr-defined]
    assert type(gemini).__name__ == "ChatGoogleGenerativeAI"
