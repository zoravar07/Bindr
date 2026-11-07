"""Fetch and parse missense protein variants from the EBI Proteins Variation API."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
import httpx
from pydantic import BaseModel, Field

from shared.cache import JsonCache, cached_get_json, default_cache
from shared.schemas import LedgerRow

EBI_VARIATION_ENDPOINT = (
    "https://www.ebi.ac.uk/proteins/api/variation/{uniprot_id}?consequencetype=missense"
)
UNIPROT_VARIANT_VIEWER_URL = "https://www.uniprot.org/uniprotkb/{uniprot_id}/variant-viewer"
REQUEST_TIMEOUT_SECONDS = 15.0

FEATURE_USED_KEYS: tuple[str, ...] = (
    "type",
    "consequenceType",
    "begin",
    "end",
    "wildType",
    "mutatedType",
    "alternativeSequence",
    "ftId",
    "sourceType",
    "codon",
    "xrefs",
    "clinicalSignificances",
    "association",
    "descriptions",
    "populationFrequencies",
)

SOURCE_LICENCE_MAP: dict[str, str] = {
    "UniProt": "CC BY 4.0",
    "ClinVar": "Public Domain (NCBI)",
    "dbSNP": "Public Domain (NCBI)",
    "COSMIC": "see source terms",
    "NCI-TCGA": "see source terms",
    "Ensembl": "see source terms",
    "gnomAD": "see source terms",
    "ClinGen": "see source terms",
    "1000Genomes": "see source terms",
    "ExAC": "see source terms",
    "ESP": "see source terms",
    "TOPMed": "see source terms",
}

UNCUTTABLE_AS_OF_FIELDS: list[str] = [
    "features[].begin/end/wildType/mutatedType (variant creation date not exposed by EBI Proteins Variation API)",
    "features[].clinicalSignificances (clinical assertion dates not exposed inline)",
    "features[].descriptions (annotation curation timestamps not exposed inline)",
    "features[].populationFrequencies (allele frequency release dates not exposed inline)",
    "features[].xrefs (cross-reference addition dates not exposed inline)",
    "features[].evidences (evidence publication dates require external PMID/UniSave lookup)",
]


class Variant(BaseModel):
    """Single-amino-acid missense variant grouped by (position, original_residue, new_residue).

    Note: `variant_frequency` is a germline population allele frequency (e.g. gnomAD MAF/AF),
    NOT clinical or somatic resistance prevalence. It is recorded for display/context in
    ResidueEvidence only and must NEVER feed or weight a failure risk score.
    """

    id: str
    uniprot_id: str
    position: int
    original_residue: str
    new_residue: str
    source: str
    sources: list[str] = Field(default_factory=list)
    xrefs: list[dict[str, str]] = Field(default_factory=list)
    clinical_significance: str | None = None
    descriptions: list[str] = Field(default_factory=list)
    variant_frequency: float | None = Field(
        default=None,
        description=(
            "Germline population allele frequency (e.g. gnomAD MAF/AF), NOT resistance "
            "prevalence. Must never be used to compute or weight a risk score."
        ),
    )
    population_frequencies: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Raw germline population allele frequency entries from EBI Variation.",
    )
    ft_id: str | None = None
    codon: str | None = None
    source_record: str
    url: str
    licence: str
    date_pulled: datetime

    @property
    def short_name(self) -> str:
        """Return standard mutation shorthand, e.g. 'T790M'."""
        return f"{self.original_residue}{self.position}{self.new_residue}"


class VariantFetchResult(BaseModel):
    """Output of fetch_variants containing Variant objects and as_of status."""

    variants: list[Variant]
    as_of_enforced: bool
    status: Literal["complete", "partial", "failed", "timeout"]
    missing: list[str] = Field(default_factory=list)


def trim_variation_payload(raw_payload: dict[str, Any]) -> dict[str, Any]:
    """Trim raw EBI Variation response to only the fields used by fetch_variants."""
    trimmed_features: list[dict[str, Any]] = []
    for feat in raw_payload.get("features") or []:
        if feat.get("type") != "VARIANT" or feat.get("consequenceType") != "missense":
            continue
        item: dict[str, Any] = {}
        for k in (
            "type",
            "consequenceType",
            "begin",
            "end",
            "wildType",
            "mutatedType",
            "alternativeSequence",
            "ftId",
            "sourceType",
            "codon",
        ):
            if feat.get(k) is not None:
                item[k] = feat[k]

        if feat.get("xrefs"):
            item["xrefs"] = [
                {
                    k: v
                    for k, v in x.items()
                    if k in ("name", "id", "url") and v is not None
                }
                for x in feat["xrefs"]
            ]

        if feat.get("clinicalSignificances"):
            item["clinicalSignificances"] = [
                {
                    k: v
                    for k, v in cs.items()
                    if k in ("type", "sources") and v is not None
                }
                for cs in feat["clinicalSignificances"]
            ]

        if feat.get("association"):
            assoc_trimmed = [
                {"clinicalSignificances": a["clinicalSignificances"]}
                for a in feat["association"]
                if a.get("clinicalSignificances")
            ]
            if assoc_trimmed:
                item["association"] = assoc_trimmed

        if feat.get("descriptions"):
            item["descriptions"] = [
                {
                    k: v
                    for k, v in d.items()
                    if k in ("value", "sources") and v is not None
                }
                for d in feat["descriptions"]
            ]

        if feat.get("populationFrequencies"):
            item["populationFrequencies"] = [
                {
                    k: v
                    for k, v in pf.items()
                    if k in ("populationName", "frequency", "source") and v is not None
                }
                for pf in feat["populationFrequencies"]
            ]

        trimmed_features.append(item)

    return {
        "accession": raw_payload.get("accession"),
        "sequence": raw_payload.get("sequence"),
        "features": trimmed_features,
    }


def build_variant_ledger_row(variant: Variant, reason_context: str | None = None) -> LedgerRow:
    """Create a LedgerRow for a Variant only when it is used in a claim."""
    sig_clause = f"; clinical significance: {variant.clinical_significance}" if variant.clinical_significance else ""
    ctx_clause = f" ({reason_context})" if reason_context else ""
    claim = (
        f"Missense variant {variant.short_name} (position {variant.position}, "
        f"{variant.original_residue} -> {variant.new_residue}) in {variant.uniprot_id} "
        f"reported via {variant.source}{sig_clause}{ctx_clause}."
    )
    return LedgerRow(
        id=variant.id,
        claim=claim,
        source_record=variant.source_record,
        url=variant.url,
        date_pulled=variant.date_pulled,
        licence=variant.licence,
        check_id="market",
        evidence_date=None,
    )


def _normalize_source_name(raw_name: str) -> str:
    """Normalize source names from EBI Variation xrefs, descriptions, and significances."""
    lowered = raw_name.strip().lower()
    if lowered == "uniprot":
        return "UniProt"
    if lowered == "clinvar":
        return "ClinVar"
    if lowered in ("cosmic curated", "nci-tcga cosmic", "cosmic"):
        return "COSMIC"
    if lowered == "dbsnp":
        return "dbSNP"
    if lowered.startswith("gnomad"):
        return "gnomAD"
    if lowered == "ensembl":
        return "Ensembl"
    if lowered == "clingen":
        return "ClinGen"
    if lowered == "nci-tcga":
        return "NCI-TCGA"
    return raw_name.strip()


def _select_primary_source(sources: list[str]) -> str:
    """Pick the highest-priority canonical source from a list of normalized sources."""
    priority = ["UniProt", "ClinVar", "COSMIC", "dbSNP"]
    for candidate in priority:
        if candidate in sources:
            return candidate
    return sources[0] if sources else "UniProt"


def _collect_feature_sources(feature: dict[str, Any]) -> list[str]:
    """Collect normalized source names from a single raw feature dict."""
    seen: list[str] = []

    def _add(name: str) -> None:
        norm = _normalize_source_name(name)
        if norm and norm not in seen:
            seen.append(norm)

    if feature.get("ftId") or feature.get("sourceType") == "uniprot":
        _add("UniProt")

    for desc in feature.get("descriptions") or []:
        for src in desc.get("sources") or []:
            _add(src)

    for cs in feature.get("clinicalSignificances") or []:
        for src in cs.get("sources") or []:
            _add(src)

    for xref in feature.get("xrefs") or []:
        if xref.get("name"):
            _add(xref["name"])

    for pf in feature.get("populationFrequencies") or []:
        if pf.get("source"):
            _add(pf["source"])

    return seen


def _extract_source_record(
    ft_id: str | None,
    xrefs: list[dict[str, str]],
    uniprot_id: str,
    primary_source: str,
    short_name: str,
) -> str:
    """Build a traceable source_record identifier from merged ft_id and xrefs."""
    if primary_source == "UniProt" and ft_id:
        return f"UniProt:{uniprot_id}:{ft_id}"

    for xref in xrefs:
        norm_name = _normalize_source_name(xref.get("name", ""))
        xref_id = xref.get("id")
        if norm_name == primary_source and xref_id:
            return f"{primary_source}:{xref_id}"

    if ft_id:
        return f"UniProt:{uniprot_id}:{ft_id}"

    for xref in xrefs:
        if xref.get("name") and xref.get("id"):
            return f"{_normalize_source_name(xref['name'])}:{xref['id']}"

    return f"EBI-Variation:{uniprot_id}:{short_name}"


def _parse_variation_payload(
    clean_id: str,
    raw_payload: dict[str, Any],
    date_pulled: datetime,
    provenance_url: str,
    as_of: date | None,
) -> VariantFetchResult:
    """Parse and group EBI Variation payload into deterministic Variant objects."""
    canonical_seq = (raw_payload.get("sequence") or "").strip()
    features = raw_payload.get("features") or []
    grouped: dict[tuple[int, str, str], dict[str, Any]] = {}

    for feature in features:
        if feature.get("type") != "VARIANT":
            continue
        if feature.get("consequenceType") != "missense":
            continue

        begin_raw = feature.get("begin")
        end_raw = feature.get("end")
        if begin_raw is None or end_raw is None:
            continue
        try:
            begin_pos = int(begin_raw)
            end_pos = int(end_raw)
        except (TypeError, ValueError):
            continue

        if begin_pos != end_pos or begin_pos < 1:
            continue

        wild_type = (feature.get("wildType") or "").strip()
        mutated_type = (feature.get("mutatedType") or feature.get("alternativeSequence") or "").strip()
        if (
            len(wild_type) != 1
            or len(mutated_type) != 1
            or not wild_type.isalpha()
            or not mutated_type.isalpha()
        ):
            continue

        if canonical_seq and (
            begin_pos > len(canonical_seq) or canonical_seq[begin_pos - 1] != wild_type
        ):
            continue

        key = (begin_pos, wild_type, mutated_type)
        bucket = grouped.get(key)
        if bucket is None:
            bucket = {
                "sources": [],
                "xrefs": [],
                "xref_keys": set(),
                "sig_types": [],
                "descriptions": [],
                "pop_freqs": [],
                "pop_freq_keys": set(),
                "ft_id": None,
                "codons": [],
            }
            grouped[key] = bucket

        for src in _collect_feature_sources(feature):
            if src not in bucket["sources"]:
                bucket["sources"].append(src)

        for xref in feature.get("xrefs") or []:
            x_name = (xref.get("name") or "").strip()
            x_id = (xref.get("id") or "").strip()
            if x_name and x_id and (x_name, x_id) not in bucket["xref_keys"]:
                bucket["xref_keys"].add((x_name, x_id))
                entry: dict[str, str] = {"name": x_name, "id": x_id}
                if xref.get("url"):
                    entry["url"] = str(xref["url"])
                bucket["xrefs"].append(entry)

        for cs in feature.get("clinicalSignificances") or []:
            cs_type = cs.get("type")
            if cs_type and cs_type not in bucket["sig_types"]:
                bucket["sig_types"].append(cs_type)

        for assoc in feature.get("association") or []:
            for cs in assoc.get("clinicalSignificances") or []:
                cs_type = cs.get("type")
                if cs_type and cs_type not in bucket["sig_types"]:
                    bucket["sig_types"].append(cs_type)

        for desc in feature.get("descriptions") or []:
            val = (desc.get("value") or "").strip()
            if val and val not in bucket["descriptions"]:
                bucket["descriptions"].append(val)

        for pf in feature.get("populationFrequencies") or []:
            freq_val = pf.get("frequency")
            if freq_val is None:
                continue
            numeric_freq = float(freq_val)
            pf_key = (pf.get("populationName", ""), pf.get("source", ""), numeric_freq)
            if pf_key not in bucket["pop_freq_keys"]:
                bucket["pop_freq_keys"].add(pf_key)
                bucket["pop_freqs"].append(
                    {
                        "populationName": pf.get("populationName", ""),
                        "frequency": numeric_freq,
                        "source": pf.get("source", ""),
                    }
                )

        if not bucket["ft_id"] and feature.get("ftId"):
            bucket["ft_id"] = feature["ftId"]

        codon = (feature.get("codon") or "").strip()
        if codon and codon not in bucket["codons"]:
            bucket["codons"].append(codon)

    variants: list[Variant] = []

    for (begin_pos, wild_type, mutated_type), bucket in grouped.items():
        all_sources: list[str] = bucket["sources"] or ["UniProt"]
        primary_source = _select_primary_source(all_sources)
        licence = SOURCE_LICENCE_MAP.get(primary_source, "see source terms")
        clinical_sig = ", ".join(bucket["sig_types"]) if bucket["sig_types"] else None
        pop_freqs: list[dict[str, Any]] = bucket["pop_freqs"]
        variant_freq: float | None = pop_freqs[0]["frequency"] if pop_freqs else None

        short_name = f"{wild_type}{begin_pos}{mutated_type}"
        det_id = f"market-variant-{clean_id}-{short_name}"
        source_record = _extract_source_record(
            bucket["ft_id"], bucket["xrefs"], clean_id, primary_source, short_name
        )

        variant = Variant(
            id=det_id,
            uniprot_id=clean_id,
            position=begin_pos,
            original_residue=wild_type,
            new_residue=mutated_type,
            source=primary_source,
            sources=all_sources,
            xrefs=bucket["xrefs"],
            clinical_significance=clinical_sig,
            descriptions=bucket["descriptions"],
            variant_frequency=variant_freq,
            population_frequencies=pop_freqs,
            ft_id=bucket["ft_id"],
            codon=", ".join(bucket["codons"]) if bucket["codons"] else None,
            source_record=source_record,
            url=provenance_url,
            licence=licence,
            date_pulled=date_pulled,
        )
        variants.append(variant)

    if as_of is not None:
        return VariantFetchResult(
            variants=variants,
            as_of_enforced=False,
            status="partial",
            missing=list(UNCUTTABLE_AS_OF_FIELDS),
        )

    return VariantFetchResult(
        variants=variants,
        as_of_enforced=True,
        status="complete",
        missing=[],
    )


async def fetch_variants(
    uniprot_id: str,
    as_of: date | None = None,
    *,
    cache: JsonCache | None = None,
    cache_dir: Path | None = None,
    client: httpx.AsyncClient | None = None,
) -> VariantFetchResult:
    """Fetch single-residue missense variants for a UniProt accession from EBI Proteins API.

    Uses `shared/cache.py::cached_get_json` and parses CPU-bound payload in `asyncio.to_thread`.
    Returns Variant objects only; LedgerRows are created downstream only for variants
    used in a claim (in the binding pocket or annotated as drug response/resistance).
    """
    clean_id = uniprot_id.strip().upper()
    api_url = EBI_VARIATION_ENDPOINT.format(uniprot_id=clean_id)
    secondary_url = UNIPROT_VARIANT_VIEWER_URL.format(uniprot_id=clean_id)
    provenance_url = f"{api_url} (secondary: {secondary_url})"
    eff_cache = cache or (JsonCache(cache_dir) if cache_dir is not None else default_cache())

    try:
        cache_entry = await cached_get_json(
            api_url,
            client=client,
            cache=eff_cache,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"Accept": "application/json"},
        )
        raw_payload = cache_entry.data
        date_pulled = cache_entry.fetched_at
    except httpx.TimeoutException:
        return VariantFetchResult(
            variants=[],
            as_of_enforced=as_of is None,
            status="timeout",
            missing=[f"EBI Proteins Variation API request timed out for {clean_id}"],
        )
    except httpx.HTTPError as exc:
        return VariantFetchResult(
            variants=[],
            as_of_enforced=as_of is None,
            status="failed",
            missing=[f"EBI Proteins Variation API request failed for {clean_id}: {exc}"],
        )

    return await asyncio.to_thread(
        _parse_variation_payload,
        clean_id,
        raw_payload,
        date_pulled,
        provenance_url,
        as_of,
    )
