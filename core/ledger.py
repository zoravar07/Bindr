"""Evidence ledger: the single source of truth the verdict writer reads.

Every claim in a verdict must resolve to a row here. The ledger dedupes rows
by ID, rejects evidence dated after the asset's `as_of` cutoff, and validates
that cited IDs exist.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Iterable, Iterator

from shared.schemas import CheckId, CheckResult, LedgerRow, Reason, RedFlag, VerdictReport


class LedgerError(ValueError):
    pass


class UnknownLedgerRowError(LedgerError):
    def __init__(self, ids: list[str]):
        self.ids = ids
        super().__init__(f"Cited ledger rows not found: {ids}")


class ConflictingLedgerRowError(LedgerError):
    pass


class AsOfViolationError(LedgerError):
    pass


class EvidenceLedger:
    def __init__(self, rows: Iterable[LedgerRow] = (), *, as_of: date | None = None):
        self.as_of = as_of
        self._rows: dict[str, LedgerRow] = {}
        self.add_many(rows)

    # ---- writes ---------------------------------------------------------

    def add(self, row: LedgerRow) -> bool:
        """Add a row. Returns False if an identical row was already present.

        Raises ConflictingLedgerRowError if the ID exists with different content,
        and AsOfViolationError if the evidence postdates the as_of cutoff.
        """
        if self.as_of is not None and row.evidence_date is not None and row.evidence_date > self.as_of:
            raise AsOfViolationError(
                f"LedgerRow {row.id!r} evidence_date {row.evidence_date} is after as_of {self.as_of}"
            )
        existing = self._rows.get(row.id)
        if existing is not None:
            if _content(existing) == _content(row):
                return False
            raise ConflictingLedgerRowError(f"LedgerRow ID {row.id!r} already exists with different content")
        self._rows[row.id] = row
        return True

    def add_many(self, rows: Iterable[LedgerRow]) -> int:
        return sum(1 for r in rows if self.add(r))

    def add_result(self, result: CheckResult) -> int:
        """Ingest a check's rows and confirm its reasons cite rows in the ledger."""
        added = self.add_many(result.ledger_rows)
        self.validate_reasons(result.reasons)
        return added

    # ---- reads ----------------------------------------------------------

    def __len__(self) -> int:
        return len(self._rows)

    def __contains__(self, row_id: object) -> bool:
        return row_id in self._rows

    def __iter__(self) -> Iterator[LedgerRow]:
        return iter(self._rows.values())

    def get(self, row_id: str) -> LedgerRow:
        try:
            return self._rows[row_id]
        except KeyError:
            raise UnknownLedgerRowError([row_id]) from None

    def rows(self, check_id: CheckId | None = None) -> list[LedgerRow]:
        return [r for r in self._rows.values() if check_id is None or r.check_id == check_id]

    # ---- validation -----------------------------------------------------

    def missing_ids(self, ids: Iterable[str]) -> list[str]:
        return [i for i in ids if i not in self._rows]

    def validate_ids(self, ids: Iterable[str]) -> None:
        missing = self.missing_ids(ids)
        if missing:
            raise UnknownLedgerRowError(missing)

    def validate_reasons(self, reasons: Iterable[Reason]) -> None:
        self.validate_ids(i for r in reasons for i in r.ledger_row_ids)

    def validate_red_flags(self, flags: Iterable[RedFlag]) -> None:
        self.validate_ids(i for f in flags for i in f.ledger_row_ids)

    def validate_verdict(self, verdict: VerdictReport) -> None:
        """Every ID cited anywhere in the verdict must exist in this ledger."""
        self.validate_red_flags(verdict.red_flags)
        self.validate_ids(i for s in verdict.check_summaries for i in s.ledger_row_ids)

    # ---- serialisation --------------------------------------------------

    def to_dicts(self) -> list[dict]:
        return [r.model_dump(mode="json") for r in self._rows.values()]

    def to_json(self, **kwargs) -> str:
        return json.dumps(
            {"as_of": self.as_of.isoformat() if self.as_of else None, "rows": self.to_dicts()}, **kwargs
        )

    @classmethod
    def from_json(cls, raw: str) -> "EvidenceLedger":
        data = json.loads(raw)
        as_of = date.fromisoformat(data["as_of"]) if data.get("as_of") else None
        return cls((LedgerRow.model_validate(r) for r in data["rows"]), as_of=as_of)

    def to_table(self) -> str:
        """Markdown table, for memos and debugging."""
        header = "| id | check | claim | source | licence | date_pulled | url |\n|---|---|---|---|---|---|---|"
        lines = [header]
        for r in self._rows.values():
            claim = r.claim.replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {r.id} | {r.check_id} | {claim} | {r.source_record} | {r.licence} | "
                f"{r.date_pulled.date().isoformat()} | {r.url} |"
            )
        return "\n".join(lines)


def _content(row: LedgerRow) -> dict:
    # date_pulled may differ between a cached and live read of the same record;
    # identity is about what the evidence says and where it came from.
    return row.model_dump(exclude={"date_pulled"})
