"""Live asset resolution: free text -> Asset with ChEMBL / Ensembl / UniProt / disease IDs.

No drug lists live here. Resolution goes:

1. Parse the query into candidate drug / target / disease strings. Gemini
   (fast router model) when a key is set, else a deterministic regex parser.
   Explicit `drug=`, `target=`, `disease=` arguments always win.
2. Drug: Open Targets search -> ChEMBL ID (salt forms mapped to the parent).
   ChEMBL REST molecule search is the fallback.
3. Target: the named target via Open Targets search, otherwise the drug's
   mechanism-of-action target (Open Targets, then ChEMBL mechanism.json).
4. Disease: the named disease via Open Targets search, otherwise the drug's
   most advanced, most specific indication.

`Asset.ids` gets: chembl_id, ensembl_id, uniprot_id, target_symbol, efo_id
(the Open Targets disease ID, which may be EFO_ or MONDO_), plus `inferred`
listing fields that were not given by the user.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from core.gemini_client import GeminiClient, GeminiError, get_gemini
from shared.cache import default_cache
from shared.config import model_id
from shared.schemas import Asset
from shared.sources import SourceError, chembl_get, ot_query, stage_rank

log = logging.getLogger(__name__)


class ResolutionError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedQuery:
    drug: str | None = None
    target: str | None = None
    disease: str | None = None
    parser: str = "deterministic"


# ---------------------------------------------------------------------------
# Query parsing
# ---------------------------------------------------------------------------

_TARGET_WORDS = r"(?:targeting|targets|target|against|inhibiting|inhibits|blocking|via|acting on)"
_DISEASE_WORDS = r"(?:in|for|treating|to treat|indicated for|in patients with)"
_PARSE_RE = re.compile(
    rf"^\s*(?P<drug>.+?)"
    rf"(?:\s*\((?P<paren>[^)]+)\))?"
    rf"(?:\s+{_TARGET_WORDS}\s+(?P<target>.+?))?"
    rf"(?:\s+{_DISEASE_WORDS}\s+(?P<disease>.+?))?\s*[.?!]?\s*$",
    re.IGNORECASE,
)


def parse_query_deterministic(query: str) -> ParsedQuery:
    q = " ".join(query.split())
    if not q:
        return ParsedQuery()
    # Delimited form: "drug / target / disease" or "drug, target, disease"
    for sep in ("/", "|", ";", ","):
        if sep in q:
            parts = [p.strip() or None for p in q.split(sep)]
            if 2 <= len(parts) <= 3:
                parts += [None] * (3 - len(parts))
                if len(parts) == 3 and parts[2] is None:
                    # two parts: "drug, disease" is more common than "drug, target"
                    return ParsedQuery(drug=parts[0], disease=parts[1])
                return ParsedQuery(drug=parts[0], target=parts[1], disease=parts[2])
    m = _PARSE_RE.match(q)
    if not m:
        return ParsedQuery(drug=q)
    target = m.group("target") or m.group("paren")
    return ParsedQuery(drug=m.group("drug").strip(), target=_clean(target), disease=_clean(m.group("disease")))


def _clean(s: str | None) -> str | None:
    if s is None:
        return None
    s = s.strip(" .,;")
    return s or None


_PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "drug": {"type": "string", "description": "Drug or molecule name exactly as written, or empty"},
        "target": {"type": "string", "description": "Gene / protein target as written, or empty"},
        "disease": {"type": "string", "description": "Disease or indication as written, or empty"},
    },
    "required": ["drug", "target", "disease"],
}

_PARSE_PROMPT = (
    "Split this biotech asset description into drug, target and disease strings. "
    "Copy the words the user wrote; do not add a target or disease they did not mention, "
    "and do not correct spellings. Use an empty string for anything absent.\n\nDescription: {query}"
)


async def parse_query(query: str, gemini: GeminiClient | None = None) -> ParsedQuery:
    """Gemini parse (cached) when available, deterministic parse otherwise or on any failure."""
    gemini = gemini if gemini is not None else get_gemini()
    fallback = parse_query_deterministic(query)
    if not gemini.available or not query.strip():
        return fallback
    # Single-token queries are just a drug name; skip the model call.
    if len(query.split()) == 1:
        return fallback

    async def _ask() -> Any:
        reply = await gemini.generate(
            _PARSE_PROMPT.format(query=query), role="fast_router", thinking_level="low",
            json_schema=_PARSE_SCHEMA, timeout_s=10,
        )
        return reply.data

    try:
        cached = await default_cache().fetch(
            "gemini:parse_asset_query:v1", {"query": query, "model": model_id("fast_router")}, _ask
        )
        data = cached.data or {}
    except GeminiError as exc:
        log.info("Gemini parse failed, using deterministic parser: %s", exc)
        return fallback
    parsed = ParsedQuery(
        drug=_clean(data.get("drug")), target=_clean(data.get("target")),
        disease=_clean(data.get("disease")), parser="gemini",
    )
    return parsed if parsed.drug else fallback


# ---------------------------------------------------------------------------
# Open Targets / ChEMBL lookups
# ---------------------------------------------------------------------------

_SEARCH_Q = """
query($q: String!, $e: [String!]!) {
  search(queryString: $q, entityNames: $e, page: {index: 0, size: 5}) { hits { id entity name } }
}"""

_DRUG_Q = """
query($id: String!) {
  drug(chemblId: $id) {
    id name drugType maximumClinicalStage
    parentMolecule { id }
    mechanismsOfAction { rows { mechanismOfAction actionType targets { id approvedSymbol } } }
    indications { rows { maxClinicalStage disease { id name isTherapeuticArea ancestors } } }
  }
}"""

_TARGET_Q = """
query($id: String!) {
  target(ensemblId: $id) { id approvedSymbol approvedName proteinIds { id source } }
}"""

_DISEASE_Q = """
query($id: String!) { disease(efoId: $id) { id name } }"""


async def _ot_search(term: str, entity: str) -> list[dict]:
    resp = await ot_query(_SEARCH_Q, {"q": term, "e": [entity]})
    return ((resp.data.get("search") or {}).get("hits")) or []


async def _resolve_drug_id(term: str) -> str:
    hits = await _ot_search(term, "drug")
    if hits:
        # Prefer an exact name match over a fuzzy one.
        exact = [h for h in hits if h["name"].lower() == term.lower()]
        return (exact or hits)[0]["id"]
    # ChEMBL fallback (also catches research codes Open Targets has no drug page for).
    resp = await chembl_get("molecule/search.json", {"q": term, "limit": 5})
    mols = resp.data.get("molecules") or []
    if not mols:
        raise ResolutionError(f"No drug found for {term!r} in Open Targets or ChEMBL")
    exact = [m for m in mols if (m.get("pref_name") or "").lower() == term.lower()]
    mol = (exact or mols)[0]
    hierarchy = mol.get("molecule_hierarchy") or {}
    return hierarchy.get("parent_chembl_id") or mol["molecule_chembl_id"]


async def _drug_record(chembl_id: str) -> dict | None:
    resp = await ot_query(_DRUG_Q, {"id": chembl_id})
    drug = resp.data.get("drug")
    if drug and drug.get("parentMolecule") and drug["parentMolecule"].get("id") not in (None, chembl_id):
        parent = await ot_query(_DRUG_Q, {"id": drug["parentMolecule"]["id"]})
        return parent.data.get("drug") or drug
    return drug


async def _chembl_mechanism_target(chembl_id: str) -> str | None:
    """ChEMBL mechanism -> UniProt accession of the first single-protein target."""
    resp = await chembl_get("mechanism.json", {"molecule_chembl_id": chembl_id, "limit": 20})
    for mech in resp.data.get("mechanisms") or []:
        tid = mech.get("target_chembl_id")
        if not tid:
            continue
        target = await chembl_get(f"target/{tid}.json")
        for comp in target.data.get("target_components") or []:
            if comp.get("accession"):
                return comp["accession"]
    return None


async def _target_record(ensembl_id: str) -> dict:
    resp = await ot_query(_TARGET_Q, {"id": ensembl_id})
    target = resp.data.get("target")
    if not target:
        raise ResolutionError(f"Open Targets has no target {ensembl_id}")
    return target


def _uniprot(target: dict) -> str | None:
    ids = target.get("proteinIds") or []
    for source in ("uniprot_swissprot", "uniprot_trembl"):
        for pid in ids:
            if pid.get("source") == source:
                return pid["id"]
    return None


def pick_primary_indication(indications: list[dict]) -> dict | None:
    """Most advanced stage, then non-therapeutic-area, then most specific (deepest), then name."""
    rows = [r for r in indications if r.get("disease")]
    if not rows:
        return None

    def key(r: dict) -> tuple:
        d = r["disease"]
        return (
            -stage_rank(r.get("maxClinicalStage")),
            bool(d.get("isTherapeuticArea")),
            -len(d.get("ancestors") or []),
            d.get("name") or "",
        )

    return sorted(rows, key=key)[0]["disease"]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def resolve_asset(
    query: str,
    drug: str | None = None,
    target: str | None = None,
    disease: str | None = None,
    as_of: date | None = None,
    *,
    gemini: GeminiClient | None = None,
) -> Asset:
    parsed = await parse_query(query, gemini) if query and query.strip() else ParsedQuery()
    drug_term = drug or parsed.drug
    target_term = target or parsed.target
    disease_term = disease or parsed.disease
    if not drug_term:
        raise ResolutionError("A drug name is required")

    try:
        return await _resolve(drug_term, target_term, disease_term, as_of)
    except SourceError as exc:
        raise ResolutionError(f"Live lookup failed: {exc}") from exc


async def _resolve(drug_term: str, target_term: str | None, disease_term: str | None,
                   as_of: date | None) -> Asset:
    inferred: list[str] = []

    chembl_id = await _resolve_drug_id(drug_term)
    drug = await _drug_record(chembl_id)
    if drug:
        chembl_id = drug["id"]
    drug_name = (drug or {}).get("name") or drug_term

    # Target
    ensembl_id: str | None = None
    if target_term:
        hits = await _ot_search(target_term, "target")
        exact = [h for h in hits if h["name"].lower() == target_term.lower()]
        if hits:
            ensembl_id = (exact or hits)[0]["id"]
    if ensembl_id is None and drug:
        for row in (drug.get("mechanismsOfAction") or {}).get("rows") or []:
            if row.get("targets"):
                ensembl_id = row["targets"][0]["id"]
                break
        if ensembl_id:
            inferred.append("target")
    if ensembl_id is None:
        accession = await _chembl_mechanism_target(chembl_id)
        if accession:
            hits = await _ot_search(accession, "target")
            if hits:
                ensembl_id = hits[0]["id"]
                inferred.append("target")
    if ensembl_id is None:
        raise ResolutionError(f"Could not resolve a target for {drug_name}; pass target= explicitly")
    target = await _target_record(ensembl_id)

    # Disease
    disease_rec: dict | None = None
    if disease_term:
        hits = await _ot_search(disease_term, "disease")
        if hits:
            disease_rec = {"id": hits[0]["id"], "name": hits[0]["name"]}
    if disease_rec is None and drug:
        disease_rec = pick_primary_indication((drug.get("indications") or {}).get("rows") or [])
        if disease_rec:
            inferred.append("disease")
    if disease_rec is None:
        raise ResolutionError(f"Could not resolve a disease for {drug_name}; pass disease= explicitly")

    ids = {
        "chembl_id": chembl_id,
        "ensembl_id": target["id"],
        "target_symbol": target["approvedSymbol"],
        "efo_id": disease_rec["id"],
    }
    uniprot = _uniprot(target)
    if uniprot:
        ids["uniprot_id"] = uniprot
    if drug and drug.get("maximumClinicalStage"):
        ids["max_clinical_stage"] = drug["maximumClinicalStage"]
    if inferred:
        ids["inferred"] = ",".join(inferred)

    return Asset(drug=drug_name, target=target["approvedSymbol"], disease=disease_rec["name"], ids=ids, as_of=as_of)
