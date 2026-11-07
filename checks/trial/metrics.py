"""Pure Python calculation of clinical trial attrition and recruitment feasibility metrics."""

from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field

from checks.trial.models import ClinicalTrialRecord
from checks.trial.classifier import classify_why_stopped


class TrialMetrics(BaseModel):
    total_direct_trials: int
    direct_stopped_count: int
    direct_active_count: int
    direct_completed_count: int
    benchmark_stopped_count: int
    category_counts: dict[str, int] = Field(default_factory=dict)
    accrual_attrition_fraction: float
    safety_attrition_fraction: float
    efficacy_attrition_fraction: float
    risk_score: float
    confidence: Literal["high", "medium", "low"]
    primary_failure_mode: str


def compute_trial_metrics(
    direct_trials: list[ClinicalTrialRecord],
    benchmark_trials: list[ClinicalTrialRecord],
) -> TrialMetrics:
    direct_stopped = [t for t in direct_trials if t.is_stopped]
    direct_active = [
        t for t in direct_trials
        if t.overall_status.upper() in {"RECRUITING", "ENROLLING_BY_INVITATION", "ACTIVE_NOT_RECRUITING"}
    ]
    direct_completed = [
        t for t in direct_trials
        if t.overall_status.upper() == "COMPLETED"
    ]

    all_stopped_records = direct_stopped + benchmark_trials
    category_counts: dict[str, int] = {
        "ACCRUAL_RECRUITMENT": 0,
        "EFFICACY_FUTILITY": 0,
        "SAFETY_TOXICITY": 0,
        "COMMERCIAL_STRATEGIC": 0,
        "OPERATIONAL_REGULATORY": 0,
        "OTHER_UNKNOWN": 0,
    }

    for record in all_stopped_records:
        if record.why_stopped:
            cls_result = classify_why_stopped(record.why_stopped)
            category_counts[cls_result.category] += 1

    total_classified = sum(category_counts.values()) or 1
    accrual_frac = round(category_counts["ACCRUAL_RECRUITMENT"] / total_classified, 3)
    safety_frac = round(category_counts["SAFETY_TOXICITY"] / total_classified, 3)
    efficacy_frac = round(category_counts["EFFICACY_FUTILITY"] / total_classified, 3)

    base_indication_risk = (0.35 * accrual_frac) + (0.30 * efficacy_frac) + (0.25 * safety_frac)

    direct_total = len(direct_trials)
    if direct_total > 0:
        direct_stopped_ratio = len(direct_stopped) / direct_total
        direct_completed_ratio = len(direct_completed) / direct_total
        raw_score = 0.30 + base_indication_risk + (direct_stopped_ratio * 0.40) - (direct_completed_ratio * 0.25)
    else:
        raw_score = 0.40 + base_indication_risk

    final_score = round(max(0.05, min(0.95, raw_score)), 2)

    has_phase3 = any("PHASE3" in t.phases for t in direct_trials)
    has_phase2 = any("PHASE2" in t.phases for t in direct_trials)

    if direct_total >= 3 or has_phase3:
        confidence = "high"
    elif direct_total >= 1 or len(benchmark_trials) >= 10 or has_phase2:
        confidence = "medium"
    else:
        confidence = "low"

    sorted_modes = sorted(
        [
            ("Accrual & Patient Recruitment Failure", accrual_frac),
            ("Primary Efficacy & Futility Attrition", efficacy_frac),
            ("Safety & Dose-Limiting Toxicity", safety_frac),
        ],
        key=lambda x: x[1],
        reverse=True,
    )

    return TrialMetrics(
        total_direct_trials=direct_total,
        direct_stopped_count=len(direct_stopped),
        direct_active_count=len(direct_active),
        direct_completed_count=len(direct_completed),
        benchmark_stopped_count=len(benchmark_trials),
        category_counts=category_counts,
        accrual_attrition_fraction=accrual_frac,
        safety_attrition_fraction=safety_frac,
        efficacy_attrition_fraction=efficacy_frac,
        risk_score=final_score,
        confidence=confidence,
        primary_failure_mode=sorted_modes[0][0],
    )
