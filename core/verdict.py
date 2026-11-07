"""Verdict writer: ledger + Python scores -> draft VerdictReport.

What the model sees: the asset names, each lane's Python-computed score /
status / confidence / missing / reasons, and the ledger rows (id, lane,
claim, source). Nothing else. It writes prose and proposes red flags.

What Python owns:
- overall_score = 1 - prod(1 - score) over usable lanes (complete/partial),
  i.e. the chance at least one failure mode bites if lanes were independent.
- overall_risk band, overall_confidence, primary_failure_mode.
- red-flag ranking (severity, then lane score), IDs and rank numbers.
- citation checks: IDs not in the ledger are stripped; a flag left with no
  valid citation is dropped.
- number checks: model text containing a number that is not in the ledger,
  the asset, or the Python scores is discarded for the deterministic text.
- is_draft is always True on output.

If the model call fails, times out or returns nothing usable, a
deterministic synthesiser builds the same report from CheckResult.reasons.
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Iterable

from core.gemini_client import GeminiClient, GeminiError, get_gemini
from core.ledger import EvidenceLedger
from shared.config import CHECK_IDS
from shared.schemas import (
    Asset,
    CheckResult,
    CheckSummary,
    Confidence,
    RedFlag,
    RiskLevel,
    VerdictReport,
)

log = logging.getLogger(__name__)

HIGH_RISK = 0.6
MEDIUM_RISK = 0.35
MAX_RED_FLAGS = 6
LLM_TIMEOUT_S = 45.0

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


# ---------------------------------------------------------------------------
# Deterministic numbers
# ---------------------------------------------------------------------------


def usable_results(check_results: dict[str, CheckResult]) -> dict[str, CheckResult]:
    return {k: r for k, r in check_results.items() if r.status in ("complete", "partial")}


def combined_risk(scores: Iterable[float]) -> float | None:
    scores = list(scores)
    if not scores:
        return None
    return 1.0 - math.prod(1.0 - s for s in scores)


def risk_band(score: float) -> RiskLevel:
    if score >= HIGH_RISK:
        return "high"
    if score >= MEDIUM_RISK:
        return "medium"
    return "low"


def overall_confidence(check_results: dict[str, CheckResult]) -> Confidence:
    usable = usable_results(check_results)
    if not usable:
        return "low"
    levels = [r.confidence for r in usable.values()]
    if len(usable) == len(check_results) and all(c == "high" for c in levels) and \
            all(r.status == "complete" for r in usable.values()):
        return "high"
    if len(usable) >= 2 and "low" not in levels:
        return "medium"
    return "low"


def primary_failure_mode(check_results: dict[str, CheckResult]) -> str | None:
    usable = usable_results(check_results)
    if not usable:
        return None
    order = {c: i for i, c in enumerate(CHECK_IDS)}
    return sorted(usable.values(), key=lambda r: (-r.score, order.get(r.check_id, 99)))[0].check_id


def fmt_score(score: float) -> str:
    return f"{score:.2f}"


# ---------------------------------------------------------------------------
# Number guard
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"(?<![A-Za-z_])\d+(?:[.,]\d+)?")


def _norm(num: str) -> str:
    num = num.replace(",", "")
    try:
        value = float(num)
    except ValueError:
        return num
    return f"{value:.6g}"


def allowed_numbers(asset: Asset, check_results: dict[str, CheckResult], ledger: EvidenceLedger) -> set[str]:
    texts = [asset.drug, asset.target, asset.disease, *asset.ids.values()]
    for row in ledger:
        texts += [row.claim, row.source_record]
    for r in check_results.values():
        texts += [x.text for x in r.reasons] + list(r.missing)
    allowed = {_norm(n) for t in texts for n in _NUM_RE.findall(t or "")}
    for r in check_results.values():
        allowed |= {_norm(fmt_score(r.score)), _norm(str(round(r.score * 100)))}
    overall = combined_risk(r.score for r in usable_results(check_results).values())
    if overall is not None:
        allowed |= {_norm(fmt_score(overall)), _norm(str(round(overall * 100)))}
    allowed |= {str(i) for i in range(0, 11)}  # small counts ("two lanes", "3 checks")
    return allowed


def numbers_ok(text: str, allowed: set[str]) -> bool:
    return all(_norm(n) in allowed for n in _NUM_RE.findall(text or ""))


# ---------------------------------------------------------------------------
# Deterministic synthesis
# ---------------------------------------------------------------------------


def _check_head(r: CheckResult) -> str:
    return (f"{r.check_id.capitalize()} failure risk {fmt_score(r.score)} "
            f"({risk_band(r.score)}, {r.confidence} confidence, {r.status}).")


def _check_summary_text(r: CheckResult) -> str:
    if r.status in ("timeout", "failed"):
        return f"{r.check_id.capitalize()} check {r.status}: " + ("; ".join(r.missing) or "no evidence collected") + "."
    head = _check_head(r)
    risks = [x.text for x in r.reasons if x.direction != "support"][:2]
    support = [x.text for x in r.reasons if x.direction == "support"][:1]
    parts = [head] + [f"Risk: {t}" for t in risks] + [f"In favour: {t}" for t in support]
    return " ".join(parts)


def _overall_summary_text(asset: Asset, check_results: dict[str, CheckResult], score: float | None,
                          primary: str | None) -> str:
    if score is None:
        return (f"No check produced usable evidence for {asset.drug} ({asset.target}, {asset.disease}). "
                "No risk call is possible; see missing evidence.")
    usable = usable_results(check_results)
    down = sorted(set(check_results) - set(usable))
    s = (f"{asset.drug} targeting {asset.target} in {asset.disease}: combined failure risk {fmt_score(score)} "
         f"({risk_band(score)}) across {len(usable)} of {len(check_results)} checks. "
         f"Most likely failure mode: {primary}.")
    if down:
        s += f" Not assessed: {', '.join(down)}."
    return s


def deterministic_flags(check_results: dict[str, CheckResult]) -> list[dict]:
    flags = []
    for r in usable_results(check_results).values():
        for reason in r.reasons:
            if reason.direction == "support":
                continue
            flags.append({
                "check_id": r.check_id,
                "severity": risk_band(r.score),
                "title": _title(reason.text),
                "text": reason.text,
                "ledger_row_ids": list(reason.ledger_row_ids),
                "_lane_score": r.score,
            })
    return flags


def _title(text: str, words: int = 9) -> str:
    first = re.split(r"(?<=[.;:])\s", text.strip(), maxsplit=1)[0].rstrip(".;:")
    tokens = first.split()
    return " ".join(tokens[:words]) + ("..." if len(tokens) > words else "")


# ---------------------------------------------------------------------------
# LLM synthesis
# ---------------------------------------------------------------------------

_SYSTEM = """You are the verdict writer for Pre-Mortem, a biotech investment triage tool.
You receive ONLY an evidence ledger and scores that Python already computed.
Rules:
- Use only facts stated in ledger rows. Do not use outside knowledge.
- Every red flag and summary must cite ledger row IDs from the input, exactly as written, in its
  ledger_row_ids field. Do not write ledger IDs inside the prose.
