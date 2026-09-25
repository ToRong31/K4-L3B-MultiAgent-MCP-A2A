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
                 Deterministic Policy Agent ← get_policy(EC_POLICY_V2)
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
| order-agent | resolved_order_ids | Thu thập item, seller ID và product context | get_order_items, get_product_context | affected_entities, order/product data |
| shipment-agent | order data | Phân tích timeline giao hàng, xác định delay | get_shipment_summary | shipment_analysis |
| payment-agent | order data + claim topic | Đối soát payment; chỉ gọi lifecycle/refund tool khi issue liên quan | get_order_payments; conditional get_payment_timeline/get_refund_timeline | payment_analysis |
| policy-agent | all normalized evidence | Áp dụng finite-state rules và policy; evidence mạnh được ưu tiên hơn customer claim | get_policy | assessment, root_cause_analysis, financial_resolution, resolution_actions |
| verifier-agent | full output draft | Kiểm tra cross-field consistency, calibration confidence | none | Validated final output |

Áp dụng least privilege; tool discovery không đồng nghĩa mọi actor đều được gọi mọi tool.

## 3. Entity resolution và A2A protocol

- Input chứa `candidate_order_ids` (thường 2 ID: 1 thật + 1 fake prefix `candidate-`).
- Entity Agent gọi `get_order` cho mỗi candidate. Candidate trả lỗi hoặc data rỗng → reject.
- Candidate thật: verify bằng `get_customer_history` với `customer_unique_id_hint`.
- `get_order` quyết định resolved/not-found; customer history là independent verification để hiệu chỉnh confidence.
- Confidence entity resolution: 0.98 nếu một candidate hợp lệ và customer history xác nhận; giảm còn 0.82 nếu history không đầy đủ.
- Handoff: entity-agent → coordinator (emit `handoff` event).
- Mỗi logical query chỉ có một audited attempt; HTTP session có total timeout 300s. Không vòng lặp giữa agents.

## 4. Evidence và conflict lifecycle

- Mỗi MCP call trả về `evidence_ref` (pattern `ev_...`). Lưu vào `evidence_cache[tool_name]`.
- Emit `tool_result_consumed` ngay sau mỗi MCP call thành công.
- Data conflict: khi shipment vs order timeline khác nhau → ghi `data_conflicts` array.
- Evidence không tái sử dụng giữa case (cache clear mỗi case).
- MCP payload được parse đệ quy để hỗ trợ cả record lồng nhau và numeric string (ví dụ `"110.00"`).
- `evidence_refs` cấp case chứa toàn bộ evidence đã thực sự tiêu thụ; mỗi `claim_assessment` chỉ gắn refs thuộc domain liên quan để tăng evidence precision.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout/error | 0 automatic retries | Skip tool, mark insufficient_evidence | warning log; no duplicate audited call |
| Entity not found/ambiguous | 0 | Try claimed_order_id only if it was not already checked | handoff attributes |
| Source conflict | 0 | Record in data_conflicts; shipment summary wins delivery fields | policy_decided conflict_count |

Cache per-case: lưu kết quả MCP theo (tool_name, case_id, key_args) để tránh gọi trùng.
Query budget target: 7 base calls/case; 8 calls cho payment/refund lifecycle case có timeline
authoritative. `get_sellers`
không được gọi vì seller IDs đã có trong `get_order_items`; timeline tools không được gọi dàn trải.

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

- Decision engine: deterministic rules over normalized MCP evidence; no external LLM call in the scoring path
- Dependencies: pinned in pyproject.toml
- Concurrency: sequential per case (no parallelism)
- Command: `day09 run` then `day09 package --output dist/submission.zip`
- Resource limits: 300s timeout per MCP session
