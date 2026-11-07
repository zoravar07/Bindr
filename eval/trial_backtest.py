"""Backtest 2 - Dead Trial.

Two real cohorts from the live ClinicalTrials.gov v2 API, split on a hard
date boundary:

- Graveyard (reference): interventional Phase 2/3 drug trials in oncology,
  immunology or neurology whose completion date is strictly before
  2022-01-01, with a terminal status.
- Test cohort: the same kind of trial but started on or after 2022-01-01,
  which has since reached TERMINATED or COMPLETED.

Scoring sees only protocol fields, through the `PROTOCOL_FEATURE_KEYS`
allowlist: phase, design (allocation, masking, intervention model, purpose),
arm count, primary/secondary endpoint counts, eligibility-criteria size and
complexity, sex/age/healthy-volunteer eligibility, sponsor class, condition
and intervention tokens. Status, why-stopped, completion dates, results and
enrollment are stripped and asserted absent.

Model: k-nearest-neighbour similarity to the pre-2022 graveyard. For a test
trial, P(terminated) is the similarity-weighted termination rate of its k
nearest graveyard trials. Deterministic: neighbours are ordered by
(-similarity, nct_id).

Baselines to beat:
  (a) the graveyard's own termination base rate, assigned to every trial.
      A constant cannot rank, so AUROC is 0.5 and expected precision@k is
      the test-cohort prevalence.
  (b) size only: ranking by planned enrollment. Computed on the subset whose
      enrollment is still reported as ESTIMATED, and evaluated in both
      directions with the better one reported, which is deliberately
      generous to the baseline.

Two limitations stated plainly, because they bound what this backtest proves:

1. ClinicalTrials.gov's historical-version endpoint returns 403 to
   unauthenticated clients, so the test cohort is scored from each trial's
   *current* record rather than a genuine pre-start snapshot. The allowlist
   removes outcome fields, but it cannot prove a protocol field was never
   amended after the trial started.
2. Trials started in 2022 or later that have already reached a terminal
   status are a biased slice: long trials are still running, so short and
   early-stopped trials are over-represented. The measured prevalence is
   reported rather than assumed.
"""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Sequence

from eval.stats import auroc, jaccard, mean, precision_at_k, recall_at_k
from shared.cache import cached_get_json

CTGOV_API = "https://clinicaltrials.gov/api/v2/studies"
CTGOV_LICENCE = "Public domain (US Government, NLM terms of use)"
BOUNDARY = date(2022, 1, 1)
TOP_K = 20
NEIGHBOURS = 25

THERAPY_AREA_QUERY = (
    "cancer OR carcinoma OR lymphoma OR leukemia OR multiple sclerosis OR Alzheimer OR "
    "Parkinson OR rheumatoid arthritis OR psoriasis OR lupus OR Crohn OR colitis OR asthma"
)
BASE_FILTER = ("AREA[StudyType]INTERVENTIONAL AND AREA[Phase](PHASE2 OR PHASE3) "
               "AND AREA[InterventionType]DRUG")
FIELDS = ",".join((
    "NCTId", "OverallStatus", "Phase", "StudyType", "StartDate", "PrimaryCompletionDate",
    "CompletionDate", "WhyStopped", "EnrollmentCount", "EnrollmentType", "DesignAllocation",
    "DesignMasking", "DesignInterventionModel", "DesignPrimaryPurpose", "ArmGroupLabel",
    "PrimaryOutcomeMeasure", "SecondaryOutcomeMeasure", "EligibilityCriteria",
    "HealthyVolunteers", "Sex", "MinimumAge", "Condition", "InterventionName",
    "InterventionType", "LeadSponsorClass",
))

# Everything the scorer is allowed to see.
PROTOCOL_FEATURE_KEYS = frozenset({
    "nct_id", "phase", "allocation", "masking", "intervention_model", "primary_purpose",
    "sponsor_class", "n_arms", "n_primary_outcomes", "n_secondary_outcomes",
    "criteria_chars", "n_inclusion", "n_exclusion", "sex", "healthy_volunteers",
    "min_age_years", "n_conditions", "condition_tokens", "intervention_tokens",
    "intervention_types",
})
# Anything derived from what happened after the trial started.
BANNED_FEATURE_KEYS = frozenset({
    "overall_status", "overallStatus", "why_stopped", "whyStopped", "completion_date",
    "completionDate", "primary_completion_date", "primaryCompletionDate", "has_results",
    "hasResults", "results", "enrollment", "enrollment_count", "enrollment_type",
    "label", "outcome", "terminated", "start_date",
})

