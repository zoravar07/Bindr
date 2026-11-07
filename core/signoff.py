"""Human sign-off and diligence memo export.

A VerdictReport leaves the verdict writer as a draft. An analyst then:
- accepts or rejects individual red flags (rejection needs a reason),
- adds flags of their own (citing ledger rows),
- accepts the whole verdict, which sets is_draft=False.

Every rejection is appended to a JSONL file (`REJECTIONS_PATH`) with the
flag, the evidence it cited and the reason, so later runs and evals can see
where the system was wrong.

`export_diligence_memo` only works on a signed-off verdict.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

from core.ledger import EvidenceLedger
from shared.config import REJECTIONS_PATH
from shared.schemas import RedFlag, SignOffAction, VerdictReport, utc_now

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


class SignOffError(ValueError):
    pass


def apply_signoff(
    verdict: VerdictReport,
    action: SignOffAction,
    ledger: EvidenceLedger,
    *,
    rejections_path: Path | str | None = None,
) -> VerdictReport:
    """Return a new VerdictReport with `action` applied. The input is not mutated."""
    if not verdict.is_draft:
        raise SignOffError("Verdict is already signed off; start a new run to change it")

    v = verdict.model_copy(deep=True)
    flags = {f.id: f for f in v.red_flags}

    if action.red_flag_id is not None and action.red_flag_id not in flags:
        raise SignOffError(f"Unknown red flag {action.red_flag_id!r}")

    if action.action == "accept":
        if action.red_flag_id:
            flags[action.red_flag_id].review_status = "accepted"
        else:
            v.is_draft = False
            v.signed_off_by = action.reviewer
            v.signed_off_at = action.timestamp
    elif action.action == "reject":
        flag = flags.get(action.red_flag_id) if action.red_flag_id else None
        if flag is not None:
            flag.review_status = "rejected"
        log_rejection(v, action, ledger, flag, rejections_path)
    elif action.action == "add":
        nf = action.new_flag
        assert nf is not None  # enforced by SignOffAction validator
        ledger.validate_ids(nf.ledger_row_ids)
        v.red_flags.append(RedFlag(
            id=_next_flag_id(v.red_flags), rank=len(v.red_flags) + 1, check_id=nf.check_id,
            severity=nf.severity, title=nf.title, text=nf.text, ledger_row_ids=list(nf.ledger_row_ids),
            review_status="accepted", added_by=action.reviewer,
        ))
        v.red_flags = _rerank(v.red_flags)
    # "comment" only lands in the log.

    v.sign_off_log.append(action)
    return VerdictReport.model_validate(v.model_dump())


def apply_signoffs(verdict: VerdictReport, actions: list[SignOffAction], ledger: EvidenceLedger,
                   *, rejections_path: Path | str | None = None) -> VerdictReport:
    for a in actions:
        verdict = apply_signoff(verdict, a, ledger, rejections_path=rejections_path)
    return verdict


def _next_flag_id(flags: list[RedFlag]) -> str:
    nums = [int(m.group(1)) for f in flags if (m := re.fullmatch(r"flag-(\d+)", f.id))]
    return f"flag-{max(nums, default=0) + 1}"


def _rerank(flags: list[RedFlag]) -> list[RedFlag]:
    ordered = sorted(flags, key=lambda f: (_SEVERITY_ORDER[f.severity], f.rank))
    return [f.model_copy(update={"rank": i}) for i, f in enumerate(ordered, start=1)]


def log_rejection(verdict: VerdictReport, action: SignOffAction, ledger: EvidenceLedger,
                  flag: RedFlag | None, path: Path | str | None = None) -> Path:
    path = Path(path) if path is not None else REJECTIONS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    cited = flag.ledger_row_ids if flag else [i for s in verdict.check_summaries for i in s.ledger_row_ids]
    record = {
        "logged_at": utc_now().isoformat(),
        "reviewer": action.reviewer,
        "reason": action.note,
        "scope": "red_flag" if flag else "verdict",
        "asset": verdict.asset.model_dump(mode="json"),
        "overall_risk": verdict.overall_risk,
        "overall_score": verdict.overall_score,
        "synthesis": verdict.synthesis,
        "red_flag": flag.model_dump(mode="json") if flag else None,
        "evidence": [ledger.get(i).model_dump(mode="json") for i in dict.fromkeys(cited) if i in ledger],
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    return path


def load_rejections(path: Path | str | None = None) -> list[dict]:
    path = Path(path) if path is not None else REJECTIONS_PATH
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Memo
# ---------------------------------------------------------------------------


def export_diligence_memo(verdict: VerdictReport, ledger: EvidenceLedger) -> str:
    if verdict.is_draft:
        raise SignOffError("Memo export requires sign-off (verdict is still a draft)")
    ledger.validate_verdict(verdict)

    # Footnote numbers in order of first citation.
    notes: dict[str, int] = {}

    def cite(ids: list[str]) -> str:
        out = []
        for i in ids:
            if i not in notes:
                notes[i] = len(notes) + 1
            out.append(f"[{notes[i]}]")
        return "".join(out)

    a = verdict.asset
    lines: list[str] = []
    lines.append(f"# Pre-Mortem diligence memo: {a.drug}")
    lines.append("")
    meta = [f"**Target** {a.target}", f"**Disease** {a.disease}"]
    if a.as_of:
        meta.append(f"**Evidence cutoff** {a.as_of.isoformat()}")
    meta.append(f"**Signed off** {verdict.signed_off_by} on {verdict.signed_off_at:%Y-%m-%d}")
    lines.append(" · ".join(meta))
    lines.append("")

    score = f"{verdict.overall_score:.2f}" if verdict.overall_score is not None else "n/a"
    lines.append(f"## Call: {verdict.overall_risk.replace('_', ' ').upper()} risk (combined score {score}, "
                 f"{verdict.overall_confidence} confidence)")
    lines.append("")
    lines.append(verdict.overall_summary)
    lines.append("")

    lines.append("| Check | Failure risk | Status | Confidence |")
    lines.append("|---|---|---|---|")
    for s in verdict.check_summaries:
        shown = f"{s.score:.2f}" if s.status in ("complete", "partial") else "not assessed"
        lines.append(f"| {s.check_id} | {shown} | {s.status} | {s.confidence} |")
    lines.append("")

    live_flags = [f for f in verdict.red_flags if f.review_status != "rejected"]
    lines.append("## Red flags")
    lines.append("")
    if not live_flags:
        lines.append("None survived review.")
    for f in live_flags:
        who = f" (added by {f.added_by})" if f.added_by else ""
        lines.append(f"{f.rank}. **{f.title}** · {f.check_id}, {f.severity}{who}. {f.text} {cite(f.ledger_row_ids)}")
    lines.append("")

    rejected = [f for f in verdict.red_flags if f.review_status == "rejected"]
    if rejected:
        reasons = {a.red_flag_id: a.note for a in verdict.sign_off_log if a.action == "reject" and a.red_flag_id}
        lines.append("## Rejected by analyst")
        lines.append("")
        for f in rejected:
            lines.append(f"- ~~{f.title}~~: {reasons.get(f.id, '')}")
        lines.append("")

    if verdict.missing:
        lines.append("## Missing evidence")
        lines.append("")
        lines.extend(f"- {m}" for m in verdict.missing)
        lines.append("")

    lines.append("## Sources")
    lines.append("")
    for row_id, n in notes.items():
        row = ledger.get(row_id)
        lines.append(f"[{n}] {row.claim} ([{row.source_record}]({row.url}), pulled {row.date_pulled:%Y-%m-%d})")
    lines.append("")

    lines.append("## Dataset licences")
    lines.append("")
    lines.append("| Source | Licence | Rows cited | Pulled |")
    lines.append("|---|---|---|---|")
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for row_id in notes:
        row = ledger.get(row_id)
        groups[(urlparse(row.url).netloc or row.url, row.licence)].append(row)
    for (host, lic), rows in sorted(groups.items()):
        dates = sorted(r.date_pulled.date().isoformat() for r in rows)
        span = dates[0] if dates[0] == dates[-1] else f"{dates[0]} to {dates[-1]}"
        lines.append(f"| {host} | {lic} | {len(rows)} | {span} |")
    lines.append("")
    return "\n".join(lines)
