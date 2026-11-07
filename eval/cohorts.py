"""Curated benchmark cohorts: the *labels* for the backtests.

What is curated here, and what is not:

- Curated: which programmes to test, their outcome (approved / failed), the
  `as_of` cutoff, and for the market benchmark which residues are known
  clinical escape mutations. These are the ground truth a benchmark must
  supply; they cannot be derived from the evidence under test without
  circularity.
- Not curated: every piece of evidence the checks score. That is pulled live
  from Open Targets, ChEMBL, ClinicalTrials.gov, RCSB PDB and UniProt.

Every identifier and outcome claim below is machine-verifiable, and
`tests/test_eval_cohorts.py` verifies them against the live APIs:
Ensembl and disease IDs resolve to the named target/disease in Open Targets,
ChEMBL `first_approval` years match `approval_year` and fall strictly after
`as_of`, and each `nct_id` exists in ClinicalTrials.gov with the claimed
status. IDs and years were checked on 2026-11-07.

Scope limits worth stating plainly:
- 20 programmes is a small cohort. Treat separation as indicative, not
  powered. The Mann-Whitney p-value is reported for exactly that reason.
- Cohort selection is retrospective and drawn from well-known programmes,
  so it over-represents drugs that were notable enough to remember. That is
  a selection bias in favour of clear-cut cases, not a random sample of the
  pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

Outcome = Literal["approved", "failed"]


# ---------------------------------------------------------------------------
# Backtest 1: historical target-disease programmes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Programme:
    programme_id: str
    target_symbol: str
    ensembl_id: str
    disease_name: str
    disease_id: str  # Open Targets disease ID (EFO_ or MONDO_)
    drug: str
    chembl_id: str
    outcome: Outcome
    as_of: date  # evidence cutoff: strictly before the outcome below
    outcome_detail: str
    approval_year: int | None = None  # ChEMBL first_approval, for approved programmes
    nct_id: str | None = None  # ClinicalTrials.gov record, for failed programmes
    nct_status: str | None = None  # status that record should report
    doi: str | None = None

    @property
    def is_failure(self) -> int:
        return 1 if self.outcome == "failed" else 0

    @property
    def citation(self) -> str:
        bits = []
        if self.nct_id:
            bits.append(f"ClinicalTrials.gov {self.nct_id}")
        if self.doi:
            bits.append(f"doi:{self.doi}")
        if self.approval_year:
            bits.append(f"ChEMBL {self.chembl_id} first_approval {self.approval_year}")
        return "; ".join(bits)

    @property
    def citation_url(self) -> str:
        if self.nct_id:
            return f"https://clinicaltrials.gov/study/{self.nct_id}"
        if self.doi:
            return f"https://doi.org/{self.doi}"
        return f"https://www.ebi.ac.uk/chembl/explore/compound/{self.chembl_id}"


APPROVED_PROGRAMMES: tuple[Programme, ...] = (
    Programme("egfr-nsclc", "EGFR", "ENSG00000146648", "non-small cell lung carcinoma", "MONDO_0005233",
              "osimertinib", "CHEMBL3353410", "approved", date(2014, 1, 1),
              "FDA approved 2015 for EGFR T790M-positive NSCLC", approval_year=2015),
    Programme("alk-nsclc", "ALK", "ENSG00000171094", "non-small cell lung carcinoma", "MONDO_0005233",
              "crizotinib", "CHEMBL601719", "approved", date(2010, 1, 1),
              "FDA approved 2011 for ALK-positive NSCLC", approval_year=2011),
    Programme("braf-melanoma", "BRAF", "ENSG00000157764", "melanoma", "MONDO_0005105",
              "vemurafenib", "CHEMBL1229517", "approved", date(2010, 1, 1),
              "FDA approved 2011 for BRAF V600E melanoma", approval_year=2011),
    Programme("pcsk9-hypercholesterolaemia", "PCSK9", "ENSG00000169174", "familial hypercholesterolemia",
              "MONDO_0005439", "evolocumab", "CHEMBL2364655", "approved", date(2013, 1, 1),
              "FDA approved 2015 for familial hypercholesterolaemia", approval_year=2015),
    Programme("cftr-cf", "CFTR", "ENSG00000001626", "cystic fibrosis", "MONDO_0009061",
              "ivacaftor", "CHEMBL2010601", "approved", date(2010, 1, 1),
              "FDA approved 2012 for CFTR G551D cystic fibrosis", approval_year=2012),
    Programme("btk-cll", "BTK", "ENSG00000010671", "B-cell chronic lymphocytic leukemia", "MONDO_0004948",
              "ibrutinib", "CHEMBL1873475", "approved", date(2012, 1, 1),
              "FDA approved 2013 (mantle cell lymphoma), 2014 for CLL", approval_year=2013),
    Programme("il17a-psoriasis", "IL17A", "ENSG00000112115", "psoriasis", "MONDO_0005083",
              "secukinumab", "CHEMBL1743068", "approved", date(2013, 1, 1),
              "FDA approved 2015 for moderate-to-severe plaque psoriasis", approval_year=2015),
    Programme("pdcd1-melanoma", "PDCD1", "ENSG00000188389", "melanoma", "MONDO_0005105",
              "pembrolizumab", "CHEMBL3137343", "approved", date(2013, 1, 1),
              "FDA approved 2014 for advanced melanoma", approval_year=2014),
    Programme("bcl2-cll", "BCL2", "ENSG00000171791", "B-cell chronic lymphocytic leukemia", "MONDO_0004948",
              "venetoclax", "CHEMBL3137309", "approved", date(2014, 1, 1),
              "FDA approved 2016 for 17p-deleted CLL", approval_year=2016),
    Programme("il6r-ra", "IL6R", "ENSG00000160712", "rheumatoid arthritis", "MONDO_0008383",
              "sarilumab", "CHEMBL2108730", "approved", date(2015, 1, 1),
              "FDA approved 2017 for moderate-to-severe rheumatoid arthritis", approval_year=2017),
)

FAILED_PROGRAMMES: tuple[Programme, ...] = (
    Programme("bace1-alzheimer", "BACE1", "ENSG00000186318", "Alzheimer disease", "MONDO_0004975",
              "verubecestat", "CHEMBL3301601", "failed", date(2016, 1, 1),
              "Phase 2/3 EPOCH stopped early for futility; no cognitive benefit",
              nct_id="NCT01739348", nct_status="TERMINATED", doi="10.1056/NEJMoa1706441"),
    Programme("app-alzheimer", "APP", "ENSG00000142192", "Alzheimer disease", "MONDO_0004975",
              "solanezumab", "CHEMBL1743072", "failed", date(2015, 1, 1),
              "Phase 3 EXPEDITION3 missed its primary cognitive endpoint",
              nct_id="NCT01900665", nct_status="TERMINATED", doi="10.1056/NEJMoa1705971"),
    Programme("pla2g7-cad", "PLA2G7", "ENSG00000146070", "coronary artery disorder", "MONDO_0005010",
              "darapladib", "CHEMBL204021", "failed", date(2012, 1, 1),
              "Phase 3 STABILITY (15,828 patients) showed no reduction in major coronary events",
              nct_id="NCT00799903", nct_status="COMPLETED", doi="10.1056/NEJMoa1315878"),
    Programme("cetp-cad", "CETP", "ENSG00000087237", "coronary artery disorder", "MONDO_0005010",
              "dalcetrapib", "CHEMBL313006", "failed", date(2010, 1, 1),
              "Phase 3 programme halted for futility after dal-OUTCOMES; companion trial terminated",
              nct_id="NCT01059682", nct_status="TERMINATED", doi="10.1056/NEJMoa1206797"),
    Programme("htr6-alzheimer", "HTR6", "ENSG00000158748", "Alzheimer disease", "MONDO_0004975",
              "idalopirdine", "CHEMBL3286580", "failed", date(2015, 1, 1),
              "Three Phase 3 trials completed without cognitive benefit over cholinesterase inhibitors",
              nct_id="NCT02006641", nct_status="COMPLETED", doi="10.1001/jama.2017.20373"),
    Programme("ager-alzheimer", "AGER", "ENSG00000204305", "Alzheimer disease", "MONDO_0004975",
              "azeliragon", "CHEMBL3989929", "failed", date(2016, 1, 1),
              "Phase 3 STEADFAST terminated after interim futility analysis",
              nct_id="NCT02080364", nct_status="TERMINATED"),
    Programme("il17a-crohn", "IL17A", "ENSG00000112115", "Crohn disease", "MONDO_0005011",
              "secukinumab", "CHEMBL1743068", "failed", date(2009, 1, 1),
              "Phase 2 terminated; IL-17A blockade was ineffective and worsened disease in some patients",
              nct_id="NCT00584740", nct_status="TERMINATED", doi="10.1136/gutjnl-2011-301668"),
    Programme("grm5-fragile-x", "GRM5", "ENSG00000168959", "fragile X syndrome", "MONDO_0010383",
              "mavoglurant", "CHEMBL3087515", "failed", date(2012, 1, 1),
              "Phase 2b in adults missed its primary behavioural endpoint",
              nct_id="NCT01253629", nct_status="COMPLETED", doi="10.1126/scitranslmed.aab4109"),
    Programme("mapk14-copd", "MAPK14", "ENSG00000112062", "chronic obstructive pulmonary disease",
              "MONDO_0005002", "losmapimod", "CHEMBL1088752", "failed", date(2014, 1, 1),
              "Phase 2 in frequent exacerbators completed without reducing exacerbations",
              nct_id="NCT02299375", nct_status="COMPLETED"),
    Programme("ptgdr2-asthma", "PTGDR2", "ENSG00000183134", "asthma", "MONDO_0004979",
              "fevipiprant", "CHEMBL3137332", "failed", date(2018, 1, 1),
              "Phase 3 LUSTER programme failed to reduce exacerbations meaningfully; development stopped",
              nct_id="NCT02555683", nct_status="COMPLETED", doi="10.1016/S2213-2600(20)30412-4"),
)

SCIENCE_COHORT: tuple[Programme, ...] = APPROVED_PROGRAMMES + FAILED_PROGRAMMES


# ---------------------------------------------------------------------------
# Backtest 3: structural resistance benchmark
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EscapeMutation:
    residue_number: int
    wild_type: str  # one-letter code, verified against the structure
    mutant: str
    doi: str

    @property
    def label(self) -> str:
        return f"{self.wild_type}{self.residue_number}{self.mutant}"


@dataclass(frozen=True)
class ResistanceCase:
    case_id: str
    target_symbol: str
    uniprot_id: str
    drug: str
    pdb_id: str
    ligand_code: str  # HET code of the bound drug in that structure
    chain: str
    escapes: tuple[EscapeMutation, ...]
    note: str = ""
    excluded_from_recall: bool = False
    exclusion_reason: str = ""
    residue_offset: int = 0  # UniProt number - PDB auth number, verified live

    @property
    def escape_residues(self) -> tuple[int, ...]:
        return tuple(e.residue_number for e in self.escapes)


RESISTANCE_BENCHMARK: tuple[ResistanceCase, ...] = (
    ResistanceCase(
        "egfr-osimertinib", "EGFR", "P00533", "osimertinib", "4ZAU", "YY3", "A",
        (EscapeMutation(797, "C", "S", "10.1038/nm.3854"),),
        note="C797S removes the cysteine osimertinib covalently binds",
    ),
    ResistanceCase(
        "egfr-gefitinib", "EGFR", "P00533", "gefitinib", "2ITY", "IRE", "A",
        (EscapeMutation(790, "T", "M", "10.1371/journal.pmed.0020073"),),
        note="T790M is the classic gatekeeper escape from first-generation EGFR inhibitors",
    ),
    ResistanceCase(
        "abl1-imatinib", "ABL1", "P00519", "imatinib", "2HYY", "STI", "A",
        (EscapeMutation(315, "T", "I", "10.1126/science.1062538"),
         EscapeMutation(255, "E", "K", "10.1016/S1535-6108(02)00096-X"),
         EscapeMutation(253, "Y", "H", "10.1016/S1535-6108(02)00096-X"),
         EscapeMutation(351, "M", "T", "10.1016/S1535-6108(02)00096-X")),
        note="ABL1 numbering in 2HYY follows the 1a isoform; offset verified live",
    ),
    ResistanceCase(
        "alk-crizotinib", "ALK", "Q9UM73", "crizotinib", "2XP2", "VGH", "A",
        (EscapeMutation(1196, "L", "M", "10.1056/NEJMoa1007478"),
         EscapeMutation(1156, "C", "Y", "10.1056/NEJMoa1007478"),
         EscapeMutation(1269, "G", "A", "10.1126/scitranslmed.3003316")),
    ),
    ResistanceCase(
        "bcl2-venetoclax", "BCL2", "P10415", "venetoclax", "6O0K", "LBM", "A",
        (EscapeMutation(101, "G", "V", "10.1158/2159-8290.CD-18-1119"),
         EscapeMutation(103, "D", "Y", "10.1182/blood.2019004205")),
    ),
    ResistanceCase(
        "btk-ibrutinib", "BTK", "Q06187", "ibrutinib", "5P9J", "8E8", "A",
        (EscapeMutation(481, "C", "S", "10.1056/NEJMoa1400029"),),
        note="C481S removes the cysteine ibrutinib covalently binds",
    ),
    ResistanceCase(
        "kit-imatinib", "KIT", "P10721", "imatinib", "1T46", "STI", "A",
        (EscapeMutation(670, "T", "I", "10.1200/JCO.2006.06.2265"),
         EscapeMutation(654, "V", "A", "10.1200/JCO.2006.06.2265")),
    ),
    ResistanceCase(
        "braf-vemurafenib", "BRAF", "P15056", "vemurafenib", "3OG7", "032", "A", (),
        note="Clinical vemurafenib resistance is driven by NRAS/MEK1 mutations, BRAF splice "
             "variants and amplification rather than by kinase-domain point mutations",
        excluded_from_recall=True,
        exclusion_reason="no established BRAF kinase-domain escape point mutations to recall",
    ),
)

SCORED_RESISTANCE_CASES = tuple(c for c in RESISTANCE_BENCHMARK if not c.excluded_from_recall)


# ---------------------------------------------------------------------------
# Stress test: unseen assets
# ---------------------------------------------------------------------------

STRESS_ASSETS: tuple[str, ...] = (
    "lecanemab",
    "sotorasib targeting KRAS in non-small cell lung carcinoma",
    "risdiplam",
    "tirzepatide for type 2 diabetes mellitus",
    "teclistamab",
    "elacestrant targeting ESR1 in breast carcinoma",
    "mitapivat",
    "aducanumab",
    "baricitinib for alopecia areata",
    "tofersen targeting SOD1 in amyotrophic lateral sclerosis",
)


# ---------------------------------------------------------------------------
# Licences of everything the eval touches
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DatasetLicence:
    dataset: str
    source: str
    licence: str
    cutoff_enforcement: str
    used_by: tuple[str, ...] = field(default_factory=tuple)


DATASET_LICENCES: tuple[DatasetLicence, ...] = (
    DatasetLicence(
        "Target-disease associations, datatype scores, tractability, genetic constraint, "
        "safety liabilities, DepMap essentiality",
        "Open Targets Platform GraphQL (api.platform.opentargets.org)", "CC0 1.0",
        "Current snapshot only; no historical API. Under as_of the science check uses only "
        "genetic_association, somatic_mutation and animal_model, and excludes clinical "
        "precedence, literature, constraint, safety and DepMap. Residual leakage is possible "
        "and is stated in the report.",
        ("science backtest", "science check", "resolver"),
    ),
    DatasetLicence(
        "Drug records, mechanisms of action, first approval year, bioactivity (pChEMBL)",
        "ChEMBL REST API (www.ebi.ac.uk/chembl)", "CC BY-SA 3.0",
        "Bioactivity filtered to document_year <= as_of.year. Approval year used only to "
        "assert the cutoff precedes approval, never as a score input under as_of.",
        ("science backtest", "science check", "resolver"),
    ),
    DatasetLicence(
        "Interventional trial protocol records and terminal statuses",
        "ClinicalTrials.gov API v2 (clinicaltrials.gov/api/v2)", "Public domain (US Government, NLM terms)",
        "Graveyard restricted to completion strictly before 2022-01-01; test cohort to starts on or "
        "after 2022-01-01. Outcome fields stripped before scoring and asserted absent in tests.",
        ("trial backtest",),
    ),
    DatasetLicence(
        "Experimental co-crystal structures and atomic coordinates",
        "RCSB PDB (files.rcsb.org)", "CC0 1.0",
        "Not time-sensitive; structures predate the resistance reports they are scored against.",
        ("market backtest",),
    ),
    DatasetLicence(
        "Protein sequence features and natural variants",
        "UniProtKB REST (rest.uniprot.org)", "CC BY 4.0",
        "Used for sequence validation and variant density, not for the escape labels.",
        ("market backtest",),
    ),
    DatasetLicence(
        "Phase transition success rates and genetic-support relative success",
        "Hay et al. 2014 (doi:10.1038/nbt.2786); BIO/Informa/QLS 2021; Nelson et al. 2015 "
        "(doi:10.1038/ng.3314); Minikel et al. 2024 (doi:10.1038/s41586-024-07316-0)",
        "Citation only (published figures quoted, not redistributed)",
        "Prior selected by publication date: only studies published on or before as_of are used.",
        ("science backtest", "science check"),
    ),
    DatasetLicence(
        "Programme outcomes and clinical escape mutations (benchmark labels)",
        "Curated in eval/cohorts.py from the cited trials and publications",
        "Curated by the Pre-Mortem team; each label carries a verifiable DOI or NCT ID",
        "Labels are outcomes, never scored as evidence. Cutoffs set strictly before each outcome "
        "and verified live against ChEMBL approval years and ClinicalTrials.gov statuses.",
        ("science backtest", "market backtest"),
    ),
)
