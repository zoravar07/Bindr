"""Backtest 2 tests.

The offline tests use hand-built study records shaped exactly like the
ClinicalTrials.gov v2 payload; they exercise parsing and the leakage guards,
not the evidence. The `live` tests use the real cohorts (cached).
"""

from datetime import date

import pytest

from eval.trial_backtest import (
    BANNED_FEATURE_KEYS,
    BOUNDARY,
    PROTOCOL_FEATURE_KEYS,
    TOP_K,
    Cohorts,
    GraveyardScorer,
    Trial,
    assert_no_outcome_fields,
    build_cohorts,
    enrollment_leakage_audit,
    graveyard_cross_validation,
    parse_trial,
    run_trial_backtest,
    similarity,
    size_only_baseline,
    verify_no_date_leakage,
)


def _study(nct="NCT00000001", status="COMPLETED", start="2022-03-01", completion="2023-06-01",
           phases=("PHASE3",), enrollment=(400, "ESTIMATED"), why_stopped=None, arms=2,
           primary=1, secondary=4, conditions=("Non-Small Cell Lung Cancer",),
           interventions=(("DRUG", "Pembrolizumab"),), criteria=None) -> dict:
    criteria = criteria if criteria is not None else (
        "Inclusion Criteria:\n- Age 18 or older\n- Measurable disease\n"
        "Exclusion Criteria:\n- Prior therapy\n- Active infection\n- Pregnancy\n")
    status_mod = {"overallStatus": status, "startDateStruct": {"date": start},
                  "completionDateStruct": {"date": completion}}
    if why_stopped:
        status_mod["whyStopped"] = why_stopped
    return {"protocolSection": {
        "identificationModule": {"nctId": nct},
        "statusModule": status_mod,
        "sponsorCollaboratorsModule": {"leadSponsor": {"class": "INDUSTRY"}},
        "conditionsModule": {"conditions": list(conditions)},
        "designModule": {
            "studyType": "INTERVENTIONAL", "phases": list(phases),
            "designInfo": {"allocation": "RANDOMIZED", "interventionModel": "PARALLEL",
                           "primaryPurpose": "TREATMENT", "maskingInfo": {"masking": "DOUBLE"}},
            "enrollmentInfo": {"count": enrollment[0], "type": enrollment[1]},
        },
        "armsInterventionsModule": {
            "armGroups": [{"label": f"arm{i}"} for i in range(arms)],
            "interventions": [{"type": t, "name": n} for t, n in interventions],
        },
        "outcomesModule": {
            "primaryOutcomes": [{"measure": f"p{i}"} for i in range(primary)],
            "secondaryOutcomes": [{"measure": f"s{i}"} for i in range(secondary)],
        },
        "eligibilityModule": {"eligibilityCriteria": criteria, "sex": "ALL",
                              "minimumAge": "18 Years", "healthyVolunteers": False},
    }}


def test_parse_extracts_protocol_features_only():
    trial = parse_trial(_study(why_stopped="Slow accrual", status="TERMINATED"))
    assert trial.label == 1 and trial.status == "TERMINATED"
    assert set(trial.features) <= PROTOCOL_FEATURE_KEYS
    assert not (BANNED_FEATURE_KEYS & set(trial.features))
    assert trial.features["n_arms"] == 2
    assert trial.features["n_primary_outcomes"] == 1 and trial.features["n_secondary_outcomes"] == 4
    assert trial.features["n_inclusion"] == 2 and trial.features["n_exclusion"] == 3
    assert trial.features["min_age_years"] == 18
    assert "pembrolizumab" in trial.features["intervention_tokens"]
    assert "lung" in trial.features["condition_tokens"]
    # outcome data is kept on the record for labelling, never inside features
    assert trial.why_stopped == "Slow accrual"
    assert trial.enrollment_count == 400


def test_parse_rejects_non_terminal_status():
    assert parse_trial(_study(status="RECRUITING")) is None
    assert parse_trial(_study(status="ACTIVE_NOT_RECRUITING")) is None
    assert parse_trial({"protocolSection": {}}) is None


def test_assert_no_outcome_fields_is_the_guard():
    trial = parse_trial(_study())
    assert_no_outcome_fields(trial.features)
    with pytest.raises(AssertionError, match="outcome fields reached the scorer"):
        assert_no_outcome_fields({**trial.features, "why_stopped": "Slow accrual"})
    with pytest.raises(AssertionError, match="outcome fields reached the scorer"):
        assert_no_outcome_fields({**trial.features, "overall_status": "TERMINATED"})
    with pytest.raises(AssertionError, match="outside the protocol allowlist"):
        assert_no_outcome_fields({**trial.features, "site_count_after_start": 3})


def test_banned_keys_cover_the_obvious_leaks():
    for key in ("overall_status", "why_stopped", "completion_date", "has_results", "enrollment_count",
                "enrollment_type", "label", "start_date"):
        assert key in BANNED_FEATURE_KEYS


def test_date_boundary_is_enforced_both_ways():
    grave_ok = parse_trial(_study(nct="NCT00000010", completion="2021-12-31"))
    grave_bad = parse_trial(_study(nct="NCT00000011", completion="2022-01-01"))
    test_ok = parse_trial(_study(nct="NCT00000012", start="2022-01-01"))
    test_bad = parse_trial(_study(nct="NCT00000013", start="2021-12-31"))

    clean = Cohorts(graveyard=[grave_ok], test=[test_ok])
    assert verify_no_date_leakage(clean) == []

    leaky = Cohorts(graveyard=[grave_ok, grave_bad], test=[test_ok, test_bad])
    problems = verify_no_date_leakage(leaky)
    assert any("NCT00000011" in p for p in problems)
    assert any("NCT00000013" in p for p in problems)

    overlapping = Cohorts(graveyard=[grave_ok], test=[grave_ok])
    assert any("both cohorts" in p for p in verify_no_date_leakage(overlapping))


