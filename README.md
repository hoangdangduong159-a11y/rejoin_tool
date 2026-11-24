# Rejoin Tool v4.0 Ultra (Termux + Root + Cloud Phone)

Hệ thống tự động **Auto-Rejoin, Multi-Instance & Auto-Change Account** cho Roblox trên Android / Cloud Phone (UgPhone, Redfinger, VPhone, VMOS...) chạy trực tiếp trên **Termux + Root**, viết 100% bằng Python chuẩn (không mã hóa, không phụ thuộc server key ngoài).

---

## Tính năng nổi bật (v4.0 Ultra)

| Nhóm tính năng | Chi tiết triển khai |
|---|---|
| **Hybrid Monitor Engine (`engine.py` + `log_watch.py`)** | Kết hợp **3 tầng giám sát**: Lua Heartbeat (`fps`, `ping`, `playersCount`, `placeVersion`, `stats`), **Native Roblox Client Log Tail** (đọc log lỗi `264`, `267`, `268`, `273`, `277`, `279`, `524`, `529` mà không phụ thuộc Executor), và **Presence API fallback** (chống kill nhầm clone thường / VNG). |
| **Chống treo ANR & Sửa lỗi thông minh** | Watchdog quét `dumpsys window windows` phát hiện app treo **ANR / Application Error**, tự đóng dialog hệ thống và restart sạch. Tự xóa cache khi gặp lỗi `268`, cooldown/rotate khi gặp `264`, và tùy chọn **Server-Hop public server** tránh trùng JobId giữa các clone. |
| **Web Control Panel (`web_panel.py`)** | Giao diện Web Responsive Dark Glass trên cổng `8787` (`127.0.0.1:8787`), hỗ trợ Bearer Token auth, theo dõi CPU/RAM/Disk, trạng thái từng clone, **1-click Rejoin / Rotate / Stop / Sort Tabs / Clean RAM / Cloud Tune**, dán cookie, đổi link hàng loạt, và chỉnh `config-change.json` trực tiếp trên trình duyệt. |
| **Hot-Folder `/storage/emulated/0/Download` (`shared_hub.py`)** | Thả cookie vào `/storage/emulated/0/Download/cookie.txt` để tool tự nuốt vào hàng đợi và đánh thức clone đang `WAITING_COOKIE`; tự đồng bộ `done.txt`, `dead.txt`, `switched/*.txt`, `config-change.json` ra `/storage/emulated/0/Download/RejoinTool/` để quản lý dễ bằng MT Manager. Hỗ trợ `rejoin hub import-shouko` di chuyển cấu hình/script từ thư mục `Download/Shouko` cũ. |
| **Nhận diện Clone & SameHWID (`rjcore.py`)** | Hỗ trợ `com.roblox*`, `free.noka*`, `com.delta*`, `vip.*`, AppCloner SameHWID (`cloneSettings.json` badge/cloneNumber), và Roblox VNG (`robloxapp.vnggames.com_appStorage.json`). Cache sẵn Chromium v14 template DB để bơm cookie cho clone mới nhanh hơn. |
| **Kiểm tra & Lọc Cookie Song Song** | `rejoin cookies validate --clean` kiểm tra đồng thời nhiều luồng qua Roblox API (`ok`, `expired`, `banned`, `deleted`, `warning`, `captcha`, `faceid`) và phân loại vào `switched/*.txt`. |
| **Tối ưu Cloud Phone (`sysutils.py`)** | `rejoin optimize --cloud-tune` tắt hiệu ứng chuyển cảnh Android (`animation_scale = 0`), whitelist Termux + các clone khỏi Doze mode, giảm FPS & Low-Graphics (`GlobalBasicSettings_13.xml` + `ClientAppSettings.json`), sắp xếp cửa sổ Freeform Grid/Tiny. |
| **Custom Change Account (`change_acc_cus` & `change_acc_bf`)** | Tương thích chuẩn `<Username>.txt` / `Completed-<Username>.txt` (Shouko/Yummy/Kaitun) và `config-change.json` cho Blox Fruits (hỗ trợ cả `race_ver` dạng số `1..4` lẫn tên tộc như `"Cyborg V4"`). |

---

## Cài đặt nhanh (1 lệnh trong Termux thường, không dùng `su`)

```bash
. <(curl -sL https://raw.githubusercontent.com/hoangdangduong159-a11y/rejoin_tool/main/setup.sh)
```

Hoặc kèm sẵn PlaceID / VIP link:

```bash
RJ_TARGET=2753915549 SKIP_UPGRADE=1 . <(curl -sL https://raw.githubusercontent.com/hoangdangduong159-a11y/rejoin_tool/main/setup.sh)
```

---

## Các lệnh vận hành chính

```bash
# Menu tương tác (chạy bằng root)
su -c "export PATH=\$PATH:/data/data/com.termux/files/usr/bin && export TERM=xterm-256color && export TMPDIR=/data/data/com.termux/files/usr/tmp && export LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib && /data/data/com.termux/files/home/rejoin_tool/rejoin"

# Chạy Auto-Rejoin kèm Bảng Điều Khiển Web (:8787)
rejoin run --dashboard --web

# Chế độ treo ngầm 24/7 + Bảng Web
rejoin daemon start --web
rejoin daemon attach
rejoin daemon stop

# Khởi động Web Control Panel độc lập
rejoin web start --host 127.0.0.1 --port 8787

# Quản lý Hot-Folder & Import cấu hình từ Shouko/Download
rejoin hub status
rejoin hub sync
rejoin hub import-shouko

# Kiểm tra & lọc sạch cookie chết song song
rejoin cookies validate --clean

# Xem log native của Roblox trên các clone
rejoin logs

# Tối ưu Cloud Phone (tắt animation + whitelist Doze + FPS 15)
rejoin optimize --cloud-tune --fps 15
```

Xem thêm:
- [Hướng dẫn Setup nhanh](HUONG_DAN_SETUP_NHANH.md)
- [Hướng dẫn tự viết script Custom Change Account](HUONG_DAN_CUSTOM_CHANGE_ACCOUNT.md)
