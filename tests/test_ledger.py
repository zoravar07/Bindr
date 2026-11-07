from datetime import date

import pytest

from core.ledger import (
    AsOfViolationError,
    ConflictingLedgerRowError,
    EvidenceLedger,
    UnknownLedgerRowError,
)
from shared.schemas import LedgerRow, Reason, utc_now


def _row(row_id="sci-1", claim="Target is genetically linked to disease", evidence_date=None):
    return LedgerRow(
        id=row_id,
        claim=claim,
        source_record="ENSG00000146648",
        url="https://platform.opentargets.org/target/ENSG00000146648",
        date_pulled=utc_now(),
        licence="CC0",
        check_id="science",
        evidence_date=evidence_date,
    )


def test_dedupes_identical_rows_and_rejects_conflicts():
    ledger = EvidenceLedger()
    assert ledger.add(_row()) is True
    assert ledger.add(_row()) is False  # same content, new date_pulled
    assert len(ledger) == 1
    with pytest.raises(ConflictingLedgerRowError):
        ledger.add(_row(claim="something else"))


def test_validates_cited_ids():
    ledger = EvidenceLedger([_row()])
    ledger.validate_reasons([Reason(text="ok", ledger_row_ids=["sci-1"])])
    with pytest.raises(UnknownLedgerRowError) as exc:
        ledger.validate_reasons([Reason(text="bad", ledger_row_ids=["sci-1", "ghost"])])
    assert exc.value.ids == ["ghost"]


def test_enforces_as_of_cutoff():
    ledger = EvidenceLedger(as_of=date(2020, 1, 1))
    ledger.add(_row("old", evidence_date=date(2019, 6, 1)))
    with pytest.raises(AsOfViolationError):
        ledger.add(_row("future", evidence_date=date(2021, 6, 1)))


def test_json_round_trip_and_table():
    ledger = EvidenceLedger([_row(), _row("sci-2", claim="a | b")], as_of=date(2020, 1, 1))
    back = EvidenceLedger.from_json(ledger.to_json())
    assert [r.id for r in back] == ["sci-1", "sci-2"]
    assert back.as_of == date(2020, 1, 1)
    table = ledger.to_table()
    assert "a \\| b" in table and table.count("\n") == 3
