# BINDR
**Biomolecular Interactions & Drug Research**

BINDR automates pre-clinical and business development due-diligence for biotech investors and pharma M&A teams. In under 60 seconds, three specialized sub-agents stress-test any drug candidate across target biology, clinical trial execution, and 3D structural resistance to surface fatal failure modes before capital is committed.

## Quickstart

```bash
cp .env.example .env                         # insert GEMINI_API_KEY
.venv/bin/uvicorn api.main:app --reload --port 8000  # open http://localhost:8000
.venv/bin/pytest -v                          # run all 127 automated tests
```

## Architecture & Verification Loop

- **Target & Biology (`checks/science`)**: Open Targets, ChEMBL, and DepMap genetic constraint & safety liabilities.
- **Clinical Trial Execution (`checks/trial`)**: ClinicalTrials.gov API v2 historical termination & patient accrual attrition.
- **Drug Resistance & 3D Binding (`checks/market`)**: RCSB PDB & AlphaFold heavy-atom contact distances (Å) mapped to UniProt missense variants.

All risk scores, probabilities, and 3D contact distances are computed deterministically in Python—models never assert numbers. Every claim is verified against a source-linked Evidence Ledger, and diligence memos require formal human analyst sign-off before export. LLM reasoning uses the Gemini Interactions API (`gemini-4-argon` for synthesis, `gemini-3.8-flash` for entity resolution) with `thinking_level`.

## Evaluation & Data Licences

BINDR was validated on historical cohorts using strict temporal cutoffs (`as_of`): Target Biology achieved AUROC 0.790 ($p=0.028$ vs. 0.500 baseline), Clinical Trials beat prevalence on precision@20 (0.400 vs. 0.370), and Structural Resistance achieved recall@10 of 0.500 (beating 0.158 pocket baseline across all $k$). Across a 10-asset unseen stress test, 10/10 produced valid verdicts. Full backtest tables, statistical separation tests, and our voluntary failure case (Imatinib M351T allosteric escape) are documented in [eval/EVALUATION_REPORT.md](eval/EVALUATION_REPORT.md).

| Source | Dataset | Licence |
|---|---|---|
| Open Targets | Genetic associations, tractability, DepMap essentiality | CC0 1.0 |
| ChEMBL | Bioactivity, mechanisms of action, approval years | CC BY-SA 3.0 |
| DepMap | Cancer cell line dependency & CRISPR knockouts | CC BY 4.0 |
| ClinicalTrials.gov | Interventional protocol records & termination statuses | Public Domain |
| UniProtKB | Protein sequence variants & clinical phenotypes | CC BY 4.0 |
| RCSB PDB | Experimental 3D co-crystal atomic coordinates | CC0 1.0 |
| AlphaFold DB | In silico predicted 3D structures | CC BY 4.0 |

## Repository Layout

```
├── api/      # FastAPI + SSE endpoints (/resolve, /run, /signoff, /memo, /eval)
├── checks/   # 3 parallel specialist checks (science, trial, market)
├── core/     # Resolver, Evidence Ledger, orchestrator, sign-off gate, memo writer
├── eval/     # Backtests, statistical tests, 10-asset stress test, evaluation report
├── shared/   # Schemas, async disk cache, model configuration
├── tests/    # 127 automated unit and live integration tests
└── web/      # Single-page application (3Dmol.js protein viewer, voice input)
```
