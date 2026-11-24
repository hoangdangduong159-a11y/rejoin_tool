# ⚡ Hướng dẫn Setup nhanh — Rejoin Tool v3.0 Hybrid

> Chỉ cần copy-paste lệnh, không phải tạo file thủ công.
> Trên máy đã cài, gõ `rejoin commands` để in lại toàn bộ các lệnh này.

---

## 0. Chuẩn bị

- Máy (hoặc cloud phone) đã **root** (Magisk/KernelSU), cài **Termux** từ F-Droid/GitHub.
- Muốn tự chạy khi khởi động máy: cài thêm app **Termux:Boot** (F-Droid) và **mở nó 1 lần**.
- Lần đầu chạy, khi Magisk hỏi cấp quyền root cho Termux → bấm **Cho phép**.

---

## 1. Setup (cài mới / cập nhật)

Mở Termux (**không** gõ `su`), dán:

```bash
. <(curl -sL https://raw.githubusercontent.com/hoangdangduong159-a11y/rejoin_tool/main/setup.sh)
```

Lệnh này sẽ tự:
1. Xin quyền bộ nhớ (bấm **Cho phép**).
2. `pkg update/upgrade`, cài `python`, `git`, `curl`, `procps`.
3. Clone (hoặc cập nhật) tool về `~/rejoin_tool` — **không xóa** config, cookie đã có.
4. Cài thư viện Python `requests`, `rich`.
5. Tạo lệnh `rejoin` dùng ở mọi nơi.
6. Quét tất cả clone Roblox, deploy Lua heartbeat + shared scripts vào executor.

Tùy chọn (đặt trước dấu `.`):

| Biến | Ý nghĩa |
|---|---|
| `RJ_TARGET=2753915549` | Gán luôn PlaceID / link VIP cho mọi clone |
| `RJ_PROFILE=blox_fruits` | Chọn profile (mặc định `default`) |
| `SKIP_UPGRADE=1` | Bỏ qua `pkg upgrade` cho nhanh |
| `SKIP_STORAGE=1` | Bỏ qua bước xin quyền bộ nhớ |

Ví dụ:
```bash
RJ_TARGET=2753915549 SKIP_UPGRADE=1 . <(curl -sL https://raw.githubusercontent.com/hoangdangduong159-a11y/rejoin_tool/main/setup.sh)
```

---

## 2. Run tool (all cloud with root)

Mở **menu**:
```bash
su -c "export PATH=\$PATH:/data/data/com.termux/files/usr/bin && export TERM=xterm-256color && export TMPDIR=/data/data/com.termux/files/usr/tmp && export LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib && /data/data/com.termux/files/home/rejoin_tool/rejoin"
```

Chạy **auto-rejoin luôn** (không qua menu):
```bash
su -c "export PATH=\$PATH:/data/data/com.termux/files/usr/bin && export TERM=xterm-256color && export TMPDIR=/data/data/com.termux/files/usr/tmp && export LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib && /data/data/com.termux/files/home/rejoin_tool/rejoin run"
```

> Có thể thêm tham số sau `run`, ví dụ `run --dashboard` (bảng trực tiếp), `run --profile blox_fruits`, `run --target 2753915549`.

---

## 3. For Termux Boot (tự chạy khi bật máy)

Cách nhanh:
```bash
rejoin boot enable
```

Hoặc thủ công:
```bash
mkdir -p ~/.termux/boot
echo '#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock
sleep 15
su -c "export PATH=\$PATH:/data/data/com.termux/files/usr/bin && export TERM=xterm-256color && export TMPDIR=/data/data/com.termux/files/usr/tmp && export LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib && /data/data/com.termux/files/home/rejoin_tool/rejoin run"' > ~/.termux/boot/rejoin_boot.sh
chmod 700 ~/.termux/boot/rejoin_boot.sh
```

Log khi chạy từ boot: `~/rejoin_tool/logs/boot.log`

---

## 4. Stop Termux Boot / dừng tool

```bash
su -c "pkill -f '/data/data/com.termux/files/home/rejoin_tool/[m]ain.py'"
```

Tắt hẳn tự chạy khi khởi động:
```bash
rejoin boot disable
```

Xem trạng thái:
```bash
rejoin boot status
```

---

## 5. Các lệnh hay dùng sau khi setup

```bash
rejoin                         # Menu tương tác
rejoin commands                # In lại toàn bộ lệnh setup/run/boot/stop
rejoin quick-setup 2753915549  # Quét clone + gán link + deploy Lua (chạy lại khi thêm clone)
rejoin cookies add -f /sdcard/Download/cookie.txt   # Nạp cookie vào hàng đợi
rejoin status                  # Trạng thái instance, CPU/RAM, cookie
rejoin config                  # Bảng cấu hình kiểu Shouko
```

---

## ❓ Lỗi thường gặp

| Lỗi | Cách xử lý |
|---|---|
| `pkg update` chậm / lỗi mirror | Chạy `termux-change-repo`, chọn mirror khác rồi setup lại |
| `Chưa có quyền root (su)` | Cấp root cho Termux trong Magisk, rồi `rejoin quick-setup <PlaceID>` |
| `No Roblox/Executor packages detected` | Kiểm tra đã cài clone Roblox chưa, rồi chạy lại `rejoin quick-setup` |
| Boot không tự chạy | Cài & mở app Termux:Boot 1 lần, tắt tối ưu pin cho Termux và Termux:Boot |
| `Permission denied` với file trong `~/rejoin_tool` | Chạy lại lệnh Setup (launcher tự sửa quyền sở hữu file) |
