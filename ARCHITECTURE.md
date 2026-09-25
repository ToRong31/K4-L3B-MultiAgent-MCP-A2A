# L3B Architecture Record

Tài liệu này mô tả các quyết định có thể kiểm chứng của workflow. Hệ thống không ghi
prompt bí mật hoặc chain-of-thought vào output hay trace.

## 1. System overview

Thiết kế dùng **một process điều phối, nhiều actor logic và một model local dùng chung**.
Không khởi chạy một model riêng cho mỗi actor vì sẽ lãng phí VRAM và làm kết quả khó tái
lập. Các phép tính tiền, so sánh timestamp, kiểm tra schema và evidence ownership luôn do
code deterministic thực hiện; model chỉ hỗ trợ xếp hạng candidate hoặc adjudication khi
evidence thực sự mơ hồ.

```text
Case input
   │
   ▼
Input normalizer ──► Case-scoped evidence ledger/cache
   │                            │
   ▼                            ▼
Entity resolver ──MCP──► Coordinator/query planner
                              │
                  ┌───────────┼────────────┐
                  ▼           ▼            ▼
             Order/item    Shipment    Payment/refund
                  │           │            │
                  └──────┬────┴─────┬──────┘
                         ▼          ▼
                    Policy      Customer context
                         └────┬─────┘
                              ▼
                  Conflict resolver (rules first,
                     local model only if needed)
                              │
                              ▼
                  Deterministic verifier
                              │
                              ▼
                    Output JSON + trace
```

Mỗi case có state riêng. Không evidence, cache key, retry state hoặc model conversation nào
được chia sẻ giữa các case. Các specialist có thể chạy đồng thời sau khi entity đã được
resolve. MCP Streamable HTTP session hiện tại được gọi tuần tự để tránh multiplexing làm hỏng
transport; concurrency chỉ được tăng sau khi backend chứng minh hỗ trợ an toàn.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| `entity-customer` | candidate IDs, claimed ID, customer hint | Xác minh candidate, tìm customer duy nhất, lấy history khi scope yêu cầu | Chỉ order lookup/resolution và customer tools | `EntityResolutionResult` kèm accepted/rejected candidates và evidence refs |
| `coordinator` | normalized case và specialist results | Lập query plan tối thiểu, quản lý budget, phân công, tổng hợp | Không gọi domain tool trực tiếp ngoài discovery | Task envelope và final draft |
| `order-product` | resolved order IDs | Xác minh order status, items, sellers và product context cần thiết | Order, item, seller, product tools | `OrderProductResult` |
| `shipment` | resolved order/shipment IDs | Dựng timeline và phân loại seller/logistics delay, lost, returned | Shipment tools; order timestamp đã có từ ledger | `ShipmentResult` |
| `payment-refund` | resolved order/payment refs | Reconcile capture, split payment, duplicate và refund | Payment và refund tools | `PaymentResult` với Decimal amounts |
| `policy` | policy version, facts đã xác minh | Chọn rule áp dụng và action/refund được phép | Chỉ policy tools | `PolicyDecision` và decision code |
| `conflict-resolver` | typed specialist results | Áp dụng source precedence, giữ lại unresolved conflict | Không tự gọi MCP; yêu cầu coordinator bổ sung evidence nếu cần | Conflict list và resolved facts |
| `verifier` | final draft và evidence ledger | Kiểm tra schema, scope, provenance, consistency và confidence | Không gọi MCP trừ một verification call đã được planner phê duyệt | Pass/fail với machine-readable reason codes |

Least privilege được áp dụng bằng allow-list tool theo domain sau khi discovery. Tên tool
thực tế phải lấy từ MCP server lúc chạy; không hard-code hoặc đoán tên chưa được discover.

## 3. Entity resolution và A2A protocol

### Candidate resolution

1. Chuẩn hóa `claimed_order_id`, `candidate_order_ids` và customer hint; loại duplicate
   nhưng giữ thứ tự input.
2. Chỉ query trực tiếp từng candidate, không quét rộng. Candidate phải khớp record order
   thật và các ràng buộc nhận dạng có sẵn (customer, claim context hoặc relation evidence).
3. Exact verified order ID là tín hiệu mạnh; customer/order linkage và các entity liên quan
   là tín hiệu bổ sung. Không dùng string similarity như bằng chứng.
4. `resolved` khi có một candidate vượt threshold và có margin đủ lớn; `ambiguous` khi nhiều
   candidate còn hợp lệ; `not_found` khi không candidate nào có record hợp lệ.
