"""Market & resistance sub-agent entry point: run_check(asset, emit).

Escape Candidate Ranking & Deterministic Risk Score Heuristic:
    1. Escape Candidate Ranking (`rank_escape_candidates`):
       Reported variants are ranked strictly by 3D structural proximity and clinical
       annotation—germline population frequency (`variant_frequency`) NEVER enters
       the ranking key or score:
         (a) Variants at a covalent attachment residue (< 2.2 Å) always rank first;
         (b) Primary sort key is heavy-atom `contact_distance_angstrom` ascending,
             with bounded secondary boosts for resistance text, pathogenic labels,
             and curated EGFR escape residues so a VUS at 1.8 Å outranks a
             pathogenic variant at 4.4 Å.

    2. Uncalibrated Structural Exposure Heuristic (`compute_resistance_score`):
       This score is an UNCALIBRATED STRUCTURAL EXPOSURE HEURISTIC in [0.0, 1.0]
       (higher = higher structural/mutational exposure in the binding site), computed
       deterministically in Python. It MUST NOT be compared across targets, and it
       rises with how well studied a target is (densely sequenced targets like EGFR
       have far more catalogued somatic/germline variants in public databases than
       under-studied targets):
           score = min(
               1.0,
               0.35 * I_covalent
               + min(0.35, 0.15 * N_annotated_pocket)
               + min(0.20, 0.10 * ln(1 + N_pocket_variants / N_pocket_residues))
               + min(0.10, 0.05 * N_annotated_nonpocket)
           )
       where:
         - I_covalent in {0, 1}: 1 if at least one reported missense variant occurs at a
           covalent attachment residue (heavy-atom distance < 2.2 Å, e.g. C797S), else 0.
         - N_annotated_pocket: count of distinct binding-pocket residue positions (<= 4.5 Å)
           with an annotated resistance or pathogenic variant (e.g. T790M).
         - N_pocket_variants / N_pocket_residues: ratio of total missense variants in the
           binding pocket to the number of binding-pocket residue positions (<= 4.5 Å),
           log-scaled via 0.10 * ln(1 + density) so targets with different variant densities
           (e.g. venetoclax/BCL2 60/20=3.0 vs olaparib/PARP1 28/15=1.87) do not saturate at 0.20.
         - N_annotated_nonpocket: count of distinct non-pocket residue positions with an
           annotated resistance variant.
"""

from __future__ import annotations

import asyncio
from datetime import date
import inspect
import math
from pathlib import Path
from typing import Awaitable, Callable, Literal
import httpx
from pydantic import BaseModel

from checks.market.structure import (
    BINDING_POCKET_CUTOFF_ANGSTROM,
    COVALENT_CUTOFF_ANGSTROM,
    CURATED_EGFR_ESCAPE_RESIDUES,
    ResidueContact,
    StructureAnalysisResult,
    analyze_structure,
    is_clinically_actionable_variant,
    is_curated_escape_residue,
    is_drug_response_variant,
    rank_by_structure_only,
    rank_escape_candidates,
    rank_structure_only_as_of,
    variant_annotation_priority_key,
)
from checks.market.variants import Variant, VariantFetchResult, fetch_variants
from shared.cache import JsonCache, default_cache
from shared.schemas import (
    Asset,
    CheckResult,
    LedgerRow,
    Reason,
    StatusEvent,
    StructurePayload,
    utc_now,
)

VARIANTS_DEADLINE_SECONDS: float = 15.0
STRUCTURE_DEADLINE_SECONDS: float = 20.0

Emit = Callable[[StatusEvent], "Awaitable[None] | None"]


async def _emit_event(
    emit: Emit | None,
    status: str,
    message: str,
) -> None:
    """Emit a live StatusEvent with check_id='market' if an emit callback is provided."""
    if emit is None:
        return
    out = emit(
        StatusEvent(
            check_id="market",
            status=status,
            message=message,
            timestamp=utc_now(),
        )
    )
    if inspect.isawaitable(out):
        await out


