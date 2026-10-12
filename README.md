# DrugTime E2E & Integration Tests

Sprint 1+ test suites.

## E2E đăng nhập Mobile App (`e2e/mobile_login/`)

App Flutter thật trên Android emulator gọi Backend API thật, nối Supabase **local**. Test UI nằm ở
`frontend/mobile/integration_test/login_e2e/`; script ở đây điều phối và đối chiếu DB.

Điều kiện:

- Docker đang chạy và `supabase start` (từ `backend/`); DB local đã áp mọi migration
  (`supabase migration up --local` hoặc `supabase db reset`).
- Một Android emulator đang chạy (`adb devices` có `emulator-xxxx  device`), `flutter` và `adb` trong PATH.
- Môi trường Python của backend (`backend/.venv`) có `sqlalchemy`, `psycopg`, `uvicorn`.

Chạy từ gốc superproject:

```bash
backend/.venv/Scripts/python.exe tests/e2e/mobile_login/run_e2e.py          # cả 3 pha
backend/.venv/Scripts/python.exe tests/e2e/mobile_login/run_e2e.py --phase 2  # một pha
```

Script tự khởi động backend ở một cổng trống trên `127.0.0.1` (emulator gọi qua `10.0.2.2`), ghi đè
mọi biến môi trường của backend để không dùng `backend/.env`, chuẩn bị dữ liệu số thử `test_otp`
(`0900000001` / `123456`), chạy ba pha `flutter test`, in các request backend nhận trong từng pha,
đối chiếu DB, rồi luôn tắt backend. Output đã che key, token và mật khẩu DB.

Mã thoát: `0` mọi pha pass · `1` có pha hoặc phép đối chiếu fail · `2` thiếu điều kiện môi trường
(không có emulator, Supabase chưa chạy hoặc không phải localhost).

Giới hạn của Supabase local: mỗi số chỉ gửi OTP được 5 giây một lần, và tối đa 30 lần xác thực OTP
trong 5 phút; chạy liên tục nhiều lần có thể nhận 429 — chờ vài phút rồi chạy lại.

Unit test của script: `cd tests && ../backend/.venv/Scripts/python.exe -m pytest -q`.

## E2E đồng bộ danh mục (`e2e/catalog_sync/`, task 2.22)

Suite chạy backend hiện có qua Celery Beat → RabbitMQ → Worker prefork → DAV HTTP giả →
PostgreSQL thật. Không gọi DAV thật, không nhận DB/broker URL từ người chạy, không sửa backend.
Windows native dừng với exit 2. Cần Linux/WSL2, Python ≥3.12, Docker Engine local và Compose v2.
Python Linux phải có dependency backend cùng `python-dotenv`, `pytest`, `httpx`; dùng venv Linux
riêng, không dùng `backend/.venv/Scripts/python.exe` của Windows.

Từ gốc superproject, sau khi môi trường Linux đã có đủ dependency:

```bash
python tests/e2e/catalog_sync/run_e2e.py --preflight
python tests/e2e/catalog_sync/run_e2e.py
python tests/e2e/catalog_sync/run_e2e.py --case AC7
python -m pytest tests -q -p no:cacheprovider
cd backend
python -m pytest tests/unit/test_catalog_scheduling.py -q -p no:cacheprovider
```

Gỡ `DOCKER_HOST`/`DOCKER_CONTEXT` trước chạy; Docker context phải dùng Unix socket local.
Suite tạo project ngẫu nhiên với PostgreSQL `16.6-bookworm`, RabbitMQ `3.13.7-management`,
volume/network riêng, port động chỉ ở 127.0.0.1; tải image lần đầu cần mạng. Suite áp **mọi**
migration hiện có nguyên văn lên DB riêng, dùng worker_role và vhost riêng từng case.
Các role Supabase nền `anon`/`authenticated`/`service_role` được tạo NOLOGIN để áp dump ban đầu;
không sửa grant sản phẩm. Thiếu dependency, lỗi image/migration/service bootstrap là thiếu điều kiện.
Không cần `supabase db reset` trên DB phát triển.

Mã thoát: **0** mọi case được chọn thực thi và pass; **1** assertion, timeout, harness hoặc cleanup
lỗi; **2** thiếu môi trường/ownership/bootstrap. `--preflight` luôn exit 2 vì chưa chạy E2E,
kể cả khi in ready. `--case` là kết quả một phần, không đại diện toàn suite. Collect-only, zero test
hoặc skip không được tính pass. Chạy runner tường minh để tránh E2E bị auto-collect với unit test.

Deadline: infra 180s, Worker ready 30s, tick 20s, case 60s (sau migration/bootstrap), toàn suite
1200s; HTTP hold tối đa 20s, SQL/HTTP/broker call tối đa 5s, cleanup tối đa 30s. Không đổi retry
sản phẩm. Proxy giữ ack theo task ID đã arm trước publish; kiểm DB committed + broker unacked +
cùng task ID redelivered. Không gửi task mới hoặc sửa run treo để thay cho redelivery.

Evidence nằm `work/2.22-sync-job-e2e/evidence/`: log Worker/Beat đã che credential,
ledger AMQP allowlist, DB fixture và kết quả từng case trước teardown. `test-catalog-result.json`
ghi số case executed/fail/skip/partial; chỉ log collect/preflight không chứng minh AC đạt.
Runtime manifest ở `work/2.22-sync-job-e2e/scratch/<run-id>/`, không commit scratch.
Nếu DrvFS không giữ quyền Linux, đường dẫn runtime này trỏ tới thư mục 0700 riêng trong `/tmp`;
suite kiểm mode thư mục/env file trước khi chạy. Process con dùng umask 077.
Cleanup chỉ kill group do suite sinh, xóa vhost/project/resource đã xác minh ownership; lỗi cleanup
nonzero và giữ manifest ID để xử lý. Có test sentinel vhost để kiểm tài nguyên ngoài case còn nguyên.

Giới hạn: snapshot 10 thuốc kiểm normalize/withdrawn/counters/ID ổn định, không chứng minh phân
trang toàn DAV. AC7 có thể phát hiện run dang_chay treo sau crash; giữ failure để lập Bug Report,
không sửa production trong task này. Heartbeat và các bước tick ngoài catalog giữ log thật;
catalog pass không tự chứng minh toàn Worker khỏe. Mốc cadence chính xác 7 ngày có unit policy
read-only; E2E dùng timestamp DB thật và kiểm cửa sổ not-due chưa vượt ngưỡng.
