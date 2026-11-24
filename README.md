# Roblox Multi-Instance Auto-Rejoin v3.0 Hybrid (`rejoin_tool` + Shouko Architecture)

Công cụ hợp nhất toàn bộ thế mạnh của **Rejoin v2** (Daemon/IPC attach, đa Profile, Rule DSL, Lua API `RejoinTool`) và **QH Rejoin / Shouko** (batch `/proc` monitoring, `WAITING_COOKIE` tự phục hồi khi nạp thêm cookie, Auto Cross-Block tránh trùng server, `focus_before_kill` + dọn task stack, `shared/Autoexec` & `shared/Workspace` sync, Quick Setup 1 bước, GoFile APK downloader, Benchmark).

---

## 1. Tính năng nâng cấp v2.0

1. **Chế độ Daemon + Local IPC (`ipc.py`)**:
   - Chạy nền ổn định và attach dashboard trực tiếp bằng Unix Socket (`~/.rejoin_ipc.sock`) mà không làm tắt tiến trình khi `Ctrl+C`.
   - Hỗ trợ hot-reload cấu hình đang chạy qua `rejoin daemon reload` hoặc ngay khi nhấn `S` trong menu cấu hình.
2. **Sắp xếp cửa sổ Roblox tự động (`tabs`)**:
   - `rejoin tabs sort` / `rejoin tabs tiny`: chia lưới cửa sổ toàn màn hình hoặc chế độ **Tiny** (ô nhỏ tối ưu GPU) thông qua `wm size` + `am task resize`.
   - `rejoin tabs open-all` & `rejoin tabs logout`.
3. **2 chế độ kiểm tra (`executor` & `online`)**:
   - `executor`: đọc heartbeat `hb_<UserId>.json` từ Autoexecute (hỗ trợ `TeleportInitFailed`, `setfpscap`, `setGameInfo`).
   - `online`: kiểm tra Presence API (kèm fallback `presence.roproxy.com`) với cơ chế đếm **3 strike** trước khi rejoin, phù hợp cho app không có executor hoặc Roblox VNG.
4. **Hỗ trợ Roblox VNG & Clone mới cài**:
   - Tự phát hiện package VNG để sync đúng `robloxapp.vnggames.com_appStorage.json`.
   - Tự khởi tạo schema Chromium v14 (`cookies` + `meta`) và ghi atomic staged nếu clone mới chưa từng mở WebView.
   - `su()` tự làm sạch `LD_LIBRARY_PATH` / `LD_PRELOAD` của Termux để `am`, `pm`, `wm`, `dumpsys`, `screencap` chạy ổn định dưới quyền root.
5. **Quản lý tài khoản nâng cao (`cookies`, `switched/*.txt`, `passwords.db`)**:
   - Hỗ trợ nhập `user:pass:cookie`, `user|cookie`, hoặc raw cookie; bảo vệ `cookies.txt` bằng `fcntl.flock`.
   - Tự phân loại tài khoản sang `switched/{acc_ok,dead,banned,deleted,captcha,faceid,captcha-lock,warning}.txt`.
   - Kiểm tra trạng thái tài khoản thụ động (`inspect_account_health`) và tạm dừng (`PAUSED_CHALLENGE`) hoặc đổi acc (`on_challenge: pause|rotate`) khi gặp cảnh báo/chờ xác minh thủ công.
6. **Multi-Executor & Profile Blox Fruits**:
   - `scan_executor_dirs()` tự quét thư mục executor trên `/sdcard/` và `/sdcard/Android/data/<pkg>/files/`.
   - Có sẵn profile `blox_fruits` + `profiles/lua/blox_fruits_auto.lua`.
7. **Discord Webhook 3 kênh & Multi-display Screenshot**:
   - Hỗ trợ kênh `webhook` (Monitor), `webhook_change_url` (Đổi acc), `webhook_alert_url` (Cảnh báo/Hết cookie), có `webhook_ping`, mask cookie tự động, xử lý HTTP 429 `Retry-After`, và fallback `screencap -d <display_id>`.
8. **Change Account Custom (`change_acc_cus`) & Blox Fruit (`config-change.json`)**:
   Hỗ trợ đổi tài khoản tự động theo cả 3 cách:
   - **Cách 1 — File Workspace chuẩn Shouko / Yummy / Kaitun (bật sẵn `change_acc_cus`)**:
     Script Kaitun chỉ cần tạo file trong thư mục `Workspace` của executor (hoạt động cả khi chạy chế độ `executor` lẫn `online`):
     - `<Username>.txt` hoặc `<Username>.json` chứa JSON `{"Type": "CDK"}` (hoặc `{"Completed": true, "Type": "Godhuman"}`) → tự xóa file trigger, lưu acc cũ vào `switched/Completed-CDK.txt` + `done.txt`, gửi Webhook Đổi Acc và nạp cookie mới.
     - Hoặc plain text: `Completed-Godhuman`, `Yummy-MaxLevel`, `Completed`, `Done` → lưu vào `switched/Completed-<Type>.txt` (hoặc `switched/Completed.txt`).
   - **Cách 2 — API Lua trực tiếp trong `rejoin_heartbeat.lua`**:
     - `RejoinTool.changeAccount("CDK", "Đã xong CDK")` (ghi `<Username>.txt` + báo heartbeat) hoặc `RejoinTool.markDone("reason")`.
   - **Cách 3 — Điều kiện game (`config-change.json` & `profiles/*.json`)**:
     - Bật `Auto change account (Blox Fruit)` trong `rejoin config` và chỉnh điều kiện qua `rejoin change-config edit` hoặc `rejoin change-config set -k level -v 2600`, khớp qua `<UserId>-info.json` hoặc `hb.stats`.
     - Hoặc cấu hình `rotate_on` / `rules` trong `profiles/*.json` (`"stat:level>=2600"`, `"elapsed_account:120m"`).
   - Xem hướng dẫn chi tiết và mẫu script tại `HUONG_DAN_CUSTOM_CHANGE_ACCOUNT.md` & `profiles/lua/custom_change_template.lua`.