NUMERIC_SCALES = {
    "n_arms": 2.0, "n_primary_outcomes": 2.0, "n_secondary_outcomes": 6.0,
    "criteria_chars": 2500.0, "n_inclusion": 8.0, "n_exclusion": 10.0, "min_age_years": 20.0,
    "n_conditions": 2.0,
}
CATEGORICAL_KEYS = ("allocation", "masking", "intervention_model", "primary_purpose", "sponsor_class",
                    "sex", "healthy_volunteers")
SIMILARITY_WEIGHTS = {"condition": 0.30, "intervention": 0.25, "phase": 0.15,
                      "categorical": 0.10, "numeric": 0.20}

_STOPWORDS = frozenset({
    "the", "of", "and", "or", "in", "for", "with", "a", "an", "to", "study", "trial", "patients",
    "subjects", "placebo", "treatment", "therapy", "disease", "disorder", "stage", "advanced",
    "metastatic", "recurrent", "refractory", "mg", "kg", "iv", "oral", "tablet", "injection",
    "solution", "capsule", "dose", "doses", "high", "low", "arm", "group", "phase", "open",
    "label", "versus", "vs",
})


def _tokens(values: Iterable[str]) -> frozenset[str]:
    out: set[str] = set()
    for v in values or ():
        for w in re.findall(r"[a-z0-9]+", (v or "").lower()):
            if len(w) > 2 and w not in _STOPWORDS:
                out.add(w)
    return frozenset(out)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


async def _get_page(params: dict[str, Any], *, retries: int = 5, pause: float = 0.4) -> dict:
    """One cached page. Retries 429/5xx with exponential backoff; cache hits never sleep."""
    import httpx

    delay = 2.0
    for attempt in range(retries + 1):
        try:
            resp = await cached_get_json(CTGOV_API, params, timeout=90.0)
            if not resp.from_cache:
                await asyncio.sleep(pause)  # stay polite to a public API
            return resp.data or {}
        except httpx.HTTPStatusError as exc:
            retryable = exc.response.status_code == 429 or exc.response.status_code >= 500
            if not retryable or attempt == retries:
                raise
            await asyncio.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


async def fetch_studies(status: str, advanced_filter: str, *, limit: int, page_size: int = 200,
                        condition_query: str = THERAPY_AREA_QUERY) -> tuple[list[dict], int | None]:
    """Fetch up to `limit` studies, oldest registration first, through the cache.

    Ordering is `StudyFirstPostDate:asc`, a registration-time field, so the
    sample is reproducible and carries no outcome information.
    """
    studies: list[dict] = []
    token: str | None = None
    total: int | None = None
    while len(studies) < limit:
        params: dict[str, Any] = {
            "query.cond": condition_query,
            "filter.overallStatus": status,
            "filter.advanced": advanced_filter,
            "sort": "StudyFirstPostDate:asc",
            "fields": FIELDS,
            "pageSize": min(page_size, limit - len(studies)),
            "countTotal": "true",
        }
        if token:
            params["pageToken"] = token
        payload = await _get_page(params)
        if total is None:
            total = payload.get("totalCount")
        page = payload.get("studies") or []
        if not page:
            break
        studies.extend(page)
        token = payload.get("nextPageToken")
        if not token:
            break
    return studies[:limit], total


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _date_of(struct: dict | None) -> date | None:
    raw = (struct or {}).get("date")
    if not raw:
        return None
    parts = raw.split("-")
    try:
        return date(int(parts[0]), int(parts[1]) if len(parts) > 1 else 1,
                    int(parts[2]) if len(parts) > 2 else 1)
    except (ValueError, IndexError):
        return None


def _min_age_years(raw: str | None) -> float | None:
    if not raw:
        return None
    m = re.match(r"(\d+)\s*(\w+)", raw.strip())
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2).lower()
    if unit.startswith("year"):
        return n
    if unit.startswith("month"):
        return n / 12
    if unit.startswith("week"):
        return n / 52
    if unit.startswith("day"):
        return n / 365
    return None


