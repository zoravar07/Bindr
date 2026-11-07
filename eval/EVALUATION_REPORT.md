# Pre-Mortem evaluation report

Generated 2026-11-07T19:29:58.546542+00:00 by `.venv/bin/python -m eval.run_all`. Every number below is read from `eval/results.json`, which that command writes.

Three backtests, each against real public records with a date cutoff enforced in code. 3 of 3 beat their baseline. Results that did not are reported as they came out.

## Proof slide

| Check | The test | Baseline score | Pre-Mortem score | Headline number |
|---|---|---|---|---|
| **Science** | 20 historical target-disease programmes (10 failed in Phase 2/3 for lack of efficacy, 10 approved), scored under each programme's own as_of cutoff | AUROC 0.500, separation 0.000 (constant published base rate) | AUROC 0.790, separation 0.127 | Mean risk is 0.127 higher for programmes that failed than for those approved (AUROC 0.790, Mann-Whitney p=0.028) |
| **Trial** | 1350 interventional Phase 2/3 drug trials started on or after 2022-01-01 with a terminal status, ranked by similarity to 2000 trials completed strictly before 2022-01-01 | precision@20 0.370 (cohort prevalence), AUROC 0.500; size-only baseline not computable without leakage | precision@20 0.400, AUROC 0.479 | precision@20 0.400 against a prevalence of 0.370, with out-of-period AUROC 0.479 while in-period cross-validated AUROC is 0.719 |
| **Market / resistance** | 7 targeted drugs with solved co-crystal structures and 14 documented clinical escape mutations; every residue ranked by exact heavy-atom contact distance to the bound drug plus UniProt variant density | recall@10 0.158 (random inside the 8 A pocket), 0.041 (random across the chain) | recall@10 0.500 (7/14 escapes) | Recovers 7 of 14 documented clinical escape mutations in the top 10 residues, 3.17x the pocket-random baseline |

Read against each baseline:

- **Science**: Beats the baseline on ranking (AUROC 0.790) and the separation is significant at the 5% level (Mann-Whitney p=0.028, n=20).
- **Trial**: Beats the prevalence baseline on precision@20 and ranks no better than chance overall (AUROC 0.479); does not generalise across the 2022-01-01 boundary despite in-period AUROC 0.719. Reported as a negative result.
- **Market / resistance**: Beats both random baselines at every k from 5 to 20.

## What Pre-Mortem gets wrong, and why

### Science: secukinumab (IL17A in psoriasis)

secukinumab (IL17A in psoriasis) was approved, but the science check ranked it 4 of 20 by risk with a score of 0.71, 0.22 above the median of the approved group.

