"""API tests. Offline tests use test-double lanes; `live` tests use the real resolver and science lane."""

import asyncio
import json

import httpx
import pytest

from api.main import create_app
from core.gemini_client import GeminiClient
from shared.schemas import CheckResult, LedgerRow, Reason, StatusEvent, StructurePayload, utc_now

NO_LLM = GeminiClient(api_key="")
ASSET = {"drug": "DRUGX", "target": "TGT1", "disease": "disease y", "ids": {"ensembl_id": "E1", "efo_id": "D1"}}


def lane(check_id, score, delay=0.02, structure=False):
    async def run_check(asset, emit):
        await emit(StatusEvent(check_id=check_id, status="fetching", message="...", timestamp=utc_now()))
        await asyncio.sleep(delay)
        row = LedgerRow(id=f"{check_id}-1", claim=f"{check_id} finding", source_record="REC",
                        url=f"https://example.org/{check_id}", date_pulled=utc_now(), licence="CC0 1.0",
                        check_id=check_id)
        return CheckResult(
            check_id=check_id, score=score, confidence="medium", status="complete",
            reasons=[Reason(text=f"{check_id} risk finding.", ledger_row_ids=[row.id], direction="risk")],
            missing=[], ledger_rows=[row],
            structure=StructurePayload(source="pdb", structure_id="1M17", structure_url="https://www.rcsb.org/structure/1M17",
                                       confidence_note="crystal", residues=[]) if structure else None,
        )
    return run_check


async def hang(asset, emit):
    await asyncio.sleep(30)


def parse_sse(text):
    events = []
    for block in text.strip().split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if line.startswith(":"):
                continue
            k, _, v = line.partition(": ")
            fields[k] = v
        if fields:
            events.append(fields)
    return events


@pytest.fixture
async def client_factory(tmp_path):
    clients = []

    def make(**kw):
        kw.setdefault("gemini", NO_LLM)
        kw.setdefault("rejections_path", tmp_path / "rejections.jsonl")
        app = create_app(**kw)
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        clients.append(c)
        return c

    yield make
    for c in clients:
        await c.aclose()


async def test_full_run_stream_signoff_and_memo(client_factory):
    c = client_factory(checks={"science": lane("science", 0.7), "trial": hang, "market": lane("market", 0.4, structure=True)},
                       timeout_s=0.5)
    r = await c.post("/api/run", json={"asset": ASSET})
    assert r.status_code == 202
    run_id = r.json()["run_id"]

    stream = await c.get(f"/api/runs/{run_id}/events")
    assert stream.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(stream.text)
    assert events[-1]["event"] == "end" and json.loads(events[-1]["data"])["status"] == "complete"
    statuses = [json.loads(e["data"]) for e in events if e.get("event") == "status"]
    assert any(s["check_id"] == "trial" and s["status"] == "error" for s in statuses)
    assert statuses[-1]["check_id"] == "verdict" and statuses[-1]["status"] == "complete"

    view = (await c.get(f"/api/runs/{run_id}")).json()
    assert view["status"] == "complete"
    assert view["check_results"]["trial"]["status"] == "timeout"
    assert {row["id"] for row in view["ledger_rows"]} == {"science-1", "market-1"}
    assert view["structure"]["structure_id"] == "1M17"
    verdict = view["verdict"]
    assert verdict["is_draft"] is True and verdict["overall_score"] == pytest.approx(0.82)

    assert (await c.get(f"/api/runs/{run_id}/memo")).status_code == 409  # not signed off

    bad = await c.post(f"/api/runs/{run_id}/signoff", json={"actions": [
        {"reviewer": "zoro", "action": "add", "new_flag": {"check_id": "science", "severity": "low", "title": "x",
                                                            "text": "x", "ledger_row_ids": ["nope"]}}]})
    assert bad.status_code == 400

    flag_id = verdict["red_flags"][1]["id"]
    r = await c.post(f"/api/runs/{run_id}/signoff", json={"actions": [
        {"reviewer": "zoro", "action": "reject", "red_flag_id": flag_id, "note": "Not relevant to this indication"},
        {"reviewer": "zoro", "action": "accept"},
    ]})
    assert r.status_code == 200 and r.json()["is_draft"] is False

    memo = await c.get(f"/api/runs/{run_id}/memo")
    assert memo.status_code == 200 and memo.headers["content-type"].startswith("text/markdown")
    assert "# Pre-Mortem diligence memo: DRUGX" in memo.text and "https://example.org/science" in memo.text

    again = await c.post(f"/api/runs/{run_id}/signoff", json={"actions": [{"reviewer": "zoro", "action": "comment"}]})
    assert again.status_code == 400


async def test_sse_streams_while_running_and_supports_resume(client_factory):
    c = client_factory(checks={"science": lane("science", 0.2, delay=0.3)}, timeout_s=2)
    run_id = (await c.post("/api/run", json={"asset": ASSET})).json()["run_id"]
    assert (await c.get(f"/api/runs/{run_id}")).json()["status"] == "running"
    full = parse_sse((await c.get(f"/api/runs/{run_id}/events")).text)
    resumed = parse_sse((await c.get(f"/api/runs/{run_id}/events", params={"after": 3})).text)
    assert len(resumed) == len(full) - 3
    assert resumed[0]["id"] == "4"


async def test_errors(client_factory):
    c = client_factory(checks={"science": lane("science", 0.2)})
    assert (await c.get("/api/runs/nope")).status_code == 404
    assert (await c.post("/api/resolve", json={"query": ""})).status_code == 422
    assert (await c.post("/api/run", json={"asset": {"drug": "x"}})).status_code == 422
    r = await c.options("/api/run", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


@pytest.mark.live
async def test_live_resolve_then_run_real_science_lane(client_factory):
    c = client_factory(timeout_s=60)  # default lane loading: all 3 checks live
    asset = (await c.post("/api/resolve", json={"query": "Osimertinib"})).json()
    assert asset["ids"]["uniprot_id"] == "P00533"
    run_id = (await c.post("/api/run", json={"asset": asset})).json()["run_id"]
    await c.get(f"/api/runs/{run_id}/events")
    view = (await c.get(f"/api/runs/{run_id}")).json()
    assert view["check_results"]["science"]["status"] == "complete"
    assert view["check_results"]["trial"]["status"] == "complete"
    assert view["check_results"]["market"]["status"] == "complete"
    assert view["structure"] is not None
    assert view["structure"]["structure_id"] == "6JXT"
    # primary_failure_mode is the riskiest usable lane
    usable = {k: r for k, r in view["check_results"].items() if r["status"] in ("complete", "partial")}
    assert view["verdict"]["primary_failure_mode"] == max(usable, key=lambda k: usable[k]["score"])
    assert (await c.post("/api/resolve", json={"query": "zzqxv-not-a-drug-9931"})).status_code == 422
