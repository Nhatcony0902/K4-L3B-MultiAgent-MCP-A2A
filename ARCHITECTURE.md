# L3B Architecture Record

Quyết định có thể kiểm chứng; không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

```text
Input ─► coordinator ─► entity-agent ──(handoff)──► coordinator
                              │ get_customer_history
                              ▼
          ┌──────── order-agent     get_order, get_order_items
          ├──────── shipment-agent  get_shipment_summary          (song song, asyncio.gather)
          ├──────── payment-agent   get_payment_timeline, get_refund_timeline
          └──────── policy-agent    get_policy
                              │ handoff
                              ▼
                     conflict-resolver ─► policy-agent (rules.classify + policy table)
                              │
                              ▼
                 llm-verifier (optional, ≤8B) ─► verifier (invariants) ─► coordinator ─► Output
MCP evidence ──► tool_result_consumed (trace, evidence_ref) ──► evidence_refs (output)
```

- Quyết định nghiệp vụ: **deterministic rules** (`src/student_agent/rules.py`), không I/O, có unit test.
- LLM: `meta-llama/llama-3.1-8b-instruct` (8B, < 10B) qua OpenRouter; chỉ là second opinion, không ghi field output.
- Orchestration + trace: `src/student_agent/workflow.py`.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | case input (claimed id, candidates, hint) | xác minh sở hữu order qua lịch sử khách, reject candidate | `get_customer_history`; `get_order` chỉ khi history không xác nhận | order_id, rejected, customer_unique_id → coordinator |
| Coordinator | case | giao task, nhận handoff, finalize | không gọi tool | `task_assigned`, `case_received/finalized` |
| Order/product | order_id | order row + item/seller rows | `get_order`, `get_order_items` | → conflict-resolver |
| Shipment | order_id | timeline + event giao hàng | `get_shipment_summary` | → conflict-resolver |
| Payment/refund | order_id | capture, mismatch, refund events | `get_payment_timeline`, `get_refund_timeline` | → conflict-resolver |
| Policy | policy_version, primary issue | tra bảng policy: status, action, refund, party | `get_policy` | `policy_decided` → verifier |
| Conflict resolver | mọi specialist result | chọn bản ghi hiệu lực, scope event theo period | không gọi tool | `data_conflicts`, facts → policy-agent |
| LLM verifier | facts rút gọn (không PII ngoài id) | gán nhãn độc lập | không gọi MCP | agree/disagree → verifier |
| Verifier | draft output | kiểm invariants, hạ confidence nếu fail | không gọi tool | `verification_completed` → coordinator |

Least privilege: mỗi actor chỉ gọi tool của domain mình; `get_order_payments`, `get_sellers`,
`get_product_context` không dùng vì trùng thông tin (payment timeline ⊇ payments; seller id có trong items).

## 3. Entity resolution và A2A protocol

- Candidate hợp lệ = candidate xuất hiện trong `get_customer_history(customer_unique_id_hint)`.
  Ưu tiên `claimed_order_id` nếu thuộc khách; còn lại vào `rejected_candidates`.
- Không gọi `get_order` cho candidate giả (tiết kiệm call); chỉ probe tối đa 2 candidate khi history thất bại.
- Confidence: 0.95 khi history xác nhận; 0.6 khi chỉ `get_order` xác nhận; `not_found` → output `insufficient_evidence`.
- Envelope A2A = trace event (`task_assigned`/`handoff` với `actor`, `target`, `decision_code`), correlation theo `case_id`.
  Luồng tuyến tính, không vòng lặp; mỗi specialist chạy đúng 1 lần/case.

## 4. Evidence và conflict lifecycle

- Mọi response đi qua `Contracts.validate_evidence`; `evidence_ref` lưu nguyên văn trong `CaseScope.refs`
  (không sinh/sửa ref), mỗi lần consume emit `tool_result_consumed` kèm ref → output `evidence_refs`.