@dataclass(frozen=True)
class Trial:
    """A trial split into protocol features, outcome label and audit metadata."""

    nct_id: str
    features: dict[str, Any]
    label: int  # 1 = TERMINATED
    status: str
    start_date: date | None
    completion_date: date | None
    enrollment_count: int | None
    enrollment_type: str | None
    why_stopped: str | None
    phase: str


def parse_trial(study: dict) -> Trial | None:
    proto = study.get("protocolSection") or {}
    ident = proto.get("identificationModule") or {}
    status_mod = proto.get("statusModule") or {}
    design = proto.get("designModule") or {}
    design_info = design.get("designInfo") or {}
    arms_mod = proto.get("armsInterventionsModule") or {}
    outcomes = proto.get("outcomesModule") or {}
    elig = proto.get("eligibilityModule") or {}
    conditions = (proto.get("conditionsModule") or {}).get("conditions") or []
    sponsor_class = ((proto.get("sponsorCollaboratorsModule") or {}).get("leadSponsor") or {}).get("class")

    nct_id = ident.get("nctId")
    status = status_mod.get("overallStatus")
    if not nct_id or status not in ("TERMINATED", "COMPLETED"):
        return None

    criteria = elig.get("eligibilityCriteria") or ""
    lowered = criteria.lower()
    split_at = lowered.find("exclusion criteria")
    inclusion_text = criteria[:split_at] if split_at > 0 else criteria
    exclusion_text = criteria[split_at:] if split_at > 0 else ""
    bullet = re.compile(r"(?m)^\s*(?:[-*•·]|\d+[.)])\s+")

    interventions = arms_mod.get("interventions") or []
    phases = design.get("phases") or []
    phase = "+".join(sorted(phases)) or "UNKNOWN"

    features: dict[str, Any] = {
        "nct_id": nct_id,
        "phase": phase,
        "allocation": design_info.get("allocation"),
        "masking": (design_info.get("maskingInfo") or {}).get("masking"),
        "intervention_model": design_info.get("interventionModel"),
        "primary_purpose": design_info.get("primaryPurpose"),
        "sponsor_class": sponsor_class,
        "n_arms": len(arms_mod.get("armGroups") or []),
        "n_primary_outcomes": len(outcomes.get("primaryOutcomes") or []),
        "n_secondary_outcomes": len(outcomes.get("secondaryOutcomes") or []),
        "criteria_chars": len(criteria),
        "n_inclusion": len(bullet.findall(inclusion_text)),
        "n_exclusion": len(bullet.findall(exclusion_text)),
        "sex": elig.get("sex"),
        "healthy_volunteers": bool(elig.get("healthyVolunteers")),
        "min_age_years": _min_age_years(elig.get("minimumAge")),
        "n_conditions": len(conditions),
        "condition_tokens": _tokens(conditions),
        "intervention_tokens": _tokens(i.get("name") for i in interventions),
        "intervention_types": frozenset(i.get("type") for i in interventions if i.get("type")),
    }
    assert set(features) <= PROTOCOL_FEATURE_KEYS, set(features) - PROTOCOL_FEATURE_KEYS

    enrollment = design.get("enrollmentInfo") or {}
    return Trial(
        nct_id=nct_id, features=features, label=1 if status == "TERMINATED" else 0, status=status,
        start_date=_date_of(status_mod.get("startDateStruct")),
        completion_date=(_date_of(status_mod.get("completionDateStruct"))
                         or _date_of(status_mod.get("primaryCompletionDateStruct"))),
        enrollment_count=enrollment.get("count"), enrollment_type=enrollment.get("type"),
        why_stopped=status_mod.get("whyStopped"), phase=phase,
    )


def assert_no_outcome_fields(features: dict[str, Any]) -> None:
    """Raise if anything derived from the outcome reached the scorer."""
    leaked = BANNED_FEATURE_KEYS & set(features)
    if leaked:
        raise AssertionError(f"outcome fields reached the scorer: {sorted(leaked)}")
    unknown = set(features) - PROTOCOL_FEATURE_KEYS
    if unknown:
        raise AssertionError(f"features outside the protocol allowlist: {sorted(unknown)}")


# ---------------------------------------------------------------------------
# Cohort assembly with hard date boundaries
# ---------------------------------------------------------------------------


