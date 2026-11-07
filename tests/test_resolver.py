"""Resolver tests. The `live` ones call Open Targets / ChEMBL (cached in .cache/)."""

from datetime import date

import pytest

from core.gemini_client import GeminiClient
from core.resolver import ResolutionError, parse_query_deterministic, pick_primary_indication, resolve_asset

NSCLC_IDS = {"MONDO_0005233", "EFO_0003060"}  # Open Targets moved NSCLC from EFO to MONDO
NO_LLM = GeminiClient(api_key="")


@pytest.mark.parametrize("query, expected", [
    ("Osimertinib", ("Osimertinib", None, None)),
    ("Osimertinib targeting EGFR in NSCLC", ("Osimertinib", "EGFR", "NSCLC")),
    ("osimertinib (EGFR) for non-small cell lung cancer", ("osimertinib", "EGFR", "non-small cell lung cancer")),
    ("Osimertinib / EGFR / NSCLC", ("Osimertinib", "EGFR", "NSCLC")),
    ("Osimertinib, NSCLC", ("Osimertinib", None, "NSCLC")),
])
def test_deterministic_parser(query, expected):
    p = parse_query_deterministic(query)
    assert (p.drug, p.target, p.disease) == expected


def test_primary_indication_prefers_stage_then_specificity():
    rows = [
        {"maxClinicalStage": "APPROVAL", "disease": {"id": "A", "name": "neoplasm", "ancestors": ["x"]}},
        {"maxClinicalStage": "APPROVAL", "disease": {"id": "B", "name": "nsclc", "ancestors": ["x"] * 20}},
        {"maxClinicalStage": "PHASE_3", "disease": {"id": "C", "name": "deep", "ancestors": ["x"] * 40}},
        {"maxClinicalStage": "APPROVAL", "disease": {"id": "D", "name": "ta", "isTherapeuticArea": True,
                                                     "ancestors": ["x"] * 50}},
    ]
    assert pick_primary_indication(rows)["id"] == "B"


@pytest.mark.live
async def test_resolves_osimertinib_from_drug_name_only():
    asset = await resolve_asset("Osimertinib", as_of=date(2020, 1, 1), gemini=NO_LLM)
    assert asset.ids["chembl_id"] == "CHEMBL3353410"
    assert asset.target == "EGFR" and asset.ids["target_symbol"] == "EGFR"
    assert asset.ids["ensembl_id"] == "ENSG00000146648"
    assert asset.ids["uniprot_id"] == "P00533"
    assert asset.ids["efo_id"] in NSCLC_IDS
    assert asset.ids["inferred"] == "target,disease"
    assert asset.as_of == date(2020, 1, 1)


@pytest.mark.live
async def test_resolves_full_sentence_and_salt_form():
    asset = await resolve_asset("Osimertinib mesylate targeting EGFR in NSCLC", gemini=NO_LLM)
    assert asset.ids["chembl_id"] == "CHEMBL3353410"  # salt mapped to parent
    assert asset.ids["efo_id"] in NSCLC_IDS
    assert "inferred" not in asset.ids


@pytest.mark.live
async def test_explicit_args_override_and_unknown_drug_raises():
    asset = await resolve_asset("", drug="pembrolizumab", disease="melanoma", gemini=NO_LLM)
    assert asset.target == "PDCD1"
    assert "melanoma" in asset.disease.lower()
    with pytest.raises(ResolutionError):
        await resolve_asset("zzqxv-not-a-drug-9931", gemini=NO_LLM)