- `CaseScope` tạo mới cho từng case ⇒ cache và ref không bao giờ dùng chéo case.
- **Conflict chính**: một order_id có nhiều bản ghi ở các thời điểm khác nhau (history/get_order/shipment
  lệch nhau). Rule: bản ghi hiệu lực = bản có `order_purchase_timestamp` muộn nhất ≤ `opened_at`
  (không có thì bản sớm nhất). Trước đó ưu tiên bản ghi mà evidence **ủng hộ claim**
  (`SELECT_RECORD_SUPPORTING_CLAIM`); claim không được evidence nào ủng hộ ⇒ dùng rule thời gian và
  primary issue theo precedence. Bản ghi trùng timestamp được gộp; dòng item/event trùng hệt được dedupe
  (`DEDUPLICATE_IDENTICAL_RECORDS`). Event/item gán theo period window; **refund gán theo số tiền khớp
  capture** của bản ghi (không theo ngày). Ghi vào `data_conflicts` (`selected_source=get_customer_history`).
- Primary issue theo thứ tự ưu tiên: canceled+paid → unavailable+paid → refund failed → refund pending →
  reconciliation mismatch mở → ≥2 capture (tổng = giá+ship ⇒ split hợp lệ; trùng số tiền ⇒ duplicate) →
  giao trễ (event actor / carrier > shipping_limit ⇒ seller, ngược lại logistics) → unsupported_claim.
- Status/action/refund/party lấy từ policy; party seller dùng seller_id của chính case.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout / transport | 1 | coi như không có evidence, không bịa dữ liệu | `tool_result_consumed` / `NO_EVIDENCE` |
| MCP tool error (không có dòng) | 0 | domain rỗng (vd. không có refund) | `tool_result_consumed` / `NO_EVIDENCE` |
| Entity not found/ambiguous | ≤2 probe `get_order` | `insufficient_evidence`, `needs_investigation` | `handoff` / `ENTITY_NOT_FOUND` |
| Source conflict | 0 | chọn bản ghi hiệu lực theo `opened_at` | `policy_decided` / `SELECT_RECORD_EFFECTIVE_AT_CASE_OPEN` |
| Invalid specialist result | 0 | verifier hạ confidence ≤ 0.5 | `verification_completed` / `FAIL_<INVARIANT>` |
| LLM lỗi/không có key | 1 | bỏ qua, giữ kết quả rule | `verification_completed` / `LLM_UNAVAILABLE` |

Budget: 7 MCP call/case (1 history + 6 specialist song song), cache theo (tool, args) trong case.
Mọi evidence của một submission phải thuộc **một** MCP session (run). Session rớt ⇒ xóa toàn bộ
output/trace và chạy lại từ đầu (≤3 lần); không bao giờ ghép kết quả từ nhiều session
(bài học: submission ghép 3 session bị 0 điểm).

## 6. Verification invariants

Schema (runner validate), entity scope (resolved ∉ rejected), evidence chỉ từ `CaseScope` của case,
tổng `refund_lines` = `recommended_refund_brl`, `no_action` ⇒ refund 0, refund > 0 ⇒ `action_required`,
party seller ∈ `affected_entities.seller_ids`, `late_seller_ids` ⊆ seller_ids, action không trùng,
`evidence_refs` không rỗng, confidence ∈ [0,1] (0.9 khớp claim, 0.7 lệch claim, −0.15 nếu LLM bất đồng).

## 7. Reproducibility

- Python 3.11, dependency pin trong `pyproject.toml`; rules deterministic, không random seed.
- LLM: `temperature=0`, `max_tokens=12`, timeout 30s; cấu hình qua `LLM_API_KEY`, `LLM_BASE_URL`,
  `LLM_MODEL` trong `.env` (không commit). Tổng tham số model ≤ 8B.
- Case chạy tuần tự trong một session; trong case tối đa 6 MCP call đồng thời. Trace timestamp tăng chặt.
- Lệnh: `day09 run && day09 validate && day09 package --output dist/submission.zip`.
