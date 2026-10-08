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
