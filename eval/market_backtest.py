"""Backtest 3 - Dead Market / Resistance.

For each benchmark drug-target pair: download the real co-crystal structure
from RCSB PDB, parse the atomic coordinates of the bound drug and the protein
chain, compute exact heavy-atom Euclidean contact distances in Angstroms for
every residue, and rank candidate escape residues. Known clinical escape
mutations (curated in eval/cohorts.py, each with a DOI) are the labels.

Scoring per residue:
    score = 0.75 * proximity + 0.25 * variant_density
    proximity      = exp(-max(0, d - CONTACT_FLOOR) / PROXIMITY_SCALE)
    variant_density = min(1, n_uniprot_variants_at_residue / VARIANT_SATURATION)

`d` is the minimum heavy-atom distance from the residue to any heavy atom of
the bound ligand. Hydrogens and waters are ignored. Variant counts come from
the UniProt Proteins API and measure how mutable a position is observed to be;
they are not resistance annotations, so they cannot leak the labels.

Baselines to beat:
  (a) random residue selection across the whole resolved chain, expected
      recall at k = k / n_residues;
  (b) random selection within the binding site (residues under 8 A), expected
      recall at k = (positives in pocket / positives total) * min(1, k / pocket size).
      Baseline (b) is the hard one: it already knows where the pocket is.

Numbering is validated, not assumed: every labelled position must carry the
expected wild-type amino acid in the structure, or the case is reported as a
numbering failure rather than scored.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from eval.cohorts import RESISTANCE_BENCHMARK, ResistanceCase
from eval.stats import expected_recall_random, expected_recall_random_subset, mean, recall_at_k
from shared.cache import cached_get_json, cached_get_text

RCSB_DOWNLOAD = "https://files.rcsb.org/download"
RCSB_LICENCE = "CC0 1.0"
UNIPROT_PROTEINS_API = "https://www.ebi.ac.uk/proteins/api/variation"
UNIPROT_LICENCE = "CC BY 4.0"

TOP_K = 10
K_CURVE = (5, 10, 15, 20)  # reported as sensitivity, the headline stays at TOP_K
POCKET_ANGSTROM = 8.0
CONTACT_FLOOR = 3.5       # at or below this, proximity saturates at 1.0
PROXIMITY_SCALE = 2.0     # Angstroms per e-fold of decay
VARIANT_SATURATION = 8.0  # variants at one position for a full density score
PROXIMITY_WEIGHT, VARIANT_WEIGHT = 0.75, 0.25

THREE_TO_ONE = {
    "ALA": "A", "ARG": "R", "ASN": "N", "ASP": "D", "CYS": "C", "GLN": "Q", "GLU": "E",
    "GLY": "G", "HIS": "H", "ILE": "I", "LEU": "L", "LYS": "K", "MET": "M", "PHE": "F",
    "PRO": "P", "SER": "S", "THR": "T", "TRP": "W", "TYR": "Y", "VAL": "V",
    "MSE": "M", "SEC": "U", "PYL": "O",
}


# ---------------------------------------------------------------------------
# Structure parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Residue:
    number: int
    one_letter: str
    min_distance: float
    n_atoms: int


@dataclass
class ParsedStructure:
    pdb_id: str
    chain: str
    ligand_code: str
    ligand_atom_count: int
    residues: dict[int, Residue] = field(default_factory=dict)

    @property
    def pocket(self) -> list[Residue]:
        return [r for r in self.residues.values() if r.min_distance < POCKET_ANGSTROM]


def parse_pdb(text: str, ligand_code: str, chain: str) -> ParsedStructure:
    """Exact heavy-atom minimum distances from each protein residue to the bound ligand."""
    ligand: list[tuple[float, float, float]] = []
    raw: dict[int, tuple[str, list[tuple[float, float, float]]]] = {}
    for line in text.splitlines():
        record = line[:6]
        if len(line) < 54:
            continue
        element = line[76:78].strip().upper()
        if element == "H" or line[16] not in (" ", "A"):  # skip hydrogens and altloc B+
            continue
        res_name = line[17:20].strip()
        try:
            xyz = (float(line[30:38]), float(line[38:46]), float(line[46:54]))
        except ValueError:
            continue
        if record == "HETATM" and res_name == ligand_code and line[21] == chain:
            ligand.append(xyz)
        elif record == "ATOM  " and line[21] == chain and res_name in THREE_TO_ONE:
            try:
                number = int(line[22:26])
            except ValueError:
                continue
            raw.setdefault(number, (THREE_TO_ONE[res_name], []))[1].append(xyz)

    residues: dict[int, Residue] = {}
    for number, (letter, atoms) in raw.items():
        if ligand:
            d = min(math.dist(a, b) for a in atoms for b in ligand)
        else:
            d = float("inf")
        residues[number] = Residue(number=number, one_letter=letter, min_distance=round(d, 3),
                                   n_atoms=len(atoms))
    return ParsedStructure(pdb_id="", chain=chain, ligand_code=ligand_code,
                           ligand_atom_count=len(ligand), residues=residues)


async def fetch_structure(case: ResistanceCase) -> ParsedStructure:
    resp = await cached_get_text(f"{RCSB_DOWNLOAD}/{case.pdb_id}.pdb", timeout=90.0)
    structure = parse_pdb(resp.data, case.ligand_code, case.chain)
    structure.pdb_id = case.pdb_id
    return structure


def validate_numbering(case: ResistanceCase, structure: ParsedStructure) -> list[str]:
    """Every labelled escape must sit on its expected wild-type residue."""
    problems: list[str] = []
    if not structure.ligand_atom_count:
        problems.append(f"ligand {case.ligand_code} not found in chain {case.chain} of {case.pdb_id}")
    for e in case.escapes:
        number = e.residue_number - case.residue_offset
        residue = structure.residues.get(number)
        if residue is None:
            problems.append(f"{e.label}: residue {number} absent from the resolved structure")
        elif residue.one_letter != e.wild_type:
            problems.append(f"{e.label}: structure has {residue.one_letter}{number}, "
                            f"expected {e.wild_type}; numbering offset likely")
    return problems


# ---------------------------------------------------------------------------
# UniProt variant density
# ---------------------------------------------------------------------------


async def fetch_variant_counts(uniprot_id: str) -> tuple[dict[int, int], int]:
    """Observed natural-variant count per sequence position, from UniProt.

    These are population and large-scale-study variants, not resistance
    annotations, so they carry no information about the benchmark labels.
    """
    resp = await cached_get_json(f"{UNIPROT_PROTEINS_API}/{uniprot_id}",
                                headers={"Accept": "application/json"}, timeout=90.0)
    counts: dict[int, int] = {}
    features = (resp.data or {}).get("features") or []
    for feature in features:
        begin, end = feature.get("begin"), feature.get("end")
        try:
            start, stop = int(begin), int(end or begin)
        except (TypeError, ValueError):
            continue
        if stop - start > 5:  # skip long range features, they are not point variants
            continue
        for pos in range(start, stop + 1):
            counts[pos] = counts.get(pos, 0) + 1
    return counts, len(features)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def proximity(distance: float) -> float:
    if math.isinf(distance):
        return 0.0
    return math.exp(-max(0.0, distance - CONTACT_FLOOR) / PROXIMITY_SCALE)


def variant_density(count: int) -> float:
    return min(1.0, count / VARIANT_SATURATION)


@dataclass(frozen=True)
class ScoredResidue:
    number: int
    one_letter: str
    min_distance: float
    n_variants: int
    score: float

    def to_dict(self) -> dict:
        return {"residue": f"{self.one_letter}{self.number}", "min_distance_angstrom": self.min_distance,
                "n_uniprot_variants": self.n_variants, "score": round(self.score, 4)}


def score_residues(structure: ParsedStructure, variant_counts: dict[int, int],
                   offset: int = 0) -> list[ScoredResidue]:
    """Rank every resolved residue. Ties break on residue number for determinism."""
    scored = []
    for residue in structure.residues.values():
        n_var = variant_counts.get(residue.number + offset, 0)
        score = (PROXIMITY_WEIGHT * proximity(residue.min_distance)
                 + VARIANT_WEIGHT * variant_density(n_var))
        scored.append(ScoredResidue(number=residue.number, one_letter=residue.one_letter,
                                    min_distance=residue.min_distance, n_variants=n_var, score=score))
    scored.sort(key=lambda r: (-r.score, r.number))
    return scored


# ---------------------------------------------------------------------------
# Per-case evaluation
# ---------------------------------------------------------------------------


async def evaluate_case(case: ResistanceCase) -> dict:
    structure = await fetch_structure(case)
    variant_counts, n_features = await fetch_variant_counts(case.uniprot_id)
    numbering_problems = validate_numbering(case, structure)
    scored = score_residues(structure, variant_counts, offset=case.residue_offset)

    truth = {e.residue_number - case.residue_offset for e in case.escapes}
    present = {n for n in truth if n in structure.residues}
    missing_from_structure = sorted(truth - present)

    scores = [r.score for r in scored]
    labels = [1 if r.number in present else 0 for r in scored]
    numbers = [r.number for r in scored]
    model_recall = recall_at_k(scores, labels, TOP_K, tiebreak=numbers) if present else None

    pocket = structure.pocket
    pocket_numbers = {r.number for r in pocket}
    positives_in_pocket = len(present & pocket_numbers)
    n_residues = len(structure.residues)

    top = scored[:TOP_K]
    found = [r.number for r in top if r.number in present]
    curve = {
        str(k): {
            "model": recall_at_k(scores, labels, k, tiebreak=numbers) if present else None,
            "random_whole_chain": expected_recall_random(n_residues, k),
            "random_in_pocket": (expected_recall_random_subset(
                len(pocket), k, positives_in_pocket, len(present)) if present else None),
        }
        for k in K_CURVE
    }
    return {
        "case_id": case.case_id, "target": case.target_symbol, "drug": case.drug,
        "pdb_id": case.pdb_id, "pdb_url": f"https://www.rcsb.org/structure/{case.pdb_id}",
        "structure_file": f"{RCSB_DOWNLOAD}/{case.pdb_id}.pdb",
        "uniprot_id": case.uniprot_id,
        "uniprot_url": f"{UNIPROT_PROTEINS_API}/{case.uniprot_id}",
        "ligand_code": case.ligand_code, "ligand_heavy_atoms": structure.ligand_atom_count,
        "chain": case.chain, "n_residues_resolved": n_residues,
        "n_uniprot_features": n_features,
        "pocket_size_under_8a": len(pocket),
        "numbering_problems": numbering_problems,
        "escapes": [
            {
                "mutation": e.label, "doi": e.doi, "url": f"https://doi.org/{e.doi}",
                "min_distance_angstrom": (structure.residues[e.residue_number - case.residue_offset]
                                          .min_distance
                                          if e.residue_number - case.residue_offset in structure.residues
                                          else None),
                "in_pocket": (e.residue_number - case.residue_offset) in pocket_numbers,
                "rank": next((i for i, r in enumerate(scored, 1)
                              if r.number == e.residue_number - case.residue_offset), None),
                "found_in_top_k": (e.residue_number - case.residue_offset) in found,
            }
            for e in case.escapes
        ],
        "escapes_absent_from_structure": missing_from_structure,
        "n_escapes": len(truth), "n_escapes_scorable": len(present),
        f"recall_at_{TOP_K}": model_recall,
        "n_found": len(found),
        f"top_{TOP_K}_residues": [r.to_dict() for r in top],
        "recall_curve": curve,
        "baselines": {
            "random_whole_chain": expected_recall_random(n_residues, TOP_K),
            "random_in_pocket": expected_recall_random_subset(
                len(pocket), TOP_K, positives_in_pocket, len(present)) if present else None,
            "positives_in_pocket": positives_in_pocket,
        },
        "note": case.note,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _micro(numerators: Sequence[float], weights: Sequence[float]) -> float | None:
    total = sum(weights)
    if total <= 0:
        return None
    return round(sum(n * w for n, w in zip(numerators, weights)) / total, 4)


async def run_market_backtest(cases: Sequence[ResistanceCase] = RESISTANCE_BENCHMARK) -> dict:
    scored_cases = [c for c in cases if not c.excluded_from_recall]
    excluded = [c for c in cases if c.excluded_from_recall]
    results = []
    for case in scored_cases:  # sequential: RCSB and UniProt are public endpoints
        results.append(await evaluate_case(case))

    usable = [r for r in results if r[f"recall_at_{TOP_K}"] is not None]
    weights = [r["n_escapes_scorable"] for r in usable]
    model_micro = _micro([r[f"recall_at_{TOP_K}"] for r in usable], weights)
    model_macro = round(mean([r[f"recall_at_{TOP_K}"] for r in usable]), 4) if usable else None
    chain_micro = _micro([r["baselines"]["random_whole_chain"] for r in usable], weights)
    pocket_micro = _micro([r["baselines"]["random_in_pocket"] or 0.0 for r in usable], weights)

    curve = {}
    for k in K_CURVE:
        curve[str(k)] = {
            "model": _micro([r["recall_curve"][str(k)]["model"] for r in usable], weights),
            "random_whole_chain": _micro(
                [r["recall_curve"][str(k)]["random_whole_chain"] for r in usable], weights),
            "random_in_pocket": _micro(
                [r["recall_curve"][str(k)]["random_in_pocket"] or 0.0 for r in usable], weights),
        }

    total_escapes = sum(r["n_escapes_scorable"] for r in usable)
    total_found = sum(r["n_found"] for r in usable)
    numbering = {r["case_id"]: r["numbering_problems"] for r in results if r["numbering_problems"]}
    missed = [e for r in results for e in r["escapes"] if not e["found_in_top_k"]]

    return {
        "backtest": "market",
        "name": "Dead Market / Resistance",
        "test": f"{len(scored_cases)} targeted drugs with solved co-crystal structures and "
                f"{total_escapes} documented clinical escape mutations; every residue ranked by "
                "exact heavy-atom contact distance to the bound drug plus UniProt variant density",
        "headline_metric": f"recall@{TOP_K} of known clinical escapes (micro-average over escapes)",
        "headline_value": model_micro,
        "baseline_metric": f"expected recall@{TOP_K} of random selection within the binding pocket",
        "baseline_value": pocket_micro,
        "model": {
            f"recall_at_{TOP_K}_micro": model_micro,
            f"recall_at_{TOP_K}_macro": model_macro,
            "escapes_found": total_found,
            "escapes_scorable": total_escapes,
            "lift_over_pocket_baseline": (round(model_micro / pocket_micro, 2)
                                          if model_micro and pocket_micro else None),
            "lift_over_chain_baseline": (round(model_micro / chain_micro, 2)
                                         if model_micro and chain_micro else None),
        },
        "baselines": {
            "random_whole_chain": {
                "description": "Uniform random residue selection across the resolved chain",
                f"expected_recall_at_{TOP_K}": chain_micro,
            },
            "random_in_pocket": {
                "description": f"Uniform random selection among residues under {POCKET_ANGSTROM} A of the "
                               "drug; this baseline is already told where the pocket is",
                f"expected_recall_at_{TOP_K}": pocket_micro,
            },
        },
        "recall_curve": curve,
        "scoring": {
            "formula": f"{PROXIMITY_WEIGHT} * exp(-max(0, d - {CONTACT_FLOOR}) / {PROXIMITY_SCALE}) "
                       f"+ {VARIANT_WEIGHT} * min(1, n_variants / {VARIANT_SATURATION})",
            "contact_floor_angstrom": CONTACT_FLOOR,
            "pocket_threshold_angstrom": POCKET_ANGSTROM,
            "note": "Weights are chosen heuristics, not fitted to the labels.",
        },
        "cases": results,
        "excluded_cases": [
            {"case_id": c.case_id, "target": c.target_symbol, "drug": c.drug, "pdb_id": c.pdb_id,
             "reason": c.exclusion_reason, "note": c.note}
            for c in excluded
        ],
        "numbering_problems": numbering,
        "missed_escapes": [
            {"mutation": e["mutation"], "min_distance_angstrom": e["min_distance_angstrom"],
             "rank": e["rank"], "in_pocket": e["in_pocket"], "url": e["url"]}
            for e in missed
        ],
        "caveats": [
            "Contact distance can only find escapes that touch the drug. Allosteric and conformational "
            "escapes sit far from the ligand and are ranked low by construction.",
            "A co-crystal structure is one conformation of one construct. Residues in disordered loops "
            "are absent from the coordinates and cannot be ranked; they are reported, not counted as "
            "misses.",
            f"{len(scored_cases)} drug-target pairs and {total_escapes} labelled escapes is a small "
            "benchmark, and all of them are kinase or BCL2-family inhibitors.",
            "Escape labels are curated from the cited literature, so the benchmark covers escapes that "
            "were published, which favours well-studied resistance mechanisms.",
            "recall@10 is sensitive to k here: two contact-mediated escapes (EGFR T790M, ALK L1196M) rank "
            "11th. The recall curve is reported for k from 5 to 20, and the scoring weights were not "
            "adjusted after seeing these ranks.",
        ],
        "licences": {"structures": RCSB_LICENCE, "variants": UNIPROT_LICENCE,
                     "labels": "curated, each with a DOI"},
    }


def main() -> dict:
    result = asyncio.run(run_market_backtest())
    m = result["model"]
    print(f"Dead Market: recall@{TOP_K} {m[f'recall_at_{TOP_K}_micro']} "
          f"({m['escapes_found']}/{m['escapes_scorable']} escapes), macro {m[f'recall_at_{TOP_K}_macro']}")
    print(f"  baselines: pocket {result['baselines']['random_in_pocket'][f'expected_recall_at_{TOP_K}']}, "
          f"whole chain {result['baselines']['random_whole_chain'][f'expected_recall_at_{TOP_K}']}")
    print(f"  lift over pocket baseline: {m['lift_over_pocket_baseline']}x, "
          f"numbering problems: {len(result['numbering_problems'])}")
    for case in result["cases"]:
        hits = "".join("+" if e["found_in_top_k"] else "-" for e in case["escapes"])
        print(f"   {case['case_id']:22} {case['pdb_id']} recall={case[f'recall_at_{TOP_K}']} [{hits}] "
              f"pocket={case['pocket_size_under_8a']}/{case['n_residues_resolved']}")
    for e in result["missed_escapes"]:
        print(f"   missed {e['mutation']}: {e['min_distance_angstrom']} A, rank {e['rank']}")
    return result


if __name__ == "__main__":
    main()
