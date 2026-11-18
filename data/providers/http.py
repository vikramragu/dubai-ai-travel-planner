"""Shared HTTP plumbing for the live providers (research.md §3.4).

- **Cache:** responses are kept on disk (``diskcache`` in ``CACHE_DIR/http``) with a lifetime per
  source (places ~7 days, routes ~7 days, weather ~3 hours). Cache hits cost nothing.
- **Fair use:** a generic User-Agent, a minimum interval between requests to the same host, and
  a per-run budget of network calls (hybrid mode); past it, calls fail fast and the chain falls
  back to local data.
- **Trace:** every network call and every refusal is logged as a ``data`` trace event of the
  current run (see :mod:`data.providers.runtime`).

Any failure (network error, timeout, HTTP 429/5xx, bad JSON, budget used up) raises
:class:`ProviderError`, which the fallback chain turns into a ``fallback`` event.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from core.config import Settings
from core.tracing import TraceKind
from data.providers.base import ProviderError
from data.providers.runtime import current_run, data_event


class HttpClient:
    """GET/POST JSON with caching, rate limiting, a call budget and trace events."""

    def __init__(
        self,
        settings: Settings,
        *,
        cache_dir: Path | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._client = httpx.Client(
            headers={"User-Agent": settings.http_user_agent},
            timeout=settings.http_timeout_s,
            transport=transport,
        )
        self._cache: Any = None
        directory = cache_dir if cache_dir is not None else settings.cache_dir / "http"
        try:
            import diskcache

            self._cache = diskcache.Cache(str(directory))
        except Exception:  # an unwritable cache dir must not break live data
            self._cache = None
        self._last_call: dict[str, float] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        self._client.close()
        if self._cache is not None:
            self._cache.close()

    # ------------------------------------------------------------------ public
    def get_json(
        self,
        source: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        ttl_h: float,
        timeout_s: float | None = None,
    ) -> Any:
        return self._request("GET", source, url, params, None, ttl_h, timeout_s)

    def post_json(
        self,
        source: str,
        url: str,
        *,
        data: Mapping[str, str],
        ttl_h: float,
        timeout_s: float | None = None,
    ) -> Any:
        return self._request("POST", source, url, None, data, ttl_h, timeout_s)

    def cached_post(self, url: str, data: Mapping[str, str]) -> Any:
        """A cached POST response, or ``None`` (never calls the network)."""
        if self._cache is None:
            return None
        return self._cache.get(self._key("POST", url, None, data))

    def remember(self, key: str, value: Any, ttl_h: float | None = None) -> None:
        """Keep a small JSON value in the cache (e.g. a live place, so its id still resolves)."""
        if self._cache is not None:
            hours = ttl_h if ttl_h is not None else self.settings.cache_ttl_places_h
            self._cache.set(f"kv:{key}", value, expire=hours * 3600)

    def recall(self, key: str) -> Any:
        return None if self._cache is None else self._cache.get(f"kv:{key}")

    # ------------------------------------------------------------------ internals
    def _request(
        self,
        method: str,
        source: str,
        url: str,
        params: Mapping[str, str] | None,
        data: Mapping[str, str] | None,
        ttl_h: float,
        timeout_s: float | None,
    ) -> Any:
        key = self._key(method, url, params, data)
        if self._cache is not None:
            hit = self._cache.get(key)
            if hit is not None:
                return hit
        run = current_run()
        if run is not None and not run.budget.take():
            data_event(
                TraceKind.FALLBACK,
                f"{source}: live-call budget for this run used up ({run.budget.limit})",
                {"source": source},
            )
            raise ProviderError(f"{source}: live-call budget used up")
        self._wait_turn(url)
        started = time.perf_counter()
        try:
            resp = self._client.request(
                method,
                url,
                params=params,
                data=data,
                timeout=timeout_s or self.settings.http_timeout_s,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"{source}: {type(exc).__name__}: {exc}") from exc
        elapsed = round(time.perf_counter() - started, 2)
        data_event(
            TraceKind.TOOL_CALL,
            f"{source}: {method} {urlsplit(url).netloc} -> HTTP {resp.status_code} ({elapsed}s)",
            {"source": source, "status": resp.status_code, "seconds": elapsed},
        )
        if resp.status_code == 429 or resp.status_code >= 500:
            raise ProviderError(f"{source}: HTTP {resp.status_code} (busy or rate-limited)")
        if resp.status_code >= 400:
            raise ProviderError(f"{source}: HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            body = resp.json()
        except json.JSONDecodeError as exc:
            raise ProviderError(f"{source}: response is not JSON") from exc
        if self._cache is not None:
            self._cache.set(key, body, expire=ttl_h * 3600)
        return body

    def _wait_turn(self, url: str) -> None:
        """At most one request per ``min_request_interval_s`` to the same host."""
        host = urlsplit(url).netloc
        gap = self.settings.min_request_interval_s
        with self._lock:
            wait = self._last_call.get(host, 0.0) + gap - time.monotonic()
            self._last_call[host] = time.monotonic() + max(0.0, wait)
        if wait > 0:
            time.sleep(wait)

    @staticmethod
    def _key(
        method: str,
        url: str,
        params: Mapping[str, str] | None,
        data: Mapping[str, str] | None,
    ) -> str:
        raw = json.dumps(
            [method, url, sorted((params or {}).items()), sorted((data or {}).items())]
        )
        return "http:" + hashlib.sha256(raw.encode()).hexdigest()