5. Mọi candidate đã kiểm tra nhưng không chọn phải có trong `rejected_candidates`. Confidence
   bị giới hạn nếu còn conflict hoặc thiếu independent evidence.

Threshold và trọng số nằm trong config/versioned constants để có thể tune trên public partition
mà không thay đổi protocol.

### A2A envelope

Thông điệp nội bộ là typed data, không phải natural-language transcript:

```text
TaskEnvelope(case_id, correlation_id, actor, task_type, entity_ids,
             evidence_refs, deadline_ms, attempt)
ResultEnvelope(case_id, correlation_id, actor, status, facts,
               evidence_refs, warnings, decision_codes)
```

Receiver reject envelope sai `case_id`, correlation ID, actor hoặc schema. Mỗi task chỉ có
một handoff kế tiếp về coordinator. Retry giữ nguyên correlation ID, tăng `attempt`, tối đa
theo failure policy; không cho specialist tự giao việc vòng tròn. Trace chỉ ghi metadata quan
sát được, không ghi reasoning riêng.

## 4. Evidence và conflict lifecycle

`EvidenceLedger` tồn tại trong đúng một case và lưu immutable record gồm tool name, canonical
arguments, domain, `evidence_ref`, result hash, data và warnings. Cache key là
`(case_id, tool_name, canonical_arguments)`. MCP response phải pass public evidence schema
trước khi vào ledger; `evidence_ref` và result hash không được sửa hoặc tự tạo.

Lifecycle:

1. Specialist yêu cầu fact, coordinator kiểm tra ledger/cache và query budget.
2. Gateway call luôn truyền đúng `case_id`.
3. Response được validate và nhập ledger.
4. Khi fact được dùng mới emit `tool_result_consumed` với đúng actor/tool/evidence refs.
5. Final evidence list là union có thứ tự của refs thực sự hỗ trợ claim; không nhét evidence thừa.

Conflict resolver ưu tiên nguồn chuyên biệt: refund ledger cho refund state, payment ledger cho
captures, shipment timeline cho delivery state, order record cho order status và policy record
đúng version cho eligibility. Source cũ, incomplete hoặc có warning bị hạ hạng. Conflict còn ảnh
hưởng kết luận được ghi vào `data_conflicts`; nếu không có source thắng thì `selected_source`
là `null`, status chuyển `needs_investigation` khi phù hợp và confidence giảm.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout/transient 5xx | 2 lần, exponential backoff có jitter | Dùng cache; nếu vẫn thiếu thì insufficient | `handoff` / `MCP_RETRY_EXHAUSTED` |
| Entity not found | 0 blind retry | Kết thúc sớm với `not_found` | `handoff` / `ENTITY_NOT_FOUND` |
| Entity ambiguous | Tối đa 1 targeted query | Không tự chọn; trả `ambiguous` | `handoff` / `ENTITY_AMBIGUOUS` |
| Source conflict | Tối đa 1 targeted verification query | Ghi conflict và hạ confidence | `policy_decided` / `SOURCE_CONFLICT` |
| Invalid specialist result | 1 deterministic repair | Loại result, không phỏng đoán | `handoff` / `SPECIALIST_INVALID` |
| Local model timeout/invalid JSON | 1 constrained retry | Rules-only path | `handoff` / `MODEL_FALLBACK_RULES` |

Query planner resolve entity trước; chỉ sau đó mới fan-out các domain cần cho claim/scope.
Không query product/customer history nếu kết quả không thể thay đổi output. Negative result cũng
được cache. Calls giống nhau trong một case được coalesce bằng single-flight. MCP concurrency
mặc định 1. Early-stop khi đã đủ evidence để phân loại issue, tính refund và pass verifier.

## 6. Verification invariants

Verifier chạy deterministic trước khi `case_finalized`:

- output pass `day09-l3b-output-v2` và `case_id` khớp input;
- resolved/rejected candidates rời nhau, nằm trong scope và không duplicate;
- mọi affected entity liên kết với resolved order hoặc customer history được phép;
- mọi evidence ref tồn tại trong ledger, đúng case và đã được trace consumed;
- claim assessment dùng relevant refs và xử lý đủ claim IDs khi data cho phép;
- shipment events có thứ tự hợp lệ; verdict và `timeline_complete` nhất quán;
- amount được parse/tính bằng `Decimal`, không dùng binary float;
- captured/refunded/refundable/refund lines không âm; tổng lines bằng recommended refund sau
  khi quantize hai chữ số BRL;
