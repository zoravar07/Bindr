"""Tests for checks/market/ using recorded real responses and orchestrator integration."""

from __future__ import annotations

import asyncio
from datetime import date
import gzip
import json
from pathlib import Path
from typing import Any
import httpx
import pytest

from checks.market import run_check
from checks.market.check import (
    assert_reasons_backed_by_ledger,
    compute_resistance_score,
    compute_score_components,
    rank_by_structure_only,
    rank_escape_candidates,
    rank_structure_only_as_of,
)
from checks.market.structure import (
    CURATED_EGFR_ESCAPE_RESIDUES,
    is_clinically_actionable_variant,
    is_curated_escape_residue,
    SiftsMappingError,
    _evaluate_pdb_candidate_sync,
    analyze_structure,
    resolve_ligand_comp_ids,
)
from checks.market.variants import build_variant_ledger_row, fetch_variants
from core.orchestrator import run_checks
from shared.schemas import Asset, CheckResult, LedgerRow, Reason, StatusEvent, utc_now

FIXTURES_DIR = Path(__file__).parent / "fixtures"
VARIATION_FIXTURE = FIXTURES_DIR / "ebi_variation_P00533.json.gz"
API_BUNDLE_FIXTURE = FIXTURES_DIR / "structure_api_P00533_YY3.json.gz"
CIFS_BUNDLE_FIXTURE = FIXTURES_DIR / "structure_cifs_P00533_YY3.json.gz"


@pytest.fixture(scope="module")
def recorded_fixtures() -> dict[str, Any]:
    """Load recorded real API responses from /tests/fixtures/."""
    with gzip.open(VARIATION_FIXTURE, "rt", encoding="utf-8") as fh:
        variation = json.load(fh)
    with gzip.open(API_BUNDLE_FIXTURE, "rt", encoding="utf-8") as fh:
        api_bundle = json.load(fh)
    with gzip.open(CIFS_BUNDLE_FIXTURE, "rt", encoding="utf-8") as fh:
        cifs_bundle = json.load(fh)
    return {
        "variation": variation,
        "api": api_bundle,
        "cifs": cifs_bundle,
    }


