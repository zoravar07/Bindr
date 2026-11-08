# BINDR — Biomolecular Interactions & Drug Research

> **AgentVerse 2026 Biotech Track · Best Use of Google DeepMind Gemini Prize**  
> *Pre-clinical & business development due-diligence triage for biopharma investment teams.*

---

## 1. The Problem & Named User

In biopharmaceutical business development (BD), biotech venture capital, and pharma M&A, investment teams evaluate hundreds of pre-clinical and clinical-stage drug assets annually. Traditional diligence takes **4 to 8 weeks** per asset, relying on fragmented searches across genetics databases, clinical trial registries, and structural biology literature.

Crucially, drug investments fail for three independent reasons:
1. **Target Biology**: The biological target is not causally linked to the disease in humans.
2. **Clinical Trial Execution**: The trial protocol is flawed, or patient enrollment fails.
3. **Acquired Resistance**: Tumors rapidly develop active-site escape mutations post-launch.

**Bindr** automates this triage in seconds. Rather than predicting generic success, Bindr conducts an adversarial triage across all three failure modes using verified live scientific data, computes all metrics deterministically in Python, and synthesizes an executive memorandum using Gemini 4 Argon that requires human analyst sign-off before capital deployment.

---

## 2. Three Parallel Specialist Sub-Agents

Bindr orchestrates three specialized check lanes concurrently with a **45-second hard timeout** per lane and honest degradation:

```
                          ┌─────────────────────────────┐
                          │     User Query / Asset      │
                          │   (Text or Voice Input)     │
                          └──────────────┬──────────────┘
                                         │
                                         ▼
                          ┌─────────────────────────────┐
                          │   Deterministic Resolver    │
                          │   ChEMBL, UniProt, EFO      │
                          └──────────────┬──────────────┘
                                         │
               ┌─────────────────────────┼─────────────────────────┐
               ▼                         ▼                         ▼
    ┌──────────────────────┐  ┌──────────────────────┐  ┌──────────────────────┐
    │     Sub-Agent 1      │  │     Sub-Agent 2      │  │     Sub-Agent 3      │
    │  Target & Biology    │  │   Trial Execution    │  │  Resistance & Pocket │
    │  Open Targets, DepMap│  │  ClinicalTrials.gov  │  │  PDB, UniProt, 3Dmol │
    └──────────┬───────────┘  └──────────┬───────────┘  └──────────┬───────────┘
               │                         │                         │
               └─────────────────────────┼─────────────────────────┘
                                         │
                                         ▼
                          ┌─────────────────────────────┐
                          │       Evidence Ledger       │
                          │  Every claim has URL & ID   │
                          └──────────────┬──────────────┘
                                         │
                                         ▼
                          ┌─────────────────────────────┐
                          │     Gemini 4 Argon LLM      │
                          │  Interactions API + Reasoning│
                          └──────────────┬──────────────┘
                                         │
                                         ▼
                          ┌─────────────────────────────┐
                          │   Human Analyst Sign-Off    │
                          │  Review, Accept, Reject     │
                          └──────────────┬──────────────┘
                                         │
                                         ▼
                          ┌─────────────────────────────┐
                          │  1-Page Diligence Memo (PDF)│
                          └─────────────────────────────┘
```

1. **Target & Biology Risk (`checks/science`)**:
   - Queries Open Targets Platform GraphQL, ChEMBL bioactivity ($p\text{ChEMBL}$), and DepMap CRISPR knockout essentiality.
   - Evaluates human genetic evidence, constraint ($\text{LOEUF}$), and recorded target safety liabilities.
2. **Clinical Trial Execution Risk (`checks/trial`)**:
   - Queries ClinicalTrials.gov API v2 across both direct asset studies and disease-level indication benchmarks.
   - Parses terminal study statuses and applies an NLP regex classifier to categorize `whyStopped` rationales (Accrual/Recruitment, Efficacy/Futility, Safety/Toxicity, Commercial/Strategic, Operational/Regulatory).
3. **Drug Resistance & 3D Binding (`checks/market`)**:
   - Downloads experimental 3D X-ray co-crystal coordinates (RCSB PDB) or AlphaFold DB predictions via `gemmi`.
   - Computes heavy-atom Euclidean contact distances in $\text{Å}$ between drug ligand and protein residues in Python.
   - Maps clinical missense variants from UniProtKB to identify active-site escape mutations ($\le 2.5\text{ \AA}$ direct contact, $2.5 - 5.0\text{ \AA}$ binding pocket, $5.0 - 8.0\text{ \AA}$ pocket rim).

---

## 3. Google DeepMind Gemini Architecture

Bindr enforces strict DeepMind engineering standards:

- **Interactions API with Multi-Turn State**: Uses `google-genai >= 2.3.0` Interactions API (`client.interactions.create` passing `previous_interaction_id`) with models configured from `shared/config.py`:
  - `gemini-4-argon`: Deep reasoning over the Evidence Ledger for diligence synthesis.
  - `gemini-3.8-flash`: Rapid entity parsing and sub-agent routing.