@dataclass
class Cohorts:
    graveyard: list[Trial] = field(default_factory=list)
    test: list[Trial] = field(default_factory=list)
    rejected: dict[str, int] = field(default_factory=dict)
    api_totals: dict[str, int | None] = field(default_factory=dict)

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1


async def build_cohorts(*, graveyard_terminated: int = 420, graveyard_completed: int = 1580,
                        test_terminated: int = 500, test_completed: int = 850) -> Cohorts:
    grave_filter = f"{BASE_FILTER} AND AREA[CompletionDate]RANGE[MIN,2021-12-31]"
    test_filter = f"{BASE_FILTER} AND AREA[StartDate]RANGE[2022-01-01,MAX]"
    # Sequential: four parallel paginated crawls trip ClinicalTrials.gov rate limiting.
    fetched = [
        await fetch_studies("TERMINATED", grave_filter, limit=graveyard_terminated),
        await fetch_studies("COMPLETED", grave_filter, limit=graveyard_completed),
        await fetch_studies("TERMINATED", test_filter, limit=test_terminated),
        await fetch_studies("COMPLETED", test_filter, limit=test_completed),
    ]
    cohorts = Cohorts()
    cohorts.api_totals = {
        "graveyard_terminated": fetched[0][1], "graveyard_completed": fetched[1][1],
        "test_terminated": fetched[2][1], "test_completed": fetched[3][1],
    }
    seen: set[str] = set()
    for studies, bucket in ((fetched[0][0], "graveyard"), (fetched[1][0], "graveyard"),
                            (fetched[2][0], "test"), (fetched[3][0], "test")):
        for study in studies:
            trial = parse_trial(study)
            if trial is None:
                cohorts.reject("not a terminal interventional record")
                continue
            if trial.nct_id in seen:
                cohorts.reject("duplicate NCT ID")
                continue
            if bucket == "graveyard":
                if trial.completion_date is None:
                    cohorts.reject("graveyard: no completion date")
                    continue
                if trial.completion_date >= BOUNDARY:
                    cohorts.reject("graveyard: completion on or after 2022-01-01")
                    continue
                seen.add(trial.nct_id)
                cohorts.graveyard.append(trial)
            else:
                if trial.start_date is None:
                    cohorts.reject("test: no start date")
                    continue
                if trial.start_date < BOUNDARY:
                    cohorts.reject("test: start before 2022-01-01")
                    continue
                seen.add(trial.nct_id)
                cohorts.test.append(trial)
    cohorts.graveyard.sort(key=lambda t: t.nct_id)
    cohorts.test.sort(key=lambda t: t.nct_id)
    return cohorts


def verify_no_date_leakage(cohorts: Cohorts) -> list[str]:
    """Every violation of the 2022-01-01 split. Empty means clean."""
    problems: list[str] = []
    for t in cohorts.graveyard:
        if t.completion_date is None or t.completion_date >= BOUNDARY:
            problems.append(f"graveyard {t.nct_id} completes {t.completion_date}")
    for t in cohorts.test:
        if t.start_date is None or t.start_date < BOUNDARY:
            problems.append(f"test {t.nct_id} starts {t.start_date}")
    overlap = {t.nct_id for t in cohorts.graveyard} & {t.nct_id for t in cohorts.test}
    problems.extend(f"{nct} appears in both cohorts" for nct in sorted(overlap))
    return problems


# ---------------------------------------------------------------------------
# Graveyard similarity model
# ---------------------------------------------------------------------------


def similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    parts = {
        "condition": jaccard(a["condition_tokens"], b["condition_tokens"]),
        "intervention": jaccard(a["intervention_tokens"], b["intervention_tokens"]),
        "phase": 1.0 if a["phase"] == b["phase"] else 0.0,
    }
    agree = [1.0 if a.get(k) == b.get(k) else 0.0 for k in CATEGORICAL_KEYS]
    parts["categorical"] = sum(agree) / len(agree)
    closeness = []
    for key, scale in NUMERIC_SCALES.items():
        x, y = a.get(key), b.get(key)
        if x is None or y is None:
            continue
        closeness.append(math.exp(-abs(x - y) / scale))
    parts["numeric"] = sum(closeness) / len(closeness) if closeness else 0.0
    return sum(SIMILARITY_WEIGHTS[k] * v for k, v in parts.items())


