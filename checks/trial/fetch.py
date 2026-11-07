"""ClinicalTrials.gov REST API v2 fetcher for the Pre-Mortem Trial workstream."""

from __future__ import annotations
from datetime import date, datetime, timezone
import inspect
import logging
from typing import Any

from shared.cache import cached_get
from shared.schemas import Asset, LedgerRow
from checks.trial.models import ClinicalTrialRecord, TrialFetchResult

logger = logging.getLogger("pre_mortem.checks.trial")

CLINICAL_TRIALS_STUDIES_ENDPOINT = "https://clinicaltrials.gov/api/v2/studies"
CLINICAL_TRIALS_LICENCE = "U.S. Public Domain (17 U.S.C. § 105)"

FIELDS_TO_REQUEST = [
    "protocolSection.identificationModule.nctId",
    "protocolSection.identificationModule.briefTitle",
    "protocolSection.identificationModule.officialTitle",
    "protocolSection.statusModule.overallStatus",
    "protocolSection.statusModule.whyStopped",
    "protocolSection.statusModule.startDateStruct",
    "protocolSection.statusModule.completionDateStruct",
    "protocolSection.statusModule.statusVerifiedDate",
    "protocolSection.designModule.phases",
    "protocolSection.designModule.studyType",
    "protocolSection.designModule.enrollmentInfo",
    "protocolSection.armsInterventionsModule.interventions",
    "protocolSection.conditionsModule.conditions",
    "protocolSection.sponsorCollaboratorsModule.leadSponsor",
]


def _parse_date_string(date_str: str | None) -> date | None:
    if not date_str:
        return None
    try:
        parts = str(date_str).split("-")
        if len(parts) == 3:
            return date(int(parts[0]), int(parts[1]), int(parts[2]))
        if len(parts) == 2:
            return date(int(parts[0]), int(parts[1]), 1)
        if len(parts) == 1 and len(parts[0]) == 4:
            return date(int(parts[0]), 1, 1)
    except Exception:
        pass
    return None


def _parse_study_record(raw: dict[str, Any], as_of: date | None = None) -> ClinicalTrialRecord | None:
    ps = raw.get("protocolSection", {})
    ident = ps.get("identificationModule", {})
    status_mod = ps.get("statusModule", {})
    design_mod = ps.get("designModule", {})
    arms_mod = ps.get("armsInterventionsModule", {})
    conds_mod = ps.get("conditionsModule", {})
    sponsor_mod = ps.get("sponsorCollaboratorsModule", {})

    nct_id = ident.get("nctId")
    if not nct_id:
        return None

    start_date_str = status_mod.get("startDateStruct", {}).get("date")
    completion_date_str = status_mod.get("completionDateStruct", {}).get("date")
    status_verified_date_str = status_mod.get("statusVerifiedDate")

    start_d = _parse_date_string(start_date_str)
    completion_d = _parse_date_string(completion_date_str)
    verified_d = _parse_date_string(status_verified_date_str)

    if as_of and start_d and start_d > as_of:
        return None

    overall_status = status_mod.get("overallStatus", "UNKNOWN")
    why_stopped = status_mod.get("whyStopped")

    if as_of:
        effective_stop = completion_d or verified_d
        if effective_stop and effective_stop > as_of:
            overall_status = "ACTIVE_AS_OF"
            why_stopped = None

    enrollment_info = design_mod.get("enrollmentInfo", {})
    interventions = [
        it.get("name", "")
        for it in arms_mod.get("interventions", [])
        if it.get("name")
    ]

    return ClinicalTrialRecord(
        nct_id=nct_id,
        title=ident.get("briefTitle") or ident.get("officialTitle") or "",
        overall_status=overall_status,
        why_stopped=why_stopped.strip() if why_stopped else None,
        phases=design_mod.get("phases", []),
        study_type=design_mod.get("studyType", "INTERVENTIONAL"),
        start_date=start_date_str,
        completion_date=completion_date_str,
        status_verified_date=status_verified_date_str,
        enrollment_count=enrollment_info.get("count"),
        enrollment_type=enrollment_info.get("type"),
        lead_sponsor=sponsor_mod.get("leadSponsor", {}).get("name"),
        interventions=interventions,
        conditions=conds_mod.get("conditions", []),
        url=f"https://clinicaltrials.gov/study/{nct_id}",
    )


def _build_ledger_row(
    record: ClinicalTrialRecord,
    claim: str,
    date_pulled: datetime,
    licence: str,
    suffix: str = "status",
    as_of: date | None = None,
) -> LedgerRow:
    ev_date = _parse_date_string(
        record.completion_date or record.status_verified_date or record.start_date
    )
    if as_of and ev_date and ev_date > as_of:
        ev_date = as_of

    row_kwargs: dict[str, Any] = {
        "id": f"ledger_trial_{record.nct_id}_{suffix}",
        "claim": claim,
        "source_record": f"ClinicalTrials.gov study {record.nct_id}",
        "url": record.url,
        "date_pulled": date_pulled,
        "licence": licence,
        "check_id": "trial",
    }
    if "evidence_date" in LedgerRow.model_fields:
        row_kwargs["evidence_date"] = ev_date

    return LedgerRow(**row_kwargs)


