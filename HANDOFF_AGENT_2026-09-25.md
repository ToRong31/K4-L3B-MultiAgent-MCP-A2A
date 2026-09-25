# Handoff cho agent tiếp theo — 2026-09-25

## Yêu cầu hiện hành

Người dùng muốn hoàn thiện kiến trúc **Bounded Hierarchical Multi-Agent + Deterministic Evidence Graph**, dùng **LLM thật <=10B** (ưu tiên IFM/K2-Horizon-7B GGUF Q4_K_M), sau đó nếu chạy được thì chạy đủ 100 case, validate, đóng ZIP để nộp. Yêu cầu gốc đầy đủ ở `C:\Users\Admin\.codex\attachments\27e9ea9d-2a48-432f-82a0-b554466db94d\Pasted text.txt`. Người dùng vừa yêu cầu **dừng lại và handoff**; không tiếp tục chạy batch trong lượt này.

## Trạng thái xác nhận

- Repo: `C:\Ki_OJT\Labs\Lab_9\K4-L3B-MultiAgent-MCP-A2A`.
- `day09 validate-inputs`: pass, 100 case.
- `pytest -q`: 22 passed sau các thay đổi hiện tại.
- `ruff check .`: pass sau các thay đổi hiện tại.
- Chưa chạy batch 100 case bằng kiến trúc mới; chưa validate/package bản mới. ZIP cũ trong `dist/` không phản ánh code mới.
- Local model server `127.0.0.1:8080/v1` không trả lời; `llama-server`, `ollama`, `winget` không có trong PATH. Không tìm thấy GGUF local.
- RTX 4070 Laptop: 8188 MiB tổng, khoảng 5370 MiB trống khi kiểm tra. K2 Q4_K_M trên model card là 5.59 GB nên có thể phải giảm GPU layers / giải phóng VRAM.
- Theo model card chính chủ: GGUF K2 hiện cần llama.cpp có hỗ trợ kiến trúc K2; PR upstream đang tiến hành, fork chính chủ là `https://github.com/MBZUAI-IFM/llama.cpp/tree/model/K2Horizon`. Model card: `https://huggingface.co/IFM/K2-Horizon-7B-GGUF`.
- Thử `git clone` fork vào `C:\Users\Admin\AppData\Local\Temp\k2-llama-runtime` bằng network escalated; clone chậm, đã **dừng bằng Ctrl+C** theo yêu cầu user. Thư mục tạm có thể còn dở dang, không nằm trong repo. Không tải model weights.

## Code đã sửa, chưa commit

- `src/student_agent/model_worker.py`: một HTTP OpenAI-compatible worker có `asyncio.Lock` (inference concurrency 1), model/config local.
- `src/student_agent/agents.py`: 8 agent logical, schema JSON, instructions, allowlist, timeout/retry, task/correlation ID, kiểm tra evidence refs.
- `src/student_agent/agent_runtime.py`: route deterministic + Supervisor khi cần; specialist, adjudicator, critic.
- `src/student_agent/evidence.py`: `ToolRequest`, actor-tool guard, per-case cache, single-flight, negative cache, budget/retry, immutable raw evidence.
- `src/student_agent/workflow.py`: route và LLM review tích hợp, DerivedFacts, deterministic output/Decimal giữ nguyên, verifier trước và sau confidence calibration.
- `src/student_agent/cli.py`, `config.py`, `.env.example`: model config, run yêu cầu model online mặc định; `--allow-model-fallback` là lựa chọn explicit. **CLI chưa smoke-test chat completion, mới GET /models.**
- `src/student_agent/domain.py`: hỗ trợ tuple do evidence đóng băng.
- `tests/test_agents.py`, `tests/test_workflow.py`, `tests/test_release_safety.py`: offline tests và cập nhật test release để chấp nhận input local đã gitignore.
- `README.md`, `ARCHITECTURE.md`: hướng dẫn mới.

`pyproject.toml`, `HANDOFF.md`, `run-artifacts/` đã là thay đổi/untracked của user trước lượt này; **không ghi đè/xóa**. Những file code trên là thay đổi của lượt này. Không commit/tag trong lượt này.

## Việc tiếp theo ưu tiên

1. Đọc yêu cầu gốc và audit diff; kiểm tra kỹ `agents.py`, `agent_runtime.py`, `workflow.py` trước official run. Hiện agent review chỉ **calibrate confidence**; model không được sửa issue/amount/output. Điều này an toàn nhưng cần xác nhận có đủ sức cải thiện score như người dùng mong muốn.
2. Cải thiện CLI preflight: phải thử một chat completion JSON thực tế, không chỉ `GET /models`, trước khi xóa output cũ. Cân nhắc lưu snapshot output/trace cũ trước run; hiện `day09 run` xóa outputs/traces nếu không `--resume` sau model readiness.
3. Thiết lập backend local <=10B tương thích. K2 cần fork IFM; máy có Visual Studio 2022 nhưng không thấy CUDA toolkit trong PATH. Có thể tìm prebuilt K2 đáng tin cậy hoặc build fork; nếu không khả thi, dùng model Qwen <=10B trên runtime ổn định và đổi `LOCAL_MODEL_ID`, nhưng nói rõ tradeoff với user.
4. Test model server với JSON schema thật và fixture nhỏ trước batch. Model card K2 khuyên reasoning dài/high; cấu hình hiện `max_tokens` rất nhỏ (192–384) và `temperature=0.1`, có nguy cơ output bị cắt. Cần đo/chỉnh theo giới hạn 8K/VRAM.
5. Kiểm tra A2A trace, schema, budget, resilience và `day09 validate-inputs`; `pytest -q`, `ruff check .` lại. Thêm test HTTP worker nếu cần.
6. Chỉ khi model và preflight pass, freeze commit/tag version. Chạy `day09 run` đủ 100 case (không dùng `--allow-model-fallback` nếu mục tiêu là LLM), lưu stdout/stderr/metrics và snapshot đầu tiên, rồi `day09 validate`, sau đó `day09 package --output dist/submission.zip`. Không sửa case-by-case trước khi lưu snapshot lỗi đầu.

## Rủi ro đã biết

- `MCP concurrency=3` trong yêu cầu chưa triển khai thực chạy; các lời gọi đang tuần tự vì session streamable HTTP trước đây có lỗi khi parallel. README/ARCHITECTURE đã ghi hạn chế này.
- Chưa có model thật, nên các offline test dùng FakeWorker, chưa chứng minh K2 chạy trên máy.
- `day09 run` mặc định stop khi model offline, nhưng trong lúc chạy lỗi agent sẽ fallback deterministic từng agent. Cần quan sát `model_invocations` và fallback count để tránh nộp bản thực chất deterministic-only.
- Dữ liệu 100 case/credentials là local và gitignored. Không thêm vào commit/ZIP ngoài cấu trúc submission chuẩn.