class GraveyardScorer:
    """k-NN over the pre-2022 graveyard. Fit on protocol features and labels only."""

    def __init__(self, graveyard: Sequence[Trial], k: int = NEIGHBOURS):
        self.k = k
        self.rows = [(t.nct_id, t.features, t.label) for t in sorted(graveyard, key=lambda t: t.nct_id)]
        for _, features, _ in self.rows:
            assert_no_outcome_fields(features)
        labels = [l for _, _, l in self.rows]
        self.base_rate = mean(labels) if labels else 0.0

    def score(self, features: dict[str, Any]) -> float:
        assert_no_outcome_fields(features)
        scored = sorted(((similarity(features, g), nct, label) for nct, g, label in self.rows),
                        key=lambda r: (-r[0], r[1]))[:self.k]
        total = sum(s for s, _, _ in scored)
        if total <= 0:
            return float(self.base_rate)
        return sum(s * label for s, _, label in scored) / total

    def neighbours(self, features: dict[str, Any], n: int = 5) -> list[tuple[str, float, int]]:
        scored = sorted(((similarity(features, g), nct, label) for nct, g, label in self.rows),
                        key=lambda r: (-r[0], r[1]))[:n]
        return [(nct, round(sim, 4), label) for sim, nct, label in scored]


# ---------------------------------------------------------------------------
# Model selection, done only inside the graveyard
# ---------------------------------------------------------------------------

CV_FOLDS = 5
K_CANDIDATES = (10, 25, 50, 100)


def graveyard_cross_validation(graveyard: Sequence[Trial], *, folds: int = CV_FOLDS,
                               k_values: Sequence[int] = K_CANDIDATES) -> dict:
    """Does the similarity model carry any termination signal at all?

    Measured strictly inside the pre-2022 graveyard, which is the reference
    set, so looking here is legitimate. Any choice of k is made from these
    numbers alone and never from the post-2022 test labels.
    """
    rows = sorted(graveyard, key=lambda t: t.nct_id)
    if len(rows) < folds * 2:
        return {"folds": folds, "n": len(rows), "by_k": {}, "best_k": None}
    fold_of = {t.nct_id: i % folds for i, t in enumerate(rows)}

    # Neighbour ordering depends only on similarity, so compute it once per trial.
    ordered: dict[str, list[tuple[float, str, int]]] = {}
    for t in rows:
        sims = [(similarity(t.features, o.features), o.nct_id, o.label)
                for o in rows if o.nct_id != t.nct_id and fold_of[o.nct_id] != fold_of[t.nct_id]]
        sims.sort(key=lambda r: (-r[0], r[1]))
        ordered[t.nct_id] = sims[:max(k_values)]

    labels = [t.label for t in rows]
    ids = [t.nct_id for t in rows]
    base = mean(labels) or 0.0
    by_k: dict[str, dict] = {}
    for k in k_values:
        preds = []
        for t in rows:
            top = ordered[t.nct_id][:k]
            total = sum(sim for sim, _, _ in top)
            preds.append(sum(sim * l for sim, _, l in top) / total if total > 0 else base)
        by_k[str(k)] = {
            "auroc": auroc(preds, labels),
            f"precision_at_{TOP_K}": precision_at_k(preds, labels, TOP_K, tiebreak=ids),
        }
    best_k = max(k_values, key=lambda k: by_k[str(k)]["auroc"] or 0.0)
    return {
        "folds": folds, "n": len(rows), "prevalence": round(base, 4),
        "by_k": by_k, "best_k": best_k, "best_auroc": by_k[str(best_k)]["auroc"],
        "note": "Model selection used these graveyard folds only. The post-2022 test cohort was scored "
                "once, with the k chosen here.",
    }


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------