async def _call_cached_get(url: str, params: dict[str, Any], timeout: float) -> tuple[dict[str, Any], datetime]:
    sig = inspect.signature(cached_get)
    call_kwargs: dict[str, Any] = {}
    if "params" in sig.parameters:
        call_kwargs["params"] = params
    if "timeout" in sig.parameters:
        call_kwargs["timeout"] = timeout
    if "licence" in sig.parameters:
        call_kwargs["licence"] = CLINICAL_TRIALS_LICENCE

    res = await cached_get(url, **call_kwargs)
    now = datetime.now(timezone.utc)

    if isinstance(res, tuple):
        data = res[0]
        pulled = res[1] if len(res) > 1 and isinstance(res[1], datetime) else now
        return data, pulled
    if isinstance(res, dict):
        if "data" in res and isinstance(res["data"], dict) and "studies" in res["data"]:
            pulled_raw = res.get("date_pulled")
            pulled = datetime.fromisoformat(pulled_raw) if isinstance(pulled_raw, str) else (pulled_raw or now)
            return res["data"], pulled
        return res, now
    if hasattr(res, "data"):
        return res.data, getattr(res, "date_pulled", now)
    return {}, now


async def fetch_trials_for_asset(
    asset: Asset,
    timeout_seconds: float = 15.0,
) -> TrialFetchResult:
    date_pulled = datetime.now(timezone.utc)
    licence = CLINICAL_TRIALS_LICENCE

    missing: list[str] = []
    ledger_rows: list[LedgerRow] = []
    direct_trials: list[ClinicalTrialRecord] = []
    drug_trials: list[ClinicalTrialRecord] = []
    disease_benchmark_trials: list[ClinicalTrialRecord] = []
    fields_arg = ",".join(FIELDS_TO_REQUEST)

    direct_params = {
        "query.intr": asset.drug,
        "query.cond": asset.disease,
        "pageSize": 25,
        "fields": fields_arg,
    }
    try:
        data_direct, date_pulled = await _call_cached_get(
            CLINICAL_TRIALS_STUDIES_ENDPOINT, direct_params, timeout_seconds
        )
        for raw_study in data_direct.get("studies", []):
            parsed = _parse_study_record(raw_study, as_of=asset.as_of)
            if parsed:
                direct_trials.append(parsed)
    except Exception as err:
        logger.warning(f"Direct trials query error: {err}")
        missing.append(f"ClinicalTrials.gov direct query failed or timed out: {err}")

    drug_params = {
        "query.intr": asset.drug,
        "pageSize": 20,
        "fields": fields_arg,
    }
    try:
        data_drug, _ = await _call_cached_get(
            CLINICAL_TRIALS_STUDIES_ENDPOINT, drug_params, timeout_seconds
        )
        direct_ids = {t.nct_id for t in direct_trials}
        for raw_study in data_drug.get("studies", []):
            parsed = _parse_study_record(raw_study, as_of=asset.as_of)
            if parsed and parsed.nct_id not in direct_ids:
                drug_trials.append(parsed)
    except Exception as err:
        logger.warning(f"Drug portfolio query error: {err}")
        missing.append(f"ClinicalTrials.gov drug portfolio query failed: {err}")

    benchmark_params = {
        "query.cond": asset.disease,
        "query.term": asset.target,
        "filter.overallStatus": "TERMINATED,WITHDRAWN,SUSPENDED",
        "pageSize": 25,
        "fields": fields_arg,
    }
    try:
        data_bench, _ = await _call_cached_get(
            CLINICAL_TRIALS_STUDIES_ENDPOINT, benchmark_params, timeout_seconds
        )
        for raw_study in data_bench.get("studies", []):
            parsed = _parse_study_record(raw_study, as_of=asset.as_of)
            if parsed:
                disease_benchmark_trials.append(parsed)
    except Exception as err:
        logger.warning(f"Benchmark trials query error: {err}")
        missing.append(f"ClinicalTrials.gov benchmark query failed: {err}")

    if not direct_trials:
        missing.append(
            f"No interventional ClinicalTrials.gov trials found matching drug '{asset.drug}' in '{asset.disease}'"
            + (f" as of {asset.as_of}" if asset.as_of else "")
        )

    for trial in direct_trials:
        phases_str = ", ".join(trial.phases) if trial.phases else "unspecified phase"
        claim_status = (
            f"Trial {trial.nct_id} testing '{asset.drug}' in '{asset.disease}' ({phases_str}) "
            f"has status '{trial.overall_status}'"
        )
        if trial.enrollment_count is not None:
            claim_status += f" with enrollment of {trial.enrollment_count} ({trial.enrollment_type or 'participants'})"

        ledger_rows.append(
            _build_ledger_row(trial, claim_status, date_pulled, licence, suffix="status", as_of=asset.as_of)
        )

        if trial.why_stopped:
            claim_stop = (
                f"Trial {trial.nct_id} for '{asset.drug}' in '{asset.disease}' was stopped/terminated. "
                f"Documented whyStopped reason: \"{trial.why_stopped}\""
            )
            ledger_rows.append(
                _build_ledger_row(trial, claim_stop, date_pulled, licence, suffix="why_stopped", as_of=asset.as_of)
            )

    for trial in disease_benchmark_trials[:10]:
        if trial.why_stopped:
            claim_bench = (
                f"Historical benchmark trial {trial.nct_id} in '{asset.disease}' ({asset.target}) terminated with reason: "
                f"\"{trial.why_stopped}\""
            )
            ledger_rows.append(
                _build_ledger_row(trial, claim_bench, date_pulled, licence, suffix="benchmark_stop", as_of=asset.as_of)
            )

    status = "complete" if (direct_trials or disease_benchmark_trials) else "partial"

    return TrialFetchResult(
        asset=asset,
        status=status,
        direct_trials=direct_trials,
        drug_trials=drug_trials,
        disease_benchmark_trials=disease_benchmark_trials,
        missing=missing,
        ledger_rows=ledger_rows,
        as_of=asset.as_of,
        date_pulled=date_pulled,
        licence=licence,
    )
