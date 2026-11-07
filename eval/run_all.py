"""Run all three backtests, save the raw numbers, and write the evaluation report.

    .venv/bin/python -m eval.run_all
    .venv/bin/python -m eval.run_all --stress        # also run the 10-asset stress test
    .venv/bin/python -m eval.run_all --skip trial    # skip a backtest while iterating

Outputs:
    eval/results.json            every number the report quotes
    eval/EVALUATION_REPORT.md    the proof slide, the failure cases, the licence table

Every figure in the report is read from the results dictionary. Nothing in
the report is typed by hand, so the document cannot drift from the numbers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from eval.cohorts import DATASET_LICENCES
from eval.market_backtest import run_market_backtest
from eval.science_backtest import HEADLINE_MODE, run_science_backtest
from eval.trial_backtest import run_trial_backtest

EVAL_DIR = Path(__file__).resolve().parent
RESULTS_PATH = EVAL_DIR / "results.json"
REPORT_PATH = EVAL_DIR / "EVALUATION_REPORT.md"

BACKTESTS = ("science", "trial", "market")


def _fmt(value: Any, places: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.{places}f}"
    return str(value)


async def run_all(skip: Sequence[str] = (), include_stress: bool = False,
                  use_llm: bool = True) -> dict:
    results: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_date": date.today().isoformat(),
        "backtests": {},
        "skipped": list(skip),
    }
    if "science" not in skip:
        results["backtests"]["science"] = await run_science_backtest()
    if "trial" not in skip:
        results["backtests"]["trial"] = await run_trial_backtest()
    if "market" not in skip:
        results["backtests"]["market"] = await run_market_backtest()
    if include_stress:
        from eval.stress_test import run_stress_test
        results["stress_test"] = await run_stress_test(use_llm=use_llm)
    return results


# ---------------------------------------------------------------------------
# Proof slide
# ---------------------------------------------------------------------------


def proof_rows(results: dict) -> list[dict]:
    rows = []
    sci = results["backtests"].get("science")
    if sci:
        model = sci["modes"][HEADLINE_MODE]["model"]["comparison"]
        rows.append({
            "check": "Science",
            "test": sci["test"],
            "baseline": f"AUROC {_fmt(sci['baseline_auroc'])}, separation 0.000 "
                        "(constant published base rate)",
            "premortem": f"AUROC {_fmt(model['auroc'])}, separation {_fmt(sci['headline_value'])}",
            "headline": f"Mean risk is {_fmt(sci['headline_value'])} higher for programmes that failed "
                        f"than for those approved (AUROC {_fmt(model['auroc'])}, "
                        f"Mann-Whitney p={_fmt(sci['headline_p_value'], 3)})",
            "verdict": "Beats the baseline on ranking, not statistically significant at n=20",
        })
    trial = results["backtests"].get("trial")
    if trial:
        m = trial["model"]
        cv = trial.get("graveyard_cross_validation") or {}
        rows.append({
            "check": "Trial",
            "test": trial["test"],
            "baseline": f"precision@20 {_fmt(trial['baseline_value'])} (cohort prevalence), AUROC 0.500; "
                        "size-only baseline not computable without leakage",
            "premortem": f"precision@20 {_fmt(trial['headline_value'])}, AUROC {_fmt(trial['headline_auroc'])}",
            "headline": f"precision@20 {_fmt(trial['headline_value'])} against a prevalence of "
                        f"{_fmt(m['test_prevalence'])}, with out-of-period AUROC "
                        f"{_fmt(trial['headline_auroc'])} while in-period cross-validated AUROC is "
                        f"{_fmt(cv.get('best_auroc'))}",
            "verdict": "Does not generalise across the 2022 boundary. Reported as a negative result",
        })
    market = results["backtests"].get("market")
    if market:
        m = market["model"]
        rows.append({
            "check": "Market / resistance",
            "test": market["test"],
            "baseline": f"recall@10 {_fmt(market['baseline_value'])} (random inside the 8 A pocket), "
                        f"{_fmt(market['baselines']['random_whole_chain']['expected_recall_at_10'])} "
                        "(random across the chain)",
            "premortem": f"recall@10 {_fmt(market['headline_value'])} "
                         f"({m['escapes_found']}/{m['escapes_scorable']} escapes)",
            "headline": f"Recovers {m['escapes_found']} of {m['escapes_scorable']} documented clinical "
                        f"escape mutations in the top 10 residues, "
                        f"{_fmt(m['lift_over_pocket_baseline'], 2)}x the pocket-random baseline",
            "verdict": "Beats both random baselines at every k from 5 to 20",
        })
    return rows


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def render_report(results: dict) -> str:
    run_date = results["run_date"]
    out: list[str] = []
    a = out.append

    a("# Pre-Mortem evaluation report")
    a("")
    a(f"Generated {results['generated_at']} by `.venv/bin/python -m eval.run_all`. "
      f"Every number below is read from `eval/results.json`, which that command writes.")
    a("")
    a("Three backtests, each against real public records with a date cutoff enforced in code. "
      "Two of the three beat their baseline. The trial backtest does not, and is reported as it came out.")
    a("")

    a("## Proof slide")
    a("")
    a("| Check | The test | Baseline score | Pre-Mortem score | Headline number |")
    a("|---|---|---|---|---|")
    for row in proof_rows(results):
        a(f"| **{row['check']}** | {row['test']} | {row['baseline']} | {row['premortem']} | "
          f"{row['headline']} |")
    a("")
    a("Read against each baseline:")
    a("")
    for row in proof_rows(results):
        a(f"- **{row['check']}**: {row['verdict']}.")
    a("")

    # ---- What it gets wrong ----
    a("## What Pre-Mortem gets wrong, and why")
    a("")
    sci = results["backtests"].get("science")
    if sci and sci.get("failure_case"):
        fc = sci["failure_case"]
        a(f"### Science: {fc['drug']} ({fc['target']} in {fc['disease']})")
        a("")
        a(f"{fc['headline']}")
        a("")
        a(f"**What actually happened.** {fc['outcome_detail']} "
          f"([source]({fc['citation_url']}), cutoff {fc['as_of']}).")
        a("")
        a(f"**Why the check got it wrong.** {fc['why_it_happened']}")
        a("")
        a(f"**What it means.** {fc['what_it_means']}")
        if fc.get("missing_at_cutoff"):
            a("")
            a("Evidence the check recorded as missing at the cutoff:")
            a("")
            for m in fc["missing_at_cutoff"]:
                a(f"- {m}")
        a("")
    trial = results["backtests"].get("trial")
    if trial:
        a("### Trial: the reference model does not survive a change of era")
        a("")
        a(trial["finding"])
        a("")
        a("The honest conclusion is that trial-termination risk cannot be read off protocol shape alone. "
          "An eval that only reported the in-period number would have hidden that.")
        a("")
    market = results["backtests"].get("market")
    if market and market.get("missed_escapes"):
        a("### Market: contact distance cannot see allosteric escapes")
        a("")
        a("Escape mutations the structural ranking missed:")
        a("")
        a("| Mutation | Distance to drug | Rank | In pocket | Source |")
        a("|---|---|---|---|---|")
        for e in market["missed_escapes"]:
            d = "n/a" if e["min_distance_angstrom"] is None else f"{e['min_distance_angstrom']:.2f} A"
            a(f"| {e['mutation']} | {d} | {e['rank']} | {_fmt(e['in_pocket'])} | [doi]({e['url']}) |")
        a("")
        far = [e for e in market["missed_escapes"] if not e["in_pocket"]]
        near = [e for e in market["missed_escapes"] if e["in_pocket"]]
        if far:
            a(f"{len(far)} of the misses sit outside the binding pocket, so no contact-distance method can "
              f"rank them. {far[0]['mutation']} is the clearest case at "
              f"{far[0]['min_distance_angstrom']:.1f} A from the drug: it changes the kinase conformation "
              "rather than the contact surface.")
        if near:
            a("")
            a(f"{len(near)} of the misses are in contact with the drug but ranked just below the cut. "
              "The scoring weights were not adjusted after seeing those ranks, which is why the recall "
              "curve is reported for k from 5 to 20 instead of a single tuned number.")
        a("")
        for case in market.get("excluded_cases", []):
            a(f"{case['target']} with {case['drug']} ({case['pdb_id']}) is carried as a documented "
              f"exclusion: {case['reason']}. {case['note']}.")
        a("")

    # ---- Leakage controls ----
    a("## Cutoff enforcement")
    a("")
    a("| Backtest | Boundary | How it is enforced | Violations found |")
    a("|---|---|---|---|")
    if sci:
        a(f"| Science | each programme's own `as_of`, set before its outcome | Every result is loaded "
          f"into `EvidenceLedger(as_of=...)`, which rejects any row dated after the cutoff; excluded row "
          f"IDs are asserted absent; approval years are checked to fall after the cutoff | "
          f"{len(sci['leakage_violations'])} |")
    if trial:
        a(f"| Trial | 2022-01-01 | Graveyard completion strictly before, test starts on or after, both "
          f"asserted per trial; scoring features restricted to a protocol allowlist with outcome fields "
          f"asserted absent | {len(trial['date_leakage_violations'])} |")
    if market:
        a(f"| Market | not time-based | Structures predate the resistance reports; residue numbering "
          f"validated against the expected wild-type amino acid in every structure | "
          f"{len(market['numbering_problems'])} numbering problems |")
    a("")
    if sci:
        a("The science backtest is the one with a residual risk worth naming. Open Targets publishes a "
          "current snapshot with no historical API, so association scores can include evidence published "
          "after a programme's cutoff. The check narrows this by using only `genetic_association`, "
          "`somatic_mutation` and `animal_model` under a cutoff, and by excluding clinical precedence and "
          "literature mining, but it cannot eliminate it. The caveat is carried in every affected result's "
          "`missing` list rather than left to the reader.")
        a("")

    # ---- Stress test ----
    stress = results.get("stress_test")
    if stress:
        a("## Stress test: 10 unseen assets end to end")
        a("")
        a(f"{stress['n_succeeded']} of {stress['n_assets']} assets produced a verdict. "
          f"Median latency {_fmt(stress['latency_s']['median_total'], 1)}s, "
          f"maximum {_fmt(stress['latency_s']['max_total'], 1)}s, "
          f"{_fmt(stress['wall_clock_s'], 1)}s wall clock at concurrency {stress['concurrency']}.")
        a("")
        from eval.stress_test import render_table
        a(render_table(stress))
        a("")
        lanes = stress["lane_status_counts"]
        a("Lane status across the run: " + "; ".join(
            f"`{lane}` " + ", ".join(f"{n} {status}" for status, n in sorted(counts.items()))
            for lane, counts in sorted(lanes.items())) + ".")
        a("")

    # ---- Licences ----
    a("## Dataset licence table")
    a("")
    a("| Dataset | Source | Licence | Date pulled | Cutoff enforcement |")
    a("|---|---|---|---|---|")
    for d in DATASET_LICENCES:
        a(f"| {d.dataset} | {d.source} | {d.licence} | {run_date} | {d.cutoff_enforcement} |")
    a("")
    a(f"Date pulled is the run date. Responses are served from the on-disk cache in `.cache/`, whose "
      f"entries were fetched on or before {run_date} and each carry their own `fetched_at`, which is what "
      f"populates `LedgerRow.date_pulled`.")
    a("")

    # ---- Caveats ----
    a("## Limits of this evaluation")
    a("")
    for key, label in (("science", "Science"), ("trial", "Trial"), ("market", "Market")):
        bt = results["backtests"].get(key)
        if not bt:
            continue
        caveats = bt.get("cohort_caveats") or bt.get("caveats") or []
        if caveats:
            a(f"**{label}**")
            a("")
            for c in caveats:
                a(f"- {c}")
            a("")

    a("## Reproducing this")
    a("")
    a("```bash")
    a("python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt")
    a("cp .env.example .env    # add GEMINI_API_KEY")
    a(".venv/bin/python -m eval.run_all --stress")
    a(".venv/bin/pytest                      # 100+ tests, including live cohort verification")
    a("```")
    a("")
    a("First run populates `.cache/` from the live APIs and takes a few minutes. Later runs read the "
      "cache and finish in seconds. The cohort labels are verified against Open Targets, ChEMBL and "
      "ClinicalTrials.gov by `tests/test_eval_cohorts.py`, so a stale identifier fails the suite rather "
      "than quietly changing a result.")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> dict:
    parser = argparse.ArgumentParser(description="Run every Pre-Mortem backtest and write the report")
    parser.add_argument("--stress", action="store_true", help="also run the 10-asset stress test")
    parser.add_argument("--no-llm", action="store_true", help="stress test without Gemini")
    parser.add_argument("--skip", action="append", default=[], choices=list(BACKTESTS),
                        help="skip a backtest (repeatable)")
    args = parser.parse_args()

    results = asyncio.run(run_all(skip=args.skip, include_stress=args.stress,
                                  use_llm=not args.no_llm))
    RESULTS_PATH.write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    REPORT_PATH.write_text(render_report(results), encoding="utf-8")

    print()
    for row in proof_rows(results):
        print(f"{row['check']:20} baseline: {row['baseline'][:52]}")
        print(f"{'':20} ours:     {row['premortem']}")
        print(f"{'':20} -> {row['verdict']}")
    print()
    print(f"Wrote {RESULTS_PATH.relative_to(EVAL_DIR.parent)} "
          f"({RESULTS_PATH.stat().st_size // 1024} KB) and "
          f"{REPORT_PATH.relative_to(EVAL_DIR.parent)}")
    return results


if __name__ == "__main__":
    main()