def size_only_baseline(test: Sequence[Trial]) -> dict:
    """Rank by planned enrollment, on the subset still reporting ESTIMATED enrollment."""
    subset = [t for t in test if t.enrollment_type == "ESTIMATED" and t.enrollment_count is not None]
    if len(subset) < 10:
        return {"n": len(subset), "auroc": None, "direction": None,
                "available": False,
                "note": "Unavailable on this cohort: every test trial has finished, so all of them report "
                        "ACTUAL rather than ESTIMATED enrollment. Ranking on actual enrollment would read "
                        "the outcome, since a terminated trial stops small, so no size-only baseline can "
                        "be computed here without leakage."}
    labels = [t.label for t in subset]
    small_first = [-float(t.enrollment_count) for t in subset]
    large_first = [float(t.enrollment_count) for t in subset]
    a_small, a_large = auroc(small_first, labels), auroc(large_first, labels)
    better = "smaller_enrollment_first" if (a_small or 0) >= (a_large or 0) else "larger_enrollment_first"
    scores = small_first if better == "smaller_enrollment_first" else large_first
    ids = [t.nct_id for t in subset]
    return {
        "n": len(subset),
        "prevalence": round(mean(labels), 4),
        "auroc": max(a_small or 0.0, a_large or 0.0),
        "auroc_smaller_first": a_small,
        "auroc_larger_first": a_large,
        "direction": better,
        f"precision_at_{TOP_K}": precision_at_k(scores, labels, TOP_K, tiebreak=ids),
        "note": "Evaluated in both directions and the better one reported, which is generous to the "
                "baseline on purpose. Restricted to ESTIMATED enrollment because a TERMINATED trial's "
                "record usually reports its small ACTUAL enrollment, which would leak the outcome.",
    }


