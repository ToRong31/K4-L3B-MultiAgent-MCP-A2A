# Agent 3 handoff: Verifier, Coordinator, integration

## Delivered

- Deterministic Verifier returns a completed `verification` fact with `approved` and targeted errors. It checks L3B schema, case and entity scope, stored case/run MCP refs, nested ref linkage, money totals and limits, status/actions, issue/verdict, timeline flag, confidence and shipment responsibility.
- Coordinator invokes Agent 1's entity resolver, routes resolved cases to Order/Payment/Shipment over A2A, collects Policy, assembles standardized facts and calls Verifier. Unresolved entity or missing specialist evidence produces a `needs_investigation` draft that is still verified. A rejected draft gets at most one repair handoff to the named specialist by default. Final output requires both `approved=true` and valid public schema.
- Assembler records conflicting object facts in `data_conflicts` and does not silently choose one. It merges array contributions, deduplicates conflict items even when source order differs, and preserves Policy conflict refs. Absent optional `claim_assessments` and empty `data_conflicts` do not lower a supported conclusion. Missing section defaults only satisfy schema; dependency checks determine whether a particular assessment needs investigation. Facts from an intermediate order outside the resolved scope are rejected.
- Servers and workflow require a shared `L3B_RUN_ID`. Verifier starts without MCP; LLM startup is optional because these agents currently analyze deterministically. CLI `run` refuses to erase existing outputs or trace.
- Added `tests/test_verifier_agent.py`, `tests/test_workflow_integration.py`; updated `ARCHITECTURE.md`.

## Integration and verification

- Read both `handoff-agent-1.md` and `handoff-agent-2.md`; their WorkOrder context, fact shapes and memory run scope are reflected in Coordinator.
- Before this fix, a completed `refund_pending/action_required` fixture with action `REVIEW_REFUND` and recommended refund 20 BRL became `insufficient_evidence/needs_investigation`, actions `[]`, refund 0 solely because `claim_assessments` and `data_conflicts` were absent. Supplying both as empty arrays preserved the original result. Regression now produces the same supported conclusion with or without those optional arrays.
- New integration tests exercise real `analyze_policy` plus real `VerifierAgent`, including an approved `action_required` output, and confirm Policy's public `data_conflicts` item and evidence refs survive assembly. Tests also cover multiple list contributions, missing financial amount, and targeted specialist gaps.
- Final full `pytest -q`: 48 passed, 1 workspace-specific failure in `test_release_safety.py` because root `case-set.json` exists. Ruff on Agent 3 files passed.
- Live MCP and five separate A2A processes: case 001, 002, 003 and 010 all reached Verifier. Case 001 and 010 Payment failed when MCP `get_refund_timeline` returned a tool error; outputs correctly remained `needs_investigation`. Cases 002 and 003 completed Payment but also remained `needs_investigation` based on available policy and findings. These are schema-valid outputs, not proven correct business decisions.
- A broader trial was started in a separate temporary artifact root and **stopped at the user's request** after 38 output files. It is incomplete; no claim of 100-case completion, `validate` pass, `package` pass or business score is made. Trial files are under `C:\tmp\l3b-agent3-b5478030a5124f82a38f506c33e5cfbf`, outside the repo.
- `test_release_safety` was run separately within the full suite and failed on the expected release-inventory assertion. No input was deleted and the test was not disabled.
- No new MCP result is claimed for this assembler fix. The attempted launch of a fresh two-case A2A trial was rejected by automatic approval review with `blocked by policy`; no server or output was created by that attempt. The earlier 38-output trial was not overwritten or resumed.

## How to run later

Set a unique shared `L3B_RUN_ID` and the same absolute `AGENT_MEMORY_DB` for Coordinator and all five servers. Start `python -m student_agent.agents.serve {order|payment|shipment|policy|verifier} PORT --root REPO` on ports 9001–9005 and set matching `A2A_*_URL`. Use a fresh trial root with copied public contracts, case-set and inputs; load the repo's `.env` into the launching environment. Run `day09 --root TRIAL run`, then `validate` and `package` only if all expected outputs exist. The CLI refuses to overwrite existing outputs and trace.

## Remaining limits

- MCP refund timeline errors and absent stable payment event IDs limit payment conclusions. Do not infer a refund or duplicate capture from amount alone.
- Local SQLite confirms only the current case/run's stored MCP envelopes. It cannot independently attest the competition server's team/run audit; validate those against server audit when available.
- Verifier checks structural and cross-field consistency. It does not prove all policy precedence or semantic entailment from raw domain payloads. A valid `needs_investigation` output is not a high-confidence business resolution.
- If Policy omits `financial_resolution` for a refund-dependent issue, assembler keeps the issue and actions but switches status to `needs_investigation` and adds `refund_amount_unverified`. The schema-required numeric 0 is a placeholder, not a conclusion that no refund is due. Verify this interpretation against downstream scoring before a 100-case run.
- Before any 100-case batch: resolve remaining MCP refund timeline errors and missing event IDs, run a fresh small live A2A group with the new assembler, inspect determinate and conflict cases, then validate and package a complete isolated run. No 100-case batch was run in this task.
- The Agent 1 helper `validate_context` currently cannot resolve relative schema refs when invoked on a detached subsection; Coordinator validates context through a complete L3B draft instead.
