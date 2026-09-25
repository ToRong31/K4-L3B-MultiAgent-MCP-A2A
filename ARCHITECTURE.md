# L3B Architecture Record

## 1. System overview

```text
Case input → Coordinator / Router
                ├─ A2A → Order/Item Agent ─┐
                ├─ A2A → Payment Agent ────┼─ MCP Evidence Collector
                └─ A2A → Shipment Agent ───┘           ↓
                                           Policy Agent (A2A)
                                                  ↓
                                           Verifier Agent (A2A)
                                                  ↓
                                      Validated l3b-output-v2 JSON
```

`solve_case(case, gateway, trace)` trong `workflow.py` là entry point. Coordinator giao ba specialist đầu bằng A2A SDK v1, đợi cả ba hoàn tất, rồi chuyển findings và evidence refs cho Policy. Sau khi ghép draft theo L3B schema, Coordinator gửi draft cho Verifier. Các endpoint A2A chạy trên loopback với Agent Card và JSON-RPC. Giao thức nội bộ dùng `WorkOrder`/`Finding` phiên bản 1; đây không phải các JSON schema công khai để nộp bài.

**Trạng thái hiện tại:** Coordinator gọi entity resolution trước ba specialist, ghép facts theo tên phần output, chuyển Policy rồi Verifier. Verifier kiểm tra schema, scope, provenance cục bộ và các bất biến nhất quán; chỉ `approved=true` mới được finalize. Các specialist và policy cần được đối chiếu với bàn giao riêng trước khi xác nhận chạy nghiệp vụ thật. Nếu thiếu evidence, draft biểu diễn `needs_investigation` và vẫn qua Verifier.

## 2. Agent ownership và tool permission

Tên tool dưới đây được lấy bằng `day09 mcp-tools`. `EvidenceCollector` chỉ cho gọi tool thuộc allowlist của actor và đã xuất hiện trong discovery.

| Actor | Input | Trách nhiệm | MCP tools được phép | Handoff/output |
| --- | --- | --- | --- | --- |
| Coordinator | Case, candidate IDs, customer hint | Route, correlation, customer context, conflict handling, ghép draft | `get_customer_history`, `get_order` | `WorkOrder`, draft L3B |
| Order/Item | Case và candidate IDs | Resolve order; item, seller, product facts | `get_order`, `get_order_items`, `get_product_context`, `get_sellers` | `Finding` và refs |
| Payment | Case và order candidates | Capture, payment, refund reconciliation | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | `Finding` và refs |
| Shipment | Case và order candidates | Shipment timeline, nguyên nhân chậm/trả/mất | `get_shipment_summary` | `Finding` và refs |
| Policy | Findings, policy version, refs | Áp dụng chính sách đã truy xuất, đề xuất action | `get_policy` | Policy `Finding` |
| Verifier | Draft L3B, findings, refs | Kiểm tra độc lập schema, provenance và consistency | Không có | Kết quả xác minh |

`QUERY_BUDGET` theo case/actor: Coordinator 8, Order 8, Payment 6, Shipment 4, Policy 3, Verifier 0. Cache chỉ theo `case_id + tool + arguments` trong cùng run; case khác luôn gọi mới. Budget tính cả lần retry có thể bị MCP audit.

## 3. Public contracts: khóa cứng

Không sửa hoặc thêm field vào các file trong `contracts/schemas/`. `Contracts` kiểm tra fingerprint SHA-256 trên JSON đã canonicalize của năm public schema: L3A output, L3B output, trace event, submission manifest và MCP evidence response. Sửa nghĩa schema hoặc thiếu file sẽ làm ứng dụng từ chối chạy. Đổi whitespace không thay đổi fingerprint.

- `EvidenceGateway.call()` validate envelope MCP trước khi agent dùng `data` hoặc `evidence_ref`.
- `TraceWriter.emit()` validate từng event trước khi ghi.
- Verifier validate draft bằng `l3b-output-v2`; Coordinator validate lại sau khi Verifier phê duyệt; CLI validate final output.
- `package_submission()` validate từng output, trace và manifest trước khi tạo ZIP.

Chỉ phát các field được định nghĩa trong schema tương ứng. Schema công khai là nguồn quyết định khi code hoặc prompt bất đồng với nó. Không đưa memory, A2A envelope, prompt, tool output thô hoặc API key vào output/trace/manifest.

## 4. Entity resolution và A2A handoff

`WorkOrder` gồm `version`, `case_id`, `target`, `task_id`, `input`, `evidence_refs`. `Finding` gồm cùng correlation, `status`, facts, refs, tool uses và open questions. Coordinator chỉ chấp nhận Finding khớp cả case, target và task; ref mới phải khớp MCP tool result được ghi trong memory. Handoff không được đổi `case_id` hoặc dùng evidence của case khác.

Candidate order IDs từ input chỉ là ứng viên. Order/Item phải xác nhận bằng MCP, ghi rejected candidates và confidence; không được coi `claimed_order_id` là sự thật. Khi còn ambiguous hoặc not found, downstream giữ trạng thái thiếu bằng chứng. Coordinator chỉ phát sinh một handoff cho mỗi phase; ba specialist đầu chạy song song, Policy và Verifier chạy tuần tự. Mỗi A2A call timeout sau 60 giây; không tự lặp handoff để tránh vòng vô hạn.

## 5. Evidence, conflict và memory

