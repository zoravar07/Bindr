"""Verify the curated benchmark labels against the live APIs.

The cohorts in eval/cohorts.py are the only hand-entered data in the eval
package. These tests exist so a typo or a stale identifier cannot silently
corrupt a backtest: every ID, approval year and trial status is checked
against Open Targets, ChEMBL and ClinicalTrials.gov.
"""

import asyncio
import re

import pytest

from eval.cohorts import (
    APPROVED_PROGRAMMES,
    DATASET_LICENCES,
    FAILED_PROGRAMMES,
    RESISTANCE_BENCHMARK,
    SCIENCE_COHORT,
    SCORED_RESISTANCE_CASES,
    STRESS_ASSETS,
)
from shared.sources import chembl_get, ot_query

DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
NCT_RE = re.compile(r"^NCT\d{8}$")


# ---------------------------------------------------------------------------
# Offline structure
# ---------------------------------------------------------------------------


def test_cohort_is_balanced_and_unique():
    assert len(APPROVED_PROGRAMMES) == 10 and len(FAILED_PROGRAMMES) == 10
    ids = [p.programme_id for p in SCIENCE_COHORT]
    assert len(set(ids)) == len(ids)
    assert sum(p.is_failure for p in SCIENCE_COHORT) == 10


def test_every_programme_has_a_verifiable_citation():
    for p in SCIENCE_COHORT:
        assert p.citation and p.citation_url.startswith("https://")
        if p.doi:
            assert DOI_RE.match(p.doi), p.doi
        if p.nct_id:
            assert NCT_RE.match(p.nct_id), p.nct_id
        if p.outcome == "approved":
            assert p.approval_year and p.as_of.year < p.approval_year
        else:
            assert p.nct_id, f"{p.programme_id} needs a trial record"
        assert p.ensembl_id.startswith("ENSG")
        assert p.disease_id.startswith(("EFO_", "MONDO_", "HP_", "OTAR_"))


def test_resistance_benchmark_shape():
    assert 6 <= len(RESISTANCE_BENCHMARK) <= 8
    assert len(SCORED_RESISTANCE_CASES) == len(RESISTANCE_BENCHMARK) - 1  # BRAF excluded
    excluded = [c for c in RESISTANCE_BENCHMARK if c.excluded_from_recall]
    assert [c.case_id for c in excluded] == ["braf-vemurafenib"]
    assert excluded[0].exclusion_reason
    for case in SCORED_RESISTANCE_CASES:
        assert case.escapes, case.case_id
        assert len(case.pdb_id) == 4
        for e in case.escapes:
            assert DOI_RE.match(e.doi), e.doi
            assert e.wild_type.isalpha() and len(e.wild_type) == 1
            assert e.label.startswith(e.wild_type)


def test_licence_table_covers_every_source():
    assert len(DATASET_LICENCES) >= 6
    blob = " ".join(d.source for d in DATASET_LICENCES).lower()
    for src in ("opentargets", "chembl", "clinicaltrials.gov", "rcsb", "uniprot"):
        assert src in blob
    for d in DATASET_LICENCES:
        assert d.licence and d.cutoff_enforcement and d.used_by
    curated = [d for d in DATASET_LICENCES if "curated" in d.licence.lower()]
    assert curated, "the curated labels must be declared in the licence table"


def test_stress_assets_are_distinct():
    assert len(STRESS_ASSETS) == 10
    assert len(set(STRESS_ASSETS)) == 10


# ---------------------------------------------------------------------------
# Live verification
# ---------------------------------------------------------------------------

_TARGETS_Q = "query($ids:[String!]!){ targets(ensemblIds:$ids){ id approvedSymbol } }"
_DISEASES_Q = "query($ids:[String!]!){ diseases(efoIds:$ids){ id name } }"


@pytest.mark.live
async def test_cohort_ensembl_and_disease_ids_resolve():
    ens = sorted({p.ensembl_id for p in SCIENCE_COHORT})
    dis = sorted({p.disease_id for p in SCIENCE_COHORT})
    t_resp, d_resp = await asyncio.gather(ot_query(_TARGETS_Q, {"ids": ens}),
                                          ot_query(_DISEASES_Q, {"ids": dis}))
    symbols = {t["id"]: t["approvedSymbol"] for t in t_resp.data["targets"]}
    names = {d["id"]: d["name"] for d in d_resp.data["diseases"]}
    for p in SCIENCE_COHORT:
        assert symbols.get(p.ensembl_id) == p.target_symbol, p.programme_id
        assert names.get(p.disease_id), p.programme_id
        assert p.disease_name.lower() == names[p.disease_id].lower(), (
            p.programme_id, p.disease_name, names[p.disease_id])


@pytest.mark.live
async def test_approved_programmes_match_chembl_approval_years():
    for p in APPROVED_PROGRAMMES:
        resp = await chembl_get(f"molecule/{p.chembl_id}.json", {"only": "first_approval,max_phase"})
        first = resp.data.get("first_approval")
        assert first is not None, p.programme_id
        assert int(first) == p.approval_year, (p.programme_id, first, p.approval_year)
        assert p.as_of.year < int(first), f"{p.programme_id} peeks past approval"


@pytest.mark.live
async def test_failed_programme_trial_records_match_claimed_status():
    import httpx

    ids = ",".join(p.nct_id for p in FAILED_PROGRAMMES)
    async with httpx.AsyncClient(timeout=45) as client:
        resp = await client.get("https://clinicaltrials.gov/api/v2/studies",
                                params={"filter.ids": ids, "pageSize": 50,
                                        "fields": "NCTId,OverallStatus,Phase,StartDate,CompletionDate"})
    resp.raise_for_status()
    found = {}
    for study in resp.json().get("studies", []):
        proto = study["protocolSection"]
        found[proto["identificationModule"]["nctId"]] = proto["statusModule"].get("overallStatus")
    for p in FAILED_PROGRAMMES:
        assert p.nct_id in found, f"{p.programme_id}: {p.nct_id} not in ClinicalTrials.gov"
        assert found[p.nct_id] == p.nct_status, (p.programme_id, found[p.nct_id], p.nct_status)


@pytest.mark.live
async def test_failed_programme_chembl_ids_exist():
    for p in FAILED_PROGRAMMES:
        resp = await chembl_get(f"molecule/{p.chembl_id}.json", {"only": "molecule_chembl_id,pref_name"})
        assert resp.data.get("molecule_chembl_id") == p.chembl_id, p.programme_id
        assert p.drug.lower() in (resp.data.get("pref_name") or "").lower(), p.programme_id
