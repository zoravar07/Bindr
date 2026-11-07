"""Trial Check Runner for Pre-Mortem."""

from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from typing import Callable
import logging

from shared.schemas import Asset, CheckResult, Reason, StatusEvent, LedgerRow
from checks.trial.fetch import fetch_trials_for_asset
from checks.trial.metrics import compute_trial_metrics

logger = logging.getLogger("pre_mortem.checks.trial.runner")


async def run_check(
    asset: Asset,
    emit: Callable[[StatusEvent], None] | None = None,
) -> CheckResult:
    def _safe_emit(status: str, msg: str):
        if emit:
            try:
                emit(
                    StatusEvent(
                        check_id="trial",
                        status=status,
                        message=msg,
                        timestamp=datetime.now(timezone.utc),
                    )
                )
            except Exception as e:
                logger.warning(f"Error in status emission: {e}")

    _safe_emit("started", f"Searching ClinicalTrials.gov for '{asset.drug}' in '{asset.disease}'...")

    missing: list[str] = []
    ledger_rows: list[LedgerRow] = []

    try:
        fetch_res = await asyncio.wait_for(
            fetch_trials_for_asset(asset, timeout_seconds=15.0),
            timeout=40.0,
        )

        missing.extend(fetch_res.missing)
        ledger_rows.extend(fetch_res.ledger_rows)

        _safe_emit(
            "fetching",
            f"Found {len(fetch_res.direct_trials)} direct studies and "
            f"{len(fetch_res.disease_benchmark_trials)} indication attrition benchmarks.",
        )

        _safe_emit("classifying", "Analyzing termination rationales and recruitment feasibility...")

        metrics = await asyncio.to_thread(
            compute_trial_metrics,
            fetch_res.direct_trials,
            fetch_res.disease_benchmark_trials,
        )

        _safe_emit(
            "computing",
            f"Computed risk score: {metrics.risk_score:.2f} ({metrics.confidence} confidence). Primary driver: {metrics.primary_failure_mode}.",
        )

        reasons: list[Reason] = []
        ledger_row_ids = {r.id for r in ledger_rows}

        accrual_rows = [
            r.id for r in ledger_rows
            if any(k in r.claim.lower() for k in ("accrual", "recruit", "enroll"))
        ]
        if accrual_rows:
            reasons.append(
                Reason(
                    text=(
                        f"Patient accrual bottleneck: historical clinical data shows {metrics.accrual_attrition_fraction * 100:.1f}% "
                        f"of stopped trials in {asset.disease} suffered recruitment or enrollment failure."
                    ),
                    ledger_row_ids=accrual_rows[:3],
                )
            )

        direct_stopped_rows = [
            r.id for r in ledger_rows
            if "why_stopped" in r.id and asset.drug.lower() in r.claim.lower()
        ]
        if direct_stopped_rows:
            reasons.append(
                Reason(
                    text=f"Direct clinical attrition: previous study for {asset.drug} in {asset.disease} was stopped early.",
                    ledger_row_ids=direct_stopped_rows[:2],
                )
            )
        elif ledger_rows:
            reasons.append(
                Reason(
                    text=(
                        f"Trial landscape baseline: evaluated {metrics.total_direct_trials} direct and "
                        f"{metrics.benchmark_stopped_count} benchmark studies in {asset.disease}."
                    ),
                    ledger_row_ids=[ledger_rows[0].id],
                )
            )

        bench_rows = [r.id for r in ledger_rows if "benchmark" in r.id]
        if bench_rows:
            reasons.append(
                Reason(
                    text=f"Indication-level failure mode: benchmark attrition in {asset.disease} is dominated by {metrics.primary_failure_mode}.",
                    ledger_row_ids=bench_rows[:2],
                )
            )

        for r in reasons:
            r.ledger_row_ids = [rid for rid in r.ledger_row_ids if rid in ledger_row_ids]
        reasons = [r for r in reasons if r.ledger_row_ids]

        _safe_emit("done", f"Trial check complete. Risk score: {metrics.risk_score:.2f}.")

        return CheckResult(
            check_id="trial",
            score=metrics.risk_score,
            confidence=metrics.confidence,
            status=fetch_res.status,
            reasons=reasons,
            missing=list(dict.fromkeys(missing)),
            ledger_rows=ledger_rows,
        )

    except asyncio.TimeoutError:
        _safe_emit("error", "Trial check timed out after 40 seconds.")
        return CheckResult(
            check_id="trial",
            score=0.5,
            confidence="low",
            status="timeout",
            reasons=[],
            missing=["Trial check timed out querying ClinicalTrials.gov."],
            ledger_rows=[],
        )
    except Exception as e:
        _safe_emit("error", f"Trial check error: {e}")
        return CheckResult(
            check_id="trial",
            score=0.5,
            confidence="low",
            status="failed",
            reasons=[],
            missing=[f"Trial check error: {e}"],
            ledger_rows=[],
        )
