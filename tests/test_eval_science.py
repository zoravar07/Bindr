"""Backtest 1 tests. The `live` ones run the real check over the cohort (cached)."""

from datetime import date

import pytest

from eval.cohorts import SCIENCE_COHORT, Programme
from eval.science_backtest import (
    AS_OF_EXCLUDED_ROWS,
    HEADLINE_MODE,
    TOP_K,
    ProgrammeScore,
    asset_for,
    audit_leakage,
    baseline_failure_probability,
    find_failure_case,
    model_block,
    run_science_backtest,
)
from shared.schemas import CheckResult, LedgerRow, Reason, utc_now


def _programme(**kw) -> Programme:
    base = dict(programme_id="x", target_symbol="T", ensembl_id="ENSG1", disease_name="d",
                disease_id="MONDO_1", drug="drugx", chembl_id="CHEMBL1", outcome="failed",
                as_of=date(2015, 1, 1), outcome_detail="failed Phase 2", nct_id="NCT00000001",
                nct_status="TERMINATED")
    base.update(kw)
    return Programme(**base)


def test_asset_carries_the_ids_the_check_needs():
    a = asset_for(SCIENCE_COHORT[0])
    assert {"chembl_id", "ensembl_id", "efo_id", "target_symbol"} <= set(a.ids)
    assert a.as_of == SCIENCE_COHORT[0].as_of


def test_leakage_audit_catches_future_evidence_and_excluded_rows():
    p = _programme()
    future = LedgerRow(id="sci-ot-assoc", claim="c", source_record="r", url="https://x/1",
                       date_pulled=utc_now(), licence="CC0 1.0", check_id="science",
                       evidence_date=date(2020, 1, 1))
    bad = CheckResult(check_id="science", score=0.5, confidence="low", status="complete",
                      reasons=[], missing=[], ledger_rows=[future])
    problems = audit_leakage(p, bad)
    assert any("after as_of" in m or "rejected" in m for m in problems)

    excluded = LedgerRow(id="sci-depmap", claim="c", source_record="r", url="https://x/2",
                         date_pulled=utc_now(), licence="CC BY 4.0", check_id="science")
    problems = audit_leakage(p, CheckResult(check_id="science", score=0.5, confidence="low",
                                            status="complete", reasons=[], missing=[],
                                            ledger_rows=[excluded]))
    assert any("must be excluded" in m for m in problems)

    clean = LedgerRow(id="sci-ot-assoc", claim="c", source_record="r", url="https://x/3",
                      date_pulled=utc_now(), licence="CC0 1.0", check_id="science",
                      evidence_date=date(2010, 1, 1))
    assert audit_leakage(p, CheckResult(check_id="science", score=0.5, confidence="low",
                                        status="complete", reasons=[], missing=[],
                                        ledger_rows=[clean])) == []


def test_leakage_audit_catches_cutoff_after_approval():
    p = _programme(outcome="approved", approval_year=2014, as_of=date(2015, 1, 1), nct_id=None,
                   nct_status=None)
    assert any("does not precede approval" in m for m in audit_leakage(
        p, CheckResult(check_id="science", score=0.1, confidence="low", status="complete",
                       reasons=[], missing=[], ledger_rows=[])))


def test_excluded_row_set_matches_the_checks_as_of_policy():
    assert {"sci-ot-dt-clinical", "sci-ot-dt-literature", "sci-depmap"} <= AS_OF_EXCLUDED_ROWS
    assert "sci-ot-dt-genetic_association" not in AS_OF_EXCLUDED_ROWS
    assert "sci-ot-dt-somatic_mutation" not in AS_OF_EXCLUDED_ROWS
    assert "sci-ot-dt-animal_model" not in AS_OF_EXCLUDED_ROWS


def test_baseline_is_constant_within_a_prior_era():
    early = _programme(as_of=date(2013, 1, 1))
    late = _programme(as_of=date(2022, 1, 1))
    assert baseline_failure_probability(early, gate_priors=True) is None  # no prior published yet
    assert baseline_failure_probability(early, gate_priors=False) == pytest.approx(1 - 0.289)
    assert baseline_failure_probability(late, gate_priors=True) == pytest.approx(1 - 0.289)


def test_model_block_ranks_and_scores_a_synthetic_perfect_model():
    scored = []
    for i in range(5):
        scored.append(ProgrammeScore(programme=_programme(programme_id=f"f{i}", outcome="failed"),
                                     score=0.9 - i * 0.01, status="complete"))
    for i in range(5):
        scored.append(ProgrammeScore(programme=_programme(programme_id=f"a{i}", outcome="approved",
                                                          approval_year=2020, nct_id=None, nct_status=None),
                                     score=0.1 + i * 0.01, status="complete"))
    block = model_block(scored)
    assert block["comparison"]["auroc"] == 1.0
    assert block["n_scored"] == 10 and block["n_not_scored"] == 0
    assert block[f"recall_at_{TOP_K}"] == 1.0
    assert block["ranking"][0]["outcome"] == "failed"


def test_failure_case_identifies_the_worst_ranked_programme():
    scored = [
        ProgrammeScore(programme=_programme(programme_id="f-low", outcome="failed"), score=0.05,
                       status="complete", support_reasons=["Strong genetic support."]),
        ProgrammeScore(programme=_programme(programme_id="f-hi", outcome="failed"), score=0.9,
                       status="complete"),
        ProgrammeScore(programme=_programme(programme_id="a-low", outcome="approved", approval_year=2020,
                                            nct_id=None, nct_status=None), score=0.1, status="complete"),
    ]
    case = find_failure_case(scored)
    assert case["programme_id"] == "f-low"
    assert case["actual_outcome"] == "failed"
    assert "target validity and clinical efficacy" in case["what_it_means"].lower()
    assert case["citation_url"].startswith("https://")
    assert find_failure_case([]) is None


@pytest.mark.live
async def test_science_backtest_runs_clean_over_the_real_cohort():
    result = await run_science_backtest()
    assert result["backtest"] == "science"
    assert result["leakage_violations"] == {}, result["leakage_violations"]

    for mode in ("strict", "methodology"):
        block = result["modes"][mode]
        assert block["model"]["n_scored"] == 20, (mode, block["model"]["n_not_scored"])
        for row in block["programmes"]:
            assert row["error"] is None, row
            assert row["status"] in ("complete", "partial")
            assert row["leakage"] == []
            assert row["n_ledger_rows"] > 0

    model = result["modes"][HEADLINE_MODE]["model"]["comparison"]
    assert 0.0 <= model["auroc"] <= 1.0
    assert model["n_positive"] == 10 and model["n_negative"] == 10
    # the headline claim: better ordering than a constant base rate
    assert model["auroc"] > result["baseline_auroc"]
    assert result["baseline_auroc"] == 0.5
    assert result["headline_value"] == pytest.approx(
        model["mean_positive"] - model["mean_negative"], abs=1e-4)
    assert result["failure_case"]["programme_id"] in {p.programme_id for p in SCIENCE_COHORT}
    assert len(result["cohort_caveats"]) >= 3


@pytest.mark.live
async def test_methodology_mode_gives_every_programme_the_same_base_rate():
    result = await run_science_backtest()
    rates = {row["base_rate"] for row in result["modes"]["methodology"]["programmes"]}
    assert len(rates) == 1 and rates.pop() == pytest.approx(0.289)
    # strict mode is heterogeneous precisely because early cutoffs predate the priors
    strict_rates = {row["base_rate"] for row in result["modes"]["strict"]["programmes"]}
    assert len(strict_rates) > 1
