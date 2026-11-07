"""Structure resolution, SIFTS residue mapping, and heavy-atom contact distances.

Multiple Chains & Ligand Copies Rule:
    For each candidate PDB structure, we identify all polymer chains mapped to the target
    UniProt accession via SIFTS and all non-polymer ligand copies matching the resolved
    `chem_comp_id` (identified as `{chain}:{comp_id}:{seqid}`, e.g. `A:YY3:1101`).
    For each ligand copy and the mapped polymer chain(s) it contacts, we compute the
    minimum heavy-atom (non-hydrogen) distance to every mapped residue in that chain
    using gemmi's `label_seq` numbering (`label_seq_id`). Across all contacting
    `(chain, ligand_copy)` pairs, we report the minimum heavy-atom distance per residue
    and record both the contacting `chain` and `ligand_copy`.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
import gemmi
import httpx
from pydantic import BaseModel, Field

from checks.market.variants import Variant, build_variant_ledger_row
from shared.cache import (
    CachedResponse,
    JsonCache,
    cached_get_json,
    cached_get_text,
    cached_post_json,
    default_cache,
)
from shared.schemas import Asset, LedgerRow, ResidueEvidence, StructurePayload, utc_now

BINDING_POCKET_CUTOFF_ANGSTROM: float = 4.5
COVALENT_CUTOFF_ANGSTROM: float = 2.2
REQUEST_TIMEOUT_SECONDS: float = 15.0
MAX_CANDIDATE_STRUCTURES: int = 5

RCSB_SEARCH_URL = "https://search.rcsb.org/rcsbsearch/v2/query"
RCSB_ENTRY_URL = "https://data.rcsb.org/rest/v1/core/entry/{pdb_id}"
RCSB_CIF_URL = "https://files.rcsb.org/download/{pdb_id}.cif"
RCSB_STRUCTURE_PAGE_URL = "https://www.rcsb.org/structure/{pdb_id}"
PDBE_SIFTS_URL = "https://www.ebi.ac.uk/pdbe/api/mappings/uniprot/{pdb_id_lower}"
CHEMBL_MOLECULE_URL = "https://www.ebi.ac.uk/chembl/api/data/molecule/{chembl_id}.json"
CHEMBL_SEARCH_URL = "https://www.ebi.ac.uk/chembl/api/data/molecule/search.json?q={query}"
CHEMBL_METABOLISM_URL = (
    "https://www.ebi.ac.uk/chembl/api/data/metabolism.json?substrate_chembl_id={chembl_id}"
)
UNICHEM_COMPOUNDS_URL = "https://www.ebi.ac.uk/unichem/api/v1/compounds"
UNICHEM_CONNECTIVITY_URL = "https://www.ebi.ac.uk/unichem/api/v1/connectivity"
ALPHAFOLD_PREDICTION_URL = "https://alphafold.ebi.ac.uk/api/prediction/{uniprot_id}"
UNIPROT_BINDING_URL = (
    "https://rest.uniprot.org/uniprotkb/{uniprot_id}.json?fields=ft_binding,ft_act_site,sequence"
)

BIOLOGIC_MOLECULE_TYPES: frozenset[str] = frozenset(
    {"Antibody", "Protein", "Oligonucleotide", "Enzyme", "Cell", "Gene"}
)

RCSB_PDB_LICENCE = "CC0 1.0"
PDBE_SIFTS_LICENCE = "EMBL-EBI Terms of Use (https://www.ebi.ac.uk/about/terms-of-use)"
COMBINED_PDB_SIFTS_LICENCE = (
    "CC0 1.0 (RCSB PDB); EMBL-EBI Terms of Use (PDBe SIFTS: https://www.ebi.ac.uk/about/terms-of-use)"
)
ALPHAFOLD_LICENCE = "CC BY 4.0"
UNIPROT_LICENCE = "CC BY 4.0"


class SiftsMappingError(ValueError):
    """Fatal error raised when a SIFTS-mapped label_seq residue mismatches UniProt wild type."""


class ResidueContact(BaseModel):
    """Computed heavy-atom contact details for a single UniProt residue position."""

    uniprot_pos: int
    uniprot_wild_type: str
    pdb_residue_name: str
    label_seq_id: int
    chain: str
    ligand_copy: str
    contact_distance_angstrom: float
    in_binding_pocket: bool
    covalent: bool


class LigandResolutionResult(BaseModel):
    """Result of resolving a drug asset to a PDB Chemical Component ID."""

    status: Literal["resolved", "ambiguous", "not_found", "timeout", "failed"]
    chem_comp_id: str | None = None
    candidates: list[str] = Field(default_factory=list)
    url: str = RCSB_SEARCH_URL
    date_pulled: datetime | None = None
    error_message: str | None = None


class StructureAnalysisResult(BaseModel):
    """Complete output of structure resolution and pocket contact analysis."""

    status: Literal["complete", "partial", "failed", "timeout"]
    confidence: Literal["high", "medium", "low"]
    ligand_status: Literal["resolved", "ambiguous", "not_found", "timeout", "failed"]
    chem_comp_id: str | None = None
    candidate_ligands: list[str] = Field(default_factory=list)
    candidate_structures_considered: int = 0
    skipped_structures: list[str] = Field(default_factory=list)
    covalent_residues: list[str] = Field(default_factory=list)
    contact_by_position: dict[int, ResidueContact] = Field(default_factory=dict)
    all_chain_contacts: dict[int, ResidueContact] = Field(default_factory=dict)
    structure_payload: StructurePayload | None = None
    ledger_rows: list[LedgerRow] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


def rank_by_structure_only(
    contacts: dict[int, ResidueContact] | list[ResidueContact] | StructureAnalysisResult,
    in_pocket_cutoff: float = BINDING_POCKET_CUTOFF_ANGSTROM,
) -> list[ResidueContact]:
    """Rank every mapped residue in the contacting chain by minimum heavy-atom distance ascending, ties by position.

    Uses strictly 3D structure coordinates and SIFTS mapping. Variant, annotation, and
    population allele frequency information are never used.
    """
    if isinstance(contacts, StructureAnalysisResult):
        source_map = contacts.all_chain_contacts or contacts.contact_by_position
        items = list(source_map.values())
    elif isinstance(contacts, dict):
        items = list(contacts.values())
    else:
        items = list(contacts)

    updated = [
        c.model_copy(
            update={
                "in_binding_pocket": (
                    0.0 <= c.contact_distance_angstrom <= in_pocket_cutoff
                )
            }
        )
        for c in items
        if c.contact_distance_angstrom >= 0.0
    ]
    return sorted(
        updated,
        key=lambda c: (c.contact_distance_angstrom, c.uniprot_pos),
    )


# Known clinical EGFR tyrosine kinase inhibitor (TKI) escape residues in NSCLC:
# gatekeeper T790, covalent warhead anchor C797, and solvent-front / hinge steric-clash
# sites L718, L792, and G796.
# Source: UniProt P00533 variant/mutagenesis curation & clinical osimertinib resistance studies
# (Thress et al. Nat Med 2015; Bersanelli et al. 2016; Ou et al. 2017).
# Note: Used strictly as a secondary ranking boost in `rank_escape_candidates`; never feeds the risk score.
CURATED_EGFR_ESCAPE_RESIDUES: frozenset[str] = frozenset(
    {"T790", "C797", "L718", "L792", "G796"}
)


def is_drug_response_variant(variant: Variant) -> bool:
    """Return True only if the variant is explicitly annotated with resistance or resistant.

    ClinVar's generic 'Drug response' string is intentionally excluded because it is neutral
    and includes sensitising/activating mutations such as EGFR G719S/C/R.
    """
    if variant.clinical_significance:
        lowered_sig = variant.clinical_significance.lower()
        if "resistance" in lowered_sig or "resistant" in lowered_sig:
            return True
    for desc in variant.descriptions:
        lowered_desc = desc.lower()
        if "resistance" in lowered_desc or "resistant" in lowered_desc:
            return True
    return False


def is_curated_escape_residue(variant: Variant) -> bool:
    """Return True if the variant occurs at a known curated EGFR escape residue (ranking boost only)."""
    if variant.uniprot_id.strip().upper() != "P00533":
        return False
    return f"{variant.original_residue}{variant.position}" in CURATED_EGFR_ESCAPE_RESIDUES


def is_benign_only_variant(variant: Variant) -> bool:
    """Return True if the variant's clinical significance is exclusively Benign / Likely benign."""
    if not variant.clinical_significance:
        return False
    lowered = variant.clinical_significance.lower()
    if "pathogenic" in lowered or "resistance" in lowered or "resistant" in lowered or "uncertain" in lowered:
        return False
    return "benign" in lowered


