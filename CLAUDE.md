# CLAUDE.md

Instructions for coding agents working in this repo. Read `TEAM_CONTEXT.md` for the full project brief and contracts.

## Project

Pre-Mortem: a biotech investment triage tool that ranks three failure modes (science, trial, market) for a drug asset.

Pipeline: Orchestrator -> 3 checks in parallel -> evidence ledger -> verdict writer -> human sign-off -> memo. The verdict writer reads ONLY the ledger.

## Hard rules (non-negotiable)

1. No mocked data and no hardcoded drug lists. Live API calls, cached through `shared/cache.py`. Test doubles for *check functions* in orchestrator unit tests are fine; fake *evidence* in product code is not.
2. All numbers are computed in Python. Models never assert numbers.
3. Every claim must be a `LedgerRow`. Every `Reason` cites at least one ledger row ID.
4. Hard timeout per check. Degrade honestly: `status="partial"`, `"timeout"` or `"failed"`.
5. Report missing evidence in `CheckResult.missing`.
6. Respect the `Asset.as_of` cutoff. Never use evidence dated after it.
7. Record `licence` and `date_pulled` on every `LedgerRow`.
8. Gemini SDK (`google-genai>=2.3.0`): use the Interactions API (`client.interactions.create`, with `previous_interaction_id` for state). Set reasoning with `generation_config={"thinking_level": "low" | "medium" | "high"}`. NEVER use `thinking_budget`, `temperature`, `top_p` or `top_k`. Take model IDs from `shared/config.py::MODEL_IDS`.
9. This checkout belongs to Workstream: Core. Name: Zoro.

## Working conventions

- Python 3.10+ (use `.venv`, built with python3.11). Run tests with `.venv/bin/pytest`.
- Shared types live in `shared/schemas.py`. Changing them affects every workstream: keep changes additive and tell the team.
- Git: `git pull` before starting. Commit and push after each logical file or module, with a clear message. Small, frequent commits.
- Never commit `.env`, API keys or `.cache/`.
