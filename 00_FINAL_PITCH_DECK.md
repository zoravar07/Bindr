# BINDR — PITCH DECK
`AGENTVERSE 2026 · BIOTECH TRACK · RESEARCH & SYSTEM OVERVIEW`

---

## Slide 1: Title & Executive Summary
**BINDR — Biomolecular Interactions & Drug Research**
*Pre-Clinical & Business Development Due-Diligence Triage for Biopharma Investment Teams*

- **The Vision**: Transform 4–8 weeks of fragmented biopharma diligence into an adversarial 60-second triage.
- **The Core Thesis**: Most drugs fail for three independent reasons: false biological target, trial execution failure, or post-launch resistance mutations. Bindr surfaces which failure mode bites before capital is committed.
- **Event**: AgentVerse 2026 Hackathon (UCL) · Google DeepMind Track
- **Live Deployed App**: [https://bindr-coc2.onrender.com](https://bindr-coc2.onrender.com)
- **Repository**: [https://github.com/rishikeshtrivedi12-a11y/biopharma-hackers-f0f70554-submission-repo](https://github.com/rishikeshtrivedi12-a11y/biopharma-hackers-f0f70554-submission-repo)

---

## Slide 2: The Problem — The Biopharma Diligence Bottleneck
**90% of clinical-stage drug assets fail. Traditional diligence is manual, slow, and backward-looking.**

- **The Diligence Dilemma**: Pharma BD and Biotech VC investment teams evaluate hundreds of pre-clinical and Phase 1/2 assets each year. Diligence takes 4 to 8 weeks per asset across genetics, trials, and structural biology.
- **The Three Independent Failure Modes**:
  1. **Target & Biology Risk**: The biological target is not causally linked to disease in humans (fails Phase 2 for lack of efficacy).
  2. **Clinical Trial Execution Risk**: Trial terminates early due to patient accrual bottlenecks, futility, or narrow therapeutic window.
  3. **Drug Resistance Risk**: Active-site escape mutations emerge in patients post-launch, destroying drug-target affinity.
- **Why AI Tools Fail Today**: Generic LLMs hallucinate clinical statistics, assert numbers without evidence, and evaluate assets with post-hoc hindsight.

---

## Slide 3: The Solution — Bindr's 3 Specialist Sub-Agents
**An adversarial multi-agent triage pipeline executing in parallel in <60 seconds.**

- **Sub-Agent 1: Target & Biology (Lab Stage)**:
  - Live queries to **Open Targets Platform GraphQL**, **ChEMBL**, and **DepMap**.
  - Evaluates human genetic evidence, somatic mutation frequency, gene constraint (LOEUF), and target safety liabilities.
- **Sub-Agent 2: Clinical Trial Execution (Trial Stage)**:
  - Live queries to **ClinicalTrials.gov API v2** across direct trials and indication benchmarks.
  - NLP classifier on `whyStopped` rationales (Recruitment/Accrual, Futility, Safety, Commercial, Regulatory).
- **Sub-Agent 3: Drug Resistance & 3D Binding (Market Stage)**:
  - Analyzes solved **RCSB PDB** co-crystal structures or **AlphaFold DB** predictions.
  - Python contact-distance engine computes exact heavy-atom distances (Å) between ligand and protein, mapped to UniProt missense mutations.

---

## Slide 4: Google DeepMind Gemini Architecture
**Built on the Gemini Interactions API with deterministic mathematical verification.**

- **Dual-Model Specialization via Interactions API**:
  - `gemini-4-argon`: Deep multi-turn reasoning over the verified Evidence Ledger to synthesize executive red flags.
  - `gemini-3.8-flash`: Rapid entity parsing, identifier resolution, and sub-agent routing.
- **Reasoning Configuration**: Configured with `generation_config={"thinking_level": "high"}` for structured diligence synthesis. Zero deprecated parameters (`temperature`, `top_p`, `top_k`, `thinking_budget` banned by architectural tests).
- **Deterministic Python Math Gate**: Gemini *never* asserts numbers. All failure risk scores ($P(\text{failure}) = 1 - \prod(1 - s_i)$), 3D contact distances in Å, AUROC, and precision/recall statistics are computed in pure Python.
- **Evidence Ledger Citation Lock**: Every claim is an atomic `LedgerRow` citing an external URL, date pulled, and licence. Final verdicts require a **Human Analyst Sign-Off Gate** before diligence memo export.

---

## Slide 5: Live Interactive System & 3D Resistance Viewer
**Production-ready single-command web application (`http://localhost:8000`).**

- **Hands-Free Voice Query**: Web Speech API speech-to-text allowing voice input directly from analysts.
- **Interactive 3D Protein Structure Viewer (3Dmol.js)**:
  - Visualizes protein cartoon backbone, bound ligand in cyan sticks, and clinical escape residues.
  - **Color-Coded Risk Tiers**: $\le 2.5\text{ \AA}$ Direct Contact (Critical Red), $2.5–5.0\text{ \AA}$ Binding Pocket (High Risk), $5–8\text{ \AA}$ Pocket Rim (Moderate Risk).
  - Clickable escape residue table: clicking any mutation flies the 3D camera to zoom directly into that residue.
- **Human Analyst Sign-Off Gate**:
  - Review, accept, or reject AI-generated red flags with logged rationale.
  - Unlocks an executive **1-Page Diligence Memorandum** with copy markdown and PDF print support.

---

## Slide 6: Tested Against a Baseline, With No Hindsight (Empirical Proof)
**Every check evaluated on real historical cohorts with strict `as_of` temporal cutoffs.**

| Check Lane | Cohort / Ground Truth | Published / Random Baseline | Bindr Empirical Result | Statistical Significance |
|---|---|---|---|---|
| **Target Biology (Science)** | 20 historical programmes (10 approved vs. 10 failed Phase 2/3 for lack of efficacy) | AUROC 0.500 (Constant published Phase 2 $\to$ 3 base rate) | **AUROC 0.790** (Separation of means: +0.127) | $p = 0.028$ (Mann-Whitney $U = 79.0$, $n=20$) |
| **Trial Execution (Trial)** | 1,350 post-2022 test trials vs. 2,000 pre-2022 graveyard | Precision@20: 0.370 (Cohort prevalence baseline) | **Precision@20: 0.400** | Beats prevalence baseline on top-20 riskiest trials |
| **3D Resistance (Market)** | 7 approved kinase inhibitors, 14 documented clinical escape mutations | Recall@10: 0.158 (Random pocket) / 0.041 (Whole chain) | **Recall@10: 0.500** (Recovers 7/14 clinical escapes) | **3.17× enrichment** over pocket baseline; beats at all $k \in [5, 20]$ |
| **Unseen Stress Test** | 10 diverse unseen assets (oncology, rare, metabolic) | — | **10 / 10 Valid Verdicts** in 3.69s total wall clock | Median latency 0.66s; honest degradation on unscored assets |

---

## Slide 7: Voluntary Failure Case & Data Integrity
**Transparent scientific limitations and zero-data-leakage verification.**

- **Voluntary Failure Case ("Where We Get It Wrong")**:
  - **Imatinib resistance via M351T**: Residue M351 is located **8.5 Å** away from imatinib in ABL1 coordinates.
  - It confers clinical resistance through an allosteric conformational shift rather than direct steric hindrance.
  - Pure Euclidean contact distance methods miss allosteric resistance—Bindr documents this limitation openly rather than concealing it.
- **Zero Date Leakage**: Every record loaded into `EvidenceLedger(as_of=...)` asserts evidence dates strictly precede clinical outcomes.
- **Dataset Licences**: 7 public sources (Open Targets CC0 1.0, ChEMBL CC BY-SA 3.0, ClinicalTrials.gov Public Domain, RCSB PDB CC0 1.0, UniProtKB CC BY 4.0, DepMap CC BY 4.0).

---

## Slide 8: Market Opportunity & Business Model
**Targeting the high-margin biopharma intelligence and diligence sector.**

- **Target Audience**: 3,500+ Biotech Venture Capital firms, Pharma Corporate Business Development teams, and Life Science Hedge Funds.
- **Value Proposition**:
  - Compresses 4–8 weeks of manual pre-screening diligence into 60 seconds.
  - Prevents $50M–$100M Phase 2/3 clinical trial failures by highlighting hidden recruitment bottlenecks and active-site mutational escape routes.
- **Commercialization Model**:
  - **Tier 1 (SaaS)**: $4,500/seat/month for investment analysts (unlimited triage runs, 3D structure maps, exportable memos).
  - **Tier 2 (Enterprise API)**: Custom integration into proprietary pharma pipeline databases and internal compound screening libraries.

---

## Slide 9: Team & Summary
**Bindr — Know why it fails before you commit capital.**

- **Team**:
  - **Zoravar Singh (Zoro)** — Lead Integration, Core Pipeline, Science Check, Gemini 4 Argon Architecture
  - **Ayman** — Clinical Trial Check, ClinicalTrials.gov API v2, `whyStopped` NLP Classifier
  - **Rishi** — Market / Resistance Check, RCSB PDB & AlphaFold 3D Euclidean Contact Distance Engine
  - **Krystal** — Frontend Architecture, 3Dmol.js Viewer, Responsive UI
  - **Khaleem** — Backtests, Statistical Methodology, 10-Asset Stress Test, Licencing
- **Live Deployed Application**: [https://bindr-coc2.onrender.com](https://bindr-coc2.onrender.com)
- **Repository**: [https://github.com/rishikeshtrivedi12-a11y/biopharma-hackers-f0f70554-submission-repo](https://github.com/rishikeshtrivedi12-a11y/biopharma-hackers-f0f70554-submission-repo)