def is_clinically_actionable_variant(variant: Variant) -> bool:
    """Return True if variant has resistance text or pathogenic annotation (curated residue list never feeds score)."""
    if is_drug_response_variant(variant):
        return True
    if variant.clinical_significance:
        lowered_sig = variant.clinical_significance.lower()
        if "pathogenic" in lowered_sig:
            return True
    return False


def variant_annotation_priority_key(variant: Variant) -> tuple[int, int, int, int, int, int, str]:
    """Order variants at the same residue by clinical annotation and curation tier (never frequency)."""
    return (
        1 if is_benign_only_variant(variant) else 0,
        0 if is_drug_response_variant(variant) else 1,
        0 if (is_clinically_actionable_variant(variant) or is_curated_escape_residue(variant)) else 1,
        0 if variant.ft_id is not None else 1,
        0 if "ClinVar" in variant.sources else 1,
        -len(variant.xrefs),
        variant.new_residue,
    )


def rank_escape_candidates(
    variants: list[Variant],
    contact_by_position: dict[int, ResidueContact],
    in_pocket_positions: set[int],
    *,
    use_annotations: bool = True,
) -> list[Variant]:
    """Rank reported escape candidate variants with contact_distance_angstrom as the primary sort key.

    Rules:
      1. Variants at a covalent attachment residue (`contact.covalent`) always rank first
         regardless of ClinVar label (excluding strictly Benign polymorphisms).
      2. Primary sort key is heavy-atom `contact_distance_angstrom` ascending, with
         annotation (resistance text, pathogenic label, curated escape residue) applied
         only as a bounded secondary distance boost (max 1.65 Å) so a VUS at 1.8 Å
         always outranks a pathogenic variant at 4.4 Å.
      3. When `use_annotations=False` (e.g. when `as_of` is set), all annotation boosts
         are zeroed so ranking uses 3D structure distance only.
      4. Germline population frequency (`variant_frequency`) never enters the ranking key.
    """
    candidates: list[Variant] = []
    for variant in variants:
        in_pocket = variant.position in in_pocket_positions
        if in_pocket or (use_annotations and is_drug_response_variant(variant)):
            candidates.append(variant)

    primary_id_by_pos: dict[int, str] = {}
    by_pos: dict[int, list[Variant]] = {}
    for v in candidates:
        by_pos.setdefault(v.position, []).append(v)
    for pos, pos_vars in by_pos.items():
        ordered = sorted(pos_vars, key=variant_annotation_priority_key)
        primary_id_by_pos[pos] = ordered[0].id

    def _sort_key(v: Variant) -> tuple[int, int, float, float, tuple[int, ...], int]:
        contact = contact_by_position.get(v.position)
        is_covalent = bool(contact is not None and contact.covalent)
        benign_flag = 1 if (use_annotations and is_benign_only_variant(v)) else 0
        covalent_first = 0 if (is_covalent and benign_flag == 0) else 1

        dist = (
            contact.contact_distance_angstrom
            if (contact is not None and contact.contact_distance_angstrom >= 0.0)
            else float("inf")
        )

        boost = 0.0
        if use_annotations and benign_flag == 0 and dist < float("inf"):
            is_primary_at_pos = primary_id_by_pos.get(v.position) == v.id
            has_res_text = is_drug_response_variant(v)
            has_pathogenic = bool(
                v.clinical_significance and "pathogenic" in v.clinical_significance.lower()
            )
            has_curated_escape = is_curated_escape_residue(v)

            if has_res_text:
                boost += 0.60 if is_primary_at_pos else 0.20
            if has_curated_escape:
                boost += 0.65 if is_primary_at_pos else 0.15
            if has_pathogenic:
                boost += (1.00 if has_curated_escape else 0.45) if is_primary_at_pos else 0.15

        effective_dist = round(dist - boost, 4) if dist < float("inf") else float("inf")
        tie_key = (
            variant_annotation_priority_key(v)
            if use_annotations
            else (0, 0, 0, 0, 0, 0, v.new_residue)
        )
        return (benign_flag, covalent_first, effective_dist, dist, tie_key, v.position)

    return sorted(candidates, key=_sort_key)


def _three_to_one(res_name: str) -> str:
    """Convert a 3-letter amino acid code to its 1-letter uppercase code via gemmi."""
    info = gemmi.find_tabulated_residue(res_name.strip().upper())
    code = info.one_letter_code.upper()
    if not code or code == " ":
        return "?"
    return code