- Never introduce a number that is not in the input. Do not compute new numbers. You may restate a given score.
- Red flags are reasons the asset could FAIL. Do not list strengths as red flags.
- If a check timed out or failed, say it was not assessed. Do not guess its result.
- Scores and bands are printed next to your text, so do not restate them. Explain why, from the evidence.
- overall_summary: two or three sentences. Each check summary: one or two sentences.
- Be terse and specific. One or two sentences per flag."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_summary": {"type": "string"},
        "check_summaries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "check_id": {"type": "string", "enum": list(CHECK_IDS)},
                    "summary": {"type": "string"},
                    "ledger_row_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["check_id", "summary", "ledger_row_ids"],
            },
        },
        "red_flags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "check_id": {"type": "string", "enum": list(CHECK_IDS)},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "title": {"type": "string"},
                    "text": {"type": "string"},
                    "ledger_row_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["check_id", "severity", "title", "text", "ledger_row_ids"],
            },
        },
    },
    "required": ["overall_summary", "check_summaries", "red_flags"],
}


def build_prompt(asset: Asset, check_results: dict[str, CheckResult], ledger: EvidenceLedger,
                 overall: float | None, primary: str | None) -> str:
    payload = {
        "asset": {"drug": asset.drug, "target": asset.target, "disease": asset.disease},
        "python_computed": {
            "overall_failure_risk": fmt_score(overall) if overall is not None else None,
            "overall_band": risk_band(overall) if overall is not None else "insufficient_evidence",
            "primary_failure_mode": primary,
        },
        "checks": [
            {
                "check_id": r.check_id,
                "score": fmt_score(r.score),
                "band": risk_band(r.score),
                "status": r.status,
                "confidence": r.confidence,
                "missing": r.missing,
                "reasons": [{"text": x.text, "direction": x.direction, "ledger_row_ids": x.ledger_row_ids}
                            for x in r.reasons],
            }
            for r in check_results.values()
        ],
        "ledger": [{"id": row.id, "check_id": row.check_id, "claim": row.claim, "source": row.source_record}
                   for row in ledger],
    }
    return ("Write the draft verdict for this asset. Propose at most "
            f"{MAX_RED_FLAGS} red flags, most serious first.\n\n" + json.dumps(payload, indent=1))


async def _llm_draft(gemini: GeminiClient, prompt: str, previous_interaction_id: str | None,
                     timeout_s: float) -> tuple[dict, str | None]:
    reply = await gemini.generate(
        prompt, role="lead_reasoning", thinking_level="medium", system_instruction=_SYSTEM,
        json_schema=_SCHEMA, previous_interaction_id=previous_interaction_id, timeout_s=timeout_s,
    )
    if not isinstance(reply.data, dict):
        raise GeminiError("verdict reply was not a JSON object")
    return reply.data, reply.interaction_id


# ---------------------------------------------------------------------------
# Assembly and validation
# ---------------------------------------------------------------------------


