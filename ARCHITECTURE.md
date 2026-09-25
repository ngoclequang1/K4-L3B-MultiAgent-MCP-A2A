# L3B Architecture Record

Kiến trúc cho bài làm cá nhân. `solve_case()` hiện điều phối các vai trò trong
`business.py`, dựng output trong `verification.py` và kiểm tra trước khi trả về.
Vai trò là hàm/khối logic trong cùng process; handoff thể hiện bằng kết quả có cấu
trúc và sự kiện trace. Đây chưa phải giao thức A2A qua HTTP hay nhiều server.

Ràng buộc model: mọi model tích hợp phải có tổng số tham số công bố không quá
10 tỷ (với MoE tính tổng tham số, không chỉ active parameters). Model không rõ
quy mô không được chọn. Entity resolver hiện dùng logic Python, không gọi LLM.

Đã triển khai bước entity resolution trong `entity.py` và `entity_mcp.py`, cùng
`day09 resolve-entity --case-id L3B_CASE_001`. Lệnh này ghi kết quả/trace vào một
thư mục riêng dưới diagnostics, không tạo submission hoặc giả workflow hoàn chỉnh.
Adapter discovery kiểm tra get_order/get_customer_history và inputSchema trước
khi gọi. Customer history authoritative được dùng để loại candidate ngoài history
nếu không có warnings/dấu hiệu phân trang hoặc thiếu dữ liệu; candidate có trong
history được đối chiếu thêm order_id/customer_id với get_order. Những timestamp
mâu thuẫn không được giải quyết ở bước identity mà dành cho conflict agent.

Giới hạn hiện tại: hỗ trợ scope một order với customer_unique_id_hint làm anchor;
thiếu hint, nhiều candidate cùng hợp lệ hoặc ownership conflict thì giữ ambiguous.
Không tự mở rộng tìm kiếm ngoài candidate set. Confidence resolved/not_found=0.9
là heuristic ban đầu chưa calibration; không phải xác suất đã được đo. Cache và
ledger thuộc từng MCPEntitySource/case; khi tích hợp solve_case phải tái sử dụng
source trong cùng lần chạy để giữ evidence, không lấy refs từ diagnostic cũ.

Kiểm chứng: case 001 đã resolve bằng 2 call thực (history + order). Tests gồm
candidate sai, ambiguity, timeout, cache, cross-case ledger, payload thực tế và
history không đầy đủ. Test release_safety của starter yêu cầu không có case-set.json,
nên sẽ fail ở workspace đã tải input; không xóa input để vượt test đó.

Bước điều tra nghiệp vụ đã có trong `business.py` và hai lệnh diagnostic:
`day09 investigate-case --case-id L3B_CASE_001` hoặc `day09 investigate-all`.
Lệnh toàn bộ tạo một thư mục `diagnostics/business-all-...` mới, ghi report cho
từng case và `failures.json` cho case lỗi; mọi MCP call đều được server audit.
Report chẩn đoán chưa phải output submission. `solve_case()` đã map report sang
L3B schema và kiểm tra độc lập các invariant trong `verification.py`. Không dùng
model trong phần này.

Customer history có thể chứa nhiều purchase snapshot cùng order ID. Resolver
chọn snapshot có purchase_timestamp gần nhất nhưng không sau `opened_at`, rồi
lọc item, shipment, payment, refund theo thời điểm snapshot đến trước snapshot
kế tiếp. Khi get_order/shipment summary trả snapshot khác, conflict ghi rõ nguồn
history được chọn và lý do. Không cộng payment events từ nhiều purchase window.
Tiền được tính bằng Decimal. Refund timeline lỗi thì refunded_total_brl=null;
không coi là 0 hoặc đề xuất trả thêm tiền khi chưa rõ đã hoàn bao nhiêu.

Business report lưu customer, order/product, shipment, payment/refund, policy,
root cause, action, conflict, claim verdict và confidence sơ bộ. Confidence
0.85/0.55/0 là heuristic cần calibration trên feedback; chưa phải xác suất học
từ dữ liệu. Chưa có oracle/feedback private nên không khẳng định điểm semantic.

## 1. System overview

Coordinator tạo context riêng cho mỗi case, resolve entity trước khi giao việc cho specialist. Lập kế hoạch từ claims và investigation_scope: customer history, product context và independent verification phải được xử lý khi input yêu cầu. claimed_order_id, customer hint và claims[].topic đều là thông tin cần kiểm chứng, không phải đáp án.

