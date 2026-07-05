"""Async HTTP helper: one shared httpx client, per-source concurrency caps,
retry/backoff on 429/5xx, and a transparent on-disk cache.

Source clients call `get_json(...)`/`get_text(...)` and never worry about
caching, retries, or rate limits.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

import cache, settings

_client: httpx.AsyncClient | None = None
_semaphores: dict[str, asyncio.Semaphore] = {}


def _sem(source: str) -> asyncio.Semaphore:
    if source not in _semaphores:
        cap = settings.CONCURRENCY.get(source, settings.DEFAULT_CONCURRENCY)
        _semaphores[source] = asyncio.Semaphore(cap)
    return _semaphores[source]


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": settings.USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _request(source: str, url: str, params: dict | None, *,
                   retries: int, want: str) -> tuple[Any, dict]:
    """Returns (value, meta). Raises on final failure."""
    cache_payload = f"{want}|{url}|{json.dumps(params or {}, sort_keys=True)}"
    cached = cache.get(source, cache_payload)
    if cached is not None:
        return cached, {"url": url, "cached": True}

    last_err: Exception | None = None
    async with _sem(source):
        for attempt in range(retries):
            try:
                resp = await client().get(url, params=params)
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    last_err = httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}", request=resp.request,
                        response=resp)
                    await asyncio.sleep(0.4 * (2 ** attempt))
                    continue
                resp.raise_for_status()
                value = resp.json() if want == "json" else resp.text
                cache.set(source, cache_payload, value)
                return value, {"url": str(resp.url), "cached": False}
            except (httpx.HTTPError, ValueError) as e:
                last_err = e
                await asyncio.sleep(0.4 * (2 ** attempt))
    raise last_err or RuntimeError("request failed")


async def get_json(source: str, url: str, params: dict | None = None,
                   retries: int = 3) -> tuple[Any, dict]:
    return await _request(source, url, params, retries=retries, want="json")


async def get_text(source: str, url: str, params: dict | None = None,
                   retries: int = 3) -> tuple[str, dict]:
    return await _request(source, url, params, retries=retries, want="text")
