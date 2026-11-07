"""Backtest 3 tests.

Offline tests build tiny PDB records with known geometry so the contact
distances can be checked against hand arithmetic. The `live` tests run the
real benchmark against RCSB and UniProt (cached).
"""

import math

import pytest

from eval.cohorts import RESISTANCE_BENCHMARK, SCORED_RESISTANCE_CASES, EscapeMutation, ResistanceCase
from eval.market_backtest import (
    CONTACT_FLOOR,
    K_CURVE,
    POCKET_ANGSTROM,
    TOP_K,
    VARIANT_SATURATION,
    parse_pdb,
    proximity,
    run_market_backtest,
    score_residues,
    validate_numbering,
    variant_density,
)


def pdb_line(record, serial, name, resname, chain, resseq, x, y, z, element):
    """A column-exact PDB ATOM/HETATM record."""
    line = (f"{record:<6}{serial:>5} {name:<4}" + " " + f"{resname:>3} {chain}{resseq:>4}" + "    "
            + f"{x:>8.3f}{y:>8.3f}{z:>8.3f}" + f"{1.0:>6.2f}{0.0:>6.2f}" + " " * 10 + f"{element:>2}")
    return line


def _structure_text():
    return "\n".join([
        # ligand heavy atom at the origin, plus a hydrogen that must be ignored
        pdb_line("HETATM", 1, "C1", "LIG", "A", 900, 0.0, 0.0, 0.0, "C"),
        pdb_line("HETATM", 2, "H1", "LIG", "A", 900, 0.5, 0.0, 0.0, "H"),
        # water must be ignored
        pdb_line("HETATM", 3, "O", "HOH", "A", 901, 1.0, 0.0, 0.0, "O"),
        # THR 790 at exactly 5.0 A (3,4,0)
        pdb_line("ATOM  ", 4, "CA", "THR", "A", 790, 3.0, 4.0, 0.0, "C"),
        pdb_line("ATOM  ", 5, "CB", "THR", "A", 790, 6.0, 8.0, 0.0, "C"),
        # CYS 797 at exactly 2.0 A
        pdb_line("ATOM  ", 6, "CA", "CYS", "A", 797, 2.0, 0.0, 0.0, "C"),
        # GLY 101 far away, 20.0 A
        pdb_line("ATOM  ", 7, "CA", "GLY", "A", 101, 20.0, 0.0, 0.0, "C"),
        # a residue on another chain must be ignored
        pdb_line("ATOM  ", 8, "CA", "LEU", "B", 500, 1.0, 0.0, 0.0, "C"),
        # altloc B must be ignored
        "ATOM      9  CA BVAL A 600       1.000   0.000   0.000  1.00  0.00           C",
    ])


def test_parse_pdb_computes_exact_heavy_atom_distances():
    s = parse_pdb(_structure_text(), "LIG", "A")
    assert s.ligand_atom_count == 1  # hydrogen excluded
    assert set(s.residues) == {790, 797, 101}  # other chain, water, altloc B excluded
    assert s.residues[790].min_distance == pytest.approx(5.0)
    assert s.residues[790].one_letter == "T" and s.residues[790].n_atoms == 2
    assert s.residues[797].min_distance == pytest.approx(2.0)
    assert s.residues[101].min_distance == pytest.approx(20.0)
    pocket = {r.number for r in s.pocket}
    assert pocket == {790, 797}  # 20 A is outside the 8 A pocket


def test_parse_pdb_without_ligand_leaves_distances_infinite():
    s = parse_pdb(_structure_text(), "NOPE", "A")
    assert s.ligand_atom_count == 0
    assert all(math.isinf(r.min_distance) for r in s.residues.values())
    assert s.pocket == []


def test_proximity_and_variant_density_curves():
    assert proximity(CONTACT_FLOOR) == pytest.approx(1.0)
    assert proximity(1.0) == pytest.approx(1.0)  # saturates below the floor
    assert proximity(CONTACT_FLOOR + 2.0) == pytest.approx(math.exp(-1.0))
    assert proximity(float("inf")) == 0.0
    assert proximity(20.0) < proximity(5.0) < proximity(3.0)
    assert variant_density(0) == 0.0
    assert variant_density(int(VARIANT_SATURATION)) == 1.0
    assert variant_density(1000) == 1.0


def test_score_ranks_contacts_first_and_breaks_ties_on_residue_number():
    s = parse_pdb(_structure_text(), "LIG", "A")
    ranked = score_residues(s, variant_counts={})
    assert [r.number for r in ranked] == [797, 790, 101]
    # determinism under equal scores
    flat = parse_pdb(_structure_text(), "NOPE", "A")
    assert [r.number for r in score_residues(flat, {})] == [101, 790, 797]


