"""Science check. `live` tests hit Open Targets + ChEMBL (cached in .cache/)."""

from datetime import date

import pytest

from checks.science import run_check
from checks.science.check import (
    AS_OF_DATATYPES,
    BASE_RATE_PRIORS,
    GENETIC_PRIORS,
    Features,
    base_key_for_stage,
    compute_score,
    depmap_lines_for_disease,
    pick_prior,
)
from core.gemini_client import GeminiClient
from core.ledger import EvidenceLedger
from core.orchestrator import run_checks
from core.resolver import resolve_asset

NO_LLM = GeminiClient(api_key="")


def test_score_monotone_in_evidence():
    weak = Features(base_rate=0.289)
    strong = Features(base_rate=0.289, association=0.9, genetic_rs=2.6,
                      datatypes={"genetic_association": 0.9, "animal_model": 0.5})
    no_assoc, _ = compute_score(weak)
    with_ev, terms = compute_score(strong)
    assert 0 < with_ev < no_assoc < 1
    assert ("no target-disease association", -0.7) in compute_score(weak)[1]
    assert any(name == "human genetics" for name, _ in terms)


def test_published_constants_match_sources():
    hay, bio = (p.values for p in BASE_RATE_PRIORS)
    assert hay["loa"] == 0.104 and bio["loa"] == 0.079
    # the published phase-transition rates multiply back to the published LOA
    for v in (hay, bio):
        assert v["p1"] * v["p2"] * v["p3"] * v["nda"] == pytest.approx(v["loa"], abs=0.001)
    assert [p.values["rs"] for p in GENETIC_PRIORS] == [2.0, 2.6]
    assert [p.url for p in GENETIC_PRIORS] == ["https://doi.org/10.1038/ng.3314",
                                                "https://doi.org/10.1038/s41586-024-07316-0"]


def test_as_of_whitelist_drops_literature_and_clinical():
    dts = {"genetic_association": 0.9, "literature": 1.0, "clinical": 1.0, "animal_model": 0.5}
    full, full_terms = compute_score(Features(base_rate=0.289, association=0.9, genetic_rs=2.6, datatypes=dts))
    hist, hist_terms = compute_score(Features(base_rate=0.289, association=0.9, genetic_rs=2.6, datatypes=dts,
                                              datatypes_allowed=AS_OF_DATATYPES, use_target_safety=False,
                                              safety_liabilities=50, lof_intolerant_inhibited=True))
    names = {n for n, _ in hist_terms}
    assert {"literature", "clinical", "many safety liabilities", "LoF-intolerant target inhibited"}.isdisjoint(names)
    assert {"human genetics", "animal_model"} <= names
    assert hist > full


def test_depmap_disease_matching():
    depmap = [{"tissueName": "lung", "screens": [
        {"diseaseFromSource": "Non-Small Cell Lung Cancer", "geneEffect": -1.2},
        {"diseaseFromSource": "Small Cell Lung Cancer", "geneEffect": -0.1},
        {"diseaseFromSource": "Lung Neuroendocrine Tumor", "geneEffect": -0.2},
        {"diseaseFromSource": "Non-Small Cell Lung Cancer", "geneEffect": None},
    ]}]
    assert len(depmap_lines_for_disease(depmap, "non-small cell lung carcinoma")) == 1
    assert len(depmap_lines_for_disease(depmap, "lung cancer")) == 2
    assert depmap_lines_for_disease(depmap, "Alzheimer disease") == []
    dep, terms = compute_score(Features(base_rate=0.5, association=0.5, depmap_dependent_fraction=0.5))
    assert ("DepMap dependency", 0.3) in terms
    _, terms = compute_score(Features(base_rate=0.5, association=0.5, common_essential=True))
    assert ("common essential gene", -0.3) in terms


def test_priors_respect_as_of():
    assert pick_prior(BASE_RATE_PRIORS, None).key == "bio2021"
    assert pick_prior(BASE_RATE_PRIORS, date(2018, 1, 1)).key == "hay2014"
    assert pick_prior(BASE_RATE_PRIORS, date(2010, 1, 1)) is None
    assert pick_prior(GENETIC_PRIORS, date(2020, 1, 1)).key == "nelson2015"
    assert base_key_for_stage("APPROVAL") == "nda"
    assert base_key_for_stage("PHASE_1_2") == "p2"
    assert base_key_for_stage(None) == "p2"


