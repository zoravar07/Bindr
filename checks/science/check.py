"""Science check: is the target-disease biology sound enough for this drug?

Evidence (live, cached via shared/cache.py):
- Open Targets: target-disease association (overall + datatype scores,
  evidence count), target tractability, genetic constraint, safety liabilities,
  the drug's mechanism and its clinical stage for this disease.
- ChEMBL: drug potency against its mechanism target (pChEMBL, filtered to
  documents published on or before as_of), first approval year.

Score (Python only):
    P(mechanism holds) = sigmoid( logit(base) + sum(adjustments) )
    score              = 1 - P            (failure risk, 0..1)

`base` is the published phase-transition success rate for the drug's next
transition (Hay et al. 2014, or BIO/Informa/QLS 2021 when as_of allows).
Human genetic support adds ln(RS) scaled by strength, with RS from
Nelson et al. 2015 (2.0) or Minikel et al. 2024 (2.6). The other weights
below are explicit heuristics, listed in WEIGHTS and echoed in the score
reason so a reviewer can see exactly how the number was made.

DepMap: CRISPR (Chronos) gene effect per cancer cell line and the
common-essential flag, served by Open Targets (target.depMapEssentiality).

as_of: Open Targets is a current snapshot with no usable time series
(associationTimeSeries returned no rows when tested on 2026-11-07). With
as_of set the score uses only AS_OF_DATATYPES (genetic_association,
somatic_mutation, animal_model), tractability, and ChEMBL activity filtered
by document year. Clinical precedence (later outcomes), literature
(text-mined, no pre-cutoff timestamps, leaks post-trial papers), other
datatypes, gnomAD constraint, safety liabilities and DepMap are excluded,
each with a note in `missing`. The indication stage comes only from the
ChEMBL first-approval year.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import re
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Awaitable, Callable

from shared.schemas import Asset, CheckResult, LedgerRow, Reason, StatusEvent, utc_now
from shared.sources import (
    CHEMBL_API,
    CHEMBL_LICENCE,
    OPENTARGETS_LICENCE,
    chembl_get,
    chembl_url,
    ot_query,
    ot_url,
    stage_rank,
)

CHECK_ID = "science"
Emit = Callable[[StatusEvent], "Awaitable[None] | None"]

DATATYPES = (
    "genetic_association",
    "somatic_mutation",
    "genetic_literature",
    "clinical",  # Open Targets renamed known_drug -> clinical
    "animal_model",
    "affected_pathway",
    "literature",
    "rna_expression",
)
DATATYPE_LABELS = {"clinical": "clinical precedence (known_drug)"}
HUMAN_GENETIC = ("genetic_association", "somatic_mutation", "genetic_literature")
# Datatypes trusted in as_of back-tests (see module docstring).
AS_OF_DATATYPES = ("genetic_association", "somatic_mutation", "animal_model")
AS_OF_EXCLUSION_WHY = {
    "clinical": "it encodes clinical outcomes that may postdate the cutoff",
    "literature": "text-mined evidence has no pre-cutoff timestamps and can include post-trial papers",
}

# Heuristic log-odds weights. Changing these changes every score; keep them visible.
WEIGHTS = {
    "clinical": 0.8,
    "animal_model": 0.3,
    "affected_pathway": 0.3,
    "literature": 0.2,
    "rna_expression": 0.1,
    "no_association": -0.7,
    "no_tractability_for_modality": -0.5,
    "lof_intolerant_inhibited": -0.3,
    "many_safety_liabilities": -0.2,
    "potent_binder": 0.2,
    "weak_binder": -0.3,
    "depmap_dependency": 0.3,   # scaled by dependent-line fraction / DEPMAP_FULL_FRACTION
    "common_essential": -0.3,   # pan-essential gene: narrow therapeutic window
}
GENETIC_FULL_STRENGTH = 0.5   # human-genetics datatype score treated as "supported"
LOEUF_INTOLERANT = 0.35       # gnomAD LOEUF below this = loss-of-function intolerant
MANY_LIABILITIES = 10
POTENT_PCHEMBL, WEAK_PCHEMBL = 8.0, 6.0
DEPMAP_DEPENDENT = -0.5       # Chronos gene effect at or below this = line depends on the gene
DEPMAP_FULL_FRACTION = 0.5
DEPMAP_MIN_LINES = 5
DEPMAP_LICENCE = "CC BY 4.0 (DepMap, served via Open Targets)"


# ---------------------------------------------------------------------------
# Published priors (constants with citations, each becomes a LedgerRow)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Prior:
    key: str
    published: date
    source: str
    url: str
    claim: str
    values: dict[str, float]


BASE_RATE_PRIORS = (
    Prior("hay2014", date(2014, 1, 9), "Hay et al. 2014, Nat Biotechnol 32:40-51 (2003-2011 data)",
          "https://doi.org/10.1038/nbt.2786",
          "Phase transition success rates 2003-2011: Phase I->II 64.5%, II->III 32.4%, "
          "III->NDA/BLA 60.1%, NDA/BLA->approval 83.2%; Phase I->approval LOA 10.4%",
          {"p1": 0.645, "p2": 0.324, "p3": 0.601, "nda": 0.832, "loa": 0.104}),
    Prior("bio2021", date(2021, 2, 1), "BIO/Informa/QLS 2021, Clinical Development Success Rates 2011-2020",
          "https://go.bio.org/rs/490-EHZ-999/images/ClinicalDevelopmentSuccessRates2011_2020.pdf",
          "Phase transition success rates 2011-2020: Phase I->II 52.0%, II->III 28.9%, "
          "III->NDA/BLA 57.8%, NDA/BLA->approval 90.6%; Phase I->approval LOA 7.9%",
          {"p1": 0.520, "p2": 0.289, "p3": 0.578, "nda": 0.906, "loa": 0.079}),
)
GENETIC_PRIORS = (
    Prior("nelson2015", date(2015, 6, 29), "Nelson et al. 2015, Nat Genet 47:856-860",
          "https://doi.org/10.1038/ng.3314",
          "Selecting genetically supported targets could double the success rate in clinical "
          "development (RS 2.0)",
          {"rs": 2.0}),
    Prior("minikel2024", date(2024, 4, 17), "Minikel et al. 2024, Nature 629:624-629",
          "https://doi.org/10.1038/s41586-024-07316-0",
          "Probability of success for drug mechanisms with genetic support is 2.6 times greater "
          "than those without (RS 2.6, Phase I to launch)",
          {"rs": 2.6}),
)
PRIOR_LICENCE = "citation only (published article/report; figures quoted)"
# Figures checked against the sources on 2026-11-07: Hay 2014 Table 1, BIO 2021 report
# (all-indication rates), Nelson 2015 and Minikel 2024 abstracts.


def pick_prior(priors: tuple[Prior, ...], as_of: date | None) -> Prior | None:
    eligible = [p for p in priors if as_of is None or p.published <= as_of]
    return max(eligible, key=lambda p: p.published) if eligible else None


def base_key_for_stage(stage: str | None) -> str:
    rank = stage_rank(stage)
    if rank == 0:
        return "p2"  # unknown: use the proof-of-concept transition, where biology usually fails
    if rank <= stage_rank("PHASE_1"):
        return "p1"
    if rank <= stage_rank("PHASE_2"):
        return "p2"
    if rank <= stage_rank("PHASE_3"):
        return "p3"
    return "nda"


# ---------------------------------------------------------------------------
# Scoring (pure)
# ---------------------------------------------------------------------------

@dataclass
class Features:
    base_rate: float = 0.5
    association: float | None = None
    datatypes: dict[str, float] = field(default_factory=dict)
    genetic_rs: float | None = None
    datatypes_allowed: tuple[str, ...] | None = None  # None = all DATATYPES
    use_target_safety: bool = True                    # gnomAD constraint + safety liabilities
    modality_tractable: bool | None = None
    lof_intolerant_inhibited: bool = False
    safety_liabilities: int = 0
    median_pchembl: float | None = None
    depmap_dependent_fraction: float | None = None
    common_essential: bool = False


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def compute_score(f: Features) -> tuple[float, list[tuple[str, float]]]:
    """Return (failure risk, [(term, log-odds contribution), ...])."""
    terms: list[tuple[str, float]] = [("base rate", _logit(f.base_rate))]
    if f.association is None:
        terms.append(("no target-disease association", WEIGHTS["no_association"]))
    allowed = set(f.datatypes_allowed if f.datatypes_allowed is not None else DATATYPES)
    g = max((f.datatypes.get(k, 0.0) for k in HUMAN_GENETIC if k in allowed), default=0.0)
    if f.genetic_rs and g > 0:
        terms.append(("human genetics", math.log(f.genetic_rs) * min(1.0, g / GENETIC_FULL_STRENGTH)))
    for k in ("clinical", "animal_model", "affected_pathway", "literature", "rna_expression"):
        v = f.datatypes.get(k, 0.0) if k in allowed else 0.0
        if v > 0:
            terms.append((k, WEIGHTS[k] * v))
    if f.modality_tractable is False:
        terms.append(("no tractability for drug modality", WEIGHTS["no_tractability_for_modality"]))
    if f.use_target_safety and f.lof_intolerant_inhibited:
        terms.append(("LoF-intolerant target inhibited", WEIGHTS["lof_intolerant_inhibited"]))
    if f.use_target_safety and f.safety_liabilities >= MANY_LIABILITIES:
        terms.append(("many safety liabilities", WEIGHTS["many_safety_liabilities"]))
    if f.depmap_dependent_fraction:
        terms.append(("DepMap dependency", WEIGHTS["depmap_dependency"]
                      * min(1.0, f.depmap_dependent_fraction / DEPMAP_FULL_FRACTION)))
    if f.common_essential:
        terms.append(("common essential gene", WEIGHTS["common_essential"]))
    if f.median_pchembl is not None:
        if f.median_pchembl >= POTENT_PCHEMBL:
            terms.append(("potent binder", WEIGHTS["potent_binder"]))
        elif f.median_pchembl < WEAK_PCHEMBL:
            terms.append(("weak binder", WEIGHTS["weak_binder"]))
    p = 1.0 / (1.0 + math.exp(-sum(v for _, v in terms)))
    risk = min(max(1.0 - p, 0.01), 0.99)
    return round(risk, 4), terms


# ---------------------------------------------------------------------------
# Live fetches
# ---------------------------------------------------------------------------

_OT_SCIENCE_Q = """
query($e: String!, $d: String!) {
  target(ensemblId: $e) {
    id approvedSymbol
    tractability { label modality value }
    geneticConstraint { constraintType oe oeUpper score }
    safetyLiabilities { event datasource literature url }
    isEssential
    depMapEssentiality { tissueName screens { depmapId cellLineName diseaseFromSource geneEffect } }
  }
  disease(efoId: $d) {
    id name
    associatedTargets(Bs: [$e]) { rows { score target { id } datatypeScores { id score } } }
    evidences(ensemblIds: [$e], size: 1) { count }
  }
}"""

_OT_DRUG_Q = """
query($id: String!) {
  drug(chemblId: $id) {
    id name drugType maximumClinicalStage
    mechanismsOfAction { rows { actionType targets { id } } }
    indications { rows { maxClinicalStage disease { id } } }
  }
}"""

_MODALITY = {"small molecule": "SM", "antibody": "AB", "protein": "AB", "antibody drug conjugate": "AB"}
_INHIBITORY = {"INHIBITOR", "ANTAGONIST", "BLOCKER", "NEGATIVE ALLOSTERIC MODULATOR", "INVERSE AGONIST",
               "DEGRADER", "NEGATIVE MODULATOR", "ANTISENSE INHIBITOR", "RNAI INHIBITOR", "DISRUPTING AGENT"}


async def _maybe_await(x: Any) -> None:
    if inspect.isawaitable(x):
        await x


async def _chembl_bundle(chembl_id: str) -> dict:
    mol, mech = await asyncio.gather(
        chembl_get(f"molecule/{chembl_id}.json", {"only": "molecule_chembl_id,max_phase,first_approval,molecule_type"}),
        chembl_get("mechanism.json", {"molecule_chembl_id": chembl_id, "limit": 20}),
    )
    target_ids = [m["target_chembl_id"] for m in mech.data.get("mechanisms") or [] if m.get("target_chembl_id")]
    out: dict[str, Any] = {"molecule": mol, "mechanism": mech, "target_chembl_id": None, "activity": None}
    if target_ids:
        tid = target_ids[0]
        params = {"molecule_chembl_id": chembl_id, "target_chembl_id": tid, "pchembl_value__isnull": "false",
                  "limit": 1000, "only": "activity_id,pchembl_value,standard_type,document_year"}
        out["target_chembl_id"] = tid
        out["activity"] = await chembl_get("activity.json", params)
        out["activity_params"] = params
    return out


_SYNONYMS = {"carcinoma": "cancer", "neoplasm": "cancer", "tumour": "tumor", "malignant": ""}


def _disease_tokens(name: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", name.lower().replace("-", " "))
    return {_SYNONYMS.get(w, w) for w in words if _SYNONYMS.get(w, w) and w not in {"of", "the", "and"}}


def depmap_lines_for_disease(depmap: list[dict], disease: str) -> list[dict]:
    """Cell lines whose source disease contains every word of `disease` (after synonyms).

    "non-small cell lung carcinoma" matches "Non-Small Cell Lung Cancer" but not
    "Small Cell Lung Cancer"; "lung cancer" matches both.
    """
    want = _disease_tokens(disease)
    if not want:
        return []
    out = []
    for tissue in depmap or []:
        for scr in tissue.get("screens") or []:
            if scr.get("geneEffect") is not None and want <= _disease_tokens(scr.get("diseaseFromSource") or ""):
                out.append(scr)
    return out


# ---------------------------------------------------------------------------
# Check
# ---------------------------------------------------------------------------

async def run_check(asset: Asset, emit: Emit) -> CheckResult:
    async def say(status: str, message: str) -> None:
        await _maybe_await(emit(StatusEvent(check_id=CHECK_ID, status=status, message=message, timestamp=utc_now())))

    await say("started", f"Science check for {asset.target} in {asset.disease}")
    ensembl_id, efo_id, chembl_id = (asset.ids.get(k) for k in ("ensembl_id", "efo_id", "chembl_id"))
    if not ensembl_id or not efo_id:
        await say("done", "Missing target or disease ID")
        return CheckResult(check_id=CHECK_ID, score=0.0, confidence="low", status="failed", reasons=[],
                           missing=["Asset has no ensembl_id/efo_id; run the resolver first"], ledger_rows=[])

    as_of = asset.as_of
    rows: list[LedgerRow] = []
    reasons: list[Reason] = []
    missing: list[str] = []
    fetch_errors: list[str] = []

    def add_row(rid: str, claim: str, record: str, url: str, licence: str, pulled, evidence_date=None) -> str:
        rows.append(LedgerRow(id=rid, claim=claim, source_record=record, url=url, date_pulled=pulled,
                              licence=licence, check_id=CHECK_ID, evidence_date=evidence_date))
        return rid

    await say("fetching", "Querying Open Targets (incl. DepMap) and ChEMBL")
    tasks = {"ot": ot_query(_OT_SCIENCE_Q, {"e": ensembl_id, "d": efo_id})}
    if chembl_id:
        tasks["drug"] = ot_query(_OT_DRUG_Q, {"id": chembl_id})
        tasks["chembl"] = _chembl_bundle(chembl_id)
    else:
        missing.append("No ChEMBL ID: drug mechanism, stage and potency not assessed")
    results = dict(zip(tasks, await asyncio.gather(*tasks.values(), return_exceptions=True)))
    for name, res in results.items():
        if isinstance(res, BaseException):
            fetch_errors.append(f"{name}: {type(res).__name__}: {res}")
    missing.extend(f"Fetch failed ({e})" for e in fetch_errors)

    await say("computing", "Scoring target biology against phase base rates")
    f = Features(datatypes_allowed=AS_OF_DATATYPES if as_of else None, use_target_safety=as_of is None)
    allowed = set(f.datatypes_allowed or DATATYPES)
    sym, dis = asset.target, asset.disease
    pair = f"{sym}-{dis}"
    if as_of is not None:
        missing.append("Open Targets is a current snapshot and cannot be rolled back to as_of; the as_of score "
                       f"uses only {', '.join(AS_OF_DATATYPES)}, tractability and year-filtered ChEMBL activity, "
                       "and the overall association score may still include later evidence")

    # ---- Open Targets association, tractability, constraint, safety ----
    ot = results.get("ot")
    target_rec: dict = {}
    if not isinstance(ot, BaseException):
        target_rec = ot.data.get("target") or {}
        disease_rec = ot.data.get("disease") or {}
        assoc_rows = ((disease_rec.get("associatedTargets") or {}).get("rows")) or []
        assoc = next((r for r in assoc_rows if (r.get("target") or {}).get("id") == ensembl_id), None)
        ev_count = ((disease_rec.get("evidences") or {}).get("count"))
        url = ot_url("evidence", ensembl_id, efo_id)
        record = f"Open Targets association {ensembl_id}/{efo_id}"
        if assoc:
            f.association = float(assoc["score"])
            rid = add_row("sci-ot-assoc", f"Open Targets overall association score {f.association:.2f} for {pair}"
                          + (f" from {ev_count} evidence items" if ev_count is not None else ""),
                          record, url, OPENTARGETS_LICENCE, ot.fetched_at)
            dts = {d["id"]: float(d["score"]) for d in assoc.get("datatypeScores") or []}
            for k in DATATYPES:
                if k not in allowed:
                    why = AS_OF_EXCLUSION_WHY.get(k, "snapshot evidence has no pre-cutoff timestamps")
                    missing.append(f"Open Targets {DATATYPE_LABELS.get(k, k)} excluded under as_of: {why}")
                elif k in dts and dts[k] > 0:
                    f.datatypes[k] = dts[k]
                    add_row(f"sci-ot-dt-{k}", f"Open Targets {DATATYPE_LABELS.get(k, k)} score {dts[k]:.2f} for {pair}",
                            f"{record} datatype {k}", url, OPENTARGETS_LICENCE, ot.fetched_at)
                else:
                    missing.append(f"No Open Targets {DATATYPE_LABELS.get(k, k)} evidence for {pair}")
            band = "strong" if f.association >= 0.5 else "moderate" if f.association >= 0.2 else "weak"
            reasons.append(Reason(text=f"{band.capitalize()} overall target-disease association "
                                       f"({f.association:.2f}) in Open Targets.", ledger_row_ids=[rid],
                                  direction="support" if f.association >= 0.5 else "risk"))
        else:
            rid = add_row("sci-ot-assoc", f"Open Targets reports no association between {sym} and {dis}",
                          record, url, OPENTARGETS_LICENCE, ot.fetched_at)
            reasons.append(Reason(text=f"Open Targets has no evidence linking {sym} to {dis}.",
                                  ledger_row_ids=[rid], direction="risk"))

        g_key = max((k for k in HUMAN_GENETIC if k in allowed), key=lambda k: f.datatypes.get(k, 0.0))
        g = f.datatypes.get(g_key, 0.0)
        if g >= GENETIC_FULL_STRENGTH:
            reasons.append(Reason(text=f"Human genetic support for {pair} ({DATATYPE_LABELS.get(g_key, g_key)} {g:.2f}).",
                                  ledger_row_ids=[f"sci-ot-dt-{g_key}"], direction="support"))
        elif assoc:
            ids = [f"sci-ot-dt-{g_key}"] if g > 0 else ["sci-ot-assoc"]
            reasons.append(Reason(text=f"Weak or absent human genetic support for {pair} "
                                       f"(best human-genetics datatype score {g:.2f}).",
                                  ledger_row_ids=ids, direction="risk"))

        # tractability
        tract = [t for t in target_rec.get("tractability") or [] if t.get("value")]
        by_mod: dict[str, list[str]] = {}
        for t in tract:
            by_mod.setdefault(t["modality"], []).append(t["label"])
        names = {"SM": "small molecule", "AB": "antibody", "PR": "PROTAC", "OC": "other modalities"}
        summary = "; ".join(f"{names.get(m, m)}: {', '.join(v)}" for m, v in sorted(by_mod.items())) or "none"
        t_rid = add_row("sci-ot-tract", f"Open Targets tractability for {sym}: {summary}",
                        f"Open Targets target {ensembl_id} tractability", ot_url("target", ensembl_id),
                        OPENTARGETS_LICENCE, ot.fetched_at)

        # genetic constraint (undated snapshot: skipped under as_of)
        lof = None if as_of else next((c for c in target_rec.get("geneticConstraint") or [] if c.get("constraintType") == "lof"), None)
        loeuf = lof.get("oeUpper") if lof else None
        if loeuf is not None:
            c_rid = add_row("sci-ot-constraint", f"gnomAD loss-of-function constraint for {sym}: LOEUF {loeuf:.2f}",
                            f"Open Targets target {ensembl_id} geneticConstraint", ot_url("target", ensembl_id),
                            OPENTARGETS_LICENCE, ot.fetched_at)
        else:
            c_rid = None
            missing.append(f"gnomAD constraint excluded under as_of (undated snapshot)" if as_of
                           else f"No genetic constraint data for {sym}")

        # safety
        liabilities = [] if as_of else target_rec.get("safetyLiabilities") or []
        f.safety_liabilities = len(liabilities)
        if as_of:
            missing.append("Target safety liabilities excluded under as_of (undated literature curation)")
        elif liabilities:
            events = sorted({l["event"] for l in liabilities if l.get("event")})
            s_rid = add_row("sci-ot-safety", f"{len(liabilities)} target safety liability records for {sym}"
                            + (f", including {', '.join(events[:5])}" if events else ""),
                            f"Open Targets target {ensembl_id} safetyLiabilities", ot_url("target", ensembl_id),
                            OPENTARGETS_LICENCE, ot.fetched_at)
            if f.safety_liabilities >= MANY_LIABILITIES:
                reasons.append(Reason(text=f"{sym} carries {len(liabilities)} recorded target safety liabilities.",
                                      ledger_row_ids=[s_rid], direction="risk"))
        else:
            missing.append(f"No target safety liability records for {sym} (absence is not evidence of safety)")

        # DepMap CRISPR gene effect (current release only: skipped under as_of)
        depmap = target_rec.get("depMapEssentiality")
        if as_of:
            missing.append("DepMap CRISPR essentiality excluded under as_of (current release only)")
        elif not depmap:
            missing.append(f"No DepMap CRISPR screens for {sym} in Open Targets")
        else:
            all_effects = [s["geneEffect"] for t in depmap for s in t.get("screens") or []
                           if s.get("geneEffect") is not None]
            lines = depmap_lines_for_disease(depmap, dis)
            f.common_essential = bool(target_rec.get("isEssential"))
            dep_url = f"https://depmap.org/portal/gene/{sym}"
            ess = "yes" if f.common_essential else "no"
            if len(lines) >= DEPMAP_MIN_LINES:
                effects = [s["geneEffect"] for s in lines]
                n_dep = sum(1 for e in effects if e <= DEPMAP_DEPENDENT)
                f.depmap_dependent_fraction = n_dep / len(effects)
                d_rid = add_row(
                    "sci-depmap", f"DepMap CRISPR (Chronos) for {sym}: {n_dep} of {len(effects)} {dis} cell lines "
                    f"dependent (gene effect <= {DEPMAP_DEPENDENT}), median gene effect "
                    f"{statistics.median(effects):.2f}; common essential: {ess}",
                    f"DepMap via Open Targets target {ensembl_id} depMapEssentiality", dep_url,
                    DEPMAP_LICENCE, ot.fetched_at)
                if f.depmap_dependent_fraction >= 0.2:
                    reasons.append(Reason(text=f"{n_dep} of {len(effects)} {dis} cell lines depend on {sym} in "
                                               f"DepMap CRISPR screens.", ledger_row_ids=[d_rid],
                                          direction="support"))
            else:
                d_rid = add_row(
                    "sci-depmap", f"DepMap CRISPR (Chronos) for {sym}: {len(all_effects)} cell lines screened, "
                    f"{sum(1 for e in all_effects if e <= DEPMAP_DEPENDENT)} dependent; common essential: {ess}",
                    f"DepMap via Open Targets target {ensembl_id} depMapEssentiality", dep_url,
                    DEPMAP_LICENCE, ot.fetched_at)
                missing.append(f"Fewer than {DEPMAP_MIN_LINES} DepMap cell lines match {dis}; "
                               "disease-specific dependency not assessed")
            if f.common_essential:
                reasons.append(Reason(text=f"{sym} is a DepMap common-essential gene, so systemic inhibition "
                                           "risks a narrow therapeutic window.", ledger_row_ids=[d_rid],
                                      direction="risk"))
    else:
        t_rid = c_rid = None
        loeuf = None
        by_mod = {}

    # ---- Drug: modality, action, stage for this disease ----
    drug_rec = results.get("drug")
    stage: str | None = None
    action_types: set[str] = set()
    drug = None if drug_rec is None or isinstance(drug_rec, BaseException) else drug_rec.data.get("drug")
    if drug:
        modality = _MODALITY.get((drug.get("drugType") or "").lower())
        for row in (drug.get("mechanismsOfAction") or {}).get("rows") or []:
            if any(t.get("id") == ensembl_id for t in row.get("targets") or []):
                action_types.add((row.get("actionType") or "").upper())
        if modality and t_rid:
            f.modality_tractable = bool(by_mod.get(modality))
            if not f.modality_tractable:
                reasons.append(Reason(text=f"No {drug.get('drugType')} tractability evidence for {sym}.",
                                      ledger_row_ids=[t_rid], direction="risk"))
        if loeuf is not None and loeuf < LOEUF_INTOLERANT and action_types & _INHIBITORY and c_rid:
            f.lof_intolerant_inhibited = True
            reasons.append(Reason(text=f"{sym} is loss-of-function intolerant (LOEUF {loeuf:.2f}) and the drug "
                                       f"is an inhibitor, raising on-target toxicity risk.",
                                  ledger_row_ids=[c_rid], direction="risk"))
        ind = next((r for r in (drug.get("indications") or {}).get("rows") or []
                    if (r.get("disease") or {}).get("id") == efo_id), None)
        if as_of is None and ind:
            stage = ind.get("maxClinicalStage")
            add_row("sci-ot-stage", f"Open Targets max clinical stage of {drug.get('name')} for {dis}: {stage}",
                    f"Open Targets drug {chembl_id} indications", ot_url("drug", chembl_id),
                    OPENTARGETS_LICENCE, drug_rec.fetched_at)

    # ---- ChEMBL: approval year, potency ----
    chembl = results.get("chembl")
    if chembl is not None and not isinstance(chembl, BaseException):
        mol = chembl["molecule"].data
        first_approval = mol.get("first_approval")
        # An approval after as_of is future evidence: not recorded, not used.
        if first_approval and (as_of is None or int(first_approval) <= as_of.year):
            add_row("sci-chembl-approval", f"ChEMBL first approval year for {chembl_id}: {first_approval}",
                    f"ChEMBL molecule {chembl_id}", chembl_url(chembl_id), CHEMBL_LICENCE,
                    chembl["molecule"].fetched_at, evidence_date=date(int(first_approval), 1, 1))
            if as_of is not None and drug:
                ind_stage = next((r.get("maxClinicalStage") for r in (drug.get("indications") or {}).get("rows") or []
                                  if (r.get("disease") or {}).get("id") == efo_id), None)
                if stage_rank(ind_stage) == stage_rank("APPROVAL"):
                    stage = "APPROVAL"
        if as_of is not None and stage is None:
            missing.append("Indication stage at as_of not reconstructible; Phase II->III base rate used")
        act = chembl.get("activity")
        if act is not None:
            acts = act.data.get("activities") or []
            vals = [float(a["pchembl_value"]) for a in acts if a.get("pchembl_value") and
                    (as_of is None or (a.get("document_year") and int(a["document_year"]) <= as_of.year))]
            years = [int(a["document_year"]) for a in acts if a.get("document_year")
                     and (as_of is None or int(a["document_year"]) <= as_of.year)]
            if vals:
                f.median_pchembl = statistics.median(vals)
                last_year = max(years) if years else None
                tid = chembl["target_chembl_id"]
                p = chembl["activity_params"]
                url = (f"{CHEMBL_API}/activity.json?molecule_chembl_id={p['molecule_chembl_id']}"
                       f"&target_chembl_id={p['target_chembl_id']}&pchembl_value__isnull=false")
                a_rid = add_row("sci-chembl-potency",
                                f"ChEMBL: median pChEMBL {f.median_pchembl:.2f} across {len(vals)} measurements of "
                                f"{chembl_id} against {tid}", f"ChEMBL activity {chembl_id} x {tid}", url,
                                CHEMBL_LICENCE, act.fetched_at,
                                evidence_date=date(last_year, 1, 1) if last_year else None)
                if f.median_pchembl >= POTENT_PCHEMBL:
                    reasons.append(Reason(text=f"Potent binder of its target (median pChEMBL {f.median_pchembl:.2f}).",
                                          ledger_row_ids=[a_rid], direction="support"))
                elif f.median_pchembl < WEAK_PCHEMBL:
                    reasons.append(Reason(text=f"Weak potency against its target (median pChEMBL {f.median_pchembl:.2f}).",
                                          ledger_row_ids=[a_rid], direction="risk"))
            else:
                missing.append(f"No ChEMBL pChEMBL measurements for {chembl_id} against its target"
                               + (" before as_of" if as_of else ""))
        else:
            missing.append(f"ChEMBL has no mechanism target for {chembl_id}; potency not assessed")

    # ---- Priors ----
    base_prior = pick_prior(BASE_RATE_PRIORS, as_of)
    prior_ids: list[str] = []
    stage_key = base_key_for_stage(stage)
    if base_prior:
        f.base_rate = base_prior.values[stage_key]
        prior_ids.append(add_row(f"sci-prior-{base_prior.key}", base_prior.claim, base_prior.source,
                                 base_prior.url, PRIOR_LICENCE, utc_now(), evidence_date=base_prior.published))
    else:
        missing.append("No phase base-rate study published before as_of; neutral 0.5 prior used")
    gen_prior = pick_prior(GENETIC_PRIORS, as_of)
    if gen_prior:
        f.genetic_rs = gen_prior.values["rs"]
        if any(f.datatypes.get(k, 0) > 0 for k in HUMAN_GENETIC):
            prior_ids.append(add_row(f"sci-prior-{gen_prior.key}", gen_prior.claim, gen_prior.source,
                                     gen_prior.url, PRIOR_LICENCE, utc_now(), evidence_date=gen_prior.published))
    else:
        missing.append("No genetic-support study published before as_of; genetics not up-weighted")

    score, terms = compute_score(f)
    labels = {"p1": "Phase I->II", "p2": "Phase II->III", "p3": "Phase III->filing", "nda": "filing->approval"}
    breakdown = ", ".join(f"{name} {val:+.2f}" for name, val in terms)
    score_ids = prior_ids + (["sci-ot-assoc"] if any(r.id == "sci-ot-assoc" for r in rows) else [])
    if score_ids:
        reasons.insert(0, Reason(
            text=f"Science failure risk {score:.2f}: {labels[stage_key]} base rate {f.base_rate:.3f} "
                 f"(stage {stage or 'unknown'}) adjusted in log-odds by {breakdown}.",
            ledger_row_ids=score_ids, direction="risk" if score >= 0.35 else "support"))

    have_assoc = f.association is not None
    if fetch_errors:
        status = "partial" if rows else "failed"
    else:
        status = "complete"
    if have_assoc and len(f.datatypes) >= 4 and not fetch_errors:
        confidence = "medium" if as_of else "high"
    elif have_assoc or rows:
        confidence = "medium" if not fetch_errors else "low"
    else:
        confidence = "low"
    if status == "failed":
        score = 0.0

    await say("done", f"Science risk {score:.2f} ({len(rows)} ledger rows)")
    return CheckResult(check_id=CHECK_ID, score=score, confidence=confidence, status=status,
                       reasons=reasons, missing=list(dict.fromkeys(missing)), ledger_rows=rows)
