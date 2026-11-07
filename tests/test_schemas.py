from datetime import date

import pytest
from pydantic import ValidationError

from shared.schemas import (
    Asset,
    CheckResult,
    LedgerRow,
    Reason,
    RedFlag,
    VerdictReport,
    utc_now,
)


def _row(row_id: str = "trial-1", check_id: str = "trial") -> LedgerRow:
    return LedgerRow(
        id=row_id,
        claim="Phase 2 enrolled 40 patients",
        source_record="NCT00000000",
        url="https://clinicaltrials.gov/study/NCT00000000",
        date_pulled=utc_now(),
        licence="public domain (US gov)",
        check_id=check_id,
    )


def test_reason_requires_ledger_id():
    with pytest.raises(ValidationError):
        Reason(text="unsupported", ledger_row_ids=[])
    with pytest.raises(ValidationError):
        Reason(text="unsupported", ledger_row_ids=["  "])


def test_check_result_rejects_dangling_citation():
    with pytest.raises(ValidationError, match="not in this result"):
        CheckResult(
            check_id="trial",
            score=0.5,
            confidence="medium",
            status="complete",
            reasons=[Reason(text="x", ledger_row_ids=["ghost"])],
            missing=[],
            ledger_rows=[_row()],
        )


def test_check_result_rejects_cross_lane_row():
    with pytest.raises(ValidationError, match="expected 'trial'"):
        CheckResult(
            check_id="trial",
            score=0.5,
            confidence="medium",
            status="complete",
            reasons=[],
            missing=[],
            ledger_rows=[_row("sci-1", "science")],
        )


def test_score_bounds():
    with pytest.raises(ValidationError):
        CheckResult(check_id="trial", score=1.5, confidence="low", status="partial",
                    reasons=[], missing=[], ledger_rows=[])


def test_verdict_defaults_to_draft_and_checks_rank_order():
    asset = Asset(drug="d", target="t", disease="x", as_of=date(2020, 1, 1))
    flag = lambda r: RedFlag(id=f"f{r}", rank=r, check_id="trial", severity="high",
                             title="t", text="x", ledger_row_ids=["trial-1"])
    v = VerdictReport(asset=asset, overall_risk="high", overall_summary="s",
                      check_summaries=[], red_flags=[flag(1), flag(2)])
    assert v.is_draft is True
    with pytest.raises(ValidationError):
        VerdictReport(asset=asset, overall_risk="high", overall_summary="s",
                      check_summaries=[], red_flags=[flag(2), flag(1)])