- refund/action/status và seller responsibility nhất quán, không duplicate action;
- source precedence đã áp dụng hoặc conflict còn lại được khai báo;
- confidence trong `[0, 1]` và có cap theo evidence/conflict;
- đủ lifecycle receive → assigned/handoff → verification → finalized.

Verifier emit `verification_completed` với `decision_code=VERIFIED` chỉ khi toàn bộ invariant
pass. Nếu fail, coordinator có một vòng repair deterministic; không dùng model sửa số tiền,
entity IDs hoặc evidence refs.

## 7. Local model and hardware policy

Target hiện tại là NVIDIA GeForce RTX 4070 Laptop 8GB. Kiến trúc dùng một inference server
OpenAI-compatible và một model duy nhất không quá 10B parameters.

Lựa chọn chính: **Qwen3.5-9B Instruct Q4**, phù hợp giới hạn, đa ngôn ngữ, reasoning và
structured/tool use. Nếu runtime chưa ổn định trên Windows/CUDA, fallback là
**Qwen3-8B-AWQ/GGUF Q4_K_M**. **Gemma 4 E4B** (8B total parameters) là phương án thay thế khi
cần throughput/context efficiency tốt hơn.

Model không trực tiếp gọi MCP hoặc tự tạo final output. Nó nhận evidence digest typed và chỉ
trả `PlannerDecision` hoặc `ConflictDecision` theo JSON Schema. Temperature 0-0.2, fixed seed
khi runtime hỗ trợ, constrained decoding và tối đa một retry. Thinking chỉ bật cho ambiguous
adjudication; routing/extraction chạy non-thinking.

Với 8GB VRAM: chỉ load một model Q4, context mục tiêu 8K-16K, model concurrency 1 và KV cache
có giới hạn. Không chạy nhiều worker model trên cùng GPU. CPU đảm nhiệm validation, Decimal
reconciliation và orchestration. Có thể pipeline case nhưng không chạy đồng thời nhiều generation
dài vì sẽ gây KV-cache thrashing.

## 8. Reproducibility

- Python 3.11; dependencies pin theo `pyproject.toml` và lock file khi chốt runtime.
- Model ID, quantization filename/hash, runtime/version và chat template được ghi trong config.
- Default: MCP concurrency 1, model concurrency 1, temperature 0.1, fixed seed khi hỗ trợ.
- Commands: `day09 validate-inputs`, `day09 mcp-tools`, `day09 run`, `day09 validate`,
  `day09 package --output dist/submission.zip`.

## Bounded hierarchical runtime (v2)

`AgentRuntime` dùng duy nhất một `LocalModelWorker`; khóa `asyncio.Lock` serialize
mọi inference. `SupervisorAgent` chỉ chạy khi routing theo claim không đủ rõ;
deterministic route không thể bị model gỡ bỏ. Năm specialist nhận context từng
domain và trả hypothesis JSON có kiểm tra schema/ref/allow-list. `AdjudicatorAgent`
chỉ đưa candidate issue, claim verdict và confidence định tính. `CriticAgent`
chỉ đưa warning; cả hai không ghi final JSON.

`ToolRequest` đi qua kiểm tra case/actor/tool của `EvidenceLedger`, cache theo
case, single-flight, retry và budget trước khi gọi MCP. Raw `EvidenceRecord`
được đóng băng đệ quy. `DerivedFact` chỉ sinh từ evidence ref có thật. Dữ liệu
nghiệp vụ, tiền Decimal, source precedence, schema và consistency tiếp tục do
Python quyết định. `verify_output` chạy trước và sau bước calibration; model
không có quyền sửa amount, entity, evidence hoặc action. JSON lỗi, ref giả,
timeout và lỗi inference được retry một lần rồi fallback theo từng agent.

`day09 run` yêu cầu model local online, không tự âm thầm chuyển sang
deterministic-only. Chỉ `--allow-model-fallback` mới cho phép điều đó. Trace
chứa task/correlation IDs và fallback reason, metrics riêng chứa model/MCP calls.
Gateway MCP hiện serialize do session streamable HTTP chưa được chứng minh an
toàn ở concurrency 3; đây là giới hạn vận hành, không phải nhiều model worker.
- Mỗi run bắt đầu với output/trace sạch; evidence không persist/reuse giữa runs.
- Submission chỉ chứa manifest, trace và outputs; không chứa model, `.env`, inputs hoặc logs.
