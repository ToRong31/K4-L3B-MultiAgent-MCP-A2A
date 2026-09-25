# CONTEXT HANDOFF: K4-L3B-MultiAgent-MCP-A2A

> **Tài liệu bàn giao bối cảnh (Handoff Document)** dành cho AI Agent hoặc Developer tiếp quản dự án.
> **Mục tiêu**: Hiểu trọn vẹn yêu cầu nghiệp vụ, kiến trúc kỹ thuật, trạng thái hiện tại của repo và hướng dẫn triển khai chi tiết mà không cần đọc lại toàn bộ mã nguồn từ đầu.

---

## 1. Tổng quan Dự án & Yêu cầu Nghiệp vụ

* **Tên bài toán / Repo**: `K4-L3B-MultiAgent-MCP-A2A` (Cuộc thi Day 09 - Variant `l3b`).
* **Mục tiêu cốt lõi**: Xây dựng hệ thống Multi-Agent tự động tiếp nhận, điều tra và phân xử các khiếu nại của khách hàng trong thương mại điện tử (dựa trên tập dữ liệu Brazilian E-Commerce của Olist).
* **Nhiệm vụ chính**:
  1. **Entity Resolution**: Xác định chính xác đơn hàng (`order_id`) và khách hàng (`customer_unique_id`) thực sự bị ảnh hưởng từ danh sách ứng viên (`candidate_order_ids`) và gợi ý (`hint`), loại trừ các candidate rác/sai.
  2. **Điều tra Đa nguồn qua MCP**: Thu thập bằng chứng khách quan từ MCP Gateway (Order, Logistics, Payment, Refund, Policy, Seller).
  3. **Phân xử Khiếu nại**: Đánh giá các claim của khách hàng là đúng (`supported`) hay sai (`unsupported`), tìm nguyên nhân gốc rễ (`root_cause_analysis`), quy trách nhiệm (`seller`, `logistics_provider`, `platform`, `customer`), và tính toán số tiền hoàn trả (`recommended_refund_brl`).
  4. **Observable Tracing (A2A)**: Ghi nhật ký chuẩn xác các sự kiện điều phối giữa các agent (`task_assigned`, `handoff`, `tool_result_consumed`, `verification_completed`, ...).

---

## 2. Các Ràng buộc Quan trọng & Tiêu chí Chấm điểm

### Ràng buộc kỹ thuật bắt buộc:
1. **Giới hạn mô hình $\le 10$ tỷ tham số (Model $\le$ 10B parameters)**:
   - **Tuyệt đối không dùng** các mô hình lớn như GPT-4o, Claude 3.5 Sonnet, Gemini 1.5 Pro.
   - Các model được phép: **Qwen 2.5 (7B / 3B / 1.5B)**, **Llama 3.1 8B**, **Gemma 2 9B**, **Phi-3.5 mini (3.8B)**.
   - **Khuyến nghị kiến trúc (Hybrid)**: 80% logic nghiệp vụ (tính toán số tiền, so sánh mốc thời gian, đối soát trạng thái đơn) nên viết bằng **Deterministic Python Logic** để đảm bảo chính xác 100%, tốc độ cao, 0 chi phí token; chỉ dùng SLM (<10B) cho trích xuất hoặc phân loại ngôn ngữ tự nhiên khi thật sự cần.
2. **Tính xác thực của Bằng chứng (Evidence Provenance)**:
   - Mọi kết luận đều phải gắn với `evidence_ref` (định dạng `ev_...`) do MCP Gateway sinh ra.
   - **Cấm**: Bịa đặt `evidence_ref`, sửa hash, hoặc dùng chéo `evidence_ref` giữa các case khác nhau (Hard gate $\rightarrow$ 0 điểm ngay lập tức).
3. **Hiệu quả gọi Tool (Efficiency Budget)**:
   - Không spam gọi tool bừa bãi. MCP server ghi nhận mọi lượt gọi để chấm điểm `efficiency` (5%).

### Bảng trọng số chấm điểm công khai:
* **Semantic (40%)**: Độ chính xác của kết luận nghiệp vụ (đúng nguyên nhân, đúng bên chịu trách nhiệm, đúng số tiền).
* **Evidence (15%)**: Mức độ bao phủ và liên quan của bằng chứng.
* **Provenance (15%)**: Nguồn gốc bằng chứng khớp 100% với audit log của MCP.
* **Consistency (10%)**: Tính nhất quán nội tại (ví dụ: kết luận `seller_delay` thì `late_seller_ids` không được rỗng).
* **Schema (5%)**: Tuân thủ nghiêm ngặt JSON Schema.
* **Calibration (5%)**: Độ tự tin (`confidence`) phản ánh đúng độ chính xác.
* **Workflow (5%)**: Mức độ hoàn thiện của trace event A2A.
* **Efficiency (5%)**: Tối ưu số lần gọi MCP tool.