async def _safe_cached_post_json(
    url: str,
    body: dict[str, Any],
    *,
    timeout: float,
    cache: JsonCache,
    client: httpx.AsyncClient | None,
) -> CachedResponse:
    """Call cached_post_json, handling RCSB 204 No Content (empty body -> ValueError) as an empty result_set."""
    try:
        return await cached_post_json(
            url,
            body,
            client=client,
            cache=cache,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
    except ValueError:
        return CachedResponse(
            data={"result_set": [], "total_count": 0},
            fetched_at=utc_now(),
            from_cache=False,
            key="",
        )


async def _rcsb_chembl_lookup(
    chembl_id: str,
    timeout: float,
    cache: JsonCache,
    client: httpx.AsyncClient | None,
) -> LigandResolutionResult | None:
    """Query RCSB mol_definition by exact ChEMBL accession match."""
    query_chembl = {
        "query": {
            "type": "terminal",
            "service": "text_chem",
            "parameters": {
                "attribute": "rcsb_chem_comp_related.resource_accession_code",
                "operator": "exact_match",
                "value": chembl_id,
            },
        },
        "return_type": "mol_definition",
    }
    entry = await _safe_cached_post_json(
        RCSB_SEARCH_URL,
        query_chembl,
        timeout=timeout,
        cache=cache,
        client=client,
    )
    hits = [
        item["identifier"]
        for item in ((entry.data or {}).get("result_set") or [])
        if item.get("identifier")
    ]
    if len(hits) == 1:
        return LigandResolutionResult(
            status="resolved",
            chem_comp_id=hits[0],
            candidates=hits,
            url=RCSB_SEARCH_URL,
            date_pulled=entry.fetched_at,
        )
    if len(hits) > 1:
        return LigandResolutionResult(
            status="ambiguous",
            chem_comp_id=None,
            candidates=hits,
            url=RCSB_SEARCH_URL,
            date_pulled=entry.fetched_at,
        )
    return None


async def _unichem_pdb_lookup(
    chembl_id: str,
    timeout: float,
    cache: JsonCache,
    client: httpx.AsyncClient | None,
    *,
    connectivity: bool = False,
) -> tuple[list[str], datetime | None]:
    """Resolve a ChEMBL ID to PDB chemical component ID(s) via EBI UniChem v1 API."""
    url = UNICHEM_CONNECTIVITY_URL if connectivity else UNICHEM_COMPOUNDS_URL
    body: dict[str, Any] = {
        "compound": chembl_id,
        "sourceID": 1,
        "type": "sourceID",
    }
    if connectivity:
        body["searchComponents"] = True

    try:
        entry = await _safe_cached_post_json(
            url,
            body,
            timeout=timeout,
            cache=cache,
            client=client,
        )
    except httpx.HTTPStatusError:
        return [], None

    payload = entry.data or {}
    pdb_ids: list[str] = []
    if connectivity:
        for src in payload.get("sources") or []:
            if src.get("id") in (3, 5) or (src.get("shortName") or "").lower() in ("rcsb_pdb", "pdbe"):
                cid = (src.get("compoundId") or "").strip().upper()
                if cid and cid not in pdb_ids:
                    pdb_ids.append(cid)
    else:
        for comp in payload.get("compounds") or []:
            for src in comp.get("sources") or []:
                if src.get("id") in (3, 5) or (src.get("shortName") or "").lower() in ("rcsb_pdb", "pdbe"):
                    cid = (src.get("compoundId") or "").strip().upper()
                    if cid and cid not in pdb_ids:
                        pdb_ids.append(cid)
    return pdb_ids, entry.fetched_at


async def _target_has_pdb_entries(
    uniprot_id: str,
    chem_comp_id: str,
    timeout: float,
    cache: JsonCache,
    client: httpx.AsyncClient | None,
) -> bool | None:
    """Return True if RCSB has at least one PDB entry for (uniprot_id, chem_comp_id), False if 0, or None if unmocked."""
    if not uniprot_id:
        return True
    nodes: list[dict[str, Any]] = [
        {
            "type": "terminal",
            "service": "text",
            "parameters": {
                "attribute": "rcsb_polymer_entity_container_identifiers.reference_sequence_identifiers.database_accession",
                "operator": "exact_match",
                "value": uniprot_id,
            },
        },
        {
            "type": "terminal",
            "service": "text",
            "parameters": {
                "attribute": "rcsb_nonpolymer_entity_container_identifiers.nonpolymer_comp_id",
                "operator": "exact_match",
                "value": chem_comp_id,
            },
        },
    ]
    search_body = {
        "query": {"type": "group", "logical_operator": "and", "nodes": nodes},
        "request_options": {
            "sort": [{"sort_by": "rcsb_entry_info.resolution_combined", "direction": "asc"}]
        },
        "return_type": "entry",
    }
    try:
        entry = await _safe_cached_post_json(
            RCSB_SEARCH_URL,
            search_body,
            timeout=timeout,
            cache=cache,
            client=client,
        )
    except httpx.HTTPStatusError:
        return None
    hits = [
        item["identifier"]
        for item in ((entry.data or {}).get("result_set") or [])
        if item.get("identifier")
    ]
    return len(hits) > 0


async def _resolve_metabolite_ligands_for_target(
    chembl_id: str,
    uniprot_id: str,
    timeout: float,
    cache: JsonCache,
    client: httpx.AsyncClient | None,
) -> LigandResolutionResult | None:
    """Check ChEMBL drug metabolism for active metabolite PDB ligands bound to uniprot_id (e.g. tamoxifen -> 4OHtam -> OHT)."""
    if not uniprot_id:
        return None
    met_url = CHEMBL_METABOLISM_URL.format(chembl_id=chembl_id)
    try:
        met_entry = await cached_get_json(
            met_url,
            client=client,
            cache=cache,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPStatusError:
        return None

    metabolisms = (met_entry.data or {}).get("metabolisms") or []
    metabolite_chembl_ids: list[str] = []
    for m in metabolisms:
        mcid = (m.get("metabolite_chembl_id") or "").strip().upper()
        if mcid and mcid != chembl_id and mcid not in metabolite_chembl_ids:
            metabolite_chembl_ids.append(mcid)

    if not metabolite_chembl_ids:
        return None

    bound_ligands: list[str] = []
    pulled_at = met_entry.fetched_at
    for mcid in metabolite_chembl_ids:
        u_hits, u_date = await _unichem_pdb_lookup(
            mcid, timeout, cache, client, connectivity=True
        )
        if u_date is not None:
            pulled_at = u_date
        for comp_id in u_hits:
            if comp_id not in bound_ligands:
                has_entries = await _target_has_pdb_entries(
                    uniprot_id, comp_id, timeout, cache, client
                )
                if has_entries is True:
                    bound_ligands.append(comp_id)

    if len(bound_ligands) == 1:
        return LigandResolutionResult(
            status="resolved",
            chem_comp_id=bound_ligands[0],
            candidates=bound_ligands,
            url=UNICHEM_CONNECTIVITY_URL,
            date_pulled=pulled_at,
        )
    if len(bound_ligands) > 1:
        return LigandResolutionResult(
            status="ambiguous",
            chem_comp_id=None,
            candidates=bound_ligands,
            url=UNICHEM_CONNECTIVITY_URL,
            date_pulled=pulled_at,
        )
    return None


async def _resolve_from_chembl_id(
    chembl_id: str,
    uniprot_id: str,
    timeout: float,
    cache: JsonCache,
    client: httpx.AsyncClient | None,
) -> LigandResolutionResult | None:
    """Resolve PDB chem_comp_id from ChEMBL ID via RCSB exact_match, UniChem cross-reference, and active metabolites."""
    rcsb_res = await _rcsb_chembl_lookup(chembl_id, timeout, cache, client)
    if rcsb_res is not None and rcsb_res.status == "ambiguous":
        return rcsb_res

    candidates: list[str] = []
    pulled_at: datetime | None = None
    source_url = RCSB_SEARCH_URL

    if rcsb_res is not None and rcsb_res.chem_comp_id:
        candidates.append(rcsb_res.chem_comp_id)
        pulled_at = rcsb_res.date_pulled

    if not candidates:
        u_hits, u_date = await _unichem_pdb_lookup(chembl_id, timeout, cache, client)
        if u_hits:
            candidates.extend(u_hits)
            pulled_at = u_date
            source_url = UNICHEM_COMPOUNDS_URL

    if len(candidates) > 1:
        return LigandResolutionResult(
            status="ambiguous",
            chem_comp_id=None,
            candidates=candidates,
            url=source_url,
            date_pulled=pulled_at,
        )

    if len(candidates) == 1:
        comp_id = candidates[0]
        if uniprot_id:
            met_res = await _resolve_metabolite_ligands_for_target(
                chembl_id, uniprot_id, timeout, cache, client
            )
            if met_res is not None:
                parent_has = await _target_has_pdb_entries(
                    uniprot_id, comp_id, timeout, cache, client
                )
                if parent_has is False:
                    return met_res
        return LigandResolutionResult(
            status="resolved",
            chem_comp_id=comp_id,
            candidates=[comp_id],
            url=source_url,
            date_pulled=pulled_at,
        )
    return None


async def resolve_ligand_comp_ids(
    asset: Asset,
    *,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    cache: JsonCache | None = None,
    cache_dir: Path | None = None,
    client: httpx.AsyncClient | None = None,
) -> LigandResolutionResult:
    """Resolve Asset (chembl_id and/or drug/brand name) to PDB chemical component ID(s) via ChEMBL/UniChem first, then RCSB."""
    chembl_id = (asset.ids.get("chembl_id") or "").strip().upper()
    uniprot_id = (asset.ids.get("uniprot_id") or "").strip().upper()
    drug_name = asset.drug.strip()
    eff_cache = cache or (JsonCache(cache_dir) if cache_dir is not None else default_cache())

    try:
        if chembl_id:
            res = await _resolve_from_chembl_id(chembl_id, uniprot_id, timeout, eff_cache, client)
            if res is not None:
                return res

        if not chembl_id and drug_name:
            search_url = CHEMBL_SEARCH_URL.format(query=drug_name)
            try:
                ch_search = await cached_get_json(
                    search_url,
                    client=client,
                    cache=eff_cache,
                    timeout=timeout,
                    headers={"Accept": "application/json"},
                )
            except httpx.HTTPStatusError:
                ch_search = None

            if ch_search is not None:
                molecules = (ch_search.data or {}).get("molecules") or []
                exact_parent_ids: list[str] = []
                fallback_parent_ids: list[str] = []
                for mol in molecules:
                    mol_type = (mol.get("molecule_type") or "").strip()
                    pref_name = (mol.get("pref_name") or "").strip()
                    if mol_type in BIOLOGIC_MOLECULE_TYPES and (
                        pref_name.upper() == drug_name.upper() or len(molecules) == 1
                    ):
                        return LigandResolutionResult(
                            status="not_found",
                            candidates=[],
                            error_message=(
                                f"Biologic modality ('{drug_name}', {mol_type}) has no small-molecule PDB chem_comp_id"
                            ),
                        )
                    hier = mol.get("molecule_hierarchy") or {}
                    pid = (
                        hier.get("parent_chembl_id")
                        or hier.get("active_chembl_id")
                        or mol.get("molecule_chembl_id")
                    )
                    if pid:
                        pid_up = str(pid).upper()
                        if pref_name.upper() == drug_name.upper():
                            if pid_up not in exact_parent_ids:
                                exact_parent_ids.append(pid_up)
                        elif pid_up not in fallback_parent_ids:
                            fallback_parent_ids.append(pid_up)

                ordered_parents = exact_parent_ids + [
                    p for p in fallback_parent_ids if p not in exact_parent_ids
                ]
                if ordered_parents:
                    chembl_id = ordered_parents[0]
                    res = await _resolve_from_chembl_id(
                        chembl_id, uniprot_id, timeout, eff_cache, client
                    )
                    if res is not None:
                        return res

        if drug_name:
            query_synonym = {
                "query": {
                    "type": "terminal",
                    "service": "text_chem",
                    "parameters": {
                        "attribute": "rcsb_chem_comp_synonyms.name",
                        "operator": "contains_phrase",
                        "value": drug_name,
                    },
                },
                "return_type": "mol_definition",
            }
            entry = await _safe_cached_post_json(
                RCSB_SEARCH_URL,
                query_synonym,
                timeout=timeout,
                cache=eff_cache,
                client=client,
            )
            hits = [
                item["identifier"]
                for item in ((entry.data or {}).get("result_set") or [])
                if item.get("identifier")
            ]
            if len(hits) == 1:
                return LigandResolutionResult(
                    status="resolved",
                    chem_comp_id=hits[0],
                    candidates=hits,
                    url=RCSB_SEARCH_URL,
                    date_pulled=entry.fetched_at,
                )
            if len(hits) > 1:
                return LigandResolutionResult(
                    status="ambiguous",
                    chem_comp_id=None,
                    candidates=hits,
                    url=RCSB_SEARCH_URL,
                    date_pulled=entry.fetched_at,
                )

        if chembl_id:
            chembl_url = CHEMBL_MOLECULE_URL.format(chembl_id=chembl_id)
            ch_entry = await cached_get_json(
                chembl_url,
                client=client,
                cache=eff_cache,
                timeout=timeout,
                headers={"Accept": "application/json"},
            )
            mol_payload = ch_entry.data or {}
            mol_type = (mol_payload.get("molecule_type") or "").strip()
            if mol_type in BIOLOGIC_MOLECULE_TYPES:
                return LigandResolutionResult(
                    status="not_found",
                    candidates=[],
                    error_message=(
                        f"Biologic modality ('{drug_name or chembl_id}', {mol_type}) has no small-molecule PDB chem_comp_id"
                    ),
                )

            hier = mol_payload.get("molecule_hierarchy") or {}
            parent_id = (hier.get("parent_chembl_id") or hier.get("active_chembl_id") or "").strip().upper()
            if parent_id and parent_id != chembl_id:
                res = await _resolve_from_chembl_id(parent_id, uniprot_id, timeout, eff_cache, client)
                if res is not None:
                    return res

            inchi = (mol_payload.get("molecule_structures") or {}).get("standard_inchi")
            if inchi:
                query_inchi = {
                    "query": {
                        "type": "terminal",
                        "service": "chemical",
                        "parameters": {
                            "value": inchi,
                            "type": "descriptor",
                            "descriptor_type": "InChI",
                            "match_type": "graph-relaxed",
                        },
                    },
                    "return_type": "mol_definition",
                }
                in_entry = await _safe_cached_post_json(
                    RCSB_SEARCH_URL,
                    query_inchi,
                    timeout=timeout,
                    cache=eff_cache,
                    client=client,
                )
                hits = [
                    item["identifier"]
                    for item in ((in_entry.data or {}).get("result_set") or [])
                    if item.get("identifier")
                ]
                if len(hits) == 1:
                    return LigandResolutionResult(
                        status="resolved",
                        chem_comp_id=hits[0],
                        candidates=hits,
                        url=RCSB_SEARCH_URL,
                        date_pulled=in_entry.fetched_at,
                    )
                if len(hits) > 1:
                    return LigandResolutionResult(
                        status="ambiguous",
                        chem_comp_id=None,
                        candidates=hits,
                        url=RCSB_SEARCH_URL,
                        date_pulled=in_entry.fetched_at,
                    )

        return LigandResolutionResult(status="not_found", candidates=[])
    except httpx.TimeoutException as exc:
        return LigandResolutionResult(status="timeout", error_message=str(exc))
    except httpx.HTTPError as exc:
        return LigandResolutionResult(status="failed", error_message=str(exc))


def _parse_engineered_mutations(block: gemmi.cif.Block) -> dict[tuple[str, int], tuple[str, str]]:
    """Extract annotated engineered mutations from mmCIF _struct_ref_seq_dif."""
    engineered: dict[tuple[str, int], tuple[str, str]] = {}
    table = block.find(
        "_struct_ref_seq_dif.",
        ["pdbx_pdb_strand_id", "asym_id", "seq_num", "mon_id", "db_mon_id", "details"],
    )
    if not table:
        table = block.find(
            "_struct_ref_seq_dif.",
            ["seq_num", "mon_id", "db_mon_id", "details"],
        )
        for row in table:
            seq_str, mon_id, db_mon_id, details = row[0], row[1], row[2], row[3]
            if "engineered" not in details.lower():
                continue
            try:
                seq_num = int(seq_str)
            except ValueError:
                continue
            wt_1 = _three_to_one(db_mon_id)
            mut_1 = _three_to_one(mon_id)
            engineered[("*", seq_num)] = (wt_1, mut_1)
        return engineered

    for row in table:
        strand_id, asym_id, seq_str, mon_id, db_mon_id, details = (
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
        )
        if "engineered" not in details.lower():
            continue
        try:
            seq_num = int(seq_str)
        except ValueError:
            continue
        wt_1 = _three_to_one(db_mon_id)
        mut_1 = _three_to_one(mon_id)
        for ch_key in {strand_id, asym_id, "*"}:
            if ch_key:
                engineered[(ch_key, seq_num)] = (wt_1, mut_1)
    return engineered


def _evaluate_pdb_candidate_sync(
    pdb_id: str,
    uniprot_id: str,
    chem_comp_id: str,
    cif_text: str,
    sifts_payload: dict[str, Any],
    variants_by_pos: dict[int, list[Variant]],
) -> tuple[str | None, dict[int, ResidueContact], dict[int, ResidueContact]]:
    """Parse mmCIF with gemmi, map residues via SIFTS label_seq_id, and compute heavy-atom distances."""
    doc = gemmi.cif.read_string(cif_text)
    block = doc.sole_block()
    engineered_map = _parse_engineered_mutations(block)
    st = gemmi.make_structure_from_block(block)
    model = st[0]

    pdb_key = pdb_id.lower()
    uniprot_block = ((sifts_payload.get(pdb_key) or {}).get("UniProt") or {}).get(uniprot_id) or {}
    mappings = uniprot_block.get("mappings") or []
    if not mappings:
        return f"{pdb_id} skipped: no SIFTS mapping for {uniprot_id}", {}, {}

    ligand_copies: list[tuple[str, str, list[gemmi.Atom]]] = []
    for chain in model:
        for res in chain:
            if res.name.strip().upper() == chem_comp_id.upper():
                heavy_atoms = [atom for atom in res if not atom.is_hydrogen()]
                if heavy_atoms:
                    copy_id = f"{chain.name}:{res.name.strip()}:{res.seqid}"
                    ligand_copies.append((chain.name, copy_id, heavy_atoms))

    if not ligand_copies:
        return f"{pdb_id} skipped: no {chem_comp_id} heavy atoms found", {}, {}

    polymer_by_label_seq: dict[tuple[str, int], tuple[str, gemmi.Residue]] = {}
    chain_heavy_atoms: dict[str, list[gemmi.Atom]] = {}
    for chain in model:
        for res in chain:
            if res.label_seq is None:
                continue
            label_seq_int = int(res.label_seq)
            for ch_id in {chain.name, res.subchain}:
                if ch_id:
                    polymer_by_label_seq[(ch_id, label_seq_int)] = (chain.name, res)
                    atoms = [a for a in res if not a.is_hydrogen()]
                    if atoms:
                        chain_heavy_atoms.setdefault(ch_id, []).extend(atoms)

    ref_seq_segments: dict[tuple[int, int], tuple[int, int]] = {}
    ref_table = block.find(
        "_struct_ref_seq.",
        ["seq_align_beg", "seq_align_end", "db_align_beg", "db_align_end"],
    )
    for row in ref_table:
        try:
            s_beg, s_end, d_beg, d_end = int(row[0]), int(row[1]), int(row[2]), int(row[3])
            if (s_end - s_beg) == (d_end - d_beg):
                ref_seq_segments[(s_beg, s_end)] = (d_beg, d_end)
        except ValueError:
            continue

    mapped_residues: dict[tuple[str, int], tuple[str, int, gemmi.Residue]] = {}
    mapped_chain_keys: set[str] = set()
    for seg in mappings:
        unp_start = int(seg["unp_start"])
        unp_end = int(seg["unp_end"])
        pdb_start = int(seg["start"]["residue_number"])
        pdb_end = int(seg["end"]["residue_number"])
        if (unp_end - unp_start) != (pdb_end - pdb_start):
            if (pdb_start, pdb_end) in ref_seq_segments:
                unp_start, unp_end = ref_seq_segments[(pdb_start, pdb_end)]
            else:
                return (
                    f"{pdb_id} skipped: gapped SIFTS segment (UniProt {unp_start}-{unp_end} "
                    f"vs PDB label_seq_id {pdb_start}-{pdb_end})",
                    {},
                    {},
                )
        ch_candidates = [seg.get("struct_asym_id"), seg.get("chain_id")]
        for offset in range(unp_end - unp_start + 1):
            u_pos = unp_start + offset
            label_seq_id = pdb_start + offset
            for ch_cand in ch_candidates:
                if ch_cand and (ch_cand, label_seq_id) in polymer_by_label_seq:
                    actual_chain_name, res_obj = polymer_by_label_seq[(ch_cand, label_seq_id)]
                    mapped_residues[(ch_cand, u_pos)] = (actual_chain_name, label_seq_id, res_obj)
                    mapped_chain_keys.add(ch_cand)
                    break

    if not mapped_residues:
        return f"{pdb_id} skipped: no polymer residues matched SIFTS label_seq_id", {}, {}

    contacting_pairs: list[tuple[str, str, list[gemmi.Atom]]] = []
    for ch_key in sorted(mapped_chain_keys):
        ch_atoms = chain_heavy_atoms.get(ch_key, [])
        if not ch_atoms:
            continue
        for lig_chain_name, copy_id, lig_atoms in ligand_copies:
            if lig_chain_name == ch_key:
                contacting_pairs.append((ch_key, copy_id, lig_atoms))
                continue
            min_chain_dist = min(ca.pos.dist(la.pos) for ca in ch_atoms for la in lig_atoms)
            if min_chain_dist <= BINDING_POCKET_CUTOFF_ANGSTROM:
                contacting_pairs.append((ch_key, copy_id, lig_atoms))

    if not contacting_pairs:
        for ch_key in sorted(mapped_chain_keys):
            for _, copy_id, lig_atoms in ligand_copies:
                contacting_pairs.append((ch_key, copy_id, lig_atoms))

    best_contacts: dict[int, tuple[float, str, str, int, gemmi.Residue]] = {}
    for ch_key, copy_id, lig_atoms in contacting_pairs:
        for (m_ch, u_pos), (actual_chain_name, label_seq_id, res_obj) in mapped_residues.items():
            if m_ch != ch_key:
                continue
            res_heavy = [a for a in res_obj if not a.is_hydrogen()]
            if not res_heavy:
                continue
            dist = min(ra.pos.dist(la.pos) for ra in res_heavy for la in lig_atoms)
            prev = best_contacts.get(u_pos)
            if prev is None or dist < prev[0]:
                best_contacts[u_pos] = (dist, actual_chain_name, copy_id, label_seq_id, res_obj)

    engineered_mismatches: list[str] = []
    contacts_out: dict[int, ResidueContact] = {}
    all_chain_contacts_out: dict[int, ResidueContact] = {}

    for u_pos, (dist, chain_name, copy_id, label_seq_id, res_obj) in sorted(best_contacts.items()):
        in_pocket = dist <= BINDING_POCKET_CUTOFF_ANGSTROM
        pdb_aa = _three_to_one(res_obj.name)
        eng = (
            engineered_map.get((chain_name, label_seq_id))
            or engineered_map.get((res_obj.subchain, label_seq_id))
            or engineered_map.get(("*", label_seq_id))
        )
        wt_aa = eng[0] if eng is not None else pdb_aa
        all_chain_contacts_out[u_pos] = ResidueContact(
            uniprot_pos=u_pos,
            uniprot_wild_type=wt_aa,
            pdb_residue_name=res_obj.name.strip(),
            label_seq_id=label_seq_id,
            chain=chain_name,
            ligand_copy=copy_id,
            contact_distance_angstrom=round(dist, 4),
            in_binding_pocket=in_pocket,
            covalent=dist < COVALENT_CUTOFF_ANGSTROM,
        )
        if not variants_by_pos and in_pocket and eng is not None and eng[0] != eng[1]:
            mut_label = f"{eng[0]}{u_pos}{eng[1]}"
            if mut_label not in engineered_mismatches:
                engineered_mismatches.append(mut_label)

    for u_pos, pos_variants in sorted(variants_by_pos.items()):
        if u_pos not in best_contacts:
            continue
        dist, chain_name, copy_id, label_seq_id, res_obj = best_contacts[u_pos]
        in_pocket = dist <= BINDING_POCKET_CUTOFF_ANGSTROM
        has_drug_annotation = any(is_drug_response_variant(v) for v in pos_variants)
        is_reported = in_pocket or has_drug_annotation

        expected_wt = pos_variants[0].original_residue
        pdb_aa = _three_to_one(res_obj.name)

        if is_reported and pdb_aa != expected_wt:
            eng = (
                engineered_map.get((chain_name, label_seq_id))
                or engineered_map.get((res_obj.subchain, label_seq_id))
                or engineered_map.get(("*", label_seq_id))
            )
            if eng is not None and eng[0] == expected_wt and eng[1] == pdb_aa:
                mut_label = f"{expected_wt}{u_pos}{pdb_aa}"
                if mut_label not in engineered_mismatches:
                    engineered_mismatches.append(mut_label)
                continue
            raise SiftsMappingError(
                f"Fatal SIFTS mapping mismatch in {pdb_id} at UniProt position {u_pos} "
                f"(chain {chain_name}, label_seq {label_seq_id}): expected UniProt wild-type "
                f"'{expected_wt}', found PDB residue '{res_obj.name}' ('{pdb_aa}')"
            )

        if is_reported:
            contacts_out[u_pos] = ResidueContact(
                uniprot_pos=u_pos,
                uniprot_wild_type=expected_wt,
                pdb_residue_name=res_obj.name.strip(),
                label_seq_id=label_seq_id,
                chain=chain_name,
                ligand_copy=copy_id,
                contact_distance_angstrom=round(dist, 4),
                in_binding_pocket=in_pocket,
                covalent=dist < COVALENT_CUTOFF_ANGSTROM,
            )

    if engineered_mismatches:
        return f"{pdb_id} skipped: {'/'.join(engineered_mismatches)} engineered", {}, {}

    return None, contacts_out, all_chain_contacts_out


async def _fetch_candidate_bundle_async(
    pdb_id: str,
    timeout: float,
    cache: JsonCache,
    client: httpx.AsyncClient | None,
) -> tuple[str, CachedResponse | None, CachedResponse | None, CachedResponse | None, Exception | None]:
    """Fetch metadata, SIFTS, and mmCIF for a single candidate PDB ID via shared/cache.py."""
    entry_url = RCSB_ENTRY_URL.format(pdb_id=pdb_id)
    sifts_url = PDBE_SIFTS_URL.format(pdb_id_lower=pdb_id.lower())
    cif_url = RCSB_CIF_URL.format(pdb_id=pdb_id)
    try:
        meta_task = cached_get_json(
            entry_url,
            client=client,
            cache=cache,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
        sifts_task = cached_get_json(
            sifts_url,
            client=client,
            cache=cache,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
        if client is not None:
            async def _fetch_cif_with_client() -> str:
                resp = await client.get(cif_url, timeout=timeout)
                resp.raise_for_status()
                return resp.text

            cif_task = cache.fetch(f"TEXT {cif_url}", None, _fetch_cif_with_client)
        else:
            cif_task = cached_get_text(cif_url, cache=cache, timeout=timeout)

        meta_cache, sifts_cache, cif_cache = await asyncio.gather(
            meta_task, sifts_task, cif_task
        )
        return pdb_id, meta_cache, sifts_cache, cif_cache, None
    except Exception as exc:
        return pdb_id, None, None, None, exc


async def _fallback_alphafold(
    uniprot_id: str,
    variants: list[Variant],
    as_of: date | None,
    skipped_structures: list[str],
    missing: list[str],
    *,
    timeout: float,
    cache: JsonCache,
    client: httpx.AsyncClient | None,
) -> StructureAnalysisResult:
    """Fall back to AlphaFold DB + UniProt binding site annotations when no drug-bound PDB exists."""
    af_url = ALPHAFOLD_PREDICTION_URL.format(uniprot_id=uniprot_id)
    up_url = UNIPROT_BINDING_URL.format(uniprot_id=uniprot_id)

    try:
        af_entry, up_entry = await asyncio.gather(
            cached_get_json(
                af_url,
                client=client,
                cache=cache,
                timeout=timeout,
                headers={"Accept": "application/json"},
            ),
            cached_get_json(
                up_url,
                client=client,
                cache=cache,
                timeout=timeout,
                headers={"Accept": "application/json"},
            ),
        )
    except httpx.TimeoutException:
        return StructureAnalysisResult(
            status="timeout",
            confidence="low",
            ligand_status="not_found",
            skipped_structures=skipped_structures,
            missing=missing + [f"AlphaFold/UniProt fallback timed out for {uniprot_id}"],
        )
    except httpx.HTTPError as exc:
        return StructureAnalysisResult(
            status="failed",
            confidence="low",
            ligand_status="not_found",
            skipped_structures=skipped_structures,
            missing=missing + [f"AlphaFold/UniProt fallback failed for {uniprot_id}: {exc}"],
        )

    af_list = af_entry.data if isinstance(af_entry.data, list) else []
    if not af_list:
        return StructureAnalysisResult(
            status="partial",
            confidence="low",
            ligand_status="not_found",
            skipped_structures=skipped_structures,
            missing=missing + [f"No AlphaFold structure found for {uniprot_id}"],
        )

    af_record = af_list[0]
    structure_id = af_record.get("modelEntityId") or f"AF-{uniprot_id}-F1"
    structure_url = (
        af_record.get("cifUrl")
        or af_record.get("pdbUrl")
        or f"https://alphafold.ebi.ac.uk/entry/{uniprot_id}"
    )

    binding_positions: set[int] = set()
    for feat in (up_entry.data or {}).get("features") or []:
        if feat.get("type") not in ("Binding site", "Active site"):
            continue
        loc = feat.get("location") or {}
        start_val = (loc.get("start") or {}).get("value")
        end_val = (loc.get("end") or {}).get("value")
        if start_val is not None and end_val is not None:
            for pos in range(int(start_val), int(end_val) + 1):
                binding_positions.add(pos)

    ledger_rows: list[LedgerRow] = []
    residues_out: list[ResidueEvidence] = []
    seen_residues: set[int] = set()

    af_ledger_id = f"market-structure-alphafold-{uniprot_id}"
    ledger_rows.append(
        LedgerRow(
            id=af_ledger_id,
            claim=(
                f"No drug-bound experimental PDB structure available for {uniprot_id}; "
                f"fell back to AlphaFold model {structure_id} and UniProt binding/active site annotations."
            ),
            source_record=f"AlphaFold:{structure_id}",
            url=f"{af_url} (secondary: {up_url})",
            date_pulled=af_entry.fetched_at,
            licence=ALPHAFOLD_LICENCE,
            check_id="market",
            evidence_date=None,
        )
    )

    for variant in variants:
        in_pocket = variant.position in binding_positions
        is_drug_resp = is_drug_response_variant(variant)
        if not (in_pocket or is_drug_resp):
            continue

        reason_ctx = (
            "in UniProt annotated binding/active site (AlphaFold fallback, no ligand contact distance)"
            if in_pocket
            else "annotated drug response/resistance variant"
        )
        v_row = build_variant_ledger_row(variant, reason_context=reason_ctx)
        ledger_rows.append(v_row)

        if variant.position not in seen_residues:
            seen_residues.add(variant.position)
            residues_out.append(
                ResidueEvidence(
                    residue=f"{variant.original_residue}{variant.position}",
                    chain=af_record.get("chainId") or "A",
                    contact_distance_angstrom=-1.0,
                    variant_frequency=variant.variant_frequency,
                    in_binding_pocket=in_pocket,
                    evidence_url=variant.url,
                    ledger_row_id=v_row.id,
                )
            )

    skip_clause = f" Skipped PDB structures: {'; '.join(skipped_structures)}." if skipped_structures else ""
    confidence_note = (
        "confidence=low: no drug-bound experimental PDB structure found on or before as_of; "
        "fell back to AlphaFold DB model with no bound ligand, using UniProt binding/active site "
        f"annotations only instead of heavy-atom contact distances.{skip_clause}"
    )

    payload = StructurePayload(
        source="alphafold",
        structure_id=structure_id,
        structure_url=structure_url,
        confidence_note=confidence_note,
        residues=residues_out,
    )

    return StructureAnalysisResult(
        status="partial",
        confidence="low",
        ligand_status="not_found",
        skipped_structures=skipped_structures,
        structure_payload=payload,
        ledger_rows=ledger_rows,
        missing=missing
        + [
            f"Structure fallback used: AlphaFold DB model {structure_id} (no drug-bound PDB structure available)"
        ],
    )


async def analyze_structure(
    asset: Asset,
    variants: list[Variant],
    *,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    cache: JsonCache | None = None,
    cache_dir: Path | None = None,
    client: httpx.AsyncClient | None = None,
) -> StructureAnalysisResult:
    """Resolve drug ligand, concurrently download up to 5 top PDB candidates, and compute pocket distances."""
    uniprot_id = (asset.ids.get("uniprot_id") or "").strip().upper()
    if not uniprot_id:
        return StructureAnalysisResult(
            status="failed",
            confidence="low",
            ligand_status="not_found",
            missing=["Asset.ids['uniprot_id'] is missing"],
        )

    eff_cache = cache or (JsonCache(cache_dir) if cache_dir is not None else default_cache())

    variants_by_pos: dict[int, list[Variant]] = {}
    for v in variants:
        variants_by_pos.setdefault(v.position, []).append(v)

    lig_res = await resolve_ligand_comp_ids(
        asset, timeout=timeout, cache=eff_cache, client=client
    )
    if lig_res.status == "ambiguous":
        ambig_row = LedgerRow(
            id=f"market-structure-ambiguous-ligand-{uniprot_id}",
            claim=(
                f"RCSB chemical component search for '{asset.drug}' returned multiple candidate "
                f"ligand IDs ({', '.join(lig_res.candidates)}); manual confirmation required."
            ),
            source_record=f"RCSB:mol_definition:{','.join(lig_res.candidates)}",
            url=f"{lig_res.url} (secondary: https://www.rcsb.org)",
            date_pulled=lig_res.date_pulled or utc_now(),
            licence=RCSB_PDB_LICENCE,
            check_id="market",
            evidence_date=None,
        )
        return StructureAnalysisResult(
            status="partial",
            confidence="low",
            ligand_status="ambiguous",
            candidate_ligands=lig_res.candidates,
            ledger_rows=[ambig_row],
            missing=[
                f"Ambiguous ligand candidates for '{asset.drug}': {', '.join(lig_res.candidates)}"
            ],
        )
    if lig_res.status == "timeout":
        return StructureAnalysisResult(
            status="timeout",
            confidence="low",
            ligand_status="timeout",
            missing=[f"Ligand resolution timed out for '{asset.drug}'"],
        )
    if lig_res.status in ("not_found", "failed") or not lig_res.chem_comp_id:
        missing_msg = (
            lig_res.error_message
            or f"Could not resolve drug '{asset.drug}' to a PDB chemical component ID"
        )
        return await _fallback_alphafold(
            uniprot_id=uniprot_id,
            variants=variants,
            as_of=asset.as_of,
            skipped_structures=[],
            missing=[missing_msg],
            timeout=timeout,
            cache=eff_cache,
            client=client,
        )

    chem_comp_id = lig_res.chem_comp_id

    nodes: list[dict[str, Any]] = [
        {
            "type": "terminal",
            "service": "text",
            "parameters": {
                "attribute": "rcsb_polymer_entity_container_identifiers.reference_sequence_identifiers.database_accession",
                "operator": "exact_match",
                "value": uniprot_id,
            },
        },
        {
            "type": "terminal",
            "service": "text",
            "parameters": {
                "attribute": "rcsb_nonpolymer_entity_container_identifiers.nonpolymer_comp_id",
                "operator": "exact_match",
                "value": chem_comp_id,
            },
        },
    ]
    search_body = {
        "query": {"type": "group", "logical_operator": "and", "nodes": nodes},
        "request_options": {
            "sort": [{"sort_by": "rcsb_entry_info.resolution_combined", "direction": "asc"}]
        },
        "return_type": "entry",
    }

    try:
        search_entry = await _safe_cached_post_json(
            RCSB_SEARCH_URL,
            search_body,
            timeout=timeout,
            cache=eff_cache,
            client=client,
        )
    except httpx.TimeoutException:
        return StructureAnalysisResult(
            status="timeout",
            confidence="low",
            ligand_status="resolved",
            chem_comp_id=chem_comp_id,
            missing=[f"RCSB structure search timed out for {uniprot_id} + {chem_comp_id}"],
        )
    except httpx.HTTPError as exc:
        return StructureAnalysisResult(
            status="failed",
            confidence="low",
            ligand_status="resolved",
            chem_comp_id=chem_comp_id,
            missing=[f"RCSB structure search failed for {uniprot_id} + {chem_comp_id}: {exc}"],
        )

    all_candidate_ids = [
        item["identifier"].upper()
        for item in ((search_entry.data or {}).get("result_set") or [])
        if item.get("identifier")
    ]
    candidate_pdb_ids = all_candidate_ids[:MAX_CANDIDATE_STRUCTURES]

    fetched_bundles = await asyncio.gather(
        *(
            _fetch_candidate_bundle_async(
                pdb_id,
                timeout,
                eff_cache,
                client,
            )
            for pdb_id in candidate_pdb_ids
        )
    )

    skipped_structures: list[str] = []
    selected_pdb_id: str | None = None
    selected_resolution: float | None = None
    selected_release_date: str | None = None
    selected_contacts: dict[int, ResidueContact] = {}
    selected_all_contacts: dict[int, ResidueContact] = {}
    selected_entry_pulled: datetime = search_entry.fetched_at

    for pdb_id, meta_cache, sifts_cache, cif_cache, err in fetched_bundles:
        if isinstance(err, httpx.TimeoutException):
            return StructureAnalysisResult(
                status="timeout",
                confidence="low",
                ligand_status="resolved",
                chem_comp_id=chem_comp_id,
                candidate_structures_considered=len(candidate_pdb_ids),
                skipped_structures=skipped_structures,
                missing=[f"Timed out fetching structure or SIFTS data for {pdb_id}"],
            )
        if err is not None or meta_cache is None or sifts_cache is None or cif_cache is None:
            skipped_structures.append(f"{pdb_id} skipped: HTTP error ({err})")
            continue

        meta = meta_cache.data
        rel_str = ((meta.get("rcsb_accession_info") or {}).get("initial_release_date") or "")[:10]
        res_list = (meta.get("rcsb_entry_info") or {}).get("resolution_combined") or []
        resolution = float(res_list[0]) if res_list else None

        if asset.as_of is not None and rel_str:
            rel_date = date.fromisoformat(rel_str)
            if rel_date > asset.as_of:
                skipped_structures.append(
                    f"{pdb_id} skipped: released {rel_str} after as_of {asset.as_of.isoformat()}"
                )
                continue

        skip_reason, contacts_map, all_contacts_map = await asyncio.to_thread(
            _evaluate_pdb_candidate_sync,
            pdb_id,
            uniprot_id,
            chem_comp_id,
            cif_cache.data,
            sifts_cache.data,
            variants_by_pos,
        )
        if skip_reason is not None:
            skipped_structures.append(skip_reason)
            continue

        selected_pdb_id = pdb_id
        selected_resolution = resolution
        selected_release_date = rel_str
        selected_contacts = contacts_map
        selected_all_contacts = all_contacts_map
        selected_entry_pulled = meta_cache.fetched_at
        break

    if selected_pdb_id is None:
        return await _fallback_alphafold(
            uniprot_id=uniprot_id,
            variants=variants,
            as_of=asset.as_of,
            skipped_structures=skipped_structures,
            missing=[],
            timeout=timeout,
            cache=eff_cache,
            client=client,
        )

    ledger_rows: list[LedgerRow] = []
    residues_out: list[ResidueEvidence] = []
    covalent_residues: list[str] = []

    pdb_entry_url = RCSB_ENTRY_URL.format(pdb_id=selected_pdb_id)
    pdb_page_url = RCSB_STRUCTURE_PAGE_URL.format(pdb_id=selected_pdb_id)
    sifts_api_url = PDBE_SIFTS_URL.format(pdb_id_lower=selected_pdb_id.lower())

    res_text = f"{selected_resolution:.2f} Å" if selected_resolution is not None else "unknown resolution"
    ev_date = date.fromisoformat(selected_release_date) if selected_release_date else None
    ledger_rows.append(
        LedgerRow(
            id=f"market-structure-pdb-{selected_pdb_id}",
            claim=(
                f"Selected experimental co-crystal structure {selected_pdb_id} ({res_text}, "
                f"released {selected_release_date}) of {uniprot_id} bound to ligand {chem_comp_id}, "
                f"with residue numbering mapped via PDBe SIFTS label_seq_id."
            ),
            source_record=f"PDB:{selected_pdb_id}|SIFTS:{selected_pdb_id}:{uniprot_id}",
            url=f"{pdb_entry_url} | {sifts_api_url} (secondary: {pdb_page_url})",
            date_pulled=selected_entry_pulled,
            licence=COMBINED_PDB_SIFTS_LICENCE,
            check_id="market",
            evidence_date=ev_date,
        )
    )

    for u_pos, contact in sorted(selected_contacts.items()):
        pos_variants = sorted(
            variants_by_pos.get(u_pos, []),
            key=variant_annotation_priority_key,
        )
        if not pos_variants:
            continue

        res_label = f"{contact.uniprot_wild_type}{u_pos}"
        if contact.covalent:
            covalent_residues.append(res_label)
            dist_desc = (
                f"covalent attachment ({contact.contact_distance_angstrom:.2f} Å) to "
                f"ligand {contact.ligand_copy} on chain {contact.chain}"
            )
        elif contact.in_binding_pocket:
            dist_desc = (
                f"in binding pocket at {contact.contact_distance_angstrom:.2f} Å from "
                f"ligand {contact.ligand_copy} on chain {contact.chain}"
            )
        else:
            dist_desc = (
                f"annotated drug response/resistance variant at {contact.contact_distance_angstrom:.2f} Å "
                f"from ligand {contact.ligand_copy} on chain {contact.chain}"
            )

        primary_variant: Variant | None = None
        for variant in pos_variants:
            if not (contact.in_binding_pocket or is_drug_response_variant(variant)):
                continue
            v_row = build_variant_ledger_row(
                variant,
                reason_context=f"{dist_desc} in PDB {selected_pdb_id}",
            )
            ledger_rows.append(v_row)
            if primary_variant is None:
                primary_variant = variant

        if primary_variant is not None:
            residues_out.append(
                ResidueEvidence(
                    residue=res_label,
                    chain=contact.chain,
                    contact_distance_angstrom=round(contact.contact_distance_angstrom, 3),
                    variant_frequency=primary_variant.variant_frequency,
                    in_binding_pocket=contact.in_binding_pocket,
                    evidence_url=primary_variant.url,
                    ledger_row_id=primary_variant.id,
                )
            )

    eff_conf: Literal["high", "medium", "low"] = "high" if asset.as_of is None else "medium"
    note_parts = [
        f"confidence={eff_conf}: experimental co-crystal structure {selected_pdb_id} ({res_text}) bound to {chem_comp_id}."
    ]
    if covalent_residues:
        cov_details = ", ".join(
            f"{r} ({selected_contacts[int(r[1:])].contact_distance_angstrom:.2f} Å)"
            for r in covalent_residues
        )
        note_parts.append(f"Detected covalent attachment at {cov_details}.")
    if skipped_structures:
        note_parts.append(f"Skipped structures: {'; '.join(skipped_structures)}.")

    structure_payload = StructurePayload(
        source="pdb",
        structure_id=selected_pdb_id,
        structure_url=RCSB_CIF_URL.format(pdb_id=selected_pdb_id),
        confidence_note=" ".join(note_parts),
        residues=residues_out,
    )

    return StructureAnalysisResult(
        status="complete",
        confidence=eff_conf,
        ligand_status="resolved",
        chem_comp_id=chem_comp_id,
        candidate_ligands=[chem_comp_id],
        candidate_structures_considered=len(candidate_pdb_ids),
        skipped_structures=skipped_structures,
        covalent_residues=covalent_residues,
        contact_by_position=selected_contacts,
        all_chain_contacts=selected_all_contacts,
        structure_payload=structure_payload,
        ledger_rows=ledger_rows,
        missing=[],
    )


async def rank_structure_only_as_of(
    asset: Asset,
    as_of: date | None = None,
    in_pocket_cutoff: float = BINDING_POCKET_CUTOFF_ANGSTROM,
    *,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    cache: JsonCache | None = None,
    cache_dir: Path | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[ResidueContact]:
    """Evaluation harness entry point: resolve structure on or before as_of and rank all chain residues by distance only."""
    effective_asset = asset.model_copy(update={"as_of": as_of}) if as_of is not None else asset
    struct_res = await analyze_structure(
        effective_asset,
        variants=[],
        timeout=timeout,
        cache=cache,
        cache_dir=cache_dir,
        client=client,
    )
    return rank_by_structure_only(struct_res.all_chain_contacts, in_pocket_cutoff=in_pocket_cutoff)