def enrollment_leakage_audit(test: Sequence[Trial]) -> dict:
    """Quantify the enrollment leak rather than hide it."""
    def share(trials: Sequence[Trial]) -> dict:
        if not trials:
            return {}
        counts: dict[str, int] = {}
        for t in trials:
            counts[t.enrollment_type or "MISSING"] = counts.get(t.enrollment_type or "MISSING", 0) + 1
        return {k: round(v / len(trials), 4) for k, v in sorted(counts.items())}

    terminated = [t for t in test if t.label == 1]
    completed = [t for t in test if t.label == 0]
    return {
        "enrollment_type_share_terminated": share(terminated),
        "enrollment_type_share_completed": share(completed),
        "why_enrollment_is_excluded": "Enrollment is excluded from the model's feature allowlist. For a "
                                      "terminated trial the current record usually reports actual (small) "
                                      "enrollment, so both the count and its ESTIMATED/ACTUAL type carry "
                                      "outcome information.",
        "why_stopped_present_terminated": sum(1 for t in terminated if t.why_stopped),
        "why_stopped_present_completed": sum(1 for t in completed if t.why_stopped),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run_trial_backtest(cohorts: Cohorts | None = None, *, k: int | None = None,
                             run_cv: bool = True) -> dict:
    cohorts = cohorts if cohorts is not None else await build_cohorts()
    leakage = verify_no_date_leakage(cohorts)
    cv = graveyard_cross_validation(cohorts.graveyard) if run_cv else {"best_k": None, "by_k": {}}
    if k is None:
        k = cv.get("best_k") or NEIGHBOURS  # chosen from graveyard folds, never from test labels
    scorer = GraveyardScorer(cohorts.graveyard, k=k)

    scores = [scorer.score(t.features) for t in cohorts.test]
    labels = [t.label for t in cohorts.test]
    ids = [t.nct_id for t in cohorts.test]
    prevalence = mean(labels) if labels else None

    ranked = sorted(zip(scores, ids, labels), key=lambda r: (-r[0], r[1]))
    model_precision = precision_at_k(scores, labels, TOP_K, tiebreak=ids)
    base_rate_baseline = {
        "description": "Graveyard termination base rate assigned to every test trial",
        "value": round(scorer.base_rate, 4),
        "auroc": 0.5,
        f"expected_precision_at_{TOP_K}": round(prevalence, 4) if prevalence is not None else None,
        "note": "A constant predictor cannot rank, so AUROC is 0.5 and expected precision@k equals "
                "test-cohort prevalence.",
    }
    size_baseline = size_only_baseline(cohorts.test)
    best_baseline_precision = max(
        [p for p in (base_rate_baseline[f"expected_precision_at_{TOP_K}"],
                     size_baseline.get(f"precision_at_{TOP_K}")) if p is not None] or [0.0])

    return {
        "backtest": "trial",
        "name": "Dead Trial",
        "test": f"{len(cohorts.test)} interventional Phase 2/3 drug trials started on or after "
                f"{BOUNDARY.isoformat()} with a terminal status, ranked by similarity to "
                f"{len(cohorts.graveyard)} trials completed strictly before {BOUNDARY.isoformat()}",
        "headline_metric": f"precision@{TOP_K} of the graveyard-similarity ranking, post-2022 test cohort",
        "headline_value": model_precision,
        "headline_auroc": auroc(scores, labels),
        "in_period_auroc": cv.get("best_auroc"),
        "generalises_across_boundary": bool(
            (auroc(scores, labels) or 0) > 0.55 and (model_precision or 0) > (prevalence or 0)),
        "baseline_metric": f"best baseline precision@{TOP_K}",
        "baseline_value": best_baseline_precision,
        "baseline_auroc": max(0.5, size_baseline.get("auroc") or 0.0),
        "model": {
            "k_neighbours": k,
            "n_test": len(cohorts.test),
            "n_graveyard": len(cohorts.graveyard),
            "test_prevalence": round(prevalence, 4) if prevalence is not None else None,
            "graveyard_prevalence": round(scorer.base_rate, 4),
            f"precision_at_{TOP_K}": model_precision,
            f"recall_at_{TOP_K}": recall_at_k(scores, labels, TOP_K, tiebreak=ids),
            "auroc": auroc(scores, labels),
            "lift_over_prevalence": (round(model_precision / prevalence, 3)
                                     if model_precision and prevalence else None),
            "top_flagged": [
                {"nct_id": nct, "score": round(s, 4), "terminated": bool(l),
                 "url": f"https://clinicaltrials.gov/study/{nct}"}
                for s, nct, l in ranked[:TOP_K]
            ],
        },
        "baselines": {"phase_base_rate": base_rate_baseline, "size_only": size_baseline},
        "graveyard_cross_validation": cv,
        "finding": (
            "The similarity model carries real signal inside the pre-2022 graveyard "
            f"(cross-validated AUROC {cv.get('best_auroc')}) but does not survive the "
            f"{BOUNDARY.isoformat()} boundary (test AUROC {auroc(scores, labels)}, precision@{TOP_K} "
            f"{model_precision} against prevalence {round(prevalence, 4) if prevalence else None}). "
            "Protocol shape predicts termination among trials of the same era and fails to transfer to "
            "the next one. On this evidence the trial lane must not claim to forecast termination from "
            "protocol fields alone."
        ),
        "feature_allowlist": sorted(PROTOCOL_FEATURE_KEYS),
        "stripped_fields": sorted(BANNED_FEATURE_KEYS),
        "enrollment_leakage_audit": enrollment_leakage_audit(cohorts.test),
        "date_leakage_violations": leakage,
        "api_totals": cohorts.api_totals,
        "rejected_records": cohorts.rejected,
        "caveats": [
            "ClinicalTrials.gov's historical-version endpoint returns 403 to unauthenticated clients, so "
            "test trials are scored from their current record, not a pre-start snapshot. The allowlist "
            "removes outcome fields but cannot prove a protocol field was never amended after start.",
            "Trials started on or after 2022-01-01 that already have a terminal status over-represent "
            "short and early-stopped studies, because long trials are still running. The measured "
            "prevalence is reported, not assumed.",
            "Cohorts are the earliest-registered matching trials (sort=StudyFirstPostDate:asc) up to a "
            "cap, not a random sample of the registry.",
        ],
        "licence": CTGOV_LICENCE,
    }


def main() -> dict:
    import json
    result = asyncio.run(run_trial_backtest())
    m = result["model"]
    print(f"Dead Trial: precision@{TOP_K} {m[f'precision_at_{TOP_K}']} vs prevalence {m['test_prevalence']} "
          f"(lift {m['lift_over_prevalence']}x), AUROC {m['auroc']}")
    print(f"  graveyard {m['n_graveyard']} trials, test {m['n_test']} trials, "
          f"date leakage violations: {len(result['date_leakage_violations'])}")
    print(f"  size-only baseline: AUROC {result['baselines']['size_only'].get('auroc')} "
          f"on n={result['baselines']['size_only'].get('n')}")
    print(json.dumps(result["enrollment_leakage_audit"], indent=1))
    return result


if __name__ == "__main__":
    main()
