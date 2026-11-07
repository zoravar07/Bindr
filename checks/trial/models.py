"""Data models for parsed ClinicalTrials.gov records and fetch results."""

from __future__ import annotations
from datetime import date, datetime
from typing import Literal
from pydantic import BaseModel, Field

from shared.schemas import Asset, LedgerRow


class ClinicalTrialRecord(BaseModel):
    nct_id: str
    title: str = ""
    overall_status: str = "UNKNOWN"
    why_stopped: str | None = None
    phases: list[str] = Field(default_factory=list)
    study_type: str = "INTERVENTIONAL"
    start_date: str | None = None
    completion_date: str | None = None
    status_verified_date: str | None = None
    enrollment_count: int | None = None
    enrollment_type: str | None = None
    lead_sponsor: str | None = None
    interventions: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    url: str

    @property
    def is_stopped(self) -> bool:
        return self.overall_status.upper() in {"TERMINATED", "WITHDRAWN", "SUSPENDED"}


class TrialFetchResult(BaseModel):
    asset: Asset
    status: Literal["complete", "partial", "failed", "timeout"]
    direct_trials: list[ClinicalTrialRecord] = Field(default_factory=list)
    drug_trials: list[ClinicalTrialRecord] = Field(default_factory=list)
    disease_benchmark_trials: list[ClinicalTrialRecord] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    ledger_rows: list[LedgerRow] = Field(default_factory=list)
    as_of: date | None = None
    date_pulled: datetime
    licence: str
    cached: bool = False
    error_message: str | None = None
