"""Verdict writer guards and sign-off flow.

Ledger rows here are test fixtures for exercising validation logic; product
code only ever builds rows from live API responses.
"""

import json
import os
from datetime import date

import pytest

from core.gemini_client import GeminiClient, GeminiError, GeminiReply
from core.ledger import EvidenceLedger
from core.signoff import SignOffError, apply_signoff, export_diligence_memo, load_rejections
from core.verdict import combined_risk, write_verdict
from shared.config import GEMINI_API_KEY
from shared.schemas import (
    Asset,
    CheckResult,
    LedgerRow,
    NewFlag,
    Reason,
    SignOffAction,
    utc_now,
)

ASSET = Asset(drug="DRUGX", target="TGT1", disease="disease y", as_of=date(2021, 6, 1),
              ids={"chembl_id": "CHEMBL0"})


def _row(rid, check, claim):
    return LedgerRow(id=rid, claim=claim, source_record=f"REC:{rid}", url=f"https://example.org/{rid}",
                     date_pulled=utc_now(), licence="CC0 1.0" if check != "market" else "CC BY 4.0",
                     check_id=check)


def _results():
    sci_rows = [_row("sci-1", "science", "Genetic association score 0.12 for TGT1 in disease y"),
                _row("sci-2", "science", "Target has an approved small-molecule ligand")]
    trial_rows = [_row("trial-1", "trial", "Phase 2 trial terminated for lack of efficacy")]
    return {
        "science": CheckResult(check_id="science", score=0.7, confidence="medium", status="complete",
                               reasons=[Reason(text="Weak human genetic support (score 0.12).",
                                               ledger_row_ids=["sci-1"], direction="risk"),
                                        Reason(text="Target is tractable.", ledger_row_ids=["sci-2"],
                                               direction="support")],
                               missing=["No animal model evidence"], ledger_rows=sci_rows),
        "trial": CheckResult(check_id="trial", score=0.5, confidence="medium", status="complete",
                             reasons=[Reason(text="A prior Phase 2 was terminated for efficacy.",
                                             ledger_row_ids=["trial-1"], direction="risk")],
                             missing=[], ledger_rows=trial_rows),
        "market": CheckResult(check_id="market", score=0.0, confidence="low", status="timeout",
                              reasons=[], missing=["market check timed out after 45s"], ledger_rows=[]),
    }


def _ledger(results):
    ledger = EvidenceLedger(as_of=ASSET.as_of)
    for r in results.values():
        ledger.add_result(r)
    return ledger


class ScriptedGemini(GeminiClient):
    """Returns a fixed JSON reply; stands in for the model transport."""

    def __init__(self, data=None, exc=None):
        super().__init__(api_key="unused")
        self.data, self.exc, self.prompts = data, exc, []

    async def generate(self, input, **kw):
        self.prompts.append((input, kw))
        if self.exc:
            raise self.exc
        return GeminiReply(text=json.dumps(self.data), interaction_id="v1_scripted", model="gemini-4-argon",
                           data=self.data)


def test_combined_risk_is_independent_failure_probability():
    assert combined_risk([0.7, 0.5]) == pytest.approx(0.85)
    assert combined_risk([]) is None


async def test_llm_hallucinations_are_stripped_and_numbers_are_python_owned():
    results = _results()
    ledger = _ledger(results)
    gemini = ScriptedGemini({
        "overall_summary": "Risky.",
        "check_summaries": [{"check_id": "science", "summary": "Genetics are weak.", "ledger_row_ids": ["sci-1"]}],
        "red_flags": [
            {"check_id": "science", "severity": "high", "title": "Weak genetics",
             "text": "Genetic association is only 0.12.", "ledger_row_ids": ["sci-1", "ghost-99"]},
            {"check_id": "trial", "severity": "high", "title": "Invented stat",
             "text": "Trials like this fail 87% of the time.", "ledger_row_ids": ["trial-1"]},
            {"check_id": "market", "severity": "high", "title": "Hallucinated",
             "text": "Payers hate it.", "ledger_row_ids": ["mkt-1"]},
            {"check_id": "trial", "severity": "medium", "title": "Prior failure",
             "text": "A prior Phase 2 was terminated.", "ledger_row_ids": ["trial-1"]},
        ],
    })
    v = await write_verdict(ASSET, results, ledger, previous_interaction_id="v1_prev", gemini=gemini)

    assert v.is_draft is True
    assert v.synthesis == "llm" and v.interaction_id == "v1_scripted"
    assert gemini.prompts[0][1]["previous_interaction_id"] == "v1_prev"
    assert v.overall_score == pytest.approx(0.85)
    assert v.overall_risk == "high"
    assert v.primary_failure_mode == "science"
    titles = [f.title for f in v.red_flags]
    assert titles == ["Weak genetics", "Prior failure"]  # invented stat and hallucinated flag dropped
    assert v.red_flags[0].ledger_row_ids == ["sci-1"]  # ghost id stripped
    assert [f.rank for f in v.red_flags] == [1, 2]
    for f in v.red_flags:
        ledger.validate_ids(f.ledger_row_ids)
    assert any("market check timed out" in m for m in v.missing)

    prompt = gemini.prompts[0][0]
    assert "example.org" not in prompt  # model gets claims, not URLs or outside context
    assert "sci-1" in prompt and "0.70" in prompt


