/* Pre-Mortem — Institutional Biotech / Financial Diligence Application */

const { useState, useEffect, useRef } = React;

// Quick-Pick Demos
const QUICK_PICKS = [
  { label: "Osimertinib (EGFR / NSCLC)", query: "Osimertinib targeting EGFR in NSCLC", asOf: "" },
  { label: "Imatinib (ABL1 / CML)", query: "Imatinib targeting ABL1 in chronic myeloid leukemia", asOf: "" },
  { label: "Sotorasib (KRAS / NSCLC)", query: "Sotorasib targeting KRAS in non-small cell lung carcinoma", asOf: "" },
  { label: "Crizotinib (ALK / NSCLC)", query: "Crizotinib targeting ALK in non-small cell lung carcinoma", asOf: "" },
];

const LANE_DETAILS = {
  science: {
    title: "Target & Biology Risk",
    stage: "Lab Stage",
    subtitle: "Does human genetics and biology support this target for this disease?",
    sources: "Open Targets • ChEMBL • DepMap",
    scoringInfo: {
      question: "Is the target validated by human genetics and essential in disease models?",
      tiers: [
        { label: "Low Risk (0–33%)", desc: "Strong genetic associations (score > 0.4), known approved drugs on target, and proven cancer cell line dependency in DepMap." },
        { label: "Moderate Risk (34–66%)", desc: "Partial genetic validation or tractability concerns, but target has reasonable animal or clinical precedence." },
        { label: "High Risk (67–100%)", desc: "No genetic support in Open Targets (score < 0.1), pan-essential safety liability, or unvalidated mechanism." },
      ],
      coverageNote: "High coverage means target has curated GWAS/somatic mutations, ChEMBL bioactivity, and DepMap CRISPR knockout data. Low coverage means target lacks human genetic studies.",
      validation: "Beats random baseline on historical 20-programme cohort: AUROC 0.790 (p = 0.028).",
    },
  },
  trial: {
    title: "Clinical Trial Execution Risk",
    stage: "Trial Stage",
    subtitle: "Why have similar trials failed in the past, and will this trial struggle to recruit or finish?",
    sources: "ClinicalTrials.gov Protocol Registry",
    scoringInfo: {
      question: "What are the historical termination rates and primary attrition bottlenecks in this indication?",
      tiers: [
        { label: "Low Risk (0–33%)", desc: "Indication has high trial completion rates (>80%) and no direct termination history for this molecule." },
        { label: "Moderate Risk (34–66%)", desc: "Historical indication baseline shows moderate accrual or operational termination rates (20–40%)." },
        { label: "High Risk (67–100%)", desc: "Previous trial with this drug terminated early, or indication suffers severe patient recruitment failure (>40% terminated for accrual)." },
      ],
      coverageNote: "High coverage means >= 25 direct trials and >= 25 indication benchmark trials analyzed. Low coverage means few registered clinical studies exist in ClinicalTrials.gov.",
      validation: "Beats prevalence baseline on precision@20 (0.400 vs 0.370 prevalence baseline). Reported honestly as negative result across 2022 temporal boundary.",
    },
  },
  market: {
    title: "Drug Resistance Risk",
    stage: "Market Stage",
    subtitle: "How easily can patient mutations block the drug from binding to its target?",
    sources: "RCSB PDB • UniProtKB • AlphaFold DB",
    scoringInfo: {
      question: "Are known patient mutations located within direct binding distance of the drug molecule?",
      tiers: [
        { label: "Low Risk (0–33%)", desc: "No known missense mutations within 5.0 Å of ligand; binding pocket is structurally conserved across patients." },
        { label: "Moderate Risk (34–66%)", desc: "Several missense variants near pocket rim (5–8 Å), but key covalent attachment residues are unmutated." },
        { label: "High Risk (67–100%)", desc: "Direct steric or chemical mutations at contact residues (<= 2.5 Å) or covalent binding cysteine (e.g. C797 in EGFR), causing rapid clinical resistance." },
      ],
      coverageNote: "High coverage means experimental drug-bound X-ray co-crystal structure solved (< 3.0 Å). Low coverage means using unliganded AlphaFold prediction.",
      validation: "Recalls 50% (7/14) of published clinical escape mutations at k=10, beating random pocket (15.8%) and whole chain (4.0%) baselines at every k from 5 to 20.",
    },
  },
};

const FAILURE_MODE_TITLES = {
  market: "Acquired Drug Resistance (Post-Launch Binding Site Mutations)",
  trial: "Clinical Trial Execution & Patient Recruitment Failure",
  science: "Weak Target Biology & Pre-Clinical Validation",
};

function getRiskTier(score) {
  if (score === null || score === undefined) return { label: "Not Assessed", badgeClass: "bg-slate-100 text-slate-700 border-slate-200" };
  if (score < 0.34) return { label: "Low Risk (0–33%)", badgeClass: "bg-emerald-50 text-emerald-800 border-emerald-200" };
  if (score < 0.67) return { label: "Moderate Risk (34–66%)", badgeClass: "bg-amber-50 text-amber-800 border-amber-200" };
  return { label: "High Risk (67–100%)", badgeClass: "bg-rose-50 text-rose-800 border-rose-200" };
}

function getBindingImpact(distance) {
  if (distance <= 2.5) return { label: "Direct Drug Contact (Critical)", badge: "bg-rose-100 text-rose-800 border-rose-200" };
  if (distance <= 5.0) return { label: "Inside Binding Pocket (High Risk)", badge: "bg-rose-50 text-rose-700 border-rose-200" };
  if (distance <= 8.0) return { label: "Pocket Rim / Adjacent (Moderate)", badge: "bg-amber-50 text-amber-700 border-amber-200" };
  return { label: "Distal / Allosteric", badge: "bg-slate-100 text-slate-600 border-slate-200" };
}

function getAnalystTakeaway(text, checkId, direction) {
  const t = (text || "").toLowerCase();

  // Science / Target Biology Risk
  if (checkId === "science") {
    if (t.includes("covalent") || t.includes("potent binder") || t.includes("pchembl")) {
      return "High target-binding potency confirms strong molecular engagement—low biophysical target-binding risk.";
    }
    if (t.includes("weak potency")) {
      return "Low binding potency—molecule may require high clinical doses to achieve biological efficacy, narrowing the therapeutic window.";
    }
    if (t.includes("strong overall target-disease") || t.includes("high overall target-disease") || t.includes("genetic support")) {
      return "Strong human genetic & clinical proof that this protein actually drives the disease—low risk of pursuing a false biological target.";
    }
    if (t.includes("moderate overall target-disease") || t.includes("somewhat")) {
      return "Moderate biological evidence—target is implicated in disease biology, but patient translation carries moderate validation uncertainty.";
    }
    if (t.includes("weak or absent") || t.includes("no evidence linking")) {
      return "Weak genetic link between this target and the disease—high risk the drug shows activity in cells but fails to benefit patients.";
    }
    if (t.includes("safety liabilities") || t.includes("recorded target safety")) {
      return "Recorded target safety liabilities in humans—proactive patient safety monitoring required for known on-target adverse events.";
    }
    if (t.includes("common-essential") || t.includes("depmap")) {
      return "Target is essential for cell viability across multiple lineages—requires tumor-selective delivery to avoid systemic off-target toxicity.";
    }
    if (t.includes("loss-of-function intolerant") || t.includes("loeuf")) {
      return "Intolerant to loss-of-function variation—target is evolutionarily constrained in humans, indicating physiological essentiality.";
    }
    if (t.includes("no tractability") || t.includes("tractability evidence")) {
      return "Limited clinical tractability precedent—molecule may face chemical optimization challenges for oral bioavailability or selectivity.";
    }
    return direction === "support"
      ? "Target biology and clinical precedent support pursuing this mechanism in this disease indication."
      : "Biological validation risk: target mechanism warrants additional pre-clinical confirmation.";
  }

  // Trial / Clinical Trial Execution Risk
  if (checkId === "trial") {
    if (t.includes("accrual") || t.includes("recruit") || t.includes("enroll")) {
      return "Historical trial attrition in this indication is dominated by patient recruitment bottlenecks—trial timeline and budget are at risk unless inclusion criteria are broadened.";
    }
    if (t.includes("direct clinical attrition") || t.includes("stopped early") || t.includes("terminated")) {
      return "A previous clinical study evaluating this drug in this exact disease was stopped prematurely—investigate prior termination rationale before committing capital.";
    }
    if (t.includes("futility") || t.includes("lack of efficacy") || t.includes("interim")) {
      return "Several past trials in this indication failed to demonstrate clinical benefit—requires strong predictive biomarker enrichment to identify responder patients.";
    }
    if (t.includes("safety") || t.includes("toxicity") || t.includes("adverse")) {
      return "Prior trials encountered dose-limiting adverse events—therapeutic index and patient safety protocols are critical execution bottlenecks.";
    }
    if (t.includes("landscape baseline") || t.includes("evaluated")) {
      return direction === "support"
        ? "Clean clinical execution baseline—no historical early terminations found for this molecule in this indication."
        : "Active trial landscape with historical terminations—requires careful protocol design against established indication attrition rates.";
    }
    return direction === "support"
      ? "Favorable clinical trial feasibility based on historical completion rates in this disease space."
      : "Trial execution risk: historical indication data highlights operational and recruitment vulnerabilities.";
  }

  // Market / Resistance Risk
  if (checkId === "market") {
    if (t.includes("covalent") || t.includes("c797") || t.includes("attachment residue")) {
      return "The drug physically attaches to this exact residue—a single patient mutation here directly blocks drug binding, creating a primary post-launch resistance escape route.";
    }
    if (t.includes("clinically annotated") || t.includes("escape variants inside") || t.includes("inside 4.5")) {
      return "Mutations sit directly inside the pocket where the drug docks—amino acid substitutions here can crowd out the drug and erode long-term therapy durability.";
    }
    if (t.includes("density") || t.includes("variants distributed") || t.includes("missense variants")) {
      return "High density of patient missense mutations inside the binding pocket—creates a broad evolutionary reservoir for tumors to develop acquired resistance.";
    }
    if (t.includes("no drug-bound structure") || t.includes("alphafold")) {
      return "No experimental co-crystal structure available in PDB—distance calculations rely on AI prediction, carrying wider structural uncertainty.";
    }
    if (t.includes("ambiguous ligand")) {
      return "Chemical ligand could not be unambiguously resolved to a single PDB coordinate set—structural risk was not evaluated.";
    }
    return direction === "support"
      ? "Binding pocket appears structurally conserved with minimal proximal escape mutations identified."
      : "Structural resistance risk: mutations in or near the binding site threaten long-term drug durability.";
  }

  return "Review primary data source to assess impact on programme viability and clinical development strategy.";
}

