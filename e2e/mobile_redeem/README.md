# Redeem flow E2E (task 3.15)

Chạy từ gốc superproject trên Windows, chỉ với Supabase local và Android emulator hoặc điện thoại USB đã bật ADB. Bản APK test phải có cùng chữ ký với app đang cài; nếu ADB báo `INSTALL_FAILED_UPDATE_INCOMPATIBLE`, dừng và dùng thiết bị khác hoặc chỉ gỡ app cũ sau khi chủ máy chấp nhận mất dữ liệu app:

```powershell
cd backend
..\node_modules\.bin\supabase.cmd start
..\node_modules\.bin\supabase.cmd db reset
cd ..
backend\.venv\Scripts\python.exe -X utf8 tests\e2e\mobile_redeem\run_e2e.py --preflight
backend\.venv\Scripts\python.exe -X utf8 tests\e2e\mobile_redeem\run_e2e.py --phase direct_pending
backend\.venv\Scripts\python.exe -X utf8 tests\e2e\mobile_redeem\run_e2e.py --device <serial> --phase qr_invalid
```

Không chạy DB deploy. Runner xác minh API và DB đều loopback trước khi tạo fixture, yêu cầu một thiết bị Android online, dùng `api_role` cho backend và session mobile local. Emulator dùng `10.0.2.2`; điện thoại USB dùng `adb reverse` tới `127.0.0.1` và gỡ mapping sau phase. Với phase nhập mã, runner sao lưu file FlutterSecureStorage sẵn có **trong vùng private của app trên điện thoại**, rồi khôi phục trong `finally`; không đọc nội dung phiên ra host. Nếu khôi phục lỗi, runner báo fail và giữ backup để xử lý. Fixture phiên/mã nằm trong file tạm trên host và app private storage; stdout chỉ nêu tên phase và kết quả, không in token/mã/body. `finally` dừng relay/backend, xóa file fixture và dispose DB; cleanup lỗi làm exit khác 0. Vì `access_logs` append-only và liên kết có lịch sử, dữ liệu fixture trong DB local được xóa bằng `supabase db reset` trước lần chạy kế tiếp. Exit 0 = pass, 1 = fail, 2 = thiếu điều kiện. Chạy riêng `--phase` nhiều lần để chọn kịch bản; không chọn thì chạy tất cả.

| Phase | Nguồn UI | Điều cần kiểm |
|---|---|---|
| `direct_pending` | `main()` thật, nhập mã | POST 201, một pending, sharing=false; schedules 403 và denied log |
| `double_tap` | `main()` thật | Chỉ một POST khi chạm hai lần lúc chờ |
| `response_lost` | `main()` thật | Relay bỏ response đầu sau commit; retry cùng UUID 200, một DB row |
| `unauthorized` | `main()` thật | Relay thay bearer chỉ ở POST redeem bằng token sai, 401 và không có row; app khởi động bằng phiên hợp lệ |
| `qr_valid` | Scanner callback của màn production | Payload từ API, không POST trước xác nhận, sau đó một pending; lỗi F1 dự kiến làm đỏ |
| `qr_invalid` | Scanner callback của màn production | Báo QR sai, không POST |

Relay chỉ chuyển tiếp HTTP tới backend local và giữ request/response trong bộ nhớ để đối chiếu; nó không sinh response redeem. QR callback không chứng minh camera hoặc quyền OS. Ca camera thật ở AC11 phải chạy tay trên thiết bị có camera; ghi thiết bị, phiên bản Android, bước và kết quả vào Test Report. Khi chưa chạy, trạng thái là Pending.

## Ma trận bao phủ

| AC task | Bằng chứng tự động được thiết kế | Phần còn cần người/thiết bị |
|---|---|---|
| AC1 | `direct_pending`; `test_creates_pending_without_profile__AC1` | Android E2E nếu chưa cài được APK test |
| AC2 | `qr_valid`, `qr_invalid`; widget `redeem_acceptance_test.dart` | Camera thật AC11 |
| AC3 | `direct_pending`; `test_pending_link_grants_no_access__AC2`; PEP schedules | Android E2E nếu chưa cài được APK test |
| AC4 | `response_lost`, `double_tap`; `test_replay_200_current_status__AC29`, concurrency `__AC31` | Android E2E nếu chưa cài được APK test |
| AC5 | `unauthorized`; redeem API `__AC3`, `__AC4`, `__AC5`, `__AC16`, `__AC20` | Android UI lỗi nếu chưa cài được APK test |
| AC6 | redeem API `__AC6`, replay `__AC29`; mobile controller tests | Android UI nếu chưa cài được APK test |
| AC7 | `test_owner_regrant_via_api_reuses_code_and_redeems__AC7`; consent/redeem API `__AC33`, `__AC34` | Không |
| AC8 | `test_daily_quota_last_second_and_midnight_vn__AC8`; redeem API `__AC8b`, `__AC9`, `__AC10`, `__AC14` | Android UI countdown nếu chưa cài được APK test |
| AC9 | `test_patient_schedules_access.py`, `test_family_links_access.py`, `test_adherence_history_access.py` | Không |
| AC10 | `test_adherence_history_access.py` fail-closed/rollback; `test_run_e2e.py` cleanup/preflight | Runner E2E nếu chưa cài được APK test |
| AC11 | Không có bằng chứng camera tự động | Manual camera/quyền OS |
| AC12 | `test_run_e2e.py`, `run_e2e.py --preflight`; `testcases.json`/workbook | E2E môi trường đầy đủ nếu chưa cài được APK test |

Các test backend dùng `TestClient` + PostgreSQL thật. Chúng không chứng minh HTTP xuyên thiết bị Android. Chạy `cd backend; .venv\Scripts\python.exe -m pytest tests/integration` và kiểm số pass/fail/skip trong evidence của task. Test tích hợp hiện có trong repo dùng fixture riêng, không nên thay một ca fail bằng kết quả của ca khác.