class ScoreComponents(BaseModel):
    """Explicit breakdown of the four resistance score heuristic terms."""

    covalent_term: float
    annotated_pocket_term: float
    pocket_density_term: float
    non_pocket_annotated_term: float
    total_score: float


def compute_score_components(
    variants: list[Variant],
    contact_by_position: dict[int, ResidueContact],
    in_pocket_positions: set[int],
    *,
    use_annotations: bool = True,
    is_drug_bound_pdb: bool = True,
) -> ScoreComponents:
    """Compute the four additive terms and total of the uncalibrated structural exposure heuristic.

    This score is an uncalibrated structural exposure heuristic that must not be compared
    across targets, and rises with how well studied a target is. Population allele frequency
    (`variant_frequency`) and `CURATED_EGFR_ESCAPE_RESIDUES` never feed the score.
    When `use_annotations=False` (e.g. `as_of` is set), all annotation terms are skipped (0.0)
    so scoring is based on structure only.
    When `is_drug_bound_pdb=False` (AlphaFold fallback, unresolved drug, or ambiguous ligand),
    pocket, covalent, and density terms are not computed and all terms return 0.0.
    """
    if not is_drug_bound_pdb:
        return ScoreComponents(
            covalent_term=0.0,
            annotated_pocket_term=0.0,
            pocket_density_term=0.0,
            non_pocket_annotated_term=0.0,
            total_score=0.0,
        )

    covalent_positions: set[int] = {
        pos for pos, contact in contact_by_position.items() if contact.covalent
    }
    annotated_pocket_positions: set[int] = set()
    pocket_variant_positions: set[int] = set()
    n_pocket_variants: int = 0
    annotated_nonpocket_positions: set[int] = set()

    for variant in variants:
        pos = variant.position
        in_pocket = pos in in_pocket_positions
        contact = contact_by_position.get(pos)
        is_covalent = bool(contact and contact.covalent)

        if in_pocket:
            pocket_variant_positions.add(pos)
            n_pocket_variants += 1
            if is_covalent:
                covalent_positions.add(pos)
            if use_annotations and is_clinically_actionable_variant(variant):
                annotated_pocket_positions.add(pos)
        elif use_annotations and is_drug_response_variant(variant):
            annotated_nonpocket_positions.add(pos)

    covalent_term = round(0.35 if covalent_positions else 0.0, 4)
    annotated_pocket_term = (
        round(min(0.35, 0.15 * len(annotated_pocket_positions)), 4)
        if use_annotations
        else 0.0
    )
    n_pocket_residues = (
        len(in_pocket_positions)
        if in_pocket_positions
        else len(pocket_variant_positions)
    )
    if n_pocket_residues > 0 and n_pocket_variants > 0:
        variants_per_pocket_residue = n_pocket_variants / n_pocket_residues
        pocket_density_term = round(
            min(0.20, 0.10 * math.log1p(variants_per_pocket_residue)), 4
        )
    else:
        pocket_density_term = 0.0

    non_pocket_annotated_term = (
        round(min(0.10, 0.05 * len(annotated_nonpocket_positions)), 4)
        if use_annotations
        else 0.0
    )

    raw_score = (
        covalent_term
        + annotated_pocket_term
        + pocket_density_term
        + non_pocket_annotated_term
    )
    total_score = round(min(1.0, max(0.0, raw_score)), 4)
    return ScoreComponents(
        covalent_term=covalent_term,
        annotated_pocket_term=annotated_pocket_term,
        pocket_density_term=pocket_density_term,
        non_pocket_annotated_term=non_pocket_annotated_term,
        total_score=total_score,
    )


def compute_resistance_score(
    variants: list[Variant],
    contact_by_position: dict[int, ResidueContact],
    in_pocket_positions: set[int],
    *,
    use_annotations: bool = True,
) -> float:
    """Compute the uncalibrated structural exposure heuristic in [0.0, 1.0]."""
    return compute_score_components(
        variants=variants,
        contact_by_position=contact_by_position,
        in_pocket_positions=in_pocket_positions,
        use_annotations=use_annotations,
    ).total_score