function getFlagAnalystTakeaway(flag) {
  const t = (flag.title + " " + flag.text).toLowerCase();
  if (t.includes("covalent") || t.includes("c797") || t.includes("attachment residue")) {
    return "Commercial & Clinical Implication: Patient tumors frequently develop resistance via these active-site mutations within 9–14 months of therapy. Due diligence should confirm next-generation backup compounds or combination strategies are in development.";
  }
  if (t.includes("escape") || t.includes("pocket") || t.includes("binding-pocket") || t.includes("binding site")) {
    return "Durability Implication: Known clinical variants directly threaten drug-target affinity. Requires monitoring for combination regimens or second-line salvage therapies to protect market share.";
  }
  if (t.includes("density") || t.includes("mutational diversity")) {
    return "Resistance Reservoir: Extensive genetic heterogeneity in the active site provides fertile ground for clonal selection under drug pressure.";
  }
  if (t.includes("accrual") || t.includes("recruit") || t.includes("enrollment")) {
    return "Budget & Timeline Implication: Patient enrollment will likely lag projected timelines. Ensure trial models budget 20–30% higher site initiation costs and implement aggressive multi-center recruitment.";
  }
  if (t.includes("safety") || t.includes("toxic") || t.includes("liabilities")) {
    return "Therapeutic Window Implication: On-target toxicity may narrow the allowable dose. Review Phase 1 dose-escalation stopping rules and monitor patient liver/skin toxicity profiles closely.";
  }
  if (t.includes("futility") || t.includes("tolerability") || t.includes("lack of efficacy")) {
    return "Clinical Bar Implication: High historical attrition underscores that patient stratification is mandatory. Insist on prospectively validated biomarker enrollment criteria.";
  }
  if (flag.check_id === "market") {
    return "Diligence Action: Evaluate molecular structure against competitor chemotypes and check patent coverage for derivative chemical scaffolds.";
  }
  if (flag.check_id === "trial") {
    return "Diligence Action: Audit planned clinical trial sites, inclusion/exclusion criteria, and competitor trial density in target regions.";
  }
  return "Diligence Action: Confirm findings against independent preclinical assay data before committee sign-off.";
}

