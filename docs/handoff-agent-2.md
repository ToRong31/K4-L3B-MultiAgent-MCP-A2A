# Agent 2 handoff: refund timeline and source conflicts

## Changes

`payment/agent.py` now separates a **refund ID** (`refund_id`, `refund_reference`, or `refund_transaction_id`) from a **lifecycle event ID** (`event_id` or `refund_event_id`). Repeated identical events count once. When a refund has multiple states, `event_at`, `occurred_at`, or `updated_at` determines the latest state; list order has no meaning. An undated or tied contradictory history, changing known amount, missing refund ID, or conflicting duplicate event ID yields `payment_analysis.verdict=insufficient_evidence` and `refunded_total_brl=null`. Completed refunds are summed once per refund ID, including partial refunds. A current pending or failed refund does not contribute to the completed total.

`policy/agent.py` now emits the public array fact when a completed finding provides at least two distinct, explicit source names and a resolution code supplied by that finding or `get_policy.data.conflict_resolutions[field]`:

```json
{
  "kind": "data_conflicts",
  "data": {
    "items": [{
      "field": "shipment_analysis.verdict",
      "sources": ["get_order.order_delivered_customer_date", "get_shipment_summary.events"],
      "selected_source": null,
      "resolution_code": "TIMELINE_UNRESOLVED"
    }]
  },
  "evidence_refs": ["ev_aaaaaaaaaaaaaaaaaaaa", "ev_bbbbbbbbbbbbbbbbbbbb"]
}
```

The example is a test fixture; its code and refs are not runtime defaults. `selected_source` stays `null` unless an explicit selection is present and names one of the listed sources. `source_conflicts` remains an internal fact. For conflicts without sufficient source provenance or an explicit resolution code, Policy emits no public `data_conflicts` item and retains `assessment.case_status=needs_investigation`. It never fabricates a second source or a resolution code to satisfy the schema. Coordinator now consumes `data_conflicts.data.items`; Agent 3 was notified in their task.

## Evidence and unresolved integration limits

The live MCP `get_refund_timeline` description is “Return authoritative refund lifecycle events for a scoped order.” A successful response for case 002 had `events` with `event_at`, `event_type`, `amount_brl`, and `status`, but no refund or event ID. That payload cannot identify whether two rows refer to one refund, so the conservative result is `insufficient_evidence` with an unknown refunded total. The prior case 001 call failed at the server. No runtime ordering rule was inferred from array positions or fixture data.

Current Order and Shipment facts identify conflicting item IDs or a conflicting shipment verdict but do not carry two explicit source names or a grounded resolution code. Policy therefore cannot yet emit a public `data_conflicts` item for those specific facts. The necessary follow-up is to enrich the originating finding from actual MCP fields or provide a policy conflict rule with a code; this requires changes outside this review's file scope. Even when a rule names a selected source, Policy holds `needs_investigation` until dependent issue analysis can be recomputed safely.

The Coordinator already passes `context` and assembles public array facts, including `data_conflicts`. Agent 3 is editing and testing that integration. No Coordinator or public contract file was changed here.

## Tests and files

`pytest -q tests/test_domain_agents.py tests/test_policy_agent.py`: **15 passed**. New regressions cover timestamped pending-to-completed transitions in both list orders, repeated events, tied/undated contradictory statuses, conflicting amounts, missing refund ID, multiple refunds with partial total, and public conflict fact shape/ref linkage. Ruff passed on all Agent 2 files.

Files changed in this review: `src/student_agent/agents/payment/agent.py`, `src/student_agent/agents/policy/agent.py`, `tests/test_domain_agents.py`, `tests/test_policy_agent.py`, and this handoff. `domain_helpers.py` did not require changes.