def _clean_flags(raw_flags: list[dict], ledger: EvidenceLedger, check_results: dict[str, CheckResult],
                 allowed: set[str]) -> list[dict]:
    usable = usable_results(check_results)
    out = []
    for f in raw_flags:
        ids = [i for i in dict.fromkeys(f.get("ledger_row_ids") or []) if i in ledger]
        if not ids:
            log.info("dropping red flag with no valid citations: %r", f.get("title"))
            continue
        text, title = str(f.get("text") or "").strip(), str(f.get("title") or "").strip()
        if not text or not numbers_ok(text + " " + title, allowed):
            log.info("dropping red flag with unsupported numbers or empty text: %r", title)
            continue
        cited_lanes = [ledger.get(i).check_id for i in ids]
        check_id = f.get("check_id") if f.get("check_id") in cited_lanes else max(set(cited_lanes),
                                                                                   key=cited_lanes.count)
        if check_id not in usable:
            continue
        severity = f.get("severity") if f.get("severity") in _SEVERITY_ORDER else risk_band(usable[check_id].score)
        out.append({"check_id": check_id, "severity": severity, "title": title or _title(text), "text": text,
                    "ledger_row_ids": ids, "_lane_score": usable[check_id].score})
    return out


def rank_flags(flags: list[dict]) -> list[RedFlag]:
    order = {c: i for i, c in enumerate(CHECK_IDS)}
    seen: set[str] = set()
    unique = []
    for f in flags:
        key = f["text"].strip().lower()
        if key not in seen:
            seen.add(key)
            unique.append(f)
    ranked = sorted(
        enumerate(unique),
        key=lambda p: (_SEVERITY_ORDER[p[1]["severity"]], -p[1].get("_lane_score", 0.0),
                       order.get(p[1]["check_id"], 99), p[0]),
    )[:MAX_RED_FLAGS]
    return [
        RedFlag(id=f"flag-{i}", rank=i, check_id=f["check_id"], severity=f["severity"], title=f["title"],
                text=f["text"], ledger_row_ids=f["ledger_row_ids"])
        for i, (_, f) in enumerate(ranked, start=1)
    ]


async def write_verdict(
    asset: Asset,
    check_results: dict[str, CheckResult],
    ledger: EvidenceLedger,
    previous_interaction_id: str | None = None,
    *,
    gemini: GeminiClient | None = None,
    timeout_s: float = LLM_TIMEOUT_S,
) -> VerdictReport:
    gemini = gemini if gemini is not None else get_gemini()
    usable = usable_results(check_results)
    overall = combined_risk(r.score for r in usable.values())
    primary = primary_failure_mode(check_results)
    allowed = allowed_numbers(asset, check_results, ledger)

    llm: dict | None = None
    interaction_id: str | None = None
    if gemini.available and usable and len(ledger):
        try:
            llm, interaction_id = await _llm_draft(
                gemini, build_prompt(asset, check_results, ledger, overall, primary),
                previous_interaction_id, timeout_s,
            )
        except GeminiError as exc:
            log.warning("verdict LLM failed, using deterministic synthesis: %s", exc)

    # Check summaries: Python numbers always; model prose only if it passes the guards.
    llm_summaries = {s.get("check_id"): s for s in (llm or {}).get("check_summaries") or []}
    summaries = []
    for cid, r in check_results.items():
        text = _check_summary_text(r)
        ids = list(dict.fromkeys(i for x in r.reasons for i in x.ledger_row_ids))
        cand = llm_summaries.get(cid)
        if cand and r.status in ("complete", "partial") and numbers_ok(cand.get("summary", ""), allowed):
            cand_ids = [i for i in cand.get("ledger_row_ids") or [] if i in ledger]
            if cand.get("summary", "").strip():
                text = f"{_check_head(r)} {cand['summary'].strip()}"
                ids = cand_ids or ids
        summaries.append(CheckSummary(check_id=cid, score=r.score, confidence=r.confidence, status=r.status,
                                      summary=text, ledger_row_ids=ids, missing=list(r.missing)))

    flags_raw = _clean_flags((llm or {}).get("red_flags") or [], ledger, check_results, allowed) if llm else []
    synthesis = "llm" if llm and flags_raw else "deterministic"
    if not flags_raw:
        flags_raw = _clean_flags(deterministic_flags(check_results), ledger, check_results, allowed)
    red_flags = rank_flags(flags_raw)

    summary = _overall_summary_text(asset, check_results, overall, primary)
    llm_summary = str((llm or {}).get("overall_summary") or "").strip()
    if synthesis == "llm" and llm_summary and numbers_ok(llm_summary, allowed):
        summary = f"{summary} {llm_summary}"

    missing = [f"{cid}: {m}" for cid, r in check_results.items() for m in r.missing]

    verdict = VerdictReport(
        asset=asset,
        overall_risk=risk_band(overall) if overall is not None else "insufficient_evidence",
        overall_summary=summary,
        overall_score=round(overall, 4) if overall is not None else None,
        overall_confidence=overall_confidence(check_results),
        primary_failure_mode=primary,
        check_summaries=summaries,
        red_flags=red_flags,
        missing=missing,
        is_draft=True,
        synthesis=synthesis,
        interaction_id=interaction_id,
    )
    ledger.validate_verdict(verdict)
    return verdict