MCP response hợp lệ có `schema_version`, `evidence_ref`, `result_hash`, `domain`, `data`. Collector ghi tool call và raw output vào memory của agent và scope `orchestrator`; Finding mang tool uses để Coordinator phát `tool_result_consumed` trong trace. Trace chỉ chứa sự kiện quan sát được, không chứa lập luận nội bộ. Ref trong Finding phải xuất phát từ handoff trước hoặc tool result cùng `case_id` và `task_id`.

Assembler ghép các phần output theo fact `kind`. `claim_assessments=[]` (tùy chọn) và `data_conflicts=[]` (không có xung đột) không làm thay đổi assessment, actions hoặc refund. Nhiều facts đóng góp danh sách được ghép và loại trùng; `affected_entities` ghép các tập ID, còn `data_conflicts` loại trùng kể cả khi thứ tự `sources` khác nhau. Fact `data_conflicts.data.items` của Policy và evidence refs đi kèm được giữ nguyên. Với hai object fact mâu thuẫn, assembler không chọn ngầm một nguồn: nó ghi conflict chưa giải quyết và dùng giá trị mặc định cho đúng phần đó.

Giá trị mặc định chỉ để output hợp schema, không được coi là chứng cứ nghiệp vụ. Thiếu Payment làm giảm trạng thái của kết luận payment; thiếu Shipment không xóa kết luận refund đã được Payment và Policy hỗ trợ. Thiếu số tiền hoàn từ Policy giữ nguyên primary issue và actions, thêm `refund_amount_unverified` vào secondary issues; các issue phụ thuộc refund chuyển sang `needs_investigation`. Số `recommended_refund_brl=0` khi thiếu fact financial là placeholder do schema bắt buộc, không phải kết luận không cần hoàn. Coordinator vẫn yêu cầu Verifier `approved=true` và schema hợp lệ trước khi finalize.

SQLite memory tách theo `(case_id, agent_id)`. Scope `orchestrator` giữ case input, handoff, tool call/output và findings; mỗi agent giữ lịch sử riêng. Khi context ước lượng vượt 80% phần còn lại sau output reserve, các turn cũ được tóm tắt vào checkpoint. Raw events không bị xóa; compact không được sửa evidence refs. Database `runtime/` bị ignore và không nằm trong ZIP nộp bài.

Năm server và Coordinator phải dùng cùng đường dẫn `AGENT_MEMORY_DB` và cùng `L3B_RUN_ID`. Runtime hiện từ chối khởi động khi thiếu run ID để tránh mỗi process tự tạo một scope khác nhau. Server Verifier không cần kết nối MCP hoặc LLM; các specialist hiện phân tích tất định nên LLM là tùy chọn.

## 6. Failure và efficiency policy

| Failure | Retry budget | Hành vi | Dấu vết |
| --- | ---: | --- | --- |
| MCP timeout/network | 1 retry, chờ 250 ms | Tính cả hai call vào budget; sau đó trả failed và dừng case | `tool_error` trong memory; `task_assigned` trong trace |
| MCP tool trả lỗi/invalid schema | 0 | Dừng, không tạo evidence thay thế | Lỗi terminal, không phát `tool_result_consumed` |
| Tool ngoài quyền/chưa discovery | 0 | Chặn trước MCP call | Permission/validation error |
| A2A timeout hoặc Finding sai correlation | 0 | Dừng case; không ghép output | `task_assigned`; không có handoff thành công |
| Entity ambiguous/not found | 0 | `needs_investigation`, không chọn candidate tùy tiện | Handoff/finding, sẽ thêm decision code khi logic hoàn thiện |
| Source conflict | 0 | Lưu hai nguồn, xử lý theo policy; unresolved nếu thiếu căn cứ | `data_conflicts` trong final output |

## 7. Verification invariants

Verifier hiện kiểm tra schema, `case_id`, context, entity scope, rejected candidates, ref trong tool result đã lưu cùng case, liên kết ref trong fact, tổng refund, trần refundable, refunded/captured, issue/status/action, verdict shipment/payment, timeline flag và confidence. Nó trả fact `verification` chứa `approved` và danh sách lỗi có `target`. Coordinator giao sửa theo target tối đa một vòng mặc định. Việc xác thực team/run trên audit server và đối chiếu ngữ nghĩa claim/action với payload MCP chưa được Verifier chứng minh chỉ bằng memory cục bộ; cần kiểm tra khi MCP thật chạy.

## 8. Reproducibility

Python >=3.11; `a2a-sdk[http-server]==1.1.5`; LLM client chọn OpenRouter hoặc OpenAI bằng `LLM_PROVIDER` và chỉ khởi tạo khi provider được chọn có cấu hình. Server mặc định chạy loopback trên các cổng 9001–9005. Không dùng random seed trong quyết định nghiệp vụ; `task_id`/trace event ID là định danh ngẫu nhiên. Fixture tests kiểm tra Coordinator, logic Policy thật và Verifier thật; case thật cần `L3B_RUN_ID` và MCP. `day09 run` từ chối ghi đè output/trace hiện có. Thử nghiệm live trước bản sửa đi qua năm A2A process cho case 001; Payment thất bại ở `get_refund_timeline`, output giữ `needs_investigation`. Chưa có thử nghiệm MCP mới sau bản sửa assembler. Không ghi secret vào tài liệu hoặc artifact.
