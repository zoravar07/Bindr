"""Backtest 1 - Dead Science.

Runs the real science check over 20 historical target-disease programmes
(10 approved, 10 failed in Phase 2/3 for lack of efficacy), each with an
`as_of` cutoff set strictly before its outcome, and measures whether the
check separates the two groups.

Every programme is scored twice:

- `strict`      gate_priors=True. Published priors are used only if they
                predate `as_of`. Faithful to product behaviour, but for a
                pre-2015 cutoff neither prior exists, so the base rate falls
                back to 0.5 and genetic evidence gets no weight.
- `methodology` gate_priors=False. The priors are treated as methodology
                constants (identical for every asset, carrying no information
                about any individual programme) while all asset evidence stays
                cutoff-gated. This is the headline configuration.

Baseline to beat: the published all-indication phase base rate, which assigns
every programme the same failure probability. A constant predictor has AUROC
exactly 0.5 and zero separation of means; its expected precision@10 is the
cohort prevalence. Per-therapy-area base rates are deliberately not used:
the figures could not be verified against the source reports, and this
package does not quote unverified numbers.

Leakage control: each programme's result is loaded into an
`EvidenceLedger(as_of=...)`, which raises on any row dated after the cutoff,
and the excluded row IDs are asserted absent.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Sequence

from checks.science import run_check
from checks.science.check import BASE_RATE_PRIORS, base_key_for_stage, pick_prior
from core.ledger import EvidenceLedger, LedgerError
from eval.cohorts import SCIENCE_COHORT, Programme
from eval.stats import GroupComparison, auroc, mean, median, precision_at_k, recall_at_k
from shared.schemas import Asset, CheckResult

# Rows the science check must never emit under an as_of cutoff.
AS_OF_EXCLUDED_ROWS = frozenset({
    "sci-ot-dt-clinical", "sci-ot-dt-literature", "sci-ot-dt-genetic_literature",
    "sci-ot-dt-affected_pathway", "sci-ot-dt-rna_expression",
    "sci-ot-safety", "sci-ot-constraint", "sci-depmap", "sci-ot-stage",
})
MODES = ("methodology", "strict")
HEADLINE_MODE = "methodology"
TOP_K = 10


@dataclass
class ProgrammeScore:
    programme: Programme
    score: float | None = None
    status: str = "not_run"
    confidence: str = "low"
    n_ledger_rows: int = 0
    base_rate: float | None = None
    stage_key: str | None = None
    terms: list[tuple[str, float]] = field(default_factory=list)
    risk_reasons: list[str] = field(default_factory=list)
    support_reasons: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    leakage: list[str] = field(default_factory=list)
    error: str | None = None
    elapsed_s: float = 0.0

    def to_dict(self) -> dict:
        p = self.programme
        return {
            "programme_id": p.programme_id, "target": p.target_symbol, "disease": p.disease_name,
            "drug": p.drug, "outcome": p.outcome, "as_of": p.as_of.isoformat(),
            "outcome_detail": p.outcome_detail, "citation": p.citation, "citation_url": p.citation_url,
            "score": self.score, "status": self.status, "confidence": self.confidence,
            "n_ledger_rows": self.n_ledger_rows, "base_rate": self.base_rate,
            "terms": [[n, round(v, 4)] for n, v in self.terms],
            "risk_reasons": self.risk_reasons, "support_reasons": self.support_reasons,
            "missing": self.missing, "leakage": self.leakage, "error": self.error,
            "elapsed_s": round(self.elapsed_s, 2),
        }


def asset_for(programme: Programme) -> Asset:
    return Asset(
        drug=programme.drug, target=programme.target_symbol, disease=programme.disease_name,
        ids={"chembl_id": programme.chembl_id, "ensembl_id": programme.ensembl_id,
             "target_symbol": programme.target_symbol, "efo_id": programme.disease_id},
        as_of=programme.as_of,
    )


def audit_leakage(programme: Programme, result: CheckResult) -> list[str]:
    """Return every cutoff violation found in a check result. Empty means clean."""
    problems: list[str] = []
    try:
        EvidenceLedger(as_of=programme.as_of).add_result(result)
    except LedgerError as exc:
        problems.append(f"ledger rejected evidence: {exc}")
    for row in result.ledger_rows:
        if row.id in AS_OF_EXCLUDED_ROWS:
            problems.append(f"row {row.id} must be excluded under as_of")
        if row.evidence_date and row.evidence_date > programme.as_of:
            problems.append(f"row {row.id} dated {row.evidence_date} after as_of {programme.as_of}")
    if programme.approval_year and programme.approval_year <= programme.as_of.year:
        problems.append(f"as_of {programme.as_of} does not precede approval year {programme.approval_year}")
    return problems


async def score_programme(programme: Programme, *, gate_priors: bool) -> ProgrammeScore:
    out = ProgrammeScore(programme=programme)
    started = time.monotonic()
    breakdown: dict = {}
    try:
        result = await run_check(asset_for(programme), lambda e: None, breakdown=breakdown,
                                 gate_priors=gate_priors)
    except Exception as exc:
        out.error = f"{type(exc).__name__}: {exc}"
        out.elapsed_s = time.monotonic() - started
        return out
    out.elapsed_s = time.monotonic() - started
    out.status, out.confidence = result.status, result.confidence
    out.n_ledger_rows = len(result.ledger_rows)
    out.missing = list(result.missing)
    out.leakage = audit_leakage(programme, result)
    out.base_rate = breakdown.get("base_rate")
    out.stage_key = breakdown.get("stage_key")
    out.terms = list(breakdown.get("terms") or [])
    out.risk_reasons = [r.text for r in result.reasons if r.direction != "support"]
    out.support_reasons = [r.text for r in result.reasons if r.direction == "support"]
    if result.status in ("complete", "partial"):
        out.score = result.score
    return out


async def score_cohort(cohort: Sequence[Programme], *, gate_priors: bool,
                       concurrency: int = 4) -> list[ProgrammeScore]:
    sem = asyncio.Semaphore(concurrency)

    async def one(p: Programme) -> ProgrammeScore:
        async with sem:
            return await score_programme(p, gate_priors=gate_priors)

    return list(await asyncio.gather(*(one(p) for p in cohort)))


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


def baseline_failure_probability(programme: Programme, *, gate_priors: bool) -> float | None:
    """Published all-indication failure probability for this programme's transition."""
    prior = pick_prior(BASE_RATE_PRIORS, programme.as_of if gate_priors else None)
    if prior is None:
        return None
    return round(1.0 - prior.values[base_key_for_stage(None)], 4)