```text
Input → Entity Resolver → Coordinator → Specialists → Conflict Resolver → Verifier → Output
            │                              │                  │             │
            └──────────────────────────── MCP ────────────────┴──────────── Trace
```

Entity được resolve trước; order/product xác định purchase snapshot bằng
customer history và `opened_at`, rồi shipment/payment chỉ nhận dữ liệu trong
window này. Hiện các lời gọi chạy tuần tự, không có vòng sửa tự động. Policy
được truy vấn sau nghiệp vụ. `business.py` ghi source conflict và selected_source;
`verification.py` kiểm tra output lần cuối. CLI emit case_received/case_finalized.

Entity ambiguous/not_found: chỉ query thêm để phân biệt candidate nếu còn budget, không cộng dữ liệu của các candidate vào một order. Nếu chưa đủ bằng chứng, dùng needs_investigation và các giá trị thiếu dữ liệu mà schema cho phép. Nếu không thể tạo output trung thực, đúng contract, báo lỗi rõ ràng. Đúng schema không bảo đảm vượt hard gate về evidence.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | Candidate IDs, customer hint | Xác minh identity, reject candidate, customer history | Order/customer capabilities | entity_resolution, customer_context, entity scope |
| Coordinator | Case và kết quả specialist | Dispatch, budget, tổng hợp draft | Không tự fetch evidence | assessment, affected_entities, draft |
| Order/product | Entity scope | Order status, item/seller/product mapping | Order/item/seller/product capabilities | OrderFacts |
| Shipment | Scope và OrderFacts | Timeline, seller vs logistics delay | Shipment capabilities | shipment_analysis, ShipmentFacts |
| Payment/refund | Scope và OrderFacts | Capture, split payment, duplicate, refund | Payment/refund capabilities | payment_analysis, PaymentFacts |
| Policy | policy_version và canonical facts | Điều kiện xử lý, tiền được hoàn | Policy capabilities | financial_resolution, resolution_actions |
| Conflict logic (`business.py`) | Các source theo purchase window | So sánh field, ghi nguồn được chọn/giữ unresolved | Dùng evidence đã fetch | data_conflicts, canonical facts |
| Output verifier (`verification.py`) | Report, output, ledger và trace | Schema, scope, linkage, totals, trách nhiệm/action | Không gọi MCP | verification_completed hoặc lỗi |

Áp dụng least privilege; tool discovery không đồng nghĩa mọi actor đều được gọi mọi tool.

Gateway dùng MCP discovery có metadata/inputSchema. Adapter xác nhận tool cần
thiết có trong discovery và validate arguments trước khi gọi. Hiện binding theo
những tool thật đã xác nhận của L3B, không phải registry tổng quát cho mọi gateway.

## 3. Entity resolution và A2A protocol

Hợp nhất claimed_order_id và candidate_order_ids, loại trùng. Lookup candidate rồi kiểm tra customer ownership, order context và evidence phân biệt. Phân loại confirmed/contradicted/unresolved. Chỉ reject khi evidence bác bỏ, không reject vì ID trông lạ hoặc request timeout. Chỉ resolved khi evidence xác định được scope và loại trừ ambiguity quan trọng; không chọn candidate đầu tiên.

related_order_ids thuộc customer history, không tự động đưa tất cả vào affected_entities hoặc tổng tiền khiếu nại. Ưu tiên rule có thể kiểm chứng; nếu dùng ranking, trọng số/threshold phải được cấu hình và kiểm thử sau khi biết tool payload. Không coi điểm tự đặt là xác suất đúng. Entity confidence và assessment confidence được đánh giá riêng, giảm khi thiếu bằng chứng hoặc conflict chưa giải quyết.

`MCPEntitySource` được tạo mới cho mỗi `solve_case`: giữ cache, request ledger,
records gồm case_id/tool/arguments/evidence_ref/result_hash/domain và call count.
Không có cache xuyên case hoặc xuyên run. TraceWriter giữ event trong RAM để
verifier nối từng submitted ref với `tool_result_consumed` và ghi JSONL xuống đĩa.

Envelope dưới đây là định hướng nếu sau này tách agent qua transport riêng; hiện
workflow gọi function trực tiếp và handoff qua report + trace:

```json
{
  "message_id": "local-message-001",
  "case_id": "L3B_CASE_001",
  "task_id": "resolve-entity",
  "parent_task_id": null,
  "sender": "coordinator",
  "recipient": "entity-agent",
  "kind": "task",
  "payload": {},
  "evidence_refs": [],
  "status": "pending"
}
```

