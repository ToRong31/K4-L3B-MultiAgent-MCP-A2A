# Agent 1 handoff: MCP evidence and entity resolution

## Status checked on 2026-09-25

- `case-set.json` and all 100 `inputs/L3B_CASE_*.json` files exist; `validate-inputs` passed.
- `.env` has endpoint and team key configured. MCP discovery succeeded and returned ten tools. Secrets were not printed.
- Discovery schemas confirmed `get_order(case_id, order_id)` and `get_customer_history(case_id, customer_unique_id)`. `list_tools()` still returns sorted names; `describe_tools()` returns `{name: input_schema}`.
- One real `get_order` and one real `get_customer_history` lookup for case 001 confirmed response structure. A nonexistent candidate caused a tool error, so that failure alone is not proof of rejection.

## Interfaces

```python
async def resolve_case_context(case: dict, evidence: EvidenceCollector,
                               memory: AgentMemory) -> dict:
    # {"context": {"entity_resolution": ..., "customer_context": ...},
    #  "evidence_refs": [...], "open_questions": [...]}
```

`EvidenceCollector.call(agent_id, tool_name, *, case_id, turn_id, **arguments)` validates discovered input schemas when available, enforces role permissions and a per case/run/actor budget, retries one transient error, persists the full response, and records `tool_result` even on cache hits. `tool_schema(name)` exposes a discovered schema. `tool_uses(case_id, agent_id, turn_id)` supplies the current task's provenance.

`AgentMemory(path, run_id=...)` stores append only events, checkpoints, evidence contents and cache entries in SQLite. `memory.get_evidence(case_id, evidence_ref)` retrieves full content only for the current `run_id` and case. This is how a recipient of `WorkOrder.evidence_refs` reads each ref. `memory.cached_evidence(...)` supports another process using the same database and run. The default run ID is `L3B_RUN_ID` from the environment or a fresh UUID.

`validate_fact(fact, allowed_refs)`, `validate_context(context, contracts)` and `validate_work_input(value)` are internal helpers in `investigation_contract.py`. Facts use exactly `{"kind": "...", "data": {...}, "evidence_refs": [...]}`. The context example below shows structure only; its IDs and ref placeholder are illustrative, not submitted runtime evidence:

```json
{
  "entity_resolution": {
    "status": "resolved", "resolved_order_ids": ["order-A"],
    "rejected_candidates": ["order-B"], "confidence": 0.95
  },
  "customer_context": {
    "customer_unique_id": "customer-A", "related_order_ids": ["order-A"]
  }
}
```

Resolver requires the order response to name the queried candidate and the customer history to contain that order with a matching customer row ID. Multiple matches remain `ambiguous`; an unverified customer leaves an open question. Rejections are supported by contradictory order/customer evidence, or by verified customer history excluding a candidate whose order lookup failed. No result ref is invented for a failed tool call.

## Integration changes for other owners

1. Coordinator/workflow and every A2A server must share the same SQLite path and `L3B_RUN_ID` for one execution. Launch the servers with that environment value, then run the coordinator with it. Rotate it for each new run. Current `serve.py` and `workflow.py` construct `AgentMemory` independently, so without this setting each process gets a different run scope. This is a required integration step outside Agent 1's allowed files.
2. Coordinator should call `resolve_case_context` before specialist handoffs and pass `{"case": case, "context": result["context"]}` plus `result["evidence_refs"]` in each `WorkOrder`. Recipients can call `get_evidence` for transferred refs. Keep `WorkOrder`/`Finding` and public schemas unchanged; no new A2A target is needed. Resolver's `get_order` permission is limited to the existing `orchestrator` actor and its budget is eight calls, covering customer history and candidate checks with bounded retry.
3. Coordinator should use `validate_fact`, `validate_context` and `validate_work_input` when accepting findings and preparing handoffs. A ref's shape alone does not prove ownership; check it is retrievable in the current case/run and cited by the current task or prior handoff.

## Limits

- The current coordinator does not invoke the resolver, and the business specialists/output assembly remain owned by other agents. No claim of completed case investigation follows from these tests.
- SQLite run isolation is explicit. Processes launched without a shared `L3B_RUN_ID` cannot read one another's evidence. Separate machines also need a shared durable store or evidence transfer protocol.
- Candidate lookup tool errors are not assumed to mean “not found.” The resolver can reject such a candidate only if confirmed customer history excludes it.
- Budget checks are durable across sequential process calls; simultaneous writes by multiple collectors for the same actor/case need a transactional reservation if that deployment pattern is used.

## Verification

`pytest -q tests/test_entity_resolution.py tests/test_evidence_runtime.py tests/test_contract_and_evidence.py tests/test_skeleton.py` passed (12 tests). Tests use simulated responses only; runtime code does not create fake evidence refs. The full suite currently has one unrelated failure in `test_release_safety.py`: it asserts `case-set.json` must be absent, while the current repo contains the supplied 100-case input set. Agent 1 did not change that test.
