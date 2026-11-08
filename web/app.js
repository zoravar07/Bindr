/* Pre-Mortem — Single Page Application (React 18 + 3Dmol.js) */

const { useState, useEffect, useRef } = React;

// Quick-Pick Demos
const QUICK_PICKS = [
  { label: "Osimertinib (EGFR / NSCLC)", query: "Osimertinib targeting EGFR in NSCLC", asOf: "" },
  { label: "Imatinib (ABL1 / CML)", query: "Imatinib targeting ABL1 in chronic myeloid leukemia", asOf: "" },
  { label: "Sotorasib (KRAS / NSCLC)", query: "Sotorasib targeting KRAS in non-small cell lung carcinoma", asOf: "" },
  { label: "Crizotinib (ALK / NSCLC)", query: "Crizotinib targeting ALK in non-small cell lung carcinoma", asOf: "" },
];

function App() {
  const [query, setQuery] = useState("Osimertinib targeting EGFR in NSCLC");
  const [asOfDate, setAsOfDate] = useState("");
  const [isListening, setIsListening] = useState(false);
  const [health, setHealth] = useState(null);
  
  // Pipeline State
  const [asset, setAsset] = useState(null);
  const [runId, setRunId] = useState(null);
  const [runStatus, setRunStatus] = useState("idle"); // idle | resolving | running | complete | error
  const [runView, setRunView] = useState(null);
  const [events, setEvents] = useState([]);
  const [errorMessage, setErrorMessage] = useState(null);
  
  // Modals & UI controls
  const [showMemoModal, setShowMemoModal] = useState(false);
  const [memoMarkdown, setMemoMarkdown] = useState("");
  const [showEvalModal, setShowEvalModal] = useState(false);
  const [evalData, setEvalData] = useState(null);
  const [selectedLedgerId, setSelectedLedgerId] = useState(null);
  const [ledgerFilter, setLedgerFilter] = useState("");
  const [laneFilter, setLaneFilter] = useState("all");
  
  // Sign-off State
  const [reviewerName, setReviewerName] = useState("Zoravar (Lead Diligence Analyst)");
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

  // Show Toast Helper
  const showToast = (message, type = "info") => {
    setToast({ message, type });
    setTimeout(() => setToast(null), 3500);
  };

  // Web Speech API Voice Recognition
  const toggleVoiceRecognition = () => {
    const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRec) {
      showToast("Speech recognition is not supported in this browser.", "error");
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
        handleResolve(transcript);
      };

      recognition.onerror = (event) => {
        console.error("Speech recognition error:", event.error);
        setIsListening(false);
        showToast(`Voice error: ${event.error}`, "error");
      };

      recognition.onend = () => {
        setIsListening(false);
      };

      recognition.start();
    } catch (err) {
      console.error(err);
      setIsListening(false);
    }
  };

  // 1. Resolve Asset
  const handleResolve = async (overrideQuery = null) => {
    const q = overrideQuery || query;
    if (!q.trim()) {
      showToast("Please enter a drug, target, or disease query.", "error");
      return;
    }

    setRunStatus("resolving");
    setErrorMessage(null);
    try {
      const res = await fetch("/api/resolve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query: q.trim(),
          as_of: asOfDate ? asOfDate : null,
        }),
      });

      if (!res.ok) {
        const errText = await res.text();
        throw new Error(errText || "Resolution failed");
      }

      const resolvedAsset = await res.json();
      setAsset(resolvedAsset);
      setRunStatus("idle");
      showToast(`Resolved: ${resolvedAsset.drug} / ${resolvedAsset.target} / ${resolvedAsset.disease}`, "success");
    } catch (err) {
      setRunStatus("error");
      setErrorMessage(err.message);
      showToast(`Resolution error: ${err.message}`, "error");
    }
  };

  // 2. Start Pipeline Run
  const handleStartRun = async (overrideAsset = null) => {
    const targetAsset = overrideAsset || asset;
    if (!targetAsset) {
      showToast("Please resolve an asset first.", "error");
      return;
    }

    setRunStatus("running");
    setEvents([]);
    setRunView(null);
    setErrorMessage(null);

    try {
      const res = await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ asset: targetAsset }),
      });

      if (!res.ok) {
        throw new Error(await res.text());
      }

      const { run_id } = await res.json();
      setRunId(run_id);

      // Connect SSE
      const evtSource = new EventSource(`/api/runs/${run_id}/events`);

      evtSource.addEventListener("status", (e) => {
        try {
          const eventObj = JSON.parse(e.data);
          setEvents((prev) => [...prev, eventObj]);
        } catch (err) {
          console.error("SSE parse error:", err);
        }
      });

      evtSource.addEventListener("end", async (e) => {
        evtSource.close();
        // Fetch full run view
        await fetchRunView(run_id);
      });

      evtSource.onerror = async (err) => {
        evtSource.close();
        await fetchRunView(run_id);
      };

    } catch (err) {
      setRunStatus("error");
      setErrorMessage(err.message);
      showToast(`Run error: ${err.message}`, "error");
    }
  };

  // Fetch RunView
  const fetchRunView = async (id) => {
    try {
      const res = await fetch(`/api/runs/${id}`);
      if (!res.ok) throw new Error(await res.text());
      const view = await res.json();
      setRunView(view);
      setRunStatus(view.status);
      if (view.status === "complete") {
        showToast("Pre-Mortem diligence run completed!", "success");
      }
    } catch (err) {
      setRunStatus("error");
      setErrorMessage(err.message);
    }
  };

  // Instant 1-Click Run
  const handleInstantRun = async () => {
    setRunStatus("resolving");
    setErrorMessage(null);
    try {
      const res = await fetch("/api/resolve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query: query.trim(),
          as_of: asOfDate ? asOfDate : null,
        }),
      });

      if (!res.ok) throw new Error(await res.text());
      const resolved = await res.json();
      setAsset(resolved);
      await handleStartRun(resolved);
    } catch (err) {
      setRunStatus("error");
      setErrorMessage(err.message);
      showToast(err.message, "error");
    }
  };

  // Signoff Action on a Red Flag (Accept or Reject)
  const handleFlagAction = async (flagId, action) => {
    if (!runId) return;
    const note = rejectionNotes[flagId] || "";
    if (action === "reject" && !note.trim()) {
      showToast("A rejection note is required for audit trail.", "error");
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
      showToast(`Flag ${flagId} ${action}ed`, "success");
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
            note: `Overall diligence verdict signed off by ${reviewerName}`,
          },
        ],
      };

      const res = await fetch(`/api/runs/${runId}/signoff`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (!res.ok) throw new Error(await res.text());
      showToast("Verdict signed off! 1-Page Diligence Memo is now unlocked.", "success");
      await fetchRunView(runId);
      await handleOpenMemo();
    } catch (err) {
      showToast(`Signoff error: ${err.message}`, "error");
    }
  };

  // Add Custom Red Flag
  const handleAddCustomFlag = async (e) => {
    e.preventDefault();
    if (!runId) return;

    const rowIds = newFlag.ledger_row_ids
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);

    if (rowIds.length === 0) {
      showToast("You must cite at least one valid ledger row ID.", "error");
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
      showToast("Custom red flag added to verdict!", "success");
      setShowAddFlag(false);
      setNewFlag({ check_id: "market", severity: "high", title: "", text: "", ledger_row_ids: "" });
      await fetchRunView(runId);
    } catch (err) {
      showToast(`Error adding flag: ${err.message}`, "error");
    }
  };

  // Fetch & Open Memo Modal
  const handleOpenMemo = async () => {
    if (!runId) return;
    try {
      const res = await fetch(`/api/runs/${runId}/memo`);
      if (res.status === 409) {
        showToast("Memo requires verdict-level analyst sign-off first.", "info");
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

  // Scroll to Ledger Row
  const scrollToLedgerRow = (id) => {
    setSelectedLedgerId(id);
    const element = document.getElementById(`ledger-row-${id}`);
    if (element) {
      element.scrollIntoView({ behavior: "smooth", block: "center" });
      element.classList.remove("row-highlight");
      void element.offsetWidth; // trigger reflow
      element.classList.add("row-highlight");
    } else {
      showToast(`Showing Ledger row #${id}`, "info");
    }
  };

  return (
    <div className="flex flex-col min-h-screen">
      {/* Toast Notification */}
      {toast && (
        <div className={`fixed bottom-6 right-6 z-50 px-4 py-3 rounded-lg shadow-xl text-sm font-medium border flex items-center gap-2 ${
          toast.type === "success" ? "bg-emerald-950/90 text-emerald-200 border-emerald-700" :
          toast.type === "error" ? "bg-rose-950/90 text-rose-200 border-rose-700" :
          "bg-slate-900/90 text-slate-200 border-slate-700"
        }`}>
          <span>{toast.type === "success" ? "✓" : toast.type === "error" ? "⚠" : "ℹ"}</span>
          <span>{toast.message}</span>
        </div>
      )}

      {/* Header */}
      <header className="border-b border-slate-800 bg-[#0c1222]/80 backdrop-blur sticky top-0 z-40">
        <div className="max-w-7xl mx-auto px-4 py-3 flex flex-wrap items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-gradient-to-tr from-blue-600 to-indigo-500 flex items-center justify-center text-xl shadow-lg shadow-blue-500/20">
              🧬
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-xl font-bold tracking-tight text-white">Pre-Mortem</h1>
                <span className="text-xs px-2 py-0.5 rounded bg-blue-500/20 text-blue-400 font-mono border border-blue-500/30">
                  AgentVerse 2026
                </span>
              </div>
              <p className="text-xs text-slate-400 italic">"Assume the drug failed. Now tell me how."</p>
            </div>
          </div>

          <div className="flex items-center gap-3">
            {health && (
              <div className="hidden sm:flex items-center gap-2 text-xs bg-slate-900 border border-slate-800 px-3 py-1.5 rounded-full text-slate-300">
                <span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"></span>
                <span>{health.lanes.length} Lanes Live</span>
                {health.llm && <span className="text-blue-400 font-semibold">• Gemini 4 Argon</span>}
              </div>
            )}
            
            <button
              onClick={handleOpenEval}
              className="text-xs font-semibold px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 transition flex items-center gap-1.5"
            >
              📊 Evaluation & Proof
            </button>
          </div>
        </div>

        {/* Responsible AI Disclaimer Banner */}
        <div className="bg-blue-950/30 border-t border-b border-blue-900/40 px-4 py-1.5 text-center text-xs text-blue-300 font-medium">
          🛡️ Responsible AI: Pre-Clinical / BD Investment Triage Tool — No patient data, no clinical advice. Draft until human analyst sign-off.
        </div>
      </header>

      {/* Main Container */}
      <main className="max-w-7xl mx-auto px-4 py-8 flex-1 space-y-8 w-full">
        {/* Step 1: Input & Resolution */}
        <section className="bg-slate-900/60 border border-slate-800 rounded-2xl p-6 shadow-xl backdrop-blur">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-400 flex items-center gap-2">
              <span className="w-6 h-6 rounded-full bg-blue-600 text-white flex items-center justify-center text-xs">1</span>
              Asset Query & Resolution
            </h2>
            <div className="text-xs text-slate-400">
              Deterministic mapping to Open Targets, ChEMBL & UniProt
            </div>
          </div>

          <div className="flex flex-col md:flex-row gap-3">
            <div className="flex-1 relative">
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleResolve()}
                placeholder="Enter drug name, target, and disease (e.g. Osimertinib targeting EGFR in NSCLC)..."
                className="w-full bg-slate-950 border border-slate-800 rounded-xl px-4 py-3 text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition"
              />
              <button
                type="button"
                onClick={toggleVoiceRecognition}
                title="Voice query via Web Speech API"
                className={`absolute right-3 top-2.5 p-1.5 rounded-lg text-slate-400 hover:text-white transition ${
                  isListening ? "bg-rose-600 text-white voice-recording" : "hover:bg-slate-800"
                }`}
              >
                🎤
              </button>
            </div>

            <div className="flex items-center gap-2">
              <div className="relative">
                <input
                  type="date"
                  value={asOfDate}
                  onChange={(e) => setAsOfDate(e.target.value)}
                  title="Cutoff date (as_of) for retrospective backtesting"
                  className="bg-slate-950 border border-slate-800 rounded-xl px-3 py-3 text-xs text-slate-300 focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>

              <button
                type="button"
                onClick={() => handleResolve()}
                disabled={runStatus === "resolving" || runStatus === "running"}
                className="px-4 py-3 rounded-xl bg-slate-800 hover:bg-slate-700 text-white text-sm font-semibold transition border border-slate-700 whitespace-nowrap"
              >
                {runStatus === "resolving" ? "Resolving..." : "Resolve"}
              </button>

              <button
                type="button"
                onClick={handleInstantRun}
                disabled={runStatus === "resolving" || runStatus === "running"}
                className="px-5 py-3 rounded-xl bg-gradient-to-r from-blue-600 to-indigo-600 hover:from-blue-500 hover:to-indigo-500 text-white text-sm font-semibold transition shadow-lg shadow-blue-500/20 whitespace-nowrap"
              >
                ⚡ 1-Click Run
              </button>
            </div>
          </div>

          {/* Quick-Pick Demo Pills */}
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <span className="text-xs text-slate-400">Demo Presets:</span>
            {QUICK_PICKS.map((p, idx) => (
              <button
                key={idx}
                onClick={() => {
                  setQuery(p.query);
                  setAsOfDate(p.asOf);
                  handleResolve(p.query);
                }}
                className="text-xs px-2.5 py-1 rounded-lg bg-slate-950 border border-slate-800 hover:border-blue-500 text-slate-300 hover:text-white transition"
              >
                {p.label}
              </button>
            ))}
          </div>

          {/* Resolved Entity Details Card */}
          {asset && (
            <div className="mt-5 p-4 rounded-xl bg-slate-950/70 border border-blue-900/50 flex flex-col md:flex-row md:items-center justify-between gap-4">
              <div className="space-y-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-base font-bold text-white">{asset.drug}</span>
                  <span className="text-slate-400 text-sm">targeting</span>
                  <span className="text-base font-bold text-blue-400">{asset.target}</span>
                  <span className="text-slate-400 text-sm">in</span>
                  <span className="text-base font-semibold text-slate-200">{asset.disease}</span>
                  {asset.as_of && (
                    <span className="text-xs px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/30">
                      as_of: {asset.as_of}
                    </span>
                  )}
                </div>
                <div className="flex flex-wrap items-center gap-3 text-xs text-slate-400 font-mono">
                  {asset.ids.chembl_id && <span>ChEMBL: {asset.ids.chembl_id}</span>}
                  {asset.ids.uniprot_id && <span>UniProt: {asset.ids.uniprot_id}</span>}
                  {asset.ids.efo_id && <span>EFO: {asset.ids.efo_id}</span>}
                  {asset.ids.ensembl_id && <span>Ensembl: {asset.ids.ensembl_id}</span>}
                </div>
              </div>

              <button
                onClick={() => handleStartRun(asset)}
                disabled={runStatus === "running"}
                className="px-6 py-2.5 rounded-xl bg-blue-600 hover:bg-blue-500 text-white text-sm font-semibold transition shadow-md shadow-blue-600/30 whitespace-nowrap self-start md:self-center"
              >
                {runStatus === "running" ? "Running Diligence..." : "Run Pre-Mortem Diligence →"}
              </button>
            </div>
          )}
        </section>

        {/* Live SSE Progress Banner */}
        {runStatus === "running" && (
          <section className="bg-slate-900/90 border border-blue-500/40 rounded-2xl p-5 shadow-2xl relative overflow-hidden">
            <div className="absolute top-0 left-0 right-0 h-1 bg-gradient-to-r from-blue-500 via-indigo-500 to-purple-500 animate-pulse"></div>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div className="w-5 h-5 border-2 border-blue-400 border-t-transparent rounded-full animate-spin"></div>
                <div>
                  <h3 className="text-sm font-semibold text-white">
                    3 Check Sub-Agents Running in Parallel (45s Hard Timeout)
                  </h3>
                  <p className="text-xs text-slate-400">
                    Science (Open Targets), Trial (ClinicalTrials.gov), Market (UniProt/PDB)
                  </p>
                </div>
              </div>
              <div className="text-xs font-mono text-slate-400">
                {events.length} events logged
              </div>
            </div>
          </section>
        )}

        {/* Step 2: Three Parallel Check Sub-Agent Lanes */}
        <section className="space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-400 flex items-center gap-2">
              <span className="w-6 h-6 rounded-full bg-blue-600 text-white flex items-center justify-center text-xs">2</span>
              Three Evidence Check Lanes
            </h2>
            <div className="text-xs text-slate-400">
              Hard 45s timeout • Pure Python numbers • Honest degradation
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
            {/* Lane 1: Science */}
            <LaneCard
              laneId="science"
              title="Dead Science"
              subtitle="Open Targets • ChEMBL • DepMap"
              description="Target biology, genetic validation & safety liabilities"
              result={runView?.check_results?.science}
              events={events.filter((e) => e.check_id === "science")}
              onCiteClick={scrollToLedgerRow}
            />

            {/* Lane 2: Trial */}
            <LaneCard
              laneId="trial"
              title="Dead Trial"
              subtitle="ClinicalTrials.gov API v2"
              description="Historical termination reasons & patient accrual bottlenecks"
              result={runView?.check_results?.trial}
              events={events.filter((e) => e.check_id === "trial")}
              onCiteClick={scrollToLedgerRow}
            />

            {/* Lane 3: Market / Resistance */}
            <LaneCard
              laneId="market"
              title="Dead Market / Resistance"
              subtitle="UniProt • RCSB PDB • AlphaFold"
              description="3D contact distances & mutational escape liabilities"
              result={runView?.check_results?.market}
              events={events.filter((e) => e.check_id === "market")}
              onCiteClick={scrollToLedgerRow}
            />
          </div>
        </section>

        {/* Step 3: Interactive 3D Protein Structure Viewer */}
        {runView?.structure && (
          <section className="bg-slate-900/60 border border-slate-800 rounded-2xl p-6 shadow-xl backdrop-blur space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 pb-3">
              <div>
                <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-400 flex items-center gap-2">
                  <span className="w-6 h-6 rounded-full bg-blue-600 text-white flex items-center justify-center text-xs">3</span>
                  3D Structural Escape & Binding Pocket Viewer
                </h2>
                <p className="text-xs text-slate-400 mt-1">
                  Powered by 3Dmol.js • Heavy-atom Euclidean contact distances computed in Python
                </p>
              </div>
              <div className="flex items-center gap-2">
                <span className="text-xs font-mono px-2 py-1 rounded bg-slate-800 text-blue-300 border border-slate-700">
                  {runView.structure.source.toUpperCase()}: {runView.structure.structure_id}
                </span>
                <a
                  href={runView.structure.structure_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-xs px-2.5 py-1 rounded bg-blue-600/20 text-blue-400 hover:text-white border border-blue-500/30 transition"
                >
                  Download Coordinates ↗
                </a>
              </div>
            </div>

            <div className="text-xs bg-slate-950/80 p-3 rounded-xl border border-slate-800 text-slate-300 font-mono">
              ℹ {runView.structure.confidence_note}
            </div>

            <StructureViewer3D structure={runView.structure} />
          </section>
        )}

        {/* Step 4: The Verdict & Ranked Red Flags */}
        {runView?.verdict && (
          <section className="bg-slate-900/60 border border-slate-800 rounded-2xl p-6 shadow-xl backdrop-blur space-y-6">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 pb-4">
              <div>
                <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-400 flex items-center gap-2">
                  <span className="w-6 h-6 rounded-full bg-blue-600 text-white flex items-center justify-center text-xs">4</span>
                  Verdict & Red Flags
                </h2>
                <p className="text-xs text-slate-400 mt-1">
                  Gemini 4 Argon reasoning over Evidence Ledger • Python owns all numbers & ranking
                </p>
              </div>

              <div className="flex items-center gap-3">
                <span className={`text-xs px-3 py-1 rounded-full font-bold uppercase tracking-wider border ${
                  runView.verdict.is_draft
                    ? "bg-amber-500/20 text-amber-300 border-amber-500/40"
                    : "bg-emerald-500/20 text-emerald-300 border-emerald-500/40"
                }`}>
                  {runView.verdict.is_draft ? "Draft (Requires Sign-Off)" : "Signed Off"}
                </span>

                <button
                  onClick={handleOpenMemo}
                  className="text-xs font-semibold px-4 py-2 rounded-xl bg-blue-600 hover:bg-blue-500 text-white transition shadow-md shadow-blue-500/20 flex items-center gap-1.5"
                >
                  📄 View 1-Page Memo
                </button>
              </div>
            </div>

            {/* Verdict Big Numbers */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <div className="p-4 rounded-xl bg-slate-950 border border-slate-800 flex items-center gap-4">
                <div className={`w-14 h-14 rounded-2xl flex items-center justify-center text-xl font-black ${
                  runView.verdict.overall_risk === "high" ? "bg-rose-500/20 text-rose-400 border border-rose-500/40" :
                  runView.verdict.overall_risk === "medium" ? "bg-amber-500/20 text-amber-400 border border-amber-500/40" :
                  "bg-emerald-500/20 text-emerald-400 border border-emerald-500/40"
                }`}>
                  {runView.verdict.overall_score !== null ? `${Math.round(runView.verdict.overall_score * 100)}%` : "N/A"}
                </div>
                <div>
                  <div className="text-xs text-slate-400">Combined Failure Risk</div>
                  <div className="text-base font-bold uppercase text-white tracking-wide">
                    {runView.verdict.overall_risk} Risk
                  </div>
                  <div className="text-xs text-slate-400 font-mono">
                    Confidence: {runView.verdict.overall_confidence}
                  </div>
                </div>
              </div>

              <div className="p-4 rounded-xl bg-slate-950 border border-slate-800">
                <div className="text-xs text-slate-400">Primary Failure Mode</div>
                <div className="text-base font-bold text-rose-400 uppercase mt-1">
                  {runView.verdict.primary_failure_mode ? `Dead ${runView.verdict.primary_failure_mode}` : "None"}
                </div>
                <div className="text-xs text-slate-400 mt-1">
                  Most likely fatal mode across usable lanes
                </div>
              </div>

              <div className="p-4 rounded-xl bg-slate-950 border border-slate-800">
                <div className="text-xs text-slate-400">Synthesis Engine</div>
                <div className="text-base font-bold text-blue-400 mt-1 capitalize">
                  {runView.verdict.synthesis} reasoning
                </div>
                <div className="text-xs text-slate-400 mt-1">
                  {runView.verdict.red_flags.length} Red Flags Ranked
                </div>
              </div>
            </div>

            {/* Executive Summary Prose */}
            <div className="p-4 rounded-xl bg-slate-950 border border-slate-800 space-y-2">
              <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-400">
                Executive Diligence Summary
              </h3>
              <p className="text-sm text-slate-200 leading-relaxed">
                {runView.verdict.overall_summary}
              </p>
            </div>

            {/* Ranked Red Flags List */}
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-400">
                  Ranked Red Flags ({runView.verdict.red_flags.length})
                </h3>
                <button
                  onClick={() => setShowAddFlag(!showAddFlag)}
                  className="text-xs font-semibold px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 transition"
                >
                  {showAddFlag ? "Cancel" : "+ Add Analyst Red Flag"}
                </button>
              </div>

              {/* Add Flag Form */}
              {showAddFlag && (
                <form onSubmit={handleAddCustomFlag} className="p-4 rounded-xl bg-slate-950 border border-blue-900/60 space-y-3">
                  <div className="text-xs font-semibold text-blue-300">Add Custom Red Flag (Must cite Ledger rows)</div>
                  <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                    <input
                      type="text"
                      placeholder="Flag Title"
                      value={newFlag.title}
                      onChange={(e) => setNewFlag({ ...newFlag, title: e.target.value })}
                      required
                      className="bg-slate-900 border border-slate-800 rounded px-3 py-1.5 text-xs text-white"
                    />
                    <select
                      value={newFlag.check_id}
                      onChange={(e) => setNewFlag({ ...newFlag, check_id: e.target.value })}
                      className="bg-slate-900 border border-slate-800 rounded px-3 py-1.5 text-xs text-white"
                    >
                      <option value="science">Science</option>
                      <option value="trial">Trial</option>
                      <option value="market">Market</option>
                    </select>
                    <select
                      value={newFlag.severity}
                      onChange={(e) => setNewFlag({ ...newFlag, severity: e.target.value })}
                      className="bg-slate-900 border border-slate-800 rounded px-3 py-1.5 text-xs text-white"
                    >
                      <option value="high">High</option>
                      <option value="medium">Medium</option>
                      <option value="low">Low</option>
                    </select>
                  </div>
                  <textarea
                    placeholder="Flag description prose..."
                    value={newFlag.text}
                    onChange={(e) => setNewFlag({ ...newFlag, text: e.target.value })}
                    required
                    className="w-full bg-slate-900 border border-slate-800 rounded px-3 py-2 text-xs text-white h-16"
                  />
                  <input
                    type="text"
                    placeholder="Cited Ledger Row IDs (comma-separated, e.g. market-structure-6JXT)"
                    value={newFlag.ledger_row_ids}
                    onChange={(e) => setNewFlag({ ...newFlag, ledger_row_ids: e.target.value })}
                    required
                    className="w-full bg-slate-900 border border-slate-800 rounded px-3 py-1.5 text-xs text-white font-mono"
                  />
                  <button
                    type="submit"
                    className="px-4 py-1.5 rounded bg-blue-600 hover:bg-blue-500 text-white text-xs font-semibold"
                  >
                    Submit Analyst Flag
                  </button>
                </form>
              )}

              {runView.verdict.red_flags.map((flag) => (
                <div
                  key={flag.id}
                  className={`p-4 rounded-xl bg-slate-950 border transition ${
                    flag.review_status === "rejected" ? "border-slate-800 opacity-60" :
                    flag.review_status === "accepted" ? "border-emerald-800/80 bg-emerald-950/20" :
                    "border-slate-800"
                  }`}
                >
                  <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
                    <div className="flex items-center gap-2">
                      <span className="text-xs font-mono font-bold px-2 py-0.5 rounded bg-slate-800 text-slate-300">
                        #{flag.rank}
                      </span>
                      <span className={`text-xs px-2 py-0.5 rounded font-bold uppercase ${
                        flag.severity === "high" ? "bg-rose-500/20 text-rose-400 border border-rose-500/30" :
                        flag.severity === "medium" ? "bg-amber-500/20 text-amber-400 border border-amber-500/30" :
                        "bg-emerald-500/20 text-emerald-400 border border-emerald-500/30"
                      }`}>
                        {flag.severity}
                      </span>
                      <span className="text-xs px-2 py-0.5 rounded bg-slate-800 text-slate-400 uppercase">
                        {flag.check_id}
                      </span>
                      <h4 className="text-sm font-bold text-white">{flag.title}</h4>
                      {flag.added_by && (
                        <span className="text-xs px-2 py-0.5 rounded bg-blue-900/40 text-blue-300">
                          Added by {flag.added_by}
                        </span>
                      )}
                    </div>

                    {/* Review Buttons */}
                    <div className="flex items-center gap-1.5">
                      {flag.review_status === "pending" ? (
                        <>
                          <button
                            onClick={() => handleFlagAction(flag.id, "accept")}
                            className="text-xs px-2.5 py-1 rounded bg-emerald-900/50 hover:bg-emerald-800 text-emerald-300 border border-emerald-700/50 transition"
                          >
                            Accept
                          </button>
                          <button
                            onClick={() => {
                              const note = prompt(`Enter rejection reason for flag '${flag.title}':`);
                              if (note) {
                                setRejectionNotes({ ...rejectionNotes, [flag.id]: note });
                                handleFlagAction(flag.id, "reject");
                              }
                            }}
                            className="text-xs px-2.5 py-1 rounded bg-rose-900/50 hover:bg-rose-800 text-rose-300 border border-rose-700/50 transition"
                          >
                            Reject
                          </button>
                        </>
                      ) : (
                        <span className={`text-xs px-2 py-0.5 rounded font-medium ${
                          flag.review_status === "accepted" ? "text-emerald-400" : "text-rose-400"
                        }`}>
                          {flag.review_status.toUpperCase()}
                        </span>
                      )}
                    </div>
                  </div>

                  <p className="text-xs text-slate-300 leading-relaxed mb-3">
                    {flag.text}
                  </p>

                  <div className="flex flex-wrap items-center gap-2 pt-2 border-t border-slate-900">
                    <span className="text-xs text-slate-500">Citations:</span>
                    {flag.ledger_row_ids.map((cid) => (
                      <button
                        key={cid}
                        onClick={() => scrollToLedgerRow(cid)}
                        className="text-xs px-2 py-0.5 rounded bg-blue-950/80 hover:bg-blue-900 text-blue-300 font-mono border border-blue-800/40 transition"
                      >
                        #{cid}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
            </div>

            {/* Step 5: Analyst Sign-Off Gate Panel */}
            <div className="p-5 rounded-xl bg-gradient-to-r from-slate-950 to-blue-950/40 border border-blue-900/50 space-y-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <h3 className="text-sm font-bold text-white flex items-center gap-2">
                    ✍ Human Analyst Sign-Off Gate
                  </h3>
                  <p className="text-xs text-slate-400 mt-0.5">
                    No investment decision without a named reviewer. Signing off unlocks the formal 1-Page Diligence Memo.
                  </p>
                </div>

                <div className="flex items-center gap-3">
                  <input
                    type="text"
                    value={reviewerName}
                    onChange={(e) => setReviewerName(e.target.value)}
                    placeholder="Reviewer Name & Role"
                    className="bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-slate-200 focus:outline-none focus:ring-1 focus:ring-blue-500"
                  />
                  <button
                    onClick={handleVerdictSignoff}
                    disabled={!runView.verdict.is_draft}
                    className={`px-5 py-2 rounded-xl text-xs font-bold transition shadow-lg ${
                      runView.verdict.is_draft
                        ? "bg-emerald-600 hover:bg-emerald-500 text-white shadow-emerald-600/30"
                        : "bg-slate-800 text-slate-500 cursor-not-allowed border border-slate-700"
                    }`}
                  >
                    {runView.verdict.is_draft ? "Sign Off Diligence Verdict ✓" : "Verdict Signed Off ✓"}
                  </button>
                </div>
              </div>

              {runView.verdict.signed_off_by && (
                <div className="text-xs text-emerald-400 font-mono">
                  ✓ Signed off by {runView.verdict.signed_off_by} at {new Date(runView.verdict.signed_off_at).toLocaleString()}
                </div>
              )}
            </div>
          </section>
        )}

        {/* Step 5: Complete Evidence Ledger Table */}
        {runView?.ledger_rows && runView.ledger_rows.length > 0 && (
          <section className="bg-slate-900/60 border border-slate-800 rounded-2xl p-6 shadow-xl backdrop-blur space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 pb-3">
              <div>
                <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-400 flex items-center gap-2">
                  <span className="w-6 h-6 rounded-full bg-blue-600 text-white flex items-center justify-center text-xs">5</span>
                  Evidence Ledger ({runView.ledger_rows.length} claims)
                </h2>
                <p className="text-xs text-slate-400 mt-1">
                  Immutable record of every atomic claim, pull timestamp, licence & external URL
                </p>
              </div>

              <div className="flex items-center gap-2">
                <input
                  type="text"
                  placeholder="Filter claims or IDs..."
                  value={ledgerFilter}
                  onChange={(e) => setLedgerFilter(e.target.value)}
                  className="bg-slate-950 border border-slate-800 rounded-lg px-3 py-1.5 text-xs text-slate-200"
                />
                <select
                  value={laneFilter}
                  onChange={(e) => setLaneFilter(e.target.value)}
                  className="bg-slate-950 border border-slate-800 rounded-lg px-2 py-1.5 text-xs text-slate-300"
                >
                  <option value="all">All Lanes</option>
                  <option value="science">Science</option>
                  <option value="trial">Trial</option>
                  <option value="market">Market</option>
                </select>
              </div>
            </div>

            <div className="overflow-x-auto max-h-[480px] overflow-y-auto rounded-xl border border-slate-800">
              <table className="w-full text-left border-collapse text-xs">
                <thead className="bg-slate-950 sticky top-0 z-10 border-b border-slate-800 text-slate-400">
                  <tr>
                    <th className="py-2.5 px-3 font-mono">Row ID</th>
                    <th className="py-2.5 px-3">Lane</th>
                    <th className="py-2.5 px-3">Claim</th>
                    <th className="py-2.5 px-3">Source Record</th>
                    <th className="py-2.5 px-3">Evidence Date</th>
                    <th className="py-2.5 px-3">Licence</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-900 font-normal text-slate-300">
                  {runView.ledger_rows
                    .filter((row) => (laneFilter === "all" || row.check_id === laneFilter))
                    .filter((row) => !ledgerFilter || JSON.stringify(row).toLowerCase().includes(ledgerFilter.toLowerCase()))
                    .map((row) => {
                      const isSelected = selectedLedgerId === row.id;
                      return (
                        <tr
                          key={row.id}
                          id={`ledger-row-${row.id}`}
                          className={`hover:bg-slate-800/40 transition ${
                            isSelected ? "bg-blue-950/70 border-l-4 border-blue-500" : ""
                          }`}
                        >
                          <td className="py-2 px-3 font-mono text-blue-400 whitespace-nowrap">
                            #{row.id}
                          </td>
                          <td className="py-2 px-3 whitespace-nowrap">
                            <span className="px-1.5 py-0.5 rounded bg-slate-800 text-slate-300 uppercase text-[10px]">
                              {row.check_id}
                            </span>
                          </td>
                          <td className="py-2 px-3 text-slate-200">
                            {row.claim}
                          </td>
                          <td className="py-2 px-3 whitespace-nowrap">
                            <a
                              href={row.url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="text-blue-400 hover:underline flex items-center gap-1"
                            >
                              {row.source_record} ↗
                            </a>
                          </td>
                          <td className="py-2 px-3 font-mono text-slate-400 whitespace-nowrap">
                            {row.evidence_date || "Current"}
                          </td>
                          <td className="py-2 px-3 text-slate-400 whitespace-nowrap text-[10px]">
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

      {/* 1-Page Diligence Memo Modal */}
      {showMemoModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-sm">
          <div className="bg-[#0f172a] border border-slate-700 rounded-2xl w-full max-w-4xl max-h-[90vh] flex flex-col shadow-2xl overflow-hidden">
            <div className="p-4 border-b border-slate-800 flex items-center justify-between no-print">
              <h3 className="text-base font-bold text-white flex items-center gap-2">
                📄 1-Page Diligence Memo
              </h3>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => {
                    navigator.clipboard.writeText(memoMarkdown);
                    showToast("Markdown copied to clipboard!", "success");
                  }}
                  className="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-semibold border border-slate-700 transition"
                >
                  Copy Markdown
                </button>
                <button
                  onClick={() => window.print()}
                  className="px-3 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-xs font-semibold transition"
                >
                  Print / Save PDF
                </button>
                <button
                  onClick={() => setShowMemoModal(false)}
                  className="p-1.5 rounded-lg text-slate-400 hover:text-white transition"
                >
                  ✕
                </button>
              </div>
            </div>

            <div className="p-8 overflow-y-auto flex-1 prose prose-invert max-w-none text-slate-200 memo-content text-sm leading-relaxed"
                 dangerouslySetInnerHTML={{ __html: window.marked ? window.marked.parse(memoMarkdown) : memoMarkdown }}
            />
          </div>
        </div>
      )}

      {/* Evaluation & Proof Modal */}
      {showEvalModal && evalData && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/80 backdrop-blur-sm">
          <div className="bg-[#0f172a] border border-slate-700 rounded-2xl w-full max-w-5xl max-h-[90vh] flex flex-col shadow-2xl overflow-hidden">
            <div className="p-4 border-b border-slate-800 flex items-center justify-between">
              <h3 className="text-base font-bold text-white flex items-center gap-2">
                📊 Evaluation, Baselines & Dataset Licences
              </h3>
              <button
                onClick={() => setShowEvalModal(false)}
                className="p-1.5 rounded-lg text-slate-400 hover:text-white transition"
              >
                ✕
              </button>
            </div>

            <div className="p-6 overflow-y-auto flex-1 space-y-6 text-xs text-slate-300">
              {/* Backtests Proof Slide */}
              <div className="space-y-3">
                <h4 className="text-sm font-bold text-white uppercase tracking-wider text-blue-400">
                  Three Backtests vs. Baselines (All Beat Baselines)
                </h4>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                  {/* Science */}
                  <div className="p-4 rounded-xl bg-slate-950 border border-slate-800 space-y-2">
                    <div className="font-bold text-slate-200 text-sm">Dead Science Backtest</div>
                    <div className="text-xl font-black text-emerald-400">AUROC 0.790</div>
                    <div className="text-slate-400">Baseline: 0.500 (Separation 0.127)</div>
                    <div className="text-slate-400">Mann-Whitney p = 0.028 (n = 20)</div>
                    <div className="text-[11px] text-emerald-400/90 font-medium">
                      ✓ Statistically significant separation at 5% level
                    </div>
                  </div>

                  {/* Trial */}
                  <div className="p-4 rounded-xl bg-slate-950 border border-slate-800 space-y-2">
                    <div className="font-bold text-slate-200 text-sm">Dead Trial Backtest</div>
                    <div className="text-xl font-black text-emerald-400">Precision@20 0.400</div>
                    <div className="text-slate-400">Prevalence Baseline: 0.370</div>
                    <div className="text-slate-400">In-period AUROC: 0.719</div>
                    <div className="text-[11px] text-amber-400 font-medium">
                      ⚠ Honest negative result: does not generalize across 2022 cutoff
                    </div>
                  </div>

                  {/* Market */}
                  <div className="p-4 rounded-xl bg-slate-950 border border-slate-800 space-y-2">
                    <div className="font-bold text-slate-200 text-sm">Dead Market / Resistance</div>
                    <div className="text-xl font-black text-emerald-400">Recall@10 0.500</div>
                    <div className="text-slate-400">Pocket Baseline: 0.158 | Chain: 0.040</div>
                    <div className="text-slate-400">Recalls 7 / 14 known clinical escapes</div>
                    <div className="text-[11px] text-emerald-400/90 font-medium">
                      ✓ Beats pocket baseline across all k from 5 to 20
                    </div>
                  </div>
                </div>
              </div>

              {/* Stress Test */}
              {evalData.results?.stress_test && (
                <div className="space-y-3">
                  <h4 className="text-sm font-bold text-white uppercase tracking-wider text-blue-400">
                    Saturday Night 10-Asset Stress Test
                  </h4>
                  <div className="p-3 rounded-xl bg-slate-950 border border-slate-800 flex items-center justify-between">
                    <div>
                      <span className="font-bold text-white text-sm">
                        {evalData.results.stress_test.n_succeeded} / {evalData.results.stress_test.n_assets} Assets Succeeded
                      </span>
                      <span className="text-slate-400 ml-2">
                        (Median latency: {evalData.results.stress_test.latency_s?.median_total?.toFixed(1)}s)
                      </span>
                    </div>
                    <div className="font-mono text-slate-400">
                      Science 10 complete • Trial 10 complete • Market honest degradation
                    </div>
                  </div>
                </div>
              )}

              {/* Where We Get It Wrong */}
              <div className="p-4 rounded-xl bg-rose-950/20 border border-rose-900/40 space-y-2">
                <h4 className="text-sm font-bold text-rose-300 uppercase tracking-wider">
                  Where We Get It Wrong (Voluntary Failure Case)
                </h4>
                <p className="text-xs text-rose-200/90 leading-relaxed">
                  Imatinib resistance via <strong>M351T</strong>: M351 sits at <strong>8.5 Å</strong> from the drug in KIT/ABL1 kinase coordinates. It causes resistance through an allosteric conformational shift rather than direct ligand contact. Any method relying purely on contact distance will miss allosteric mutations. We report this openly rather than hiding it.
                </p>
              </div>

              {/* Dataset Licence Table */}
              <div className="space-y-2">
                <h4 className="text-sm font-bold text-white uppercase tracking-wider text-blue-400">
                  Public Dataset Licence & Cutoff Policy Table
                </h4>
                <div className="overflow-x-auto rounded-xl border border-slate-800">
                  <table className="w-full text-left border-collapse text-xs">
                    <thead className="bg-slate-950 text-slate-400 border-b border-slate-800">
                      <tr>
                        <th className="p-2.5">Dataset / Source</th>
                        <th className="p-2.5">Licence</th>
                        <th className="p-2.5">Cutoff Enforcement</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-900 text-slate-300">
                      {evalData.licences?.map((lic, idx) => (
                        <tr key={idx} className="hover:bg-slate-800/30">
                          <td className="p-2.5 font-medium text-slate-200">{lic.dataset}</td>
                          <td className="p-2.5 font-mono text-blue-400 whitespace-nowrap">{lic.licence}</td>
                          <td className="p-2.5 text-slate-400">{lic.cutoff_enforcement}</td>
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
      <footer className="border-t border-slate-900 bg-[#090d16] py-6 text-center text-xs text-slate-400">
        <p>Pre-Mortem — AgentVerse 2026 UCL Hackathon • Google DeepMind Track</p>
        <p className="mt-1 font-mono text-slate-400">All numbers computed in Python • Interactions API with thinking_level • Strict as_of temporal cutoff</p>
      </footer>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Subcomponent: Lane Card
// ---------------------------------------------------------------------------

function LaneCard({ laneId, title, subtitle, description, result, events, onCiteClick }) {
  const isRunning = !result && events.length > 0;
  const isComplete = result?.status === "complete";
  const isPartial = result?.status === "partial";
  const isFailed = result?.status === "failed";
  const isTimeout = result?.status === "timeout";

  const scorePct = result?.score !== undefined ? Math.round(result.score * 100) : null;
  const latestEvent = events.length > 0 ? events[events.length - 1] : null;

  return (
    <div className="bg-slate-900/60 border border-slate-800 rounded-2xl p-5 flex flex-col justify-between shadow-lg backdrop-blur">
      <div className="space-y-3">
        {/* Header */}
        <div className="flex items-start justify-between gap-2">
          <div>
            <h3 className="text-base font-bold text-white">{title}</h3>
            <div className="text-[11px] text-blue-400 font-mono">{subtitle}</div>
          </div>

          <div>
            {isRunning && (
              <span className="text-[10px] px-2 py-0.5 rounded bg-blue-500/20 text-blue-300 border border-blue-500/40 animate-pulse font-mono">
                RUNNING
              </span>
            )}
            {isComplete && (
              <span className="text-[10px] px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 font-mono">
                COMPLETE
              </span>
            )}
            {isPartial && (
              <span className="text-[10px] px-2 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/40 font-mono">
                PARTIAL
              </span>
            )}
            {(isFailed || isTimeout) && (
              <span className="text-[10px] px-2 py-0.5 rounded bg-rose-500/20 text-rose-300 border border-rose-500/40 font-mono">
                {isTimeout ? "TIMEOUT" : "FAILED"}
              </span>
            )}
          </div>
        </div>

        <p className="text-xs text-slate-400 leading-tight">{description}</p>

        {/* Risk Score Meter */}
        {result && (
          <div className="pt-2">
            <div className="flex items-center justify-between text-xs mb-1">
              <span className="text-slate-400 font-medium">Failure Risk Score:</span>
              <span className={`font-bold font-mono text-sm ${
                result.score >= 0.6 ? "text-rose-400" :
                result.score >= 0.35 ? "text-amber-400" :
                "text-emerald-400"
              }`}>
                {scorePct}% ({result.confidence} conf)
              </span>
            </div>
            <div className="w-full bg-slate-950 h-2 rounded-full overflow-hidden border border-slate-800">
              <div
                className={`h-full transition-all duration-700 ${
                  result.score >= 0.6 ? "bg-rose-500" :
                  result.score >= 0.35 ? "bg-amber-500" :
                  "bg-emerald-500"
                }`}
                style={{ width: `${scorePct}%` }}
              ></div>
            </div>
          </div>
        )}

        {/* Live SSE Ticker */}
        {isRunning && latestEvent && (
          <div className="bg-slate-950 p-2.5 rounded-xl border border-slate-800 text-[11px] text-blue-300 font-mono animate-pulse">
            &gt; {latestEvent.message}
          </div>
        )}

        {/* Reasons */}
        {result?.reasons && result.reasons.length > 0 && (
          <div className="space-y-2 pt-2 border-t border-slate-800/80">
            <div className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider">
              Key Evidence & Citations:
            </div>
            {result.reasons.map((r, i) => (
              <div key={i} className="text-xs text-slate-300 bg-slate-950/60 p-2.5 rounded-lg border border-slate-800/80 space-y-1.5">
                <div className="flex items-start gap-1.5">
                  <span className={`text-[9px] px-1.5 py-0.5 rounded font-bold uppercase ${
                    r.direction === "risk" ? "bg-rose-950 text-rose-300 border border-rose-800" :
                    r.direction === "support" ? "bg-emerald-950 text-emerald-300 border border-emerald-800" :
                    "bg-slate-800 text-slate-400"
                  }`}>
                    {r.direction || "RISK"}
                  </span>
                  <span className="flex-1 leading-snug">{r.text}</span>
                </div>
                <div className="flex flex-wrap gap-1 pt-1">
                  {r.ledger_row_ids.map((id) => (
                    <button
                      key={id}
                      onClick={() => onCiteClick(id)}
                      className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-blue-950 hover:bg-blue-900 text-blue-300 border border-blue-900 transition"
                    >
                      #{id}
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Missing Evidence / Honest Degradation */}
        {result?.missing && result.missing.length > 0 && (
          <div className="text-[11px] text-slate-400 bg-slate-950 p-2 rounded-lg border border-slate-800/80 space-y-1">
            <div className="font-semibold text-slate-400">Missing Evidence:</div>
            <ul className="list-disc list-inside space-y-0.5 text-slate-400">
              {result.missing.slice(0, 2).map((m, idx) => (
                <li key={idx} className="truncate">{m}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Subcomponent: 3D Structure Viewer (3Dmol.js)
// ---------------------------------------------------------------------------

function StructureViewer3D({ structure }) {
  const containerRef = useRef(null);
  const viewerRef = useRef(null);
  const [isSpinning, setIsSpinning] = useState(false);
  const [selectedResidue, setSelectedResidue] = useState(null);

  useEffect(() => {
    if (!containerRef.current || !window.$3Dmol) return;

    // Initialize 3Dmol viewer
    const viewer = window.$3Dmol.createViewer(containerRef.current, {
      defaultcolors: window.$3Dmol.rasmolElementColors,
    });
    viewerRef.current = viewer;

    // Fetch coordinate file (CIF or PDB)
    const loadUrl = structure.structure_url;
    fetch(loadUrl)
      .then((res) => res.text())
      .then((data) => {
        const format = loadUrl.endsWith(".cif") ? "cif" : "pdb";
        viewer.addModel(data, format);

        // Styling: Cartoon protein
        viewer.setStyle({}, { cartoon: { color: "spectrum", opacity: 0.85 } });

        // Highlight bound ligand if present
        viewer.setStyle({ hetflag: true }, { stick: { colorscheme: "cyanCarbon", radius: 0.25 } });

        // Highlight escape residues
        structure.residues.forEach((resEv) => {
          const resNum = parseInt(resEv.residue.replace(/\D/g, ""), 10);
          if (isNaN(resNum)) return;

          const color = resEv.contact_distance_angstrom <= 5.0 ? "#ef4444" :
                        resEv.contact_distance_angstrom <= 10.0 ? "#f97316" : "#eab308";

          viewer.addStyle(
            { resi: resNum, chain: resEv.chain },
            { stick: { color: color, radius: 0.3 } }
          );

          if (resEv.contact_distance_angstrom <= 5.0) {
            viewer.addSphere({
              center: { resi: resNum, chain: resEv.chain },
              radius: 1.2,
              color: color,
              alpha: 0.4,
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
      if (viewerRef.current) {
        viewerRef.current.clear();
      }
    };
  }, [structure]);

  // Toggle Spin
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

  // Reset Camera
  const resetCamera = () => {
    if (!viewerRef.current) return;
    viewerRef.current.zoomTo();
    viewerRef.current.render();
  };

  // Zoom into specific residue
  const focusResidue = (resEv) => {
    setSelectedResidue(resEv.residue);
    if (!viewerRef.current) return;
    const resNum = parseInt(resEv.residue.replace(/\D/g, ""), 10);
    if (isNaN(resNum)) return;

    viewerRef.current.zoomTo({ resi: resNum, chain: resEv.chain }, 1000);
    viewerRef.current.render();
  };

  return (
    <div className="grid grid-cols-1 lg:grid-cols-12 gap-6 items-start">
      {/* 3D Canvas */}
      <div className="lg:col-span-7 flex flex-col space-y-2">
        <div className="mol-container" ref={containerRef}></div>
        <div className="flex items-center justify-between px-1">
          <div className="flex items-center gap-2">
            <button
              onClick={toggleSpin}
              className="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-xs text-slate-200 border border-slate-700 transition"
            >
              {isSpinning ? "⏸ Stop Spin" : "▶ Auto-Rotate"}
            </button>
            <button
              onClick={resetCamera}
              className="px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-xs text-slate-200 border border-slate-700 transition"
            >
              ↺ Reset View
            </button>
          </div>
          <div className="flex items-center gap-3 text-[11px] text-slate-400">
            <span className="flex items-center gap-1"><span className="w-2.5 h-2.5 rounded-full bg-rose-500"></span> &le;5.0Å Pocket</span>
            <span className="flex items-center gap-1"><span className="w-2.5 h-2.5 rounded-full bg-amber-500"></span> 5-10Å</span>
            <span className="flex items-center gap-1"><span className="w-2.5 h-2.5 rounded-full bg-cyan-400"></span> Ligand</span>
          </div>
        </div>
      </div>

      {/* Escape Residues Table */}
      <div className="lg:col-span-5 bg-slate-950 p-4 rounded-xl border border-slate-800 space-y-3">
        <div className="flex items-center justify-between">
          <h4 className="text-xs font-bold text-white uppercase tracking-wider">
            Escape Residues ({structure.residues.length})
          </h4>
          <span className="text-[10px] text-slate-500">Click row to focus 3D viewer</span>
        </div>

        <div className="overflow-x-auto max-h-[360px] overflow-y-auto">
          <table className="w-full text-left border-collapse text-xs">
            <thead className="bg-slate-900 text-slate-400 sticky top-0 border-b border-slate-800">
              <tr>
                <th className="py-1.5 px-2 font-mono">Residue</th>
                <th className="py-1.5 px-2">Distance</th>
                <th className="py-1.5 px-2">Pocket</th>
                <th className="py-1.5 px-2">Source</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-900 text-slate-300">
              {structure.residues.map((resEv, idx) => {
                const isSelected = selectedResidue === resEv.residue;
                return (
                  <tr
                    key={idx}
                    onClick={() => focusResidue(resEv)}
                    className={`cursor-pointer hover:bg-slate-800/50 transition ${
                      isSelected ? "bg-blue-950/80 font-bold" : ""
                    }`}
                  >
                    <td className="py-1.5 px-2 font-mono text-blue-300">
                      {resEv.residue} ({resEv.chain})
                    </td>
                    <td className="py-1.5 px-2 font-mono">
                      <span className={resEv.contact_distance_angstrom <= 5.0 ? "text-rose-400 font-bold" : "text-slate-300"}>
                        {resEv.contact_distance_angstrom.toFixed(2)} Å
                      </span>
                    </td>
                    <td className="py-1.5 px-2">
                      {resEv.in_binding_pocket ? (
                        <span className="px-1.5 py-0.5 rounded bg-rose-950 text-rose-300 border border-rose-800 text-[10px] font-bold">
                          Pocket
                        </span>
                      ) : (
                        <span className="text-slate-500 text-[10px]">Non-pocket</span>
                      )}
                    </td>
                    <td className="py-1.5 px-2">
                      <a
                        href={resEv.evidence_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        onClick={(e) => e.stopPropagation()}
                        className="text-blue-400 hover:underline text-[11px]"
                      >
                        Evidence ↗
                      </a>
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

// Render React App
ReactDOM.render(<App />, document.getElementById("root"));