kind: task/result/evidence_request/verification_report. status: pending/completed/insufficient_evidence/failed. Receiver kiểm tra case_id, task_id, sender và quyền; mỗi task chỉ nhận một kết quả, loại kết quả sau deadline. Specialist trả facts, claim links, missing fields và decision codes. Agent chỉ xin bổ sung qua coordinator, không gọi lẫn nhau tạo vòng lặp. Không trace prompt hoặc chain-of-thought.

## 4. Evidence và conflict lifecycle

Gateway validate MCP envelope theo JSON Schema. Adapter ledger ghi case_id,
tool_name, arguments, evidence_ref, result_hash và domain; ref/hash được giữ
nguyên. Envelope không chứa team_id/case_id/run_id nên client chỉ kiểm soát scope
theo request; MCP audit xác nhận provenance cuối cùng. Không tự tính lại hash khi
chưa có canonicalization chính thức.

Cache key gồm tool_name và arguments chuẩn hóa trong source riêng của case.
Workflow tuần tự nên không có in-flight de-duplication. Khi actor dùng response,
emit `tool_result_consumed`; report giữ union refs đã dùng, claim assessments có
refs liên quan. Output verifier yêu cầu mỗi ref nằm trong ledger đúng case và có
consumed event với đúng tool. Tối đa 30 refs; vượt ngưỡng thì dừng thay vì cắt.

Conflict so sánh cùng order và purchase window. Khi top-level order/shipment
summary trả snapshot khác, customer history có purchase timestamp gần nhất trước
opened_at được chọn, ghi `SNAPSHOT_SCOPE_SELECTED`. Không đủ căn cứ thì
selected_source=null, status=needs_investigation và confidence giảm. Timestamp
được parse timezone-aware; tiền dùng Decimal. `sources` là tool names; evidence
refs được lưu riêng.

Quy tắc nghiệp vụ:

- Shipment: chỉ quy seller_delay khi deadline/handoff hỗ trợ; giao trễ đơn thuần chưa đủ quy trách nhiệm seller.
- Payment: nhiều records có thể là split payment hợp lệ. Kiểm tra transaction identity và capture trước khi kết luận duplicate.
- Tiền: dùng Decimal/integer cents; phân biệt capture thực tế, refund hoàn tất và pending. Missing total là null, không tự đổi thành 0. Refundable phụ thuộc policy, không luôn bằng captured trừ refunded.
- recommended_refund_brl bằng tổng refund_lines, không vượt phần đủ điều kiện đã xác minh. Schema không cho null ở recommended_refund_brl: khi chưa thể đề xuất khoản tiền, 0 biểu thị chưa đề xuất chi thêm, kèm needs_investigation/action kiểm tra; không có nghĩa chắc chắn không được hoàn tiền.
- Claim thiếu evidence là insufficient_evidence, không tự coi unsupported. primary_issue dùng enum schema. Cause/reason/action codes cần bám policy hoặc quy ước công khai; chuỗi hợp schema chưa chắc đúng semantic scorer.
- Các analysis bắt buộc vẫn xuất hiện, dùng insufficient_evidence/null/array rỗng đúng vị trí schema khi thiếu dữ liệu.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| Entity tool timeout | Không retry tự động | Candidate unresolved, không reject | handoff / ENTITY_AMBIGUOUS |
| Entity ambiguous/not_found | Không mở rộng ngoài candidate set | needs_investigation | handoff / ENTITY_AMBIGUOUS hoặc ENTITY_NOT_FOUND |
| Refund tool RuntimeError | Không retry tự động | refunded_total_brl=null; không đề xuất trả thêm khi chưa rõ refund | handoff payment verdict và investigation_notes |
| Source conflict | Không query vòng lặp tự động | selected_source=null khi chưa giải quyết; confidence giảm | policy_decided và verification_completed |
| Auth/permission/tool thiếu hoặc envelope sai | Không retry mù | Workflow dừng với lỗi | Không emit case_finalized giả |
| Output/schema/provenance sai | Không tự sửa | VerificationError, không ghi output | verification_completed / OUTPUT_REJECTED |