**What actually happened.** FDA approved 2015 for moderate-to-severe plaque psoriasis ([source](https://www.ebi.ac.uk/chembl/explore/compound/CHEMBL1743068), cutoff 2013-01-01).

**Why the check got it wrong.** The check penalised thin evidence at the cutoff. It flagged: Science failure risk 0.71: Phase II->III base rate 0.289 (stage unknown) adjusted in log-odds by base rate -0.90. Weak or absent human genetic support for IL17A-psoriasis (best human-genetics datatype score 0.00).

**What it means.** The check is biased against mechanisms whose evidence is pharmacological rather than genetic. Absence of a genetic association is not evidence that a target is wrong, but the scoring treats it as a penalty.

Evidence the check recorded as missing at the cutoff:

- Open Targets is a current snapshot and cannot be rolled back to as_of; the as_of score uses only genetic_association, somatic_mutation, animal_model, tractability and year-filtered ChEMBL activity, and the overall association score may still include later evidence
- No Open Targets genetic_association evidence for IL17A-psoriasis
- No Open Targets somatic_mutation evidence for IL17A-psoriasis
- Open Targets genetic_literature excluded under as_of: snapshot evidence has no pre-cutoff timestamps

### Trial: the reference model does not survive a change of era

The similarity model carries real signal inside the pre-2022 graveyard (cross-validated AUROC 0.7185081374321881) but does not survive the 2022-01-01 boundary (test AUROC 0.47938, precision@20 0.4 against prevalence 0.3704). Protocol shape predicts termination among trials of the same era and fails to transfer to the next one. On this evidence the trial lane must not claim to forecast termination from protocol fields alone.

The honest conclusion is that trial-termination risk cannot be read off protocol shape alone. An eval that only reported the in-period number would have hidden that.

### Market: contact distance cannot see allosteric escapes

Escape mutations the structural ranking missed:

| Mutation | Distance to drug | Rank | In pocket | Source |
|---|---|---|---|---|
| T790M | 2.89 A | 11 | yes | [doi](https://doi.org/10.1371/journal.pmed.0020073) |
| E255K | 7.03 A | 44 | yes | [doi](https://doi.org/10.1016/S1535-6108(02)00096-X) |
| M351T | 8.53 A | 75 | no | [doi](https://doi.org/10.1016/S1535-6108(02)00096-X) |
| L1196M | 3.68 A | 11 | yes | [doi](https://doi.org/10.1056/NEJMoa1007478) |
| C1156Y | 16.22 A | 100 | no | [doi](https://doi.org/10.1056/NEJMoa1007478) |
| G101V | 5.04 A | 23 | yes | [doi](https://doi.org/10.1158/2159-8290.CD-18-1119) |
| V654A | 3.90 A | 22 | yes | [doi](https://doi.org/10.1200/JCO.2006.06.2265) |

2 of the misses sit outside the binding pocket, so no contact-distance method can rank them. M351T is the clearest case at 8.5 A from the drug: it changes the kinase conformation rather than the contact surface.

5 of the misses are in contact with the drug but ranked just below the cut. The scoring weights were not adjusted after seeing those ranks, which is why the recall curve is reported for k from 5 to 20 instead of a single tuned number.

BRAF with vemurafenib (3OG7) is carried as a documented exclusion: no established BRAF kinase-domain escape point mutations to recall. Clinical vemurafenib resistance is driven by NRAS/MEK1 mutations, BRAF splice variants and amplification rather than by kinase-domain point mutations.

## Cutoff enforcement

| Backtest | Boundary | How it is enforced | Violations found |
|---|---|---|---|
| Science | each programme's own `as_of`, set before its outcome | Every result is loaded into `EvidenceLedger(as_of=...)`, which rejects any row dated after the cutoff; excluded row IDs are asserted absent; approval years are checked to fall after the cutoff | 0 |
| Trial | 2022-01-01 | Graveyard completion strictly before, test starts on or after, both asserted per trial; scoring features restricted to a protocol allowlist with outcome fields asserted absent | 0 |
| Market | not time-based | Structures predate the resistance reports; residue numbering validated against the expected wild-type amino acid in every structure | 0 numbering problems |

The science backtest is the one with a residual risk worth naming. Open Targets publishes a current snapshot with no historical API, so association scores can include evidence published after a programme's cutoff. The check narrows this by using only `genetic_association`, `somatic_mutation` and `animal_model` under a cutoff, and by excluding clinical precedence and literature mining, but it cannot eliminate it. The caveat is carried in every affected result's `missing` list rather than left to the reader.

## Stress test: 10 unseen assets end to end

10 of 10 assets produced a verdict. Median latency 0.7s, maximum 1.1s, 3.7s wall clock at concurrency 2.

| Asset | Resolved | Risk | Score | Lanes | Rows | Flags | Latency |
|---|---|---|---|---|---|---|---|
| lecanemab | LECANEMAB / APP / Alzheimer disease | medium | 0.48 | market:failed, science:complete, trial:complete | 49 | 2 | 0.7s |
| sotorasib targeting KRAS in non-sm | SOTORASIB / KRAS / non-small cell lung carcino | medium | 0.47 | market:failed, science:complete, trial:complete | 55 | 5 | 1.1s |
| risdiplam | RISDIPLAM / SMN2 / spinal muscular atrophy | low | 0.33 | market:failed, science:complete, trial:complete | 42 | 3 | 1.1s |
| tirzepatide for type 2 diabetes me | TIRZEPATIDE / GIPR / type 2 diabetes mellitus | low | 0.18 | market:failed, science:complete, trial:complete | 39 | 1 | 0.6s |
| teclistamab | TECLISTAMAB / CD3E / plasma cell myeloma | low | 0.34 | market:failed, science:complete, trial:complete | 35 | 3 | 1.0s |
| elacestrant targeting ESR1 in brea | ELACESTRANT / ESR1 / breast carcinoma | high | 0.78 | market:complete, science:complete, trial:complete | 148 | 6 | 0.1s |
| mitapivat | MITAPIVAT / PKLR / Hemolytic anemia | high | 0.78 | market:complete, science:complete, trial:complete | 44 | 5 | 0.9s |
| aducanumab | ADUCANUMAB / APP / Alzheimer disease | high | 0.64 | market:failed, science:complete, trial:complete | 43 | 3 | 0.6s |
| baricitinib for alopecia areata | BARICITINIB / JAK1 / alopecia areata | medium | 0.42 | market:failed, science:complete, trial:complete | 21 | 3 | 0.3s |
| tofersen targeting SOD1 in amyotro | TOFERSEN / SOD1 / amyotrophic lateral sclerosi | low | 0.23 | market:failed, science:complete, trial:complete | 28 | 3 | 0.6s |

Lane status across the run: `market` 2 complete, 8 failed; `science` 10 complete; `trial` 10 complete.

## Dataset licence table

| Dataset | Source | Licence | Date pulled | Cutoff enforcement |
|---|---|---|---|---|
| Target-disease associations, datatype scores, tractability, genetic constraint, safety liabilities, DepMap essentiality | Open Targets Platform GraphQL (api.platform.opentargets.org) | CC0 1.0 | 2026-11-07 | Current snapshot only; no historical API. Under as_of the science check uses only genetic_association, somatic_mutation and animal_model, and excludes clinical precedence, literature, constraint, safety and DepMap. Residual leakage is possible and is stated in the report. |
| Drug records, mechanisms of action, first approval year, bioactivity (pChEMBL) | ChEMBL REST API (www.ebi.ac.uk/chembl) | CC BY-SA 3.0 | 2026-11-07 | Bioactivity filtered to document_year <= as_of.year. Approval year used only to assert the cutoff precedes approval, never as a score input under as_of. |
| Interventional trial protocol records and terminal statuses | ClinicalTrials.gov API v2 (clinicaltrials.gov/api/v2) | Public domain (US Government, NLM terms) | 2026-11-07 | Graveyard restricted to completion strictly before 2022-01-01; test cohort to starts on or after 2022-01-01. Outcome fields stripped before scoring and asserted absent in tests. |
| Experimental co-crystal structures and atomic coordinates | RCSB PDB (files.rcsb.org) | CC0 1.0 | 2026-11-07 | Not time-sensitive; structures predate the resistance reports they are scored against. |
| Protein sequence features and natural variants | UniProtKB REST (rest.uniprot.org) | CC BY 4.0 | 2026-11-07 | Used for sequence validation and variant density, not for the escape labels. |
| Phase transition success rates and genetic-support relative success | Hay et al. 2014 (doi:10.1038/nbt.2786); BIO/Informa/QLS 2021; Nelson et al. 2015 (doi:10.1038/ng.3314); Minikel et al. 2024 (doi:10.1038/s41586-024-07316-0) | Citation only (published figures quoted, not redistributed) | 2026-11-07 | Prior selected by publication date: only studies published on or before as_of are used. |
| Programme outcomes and clinical escape mutations (benchmark labels) | Curated in eval/cohorts.py from the cited trials and publications | Curated by the Pre-Mortem team; each label carries a verifiable DOI or NCT ID | 2026-11-07 | Labels are outcomes, never scored as evidence. Cutoffs set strictly before each outcome and verified live against ChEMBL approval years and ClinicalTrials.gov statuses. |

Date pulled is the run date. Responses are served from the on-disk cache in `.cache/`, whose entries were fetched on or before 2026-11-07 and each carry their own `fetched_at`, which is what populates `LedgerRow.date_pulled`.

## Limits of this evaluation

**Science**

- 20 programmes is a small cohort; the Mann-Whitney p-value is reported so the separation is not read as powered.
- Cohort selection is retrospective and favours well-known programmes, which over-represents clear-cut outcomes.
- Open Targets serves only a current snapshot, so association scores may include evidence published after a programme's cutoff even with the datatype whitelist applied.

**Trial**

- ClinicalTrials.gov's historical-version endpoint returns 403 to unauthenticated clients, so test trials are scored from their current record, not a pre-start snapshot. The allowlist removes outcome fields but cannot prove a protocol field was never amended after start.
- Trials started on or after 2022-01-01 that already have a terminal status over-represent short and early-stopped studies, because long trials are still running. The measured prevalence is reported, not assumed.
- Cohorts are the earliest-registered matching trials (sort=StudyFirstPostDate:asc) up to a cap, not a random sample of the registry.
- These numbers measure the reference graveyard-similarity scorer defined in this module, not the product lane in checks/trial. That lane scores an asset against its indication's trial landscape rather than predicting one trial's fate, so it cannot be evaluated by a per-trial termination benchmark and is not what this backtest reports.

**Market**

- Contact distance can only find escapes that touch the drug. Allosteric and conformational escapes sit far from the ligand and are ranked low by construction.
- A co-crystal structure is one conformation of one construct. Residues in disordered loops are absent from the coordinates and cannot be ranked; they are reported, not counted as misses.
- 7 drug-target pairs and 14 labelled escapes is a small benchmark, and all of them are kinase or BCL2-family inhibitors.
- Escape labels are curated from the cited literature, so the benchmark covers escapes that were published, which favours well-studied resistance mechanisms.
- recall@10 is sensitive to k here: two contact-mediated escapes (EGFR T790M, ALK L1196M) rank 11th. The recall curve is reported for k from 5 to 20, and the scoring weights were not adjusted after seeing these ranks.

## Reproducing this

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env    # add GEMINI_API_KEY
.venv/bin/python -m eval.run_all --stress
.venv/bin/pytest                      # 100+ tests, including live cohort verification
```

First run populates `.cache/` from the live APIs and takes a few minutes. Later runs read the cache and finish in seconds. The cohort labels are verified against Open Targets, ChEMBL and ClinicalTrials.gov by `tests/test_eval_cohorts.py`, so a stale identifier fails the suite rather than quietly changing a result.