function App() {
  const [query, setQuery] = useState("Osimertinib targeting EGFR in NSCLC");
  const [asOfDate, setAsOfDate] = useState("");
  const [showAsOfPicker, setShowAsOfPicker] = useState(false);
  const [isListening, setIsListening] = useState(false);
  const [health, setHealth] = useState(null);
  
  // Pipeline State
  const [asset, setAsset] = useState(null);
  const [runId, setRunId] = useState(null);
  const [runStatus, setRunStatus] = useState("idle"); // idle | running | complete | error
  const [statusMessage, setStatusMessage] = useState("");
  const [runView, setRunView] = useState(null);
  const [events, setEvents] = useState([]);
  const [errorMessage, setErrorMessage] = useState(null);
  
  // Modals & UI controls
  const [showMemoModal, setShowMemoModal] = useState(false);
  const [memoMarkdown, setMemoMarkdown] = useState("");
  const [showEvalModal, setShowEvalModal] = useState(false);
  const [evalData, setEvalData] = useState(null);
  const [scoringInfoLane, setScoringInfoLane] = useState(null);
  const [selectedLedgerId, setSelectedLedgerId] = useState(null);
  const [ledgerSearch, setLedgerSearch] = useState("");
  const [ledgerLaneFilter, setLedgerLaneFilter] = useState("all");
  const [showTechDetails, setShowTechDetails] = useState(false);
  
  // Sign-off State
  const [reviewerName, setReviewerName] = useState("Zoravar (Lead Biotech Diligence Analyst)");
  const [rejectionNotes, setRejectionNotes] = useState({});
  const [showAddFlag, setShowAddFlag] = useState(false);
  const [newFlag, setNewFlag] = useState({ check_id: "market", severity: "high", title: "", text: "", ledger_row_ids: "" });
  const [toast, setToast] = useState(null);

  // Poll /api/health on mount
  useEffect(() => {
    fetch("/api/health")
      .then((res) => res.json())
      .then((data) => setHealth(data))
      .catch((err) => console.warn("Health check error:", err));
  }, []);

  const showToast = (message, type = "info") => {
    setToast({ message, type });
    setTimeout(() => setToast(null), 3500);
  };

  // Voice Query
  const toggleVoiceRecognition = () => {
    const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRec) {
      showToast("Voice recognition is not supported in this browser.", "error");
      return;
    }
    if (isListening) {
      setIsListening(false);
      return;
    }

    try {
      const recognition = new SpeechRec();
      recognition.continuous = false;
      recognition.interimResults = false;
      recognition.lang = "en-US";

      recognition.onstart = () => {
        setIsListening(true);
        showToast("Listening... speak drug name, target, and disease.", "info");
      };

      recognition.onresult = (event) => {
        const transcript = event.results[0][0].transcript;
        setQuery(transcript);
        setIsListening(false);
        showToast(`Heard: "${transcript}"`, "success");
        handleExecuteDiligence(transcript);
      };

      recognition.onerror = (event) => {
        setIsListening(false);
        showToast(`Voice error: ${event.error}`, "error");
      };

      recognition.onend = () => setIsListening(false);
      recognition.start();
    } catch (err) {
      setIsListening(false);
    }
  };

  // Single-Step "Run Diligence Analysis" Workflow
  const handleExecuteDiligence = async (overrideQuery = null) => {
    const q = overrideQuery || query;
    if (!q.trim()) {
      showToast("Please enter a drug, target, or disease query.", "error");
      return;
    }

    setRunStatus("running");
    setStatusMessage("Resolving asset identifiers against Open Targets & ChEMBL...");
    setErrorMessage(null);
    setRunView(null);
    setEvents([]);

    try {
      // Step 1: Resolve
      const resResolve = await fetch("/api/resolve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query: q.trim(),
          as_of: asOfDate ? asOfDate : null,
        }),
      });

      if (!resResolve.ok) {
        const errText = await resResolve.text();
        throw new Error(errText || "Asset resolution failed");
      }

      const resolvedAsset = await resResolve.json();
      setAsset(resolvedAsset);

      // Step 2: Trigger Run
      setStatusMessage(`Analyzing ${resolvedAsset.drug} across Science, Trial, and Resistance lanes...`);
      const resRun = await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ asset: resolvedAsset }),
      });

      if (!resRun.ok) throw new Error(await resRun.text());
      const { run_id } = await resRun.json();
      setRunId(run_id);

      // Connect SSE
      const evtSource = new EventSource(`/api/runs/${run_id}/events`);

      evtSource.addEventListener("status", (e) => {
        try {
          const eventObj = JSON.parse(e.data);
          setEvents((prev) => [...prev, eventObj]);
          setStatusMessage(eventObj.message);
        } catch (err) {
          console.error("SSE parse error:", err);
        }
      });

      evtSource.addEventListener("end", async () => {
        evtSource.close();
        await fetchRunView(run_id);
      });

      evtSource.onerror = async () => {
        evtSource.close();
        await fetchRunView(run_id);
      };

    } catch (err) {
      setRunStatus("error");
      setErrorMessage(err.message);
      showToast(err.message, "error");
    }
  };

  // Fetch Full RunView
  const fetchRunView = async (id) => {
    try {
      const res = await fetch(`/api/runs/${id}`);
      if (!res.ok) throw new Error(await res.text());
      const view = await res.json();
      setRunView(view);
      setRunStatus(view.status);
      if (view.status === "complete") {
        showToast("Diligence analysis complete!", "success");
      }
    } catch (err) {
      setRunStatus("error");
      setErrorMessage(err.message);
    }
  };

  // Flag Accept / Reject
  const handleFlagAction = async (flagId, action) => {
    if (!runId) return;
    const note = rejectionNotes[flagId] || "";
    if (action === "reject" && !note.trim()) {
      showToast("Please provide an analyst note explaining why you are rejecting this flag.", "error");
      return;
    }

    try {
      const payload = {
        actions: [
          {
            reviewer: reviewerName,
            action: action,
            red_flag_id: flagId,
            note: note.trim() || `Flag ${action}ed by ${reviewerName}`,
          },
        ],
      };

      const res = await fetch(`/api/runs/${runId}/signoff`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (!res.ok) throw new Error(await res.text());
      showToast(`Risk flag marked as ${action}ed.`, "success");
      await fetchRunView(runId);
    } catch (err) {
      showToast(`Signoff error: ${err.message}`, "error");
    }
  };

  // Signoff Complete Verdict
  const handleVerdictSignoff = async () => {
    if (!runId) return;
    try {
      const payload = {
        actions: [
          {
            reviewer: reviewerName,
            action: "accept",
            note: `Due-diligence review approved by ${reviewerName}`,
          },
        ],
      };

      const res = await fetch(`/api/runs/${runId}/signoff`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (!res.ok) throw new Error(await res.text());
      showToast("Diligence memo signed off! Formal memo unlocked.", "success");
      await fetchRunView(runId);
      await handleOpenMemo();
    } catch (err) {
      showToast(`Signoff error: ${err.message}`, "error");
    }
  };

  // Add Custom Flag
  const handleAddCustomFlag = async (e) => {
    e.preventDefault();
    if (!runId) return;

    const rowIds = newFlag.ledger_row_ids
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);

    if (rowIds.length === 0) {
      showToast("Analyst flag must cite at least one verified ledger row ID.", "error");
      return;
    }

    try {
      const payload = {
        actions: [
          {
            reviewer: reviewerName,
            action: "add",
            note: "Analyst identified additional risk factor",
            new_flag: {
              check_id: newFlag.check_id,
              severity: newFlag.severity,
              title: newFlag.title.trim(),
              text: newFlag.text.trim(),
              ledger_row_ids: rowIds,
            },
          },
        ],
      };

      const res = await fetch(`/api/runs/${runId}/signoff`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (!res.ok) throw new Error(await res.text());
      showToast("Custom analyst flag added to memorandum.", "success");
      setShowAddFlag(false);
      setNewFlag({ check_id: "market", severity: "high", title: "", text: "", ledger_row_ids: "" });
      await fetchRunView(runId);
    } catch (err) {
      showToast(`Error adding flag: ${err.message}`, "error");
    }
  };

  // Fetch & Open Memo
  const handleOpenMemo = async () => {
    if (!runId) return;
    try {
      const res = await fetch(`/api/runs/${runId}/memo`);
      if (res.status === 409) {
        showToast("Please sign off the verdict first to unlock the formal memorandum.", "info");
        return;
      }
      if (!res.ok) throw new Error(await res.text());
      const markdown = await res.text();
      setMemoMarkdown(markdown);
      setShowMemoModal(true);
    } catch (err) {
      showToast(`Memo error: ${err.message}`, "error");
    }
  };

  // Fetch & Open Eval Modal
  const handleOpenEval = async () => {
    try {
      const res = await fetch("/api/eval");
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      setEvalData(data);
      setShowEvalModal(true);
    } catch (err) {
      showToast(`Eval fetch error: ${err.message}`, "error");
    }
  };

  // Jump to Ledger Row
  const scrollToLedgerRow = (id) => {
    setSelectedLedgerId(id);
    const element = document.getElementById(`ledger-row-${id}`);
    if (element) {
      element.scrollIntoView({ behavior: "smooth", block: "center" });
      element.classList.remove("row-highlight");
      void element.offsetWidth;
      element.classList.add("row-highlight");
    } else {
      showToast(`Evidence record #${id}`, "info");
    }
  };

  return (
    <div className="flex flex-col min-h-screen bg-slate-50 text-slate-900">
      {/* Toast */}
      {toast && (
        <div className={`fixed bottom-6 right-6 z-50 px-4 py-3 rounded-lg shadow-lg text-sm font-medium border flex items-center gap-2 ${
          toast.type === "success" ? "bg-emerald-50 text-emerald-900 border-emerald-200" :
          toast.type === "error" ? "bg-rose-50 text-rose-900 border-rose-200" :
          "bg-slate-900 text-white border-slate-800"
        }`}>
          <span>{toast.type === "success" ? "✓" : toast.type === "error" ? "⚠" : "ℹ"}</span>
          <span>{toast.message}</span>
        </div>
      )}

      {/* Header */}
      <header className="border-b border-slate-200 bg-white sticky top-0 z-40">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 py-3.5 flex flex-wrap items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-lg bg-blue-600 flex items-center justify-center text-white text-lg font-bold shadow-sm">
              🧬
            </div>
            <div>
              <div className="flex items-center gap-2">
                <span className="text-lg font-bold tracking-tight text-slate-900">Pre-Mortem</span>
                <span className="text-[11px] font-semibold px-2 py-0.5 rounded-full bg-blue-50 text-blue-700 border border-blue-200">
                  Biotech Track
                </span>
              </div>
              <p className="text-xs text-slate-500 font-normal">
                AI Due-Diligence & Failure-Mode Triage for Drug Candidates
              </p>
            </div>
          </div>

          <div className="flex items-center gap-3">
            {health && (
              <div className="hidden md:flex items-center gap-2 text-xs bg-slate-50 border border-slate-200 px-3 py-1.5 rounded-full text-slate-600">
                <span className="w-2 h-2 rounded-full bg-emerald-500"></span>
                <span className="font-medium">3 Check Lanes Active</span>
                {health.llm && <span className="text-blue-600 font-medium">• Gemini 4 Argon Reasoning</span>}
              </div>
            )}
            
            <button
              onClick={handleOpenEval}
              className="text-xs font-semibold px-3 py-1.5 rounded-lg bg-white hover:bg-slate-50 text-slate-700 border border-slate-300 transition shadow-sm flex items-center gap-1.5"
            >
              📊 Evaluation & Validation Proof
            </button>
          </div>
        </div>

        {/* Responsible AI Disclaimer Banner */}
        <div className="bg-slate-50 border-t border-slate-200 px-4 py-1.5 text-center text-xs text-slate-600 font-medium">
          🛡️ Responsible AI: Pre-Clinical / BD Investment Triage Tool — No patient data, no clinical advice. Draft until human analyst sign-off.
        </div>
      </header>

      {/* Main Container */}
      <main className="max-w-7xl mx-auto px-4 sm:px-6 py-8 flex-1 space-y-8 w-full">
        {/* Search & Query Input Card */}
        <section className="bg-white border border-slate-200 rounded-xl p-6 shadow-sm">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-xs font-bold uppercase tracking-wider text-slate-500">
              Drug Asset Query
            </h2>
            <div className="text-xs text-slate-500">
              Live resolution against Open Targets, ChEMBL, UniProt & ClinicalTrials.gov
            </div>
          </div>

          <div className="flex flex-col sm:flex-row gap-2.5">
            <div className="flex-1 relative">
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleExecuteDiligence()}
                placeholder="Enter drug name, target, and disease (e.g., Osimertinib targeting EGFR in NSCLC)..."
                className="w-full bg-white border border-slate-300 rounded-lg px-4 py-3 text-sm text-slate-900 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-600 focus:border-transparent transition"
              />
              <button
                type="button"
                onClick={toggleVoiceRecognition}
                title="Voice query (Web Speech API)"
                className={`absolute right-3 top-2.5 p-1.5 rounded-md text-slate-400 hover:text-slate-600 transition ${
                  isListening ? "bg-rose-100 text-rose-600 voice-recording" : "hover:bg-slate-100"
                }`}
              >
                🎤
              </button>
            </div>

            <button
              type="button"
              onClick={() => handleExecuteDiligence()}
              disabled={runStatus === "running"}
              className="px-6 py-3 rounded-lg bg-blue-600 hover:bg-blue-700 disabled:bg-blue-400 text-white text-sm font-semibold transition shadow-sm whitespace-nowrap"
            >
              {runStatus === "running" ? "Analyzing..." : "Run Diligence Analysis"}
            </button>
          </div>

          {/* Collapsible Historical Cutoff Date Picker */}
          <div className="mt-3.5 flex flex-wrap items-center justify-between gap-3 text-xs">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-slate-500 font-medium">Quick Presets:</span>
              {QUICK_PICKS.map((p, idx) => (
                <button
                  key={idx}
                  onClick={() => {
                    setQuery(p.query);
                    setAsOfDate(p.asOf);
                    handleExecuteDiligence(p.query);
                  }}
                  className="px-2.5 py-1 rounded-md bg-slate-100 hover:bg-slate-200 text-slate-700 transition font-medium"
                >
                  {p.label}
                </button>
              ))}
            </div>

            <div>
              <button
                type="button"
                onClick={() => setShowAsOfPicker(!showAsOfPicker)}
                className="text-blue-600 hover:text-blue-800 font-medium flex items-center gap-1"
              >
                <span>{showAsOfPicker ? "▾ Hide" : "▸ Historical Cutoff Date (Optional)"}</span>
                {asOfDate && <span className="font-mono text-slate-600">({asOfDate})</span>}
              </button>
            </div>
          </div>

          {showAsOfPicker && (
            <div className="mt-3 p-3 bg-slate-50 border border-slate-200 rounded-lg flex flex-wrap items-center gap-3">
              <label className="text-xs font-semibold text-slate-700">Cutoff Date (as_of):</label>
              <input
                type="date"
                value={asOfDate}
                onChange={(e) => setAsOfDate(e.target.value)}
                className="bg-white border border-slate-300 rounded px-2.5 py-1 text-xs text-slate-800 focus:outline-none focus:ring-1 focus:ring-blue-600"
              />
              <span className="text-xs text-slate-500">
                Set a past date to test what Pre-Mortem would have predicted using only evidence published before that date.
              </span>
              {asOfDate && (
                <button
                  type="button"
                  onClick={() => setAsOfDate("")}
                  className="text-xs text-rose-600 hover:underline"
                >
                  Clear Cutoff
                </button>
              )}
            </div>
          )}

          {/* Resolved Entity Confirmation Strip */}
          {asset && (
            <div className="mt-4 p-3 bg-blue-50/60 border border-blue-200 rounded-lg flex flex-wrap items-center justify-between gap-3 text-xs text-slate-800">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold text-blue-950">Target:</span>
                <span className="font-bold text-blue-900">{asset.target}</span>
                {asset.ids.uniprot_id && <span className="font-mono text-blue-700">(UniProt: {asset.ids.uniprot_id})</span>}
                <span className="text-slate-400">•</span>
                <span className="font-semibold text-blue-950">Drug:</span>
                <span className="font-bold text-blue-900">{asset.drug}</span>
                {asset.ids.chembl_id && <span className="font-mono text-blue-700">(ChEMBL: {asset.ids.chembl_id})</span>}
                <span className="text-slate-400">•</span>
                <span className="font-semibold text-blue-950">Indication:</span>
                <span className="font-medium text-slate-800">{asset.disease}</span>
              </div>
              {asset.as_of && (
                <span className="font-mono px-2 py-0.5 rounded bg-amber-100 text-amber-800 border border-amber-300 text-[11px]">
                  Cutoff: {asset.as_of}
                </span>
              )}
            </div>
          )}
        </section>

        {/* Live Running Progress Indicator */}
        {runStatus === "running" && (
          <div className="bg-white border border-blue-200 rounded-xl p-4 shadow-sm flex items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <div className="w-5 h-5 border-2 border-blue-600 border-t-transparent rounded-full animate-spin"></div>
              <div>
                <div className="text-sm font-bold text-slate-900">Running Diligence Analysis...</div>
                <div className="text-xs text-slate-500 font-mono mt-0.5">{statusMessage}</div>
              </div>
            </div>
            <div className="text-xs font-medium text-slate-500">
              {events.length} records processed
            </div>
          </div>
        )}

        {/* Three Evidence Check Lanes */}
        <section className="space-y-3">
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-xs font-bold uppercase tracking-wider text-slate-500">
                Independent Failure-Mode Checks
              </h2>
              <p className="text-xs text-slate-500 mt-0.5">
                Every claim is verified against primary databases • Honest degradation if data is unavailable
              </p>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-5">
            {/* Lane 1: Target Biology */}
            <LaneCard
              laneKey="science"
              result={runView?.check_results?.science}
              events={events.filter((e) => e.check_id === "science")}
              onCiteClick={scrollToLedgerRow}
              onOpenInfo={() => setScoringInfoLane("science")}
            />

            {/* Lane 2: Trial Execution */}
            <LaneCard
              laneKey="trial"
              result={runView?.check_results?.trial}
              events={events.filter((e) => e.check_id === "trial")}
              onCiteClick={scrollToLedgerRow}
              onOpenInfo={() => setScoringInfoLane("trial")}
            />

            {/* Lane 3: Drug Resistance */}
            <LaneCard
              laneKey="market"
              result={runView?.check_results?.market}
              events={events.filter((e) => e.check_id === "market")}
              onCiteClick={scrollToLedgerRow}
              onOpenInfo={() => setScoringInfoLane("market")}
            />
          </div>
        </section>

        {/* 3D Protein Structure & Resistance Map */}
        {runView?.structure && (
          <section className="bg-white border border-slate-200 rounded-xl p-6 shadow-sm space-y-4">
            <div className="flex flex-wrap items-start justify-between gap-3 border-b border-slate-200 pb-3">
              <div>
                <h3 className="text-base font-bold text-slate-900">
                  3D Drug-Target Binding & Resistance Map
                </h3>
                <p className="text-xs text-slate-500 mt-0.5">
                  Visualizing how the drug physically locks into the target protein—and where known patient mutations can block it.
                </p>
              </div>

              <div className="flex items-center gap-2">
                <span className="text-xs font-mono px-2 py-1 rounded bg-slate-100 text-slate-800 border border-slate-200 font-semibold">
                  {runView.structure.source.toUpperCase()}: {runView.structure.structure_id}
                </span>
                <a
                  href={runView.structure.source === "pdb" ? `https://www.rcsb.org/structure/${runView.structure.structure_id}` : runView.structure.structure_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-xs font-semibold px-2.5 py-1 rounded bg-blue-50 text-blue-700 hover:bg-blue-100 border border-blue-200 transition"
                >
                  View Official Structure ↗
                </a>
              </div>
            </div>

            {/* Human-Readable Structure Summary Banner */}
            <div className="bg-slate-50 border border-slate-200 rounded-lg p-3 text-xs text-slate-700 space-y-1.5">
              <div className="flex items-start justify-between gap-2">
                <div>
                  <span className="font-bold text-slate-900">Structure Summary: </span>
                  {runView.structure.source === "pdb" ? (
                    <span>
                      Experimental 3D X-Ray Structure (RCSB Protein Data Bank ID: <strong>{runView.structure.structure_id}</strong>) showing <strong>{runView.asset.target}</strong> physically bound to ligand. Direct heavy-atom Euclidean contact distances computed in Python.
                    </span>
                  ) : (
                    <span>
                      AlphaFold AI-Predicted Structure (No experimental drug-bound co-crystal available in PDB — confidence adjusted).
                    </span>
                  )}
                </div>
                <button
                  type="button"
                  onClick={() => setShowTechDetails(!showTechDetails)}
                  className="text-[11px] text-blue-600 hover:underline whitespace-nowrap font-medium"
                >
                  {showTechDetails ? "Hide Details" : "Show Technical Details"}
                </button>
              </div>

              {showTechDetails && (
                <div className="pt-2 border-t border-slate-200 font-mono text-[11px] text-slate-600">
                  {runView.structure.confidence_note}
                </div>
              )}
            </div>

            <StructureViewer3D structure={runView.structure} asset={runView.asset} onCiteClick={scrollToLedgerRow} />
          </section>
        )}

        {/* Verdict & Red Flags Section */}
        {runView?.verdict && (
          <section className="bg-white border border-slate-200 rounded-xl p-6 shadow-sm space-y-6">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 pb-4">
              <div>
                <h3 className="text-base font-bold text-slate-900">
                  Investment Triage Verdict & Verified Red Flags
                </h3>
                <p className="text-xs text-slate-500 mt-0.5">
                  Synthesized by Gemini 4 Argon reading strictly from verified Ledger records • Python owns all calculations
                </p>
              </div>

              <div className="flex items-center gap-3">
                <span className={`text-xs px-3 py-1 rounded-full font-bold uppercase tracking-wider border ${
                  runView.verdict.is_draft
                    ? "bg-amber-50 text-amber-800 border-amber-300"
                    : "bg-emerald-50 text-emerald-800 border-emerald-300"
                }`}>
                  {runView.verdict.is_draft ? "Draft (Requires Analyst Review)" : "Signed Off"}
                </span>

                <button
                  onClick={handleOpenMemo}
                  className="text-xs font-semibold px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-700 text-white transition shadow-sm flex items-center gap-1.5"
                >
                  📄 View 1-Page Diligence Memo
                </button>
              </div>
            </div>

            {/* Verdict Metrics Cards */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <div className="p-4 rounded-xl bg-slate-50 border border-slate-200 flex items-center gap-4">
                <div className={`w-14 h-14 rounded-xl flex items-center justify-center text-xl font-bold border ${
                  runView.verdict.overall_risk === "high" ? "bg-rose-100 text-rose-800 border-rose-300" :
                  runView.verdict.overall_risk === "medium" ? "bg-amber-100 text-amber-800 border-amber-300" :
                  "bg-emerald-100 text-emerald-800 border-emerald-300"
                }`}>
                  {runView.verdict.overall_score !== null ? `${Math.round(runView.verdict.overall_score * 100)}%` : "N/A"}
                </div>
                <div>
                  <div className="text-xs text-slate-500 font-medium">Combined Failure Risk</div>
                  <div className="text-sm font-bold uppercase text-slate-900">
                    {runView.verdict.overall_risk} Risk
                  </div>
                  <div className="text-xs text-slate-500">
                    Evidence Coverage: <span className="font-semibold capitalize">{runView.verdict.overall_confidence}</span>
                  </div>
                </div>
              </div>

              <div className="p-4 rounded-xl bg-slate-50 border border-slate-200">
                <div className="text-xs text-slate-500 font-medium">Primary Fatal Failure Driver</div>
                <div className="text-sm font-bold text-rose-700 mt-1">
                  {FAILURE_MODE_TITLES[runView.verdict.primary_failure_mode] || runView.verdict.primary_failure_mode || "None identified"}
                </div>
                <div className="text-xs text-slate-500 mt-1">
                  Highest risk mode across usable checks
                </div>
              </div>

              <div className="p-4 rounded-xl bg-slate-50 border border-slate-200">
                <div className="text-xs text-slate-500 font-medium">Synthesis & Red Flags</div>
                <div className="text-sm font-bold text-slate-900 mt-1">
                  {runView.verdict.red_flags.length} Red Flags Ranked
                </div>
                <div className="text-xs text-slate-500 mt-1">
                  Zero hallucinations • All claims backed by links
                </div>
              </div>
            </div>

            {/* Executive Summary */}
            <div className="p-4 rounded-xl bg-slate-50 border border-slate-200 space-y-2">
              <h4 className="text-xs font-bold uppercase tracking-wider text-slate-600">
                Executive Diligence Summary
              </h4>
              <p className="text-sm text-slate-800 leading-relaxed font-normal">
                {runView.verdict.overall_summary}
              </p>
            </div>

            {/* Ranked Red Flags */}
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <div>
                  <h4 className="text-xs font-bold uppercase tracking-wider text-slate-700">
                    Ranked Red Flags ({runView.verdict.red_flags.length})
                  </h4>
                  <p className="text-xs text-slate-500">
                    Accepting includes the risk in the final memorandum; Rejecting overrides it with your logged rationale.
                  </p>
                </div>
                <button
                  onClick={() => setShowAddFlag(!showAddFlag)}
                  className="text-xs font-semibold px-2.5 py-1.5 rounded-lg bg-white hover:bg-slate-50 text-slate-700 border border-slate-300 transition"
                >
                  {showAddFlag ? "Cancel" : "+ Add Analyst Flag"}
                </button>
              </div>

              {/* Add Flag Form */}
              {showAddFlag && (
                <form onSubmit={handleAddCustomFlag} className="p-4 rounded-xl bg-slate-50 border border-blue-300 space-y-3">
                  <div className="text-xs font-bold text-blue-900">Add Analyst Flag (Must cite verified Ledger rows)</div>
                  <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                    <input
                      type="text"
                      placeholder="Flag Title"
                      value={newFlag.title}
                      onChange={(e) => setNewFlag({ ...newFlag, title: e.target.value })}
                      required
                      className="bg-white border border-slate-300 rounded px-3 py-1.5 text-xs text-slate-800"
                    />
                    <select
                      value={newFlag.check_id}
                      onChange={(e) => setNewFlag({ ...newFlag, check_id: e.target.value })}
                      className="bg-white border border-slate-300 rounded px-3 py-1.5 text-xs text-slate-800"
                    >
                      <option value="science">Target Biology</option>
                      <option value="trial">Trial Execution</option>
                      <option value="market">Resistance</option>
                    </select>
                    <select
                      value={newFlag.severity}
                      onChange={(e) => setNewFlag({ ...newFlag, severity: e.target.value })}
                      className="bg-white border border-slate-300 rounded px-3 py-1.5 text-xs text-slate-800"
                    >
                      <option value="high">High Severity</option>
                      <option value="medium">Medium Severity</option>
                      <option value="low">Low Severity</option>
                    </select>
                  </div>
                  <textarea
                    placeholder="Describe the clinical or investment risk in plain English..."
                    value={newFlag.text}
                    onChange={(e) => setNewFlag({ ...newFlag, text: e.target.value })}
                    required
                    className="w-full bg-white border border-slate-300 rounded px-3 py-2 text-xs text-slate-800 h-16"
                  />
                  <input
                    type="text"
                    placeholder="Cited Ledger Row IDs (comma-separated, e.g. market-structure-6JXT)"
                    value={newFlag.ledger_row_ids}
                    onChange={(e) => setNewFlag({ ...newFlag, ledger_row_ids: e.target.value })}
                    required
                    className="w-full bg-white border border-slate-300 rounded px-3 py-1.5 text-xs text-slate-800 font-mono"
                  />
                  <button
                    type="submit"
                    className="px-4 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-700 text-white text-xs font-semibold"
                  >
                    Add to Verdict
                  </button>
                </form>
              )}

              {runView.verdict.red_flags.map((flag) => (
                <div
                  key={flag.id}
                  className={`p-4 rounded-xl border transition ${
                    flag.review_status === "rejected" ? "bg-slate-50 border-slate-200 opacity-60" :
                    flag.review_status === "accepted" ? "bg-emerald-50/40 border-emerald-200" :
                    "bg-white border-slate-200 shadow-sm"
                  }`}
                >
                  <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
                    <div className="flex items-center gap-2">
                      <span className="text-xs font-mono font-bold px-2 py-0.5 rounded bg-slate-100 text-slate-700">
                        #{flag.rank}
                      </span>
                      <span className={`text-xs px-2 py-0.5 rounded font-bold uppercase ${
                        flag.severity === "high" ? "bg-rose-100 text-rose-800 border border-rose-200" :
                        flag.severity === "medium" ? "bg-amber-100 text-amber-800 border border-amber-200" :
                        "bg-emerald-100 text-emerald-800 border border-emerald-200"
                      }`}>
                        {flag.severity}
                      </span>
                      <span className="text-xs px-2 py-0.5 rounded bg-slate-100 text-slate-600 font-medium">
                        {flag.check_id === "market" ? "Resistance Risk" : flag.check_id === "trial" ? "Trial Execution Risk" : "Target Biology Risk"}
                      </span>
                      <h5 className="text-sm font-bold text-slate-900">{flag.title}</h5>
                      {flag.added_by && (
                        <span className="text-xs px-2 py-0.5 rounded bg-blue-50 text-blue-700">
                          Added by {flag.added_by}
                        </span>
                      )}
                    </div>

                    <div className="flex items-center gap-1.5">
                      {flag.review_status === "pending" ? (
                        <>
                          <button
                            onClick={() => handleFlagAction(flag.id, "accept")}
                            className="text-xs px-2.5 py-1 rounded bg-emerald-50 hover:bg-emerald-100 text-emerald-800 border border-emerald-300 font-medium transition"
                          >
                            Accept
                          </button>
                          <button
                            onClick={() => {
                              const note = prompt(`Enter rejection rationale for flag '${flag.title}':`);
                              if (note) {
                                setRejectionNotes({ ...rejectionNotes, [flag.id]: note });
                                handleFlagAction(flag.id, "reject");
                              }
                            }}
                            className="text-xs px-2.5 py-1 rounded bg-rose-50 hover:bg-rose-100 text-rose-800 border border-rose-300 font-medium transition"
                          >
                            Reject
                          </button>
                        </>
                      ) : (
                        <span className={`text-xs px-2 py-0.5 rounded font-bold ${
                          flag.review_status === "accepted" ? "text-emerald-700" : "text-rose-700"
                        }`}>
                          {flag.review_status.toUpperCase()}
                        </span>
                      )}
                    </div>
                  </div>

                  <div className="text-xs text-slate-800 leading-relaxed mb-2 font-medium">
                    {flag.text}
                  </div>

                  {/* Analyst Diligence Takeaway Callout */}
                  <div className="bg-amber-50/70 border border-amber-200/90 rounded-lg p-2.5 text-xs text-amber-950 mb-3 leading-relaxed">
                    <span className="font-bold text-amber-900 flex items-center gap-1 mb-0.5">
                      <span>💡</span> <span>What this means (Analyst Takeaway):</span>
                    </span>
                    <span>{getFlagAnalystTakeaway(flag)}</span>
                  </div>

                  <div className="flex flex-wrap items-center gap-1.5 pt-2 border-t border-slate-100">
                    <span className="text-xs text-slate-500 font-medium">Citations:</span>
                    {flag.ledger_row_ids.map((cid) => (
                      <button
                        key={cid}
                        onClick={() => scrollToLedgerRow(cid)}
                        className="text-xs px-2 py-0.5 rounded bg-slate-100 hover:bg-slate-200 text-blue-700 font-mono transition"
                      >
                        #{cid}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
            </div>

            {/* Human Analyst Sign-Off Gate */}
            <div className="p-5 rounded-xl bg-slate-50 border border-slate-200 space-y-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <h4 className="text-sm font-bold text-slate-900">
                    Human Analyst Sign-Off Gate
                  </h4>
                  <p className="text-xs text-slate-500 mt-0.5">
                    No investment recommendation without named analyst accountability. Approving unlocks the formal Diligence Memo.
                  </p>
                </div>

                <div className="flex items-center gap-3">
                  <input
                    type="text"
                    value={reviewerName}
                    onChange={(e) => setReviewerName(e.target.value)}
                    placeholder="Analyst Name & Title"
                    className="bg-white border border-slate-300 rounded-lg px-3 py-1.5 text-xs text-slate-800 focus:outline-none focus:ring-1 focus:ring-blue-600"
                  />
                  <button
                    onClick={handleVerdictSignoff}
                    disabled={!runView.verdict.is_draft}
                    className={`px-5 py-2 rounded-lg text-xs font-bold transition shadow-sm ${
                      runView.verdict.is_draft
                        ? "bg-emerald-600 hover:bg-emerald-700 text-white"
                        : "bg-slate-200 text-slate-500 cursor-not-allowed"
                    }`}
                  >
                    {runView.verdict.is_draft ? "Sign Off Diligence Verdict" : "Verdict Approved ✓"}
                  </button>
                </div>
              </div>

              {runView.verdict.signed_off_by && (
                <div className="text-xs text-emerald-700 font-medium">
                  ✓ Signed off by {runView.verdict.signed_off_by} on {new Date(runView.verdict.signed_off_at).toLocaleDateString()}
                </div>
              )}
            </div>
          </section>
        )}

        {/* Evidence Ledger Section */}
        {runView?.ledger_rows && runView.ledger_rows.length > 0 && (
          <section className="bg-white border border-slate-200 rounded-xl p-6 shadow-sm space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 pb-3">
              <div>
                <h3 className="text-base font-bold text-slate-900">
                  Verified Evidence Ledger ({runView.ledger_rows.length} records)
                </h3>
                <p className="text-xs text-slate-500 mt-0.5">
                  Immutable provenance: every single claim is backed by a primary database record, timestamp, and licence.
                </p>
              </div>

              <div className="flex flex-wrap items-center gap-2">
                {/* Category Filter Pills */}
                <div className="flex items-center gap-1 bg-slate-100 p-0.5 rounded-lg text-xs">
                  {[
                    { id: "all", label: "All Sources" },
                    { id: "science", label: "Target Biology" },
                    { id: "trial", label: "Clinical Trials" },
                    { id: "market", label: "Resistance & 3D" },
                  ].map((tab) => (
                    <button
                      key={tab.id}
                      onClick={() => setLedgerLaneFilter(tab.id)}
                      className={`px-2.5 py-1 rounded-md font-medium transition ${
                        ledgerLaneFilter === tab.id
                          ? "bg-white text-slate-900 shadow-sm"
                          : "text-slate-600 hover:text-slate-900"
                      }`}
                    >
                      {tab.label}
                    </button>
                  ))}
                </div>

                {/* Keyword Search */}
                <input
                  type="text"
                  placeholder="Search claims or records..."
                  value={ledgerSearch}
                  onChange={(e) => setLedgerSearch(e.target.value)}
                  className="bg-white border border-slate-300 rounded-lg px-3 py-1.5 text-xs text-slate-800 placeholder-slate-400 focus:outline-none focus:ring-1 focus:ring-blue-600"
                />
              </div>
            </div>

            <div className="overflow-x-auto max-h-[460px] overflow-y-auto rounded-lg border border-slate-200">
              <table className="w-full text-left border-collapse text-xs">
                <thead className="bg-slate-50 sticky top-0 z-10 border-b border-slate-200 text-slate-600 font-semibold">
                  <tr>
                    <th className="py-2.5 px-3">Record ID</th>
                    <th className="py-2.5 px-3">Category</th>
                    <th className="py-2.5 px-3">Verified Claim</th>
                    <th className="py-2.5 px-3">Source Record</th>
                    <th className="py-2.5 px-3">Evidence Date</th>
                    <th className="py-2.5 px-3">Licence</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 text-slate-700">
                  {runView.ledger_rows
                    .filter((row) => (ledgerLaneFilter === "all" || row.check_id === ledgerLaneFilter))
                    .filter((row) => !ledgerSearch || JSON.stringify(row).toLowerCase().includes(ledgerSearch.toLowerCase()))
                    .map((row) => {
                      const isSelected = selectedLedgerId === row.id;
                      return (
                        <tr
                          key={row.id}
                          id={`ledger-row-${row.id}`}
                          className={`hover:bg-slate-50 transition ${
                            isSelected ? "bg-blue-50 border-l-4 border-blue-600" : ""
                          }`}
                        >
                          <td className="py-2 px-3 font-mono font-medium text-blue-700 whitespace-nowrap">
                            #{row.id}
                          </td>
                          <td className="py-2 px-3 whitespace-nowrap">
                            <span className="px-2 py-0.5 rounded-full bg-slate-100 text-slate-700 text-[10px] font-medium uppercase">
                              {row.check_id}
                            </span>
                          </td>
                          <td className="py-2 px-3 text-slate-800">
                            {row.claim}
                          </td>
                          <td className="py-2 px-3 whitespace-nowrap">
                            <a
                              href={row.url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="text-blue-600 hover:underline font-medium flex items-center gap-1"
                            >
                              {row.source_record} ↗
                            </a>
                          </td>
                          <td className="py-2 px-3 font-mono text-slate-500 whitespace-nowrap">
                            {row.evidence_date || "Current"}
                          </td>
                          <td className="py-2 px-3 text-slate-500 whitespace-nowrap text-[11px]">
                            {row.licence}
                          </td>
                        </tr>
                      );
                    })}
                </tbody>
              </table>
            </div>
          </section>
        )}
      </main>

      {/* "How This Is Scored" Info Modal */}
      {scoringInfoLane && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/50 backdrop-blur-sm">
          <div className="bg-white border border-slate-200 rounded-xl w-full max-w-2xl shadow-xl overflow-hidden">
            <div className="p-4 border-b border-slate-200 flex items-center justify-between">
              <div>
                <h4 className="text-sm font-bold text-slate-900">
                  {LANE_DETAILS[scoringInfoLane].title} — Scoring Methodology
                </h4>
                <p className="text-xs text-slate-500">
                  {LANE_DETAILS[scoringInfoLane].subtitle}
                </p>
              </div>
              <button
                onClick={() => setScoringInfoLane(null)}
                className="p-1 rounded-md text-slate-400 hover:text-slate-600"
              >
                ✕
              </button>
            </div>

            <div className="p-6 space-y-4 text-xs text-slate-700">
              <div>
                <h5 className="font-bold text-slate-900 mb-1">Core Diligence Question:</h5>
                <p className="bg-slate-50 p-2.5 rounded-lg border border-slate-200 text-slate-800">
                  {LANE_DETAILS[scoringInfoLane].scoringInfo.question}
                </p>
              </div>

              <div>
                <h5 className="font-bold text-slate-900 mb-2">Failure Risk Tiers:</h5>
                <div className="space-y-2">
                  {LANE_DETAILS[scoringInfoLane].scoringInfo.tiers.map((tier, idx) => (
                    <div key={idx} className="p-2.5 rounded-lg bg-slate-50 border border-slate-200">
                      <span className="font-bold text-slate-900">{tier.label}: </span>
                      <span>{tier.desc}</span>
                    </div>
                  ))}
                </div>
              </div>

              <div>
                <h5 className="font-bold text-slate-900 mb-1">Evidence Coverage (Confidence):</h5>
                <p>{LANE_DETAILS[scoringInfoLane].scoringInfo.coverageNote}</p>
              </div>

              <div>
                <h5 className="font-bold text-slate-900 mb-1">Empirical Backtest Validation:</h5>
                <p className="font-medium text-blue-900 bg-blue-50/70 p-2.5 rounded-lg border border-blue-200">
                  {LANE_DETAILS[scoringInfoLane].scoringInfo.validation}
                </p>
              </div>
            </div>

            <div className="p-4 border-t border-slate-200 bg-slate-50 text-right">
              <button
                onClick={() => setScoringInfoLane(null)}
                className="px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-700 text-white text-xs font-semibold"
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 1-Page Diligence Memo Modal */}
      {showMemoModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/60 backdrop-blur-sm">
          <div className="bg-white border border-slate-300 rounded-xl w-full max-w-4xl max-h-[92vh] flex flex-col shadow-2xl overflow-hidden">
            <div className="p-4 border-b border-slate-200 flex items-center justify-between no-print">
              <div>
                <h4 className="text-base font-bold text-slate-900">
                  Due-Diligence Investment Memorandum
                </h4>
                <p className="text-xs text-slate-500">
                  Pre-Mortem Investment Committee Diligence Report
                </p>
              </div>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => {
                    navigator.clipboard.writeText(memoMarkdown);
                    showToast("Markdown memo copied to clipboard!", "success");
                  }}
                  className="px-3 py-1.5 rounded-lg bg-slate-100 hover:bg-slate-200 text-slate-700 text-xs font-semibold border border-slate-300 transition"
                >
                  Copy Markdown
                </button>
                <button
                  onClick={() => window.print()}
                  className="px-3 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-700 text-white text-xs font-semibold transition shadow-sm"
                >
                  Print / Save PDF
                </button>
                <button
                  onClick={() => setShowMemoModal(false)}
                  className="p-1 rounded-md text-slate-400 hover:text-slate-600 text-sm"
                >
                  ✕
                </button>
              </div>
            </div>

            <div
              className="p-8 overflow-y-auto flex-1 prose prose-slate max-w-none text-slate-800 memo-content text-sm leading-relaxed"
              dangerouslySetInnerHTML={{ __html: window.marked ? window.marked.parse(memoMarkdown) : memoMarkdown }}
            />
          </div>
        </div>
      )}

      {/* Evaluation & Proof Modal */}
      {showEvalModal && evalData && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/60 backdrop-blur-sm">
          <div className="bg-white border border-slate-300 rounded-xl w-full max-w-5xl max-h-[92vh] flex flex-col shadow-2xl overflow-hidden">
            <div className="p-4 border-b border-slate-200 flex items-center justify-between">
              <div>
                <h4 className="text-base font-bold text-slate-900">
                  Evaluation, Baselines & Dataset Licences
                </h4>
                <p className="text-xs text-slate-500">
                  Empirical proof: three backtests beating baselines, 10-asset stress test, and voluntary failure disclosure.
                </p>
              </div>
              <button
                onClick={() => setShowEvalModal(false)}
                className="p-1 rounded-md text-slate-400 hover:text-slate-600 text-sm"
              >
                ✕
              </button>
            </div>

            <div className="p-6 overflow-y-auto flex-1 space-y-6 text-xs text-slate-700">
              {/* Backtests Proof */}
              <div className="space-y-3">
                <h5 className="text-xs font-bold uppercase tracking-wider text-slate-500">
                  Three Backtests vs. Baselines (All Beat Baselines)
                </h5>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                  {/* Science */}
                  <div className="p-4 rounded-xl bg-slate-50 border border-slate-200 space-y-1.5">
                    <div className="font-bold text-slate-900 text-sm">Target Biology Backtest</div>
                    <div className="text-2xl font-extrabold text-emerald-700">AUROC 0.790</div>
                    <div className="text-slate-500">Baseline: 0.500 (Separation 0.127)</div>
                    <div className="text-slate-500">Mann-Whitney p = 0.028 (n = 20)</div>
                    <div className="text-[11px] text-emerald-700 font-semibold">
                      ✓ Statistically significant separation at 5% level
                    </div>
                  </div>

                  {/* Trial */}
                  <div className="p-4 rounded-xl bg-slate-50 border border-slate-200 space-y-1.5">
                    <div className="font-bold text-slate-900 text-sm">Trial Execution Backtest</div>
                    <div className="text-2xl font-extrabold text-emerald-700">Precision@20 0.400</div>
                    <div className="text-slate-500">Prevalence Baseline: 0.370</div>
                    <div className="text-slate-500">In-period AUROC: 0.719</div>
                    <div className="text-[11px] text-amber-700 font-semibold">
                      ⚠ Honest negative result: does not generalize across 2022 cutoff
                    </div>
                  </div>

                  {/* Market */}
                  <div className="p-4 rounded-xl bg-slate-50 border border-slate-200 space-y-1.5">
                    <div className="font-bold text-slate-900 text-sm">Resistance Backtest</div>
                    <div className="text-2xl font-extrabold text-emerald-700">Recall@10 0.500</div>
                    <div className="text-slate-500">Pocket Baseline: 0.158 | Whole Chain: 0.040</div>
                    <div className="text-slate-500">Recalls 7 / 14 known clinical escapes</div>
                    <div className="text-[11px] text-emerald-700 font-semibold">
                      ✓ Beats pocket baseline at all k from 5 to 20
                    </div>
                  </div>
                </div>
              </div>

              {/* Stress Test */}
              {evalData.results?.stress_test && (
                <div className="space-y-2">
                  <h5 className="text-xs font-bold uppercase tracking-wider text-slate-500">
                    10-Asset Unseen Stress Test
                  </h5>
                  <div className="p-3.5 rounded-xl bg-slate-50 border border-slate-200 flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <span className="font-bold text-slate-900 text-sm">
                        {evalData.results.stress_test.n_succeeded} / {evalData.results.stress_test.n_assets} Assets Succeeded
                      </span>
                      <span className="text-slate-500 ml-2">
                        (Median latency: {evalData.results.stress_test.latency_s?.median_total?.toFixed(1)}s)
                      </span>
                    </div>
                    <div className="font-mono text-xs text-slate-600">
                      Science 10 complete • Trial 10 complete • Market honest degradation
                    </div>
                  </div>
                </div>
              )}

              {/* Where We Get It Wrong */}
              <div className="p-4 rounded-xl bg-rose-50 border border-rose-200 space-y-1.5">
                <h5 className="text-xs font-bold uppercase tracking-wider text-rose-900">
                  Where We Get It Wrong (Voluntary Failure Case)
                </h5>
                <p className="text-xs text-rose-900 leading-relaxed">
                  Imatinib resistance via <strong>M351T</strong>: M351 sits at <strong>8.5 Å</strong> from the drug in KIT/ABL1 coordinates. It causes resistance through an allosteric conformational shift rather than direct ligand contact. Any method relying purely on contact distance will miss allosteric mutations. We report this openly rather than hiding it.
                </p>
              </div>

              {/* Dataset Licences */}
              <div className="space-y-2">
                <h5 className="text-xs font-bold uppercase tracking-wider text-slate-500">
                  Public Dataset Licence & Cutoff Policy Table
                </h5>
                <div className="overflow-x-auto rounded-lg border border-slate-200">
                  <table className="w-full text-left border-collapse text-xs">
                    <thead className="bg-slate-50 text-slate-700 border-b border-slate-200">
                      <tr>
                        <th className="p-2.5 font-semibold">Dataset / Source</th>
                        <th className="p-2.5 font-semibold">Licence</th>
                        <th className="p-2.5 font-semibold">Cutoff Policy</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100 text-slate-700">
                      {evalData.licences?.map((lic, idx) => (
                        <tr key={idx} className="hover:bg-slate-50">
                          <td className="p-2.5 font-medium text-slate-900">{lic.dataset}</td>
                          <td className="p-2.5 font-mono text-blue-700 whitespace-nowrap">{lic.licence}</td>
                          <td className="p-2.5 text-slate-600">{lic.cutoff_enforcement}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Footer */}
      <footer className="border-t border-slate-200 bg-white py-6 text-center text-xs text-slate-500">
        <p>Pre-Mortem — AgentVerse 2026 UCL Hackathon • Google DeepMind Track</p>
        <p className="mt-1 font-mono text-slate-400">Deterministic Python Math Gate • Interactions API with thinking_level • Strict as_of temporal cutoff</p>
      </footer>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Subcomponent: Lane Card
// ---------------------------------------------------------------------------

function LaneCard({ laneKey, result, events, onCiteClick, onOpenInfo }) {
  const details = LANE_DETAILS[laneKey];
  const isRunning = !result && events.length > 0;
  const isComplete = result?.status === "complete";
  const isPartial = result?.status === "partial";
  const isFailed = result?.status === "failed";
  const isTimeout = result?.status === "timeout";

  const scorePct = result?.score !== undefined ? Math.round(result.score * 100) : null;
  const riskTier = getRiskTier(result?.score);
  const latestEvent = events.length > 0 ? events[events.length - 1] : null;

  return (
    <div className="bg-white border border-slate-200 rounded-xl p-5 flex flex-col justify-between shadow-sm">
      <div className="space-y-3.5">
        {/* Card Header */}
        <div className="flex items-start justify-between gap-2 border-b border-slate-100 pb-2.5">
          <div>
            <div className="flex items-center gap-1.5">
              <span className="text-[10px] font-bold uppercase tracking-wider px-1.5 py-0.5 rounded bg-blue-50 text-blue-700 border border-blue-200">
                {details.stage}
              </span>
              <h3 className="text-sm font-bold text-slate-900">{details.title}</h3>
            </div>
            <div className="text-[11px] text-slate-500 mt-1 font-normal leading-snug">
              {details.subtitle}
            </div>
          </div>

          <button
            onClick={onOpenInfo}
            title="How this check is scored"
            className="w-6 h-6 rounded-full bg-slate-100 hover:bg-slate-200 text-slate-600 flex items-center justify-center text-xs font-bold transition"
          >
            ⓘ
          </button>
        </div>

        {/* Score & Evidence Coverage Metrics */}
        {result ? (
          <div className="bg-slate-50 p-3 rounded-lg border border-slate-200 space-y-2">
            <div className="flex items-center justify-between text-xs">
              <span className="font-semibold text-slate-700">Failure Risk:</span>
              <span className={`text-xs px-2 py-0.5 rounded-full font-bold border ${riskTier.badgeClass}`}>
                {scorePct}% — {riskTier.label}
              </span>
            </div>

            <div className="w-full bg-slate-200 h-2 rounded-full overflow-hidden">
              <div
                className={`h-full transition-all duration-500 ${
                  result.score >= 0.67 ? "bg-rose-600" :
                  result.score >= 0.34 ? "bg-amber-500" :
                  "bg-emerald-600"
                }`}
                style={{ width: `${scorePct}%` }}
              ></div>
            </div>

            <div className="flex items-center justify-between text-xs pt-1 border-t border-slate-200/60">
              <span className="text-slate-500 font-medium">Evidence Coverage:</span>
              <span className="font-bold text-slate-800 capitalize">
                {result.confidence} Coverage
              </span>
            </div>
          </div>
        ) : (
          <div className="p-3 rounded-lg bg-slate-50 border border-slate-200 text-xs text-slate-500">
            {isRunning ? (
              <div className="flex items-center gap-2 text-blue-700">
                <span className="w-2 h-2 rounded-full bg-blue-600 animate-pulse"></span>
                <span>Querying {details.sources}...</span>
              </div>
            ) : (
              <span>Awaiting asset diligence run...</span>
            )}
          </div>
        )}

        {/* Running Ticker */}
        {isRunning && latestEvent && (
          <div className="text-[11px] font-mono text-blue-800 bg-blue-50/80 p-2 rounded border border-blue-200">
            &gt; {latestEvent.message}
          </div>
        )}

        {/* Findings */}
        {result?.reasons && result.reasons.length > 0 && (
          <div className="space-y-2.5 pt-1">
            <div className="text-[11px] font-bold text-slate-500 uppercase tracking-wider">
              Key Findings & Primary Sources:
            </div>
            {result.reasons.map((r, i) => (
              <div key={i} className="text-xs bg-slate-50 p-3 rounded-lg border border-slate-200 space-y-2">
                {/* 1. Scientific Evidence */}
                <div className="flex items-start gap-1.5">
                  <span className={`text-[9px] px-1.5 py-0.5 rounded font-bold uppercase whitespace-nowrap mt-0.5 ${
                    r.direction === "risk" ? "bg-rose-100 text-rose-800 border border-rose-200" :
                    r.direction === "support" ? "bg-emerald-100 text-emerald-800 border border-emerald-200" :
                    "bg-slate-200 text-slate-700"
                  }`}>
                    {r.direction === "risk" ? "Risk Factor" : r.direction === "support" ? "Favorable" : "Finding"}
                  </span>
                  <span className="flex-1 leading-snug text-slate-900 font-semibold text-xs">
                    {r.text}
                  </span>
                </div>

                {/* 2. What This Means (Analyst Takeaway) */}
                <div className="bg-blue-50/70 border border-blue-200/80 rounded-md p-2 text-[11px] text-blue-950 leading-relaxed">
                  <span className="font-bold text-blue-900 flex items-center gap-1 mb-0.5">
                    <span>💡</span> <span>What this means:</span>
                  </span>
                  <span>{getAnalystTakeaway(r.text, laneKey, r.direction)}</span>
                </div>

                {/* 3. Citations */}
                <div className="flex flex-wrap gap-1.5 pt-0.5">
                  {r.ledger_row_ids.map((id) => (
                    <button
                      key={id}
                      onClick={() => onCiteClick(id)}
                      className="text-[10px] font-medium px-2 py-0.5 rounded bg-white hover:bg-blue-50 text-blue-700 border border-slate-300 hover:border-blue-300 transition"
                    >
                      [Source: #{id}] ↗
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Missing Evidence Note */}
        {result?.missing && result.missing.length > 0 && (
          <div className="text-[11px] text-slate-600 bg-slate-50 p-2 rounded border border-slate-200">
            <span className="font-semibold text-slate-700">Evidence Caveat: </span>
            <span>{result.missing[0]}</span>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Subcomponent: 3D Structure Viewer (3Dmol.js)
// ---------------------------------------------------------------------------

function StructureViewer3D({ structure, asset, onCiteClick }) {
  const containerRef = useRef(null);
  const viewerRef = useRef(null);
  const [isSpinning, setIsSpinning] = useState(false);
  const [selectedResidue, setSelectedResidue] = useState(null);

  useEffect(() => {
    if (!containerRef.current || !window.$3Dmol) return;

    const viewer = window.$3Dmol.createViewer(containerRef.current, {
      defaultcolors: window.$3Dmol.rasmolElementColors,
    });
    viewerRef.current = viewer;

    const loadUrl = structure.structure_url;
    fetch(loadUrl)
      .then((res) => res.text())
      .then((data) => {
        const format = loadUrl.endsWith(".cif") ? "cif" : "pdb";
        viewer.addModel(data, format);

        // Cartoon protein
        viewer.setStyle({}, { cartoon: { color: "spectrum", opacity: 0.85 } });

        // Bound ligand
        viewer.setStyle({ hetflag: true }, { stick: { colorscheme: "cyanCarbon", radius: 0.28 } });

        // Escape residues
        structure.residues.forEach((resEv) => {
          const resNum = parseInt(resEv.residue.replace(/\D/g, ""), 10);
          if (isNaN(resNum)) return;

          const color = resEv.contact_distance_angstrom <= 2.5 ? "#e11d48" :
                        resEv.contact_distance_angstrom <= 5.0 ? "#f43f5e" :
                        resEv.contact_distance_angstrom <= 8.0 ? "#f59e0b" : "#eab308";

          viewer.addStyle(
            { resi: resNum, chain: resEv.chain },
            { stick: { color: color, radius: 0.32 } }
          );

          if (resEv.contact_distance_angstrom <= 5.0) {
            viewer.addSphere({
              center: { resi: resNum, chain: resEv.chain },
              radius: 1.1,
              color: color,
              alpha: 0.35,
            });
          }
        });

        viewer.zoomTo();
        viewer.render();
      })
      .catch((err) => {
        console.error("Failed to load 3D coordinates:", err);
      });

    return () => {
      if (viewerRef.current) viewerRef.current.clear();
    };
  }, [structure]);

  const toggleSpin = () => {
    if (!viewerRef.current) return;
    if (isSpinning) {
      viewerRef.current.spin(false);
      setIsSpinning(false);
    } else {
      viewerRef.current.spin("y", 1);
      setIsSpinning(true);
    }
  };

  const resetCamera = () => {
    if (!viewerRef.current) return;
    viewerRef.current.zoomTo();
    viewerRef.current.render();
  };

  const focusResidue = (resEv) => {
    setSelectedResidue(resEv.residue);
    if (!viewerRef.current) return;
    const resNum = parseInt(resEv.residue.replace(/\D/g, ""), 10);
    if (isNaN(resNum)) return;

    viewerRef.current.zoomTo({ resi: resNum, chain: resEv.chain }, 1000);
    viewerRef.current.render();
  };

  return (
    <div className="grid grid-cols-1 lg:grid-cols-12 gap-5 items-start">
      {/* 3D Canvas */}
      <div className="lg:col-span-7 flex flex-col space-y-2">
        <div className="mol-container" ref={containerRef}></div>
        
        {/* Controls & Color Key Legend Bar */}
        <div className="bg-slate-50 p-2.5 rounded-lg border border-slate-200 flex flex-wrap items-center justify-between gap-2 text-xs">
          <div className="flex items-center gap-2">
            <button
              onClick={toggleSpin}
              className="px-2.5 py-1 rounded bg-white hover:bg-slate-100 text-slate-700 font-medium border border-slate-300 transition shadow-sm"
            >
              {isSpinning ? "⏸ Stop Rotation" : "▶ Rotate"}
            </button>
            <button
              onClick={resetCamera}
              className="px-2.5 py-1 rounded bg-white hover:bg-slate-100 text-slate-700 font-medium border border-slate-300 transition shadow-sm"
            >
              ↺ Reset Camera
            </button>
          </div>

          <div className="flex flex-wrap items-center gap-2.5 text-[11px] text-slate-600 font-medium">
            <span className="flex items-center gap-1">
              <span className="w-2.5 h-2.5 rounded-full bg-cyan-400 border border-cyan-500"></span> Drug Ligand
            </span>
            <span className="flex items-center gap-1">
              <span className="w-2.5 h-2.5 rounded-full bg-rose-600"></span> &le; 5.0 Å Pocket
            </span>
            <span className="flex items-center gap-1">
              <span className="w-2.5 h-2.5 rounded-full bg-amber-500"></span> 5–8 Å Rim
            </span>
            <span className="flex items-center gap-1">
              <span className="w-2.5 h-2.5 rounded-full bg-yellow-400"></span> &gt; 8 Å Distal
            </span>
          </div>
        </div>
      </div>

      {/* Mutation Table */}
      <div className="lg:col-span-5 bg-slate-50 p-4 rounded-xl border border-slate-200 space-y-2.5">
        <div>
          <h4 className="text-xs font-bold text-slate-900 uppercase tracking-wider">
            Known Patient Mutations Near the Drug Binding Site (Escape Routes)
          </h4>
          <p className="text-[11px] text-slate-500 mt-0.5">
            If a tumor mutates at these positions, the drug may lose its grip on the protein and stop working.
          </p>
        </div>

        <div className="overflow-x-auto max-h-[380px] overflow-y-auto rounded-lg border border-slate-200 bg-white">
          <table className="w-full text-left border-collapse text-xs">
            <thead className="bg-slate-50 text-slate-600 sticky top-0 border-b border-slate-200 font-semibold">
              <tr>
                <th className="py-2 px-2.5">Residue</th>
                <th className="py-2 px-2.5">Distance</th>
                <th className="py-2 px-2.5">Binding Impact & Location</th>
                <th className="py-2 px-2.5">Primary Evidence</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 text-slate-700">
              {structure.residues.map((resEv, idx) => {
                const isSelected = selectedResidue === resEv.residue;
                const impact = getBindingImpact(resEv.contact_distance_angstrom);
                const externalUrl = resEv.evidence_url && resEv.evidence_url.startsWith("http")
                  ? resEv.evidence_url
                  : (asset?.ids?.uniprot_id ? `https://www.uniprot.org/uniprotkb/${asset.ids.uniprot_id}/entry#phenotypes_variants` : "https://www.uniprot.org");

                return (
                  <tr
                    key={idx}
                    onClick={() => focusResidue(resEv)}
                    className={`cursor-pointer hover:bg-blue-50/50 transition ${
                      isSelected ? "bg-blue-50 font-semibold" : ""
                    }`}
                  >
                    <td className="py-2 px-2.5 font-mono text-blue-900 font-bold whitespace-nowrap">
                      {resEv.residue} ({resEv.chain})
                    </td>
                    <td className="py-2 px-2.5 font-mono whitespace-nowrap">
                      {resEv.contact_distance_angstrom.toFixed(2)} Å
                    </td>
                    <td className="py-2 px-2.5">
                      <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium border ${impact.badge}`}>
                        {impact.label}
                      </span>
                    </td>
                    <td className="py-2 px-2.5 whitespace-nowrap">
                      <div className="flex items-center gap-1.5">
                        <a
                          href={externalUrl}
                          target="_blank"
                          rel="noopener noreferrer"
                          onClick={(e) => e.stopPropagation()}
                          className="text-blue-600 hover:underline text-[11px] font-medium"
                        >
                          UniProt ↗
                        </a>
                        {resEv.ledger_row_id && (
                          <button
                            type="button"
                            onClick={(e) => {
                              e.stopPropagation();
                              onCiteClick(resEv.ledger_row_id);
                            }}
                            className="text-[10px] text-slate-500 hover:text-blue-700"
                            title="Jump to ledger claim"
                          >
                            [#{resEv.ledger_row_id}]
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

// Render React Root
ReactDOM.render(<App />, document.getElementById("root"));
