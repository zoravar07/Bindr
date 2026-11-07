"""Saturday-night stress test: 10 unseen assets end to end.

For each asset: resolve_asset -> run_checks (all available lanes in parallel)
-> write_verdict, with latency recorded at every stage. Nothing here is
mocked, so this is also the honest answer to "does it hold up live?".

Lanes that teammates have not pushed yet degrade to status "failed" with the
reason in `missing`, and the run still produces a verdict from whatever
lanes did work. That is the behaviour under test.

    .venv/bin/python -m eval.stress_test
    .venv/bin/python -m eval.stress_test --no-llm     # skip Gemini, deterministic verdict
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Sequence

from core.gemini_client import GeminiClient
from core.orchestrator import load_default_checks, run_checks
from core.resolver import ResolutionError, resolve_asset
from core.verdict import write_verdict
from eval.cohorts import STRESS_ASSETS
from eval.stats import mean, median
from shared.config import CHECK_TIMEOUT_S


@dataclass
class StressResult:
    query: str
    resolved: str | None = None
    target: str | None = None
    disease: str | None = None
    ids: dict[str, str] = field(default_factory=dict)
    lane_status: dict[str, str] = field(default_factory=dict)
    lane_scores: dict[str, float | None] = field(default_factory=dict)
    overall_risk: str | None = None
    overall_score: float | None = None
    primary_failure_mode: str | None = None
    n_ledger_rows: int = 0
    n_red_flags: int = 0
    synthesis: str | None = None
    resolve_s: float = 0.0
    checks_s: float = 0.0
    verdict_s: float = 0.0
    total_s: float = 0.0
    stage_failed: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.overall_risk is not None

    def to_dict(self) -> dict:
        return {
            "query": self.query, "resolved_drug": self.resolved, "target": self.target,
            "disease": self.disease, "ids": self.ids, "lane_status": self.lane_status,
            "lane_scores": self.lane_scores, "overall_risk": self.overall_risk,
            "overall_score": self.overall_score, "primary_failure_mode": self.primary_failure_mode,
            "n_ledger_rows": self.n_ledger_rows, "n_red_flags": self.n_red_flags,
            "synthesis": self.synthesis, "resolve_s": round(self.resolve_s, 2),
            "checks_s": round(self.checks_s, 2), "verdict_s": round(self.verdict_s, 2),
            "total_s": round(self.total_s, 2), "stage_failed": self.stage_failed,
            "error": self.error, "ok": self.ok,
        }


async def stress_one(query: str, *, gemini: GeminiClient | None, timeout_s: float) -> StressResult:
    out = StressResult(query=query)
    started = time.monotonic()
    try:
        t0 = time.monotonic()
        asset = await resolve_asset(query, gemini=gemini)
        out.resolve_s = time.monotonic() - t0
        out.resolved, out.target, out.disease = asset.drug, asset.target, asset.disease
        out.ids = dict(asset.ids)
    except (ResolutionError, Exception) as exc:
        out.stage_failed = "resolve"
        out.error = f"{type(exc).__name__}: {exc}"
        out.total_s = time.monotonic() - started
        return out

    try:
        t0 = time.monotonic()
        run = await run_checks(asset, checks=load_default_checks(), timeout_s=timeout_s)
        out.checks_s = time.monotonic() - t0
        out.lane_status = {k: r.status for k, r in run.results.items()}
        out.lane_scores = {k: (r.score if r.status in ("complete", "partial") else None)
                           for k, r in run.results.items()}
        out.n_ledger_rows = len(run.ledger)
    except Exception as exc:
        out.stage_failed = "checks"
        out.error = f"{type(exc).__name__}: {exc}"
        out.total_s = time.monotonic() - started
        return out

    try:
        t0 = time.monotonic()
        verdict = await write_verdict(asset, run.results, run.ledger, gemini=gemini)
        out.verdict_s = time.monotonic() - t0
        out.overall_risk = verdict.overall_risk
        out.overall_score = verdict.overall_score
        out.primary_failure_mode = verdict.primary_failure_mode
        out.n_red_flags = len(verdict.red_flags)
        out.synthesis = verdict.synthesis
    except Exception as exc:
        out.stage_failed = "verdict"
        out.error = f"{type(exc).__name__}: {exc}"
    out.total_s = time.monotonic() - started
    return out


async def run_stress_test(assets: Sequence[str] = STRESS_ASSETS, *, use_llm: bool = True,
                          timeout_s: float = CHECK_TIMEOUT_S, concurrency: int = 2) -> dict:
    gemini = None if use_llm else GeminiClient(api_key="")
    sem = asyncio.Semaphore(concurrency)

    async def one(q: str) -> StressResult:
        async with sem:
            return await stress_one(q, gemini=gemini, timeout_s=timeout_s)

    started = time.monotonic()
    results = list(await asyncio.gather(*(one(q) for q in assets)))
    wall = time.monotonic() - started

    totals = [r.total_s for r in results]
    succeeded = [r for r in results if r.ok]
    lanes_seen: dict[str, dict[str, int]] = {}
    for r in results:
        for lane, status in r.lane_status.items():
            lanes_seen.setdefault(lane, {})[status] = lanes_seen.setdefault(lane, {}).get(status, 0) + 1

    return {
        "n_assets": len(assets),
        "n_succeeded": len(succeeded),
        "n_failed": len(results) - len(succeeded),
        "llm_enabled": use_llm,
        "lane_timeout_s": timeout_s,
        "concurrency": concurrency,
        "wall_clock_s": round(wall, 2),
        "latency_s": {
            "mean_total": round(mean(totals), 2) if totals else None,
            "median_total": round(median(totals), 2) if totals else None,
            "max_total": round(max(totals), 2) if totals else None,
            "median_resolve": round(median([r.resolve_s for r in results]), 2) if results else None,
            "median_checks": round(median([r.checks_s for r in results]), 2) if results else None,
            "median_verdict": round(median([r.verdict_s for r in succeeded]), 2) if succeeded else None,
        },
        "lane_status_counts": lanes_seen,
        "stage_failures": {r.query: r.stage_failed for r in results if r.stage_failed},
        "results": [r.to_dict() for r in results],
    }


def render_table(summary: dict) -> str:
    lines = ["| Asset | Resolved | Risk | Score | Lanes | Rows | Flags | Latency |",
             "|---|---|---|---|---|---|---|---|"]
    for r in summary["results"]:
        if r["ok"]:
            lanes = ", ".join(f"{k}:{v}" for k, v in sorted(r["lane_status"].items()))
            resolved = f"{r['resolved_drug']} / {r['target']} / {r['disease']}"
            score = "n/a" if r["overall_score"] is None else f"{r['overall_score']:.2f}"
            lines.append(f"| {r['query'][:34]} | {resolved[:46]} | {r['overall_risk']} | {score} | "
                         f"{lanes} | {r['n_ledger_rows']} | {r['n_red_flags']} | {r['total_s']:.1f}s |")
        else:
            lines.append(f"| {r['query'][:34]} | FAILED at {r['stage_failed']} | - | - | "
                         f"{r['error'][:40] if r['error'] else ''} | - | - | {r['total_s']:.1f}s |")
    return "\n".join(lines)


def main() -> dict:
    parser = argparse.ArgumentParser(description="Pre-Mortem 10-asset stress test")
    parser.add_argument("--no-llm", action="store_true", help="skip Gemini, deterministic verdict only")
    parser.add_argument("--timeout", type=float, default=CHECK_TIMEOUT_S, help="per-lane timeout")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--json", type=str, default=None, help="write the summary to this path")
    args = parser.parse_args()

    summary = asyncio.run(run_stress_test(use_llm=not args.no_llm, timeout_s=args.timeout,
                                          concurrency=args.concurrency))
    print(f"Stress test: {summary['n_succeeded']}/{summary['n_assets']} assets produced a verdict "
          f"in {summary['wall_clock_s']}s wall clock")
    print(f"  latency: median {summary['latency_s']['median_total']}s, max {summary['latency_s']['max_total']}s")
    print(f"  lanes: {json.dumps(summary['lane_status_counts'])}")
    print()
    print(render_table(summary))
    if summary["stage_failures"]:
        print("\nStage failures:")
        for query, stage in summary["stage_failures"].items():
            print(f"  {query}: {stage}")
    if args.json:
        from pathlib import Path
        Path(args.json).write_text(json.dumps(summary, indent=1), encoding="utf-8")
        print(f"\nWrote {args.json}")
    return summary


if __name__ == "__main__":
    main()