async def test_missing_ids_fail_cleanly():
    from shared.schemas import Asset
    r = await run_check(Asset(drug="x", target="y", disease="z"), lambda e: None)
    assert r.status == "failed" and r.ledger_rows == [] and r.missing


@pytest.fixture
async def egfr_nsclc():
    return await resolve_asset("Osimertinib targeting EGFR in NSCLC", gemini=NO_LLM)


@pytest.mark.live
async def test_egfr_nsclc_is_low_risk_with_full_provenance(egfr_nsclc):
    events = []
    r = await run_check(egfr_nsclc, events.append)
    assert [e.status for e in events] == ["started", "fetching", "computing", "done"]
    assert r.status == "complete" and r.confidence == "high"
    assert r.score < 0.15

    by_id = {row.id: row for row in r.ledger_rows}
    assert {"sci-ot-assoc", "sci-ot-dt-genetic_association", "sci-ot-tract", "sci-chembl-potency",
            "sci-depmap"} <= set(by_id)
    assert by_id["sci-depmap"].licence.startswith("CC BY 4.0")
    assert by_id["sci-depmap"].url == "https://depmap.org/portal/gene/EGFR"
    assert "non-small cell lung carcinoma cell lines dependent" in by_id["sci-depmap"].claim
    assert by_id["sci-ot-assoc"].licence == "CC0 1.0"
    assert by_id["sci-chembl-potency"].licence == "CC BY-SA 3.0"
    assert by_id["sci-ot-assoc"].url.startswith("https://platform.opentargets.org/evidence/ENSG00000146648/")
    assert by_id["sci-chembl-potency"].url.startswith("https://www.ebi.ac.uk/chembl/api/data/activity.json")
    for row in r.ledger_rows:
        assert row.licence and row.date_pulled and row.url.startswith("https://")
    EvidenceLedger().add_result(r)  # every reason cites a row the result carries


@pytest.mark.live
async def test_weak_target_scores_much_higher_risk(egfr_nsclc):
    strong = await run_check(egfr_nsclc, lambda e: None)
    weak_asset = await resolve_asset("", drug="metoprolol", disease="non-small cell lung carcinoma", gemini=NO_LLM)
    assert weak_asset.target == "ADRB1"
    weak = await run_check(weak_asset, lambda e: None)
    assert weak.status == "complete"
    assert weak.score > strong.score + 0.4
    assert any(x.direction == "risk" and "association" in x.text for x in weak.reasons)
    assert any("genetic_association" in m for m in weak.missing)


@pytest.mark.live
async def test_as_of_blocks_future_evidence(egfr_nsclc):
    asset = egfr_nsclc.model_copy(update={"as_of": date(2016, 1, 1)})
    r = await run_check(asset, lambda e: None)
    ids = {row.id for row in r.ledger_rows}
    assert "sci-prior-hay2014" in ids and "sci-prior-bio2021" not in ids
    assert "sci-prior-nelson2015" in ids and "sci-prior-minikel2024" not in ids
    assert "clinical" not in r.reasons[0].text  # clinical precedence excluded
    assert "literature" not in r.reasons[0].text
    assert not ids & {"sci-ot-dt-literature", "sci-ot-dt-clinical", "sci-ot-safety", "sci-ot-constraint", "sci-depmap"}
    assert any("current snapshot" in m for m in r.missing)
    assert any("literature excluded under as_of" in m and "post-trial" in m for m in r.missing)
    assert any("DepMap" in m and "as_of" in m for m in r.missing)
    assert r.confidence != "high"
    EvidenceLedger(as_of=asset.as_of).add_result(r)  # nothing dated after as_of


@pytest.mark.live
async def test_orchestrator_runs_real_science_lane(egfr_nsclc):
    run = await run_checks(egfr_nsclc, timeout_s=60)
    assert run.results["science"].status == "complete"
    assert len(run.ledger.rows("science")) >= 8