def _build_mock_client(
    recorded: dict[str, Any],
    *,
    override_ligand_response: dict | None = None,
    override_entry_response: dict | None = None,
) -> httpx.AsyncClient:
    """Create an httpx.AsyncClient serving recorded real API responses."""
    api = recorded["api"]
    cifs = recorded["cifs"]
    variation = recorded["variation"]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "ebi.ac.uk/proteins/api/variation/P00533" in url:
            return httpx.Response(200, json=variation, request=request)
        if "ebi.ac.uk/proteins/api/variation/P29371" in url:
            return httpx.Response(200, json=api["variation_P29371"], request=request)

        if "search.rcsb.org/rcsbsearch/v2/query" in url:
            body = json.loads(request.content.decode("utf-8"))
            if body.get("return_type") == "mol_definition":
                if override_ligand_response is not None:
                    return httpx.Response(200, json=override_ligand_response, request=request)
                val = body.get("query", {}).get("parameters", {}).get("value", "")
                if val == "CHEMBL3353410":
                    return httpx.Response(200, json=api["rcsb_ligand_CHEMBL3353410"], request=request)
                if val == "CHEMBL83":
                    return httpx.Response(200, json=api["rcsb_ligand_CHEMBL83"], request=request)
                if val == "Tagrisso":
                    return httpx.Response(200, json=api["rcsb_ligand_synonym_Tagrisso"], request=request)
                return httpx.Response(
                    200,
                    json={"result_type": "mol_definition", "total_count": 0, "result_set": []},
                    request=request,
                )
            if body.get("return_type") == "entry":
                if override_entry_response is not None:
                    return httpx.Response(200, json=override_entry_response, request=request)
                nodes = body.get("query", {}).get("nodes", [])
                vals = [n.get("parameters", {}).get("value", "") for n in nodes]
                if "P03372" in vals:
                    for comp in ("CTX", "QBN", "OHT"):
                        if comp in vals:
                            return httpx.Response(200, json=api[f"rcsb_entries_P03372_{comp}"], request=request)
                full_res = dict(api["rcsb_entries_P00533_YY3"])
                full_res["result_set"] = [
                    r for r in full_res["result_set"] if r["identifier"] in ("6LUD", "7JXP", "7JXM", "6JXT")
                ]
                return httpx.Response(200, json=full_res, request=request)

        if "ebi.ac.uk/chembl/api/data/molecule/search.json" in url and "Tagrisso" in url:
            return httpx.Response(200, json=api["chembl_search_Tagrisso"], request=request)
        if "ebi.ac.uk/chembl/api/data/molecule/search.json" in url and "tamoxifen" in url.lower():
            return httpx.Response(200, json=api["chembl_search_tamoxifen"], request=request)
        if "ebi.ac.uk/chembl/api/data/metabolism.json" in url and "CHEMBL83" in url:
            return httpx.Response(200, json=api["chembl_metabolism_CHEMBL83"], request=request)
        if "ebi.ac.uk/unichem/api/v1/connectivity" in url:
            ubody = json.loads(request.content.decode("utf-8"))
            mcid = ubody.get("compound", "")
            if f"unichem_conn_{mcid}" in api:
                return httpx.Response(200, json=api[f"unichem_conn_{mcid}"], request=request)

        if "ebi.ac.uk/chembl/api/data/molecule/CHEMBL1201577.json" in url:
            return httpx.Response(200, json=api["chembl_molecule_CHEMBL1201577"], request=request)
        if "ebi.ac.uk/chembl/api/data/molecule/CHEMBL3608680.json" in url:
            return httpx.Response(200, json=api["chembl_molecule_CHEMBL3608680"], request=request)

        if "data.rcsb.org/rest/v1/core/entry/" in url:
            pdb_id = url.rsplit("/", 1)[-1].upper()
            return httpx.Response(200, json=api["rcsb_entry_meta"][pdb_id], request=request)

        if "ebi.ac.uk/pdbe/api/mappings/uniprot/" in url:
            pdb_id_lower = url.rsplit("/", 1)[-1].lower()
            return httpx.Response(200, json=api["pdbe_sifts"][pdb_id_lower], request=request)

        if "files.rcsb.org/download/" in url and url.endswith(".cif"):
            pdb_id = url.rsplit("/", 1)[-1].replace(".cif", "").upper()
            return httpx.Response(200, text=cifs[pdb_id], request=request)

        if "alphafold.ebi.ac.uk/api/prediction/P00533" in url:
            return httpx.Response(200, json=api["alphafold_P00533"], request=request)
        if "alphafold.ebi.ac.uk/api/prediction/P29371" in url:
            return httpx.Response(200, json=api["alphafold_P29371"], request=request)

        if "rest.uniprot.org/uniprotkb/P00533.json" in url:
            return httpx.Response(200, json=api["uniprot_binding_P00533"], request=request)
        if "rest.uniprot.org/uniprotkb/P29371.json" in url:
            return httpx.Response(200, json=api["uniprot_binding_P29371"], request=request)

        return httpx.Response(404, json={"error": f"Unhandled URL: {url}"}, request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_run_check_osimertinib_egfr_full_run(
    tmp_path: Path, recorded_fixtures: dict[str, Any]
) -> None:
    """Verify full osimertinib/EGFR run returns CheckResult + CheckResult.structure, high confidence, and valid reasons."""
    async with _build_mock_client(recorded_fixtures) as client:
        events: list[StatusEvent] = []

        asset = Asset(
            drug="osimertinib",
            target="EGFR",
            disease="NSCLC",
            ids={"uniprot_id": "P00533", "chembl_id": "CHEMBL3353410"},
            as_of=None,
        )

        result = await run_check(asset, events.append, cache_dir=tmp_path, client=client)

        assert isinstance(result, CheckResult)
        assert result.check_id == "market"
        assert result.status == "complete"
        assert result.confidence == "high"
        assert 0.5 < result.score <= 1.0

        # Verify StatusEvent lifecycle
        assert all(e.check_id == "market" for e in events)
        event_statuses = [e.status for e in events]
        assert event_statuses[0] == "started"
        assert "fetching" in event_statuses
        assert "computing" in event_statuses
        assert event_statuses[-1] == "done"

        # Verify StructurePayload attached on CheckResult.structure with confidence in confidence_note
        assert result.structure is not None
        assert result.structure.source == "pdb"
        assert result.structure.structure_id == "6JXT"
        assert "confidence=high" in result.structure.confidence_note

        # Verify exactly 1 structure LedgerRow (with evidence_date) + claimed variant LedgerRows
        struct_rows = [r for r in result.ledger_rows if r.id.startswith("market-structure-")]
        assert len(struct_rows) == 1
        assert struct_rows[0].id == "market-structure-pdb-6JXT"
        assert struct_rows[0].evidence_date == date(2020, 4, 29)
        assert all(r.check_id == "market" for r in result.ledger_rows)

        ledger_ids = {r.id for r in result.ledger_rows}
        assert "market-variant-P00533-C797S" in ledger_ids
        assert "market-variant-P00533-T790M" in ledger_ids

        # Verify skipped engineered structures are listed in missing
        assert any("6LUD skipped:" in m for m in result.missing)
        assert any("7JXP skipped:" in m for m in result.missing)
        assert any("7JXM skipped:" in m for m in result.missing)

        # Verify every Reason references valid LedgerRow IDs, has direction in ('risk', 'support'), and includes the 4 score components
        assert len(result.reasons) >= 2
        for reason in result.reasons:
            assert len(reason.ledger_row_ids) >= 1
            assert all(rid in ledger_ids for rid in reason.ledger_row_ids)
            assert reason.direction in ("risk", "support")
        assert any(
            "covalent term=" in r.text
            and "annotated pocket term=" in r.text
            and "pocket density term=" in r.text
            and "non-pocket annotated term=" in r.text
            for r in result.reasons
        )


async def test_orchestrator_run_checks_osimertinib_egfr_market_only(
    tmp_path: Path, recorded_fixtures: dict[str, Any]
) -> None:
    """Verify core.orchestrator.run_checks on osimertinib / EGFR with only the market check returns a valid CheckResult."""
    async with _build_mock_client(recorded_fixtures) as client:
        asset = Asset(
            drug="osimertinib",
            target="EGFR",
            disease="NSCLC",
            ids={"uniprot_id": "P00533", "chembl_id": "CHEMBL3353410"},
            as_of=None,
        )

        async def _market_lane(a: Asset, emit: Any) -> CheckResult:
            return await run_check(a, emit, cache_dir=tmp_path, client=client)

        orch_run = await run_checks(asset, checks={"market": _market_lane}, timeout_s=30.0)

        assert "market" in orch_run.results
        market_res = orch_run.results["market"]
        assert isinstance(market_res, CheckResult)
        assert market_res.check_id == "market"
        assert market_res.status == "complete"
        assert market_res.confidence == "high"
        assert 0.5 < market_res.score <= 1.0
        assert market_res.structure is not None
        assert market_res.structure.structure_id == "6JXT"
        assert "market" in orch_run.usable
        assert len(orch_run.ledger) == len(market_res.ledger_rows)


async def test_run_check_as_of_caps_confidence_at_medium(
    tmp_path: Path, recorded_fixtures: dict[str, Any]
) -> None:
    """Verify passing as_of after 6JXT release date uses PDB 6JXT but caps confidence at 'medium'."""
    async with _build_mock_client(recorded_fixtures) as client:
        asset = Asset(
            drug="osimertinib",
            target="EGFR",
            disease="NSCLC",
            ids={"uniprot_id": "P00533", "chembl_id": "CHEMBL3353410"},
            as_of=date(2020, 6, 1),
        )

        result = await run_check(asset, None, cache_dir=tmp_path, client=client)

        assert result.structure is not None
        assert result.structure.source == "pdb"
        assert result.structure.structure_id == "6JXT"
        assert "confidence=medium" in result.structure.confidence_note
        assert result.confidence == "medium"
        assert result.status == "partial"
        assert any("as_of (2020-06-01) not enforceable on EBI Variation variants" in m for m in result.missing)
        assert any("6LUD skipped: released 2020-10-07 after as_of 2020-06-01" in m for m in result.missing)


async def test_run_check_no_structure_fallback_to_alphafold(
    tmp_path: Path, recorded_fixtures: dict[str, Any]
) -> None:
    """Verify fallback to AlphaFold sets confidence='low', status='failed' (unscored), and lists fallback + Not scored in missing."""
    empty_entries = {"result_type": "entry", "total_count": 0, "result_set": []}
    async with _build_mock_client(recorded_fixtures, override_entry_response=empty_entries) as client:
        asset = Asset(
            drug="osimertinib",
            target="EGFR",
            disease="NSCLC",
            ids={"uniprot_id": "P00533", "chembl_id": "CHEMBL3353410"},
            as_of=None,
        )

        result = await run_check(asset, None, cache_dir=tmp_path, client=client)

        assert result.status == "failed"
        assert result.confidence == "low"
        assert result.score == 0.0
        assert result.structure is not None
        assert result.structure.source == "alphafold"
        assert result.structure.structure_id == "AF-P00533-F1"
        assert "confidence=low" in result.structure.confidence_note
        assert any("Structure fallback used: AlphaFold DB model AF-P00533-F1" in m for m in result.missing)
        assert any("Not scored:" in m for m in result.missing)


async def test_run_check_ambiguous_ligand_returns_failed_with_not_scored_in_missing(
    tmp_path: Path, recorded_fixtures: dict[str, Any]
) -> None:
    """Verify ambiguous ligand resolution returns status='failed', Not scored in missing, and 1 backed Reason."""
    ambig_lig = {
        "result_type": "mol_definition",
        "total_count": 2,
        "result_set": [
            {"identifier": "YY3", "score": 1.0},
            {"identifier": "7V0", "score": 0.95},
        ],
    }
    async with _build_mock_client(recorded_fixtures, override_ligand_response=ambig_lig) as client:
        asset = Asset(
            drug="osimertinib",
            target="EGFR",
            disease="NSCLC",
            ids={"uniprot_id": "P00533", "chembl_id": "CHEMBL3353410"},
        )

        result = await run_check(asset, None, cache_dir=tmp_path, client=client)

        assert result.status == "failed"
        assert result.confidence == "low"
        assert result.score == 0.0
        assert result.structure is None
        assert any("Ambiguous ligand candidates for 'osimertinib': YY3, 7V0" in m for m in result.missing)
        assert any("Not scored:" in m and "YY3, 7V0" in m for m in result.missing)
        assert len(result.reasons) == 1
        assert "YY3, 7V0" in result.reasons[0].text
        assert result.reasons[0].direction == "risk"
        assert result.reasons[0].ledger_row_ids == [result.ledger_rows[0].id]


async def test_reason_ledger_id_assertion_and_frequency_independence(
    tmp_path: Path, recorded_fixtures: dict[str, Any]
) -> None:
    """Verify Reason ledger-ID assertion fails on unbacked IDs and population frequency never alters score/rank."""
    sample_row = LedgerRow(
        id="market-structure-pdb-6JXT",
        claim="Test structure claim",
        source_record="PDB:6JXT",
        url="https://www.rcsb.org/structure/6JXT",
        date_pulled=utc_now(),
        licence="CC0 1.0",
        check_id="market",
    )

    with pytest.raises(AssertionError, match="not in CheckResult.ledger_rows"):
        assert_reasons_backed_by_ledger(
            [Reason(text="Bad citation", ledger_row_ids=["non-existent-id"], direction="risk")],
            [sample_row],
        )

    async with _build_mock_client(recorded_fixtures) as client:
        asset = Asset(
            drug="osimertinib",
            target="EGFR",
            disease="NSCLC",
            ids={"uniprot_id": "P00533", "chembl_id": "CHEMBL3353410"},
        )
        res1 = await run_check(asset, None, cache_dir=tmp_path, client=client)
        var_res = await fetch_variants("P00533", cache_dir=tmp_path, client=client)
        mutated_variants = [v.model_copy(update={"variant_frequency": 0.999}) for v in var_res.variants]
        assert res1.structure is not None
        pocket_pos = {
            int(r.residue[1:]) for r in res1.structure.residues if r.in_binding_pocket
        }
        rank_orig = [v.id for v in rank_escape_candidates(var_res.variants, {}, pocket_pos)]
        rank_mut = [v.id for v in rank_escape_candidates(mutated_variants, {}, pocket_pos)]
        assert rank_orig == rank_mut
        assert compute_resistance_score(var_res.variants, {}, pocket_pos) == compute_resistance_score(
            mutated_variants, {}, pocket_pos
        )


async def test_run_check_brand_name_tagrisso_and_biologic_cetuximab(
    tmp_path: Path, recorded_fixtures: dict[str, Any]
) -> None:
    """Verify brand name Tagrisso resolves via ChEMBL parent to YY3/6JXT, and biologic cetuximab reports Antibody modality."""
    async with _build_mock_client(recorded_fixtures) as client:
        tagrisso_asset = Asset(
            drug="Tagrisso",
            target="EGFR",
            disease="NSCLC",
            ids={"uniprot_id": "P00533"},
        )
        res_tag = await run_check(tagrisso_asset, None, cache_dir=tmp_path / "tagrisso", client=client)
        assert res_tag.status == "complete"
        assert res_tag.confidence == "high"
        assert res_tag.structure is not None
        assert res_tag.structure.source == "pdb"
        assert res_tag.structure.structure_id == "6JXT"

        cetuximab_asset = Asset(
            drug="cetuximab",
            target="EGFR",
            disease="NSCLC",
            ids={"uniprot_id": "P00533", "chembl_id": "CHEMBL1201577"},
        )
        res_cet = await run_check(cetuximab_asset, None, cache_dir=tmp_path / "cetuximab", client=client)
        assert res_cet.status == "failed"
        assert res_cet.confidence == "low"
        assert res_cet.structure is not None
        assert res_cet.structure.source == "alphafold"
        assert any(
            "Biologic modality ('cetuximab', Antibody) has no small-molecule PDB chem_comp_id" in m
            for m in res_cet.missing
        )
        assert any("Not scored:" in m for m in res_cet.missing)


async def test_run_check_insufficient_evidence_reason_on_no_structure_and_no_annotated_variants(
    tmp_path: Path, recorded_fixtures: dict[str, Any]
) -> None:
    """Verify no drug-bound structure + no annotated variants returns status='failed' and 'Not scored: insufficient evidence'."""
    async with _build_mock_client(recorded_fixtures) as client:
        asset = Asset(
            drug="fezolinetant",
            target="TACR3",
            disease="vasomotor symptoms",
            ids={"uniprot_id": "P29371", "chembl_id": "CHEMBL3608680"},
        )

        result = await run_check(asset, None, cache_dir=tmp_path, client=client)

        assert result.status == "failed"
        assert result.confidence == "low"
        assert result.score == 0.0
        assert (
            "None found in sources used (EBI Variation/ClinVar); absence is not evidence of no resistance."
            in result.missing
        )
        assert (
            "Not scored: insufficient evidence. A score of 0.0 here means we found nothing, not low risk"
            in result.missing
        )
        assert any(
            r.text == "Not scored: insufficient evidence. A score of 0.0 here means we found nothing, not low risk"
            for r in result.reasons
        )


async def test_rank_by_structure_only_and_as_of_harness(
    tmp_path: Path, recorded_fixtures: dict[str, Any]
) -> None:
    """Verify rank_by_structure_only ranks all mapped chain residues strictly by distance ascending, ties by position."""
    async with _build_mock_client(recorded_fixtures) as client:
        asset = Asset(
            drug="osimertinib",
            target="EGFR",
            disease="NSCLC",
            ids={"uniprot_id": "P00533", "chembl_id": "CHEMBL3353410"},
        )

        ranked_contacts = await rank_structure_only_as_of(
            asset,
            as_of=date(2020, 6, 1),
            in_pocket_cutoff=4.5,
            cache_dir=tmp_path,
            client=client,
        )
        assert len(ranked_contacts) > 200
        assert ranked_contacts[0].uniprot_pos == 797
        assert ranked_contacts[0].uniprot_wild_type == "C"
        assert ranked_contacts[0].in_binding_pocket is True
        pairs = [(c.contact_distance_angstrom, c.uniprot_pos) for c in ranked_contacts]
        assert pairs == sorted(pairs)
        tight_ranked = rank_by_structure_only(ranked_contacts, in_pocket_cutoff=2.0)
        assert [c.uniprot_pos for c in tight_ranked if c.in_binding_pocket] == [797]


async def test_osimertinib_6jxt_escape_ranking_vs_structure_only(
    tmp_path: Path, recorded_fixtures: dict[str, Any]
) -> None:
    """Verify C797S is rank 1, T790M is in top 5, G719S/C/R do not outrank them, and ranking beats/matches structure-only."""
    async with _build_mock_client(recorded_fixtures) as client:
        asset = Asset(
            drug="osimertinib",
            target="EGFR",
            disease="NSCLC",
            ids={"uniprot_id": "P00533", "chembl_id": "CHEMBL3353410"},
        )
        var_res = await fetch_variants("P00533", cache_dir=tmp_path, client=client)
        st_res = await analyze_structure(asset, var_res.variants, cache_dir=tmp_path, client=client)
        pocket_pos = {
            pos for pos, c in st_res.contact_by_position.items() if c.in_binding_pocket
        }
        ranked = rank_escape_candidates(var_res.variants, st_res.contact_by_position, pocket_pos)
        names = [v.short_name for v in ranked]

        assert names[0] == "C797S"
        assert "T790M" in names[:5]
        t790m_idx = names.index("T790M")

        for g719_var in ("G719S", "G719C", "G719R"):
            assert g719_var in names
            assert names.index(g719_var) > t790m_idx

        struct_only = rank_by_structure_only(st_res.all_chain_contacts)
        struct_pos_order = [c.uniprot_pos for c in struct_only]

        ranked_unique_pos: list[int] = []
        for v in ranked:
            if v.position not in ranked_unique_pos:
                ranked_unique_pos.append(v.position)

        for res_code in CURATED_EGFR_ESCAPE_RESIDUES:
            pos = int(res_code[1:])
            assert ranked_unique_pos.index(pos) <= struct_pos_order.index(pos)

        # Verify CURATED_EGFR_ESCAPE_RESIDUES does not feed the score (only T790M has pathogenic/resistance annotation in pocket)
        comp = compute_score_components(var_res.variants, st_res.contact_by_position, pocket_pos)
        vus_curated_only = [
            v for v in var_res.variants
            if is_curated_escape_residue(v) and not is_clinically_actionable_variant(v)
        ]
        assert len(vus_curated_only) > 0
        comp_curated_vus = compute_score_components(
            vus_curated_only, st_res.contact_by_position, pocket_pos
        )
        assert comp_curated_vus.annotated_pocket_term == 0.0
        assert comp_curated_vus.non_pocket_annotated_term == 0.0


async def test_as_of_2019_uses_no_annotation_data(
    tmp_path: Path, recorded_fixtures: dict[str, Any]
) -> None:
    """Verify a 2019-01-01 run (and any as_of run) skips all annotation terms and records leakage exclusion."""
    async with _build_mock_client(recorded_fixtures) as client:
        asset_2019 = Asset(
            drug="osimertinib",
            target="EGFR",
            disease="NSCLC",
            ids={"uniprot_id": "P00533", "chembl_id": "CHEMBL3353410"},
            as_of=date(2019, 1, 1),
        )

        result = await run_check(asset_2019, None, cache_dir=tmp_path, client=client)
        assert any("Annotations excluded for as_of (2019-01-01) to avoid future leakage" in m for m in result.missing)
        assert any("Annotations were excluded for as_of (2019-01-01) to avoid future leakage" in r.text for r in result.reasons)
        assert not any("Clinically annotated binding-pocket escape variants" in r.text for r in result.reasons)

        var_res = await fetch_variants("P00533", cache_dir=tmp_path, client=client)
        comp = compute_score_components(var_res.variants, {}, {790, 797, 718}, use_annotations=False)
        assert comp.annotated_pocket_term == 0.0
        assert comp.non_pocket_annotated_term == 0.0


async def test_unscored_cases_osimertinibb_alphafold_and_ambiguous(
    tmp_path: Path, recorded_fixtures: dict[str, Any]
) -> None:
    """Verify non-PDB cases (osimertinibb, AlphaFold fallback, ambiguous ligand) return status='failed' with 'Not scored' in missing."""
    async with _build_mock_client(recorded_fixtures) as client:
        typo_asset = Asset(
            drug="osimertinibb",
            target="EGFR",
            disease="NSCLC",
            ids={"uniprot_id": "P00533"},
        )
        res_typo = await run_check(typo_asset, None, cache_dir=tmp_path / "typo", client=client)
        assert res_typo.status == "failed"
        assert res_typo.confidence == "low"
        assert res_typo.score == 0.0
        assert any("Not scored:" in m and "osimertinibb" in m for m in res_typo.missing)

    empty_entries = {"result_type": "entry", "total_count": 0, "result_set": []}
    async with _build_mock_client(recorded_fixtures, override_entry_response=empty_entries) as af_client:
        af_asset = Asset(
            drug="osimertinib",
            target="EGFR",
            disease="NSCLC",
            ids={"uniprot_id": "P00533", "chembl_id": "CHEMBL3353410"},
        )
        res_af = await run_check(af_asset, None, cache_dir=tmp_path / "af", client=af_client)
        assert res_af.status == "failed"
        assert res_af.confidence == "low"
        assert res_af.score == 0.0
        assert any("Not scored:" in m and "AF-P00533-F1" in m for m in res_af.missing)

    ambig_lig = {
        "result_type": "mol_definition",
        "total_count": 2,
        "result_set": [
            {"identifier": "YY3", "score": 1.0},
            {"identifier": "7V0", "score": 0.95},
        ],
    }
    async with _build_mock_client(recorded_fixtures, override_ligand_response=ambig_lig) as ambig_client:
        res_ambig = await run_check(af_asset, None, cache_dir=tmp_path / "ambig", client=ambig_client)
        assert res_ambig.status == "failed"
        assert res_ambig.confidence == "low"
        assert res_ambig.score == 0.0
        assert any("Not scored:" in m and "ambiguous" in m for m in res_ambig.missing)


async def test_pocket_density_term_normalised_log_scale(
    tmp_path: Path, recorded_fixtures: dict[str, Any]
) -> None:
    """Verify normalised log-scaled pocket density term separates venetoclax/BCL2 (60/20) from olaparib/PARP1 (28/15)."""
    async with _build_mock_client(recorded_fixtures) as client:
        var_res = await fetch_variants("P00533", cache_dir=tmp_path, client=client)

        bcl2_pocket = set(range(101, 121))
        bcl2_variants = [
            var_res.variants[i].model_copy(update={"position": 101 + (i % 20)})
            for i in range(60)
        ]
        comp_bcl2 = compute_score_components(bcl2_variants, {}, bcl2_pocket, use_annotations=False)

        parp1_pocket = set(range(201, 216))
        parp1_variants = [
            var_res.variants[i].model_copy(update={"position": 201 + (i % 15)})
            for i in range(28)
        ]
        comp_parp1 = compute_score_components(parp1_variants, {}, parp1_pocket, use_annotations=False)

        assert comp_bcl2.pocket_density_term == 0.1386
        assert comp_parp1.pocket_density_term == 0.1053
        assert comp_bcl2.pocket_density_term > comp_parp1.pocket_density_term


async def test_tamoxifen_esr1_resolves_to_oht_via_chembl_unichem(
    tmp_path: Path, recorded_fixtures: dict[str, Any]
) -> None:
    """Verify tamoxifen/ESR1 resolves via ChEMBL (CHEMBL83 -> 4OHtam CHEMBL2137046 -> UniChem OHT) before RCSB name search."""
    async with _build_mock_client(recorded_fixtures) as client:
        asset = Asset(
            drug="tamoxifen",
            target="ESR1",
            disease="breast cancer",
            ids={"uniprot_id": "P03372"},
        )
        lig_res = await resolve_ligand_comp_ids(asset, cache_dir=tmp_path, client=client)
        assert lig_res.status == "resolved"
        assert lig_res.chem_comp_id == "OHT"
        assert lig_res.candidates == ["OHT"]


async def test_variants_grouping_and_sifts_mismatch_assertions(
    tmp_path: Path, recorded_fixtures: dict[str, Any]
) -> None:
    """Verify variant grouping (T790M, C797S), on-demand LedgerRow creation, and fatal SIFTS mismatch error."""
    async with _build_mock_client(recorded_fixtures) as client:
        var_res = await fetch_variants("P00533", cache_dir=tmp_path, client=client)
        by_short = {v.short_name: v for v in var_res.variants}
        assert "T790M" in by_short
        assert "C797S" in by_short
        c797s_matches = [v for v in var_res.variants if (v.position, v.original_residue, v.new_residue) == (797, "C", "S")]
        assert len(c797s_matches) == 1
        row = build_variant_ledger_row(by_short["T790M"], reason_context="gatekeeper")
        assert row.check_id == "market"

        # Fatal SIFTS mismatch raises SiftsMappingError
        corrupted_sifts = json.loads(json.dumps(recorded_fixtures["api"]["pdbe_sifts"]["6jxt"]))
        corrupted_sifts["6jxt"]["UniProt"]["P00533"]["mappings"][0]["unp_start"] += 1
        corrupted_sifts["6jxt"]["UniProt"]["P00533"]["mappings"][0]["unp_end"] += 1
        variants_by_pos = {v.position: [v] for v in var_res.variants}
        with pytest.raises(SiftsMappingError, match="Fatal SIFTS mapping mismatch"):
            _evaluate_pdb_candidate_sync(
                pdb_id="6JXT",
                uniprot_id="P00533",
                chem_comp_id="YY3",
                cif_text=recorded_fixtures["cifs"]["6JXT"],
                sifts_payload=corrupted_sifts,
                variants_by_pos=variants_by_pos,
            )