---

## 2. Cách chạy nhanh

Chạy từ Termux:
```bash
rejoin                         # Menu tương tác v3.0 Hybrid
rejoin quick-setup 2753915549  # Cài đặt 1 bước (quét clone, gán link, deploy Lua + shared)
rejoin status                  # Xem trạng thái instance, daemon, CPU/RAM/Disk, cookies
rejoin benchmark               # Đo độ trễ thực tế (/proc batch, SQLite cookie, executor scan)
rejoin config                  # Bảng cấu hình 23 mục kiểu Shouko (S=lưu+reload)
rejoin links import-shouko -f /data/data/com.termux/files/home/qh_rejoin/data/server_links.txt
rejoin gofile <url> --download # Tải APK từ GoFile về Download (thêm --install để cài)
rejoin run --dashboard         # Chạy kèm dashboard trực tiếp
rejoin daemon start            # Chạy nền với IPC (~/.rejoin_ipc.sock)
rejoin daemon attach           # Xem dashboard của daemon đang chạy
rejoin tabs sort               # Chia lưới cửa sổ Roblox
rejoin tabs tiny               # Thu nhỏ các cửa sổ Roblox dạng ô tiny
```

Chạy từ root shell (`su -c`):
```bash
su -c "export PATH=\$PATH:/data/data/com.termux/files/usr/bin && export TERM=xterm-256color && export TMPDIR=/data/data/com.termux/files/usr/tmp && export LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib && rejoin status"
```

---

## 3. Termux:Boot

```bash
rejoin boot enable --profile default
rejoin boot status
rejoin boot stop
rejoin boot disable
```

---

## 4. ⚡ Quick Setup (kiểu QH Rejoin)

> Gõ `rejoin commands` để in toàn bộ các lệnh bên dưới với đúng đường dẫn trên máy bạn.

**Setup** (cài mới / cập nhật — cài Termux packages, xin quyền bộ nhớ, clone repo, cài `requests` + `rich`, tạo lệnh `rejoin`, quét clone + deploy Lua):
```bash
. <(curl -sL https://raw.githubusercontent.com/hoangdangduong159-a11y/rejoin_tool/main/setup.sh)
```
Cài lại từ thư mục local có sẵn (không cần mạng để lấy code):
```bash
. ~/rejoin_tool/setup.sh
```
Tùy chọn: `RJ_TARGET=2753915549 RJ_PROFILE=blox_fruits SKIP_UPGRADE=1 . <(curl -sL ...)`

**Run tool (all cloud with root)** — mở menu:
```bash
su -c "export PATH=\$PATH:/data/data/com.termux/files/usr/bin && export TERM=xterm-256color && export TMPDIR=/data/data/com.termux/files/usr/tmp && export LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib && /data/data/com.termux/files/home/rejoin_tool/rejoin"
```
Thêm ` run` vào cuối (trước dấu `"`) để chạy auto-rejoin headless luôn, không cần chọn menu.

**For termux boot**:
```bash
rejoin boot enable
```
hoặc thủ công:
```bash
mkdir -p ~/.termux/boot
echo '#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock
sleep 15
su -c "export PATH=\$PATH:/data/data/com.termux/files/usr/bin && export TERM=xterm-256color && export TMPDIR=/data/data/com.termux/files/usr/tmp && export LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib && /data/data/com.termux/files/home/rejoin_tool/rejoin run"' > ~/.termux/boot/rejoin_boot.sh
chmod 700 ~/.termux/boot/rejoin_boot.sh
```

**Stop termux boot**:
```bash
su -c "pkill -f '/data/data/com.termux/files/home/rejoin_tool/[m]ain.py'"
```

> [!NOTE]
> - Launcher `rejoin_tool/rejoin` chạy được cả user lẫn root; khi chạy bằng root nó tự trả quyền sở hữu file (config, cookies, logs) về user Termux sau khi thoát.
> - Khác QH Rejoin: boot dùng lệnh headless `rejoin run` thay vì dùng heredoc `<<EOF 1 99999 EOF` để chọn menu.
> - Trước khi dùng link `curl`, hãy push repo lên GitHub và sửa `DEFAULT_REPO` trong `setup.sh`.