- **Thinking Configuration**: Reasoning depth is set via `generation_config={"thinking_level": "low" | "medium" | "high"}`. Deprecated parameters (`temperature`, `top_p`, `top_k`, `thinking_budget`) are strictly forbidden by architectural tests.
- **Python Owns All Numbers**: Gemini never invents or asserts numerical values. All risk probabilities ($P(\text{failure}) = 1 - \prod(1 - s_i)$), contact distances ($\text{\AA}$), AUROC, and precision/recall metrics are computed in pure Python math.
- **Evidence Ledger Citation Lock**: Every claim is an atomic `LedgerRow` with an immutable ID, URL, source record, pull date, and licence. Gemini is constrained to cite ONLY verified IDs present in the ledger.
- **Human Analyst Sign-Off Gate**: Diligence verdicts remain in `DRAFT` status until a human investment analyst formally reviews each red flag, logs any overrides with required rationale, and applies their signature.

---

## 4. Empirical Evaluation & Validation

All backtest metrics and stress tests are reproducible via `.venv/bin/python -m eval.run_all --stress --no-llm`:

### Backtests vs. Published Baselines (Strict `as_of` Cutoffs)

| Check Lane | Cohort / Task | Published Baseline | Bindr Empirical Result | Statistical Significance |
|---|---|---|---|---|
| **Science (Target Biology)** | 20 historical approved/failed programmes | AUROC 0.500 (Random baseline) | **AUROC 0.790** (Separation 0.127) | $p = 0.028$ (Mann-Whitney $U$, $n=20$) |
| **Trial (Clinical Execution)** | Pre-2022 graveyard vs. Post-2022 test cohort | Precision@20 0.370 (Cohort prevalence) | **Precision@20 0.400** | Beats prevalence baseline on top-ranked risky trials |
| **Market (3D Resistance)** | Solved PDB co-crystal structures & known escapes | Recall@10 0.158 (Random pocket) / 0.040 (Whole chain) | **Recall@10 0.500** (7/14 known clinical escapes) | Beats random baselines at every $k \in [5, 20]$ |

### Saturday Night 10-Asset Unseen Stress Test
- **10 of 10 unseen assets** produced a valid, structured verdict (median latency 0.7s).
- Demonstrates honest degradation: small-molecule oncology assets with solved co-crystals (`elacestrant`, `mitapivat`) complete all 3 lanes, while biologics and unliganded peptides degrade cleanly to `Not Scored` without crashing the pipeline.

### Voluntary Failure Case ("Where We Get It Wrong")
We openly report a failure case: **Imatinib resistance via M351T**. Residue M351 is located **8.5 Å** away from the bound imatinib molecule in ABL1 coordinates. It confers clinical resistance through an allosteric conformational shift rather than direct steric hindrance. Pure Euclidean contact distance methods miss allosteric resistance, and Bindr documents this limitation transparently.

### Dataset Licences & Cutoff Enforcement

| Dataset / Source | Terms & Licence | Primary Purpose | Cutoff Enforcement |
|---|---|---|---|
| **Open Targets Platform** | Creative Commons CC0 1.0 | Target-disease genetic associations, tractability, DepMap | Strict `as_of` publication filtering |
| **ChEMBL Database (EMBL-EBI)** | Creative Commons CC BY-SA 3.0 | Drug bioactivity, mechanisms of action, approval years | Bioactivities recorded strictly on or before `as_of` |
| **ClinicalTrials.gov (NLM)** | U.S. Public Domain | Interventional trial protocols and termination statuses | Studies starting after `as_of` excluded; outcomes masked |
| **RCSB Protein Data Bank (PDB)** | Creative Commons CC0 1.0 | High-resolution 3D atomic coordinates | Structure deposition date audited against `as_of` |
| **UniProtKB / Swiss-Prot** | Creative Commons CC BY 4.0 | Natural variants and phenotypes | Variant curation era validated |

---

## 5. Quickstart & Verification

### Prerequisites
- Python 3.10+ (tested on Python 3.11)
- Modern web browser (Chrome, Safari, Edge, Firefox)

### Setup & Launch

```bash
# 1. Clone repository
git clone https://github.com/rishikeshtrivedi12-a11y/biopharma-hackers-f0f70554-submission-repo.git
cd biopharma-hackers-f0f70554-submission-repo

# 2. Activate virtual environment and install dependencies
source .venv/bin/activate
pip install -r requirements.txt

# 3. Configure environment (optional: works offline/cached with NO_LLM)
cp .env.example .env
# Edit .env and insert your GEMINI_API_KEY

# 4. Launch single-command server (serves both API and Web UI on port 8000)
uvicorn api.main:app --reload --port 8000
```

Open **`http://localhost:8000`** in your browser.

### Running Test Suite

```bash
# Run all 127 automated unit and integration tests
pytest -v
```

---

## 6. Security & Credential Audit

- Repository audited: **0 API keys, secrets, or bearer tokens exist in source code or git history**.
- Network calls are cached locally via `shared/cache.py` (`.cache/` is git-ignored).
- All patient-identifying data is excluded by design; Bindr operates strictly on pre-clinical and anonymized registry data.
