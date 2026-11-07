"""Live data-source clients (Open Targets GraphQL, ChEMBL REST), cached.

Every call goes to the live API on a cache miss and is stored by
`shared/cache.py`. GraphQL error payloads are raised, never cached.

Licences (record on every LedgerRow built from these sources):
- Open Targets Platform: CC0 1.0
- ChEMBL: CC BY-SA 3.0
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from shared.cache import CachedResponse, JsonCache, default_cache

OPENTARGETS_GRAPHQL = "https://api.platform.opentargets.org/api/v4/graphql"
OPENTARGETS_WEB = "https://platform.opentargets.org"
OPENTARGETS_LICENCE = "CC0 1.0"

CHEMBL_API = "https://www.ebi.ac.uk/chembl/api/data"
CHEMBL_WEB = "https://www.ebi.ac.uk/chembl/explore/compound"
CHEMBL_LICENCE = "CC BY-SA 3.0"

USER_AGENT = "pre-mortem/0.1 (UCL AgentVerse 2026 hackathon)"


class SourceError(RuntimeError):
    pass


async def ot_query(
    query: str,
    variables: dict[str, Any] | None = None,
    *,
    as_of: date | None = None,
    cache: JsonCache | None = None,
    timeout: float = 30.0,
) -> CachedResponse:
    """Run an Open Targets GraphQL query. `.data` is the GraphQL `data` object."""
    cache = cache or default_cache()
    body = {"query": " ".join(query.split()), "variables": variables or {}}

    async def _fetch() -> Any:
        async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.post(OPENTARGETS_GRAPHQL, json=body)
        try:
            payload = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise SourceError(f"Open Targets returned non-JSON (HTTP {resp.status_code})")
        if payload.get("errors"):
            msg = "; ".join(e.get("message", "?") for e in payload["errors"])[:500]
            raise SourceError(f"Open Targets GraphQL error: {msg}")
        resp.raise_for_status()
        return payload.get("data") or {}

    return await cache.fetch(f"POST {OPENTARGETS_GRAPHQL}", body, _fetch, as_of=as_of)


async def chembl_get(
    path: str,
    params: dict[str, Any] | None = None,
    *,
    as_of: date | None = None,
    cache: JsonCache | None = None,
    timeout: float = 30.0,
) -> CachedResponse:
    """GET a ChEMBL REST resource, e.g. chembl_get("mechanism.json", {"molecule_chembl_id": "CHEMBL25"})."""
    cache = cache or default_cache()
    url = f"{CHEMBL_API}/{path.lstrip('/')}"

    async def _fetch() -> Any:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                     headers={"User-Agent": USER_AGENT, "Accept": "application/json"}) as client:
            resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    return await cache.fetch(url, params, _fetch, as_of=as_of)


def ot_url(kind: str, entity_id: str, sub: str | None = None) -> str:
    """Human-facing Open Targets page, e.g. ot_url("evidence", "ENSG...", "MONDO_...")."""
    if kind == "evidence" and sub:
        return f"{OPENTARGETS_WEB}/evidence/{entity_id}/{sub}"
    return f"{OPENTARGETS_WEB}/{kind}/{entity_id}"


def chembl_url(chembl_id: str) -> str:
    return f"{CHEMBL_WEB}/{chembl_id}"


# Open Targets clinical stage strings, ordered. Unknown strings rank 0.
_STAGE_ORDER = [
    "PRECLINICAL",
    "EARLY_PHASE_1",
    "PHASE_1",
    "PHASE_1_2",
    "PHASE_2",
    "PHASE_2_3",
    "PHASE_3",
    "PHASE_4",
    "APPROVAL",
]


def stage_rank(stage: str | None) -> int:
    if not stage:
        return 0
    s = stage.upper().replace(" ", "_")
    return _STAGE_ORDER.index(s) + 1 if s in _STAGE_ORDER else 0
