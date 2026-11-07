# Pre-Mortem: team context

UCL AgentVerse 2026 Biotech Hackathon.

## What we are building

Pre-Mortem is a biotech investment triage tool. Given a drug asset (drug, target, disease), it ranks the three ways the asset is most likely to fail:

| Check     | Question it answers                                              |
|-----------|------------------------------------------------------------------|
| `science` | Is the target-disease biology and the molecule's mechanism sound? |
| `trial`   | Is the clinical programme designed, powered and run to succeed?   |
| `market`  | If it works, can it win against competitors, payers and pricing?  |

The output is a draft verdict memo. A human signs it off before it counts.

## Architecture

```
            Asset (drug, target, disease, ids, as_of)
                              |
                        Orchestrator
            (parallel, hard timeout per check)
               /              |              \
        science check    trial check     market check
               \              |              /
                       Evidence ledger
              (every claim is a LedgerRow)
                              |
                       Verdict writer
                  (reads ONLY the ledger)
                              |
                      Human sign-off
                              |
                            Memo
```

The verdict writer never calls a data source or a model with outside context. If a fact is not in the ledger, the verdict cannot use it.

## Hard rules

1. **No mocked data, no hardcoded drug lists.** Live API calls only, cached via `shared/cache.py` so we survive rate limits.
2. **All numbers are computed in Python.** Models never assert a number. A model may explain a number Python computed.
3. **Every claim is a `LedgerRow`.** A `Reason` with no `ledger_row_ids` fails validation.
4. **Hard timeout per check.** The orchestrator degrades honestly to `status="partial"`, `"timeout"` or `"failed"`. Two working lanes still produce a verdict.
5. **Report missing evidence** in `CheckResult.missing`. Silence is not the same as "no risk".
6. **Respect the `as_of` cutoff.** No evidence dated after `Asset.as_of`. This is what makes back-testing honest.
7. **Record `licence` and `date_pulled`** on every `LedgerRow`.
8. **Gemini SDK** (`google-genai>=2.3.0`):
   - Use the Interactions API: `client.interactions.create(...)`, and `previous_interaction_id=` for multi-turn state.
   - Reasoning control: `generation_config={"thinking_level": "low" | "medium" | "high"}`.
   - Never use `thinking_budget`, `temperature`, `top_p` or `top_k`. They are deprecated.
   - Model IDs live in `shared/config.py::MODEL_IDS`. Do not hardcode model strings elsewhere.

## Contracts

All shared types live in `shared/schemas.py`. Change them only with a heads-up to the team, since every workstream imports them.

### Check interface

Every check package exposes one coroutine:

```python
# checks/<name>/__init__.py  (or checks/<name>/check.py, re-exported)
async def run_check(asset: Asset, emit: Callable[[StatusEvent], Awaitable[None] | None]) -> CheckResult: ...
```

- `emit` streams progress to the UI. Call it with `StatusEvent(check_id=..., status="running", message=..., timestamp=...)`.
- Return a `CheckResult` whose `check_id` matches the lane, with every `Reason` citing IDs present in its own `ledger_rows`.
- Do not catch the orchestrator's timeout. If you can return partial evidence early, set `status="partial"` yourself.

### Key types (summary)

| Type               | Purpose                                                                 |
|--------------------|-------------------------------------------------------------------------|
| `Asset`            | Input. `drug`, `target`, `disease`, resolved `ids`, optional `as_of`.  |
| `LedgerRow`        | One sourced fact: claim, source record, URL, `date_pulled`, licence.   |
| `Reason`           | Text plus the `ledger_row_ids` that back it (at least one).            |
| `CheckResult`      | A lane's score, confidence, status, reasons, missing evidence, rows.   |
| `StatusEvent`      | Progress event for the live UI.                                        |
| `StructurePayload` | Science lane's 3D structure view (PDB or AlphaFold) with residues.     |
| `RedFlag`          | One ranked risk in the verdict, linked to ledger rows.                 |
| `VerdictReport`    | Final draft: overall call, per-check summaries, red flags, sign-off.   |
| `SignOffAction`    | A human's approve / reject / amend action on a verdict.                |

## Repo layout

```
core/          orchestrator, ledger, asset resolution, verdict writer   (Core / Zoro)
checks/science science failure-mode check                               (Core / Zoro, later)
checks/trial   clinical trial failure-mode check
checks/market  market failure-mode check
api/           FastAPI service
web/           frontend
eval/          back-tests against known outcomes (uses as_of)
shared/        config, schemas, cache
tests/         pytest suite
```

## Workstreams

| Workstream | Owner | Scope                                                                                         |
|------------|-------|-----------------------------------------------------------------------------------------------|
| Core       | Zoro  | Orchestrator, ledger, asset resolution, Gemini 4 Argon wiring, verdict writer, science check |

## Dev setup

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add GEMINI_API_KEY
pytest
```

Python 3.10+ is required (schemas use `X | None` syntax).
