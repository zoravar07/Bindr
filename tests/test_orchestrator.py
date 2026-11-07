"""Orchestrator behaviour under timeouts and failures.

The check functions below are test doubles for the *lane interface*, used to
exercise timeout and error handling. They are not product evidence.
"""

import asyncio
import sys
import time
import types
from datetime import date

import pytest

from core.orchestrator import load_default_checks, run_checks
from shared.schemas import Asset, CheckResult, LedgerRow, Reason, StatusEvent, utc_now

ASSET = Asset(drug="test-drug", target="TEST1", disease="test disease", as_of=date(2020, 1, 1))
TIMEOUT = 0.5


def _row(check_id: str, n: int = 1, evidence_date=None) -> LedgerRow:
    return LedgerRow(
        id=f"{check_id}-{n}",
        claim=f"{check_id} claim {n}",
        source_record=f"REC-{check_id}-{n}",
        url=f"https://example.org/{check_id}/{n}",
        date_pulled=utc_now(),
        licence="CC0",
        check_id=check_id,
        evidence_date=evidence_date,
    )


def ok_check(check_id: str, *, rows: int = 2, delay: float = 0.05):
    async def run_check(asset, emit):
        await emit(StatusEvent(check_id=check_id, status="running", message="fetching", timestamp=utc_now()))
        await asyncio.sleep(delay)
        ledger_rows = [_row(check_id, i) for i in range(1, rows + 1)]
        return CheckResult(
            check_id=check_id, score=0.4, confidence="medium", status="complete",
            reasons=[Reason(text="supported", ledger_row_ids=[ledger_rows[0].id])],
            missing=[], ledger_rows=ledger_rows,
        )
    return run_check


def slow_check(seconds: float = 10.0):
    async def run_check(asset, emit):
        await asyncio.sleep(seconds)
        raise AssertionError("should have been cancelled")
    return run_check


async def test_one_timeout_two_succeed_completes_on_time():
    events: list[StatusEvent] = []
    checks = {"science": ok_check("science"), "trial": slow_check(), "market": ok_check("market", rows=3)}

    t0 = time.monotonic()
    run = await run_checks(ASSET, events.append, checks=checks, timeout_s=TIMEOUT)
    elapsed = time.monotonic() - t0

    assert elapsed < TIMEOUT + 0.5, f"orchestrator took {elapsed:.2f}s"
    assert run.results["trial"].status == "timeout"
    assert run.results["trial"].ledger_rows == []
    assert "timed out" in run.results["trial"].missing[0]
    assert run.results["science"].status == "complete"
    assert run.results["market"].status == "complete"

    assert len(run.ledger) == 5
    assert {r.check_id for r in run.ledger} == {"science", "market"}
    assert set(run.usable) == {"science", "market"}

    errors = [e for e in events if e.status == "error"]
    assert [e.check_id for e in errors] == ["trial"]
    assert events[-1].check_id == "orchestrator" and events[-1].status == "complete"


async def test_check_that_swallows_cancellation_still_times_out():
    async def stubborn(asset, emit):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await asyncio.sleep(TIMEOUT * 2)  # misbehaving check ignores the cancel for a while
        raise AssertionError("result should be discarded")

    t0 = time.monotonic()
    run = await run_checks(ASSET, checks={"science": stubborn, "trial": ok_check("trial")}, timeout_s=TIMEOUT)
    assert time.monotonic() - t0 < TIMEOUT + 0.5
    assert run.results["science"].status == "timeout"
    assert run.results["trial"].status == "complete"


async def test_exception_degrades_to_failed():
    async def boom(asset, emit):
        raise RuntimeError("upstream 503")

    run = await run_checks(ASSET, checks={"science": ok_check("science"), "market": boom}, timeout_s=TIMEOUT)
    assert run.results["market"].status == "failed"
    assert "upstream 503" in run.results["market"].missing[0]
    assert len(run.ledger) == 2


async def test_wrong_lane_result_and_as_of_violation_are_rejected():
    async def future_evidence(asset, emit):
        row = _row("market", evidence_date=date(2024, 1, 1))  # after as_of
        return CheckResult(check_id="market", score=0.9, confidence="high", status="complete",
                           reasons=[Reason(text="peeked", ledger_row_ids=[row.id])],
                           missing=[], ledger_rows=[row])

    run = await run_checks(
        ASSET,
        checks={"science": ok_check("trial"), "market": future_evidence, "trial": ok_check("trial")},
        timeout_s=TIMEOUT,
    )
    assert run.results["science"].status == "failed"  # returned trial's result
    assert run.results["market"].status == "failed"
    assert "as_of" in run.results["market"].missing[0]
    assert {r.check_id for r in run.ledger} == {"trial"}


async def test_async_emit_and_broken_emit_do_not_break_lanes():
    seen = []

    async def async_emit(event):
        seen.append(event.status)
        if event.status == "running":
            raise RuntimeError("websocket closed")

    run = await run_checks(ASSET, async_emit, checks={"science": ok_check("science")}, timeout_s=TIMEOUT)
    assert run.results["science"].status == "complete"
    assert "running" in seen


async def test_unbuilt_lane_fails_cleanly(monkeypatch):
    # An empty module stands in for a lane with no run_check yet, keeping this test offline.
    monkeypatch.setitem(sys.modules, "checks.trial", types.ModuleType("checks.trial"))
    checks = load_default_checks(("trial",))
    checks["science"] = ok_check("science")
    run = await run_checks(ASSET, checks=checks, timeout_s=TIMEOUT)
    assert run.results["science"].status == "complete"
    assert run.results["trial"].status == "failed"
    assert "not available" in run.results["trial"].missing[0]


async def test_unknown_lane_id_is_rejected():
    with pytest.raises(ValueError, match="Unknown check ids"):
        await run_checks(ASSET, checks={"vibes": ok_check("science")}, timeout_s=TIMEOUT)