---

## 3. Trạng thái Hiện tại của Repository (Current State)

* **Môi trường & Dependencies**:
  - Python $\ge$ 3.11 trong virtualenv `.venv`.
  - Đã cài đặt các gói: `mcp`, `httpx2`, `jsonschema`, `python-dotenv`, `pytest`, `ruff`.
* **Cấu hình (`.env`)**:
  - Đã có file `.env` chứa `COMPETITION_API_URL`, `COMPETITION_TEAM_API_KEY`, `MCP_ENDPOINT`.
* **MCP Tools**:
  - Đã kiểm tra qua `day09 mcp-tools` $\rightarrow$ Kết nối thành công, nhận diện đủ **10 tools**:
    1. `get_customer_history`
    2. `get_order`
    3. `get_order_items`
    4. `get_order_payments`
    5. `get_payment_timeline`
    6. `get_policy`
    7. `get_product_context`
    8. `get_refund_timeline`
    9. `get_sellers`
    10. `get_shipment_summary`
* **Dữ liệu đầu vào**:
  - Đã có `case-set.json` và 100 file trong `inputs/L3B_CASE_001.json` đến `L3B_CASE_100.json`.
* **Tests**:
  - `pytest tests/test_starter.py` $\rightarrow$ **PASS**.
  - `test_release_safety.py` báo fail vì input đã được tải về (đây là test nội bộ của ban tổ chức trước khi public đề thi, không phải lỗi).
  - `ruff check .` $\rightarrow$ **PASS**.
