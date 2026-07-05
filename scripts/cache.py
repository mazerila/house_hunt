"""Tiny on-disk JSON cache keyed by sha256(source + params), per-source TTL.

Wraps both the async httpx calls and the sync RNB script calls. A 40-line cache
with explicit TTLs is easier to inspect (just look in .cache/) than a
sqlite blob, and works regardless of which HTTP library a source uses.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

import settings


def _key(source: str, payload: str) -> str:
    return hashlib.sha256(f"{source}::{payload}".encode("utf-8")).hexdigest()


def _path(source: str, key: str) -> str:
    return os.path.join(settings.CACHE_DIR, source, f"{key}.json")


def get(source: str, payload: str) -> Any | None:
    """Return cached value if present and within its TTL, else None."""
    path = _path(source, _key(source, payload))
    try:
        with open(path, "r", encoding="utf-8") as fh:
            entry = json.load(fh)
    except (OSError, ValueError):
        return None
    ttl = settings.CACHE_TTL.get(source, settings.DEFAULT_TTL)
    if time.time() - entry.get("fetched_at", 0) > ttl:
        return None
    return entry.get("payload")


def set(source: str, payload: str, value: Any) -> None:
    path = _path(source, _key(source, payload))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"fetched_at": time.time(), "payload": value}, fh,
                  ensure_ascii=False)
    os.replace(tmp, path)