async def test_llm_failure_falls_back_to_deterministic():
    results = _results()
    ledger = _ledger(results)
    v = await write_verdict(ASSET, results, ledger, gemini=ScriptedGemini(exc=GeminiError("503")))
    assert v.synthesis == "deterministic" and v.is_draft
    assert [f.check_id for f in v.red_flags] == ["science", "trial"]
    assert "Target is tractable." not in [f.text for f in v.red_flags]  # support reasons are not flags
    assert v.overall_score == pytest.approx(0.85)


async def test_no_usable_lanes_gives_insufficient_evidence():
    results = {k: CheckResult(check_id=k, score=0.0, confidence="low", status="failed", reasons=[],
                              missing=[f"{k} down"], ledger_rows=[]) for k in ("science", "trial", "market")}
    v = await write_verdict(ASSET, results, EvidenceLedger(), gemini=GeminiClient(api_key=""))
    assert v.overall_risk == "insufficient_evidence" and v.overall_score is None
    assert v.red_flags == [] and v.primary_failure_mode is None


async def test_signoff_flow_and_memo(tmp_path):
    results = _results()
    ledger = _ledger(results)
    v = await write_verdict(ASSET, results, ledger, gemini=GeminiClient(api_key=""))
    log = tmp_path / "rejections.jsonl"

    with pytest.raises(SignOffError):
        export_diligence_memo(v, ledger)
    with pytest.raises(ValueError):
        SignOffAction(reviewer="zoro", action="reject", red_flag_id="flag-2")  # no reason

    v = apply_signoff(v, SignOffAction(reviewer="zoro", action="accept", red_flag_id="flag-1"), ledger,
                      rejections_path=log)
    v = apply_signoff(v, SignOffAction(reviewer="zoro", action="reject", red_flag_id="flag-2",
                                       note="Trial was stopped for funding, not efficacy"), ledger,
                      rejections_path=log)
    v = apply_signoff(v, SignOffAction(reviewer="zoro", action="add", new_flag=NewFlag(
        check_id="science", severity="high", title="Analyst: tractable but undruggable pocket",
        text="Ligand exists but not in disease-relevant isoform.", ledger_row_ids=["sci-2"])), ledger,
                      rejections_path=log)
    with pytest.raises(Exception):
        apply_signoff(v, SignOffAction(reviewer="zoro", action="add", new_flag=NewFlag(
            check_id="science", severity="low", title="x", text="x", ledger_row_ids=["not-in-ledger"])),
                      ledger, rejections_path=log)
    assert v.is_draft is True

    v = apply_signoff(v, SignOffAction(reviewer="zoro", action="accept"), ledger, rejections_path=log)
    assert v.is_draft is False and v.signed_off_by == "zoro"
    assert [a.action for a in v.sign_off_log] == ["accept", "reject", "add", "accept"]
    with pytest.raises(SignOffError):
        apply_signoff(v, SignOffAction(reviewer="zoro", action="comment"), ledger, rejections_path=log)

    rejections = load_rejections(log)
    assert len(rejections) == 1
    assert rejections[0]["reason"].startswith("Trial was stopped")
    assert rejections[0]["evidence"][0]["id"] == "trial-1"

    memo = export_diligence_memo(v, ledger)
    assert "# Bindr diligence memo: DRUGX" in memo
    assert "Analyst: tractable" in memo and "added by zoro" in memo
    assert "~~" in memo  # rejected flag shown struck through
    assert "https://example.org/sci-1" in memo and "https://example.org/sci-2" in memo
    assert "## Dataset licences" in memo and "| example.org | CC0 1.0 |" in memo
    assert "not assessed" in memo  # market lane timed out


@pytest.mark.live
@pytest.mark.skipif(not GEMINI_API_KEY or os.getenv("PREMORTEM_OFFLINE"), reason="needs GEMINI_API_KEY")
async def test_live_verdict_respects_ledger():
    results = _results()
    ledger = _ledger(results)
    v = await write_verdict(ASSET, results, ledger, timeout_s=60)
    assert v.is_draft is True
    assert v.overall_score == pytest.approx(0.85)
    for f in v.red_flags:
        assert f.ledger_row_ids and all(i in ledger for i in f.ledger_row_ids)


async def test_memo_turns_inline_ledger_ids_into_footnotes():
    results = _results()
    ledger = _ledger(results)
    v = await write_verdict(ASSET, results, ledger, gemini=GeminiClient(api_key=""))
    v = v.model_copy(update={"overall_summary": "Genetics are weak (sci-1) and a trial stopped (trial-1, sci-2)."})
    v = apply_signoff(v, SignOffAction(reviewer="zoro", action="accept"), ledger)
    memo = export_diligence_memo(v, ledger)
    assert "(sci-1)" not in memo and "(trial-1," not in memo
    assert "weak [1] and a trial stopped [2][3]." in memo
    assert "https://example.org/sci-2" in memo  # inline-only citation still listed in Sources