* **Nơi cần lập trình**:
  - File [src/student_agent/workflow.py](file:///c:/Ki_OJT/Labs/Lab_9/K4-L3B-MultiAgent-MCP-A2A/src/student_agent/workflow.py) chứa hàm `async def solve_case(case, gateway, trace) -> dict` hiện đang để `raise NotImplementedError`.

---

## 4. Kiến trúc Luồng Multi-Agent Đề xuất

```text
Input Case
   │
   ▼
[Coordinator Agent] ───────────────────────────┐
   │                                            │
   │ (task_assigned: resolve_entity)            │
   ▼                                            │
[Entity Specialist]                             │
   ├─ Gọi get_customer_history / get_order      │
   ├─ Xác định resolved_order_ids & rejected    │
   └─ (handoff -> Coordinator)                  │
   │                                            │
   ▼                                            │
[Shipment Specialist]                           │
   ├─ Gọi get_shipment_summary                  │
   ├─ Tính: seller_delay vs logistics_delay     │
   └─ (handoff -> Coordinator)                  │
   │                                            │ (MCP Gateway & EvidenceRefs)
   ▼                                            │
[Payment Specialist]                            │
   ├─ Gọi get_order_payments / timelines        │
   ├─ Tính: captured, refunded, refundable      │
   └─ (handoff -> Coordinator)                  │
   │                                            │
   ▼                                            │
[Policy & Conflict Specialist]                  │
   ├─ Gọi get_policy                            │
   ├─ So khớp claims với policy & timelines     │
   └─ Đưa ra: primary_issue, root cause, refund │
   │                                            │
   ▼                                            │
[Verifier Agent] ◄──────────────────────────────┘
   ├─ Kiểm tra invariants & schema
   ├─ Bắn trace: verification_completed
   ▼
Output JSON (day09-l3b-output-v2)
```

---

## 5. Chi tiết Contract Đầu ra (Output Contract Specs)

Mọi case xử lý xong phải trả về dictionary tuân theo [contracts/schemas/l3b-output-v2.schema.json](file:///c:/Ki_OJT/Labs/Lab_9/K4-L3B-MultiAgent-MCP-A2A/contracts/schemas/l3b-output-v2.schema.json):

1. **`assessment`**:
   - `primary_issue`: Một trong các giá trị enum:
     `canceled_order_paid`, `unavailable_order_paid`, `late_delivery_seller`, `late_delivery_logistics`, `valid_split_payment`, `payment_mismatch`, `duplicate_charge`, `refund_pending`, `refund_failed`, `unsupported_claim`, `insufficient_evidence`.
   - `secondary_issues`: Danh sách chuỗi (tối đa 10).
   - `case_status`: `"action_required"` | `"no_action"` | `"needs_investigation"`.
   - `confidence`: Số thực từ `0.0` đến `1.0`.
2. **`entity_resolution`**:
   - `status`: `"resolved"` | `"ambiguous"` | `"not_found"`.
   - `resolved_order_ids`: Danh sách `order_id` đã được xác minh.
   - `rejected_candidates`: Danh sách `candidate_order_ids` bị loại trừ.
   - `confidence`: `0.0` - `1.0`.
3. **`customer_context`**:
   - `customer_unique_id`: string hoặc `null`.
   - `related_order_ids`: danh sách các order ID liên quan của khách.
4. **`shipment_analysis`**:
   - `verdict`: `"on_time"` | `"seller_delay"` | `"logistics_delay"` | `"lost"` | `"returned"` | `"conflicting"` | `"insufficient_evidence"`.
   - `late_seller_ids`: Danh sách seller bị trễ (nếu là `seller_delay`, danh sách này bắt buộc không được rỗng).
   - `timeline_complete`: boolean.
5. **`payment_analysis`**:
   - `verdict`: `"reconciled"` | `"capture_mismatch"` | `"duplicate_capture"` | `"refund_pending"` | `"refund_failed"` | `"refunded"` | `"insufficient_evidence"`.
   - `captured_total_brl`: float $\ge 0$ hoặc `null`.
   - `refunded_total_brl`: float $\ge 0$ hoặc `null`.
   - `refundable_total_brl`: float $\ge 0$ hoặc `null`.
6. **`root_cause_analysis`**:
   - `ranked_causes`: `[{"cause_code": "...", "rank": 1}, ...]`.
   - `responsible_parties`: `[{"party_type": "seller"|"platform"|"logistics_provider"|"payment_provider"|"customer"|"unknown", "party_id": "..."}]`.
7. **`evidence_refs`**: Mảng tập hợp tất cả các mã `ev_...` đã được sử dụng.
8. **`financial_resolution`**:
   - `currency`: `"BRL"`.
   - `recommended_refund_brl`: Số tiền đề xuất hoàn.
   - `refund_lines`: Danh sách chi tiết `[{"reason_code": "...", "amount_brl": ..., "entity_id": ...}]`.
9. **`resolution_actions`**: Mảng hành động đề xuất (ví dụ: `["REFUND_ORDER", "NOTIFY_CUSTOMER"]`).

---

## 6. Quy định về Observable Trace Events

Sử dụng `trace.emit(...)` trong [src/student_agent/trace.py](file:///c:/Ki_OJT/Labs/Lab_9/K4-L3B-MultiAgent-MCP-A2A/src/student_agent/trace.py). Các event bắt buộc theo chính sách chấm điểm:
1. `case_received` (Đã được gọi tự động trong `cli.py`).
2. `task_assigned` (Khi Coordinator giao việc cho Specialist).
3. `tool_result_consumed` (Khi Specialist nhận và sử dụng dữ liệu từ MCP tool kèm `evidence_refs`).
4. `handoff` (Khi Specialist trả kết quả về Coordinator).
5. `policy_decided` (Khi chốt quyết định bồi thường/xử lý theo policy).
6. `verification_completed` (Khi Verifier kiểm tra xong toàn bộ tính nhất quán).
7. `case_finalized` (Đã được gọi tự động trong `cli.py`).

---

## 7. Các bước Tiếp theo cho Agent Triển khai (Action Plan)

1. **Khảo sát dữ liệu mẫu qua MCP**:
   - Viết một script test nhỏ hoặc gọi thử nghiệm trên `L3B_CASE_001` để xem cấu trúc chi tiết trả về của `get_customer_history`, `get_order`, `get_shipment_summary`, `get_order_payments`, `get_policy`.
2. **Cài đặt logic chi tiết trong `workflow.py`**:
   - Chia module hóa hoặc viết các hàm phụ trợ cho từng specialist.
   - Áp dụng các quy tắc deterministic:
     - So sánh ngày: `order_delivered_carrier_date > shipping_limit_date` $\rightarrow$ `seller_delay`.
     - So sánh ngày: `order_delivered_customer_date > order_estimated_delivery_date` $\rightarrow$ `logistics_delay`.
     - So sánh tiền: Đơn bị cancel/unavailable mà `captured > 0` $\rightarrow$ `canceled_order_paid`.
3. **Thử nghiệm & Đóng gói**:
   - Chạy thử nghiệm: `day09 run` (hoặc test trên tập nhỏ trước).
   - Kiểm tra tính hợp lệ: `day09 validate`.
   - Đóng gói submission: `day09 package --output dist/submission.zip`.