def baseline_block(scored: Sequence[ProgrammeScore], *, gate_priors: bool) -> dict:
    usable = [s for s in scored if s.score is not None]
    labels = [s.programme.is_failure for s in usable]
    base = [baseline_failure_probability(s.programme, gate_priors=gate_priors) for s in usable]
    known = [(b, l) for b, l in zip(base, labels) if b is not None]
    prevalence = mean(labels) if labels else None
    return {
        "description": "Published all-indication phase base rate of failure, assigned to every programme",
        "source": "Hay et al. 2014 (doi:10.1038/nbt.2786) / BIO-Informa-QLS 2021, Phase II->III transition",
        "values_used": sorted({b for b in base if b is not None}),
        "n_scored": len(known),
        "auroc": auroc([b for b, _ in known], [l for _, l in known]) if known else None,
        "separation_of_means": 0.0 if len({b for b, _ in known}) <= 1 else None,
        "expected_precision_at_k": round(prevalence, 4) if prevalence is not None else None,
        "k": TOP_K,
        "note": "A constant predictor cannot rank, so its AUROC is 0.5 and its expected precision@k "
                "equals cohort prevalence. Per-therapy-area rates were not used: those figures were "
                "not verified against the source reports.",
    }


# ---------------------------------------------------------------------------
# Model metrics
# ---------------------------------------------------------------------------


