"""Trial check unit and integration tests.

Tests the classifier, pure Python metrics computation, as_of date guard,
and live integration with the EvidenceLedger and core Orchestrator.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import pytest

from checks.trial.classifier import classify_why_stopped
from checks.trial.fetch import _build_ledger_row, _parse_study_record
from checks.trial.metrics import compute_trial_metrics
from checks.trial.models import ClinicalTrialRecord
from checks.trial.runner import run_check
from core.gemini_client import GeminiClient
from core.ledger import EvidenceLedger
from core.orchestrator import run_checks
from core.resolver import resolve_asset
from shared.schemas import Asset, LedgerRow

NO_LLM = GeminiClient(api_key="")


# ---------------------------------------------------------------------------
# Unit tests: Classifier & Metrics
# ---------------------------------------------------------------------------


def test_classifier_categories():
    assert classify_why_stopped("Study terminated due to poor accrual and slow enrollment").category == "ACCRUAL_RECRUITMENT"
    assert classify_why_stopped("Terminated after interim analysis showed lack of efficacy").category == "EFFICACY_FUTILITY"
    assert classify_why_stopped("Halted due to dose-limiting toxicity and adverse events").category == "SAFETY_TOXICITY"
    assert classify_why_stopped("Sponsor business decision to reprioritize pipeline and budget").category == "COMMERCIAL_STRATEGIC"
    assert classify_why_stopped("Regulatory review delayed and covid disruptions").category == "OPERATIONAL_REGULATORY"
    assert classify_why_stopped("").category == "OTHER_UNKNOWN"
    assert classify_why_stopped(None).category == "OTHER_UNKNOWN"


def test_compute_trial_metrics():
    direct = [
        ClinicalTrialRecord(
            nct_id="NCT00000001",
            overall_status="TERMINATED",
            why_stopped="Low patient accrual",
            phases=["PHASE3"],
            url="https://clinicaltrials.gov/study/NCT00000001",
        ),
        ClinicalTrialRecord(
            nct_id="NCT00000002",
            overall_status="COMPLETED",
            phases=["PHASE2"],
            url="https://clinicaltrials.gov/study/NCT00000002",
        ),
    ]
    benchmark = [
        ClinicalTrialRecord(
            nct_id="NCT00000003",
            overall_status="TERMINATED",
            why_stopped="Interim futility",
            phases=["PHASE2"],
            url="https://clinicaltrials.gov/study/NCT00000003",
        ),
        ClinicalTrialRecord(
            nct_id="NCT00000004",
            overall_status="TERMINATED",
            why_stopped="Dose-limiting safety toxicity",
            phases=["PHASE1"],
            url="https://clinicaltrials.gov/study/NCT00000004",
        ),
    ]

    metrics = compute_trial_metrics(direct, benchmark)
    assert 0.05 <= metrics.risk_score <= 0.95
    assert metrics.confidence == "high"  # due to PHASE3
    assert metrics.total_direct_trials == 2
    assert metrics.direct_stopped_count == 1
    assert metrics.direct_completed_count == 1
    assert metrics.benchmark_stopped_count == 2
    assert metrics.accrual_attrition_fraction > 0
    assert metrics.efficacy_attrition_fraction > 0
    assert metrics.safety_attrition_fraction > 0


def test_parse_study_record_as_of_boundary():
    raw_study = {
        "protocolSection": {
            "identificationModule": {"nctId": "NCT01234567", "briefTitle": "Test Study"},
            "statusModule": {
                "overallStatus": "TERMINATED",
                "whyStopped": "Slow enrollment",
                "startDateStruct": {"date": "2018-01-15"},
                "completionDateStruct": {"date": "2020-06-30"},
                "statusVerifiedDate": "2020-07-01",
            },
            "designModule": {
                "phases": ["PHASE2"],
                "studyType": "INTERVENTIONAL",
                "enrollmentInfo": {"count": 100, "type": "ACTUAL"},
            },
            "armsInterventionsModule": {
                "interventions": [{"name": "TestDrug"}],
            },
            "conditionsModule": {
                "conditions": ["Cancer"],
            },
            "sponsorCollaboratorsModule": {
                "leadSponsor": {"name": "Pharma Inc"},
            },
        }
    }

    # 1. as_of before trial start -> dropped completely (no retrospective peeking)
    assert _parse_study_record(raw_study, as_of=date(2017, 1, 1)) is None

    # 2. as_of between start and completion -> active as of cutoff, outcome stripped
    mid_trial = _parse_study_record(raw_study, as_of=date(2019, 1, 1))
    assert mid_trial is not None
    assert mid_trial.overall_status == "ACTIVE_AS_OF"
    assert mid_trial.why_stopped is None

    # 3. as_of after completion -> terminal status and why_stopped preserved
    post_trial = _parse_study_record(raw_study, as_of=date(2021, 1, 1))
    assert post_trial is not None
    assert post_trial.overall_status == "TERMINATED"
    assert post_trial.why_stopped == "Slow enrollment"


# ---------------------------------------------------------------------------
# Integration tests: Live API, Ledger and Orchestrator
# ---------------------------------------------------------------------------


@pytest.fixture
async def egfr_nsclc():
    return await resolve_asset("Osimertinib targeting EGFR in NSCLC", gemini=NO_LLM)


@pytest.mark.live
async def test_live_trial_check_osimertinib(egfr_nsclc):
    events = []

    async def emit_callback(event):
        events.append(event)

    res = await run_check(egfr_nsclc, emit_callback)
    assert res.check_id == "trial"
    assert res.status in ("complete", "partial")
    assert 0.05 <= res.score <= 0.95
    assert res.confidence in ("high", "medium")
    assert len(res.ledger_rows) > 0
    assert len(res.reasons) > 0

    event_statuses = [e.status for e in events]
    assert "started" in event_statuses
    assert "done" in event_statuses

    # Check that ledger rows have proper provenance and licences
    for row in res.ledger_rows:
        assert row.check_id == "trial"
        assert row.licence
        assert row.url.startswith("https://clinicaltrials.gov")
        assert row.date_pulled is not None

    # Verify that EvidenceLedger ingests the result without dangling citations
    ledger = EvidenceLedger()
    added = ledger.add_result(res)
    assert added == len(res.ledger_rows)

    # Every reason must cite rows present in ledger
    for reason in res.reasons:
        assert reason.direction in ("risk", "support")
        assert len(reason.ledger_row_ids) > 0
        for rid in reason.ledger_row_ids:
            assert rid in ledger


@pytest.mark.live
async def test_live_trial_check_as_of_enforcement(egfr_nsclc):
    cutoff = date(2016, 1, 1)
    asset = egfr_nsclc.model_copy(update={"as_of": cutoff})
    res = await run_check(asset, lambda e: None)

    assert res.status in ("complete", "partial")
    assert len(res.ledger_rows) > 0

    # Ensure no evidence postdates the cutoff
    for row in res.ledger_rows:
        if row.evidence_date:
            assert row.evidence_date <= cutoff

    # EvidenceLedger with strict as_of must accept the entire result
    ledger = EvidenceLedger(as_of=cutoff)
    ledger.add_result(res)


@pytest.mark.live
async def test_orchestrator_runs_trial_lane(egfr_nsclc):
    run = await run_checks(egfr_nsclc, timeout_s=60)
    assert run.results["trial"].status in ("complete", "partial")
    assert len(run.ledger.rows("trial")) > 0