def assert_reasons_backed_by_ledger(reasons: list[Reason], ledger_rows: list[LedgerRow]) -> None:
    """Assert that every Reason cites at least one LedgerRow ID present in ledger_rows."""
    valid_ids = {row.id for row in ledger_rows}
    for idx, reason in enumerate(reasons):
        if not reason.ledger_row_ids:
            raise AssertionError(f"Reason[{idx}] ('{reason.text}') has an empty ledger_row_ids list")
        missing_ids = [rid for rid in reason.ledger_row_ids if rid not in valid_ids]
        if missing_ids:
            raise AssertionError(
                f"Reason[{idx}] ('{reason.text}') cites ledger_row_ids not in CheckResult.ledger_rows: "
                f"{missing_ids}"
            )


async def run_check(
    asset: Asset,
    emit: Emit | None = None,
    *,
    variants_timeout: float = VARIANTS_DEADLINE_SECONDS,
    structure_timeout: float = STRUCTURE_DEADLINE_SECONDS,
    cache: JsonCache | None = None,
    cache_dir: Path | None = None,
    client: httpx.AsyncClient | None = None,
) -> CheckResult:
    """Run the market/resistance check for an Asset and return a CheckResult."""
    uniprot_id = (asset.ids.get("uniprot_id") or "").strip().upper()
    eff_cache = cache or (JsonCache(cache_dir) if cache_dir is not None else default_cache())

    await _emit_event(
        emit,
        "started",
        f"Starting resistance check for {asset.drug} targeting {asset.target} ({uniprot_id or 'unknown UniProt ID'})",
    )

    if not uniprot_id:
        await _emit_event(emit, "error", "Missing uniprot_id in Asset.ids")
        return CheckResult(
            check_id="market",
            score=0.0,
            confidence="low",
            status="failed",
            reasons=[],
            missing=["Not scored: Asset.ids['uniprot_id'] is missing"],
            ledger_rows=[],
            structure=None,
        )

    missing: list[str] = []
    status_flags: list[Literal["complete", "partial", "failed", "timeout"]] = []

    await _emit_event(
        emit,
        "fetching",
        f"Fetching missense variants for {uniprot_id} from EBI Proteins Variation API",
    )
    try:
        var_result: VariantFetchResult = await asyncio.wait_for(
            fetch_variants(
                uniprot_id,
                asset.as_of,
                cache=eff_cache,
                client=client,
            ),
            timeout=variants_timeout,
        )
        status_flags.append(var_result.status)
        if not var_result.as_of_enforced and asset.as_of is not None:
            missing.append(
                f"as_of ({asset.as_of.isoformat()}) not enforceable on EBI Variation variants: "
                + "; ".join(var_result.missing)
            )
        elif var_result.missing:
            missing.extend(var_result.missing)
    except asyncio.TimeoutError:
        await _emit_event(
            emit,
            "error",
            f"Variant fetch timed out after {variants_timeout:.1f}s for {uniprot_id}",
        )
        var_result = VariantFetchResult(
            variants=[],
            as_of_enforced=asset.as_of is None,
            status="partial",
            missing=[f"Variant fetch timed out after {variants_timeout:.1f}s for {uniprot_id}"],
        )
        status_flags.append("partial")
        missing.extend(var_result.missing)

    variants = var_result.variants
    has_drug_response_variants = any(is_drug_response_variant(v) for v in variants)
    if not has_drug_response_variants:
        missing.append(
            "None found in sources used (EBI Variation/ClinVar); absence is not evidence of no resistance."
        )

    await _emit_event(
        emit,
        "fetching",
        f"Resolving ligand for {asset.drug} and comparing up to 5 candidate structures for {uniprot_id}",
    )
    try:
        struct_result: StructureAnalysisResult = await asyncio.wait_for(
            analyze_structure(
                asset,
                variants,
                timeout=structure_timeout,
                cache=eff_cache,
                client=client,
            ),
            timeout=structure_timeout,
        )
        status_flags.append(struct_result.status)
    except asyncio.TimeoutError:
        await _emit_event(
            emit,
            "error",
            f"Structure analysis timed out after {structure_timeout:.1f}s for {uniprot_id}",
        )
        missing.append(
            f"Not scored: structure analysis timed out after {structure_timeout:.1f}s for {uniprot_id}"
        )
        return CheckResult(
            check_id="market",
            score=0.0,
            confidence="low",
            status="failed",
            reasons=[],
            missing=missing,
            ledger_rows=[],
            structure=None,
        )

    for skipped_msg in struct_result.skipped_structures:
        if skipped_msg not in missing:
            missing.append(skipped_msg)

    for m_item in struct_result.missing:
        if m_item not in missing:
            missing.append(m_item)

    if struct_result.ligand_status == "ambiguous":
        ambig_rows = struct_result.ledger_rows
        ambig_text = (
            f"Not scored: ligand resolution for {asset.drug} is ambiguous across multiple PDB chemical "
            f"components ({', '.join(struct_result.candidate_ligands)}); confirm ligand ID before scoring."
        )
        if ambig_text not in missing:
            missing.append(ambig_text)
        ambig_reason = Reason(
            text=ambig_text,
            ledger_row_ids=[ambig_rows[0].id],
            direction="risk",
        )
        assert_reasons_backed_by_ledger([ambig_reason], ambig_rows)
        await _emit_event(
            emit,
            "done",
            f"Ambiguous ligand resolution for {asset.drug}: {', '.join(struct_result.candidate_ligands)}",
        )
        return CheckResult(
            check_id="market",
            score=0.0,
            confidence="low",
            status="failed",
            reasons=[ambig_reason],
            missing=missing,
            ledger_rows=ambig_rows,
            structure=None,
        )

    if struct_result.structure_payload is not None and struct_result.chem_comp_id:
        await _emit_event(
            emit,
            "computing",
            (
                f"Resolved ligand {struct_result.chem_comp_id}, compared "
                f"{struct_result.candidate_structures_considered} candidate structures "
                f"(selected {struct_result.structure_payload.structure_id})"
            ),
        )
    elif struct_result.structure_payload is not None:
        await _emit_event(
            emit,
            "computing",
            f"Using AlphaFold fallback structure {struct_result.structure_payload.structure_id} for {uniprot_id}",
        )

    ledger_rows = list(struct_result.ledger_rows)
    ledger_ids_set = {row.id for row in ledger_rows}

    in_pocket_positions: set[int] = set()
    if struct_result.structure_payload is not None:
        for res_ev in struct_result.structure_payload.residues:
            if res_ev.in_binding_pocket:
                try:
                    in_pocket_positions.add(int(res_ev.residue[1:]))
                except ValueError:
                    pass

    use_annotations = asset.as_of is None
    if not use_annotations and asset.as_of is not None:
        leak_msg = (
            f"Annotations excluded for as_of ({asset.as_of.isoformat()}) to avoid future leakage "
            f"(scored on structure only)"
        )
        if leak_msg not in missing:
            missing.append(leak_msg)

    is_drug_bound_pdb = (
        struct_result.structure_payload is not None
        and struct_result.structure_payload.source == "pdb"
    )
    no_drug_bound_structure = not is_drug_bound_pdb

    ranked_candidates = await asyncio.to_thread(
        rank_escape_candidates,
        variants,
        struct_result.contact_by_position,
        in_pocket_positions,
        use_annotations=use_annotations,
    )
    score_components = await asyncio.to_thread(
        compute_score_components,
        variants,
        struct_result.contact_by_position,
        in_pocket_positions,
        use_annotations=use_annotations,
        is_drug_bound_pdb=is_drug_bound_pdb,
    )
    score = score_components.total_score if is_drug_bound_pdb else 0.0

    has_annotated_candidates = use_annotations and (
        has_drug_response_variants
        or any(is_clinically_actionable_variant(v) for v in ranked_candidates)
    )
    insufficient_evidence = no_drug_bound_structure and not has_annotated_candidates

    confidence: Literal["high", "medium", "low"]
    if no_drug_bound_structure:
        confidence = "low"
    elif (
        struct_result.status == "complete"
        and var_result.status == "complete"
    ):
        confidence = "high" if asset.as_of is None else "medium"
    elif asset.as_of is not None:
        confidence = "medium"
    else:
        confidence = "low"

    # Unscored cases (unresolved drug, AlphaFold fallback, ambiguous ligand) return status="failed"
    overall_status: Literal["complete", "partial", "failed", "timeout"]
    if no_drug_bound_structure:
        overall_status = "failed"
    elif (
        "partial" in status_flags
        or struct_result.status == "partial"
        or var_result.status == "partial"
    ):
        overall_status = "partial"
    else:
        overall_status = "complete"

    structure_out: StructurePayload | None = struct_result.structure_payload
    if structure_out is not None and f"confidence={confidence}" not in structure_out.confidence_note:
        structure_out = structure_out.model_copy(
            update={"confidence_note": f"confidence={confidence}: {structure_out.confidence_note}"}
        )

    reasons: list[Reason] = []
    struct_row_id = next(
        (r.id for r in ledger_rows if r.id.startswith("market-structure-")),
        None,
    )

    if no_drug_bound_structure:
        if struct_row_id is None:
            fallback_row = LedgerRow(
                id=f"market-structure-none-{uniprot_id}",
                claim=f"No drug-bound PDB structure available for {asset.drug} / {uniprot_id}.",
                source_record=f"UniProt:{uniprot_id}",
                url=f"https://www.ebi.ac.uk/proteins/api/variation/{uniprot_id}?consequencetype=missense",
                date_pulled=utc_now(),
                licence="CC BY 4.0",
                check_id="market",
                evidence_date=None,
            )
            ledger_rows.append(fallback_row)
            struct_row_id = fallback_row.id
        if asset.as_of is not None:
            reasons.append(
                Reason(
                    text=(
                        f"Annotations were excluded for as_of ({asset.as_of.isoformat()}) to avoid "
                        f"future leakage because EBI Variation cannot be date-filtered; scored on structure only."
                    ),
                    ledger_row_ids=[struct_row_id],
                    direction="risk",
                )
            )
        fallback_struct_id = (
            structure_out.structure_id
            if structure_out is not None
            else "none"
        )
        if insufficient_evidence and asset.as_of is None:
            not_scored_msg = (
                "Not scored: insufficient evidence. A score of 0.0 here means we found nothing, not low risk"
            )
        elif struct_result.ligand_status in ("not_found", "failed") or not struct_result.chem_comp_id:
            not_scored_msg = (
                f"Not scored: drug '{asset.drug}' could not be resolved to a small-molecule PDB ligand "
                f"(used {fallback_struct_id} fallback); pocket, covalent, and density terms are not computed "
                f"without a drug-bound PDB structure. A score of 0.0 here means not scored, not low risk."
            )
        else:
            not_scored_msg = (
                f"Not scored: no qualifying drug-bound PDB structure available for '{asset.drug}' "
                f"(ligand {struct_result.chem_comp_id}; used {fallback_struct_id} fallback); pocket, covalent, "
                f"and density terms are not computed on unbound models. A score of 0.0 here means not scored, not low risk."
            )
        if not_scored_msg not in missing:
            missing.append(not_scored_msg)
        reasons.append(
            Reason(
                text=not_scored_msg,
                ledger_row_ids=[struct_row_id],
                direction="risk",
            )
        )
    else:
        covalent_variants = [
            v
            for v in ranked_candidates
            if v.id in ledger_ids_set
            and v.position in struct_result.contact_by_position
            and struct_result.contact_by_position[v.position].covalent
        ]
        if covalent_variants:
            top_cov = covalent_variants[0]
            cov_contact = struct_result.contact_by_position[top_cov.position]
            cited = [top_cov.id] + ([struct_row_id] if struct_row_id else [])
            reasons.append(
                Reason(
                    text=(
                        f"Variant {top_cov.short_name} mutates the covalent attachment residue "
                        f"{top_cov.original_residue}{top_cov.position} ({cov_contact.contact_distance_angstrom:.2f} Å "
                        f"to {struct_result.chem_comp_id} in {structure_out.structure_id if structure_out else 'PDB'})."
                    ),
                    ledger_row_ids=cited,
                    direction="risk",
                )
            )

        if use_annotations:
            pocket_annotated = [
                v
                for v in ranked_candidates
                if v.id in ledger_ids_set
                and v.position in in_pocket_positions
                and is_clinically_actionable_variant(v)
                and v not in covalent_variants
            ]
            if pocket_annotated:
                top_pa = pocket_annotated[:3]
                labels: list[str] = []
                cited_ids: list[str] = []
                for v in top_pa:
                    cited_ids.append(v.id)
                    c_info = struct_result.contact_by_position.get(v.position)
                    if c_info is not None and c_info.contact_distance_angstrom >= 0:
                        labels.append(f"{v.short_name} ({c_info.contact_distance_angstrom:.2f} Å)")
                    else:
                        labels.append(v.short_name)
                if struct_row_id:
                    cited_ids.append(struct_row_id)
                reasons.append(
                    Reason(
                        text=(
                            f"Clinically annotated binding-pocket escape variants identified within "
                            f"{BINDING_POCKET_CUTOFF_ANGSTROM} Å of the ligand: {', '.join(labels)}."
                        ),
                        ledger_row_ids=cited_ids,
                        direction="risk",
                    )
                )
        elif struct_row_id and asset.as_of is not None:
            reasons.append(
                Reason(
                    text=(
                        f"Annotations were excluded for as_of ({asset.as_of.isoformat()}) to avoid "
                        f"future leakage because EBI Variation cannot be date-filtered; scored on structure only."
                    ),
                    ledger_row_ids=[struct_row_id],
                    direction="risk",
                )
            )

        pocket_claimed_ids = [
            v.id for v in ranked_candidates if v.id in ledger_ids_set and v.position in in_pocket_positions
        ]
        if pocket_claimed_ids and struct_row_id:
            struct_id_str = (
                structure_out.structure_id
                if structure_out
                else uniprot_id
            )
            reasons.append(
                Reason(
                    text=(
                        f"Observed {len(pocket_claimed_ids)} missense variants across "
                        f"{len(in_pocket_positions)} binding-pocket residues in {struct_id_str}."
                    ),
                    ledger_row_ids=[struct_row_id] + pocket_claimed_ids[:5],
                    direction="risk",
                )
            )
        elif struct_row_id:
            reasons.append(
                Reason(
                    text=f"Evaluated structural pocket model for {uniprot_id} ({struct_row_id}).",
                    ledger_row_ids=[struct_row_id],
                    direction="support",
                )
            )

        breakdown_citations = (
            ([struct_row_id] if struct_row_id else [])
            + [v.id for v in ranked_candidates if v.id in ledger_ids_set][:3]
        )
        if not breakdown_citations and ledger_rows:
            breakdown_citations = [ledger_rows[0].id]
        if breakdown_citations:
            reasons.append(
                Reason(
                    text=(
                        f"Score components (total={score:.4g}): "
                        f"covalent term={score_components.covalent_term:.4g}, "
                        f"annotated pocket term={score_components.annotated_pocket_term:.4g}, "
                        f"pocket density term={score_components.pocket_density_term:.4g}, "
                        f"non-pocket annotated term={score_components.non_pocket_annotated_term:.4g}."
                    ),
                    ledger_row_ids=list(dict.fromkeys(breakdown_citations)),
                    direction="risk" if score >= 0.35 else "support",
                )
            )

    assert_reasons_backed_by_ledger(reasons, ledger_rows)

    await _emit_event(
        emit,
        "done",
        f"Market resistance check finished (score={score:.2f}, confidence={confidence}, status={overall_status})",
    )

    return CheckResult(
        check_id="market",
        score=score,
        confidence=confidence,
        status=overall_status,
        reasons=reasons,
        missing=missing,
        ledger_rows=ledger_rows,
        structure=structure_out,
    )
