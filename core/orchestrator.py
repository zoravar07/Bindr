"""Parallel check runner.

Runs the science, trial and market checks concurrently, each under a hard
timeout. A lane that times out, raises, or returns invalid evidence degrades
to a `CheckResult` with status "timeout" or "failed" and an explanation in
`missing`, so the remaining lanes still produce a verdict.

Check contract (see TEAM_CONTEXT.md):

    async def run_check(asset: Asset, emit: Emit) -> CheckResult

Checks must be async and must not block the event loop. Wrap blocking work in
`asyncio.to_thread`, or the timeout cannot fire.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Mapping

from core.ledger import EvidenceLedger, LedgerError
from shared.config import CHECK_IDS, CHECK_TIMEOUT_S
from shared.schemas import Asset, CheckId, CheckResult, StatusEvent, utc_now

log = logging.getLogger(__name__)

Emit = Callable[[StatusEvent], "Awaitable[None] | None"]
CheckFn = Callable[[Asset, Emit], Awaitable[CheckResult]]


@dataclass
class OrchestratorRun:
    asset: Asset
    results: dict[str, CheckResult]
    ledger: EvidenceLedger
    events: list[StatusEvent] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def usable(self) -> dict[str, CheckResult]:
        """Lanes whose scores can feed a verdict."""
        return {k: r for k, r in self.results.items() if r.status in ("complete", "partial")}

    @property
    def degraded(self) -> dict[str, CheckResult]:
        return {k: r for k, r in self.results.items() if r.status in ("timeout", "failed")}


def degraded_result(check_id: CheckId, status: str, reason: str) -> CheckResult:
    """An honest empty result: no score claim, no evidence, and why."""
    return CheckResult(
        check_id=check_id,
        score=0.0,
        confidence="low",
        status=status,  # "timeout" | "failed"
        reasons=[],
        missing=[reason],
        ledger_rows=[],
    )


def load_default_checks(check_ids: tuple[str, ...] = CHECK_IDS) -> dict[str, CheckFn]:
    """Import `checks.<id>.run_check` for each lane. Lanes not built yet fail cleanly at run time."""
    checks: dict[str, CheckFn] = {}
    for cid in check_ids:
        try:
            module = importlib.import_module(f"checks.{cid}")
            fn = getattr(module, "run_check")
        except (ImportError, AttributeError) as exc:
            fn = _not_implemented(cid, exc)
        checks[cid] = fn
    return checks


def _not_implemented(check_id: str, exc: Exception) -> CheckFn:
    async def _missing(asset: Asset, emit: Emit) -> CheckResult:
        raise NotImplementedError(f"checks.{check_id}.run_check is not available ({exc})")

    return _missing


async def run_checks(
    asset: Asset,
    emit: Emit | None = None,
    *,
    checks: Mapping[str, CheckFn] | None = None,
    timeout_s: float = CHECK_TIMEOUT_S,
) -> OrchestratorRun:
    """Run every check in parallel and collect their evidence into one ledger."""
    checks = dict(checks) if checks is not None else load_default_checks()
    unknown = sorted(set(checks) - set(CHECK_IDS))
    if unknown:
        raise ValueError(f"Unknown check ids {unknown}; expected a subset of {list(CHECK_IDS)}")
    events: list[StatusEvent] = []

    async def _emit(event: StatusEvent) -> None:
        events.append(event)
        if emit is None:
            return
        try:
            out = emit(event)
            if inspect.isawaitable(out):
                await out
        except Exception:  # a broken UI stream must never kill a lane
            log.exception("emit failed for %s", event.check_id)

    started = time.monotonic()
    await _emit(StatusEvent(check_id="orchestrator", status="started",
                            message=f"Running {', '.join(checks)} for {asset.drug}", timestamp=utc_now()))

    lane_results = await asyncio.gather(
        *(_run_lane(cid, fn, asset, _emit, timeout_s) for cid, fn in checks.items())
    )

    ledger = EvidenceLedger(as_of=asset.as_of)
    results: dict[str, CheckResult] = {}
    for cid, result in zip(checks, lane_results):
        if result.status in ("complete", "partial"):
            try:
                ledger.add_result(result)
            except LedgerError as exc:
                result = degraded_result(cid, "failed", f"{cid} check returned evidence the ledger rejected: {exc}")
                await _emit(StatusEvent(check_id=cid, status="error", message=str(exc), timestamp=utc_now()))
        results[cid] = result

    elapsed = time.monotonic() - started
    await _emit(StatusEvent(
        check_id="orchestrator", status="complete",
        message=f"{len([r for r in results.values() if r.status in ('complete', 'partial')])}/{len(results)} "
                f"checks usable, {len(ledger)} ledger rows, {elapsed:.1f}s",
        timestamp=utc_now(),
    ))
    return OrchestratorRun(asset=asset, results=results, ledger=ledger, events=events, elapsed_s=elapsed)


async def _run_lane(check_id: str, fn: CheckFn, asset: Asset, emit: Emit, timeout_s: float) -> CheckResult:
    await emit(StatusEvent(check_id=check_id, status="started", message=f"{check_id} check started",
                           timestamp=utc_now()))
    task = asyncio.ensure_future(fn(asset, emit))
    try:
        # shield + explicit cancel: we return on time even if the check
        # swallows CancelledError and keeps running.
        result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
    except asyncio.TimeoutError:
        task.cancel()
        task.add_done_callback(_drain)
        msg = f"{check_id} check timed out after {timeout_s:g}s; no evidence collected"
        await emit(StatusEvent(check_id=check_id, status="error", message=msg, timestamp=utc_now()))
        return degraded_result(check_id, "timeout", msg)
    except Exception as exc:
        msg = f"{check_id} check failed: {type(exc).__name__}: {exc}"
        log.warning(msg, exc_info=True)
        await emit(StatusEvent(check_id=check_id, status="error", message=msg, timestamp=utc_now()))
        return degraded_result(check_id, "failed", msg)

    if not isinstance(result, CheckResult) or result.check_id != check_id:
        got = getattr(result, "check_id", type(result).__name__)
        msg = f"{check_id} check returned an invalid result (got {got!r})"
        await emit(StatusEvent(check_id=check_id, status="error", message=msg, timestamp=utc_now()))
        return degraded_result(check_id, "failed", msg)

    await emit(StatusEvent(check_id=check_id, status="complete",
                           message=f"{check_id} check {result.status}: score {result.score:.2f}, "
                                   f"{len(result.ledger_rows)} ledger rows",
                           timestamp=utc_now()))
    return result


def _drain(task: asyncio.Future) -> None:
    # Retrieve the outcome of an abandoned task so asyncio does not log it as unhandled.
    if not task.cancelled():
        task.exception()
