"""Deterministic file-based JSON cache for live API responses.

This is not a mock layer. Every entry is a real response captured from a live
call, stored with the time it was fetched so `LedgerRow.date_pulled` stays
honest. The cache lets demos survive rate limits and flaky networks.

Key = SHA256 of canonical JSON {endpoint, params, as_of}. Same inputs always
map to the same file, so runs are reproducible.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from shared.config import CACHE_DIR


@dataclass(frozen=True)
class CachedResponse:
    data: Any
    fetched_at: datetime  # use as LedgerRow.date_pulled
    from_cache: bool
    key: str


def cache_key(endpoint: str, params: dict[str, Any] | None = None, as_of: date | str | None = None) -> str:
    payload = {
        "endpoint": endpoint,
        "params": params or {},
        "as_of": as_of.isoformat() if isinstance(as_of, date) else as_of,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class JsonCache:
    def __init__(self, root: Path | str | None = None, ttl: timedelta | None = None):
        self.root = Path(root) if root is not None else CACHE_DIR
        self.ttl = ttl
        self._locks: dict[str, asyncio.Lock] = {}

    def _path(self, key: str) -> Path:
        # Two-level fan-out keeps directories small.
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> CachedResponse | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            entry = json.loads(path.read_text("utf-8"))
            fetched_at = datetime.fromisoformat(entry["fetched_at"])
        except (OSError, ValueError, KeyError):
            return None  # corrupt entry: treat as a miss and refetch
        if self.ttl is not None and datetime.now(timezone.utc) - fetched_at > self.ttl:
            return None
        return CachedResponse(data=entry["data"], fetched_at=fetched_at, from_cache=True, key=key)

    def set(self, key: str, data: Any, *, endpoint: str = "", params: dict[str, Any] | None = None,
            as_of: date | str | None = None) -> CachedResponse:
        fetched_at = datetime.now(timezone.utc)
        entry = {
            "key": key,
            "endpoint": endpoint,
            "params": params or {},
            "as_of": as_of.isoformat() if isinstance(as_of, date) else as_of,
            "fetched_at": fetched_at.isoformat(),
            "data": data,
        }
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write so a crash mid-write never leaves a half-file.
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(entry, fh, sort_keys=True, default=str)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return CachedResponse(data=data, fetched_at=fetched_at, from_cache=False, key=key)

    async def fetch(
        self,
        endpoint: str,
        params: dict[str, Any] | None,
        fetcher: Callable[[], Awaitable[Any]],
        *,
        as_of: date | str | None = None,
    ) -> CachedResponse:
        """Return the cached response, or call `fetcher()` live and cache it.

        Concurrent calls with the same key share one live request.
        """
        key = cache_key(endpoint, params, as_of)
        hit = self.get(key)
        if hit is not None:
            return hit
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self.get(key)
            if hit is not None:
                return hit
            data = await fetcher()
            return self.set(key, data, endpoint=endpoint, params=params, as_of=as_of)


_default_cache: JsonCache | None = None


def default_cache() -> JsonCache:
    global _default_cache
    if _default_cache is None:
        _default_cache = JsonCache()
    return _default_cache


async def cached_get_json(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    as_of: date | str | None = None,
    client: httpx.AsyncClient | None = None,
    cache: JsonCache | None = None,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
) -> CachedResponse:
    """GET a JSON endpoint live, through the cache. Non-2xx responses raise and are not cached."""
    cache = cache or default_cache()

    async def _fetch() -> Any:
        if client is not None:
            resp = await client.get(url, params=params, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
            resp = await c.get(url, params=params, headers=headers)
            resp.raise_for_status()
            return resp.json()

    return await cache.fetch(url, params, _fetch, as_of=as_of)


async def cached_get(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    as_of: date | str | None = None,
    cache: JsonCache | None = None,
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
) -> tuple[Any, datetime]:
    """GET a JSON endpoint through the cache, returning (data, fetched_at).

    Convenience wrapper over `cached_get_json` for callers that want the
    payload and the pull timestamp directly, e.g. to populate
    `LedgerRow.date_pulled`. Used by checks/trial.
    """
    resp = await cached_get_json(url, params, as_of=as_of, cache=cache, timeout=timeout,
                                 headers=headers)
    return resp.data, resp.fetched_at


async def cached_get_text(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    as_of: date | str | None = None,
    cache: JsonCache | None = None,
    timeout: float = 60.0,
    headers: dict[str, str] | None = None,
) -> CachedResponse:
    """GET a text resource (e.g. a PDB coordinate file) live, through the cache.

    `.data` is the response body as a string. Non-2xx responses raise and are not cached.
    """
    cache = cache or default_cache()

    async def _fetch() -> Any:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
            resp = await c.get(url, params=params, headers=headers)
            resp.raise_for_status()
            return resp.text

    return await cache.fetch(f"TEXT {url}", params, _fetch, as_of=as_of)


async def cached_post_json(
    url: str,
    body: dict[str, Any],
    *,
    as_of: date | str | None = None,
    client: httpx.AsyncClient | None = None,
    cache: JsonCache | None = None,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
) -> CachedResponse:
    """POST JSON (e.g. Open Targets GraphQL) live, through the cache. The body is part of the key."""
    cache = cache or default_cache()

    async def _fetch() -> Any:
        if client is not None:
            resp = await client.post(url, json=body, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
            resp = await c.post(url, json=body, headers=headers)
            resp.raise_for_status()
            return resp.json()

    return await cache.fetch(f"POST {url}", body, _fetch, as_of=as_of)