Hiện CLI xử lý case và tool tuần tự. `get_sellers` chỉ gọi khi shipment nghi
seller delay để xác minh seller ID, tránh thêm call cho case khác.
`resolve_entity` đặt timeout 30 giây cho
mỗi candidate; Gateway HTTP timeout 300 giây. Adapter chặn từ call thứ 22 trong
một case; đây là cap an toàn, không phải private scoring budget. Thường 9 calls
cho case đã resolved khi đủ product/independent verification; refund tool lỗi vẫn
được tính call. Không có retry vì retry cũng được audit. Nếu cap hoặc lỗi nghiêm
trọng xảy ra, workflow dừng; `day09 run` hiện không resume giữa chừng.

Chỉ dùng event_type trong schema: case_received, task_assigned, tool_result_consumed, handoff, policy_decided, verification_completed, case_finalized. CLI đã emit received/finalized, solve_case không emit lặp. task_assigned trước dispatch, handoff khi chuyển kết quả thật, verification_completed sau kiểm tra với attributes.passed đúng thực tế. Lỗi ghi decision_code/attributes của event phù hợp, không tạo event_type mới. attributes chỉ nhận scalar; tối đa 20 refs/event, chia batch nếu cần. Output tối đa 30 refs.

## 6. Verification invariants

`business.py` kiểm tra facts khi tiêu thụ raw MCP data. `verification.py` là lớp
kiểm tra độc lập trên report, output, ledger và trace; nó không đọc lại toàn bộ
raw payload và không gọi MCP. Không có vòng tự sửa. Một event
`verification_completed` cho business checks và một event cho output checks;
`attributes.passed` phản ánh kết quả mỗi lớp. Output bị reject thì raise lỗi.

- Đúng toàn bộ output schema, schema_version và case_id; không thêm field nội bộ.
- Resolved/rejected không giao nhau; affected IDs có căn cứ, không lẫn related order ngoài khiếu nại.
- Mọi output ref tồn tại trong ledger case hiện tại và có consumed trace đúng tool.
- Claim verdict gắn evidence, không chép topic input làm đáp án.
- Timeline, late_seller_ids và responsible parties khớp nhau trong các case đủ dữ liệu.
- Capture/refund không double-count; refund_lines, tổng đề xuất và policy nhất quán.
- Source selection có căn cứ; unresolved được phản ánh trong assessment/confidence.
- Primary issue, status, root cause và actions nhất quán, không duplicate IDs/actions.
- Confidence trong [0,1], số phần tử không vượt schema; trace đúng lifecycle và có phối hợp thật.

`day09 validate` vẫn chủ yếu kiểm tra schema/artifact. Verifier nội bộ bổ sung
scope/provenance/linkage/consistency, nhưng không thể chứng minh semantic score
hay thay thế server audit/private oracle.

## 7. Reproducibility

Module thực tế: `entity.py` (rule resolver), `entity_mcp.py` (discovery, cache,
ledger), `business.py` (specialists/conflict/policy), `verification.py` (output
builder/verifier), `workflow.py` (orchestrator), `trace.py` (JSONL + event memory),
`cli.py` (run/diagnostic). Không có model, prompt, random seed hoặc LLM client.
Ràng buộc <=10 tỷ tham số vẫn áp dụng nếu bổ sung model sau này.

Môi trường đã kiểm tra: Windows, Python 3.14.2; mcp 2.2.0, httpx2 2.13.1,
jsonschema 4.26.0, python-dotenv 1.2.3, pytest 8.4.2, ruff 0.16.9. Các
dependency trực tiếp được pin trong pyproject.toml; transitive dependencies chưa
có lockfile. Repo yêu cầu Python >=3.11. Không ghi API key vào tài liệu/trace.

Test fixture/mock gateway kiểm tra resolver, split payment, refund pending,
snapshot scope, seller/logistics delay, provenance, schema và verifier reject.
Fixture refs không được dùng để nộp; output thật lấy refs từ MCP audit hiện tại.

```powershell
python -m pytest -q
python -m ruff check .
day09 validate-inputs
day09 mcp-tools
day09 check-case --case-id L3B_CASE_002
day09 run
day09 validate
day09 package --output dist/submission.zip
```

`day09 check-case` chỉ ghi dưới diagnostics để kiểm tra một case và không xóa
outputs. `day09 run` xóa output/trace cũ trước khi chạy 100 case; lưu riêng
artifact cần giữ trước khi rerun. Mỗi lần chạy tạo evidence refs mới, không tái
sử dụng refs từ diagnostic/run trước. Chưa chạy trọn 100 case trong phiên này vì
mọi call đều được audit và có thể ảnh hưởng efficiency.
