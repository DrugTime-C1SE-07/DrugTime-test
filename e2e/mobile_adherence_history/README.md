# PB21 — lịch sử tuân thủ người thân

Chạy từ gốc superproject bằng Python của backend. Runner dùng Android package riêng
`vn.drugtime.adherence_e2e` trên cả emulator và điện thoại, API và Supabase cục bộ.
Không dùng app đang cài của người dùng hoặc backend triển khai.

```powershell
backend/.venv/Scripts/python.exe -m pytest tests/e2e/mobile_adherence_history/test_run_e2e.py -q -p no:cacheprovider
backend/.venv/Scripts/python.exe -X utf8 tests/e2e/mobile_adherence_history/run_e2e.py --preflight
backend/.venv/Scripts/python.exe -X utf8 tests/e2e/mobile_adherence_history/run_e2e.py --device emulator-5554
# Hoặc một kịch bản:
backend/.venv/Scripts/python.exe -X utf8 tests/e2e/mobile_adherence_history/run_e2e.py --scenario revoke
```

Trước khi chạy thật: `supabase start` và `supabase db reset` từ `backend`; đặt mật khẩu
`api_role` local theo fixture integration hiện có (mặc định `postgres`). Runner kiểm
kết nối bằng role này, không đổi mật khẩu. Cần Flutter/ADB trong PATH, Android online,
SDK Android/JDK và dependency Flutter đã cache (`flutter pub get --offline`).
`TEST_ADMIN_DATABASE_URL` phải khớp host/port/database Supabase local;
`TEST_API_ROLE_PASSWORD` chỉ đọc từ môi trường, không đưa vào lệnh hoặc evidence.

Runner từ chối khi package E2E đã tồn tại để tránh ghi đè dữ liệu lượt trước.
Kiểm tra và xử lý package cũ trước khi chạy lại. Preflight chỉ đọc; thiếu thiết bị
trả **exit 2 / PENDING**, không seed DB, build hoặc cài app. Exit 1 là lỗi kiểm tra;
exit 0 khi mọi kịch bản được chọn đã đạt các assertion.

| Kịch bản | Kiểm tra app/API | Kiểm tra DB | AC |
|---|---|---|---|
| `linked` | Mở production named route, thấy thuốc fixture, chỉ GET; 200/no-store | Audit `allowed`, scope `lich_su_tuan_thu` | AC1, AC6 |
| `revoke` | Sau `history_loaded`, host thu hồi `family_sharing` và commit; refresh cùng bearer → 403, xóa lịch sử | Audit allowed → denied cùng patient/caller | AC4, AC5, AC6 |
| `cross_patient` | Đóng route P, mở route Q không liên kết → 403, không còn text P | Allowed P → denied Q | AC4, AC6 |

Fixture dùng `Factory` của bộ integration hiện có; pending link được xác nhận trước
khi active. Liều giả lúc 00:00 hôm nay theo `Asia/Ho_Chi_Minh` đã được xác nhận đúng giờ,
không dựa vào Worker hoặc chờ cửa sổ 120 phút. Mỗi kịch bản tạo người dùng mới.
API chạy bằng `api_role`; kiểm audit từ kết nối DB mới sau response commit.

Bearer chỉ đi qua stdin vào file private của package E2E, không `adb push` qua vùng
dùng chung, không đưa vào asset/dart-define hoặc ghi output. Relay chỉ giữ status,
patient, cờ no-store và kết quả so HMAC của bearer, không giữ payload sức khỏe.
Thu hồi consent và command đồng bộ bằng phase file; thời gian polling chỉ giới hạn
chờ protocol, không dùng làm oracle về quyền.

Cleanup luôn thử dừng package, xóa fixture/phase/command, gỡ package E2E và bỏ riêng
reverse mapping đã tạo; lỗi cleanup làm lượt chạy thất bại. Link fixture bị tắt chia
sẻ. Người dùng giả và audit được giữ trên DB local vì access_logs chỉ ghi thêm;
`supabase db reset` là cách chuẩn làm sạch, không DELETE/UPDATE audit để dọn test.
Build sources nằm trong thư mục tạm, không sửa frontend. Không copy signing key
hoặc google-services.json sang package E2E.

Giới hạn: auth/medication/consent bootstrap phía Flutter dùng in-memory collaborators;
history dùng production route/controller/repository và HTTP/API/PEP/DB thật. Runner
**chưa chứng minh** thao tác chọn patient từ tab chính, đăng nhập thật, vòng đời OS,
offline native, logout trên thiết bị hoặc không lưu bền dữ liệu. Các ca đó vẫn Pending
đến khi có thao tác và bằng chứng riêng. Pass host-runner unit không phải Pass E2E.