def test_variant_density_can_promote_a_residue():
    s = parse_pdb(_structure_text(), "LIG", "A")
    plain = {r.number: r.score for r in score_residues(s, {})}
    boosted = {r.number: r.score for r in score_residues(s, {101: 100})}
    assert boosted[101] > plain[101]
    assert boosted[797] == pytest.approx(plain[797])


def test_validate_numbering_catches_offsets_and_missing_residues():
    s = parse_pdb(_structure_text(), "LIG", "A")
    s.pdb_id = "TEST"
    good = ResistanceCase("t", "EGFR", "P00533", "d", "TEST", "LIG", "A",
                          (EscapeMutation(797, "C", "S", "10.1/x"),))
    assert validate_numbering(good, s) == []

    wrong_wt = ResistanceCase("t", "EGFR", "P00533", "d", "TEST", "LIG", "A",
                              (EscapeMutation(797, "T", "M", "10.1/x"),))
    problems = validate_numbering(wrong_wt, s)
    assert any("numbering offset likely" in p for p in problems)

    absent = ResistanceCase("t", "EGFR", "P00533", "d", "TEST", "LIG", "A",
                            (EscapeMutation(5000, "C", "S", "10.1/x"),))
    assert any("absent from the resolved structure" in p for p in validate_numbering(absent, s))

    no_ligand = ResistanceCase("t", "EGFR", "P00533", "d", "TEST", "ZZZ", "A",
                               (EscapeMutation(797, "C", "S", "10.1/x"),))
    assert any("not found in chain" in p for p in validate_numbering(no_ligand, parse_pdb(
        _structure_text(), "ZZZ", "A")))


def test_offset_is_applied_when_declared():
    s = parse_pdb(_structure_text(), "LIG", "A")
    # label numbered 10 higher than the structure, declared via residue_offset
    shifted = ResistanceCase("t", "EGFR", "P00533", "d", "TEST", "LIG", "A",
                             (EscapeMutation(807, "C", "S", "10.1/x"),), residue_offset=10)
    assert validate_numbering(shifted, s) == []


# ---------------------------------------------------------------------------
# Live
# ---------------------------------------------------------------------------


@pytest.mark.live
async def test_every_benchmark_structure_has_verified_numbering():
    result = await run_market_backtest()
    assert result["numbering_problems"] == {}, result["numbering_problems"]
    for case in result["cases"]:
        assert case["ligand_heavy_atoms"] > 10, case["case_id"]
        assert case["n_residues_resolved"] > 100, case["case_id"]
        assert case["escapes_absent_from_structure"] == [], case["case_id"]
        assert case["n_escapes_scorable"] == case["n_escapes"]
        assert case["pdb_url"].startswith("https://www.rcsb.org/structure/")
        assert case["n_uniprot_features"] > 0, case["case_id"]
        for e in case["escapes"]:
            assert e["url"].startswith("https://doi.org/10.")
            assert e["min_distance_angstrom"] is not None
            assert e["rank"] is not None


@pytest.mark.live
async def test_recall_beats_both_random_baselines_at_every_k():
    result = await run_market_backtest()
    headline = result["headline_value"]
    assert headline == result["model"][f"recall_at_{TOP_K}_micro"]
    assert headline > result["baselines"]["random_in_pocket"][f"expected_recall_at_{TOP_K}"]
    assert headline > result["baselines"]["random_whole_chain"][f"expected_recall_at_{TOP_K}"]
    assert result["model"]["lift_over_pocket_baseline"] > 1.0
    for k in K_CURVE:
        row = result["recall_curve"][str(k)]
        assert row["model"] > row["random_in_pocket"] >= row["random_whole_chain"], (k, row)


@pytest.mark.live
async def test_allosteric_escape_is_reported_as_a_miss_not_hidden():
    result = await run_market_backtest()
    missed = {m["mutation"]: m for m in result["missed_escapes"]}
    # ALK C1156Y acts away from the ligand, so contact distance cannot rank it
    assert "C1156Y" in missed
    assert missed["C1156Y"]["min_distance_angstrom"] > POCKET_ANGSTROM
    assert missed["C1156Y"]["in_pocket"] is False
    assert any("allosteric" in c.lower() for c in result["caveats"])


@pytest.mark.live
async def test_braf_is_carried_as_a_documented_exclusion():
    result = await run_market_backtest()
    excluded = result["excluded_cases"]
    assert [c["case_id"] for c in excluded] == ["braf-vemurafenib"]
    assert excluded[0]["reason"]
    assert len(result["cases"]) == len(SCORED_RESISTANCE_CASES) == len(RESISTANCE_BENCHMARK) - 1