def test_similarity_is_symmetric_and_bounded():
    a = parse_trial(_study(nct="NCT1")).features
    b = parse_trial(_study(nct="NCT2", conditions=("Psoriasis",),
                           interventions=(("DRUG", "Secukinumab"),), phases=("PHASE2",))).features
    assert similarity(a, a) == pytest.approx(1.0)
    assert similarity(a, b) == pytest.approx(similarity(b, a))
    assert 0.0 <= similarity(a, b) < similarity(a, a)


def test_scorer_is_deterministic_and_refuses_leaky_features():
    graveyard = [
        Trial(nct_id=f"NCT{i:08d}", features=parse_trial(_study(nct=f"NCT{i:08d}")).features,
              label=i % 2, status="TERMINATED" if i % 2 else "COMPLETED", start_date=date(2015, 1, 1),
              completion_date=date(2018, 1, 1), enrollment_count=100, enrollment_type="ACTUAL",
              why_stopped=None, phase="PHASE3")
        for i in range(10)
    ]
    scorer = GraveyardScorer(graveyard, k=5)
    query = parse_trial(_study(nct="NCT99999999")).features
    assert scorer.score(query) == scorer.score(query)
    assert 0.0 <= scorer.score(query) <= 1.0
    assert scorer.base_rate == pytest.approx(0.5)
    assert len(scorer.neighbours(query, 3)) == 3
    with pytest.raises(AssertionError):
        scorer.score({**query, "why_stopped": "x"})


def test_size_only_baseline_reports_when_it_cannot_run():
    finished = [
        Trial(nct_id=f"NCT{i:08d}", features={}, label=i % 2, status="COMPLETED",
              start_date=date(2022, 6, 1), completion_date=date(2023, 6, 1), enrollment_count=50,
              enrollment_type="ACTUAL", why_stopped=None, phase="PHASE3")
        for i in range(40)
    ]
    out = size_only_baseline(finished)
    assert out["available"] is False and out["auroc"] is None
    assert "ACTUAL" in out["note"]

    audit = enrollment_leakage_audit(finished)
    assert audit["enrollment_type_share_terminated"] == {"ACTUAL": 1.0}


def test_enrollment_audit_surfaces_why_stopped_asymmetry():
    trials = [
        Trial(nct_id="NCT1", features={}, label=1, status="TERMINATED", start_date=date(2022, 1, 1),
              completion_date=date(2023, 1, 1), enrollment_count=10, enrollment_type="ACTUAL",
              why_stopped="Business decision", phase="PHASE3"),
        Trial(nct_id="NCT2", features={}, label=0, status="COMPLETED", start_date=date(2022, 1, 1),
              completion_date=date(2023, 1, 1), enrollment_count=300, enrollment_type="ACTUAL",
              why_stopped=None, phase="PHASE3"),
    ]
    audit = enrollment_leakage_audit(trials)
    assert audit["why_stopped_present_terminated"] == 1
    assert audit["why_stopped_present_completed"] == 0


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------


@pytest.mark.live
async def test_real_cohorts_have_zero_date_leakage():
    cohorts = await build_cohorts()
    assert len(cohorts.graveyard) > 500 and len(cohorts.test) > 500
    assert verify_no_date_leakage(cohorts) == []
    # the explicit boundary assertions
    for t in cohorts.graveyard:
        assert t.completion_date < BOUNDARY, (t.nct_id, t.completion_date)
    for t in cohorts.test:
        assert t.start_date >= BOUNDARY, (t.nct_id, t.start_date)
    assert not ({t.nct_id for t in cohorts.graveyard} & {t.nct_id for t in cohorts.test})
    for t in list(cohorts.graveyard)[:50] + list(cohorts.test)[:50]:
        assert_no_outcome_fields(t.features)


@pytest.mark.live
async def test_trial_backtest_reports_an_honest_headline():
    cohorts = await build_cohorts()
    result = await run_trial_backtest(cohorts, k=25, run_cv=False)
    assert result["backtest"] == "trial"
    assert result["date_leakage_violations"] == []
    m = result["model"]
    assert m["n_test"] == len(cohorts.test) and m["n_graveyard"] == len(cohorts.graveyard)
    assert 0.0 <= m[f"precision_at_{TOP_K}"] <= 1.0
    assert 0.0 <= m["auroc"] <= 1.0
    assert result["baselines"]["phase_base_rate"]["auroc"] == 0.5
    assert len(m["top_flagged"]) == TOP_K
    assert all(f["url"].startswith("https://clinicaltrials.gov/study/NCT") for f in m["top_flagged"])
    assert len(result["caveats"]) >= 3
    # every test trial reports ACTUAL enrollment, so the size baseline must declare itself unavailable
    assert result["baselines"]["size_only"].get("available") is False


@pytest.mark.live
async def test_graveyard_cv_shows_in_period_signal():
    """The model works within its own era; the test cohort is where it breaks."""
    cohorts = await build_cohorts()
    cv = graveyard_cross_validation(cohorts.graveyard[:600], k_values=(10, 50))
    assert cv["n"] == 600 and cv["best_k"] in (10, 50)
    assert cv["best_auroc"] > 0.55, cv
    assert set(cv["by_k"]) == {"10", "50"}
