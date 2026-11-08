"""FastAPI backend for Pre-Mortem.

Run locally:
    .venv/bin/uvicorn api.main:app --reload --port 8000

Flow for the web app:
    POST /api/resolve                -> Asset for the user to confirm
    POST /api/run {asset}            -> {run_id}
    GET  /api/runs/{id}/events       -> SSE stream of StatusEvents, ends with `event: end`
    GET  /api/runs/{id}              -> verdict + ledger rows + check results + structure
    POST /api/runs/{id}/signoff      -> apply accept / reject / add, returns updated verdict
    GET  /api/runs/{id}/memo         -> Markdown memo (after verdict-level accept)

Runs live in memory. Restarting the server drops them (rejections persist in
data/rejections.jsonl).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Mapping

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from core.gemini_client import GeminiClient, get_gemini
from core.ledger import EvidenceLedger, LedgerError
from core.orchestrator import CheckFn, load_default_checks, run_checks
from core.resolver import ResolutionError, resolve_asset
from core.signoff import SignOffError, apply_signoff, export_diligence_memo
from core.verdict import write_verdict
from shared.config import CHECK_TIMEOUT_S
from shared.schemas import Asset, CheckResult, SignOffAction, StatusEvent, StructurePayload, VerdictReport, utc_now

log = logging.getLogger(__name__)

SSE_KEEPALIVE_S = 15.0


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = ""
    drug: str | None = None
    target: str | None = None
    disease: str | None = None
    as_of: date | None = None


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset: Asset
    timeout_s: float | None = Field(default=None, gt=0, le=600)


class RunCreated(BaseModel):
    run_id: str
    status: str


class SignOffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actions: list[SignOffAction] = Field(min_length=1)


class RunView(BaseModel):
    run_id: str
    status: Literal["running", "complete", "error"]
    asset: Asset
    error: str | None = None
    verdict: VerdictReport | None = None
    check_results: dict[str, CheckResult] = Field(default_factory=dict)
    ledger_rows: list[dict] = Field(default_factory=list)
    structure: StructurePayload | None = None
    events: int = 0


# ---------------------------------------------------------------------------
# Run state
# ---------------------------------------------------------------------------


@dataclass
class RunState:
    run_id: str
    asset: Asset
    status: str = "running"
    error: str | None = None
    events: list[StatusEvent] = field(default_factory=list)
    results: dict[str, CheckResult] = field(default_factory=dict)
    ledger: EvidenceLedger | None = None
    verdict: VerdictReport | None = None
    task: asyncio.Task | None = None
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def push(self, event: StatusEvent) -> None:
        async with self.changed:
            self.events.append(event)
            self.changed.notify_all()

    async def finish(self, status: str, error: str | None = None) -> None:
        async with self.changed:
            self.status, self.error = status, error
            self.changed.notify_all()

    @property
    def done(self) -> bool:
        return self.status != "running"

    def view(self) -> RunView:
        structure = next((r.structure for r in self.results.values() if r.structure is not None), None)
        return RunView(
            run_id=self.run_id, status=self.status, asset=self.asset, error=self.error, verdict=self.verdict,
            check_results=self.results, ledger_rows=self.ledger.to_dicts() if self.ledger else [],
            structure=structure, events=len(self.events),
        )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    *,
    checks: Mapping[str, CheckFn] | None = None,
    gemini: GeminiClient | None = None,
    rejections_path: Path | str | None = None,
    timeout_s: float = CHECK_TIMEOUT_S,
) -> FastAPI:
    """`checks=None` loads checks.<lane>.run_check at each run, so teammates' lanes appear once pushed."""
    app = FastAPI(title="Pre-Mortem API", version="0.1.0")
    origins = [o.strip() for o in os.getenv("PREMORTEM_CORS_ORIGINS", "").split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_origin_regex=None if origins else r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_methods=["*"],
        allow_headers=["*"],
    )
    runs: dict[str, RunState] = {}
    app.state.runs = runs

    def get_run(run_id: str) -> RunState:
        run = runs.get(run_id)
        if run is None:
            raise HTTPException(404, f"Unknown run {run_id}")
        return run

    async def pipeline(run: RunState, lane_timeout: float) -> None:
        try:
            lanes = dict(checks) if checks is not None else load_default_checks()
            orch = await run_checks(run.asset, run.push, checks=lanes, timeout_s=lane_timeout)
            run.results, run.ledger = orch.results, orch.ledger
            await run.push(StatusEvent(check_id="verdict", status="started", message="Writing draft verdict",
                                       timestamp=utc_now()))
            run.verdict = await write_verdict(run.asset, orch.results, orch.ledger, gemini=gemini)
            await run.push(StatusEvent(
                check_id="verdict", status="complete",
                message=f"Draft verdict: {run.verdict.overall_risk} ({run.verdict.synthesis}), "
                        f"{len(run.verdict.red_flags)} red flags", timestamp=utc_now()))
            await run.finish("complete")
        except Exception as exc:  # the stream must always terminate
            log.exception("run %s failed", run.run_id)
            await run.push(StatusEvent(check_id="orchestrator", status="error",
                                       message=f"{type(exc).__name__}: {exc}", timestamp=utc_now()))
            await run.finish("error", f"{type(exc).__name__}: {exc}")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        lanes = checks if checks is not None else load_default_checks()
        return {"ok": True, "lanes": sorted(lanes), "llm": (gemini or get_gemini()).available}

    @app.post("/api/resolve", response_model=Asset)
    async def resolve(req: ResolveRequest) -> Asset:
        if not (req.query.strip() or req.drug):
            raise HTTPException(422, "Provide a query or a drug name")
        try:
            return await resolve_asset(req.query, drug=req.drug, target=req.target, disease=req.disease,
                                       as_of=req.as_of, gemini=gemini)
        except ResolutionError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/run", response_model=RunCreated, status_code=202)
    async def start_run(req: RunRequest) -> RunCreated:
        run = RunState(run_id=uuid.uuid4().hex[:12], asset=req.asset)
        runs[run.run_id] = run
        run.task = asyncio.create_task(pipeline(run, req.timeout_s or timeout_s))
        return RunCreated(run_id=run.run_id, status=run.status)

    @app.get("/api/runs/{run_id}", response_model=RunView)
    async def get_run_view(run_id: str) -> RunView:
        return get_run(run_id).view()

    @app.get("/api/runs/{run_id}/events")
    async def stream_events(run_id: str, after: int = 0) -> StreamingResponse:
        run = get_run(run_id)

        async def gen() -> AsyncIterator[str]:
            i = max(after, 0)
            while True:
                while i < len(run.events):
                    yield _sse(run.events[i].model_dump_json(), event="status", id=str(i + 1))
                    i += 1
                if run.done:
                    yield _sse(json.dumps({"run_id": run.run_id, "status": run.status, "error": run.error}),
                               event="end")
                    return
                async with run.changed:
                    if i >= len(run.events) and not run.done:
                        try:
                            await asyncio.wait_for(run.changed.wait(), timeout=SSE_KEEPALIVE_S)
                        except asyncio.TimeoutError:
                            yield ": keepalive\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/runs/{run_id}/signoff", response_model=VerdictReport)
    async def signoff(run_id: str, req: SignOffRequest) -> VerdictReport:
        run = get_run(run_id)
        if run.verdict is None or run.ledger is None:
            raise HTTPException(409, f"Run {run_id} has no verdict yet (status {run.status})")
        async with run.lock:
            verdict = run.verdict
            try:
                for action in req.actions:
                    verdict = apply_signoff(verdict, action, run.ledger, rejections_path=rejections_path)
            except (SignOffError, LedgerError) as exc:
                raise HTTPException(400, str(exc)) from exc
            run.verdict = verdict  # all-or-nothing per request
        return verdict

    @app.get("/api/runs/{run_id}/memo", response_class=PlainTextResponse)
    async def memo(run_id: str) -> PlainTextResponse:
        run = get_run(run_id)
        if run.verdict is None or run.ledger is None:
            raise HTTPException(409, "No verdict yet")
        try:
            text = export_diligence_memo(run.verdict, run.ledger)
        except SignOffError as exc:
            raise HTTPException(409, str(exc)) from exc
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")

    @app.get("/api/eval")
    async def get_eval() -> dict[str, Any]:
        results_path = Path(__file__).resolve().parent.parent / "eval" / "results.json"
        raw_results: dict[str, Any] = {}
        if results_path.exists():
            try:
                raw_results = json.loads(results_path.read_text(encoding="utf-8"))
            except Exception as err:
                log.warning("Could not read results.json: %s", err)

        from eval.cohorts import DATASET_LICENCES

        licences = [
            {
                "dataset": lic.dataset,
                "source": lic.source,
                "licence": lic.licence,
                "cutoff_enforcement": lic.cutoff_enforcement,
                "used_by": list(lic.used_by),
            }
            for lic in DATASET_LICENCES
        ]
        return {
            "results": raw_results,
            "licences": licences,
        }

    web_dir = Path(__file__).resolve().parent.parent / "web"
    if (web_dir / "index.html").exists():
        @app.get("/", response_class=FileResponse)
        async def serve_index() -> FileResponse:
            return FileResponse(web_dir / "index.html")

        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")

    return app


def _sse(data: str, *, event: str | None = None, id: str | None = None) -> str:
    out = []
    if id is not None:
        out.append(f"id: {id}")
    if event:
        out.append(f"event: {event}")
    out.extend(f"data: {line}" for line in data.splitlines() or [""])
    return "\n".join(out) + "\n\n"


app = create_app()
