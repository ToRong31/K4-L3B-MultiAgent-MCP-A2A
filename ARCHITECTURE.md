# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Pipeline xử lý tuần tự theo giai đoạn, mỗi giai đoạn do một Specialist Agent phụ trách. Coordinator
điều phối luồng, phân phát kết quả giữa các agent qua shared state dict.

```text
Input → Entity Resolver → Coordinator dispatch
                              │
            ┌─────────────────┼─────────────────┐
            ▼                 ▼                 ▼
     Order Agent       Shipment Agent    Payment Agent
            │                 │                 │
            └─────────────────┼─────────────────┘
                              ▼
                       Policy Agent ← get_policy(EC_POLICY_V2)
                              │
                       Verifier Agent (cross-field consistency + calibration)
                              │
                         Output JSON
                              │
                      MCP ──► Trace (tool_result_consumed, handoff, …)
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| coordinator | case JSON | Điều phối pipeline, emit lifecycle events | none | Gọi từng agent, collect output |
| entity-agent | candidate_order_ids, customer_unique_id_hint | Resolve order ID thật, reject fakes, lấy customer context | get_order, get_customer_history | resolved_order_ids, rejected_candidates, customer_context |
| order-agent | resolved_order_ids | Thu thập order details, items, sellers, product context | get_order, get_order_items, get_sellers, get_product_context | affected_entities, order data |
| shipment-agent | order data | Phân tích timeline giao hàng, xác định delay | get_shipment_summary | shipment_analysis |
| payment-agent | order data | Đối soát thanh toán, refund | get_order_payments, get_payment_timeline, get_refund_timeline | payment_analysis |
| policy-agent | all evidence collected | Áp dụng policy, quyết định primary_issue, responsible_parties, financial_resolution | get_policy | assessment, root_cause_analysis, financial_resolution, resolution_actions |
| verifier-agent | full output draft | Kiểm tra cross-field consistency, calibration confidence | none | Validated final output |

Áp dụng least privilege; tool discovery không đồng nghĩa mọi actor đều được gọi mọi tool.

## 3. Entity resolution và A2A protocol

- Input chứa `candidate_order_ids` (thường 2 ID: 1 thật + 1 fake prefix `candidate-`).
- Entity Agent gọi `get_order` cho mỗi candidate. Candidate trả lỗi hoặc data rỗng → reject.
- Candidate thật: verify bằng `get_customer_history` với `customer_unique_id_hint`.
- Nếu customer history chứa order ID đó → `resolved`. Nếu không → `ambiguous`.
- Confidence entity resolution: 1.0 nếu chỉ 1 candidate hợp lệ, 0.7 nếu ambiguous.
- Handoff: entity-agent → coordinator (emit `handoff` event).
- Timeout: 30s per MCP call, max 2 retries. Không vòng lặp giữa agents.

## 4. Evidence và conflict lifecycle

- Mỗi MCP call trả về `evidence_ref` (pattern `ev_...`). Lưu vào `evidence_cache[tool_name]`.
- Emit `tool_result_consumed` ngay sau mỗi MCP call thành công.
- Data conflict: khi shipment vs order timeline khác nhau → ghi `data_conflicts` array.
- Evidence không tái sử dụng giữa case (cache clear mỗi case).
- Map evidence vào output: `evidence_refs` = tất cả refs thu thập trong case.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout | 2 | Skip tool, mark insufficient_evidence | error_mcp_timeout |
| Entity not found/ambiguous | 1 | Use claimed_order_id as fallback | entity_fallback |
| Source conflict | 0 | Record in data_conflicts, use policy precedence | conflict_detected |
| Invalid specialist result | 1 | Re-run with simplified query | specialist_retry |

Cache per-case: lưu kết quả MCP theo (tool_name, case_id, key_args) để tránh gọi trùng.
Query budget target: ~8-12 MCP calls/case (đủ cover 10 tools, tránh gọi thừa).

## 6. Verification invariants

Trước finalize, verifier kiểm tra:
- Schema compliance (output validate against l3b-output-v2)
- Entity scope: resolved_order_ids phải nằm trong candidate_order_ids
- Rejected candidates: không trùng resolved_order_ids
- Evidence ownership: tất cả evidence_refs phải từ MCP calls trong case hiện tại
- Claim linkage: claim verdicts phải có evidence_refs
- Timeline: shipment dates phải logic (purchase < approved < shipped < delivered)
- Payment/refund totals: recommended_refund_brl ≤ captured_total_brl
- Source precedence: selected_source trong data_conflicts phải hợp lý
- Responsibility/action consistency: nếu seller chịu trách nhiệm → action liên quan seller
- Confidence bounds: 0.0-1.0, giảm nếu có conflicts hoặc insufficient evidence

## 7. Reproducibility

- Model: qwen/qwen3.5-9b via OpenRouter (configurable via LLM_PROVIDER env)
- Dependencies: pinned in pyproject.toml
- Concurrency: sequential per case (no parallelism)
- Temperature: 0.1 for deterministic output
- Command: `day09 run` then `day09 package --output dist/submission.zip`
- Resource limits: 300s timeout per MCP session
