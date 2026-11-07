"""Shared contracts for every workstream.

Changes here affect the whole team. Keep them additive and announce them.

Conventions
- `CheckResult.score` is failure risk in [0, 1]: 0 = no sign of this failure
  mode, 1 = near-certain failure. Degraded results (timeout/failed) carry
  score 0.0 and confidence "low"; consumers must check `status` before
  trusting a score.
- Timestamps are timezone-aware UTC (`utc_now()`).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CheckId = Literal["science", "trial", "market"]
Confidence = Literal["high", "medium", "low"]
CheckStatus = Literal["complete", "partial", "failed", "timeout"]
RiskLevel = Literal["high", "medium", "low"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _require_ids(ids: list[str], what: str) -> list[str]:
    cleaned = [i for i in ids if i and i.strip()]
    if not cleaned:
        raise ValueError(f"{what} must cite at least one ledger_row_id")
    return cleaned


# --------------------------------------------------------------------------
# Input
# --------------------------------------------------------------------------


class Asset(_Model):
    drug: str
    target: str
    disease: str
    # Resolved identifiers, e.g. {"chembl": "CHEMBL25", "ensembl": "ENSG...", "efo": "EFO_..."}
    ids: dict[str, str] = Field(default_factory=dict)
    # Evidence cutoff. Nothing dated after this may enter the ledger.
    as_of: date | None = None


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------


class LedgerRow(_Model):
    id: str
    claim: str
    source_record: str  # e.g. "NCT01234567", "PMID:123456", "CHEMBL25"
    url: str
    date_pulled: datetime
    licence: str  # e.g. "CC0", "CC-BY-4.0", "public domain (US gov)"
    check_id: CheckId
    # Date the evidence itself was published/updated. Used to enforce Asset.as_of.
    evidence_date: date | None = None


class Reason(_Model):
    text: str
    ledger_row_ids: list[str]

    @field_validator("ledger_row_ids")
    @classmethod
    def _at_least_one(cls, v: list[str]) -> list[str]:
        return _require_ids(v, "Reason")


class CheckResult(_Model):
    check_id: CheckId
    score: float = Field(ge=0.0, le=1.0)
    confidence: Confidence
    status: CheckStatus
    reasons: list[Reason]
    missing: list[str]
    ledger_rows: list[LedgerRow]

    @model_validator(mode="after")
    def _reasons_cite_own_rows(self) -> "CheckResult":
        row_ids = {r.id for r in self.ledger_rows}
        for row in self.ledger_rows:
            if row.check_id != self.check_id:
                raise ValueError(
                    f"LedgerRow {row.id!r} has check_id {row.check_id!r}, expected {self.check_id!r}"
                )
        for reason in self.reasons:
            unknown = [i for i in reason.ledger_row_ids if i not in row_ids]
            if unknown:
                raise ValueError(f"Reason cites ledger rows not in this result: {unknown}")
        return self


class StatusEvent(_Model):
    check_id: str  # a CheckId, or "orchestrator" / "verdict"
    status: str  # e.g. "started", "running", "complete", "error"
    message: str
    timestamp: datetime = Field(default_factory=utc_now)


# --------------------------------------------------------------------------
# Science lane: structure view
# --------------------------------------------------------------------------


class ResidueEvidence(_Model):
    residue: str  # e.g. "T790"
    chain: str
    contact_distance_angstrom: float
    variant_frequency: float | None = None
    in_binding_pocket: bool
    evidence_url: str
    ledger_row_id: str


class StructurePayload(_Model):
    source: Literal["pdb", "alphafold"]
    structure_id: str
    structure_url: str
    confidence_note: str
    residues: list[ResidueEvidence]


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------


class RedFlag(_Model):
    id: str
    rank: int = Field(ge=1)  # 1 = most serious
    check_id: CheckId
    severity: RiskLevel
    title: str
    text: str
    ledger_row_ids: list[str]

    @field_validator("ledger_row_ids")
    @classmethod
    def _at_least_one(cls, v: list[str]) -> list[str]:
        return _require_ids(v, "RedFlag")


class CheckSummary(_Model):
    check_id: CheckId
    score: float = Field(ge=0.0, le=1.0)
    confidence: Confidence
    status: CheckStatus
    summary: str
    ledger_row_ids: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class SignOffAction(_Model):
    reviewer: str
    action: Literal["approve", "reject", "amend", "comment"]
    note: str = ""
    # Optional pointer to the red flag the action concerns.
    red_flag_id: str | None = None
    timestamp: datetime = Field(default_factory=utc_now)


class VerdictReport(_Model):
    asset: Asset
    overall_risk: RiskLevel
    overall_summary: str
    # The check most likely to kill the asset (highest risk among usable lanes).
    primary_failure_mode: CheckId | None = None
    check_summaries: list[CheckSummary]
    red_flags: list[RedFlag]  # ordered by rank
    missing: list[str] = Field(default_factory=list)
    is_draft: bool = True
    sign_off_log: list[SignOffAction] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _ranked_and_unique(self) -> "VerdictReport":
        ranks = [f.rank for f in self.red_flags]
        if ranks != sorted(ranks) or len(set(ranks)) != len(ranks):
            raise ValueError("red_flags must be ordered by unique ascending rank")
        ids = [s.check_id for s in self.check_summaries]
        if len(set(ids)) != len(ids):
            raise ValueError("check_summaries must have one entry per check_id")
        return self


__all__ = [
    "Asset",
    "CheckId",
    "CheckResult",
    "CheckStatus",
    "CheckSummary",
    "Confidence",
    "LedgerRow",
    "Reason",
    "RedFlag",
    "ResidueEvidence",
    "RiskLevel",
    "SignOffAction",
    "StatusEvent",
    "StructurePayload",
    "VerdictReport",
    "utc_now",
]