def model_block(scored: Sequence[ProgrammeScore]) -> dict:
    usable = [s for s in scored if s.score is not None]
    scores = [s.score for s in usable]
    labels = [s.programme.is_failure for s in usable]
    ids = [s.programme.programme_id for s in usable]
    comparison = GroupComparison(
        positive_label="failed", negative_label="approved",
        positive_scores=[s.score for s in usable if s.programme.outcome == "failed"],
        negative_scores=[s.score for s in usable if s.programme.outcome == "approved"],
    )
    return {
        "n_scored": len(usable),
        "n_not_scored": len(scored) - len(usable),
        "comparison": comparison.to_dict(),
        f"precision_at_{TOP_K}": precision_at_k(scores, labels, TOP_K, tiebreak=ids),
        f"recall_at_{TOP_K}": recall_at_k(scores, labels, TOP_K, tiebreak=ids),
        "ranking": [
            {"rank": i, "programme_id": s.programme.programme_id, "outcome": s.programme.outcome,
             "score": s.score}
            for i, s in enumerate(sorted(usable, key=lambda s: (-s.score, s.programme.programme_id)), 1)
        ],
    }


# ---------------------------------------------------------------------------
# Failure case
# ---------------------------------------------------------------------------


def find_failure_case(scored: Sequence[ProgrammeScore]) -> dict | None:
    """The programme the check gets most wrong, with a plain-English explanation."""
    usable = [s for s in scored if s.score is not None]
    if len(usable) < 2:
        return None
    ranked = sorted(usable, key=lambda s: (-s.score, s.programme.programme_id))
    n = len(ranked)
    rank_of = {s.programme.programme_id: i for i, s in enumerate(ranked, 1)}
    failed_median = median([s.score for s in usable if s.programme.outcome == "failed"]) or 0.0
    approved_median = median([s.score for s in usable if s.programme.outcome == "approved"]) or 0.0

    def badness(s: ProgrammeScore) -> tuple[int, float]:
        r = rank_of[s.programme.programme_id]
        if s.programme.outcome == "failed":
            return r, failed_median - s.score  # should rank near the top
        return n + 1 - r, s.score - approved_median  # should rank near the bottom

    worst = max(usable, key=lambda s: badness(s))
    r, margin = badness(worst)
    p = worst.programme
    if p.outcome == "failed":
        drivers = worst.support_reasons or worst.risk_reasons
        headline = (f"{p.drug} ({p.target_symbol} in {p.disease_name}) failed in the clinic, but the science "
                    f"check ranked it {rank_of[p.programme_id]} of {n} by risk with a score of {worst.score:.2f}, "
                    f"{margin:.2f} below the median of the failed group.")
        why = ("The check scored the target-disease biology as sound, and on the evidence available at the "
               "cutoff it was. " + (f"It leaned on: {' '.join(drivers[:2])} " if drivers else ""))
        lesson = ("This is the check's structural blind spot: it assesses whether the target is implicated in "
                  "the disease, not whether modulating it in humans changes the course of the disease. Target "
                  "validity and clinical efficacy are different questions, and only the first is visible in "
                  "genetic and preclinical evidence.")
    else:
        drivers = worst.risk_reasons or worst.support_reasons
        headline = (f"{p.drug} ({p.target_symbol} in {p.disease_name}) was approved, but the science check "
                    f"ranked it {rank_of[p.programme_id]} of {n} by risk with a score of {worst.score:.2f}, "
                    f"{margin:.2f} above the median of the approved group.")
        why = ("The check penalised thin evidence at the cutoff. " +
               (f"It flagged: {' '.join(drivers[:2])} " if drivers else ""))
        lesson = ("The check is biased against mechanisms whose evidence is pharmacological rather than "
                  "genetic. Absence of a genetic association is not evidence that a target is wrong, but the "
                  "scoring treats it as a penalty.")
    return {
        "programme_id": p.programme_id, "drug": p.drug, "target": p.target_symbol,
        "disease": p.disease_name, "actual_outcome": p.outcome, "outcome_detail": p.outcome_detail,
        "citation": p.citation, "citation_url": p.citation_url,
        "as_of": p.as_of.isoformat(), "score": worst.score,
        "rank_by_risk": rank_of[p.programme_id], "cohort_size": n,
        "headline": headline, "why_it_happened": why.strip(), "what_it_means": lesson,
        "missing_at_cutoff": worst.missing[:4],
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run_science_backtest(cohort: Sequence[Programme] = SCIENCE_COHORT,
                               concurrency: int = 4) -> dict:
    modes: dict[str, dict] = {}
    scored_by_mode: dict[str, list[ProgrammeScore]] = {}
    for mode in MODES:
        scored = await score_cohort(cohort, gate_priors=(mode == "strict"), concurrency=concurrency)
        scored_by_mode[mode] = scored
        modes[mode] = {
            "gate_priors": mode == "strict",
            "model": model_block(scored),
            "baseline": baseline_block(scored, gate_priors=(mode == "strict")),
            "programmes": [s.to_dict() for s in scored],
        }
    headline = scored_by_mode[HEADLINE_MODE]
    leakage = {s.programme.programme_id: s.leakage
               for scored in scored_by_mode.values() for s in scored if s.leakage}
    model = modes[HEADLINE_MODE]["model"]
    baseline = modes[HEADLINE_MODE]["baseline"]
    sep = model["comparison"]["separation_of_means"]
    return {
        "backtest": "science",
        "name": "Dead Science",
        "test": f"{len(cohort)} historical target-disease programmes "
                f"({sum(1 for p in cohort if p.outcome == 'failed')} failed in Phase 2/3 for lack of efficacy, "
                f"{sum(1 for p in cohort if p.outcome == 'approved')} approved), scored under each "
                "programme's own as_of cutoff",
        "headline_mode": HEADLINE_MODE,
        "headline_metric": "separation of mean risk score, failed minus approved",
        "headline_value": sep,
        "headline_auroc": model["comparison"]["auroc"],
        "headline_p_value": model["comparison"]["mann_whitney_p"],
        "baseline_metric": "constant published base rate (cannot rank)",
        "baseline_value": baseline["separation_of_means"],
        "baseline_auroc": baseline["auroc"],
        "modes": modes,
        "failure_case": find_failure_case(headline),
        "leakage_violations": leakage,
        "cohort_caveats": [
            f"{len(cohort)} programmes is a small cohort; the Mann-Whitney p-value is reported so the "
            "separation is not read as powered.",
            "Cohort selection is retrospective and favours well-known programmes, which over-represents "
            "clear-cut outcomes.",
            "Open Targets serves only a current snapshot, so association scores may include evidence "
            "published after a programme's cutoff even with the datatype whitelist applied.",
        ],
    }


def main() -> dict:
    import json
    result = asyncio.run(run_science_backtest())
    m = result["modes"][HEADLINE_MODE]["model"]["comparison"]
    print(f"Dead Science: mean failed {m['mean_positive']} vs approved {m['mean_negative']}, "
          f"separation {m['separation_of_means']}, AUROC {m['auroc']}, p={m['mann_whitney_p']}")
    print(f"  baseline AUROC {result['baseline_auroc']}, leakage violations: {len(result['leakage_violations'])}")
    if result["failure_case"]:
        print(f"  failure case: {result['failure_case']['headline']}")
    print(json.dumps({k: v for k, v in result.items() if k != "modes"}, indent=1)[:400])
    return result


if __name__ == "__main__":
    main()
