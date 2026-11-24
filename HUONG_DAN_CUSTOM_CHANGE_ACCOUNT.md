# Hướng Dẫn Tự Viết Script Custom Change Account (`change_acc_cus`)

Tài liệu này giải thích chi tiết cơ chế **Auto Change Account (Custom)** của `rejoin_tool` (tương thích chuẩn **Shouko / Yummy / Kaitun**) và cung cấp mẫu code Lua hoàn chỉnh để bạn tự viết script đổi acc cho **bất kỳ game Roblox nào**.

---

## 1. Tổng quan luồng hoạt động

Khi bạn bật **`Auto change account (Custom)`** (bật sẵn mặc định), quá trình đổi acc tự động diễn ra theo 6 bước:

```text
[Script Lua trong Roblox]
        │  1. Phát hiện acc đạt mục tiêu -> writefile("<Username>.txt", '{"Type":"CDK"}')
        ▼
[Executor Workspace (/sdcard/Delta/Workspace/<Username>.txt)]
        │  2. rejoin_tool quét Workspace mỗi 1–3 giây (cả mode executor & online)
        ▼
[rejoin_tool: _check_workspace_account_change()]
        │  3. Đọc & parse nội dung -> XÓA NGAY file trigger để không bị lặp
        ▼
[Lưu trữ & Webhook]
        │  4. Lưu acc cũ vào done.txt + switched/Completed-<Type>.txt (kèm user:pass nếu có)
        │     + gửi Discord Webhook Đổi Acc
        ▼
[Đổi Cookie & Khởi động lại]
        │  5. Force-stop package -> pop cookie tiếp theo từ cookies.txt
        │     -> ghi .ROBLOSECURITY + đồng bộ appStorage.json -> mở lại game
```

### So sánh nhanh 3 cách đổi acc trong `rejoin_tool`

| Cách | Cơ chế | Nơi lưu acc xong | Phù hợp khi nào? |
|---|---|---|---|
| **1. Workspace File (`change_acc_cus`)** | Lua dùng `writefile("<Username>.txt", ...)` | `switched/Completed-<Type>.txt` + `done.txt` | **Khuyên dùng nhất**: Tự viết script cho mọi game, hoặc chạy chung với Hub/Kaitun có sẵn. Hoạt động cả ở `check_method: online`. |
| **2. Gọi API `RejoinTool.changeAccount()`** | Gọi hàm Lua có sẵn từ `rejoin_heartbeat.lua` | `switched/Completed-<Type>.txt` + `done.txt` | Bạn đang viết script mới và chỉ cần dùng 1 dòng lệnh gọn nhẹ. |
| **3. `config-change.json` / `profiles/*.json`** | Khớp chỉ số Blox Fruits (`<UserId>-info.json`) hoặc rule `rotate_on` (`stat:level>=2600`) | `switched/acc_ok.txt` + `done.txt` | Farm Blox Fruits không cần viết thêm điều kiện Lua, hoặc xoay acc theo giờ (`elapsed_account:120m`). |

---

## 2. Quy chuẩn Tên File & Nội Dung Trigger (`change_acc_cus`)

`rejoin_tool` tự động theo dõi toàn bộ thư mục `Workspace` của các Executor đã cài (Delta, Codex, Arceus, v.v.). Chỉ cần tạo **1 trong các file** dưới đây ngay bên ngoài thư mục gốc của `Workspace`:

| Tên file trong `Workspace` | Nội dung bên trong | Kết quả phân loại lưu vào `switched/` |
|---|---|---|
| `<Username>.txt` hoặc `<Username>.json` | `{"Type": "CDK"}` hoặc `{"Completed": true, "Type": "CDK", "Reason": "Xong CDK"}` | `switched/Completed-CDK.txt` |
| `<Username>.txt` | `Completed-Godhuman` hoặc `Yummy-Godhuman` | `switched/Completed-Godhuman.txt` |
| `<Username>.txt` | `Completed` hoặc `Done` hoặc `Yummy` | `switched/Completed.txt` |
| `<UserId>.txt` hoặc `<UserId>.json` | Như trên (dùng ID số nếu không muốn dùng tên acc) | `switched/Completed-<Type>.txt` (hoặc `Completed.txt`) |
| `Completed-<Username>.txt` / `.json` hoặc `Completed-<UserId>.txt` / `.json` | Rỗng, hoặc text phân loại (vd: `CDK`), hoặc JSON `{"Type": "CDK"}` | `switched/Completed-<Type>.txt` (hoặc `Completed.txt`) |

### Quy tắc đặt tên `Type` và lọc file an toàn
- `Type` chỉ giữ ký tự `A-Z`, `a-z`, `0-9`, `.`, `_`, `-` (khoảng trắng hoặc ký tự đặc biệt sẽ tự đổi thành `_`). Ví dụ `"Sanguine Art"` thành `switched/Completed-Sanguine_Art.txt`.
- **Không xung đột file khác**: Các file log/config khác trong `Workspace` (kể cả file vô tình trùng `<Username>.txt`) nếu **không** chứa JSON có key `"Type"` / `"Completed": true` / `"status": "done"|"completed"` và **không** bắt đầu bằng `Completed` / `Yummy` / `Done` / `acc_ok` thì sẽ **bị bỏ qua hoàn toàn**, không bao giờ kích hoạt đổi acc nhầm.

---

## 3. Hàm Lua chuẩn & 4 Mẫu Script Thực Tế

Bạn có thể copy file mẫu có sẵn tại [custom_change_template.lua](file:///data/data/com.termux/files/home/rejoin_tool/profiles/lua/custom_change_template.lua) hoặc dùng 1 trong 4 mẫu dưới đây.

### Hàm helper chuẩn (Copy vào đầu script của bạn)

```lua
local Players = game:GetService("Players")
local HttpService = game:GetService("HttpService")
local player = Players.LocalPlayer or Players.PlayerAdded:Wait()

_G._CustomChangeTriggered = false

local function ChangeAccountCustom(switchType, reason)
    if _G._CustomChangeTriggered then return end
    _G._CustomChangeTriggered = true

    local sw = tostring(switchType or "Custom")
    local rs = tostring(reason or sw)

    if writefile then
        -- Tương thích cả rejoin_tool và Shouko gốc
        writefile(player.Name .. ".txt", HttpService:JSONEncode({
            Type = sw,
            Completed = true,
            Reason = rs,
        }))
    end

    -- Đồng bộ thêm với rejoin_heartbeat.lua nếu có
    local rt = (getgenv and getgenv().RejoinTool) or _G.RejoinTool
    if rt and rt.changeAccount then
        pcall(rt.changeAccount, sw, rs)
    end
end
```

---

### Mẫu 1: Đổi acc khi đủ Level / Tiền / Gems (game bất kỳ có `Data` hoặc `leaderstats`)

```lua
task.spawn(function()
    if not game:IsLoaded() then game.Loaded:Wait() end
    local player = game:GetService("Players").LocalPlayer
    while not player do task.wait(0.2) player = game:GetService("Players").LocalPlayer end

    task.wait(10) -- Đợi game load Data xong

    while not _G._CustomChangeTriggered do
        local ls = player:FindFirstChild("leaderstats")
        local level = ls and ls:FindFirstChild("Level")
        local coins = ls and ls:FindFirstChild("Coins")

        if level and tonumber(level.Value) >= 100 and coins and tonumber(coins.Value) >= 500000 then
            ChangeAccountCustom("Lv100_500k", "Dat Lv100 + 500k Coins")
            break
        end
        task.wait(5)
    end
end)
```

---

### Mẫu 2: Tự phân loại tài khoản vào nhiều file `Completed-*.txt` theo item đạt được

Ví dụ: Acc nào xong **CDK** lưu vào `switched/Completed-CDK.txt`, xong **Sanguine Art** lưu vào `switched/Completed-SanguineArt.txt`:

```lua
local function hasItem(name)
    local p = game:GetService("Players").LocalPlayer
    local bp = p:FindFirstChild("Backpack")
    local ch = p.Character
    return (bp and bp:FindFirstChild(name) ~= nil) or (ch and ch:FindFirstChild(name) ~= nil)
end

task.spawn(function()
    task.wait(12)
    while not _G._CustomChangeTriggered do
        if hasItem("Cursed Dual Katana") and hasItem("Sanguine Art") then
            ChangeAccountCustom("CDK_Sanguine", "Co du CDK + Sanguine Art")
            break
        elseif hasItem("Cursed Dual Katana") then
            ChangeAccountCustom("CDK", "Co Cursed Dual Katana")
            break
        end
        task.wait(5)
    end
end)
```

---

### Mẫu 3: Chạy kèm bên ngoài Hub/Kaitun đã mã hóa (không cần sửa code của Hub)

Nếu bạn dùng các script Kaitun đóng gói (`loadstring(...)()`), bạn chỉ cần đặt script canh điều kiện của bạn thành **1 file `.lua` thứ 2 trong `Autoexecute`**.
Hai script chạy song song độc lập: Hub lo farm, còn script của bạn định kỳ kiểm tra kho đồ / chỉ số hoặc file log của Hub đó rồi gọi `ChangeAccountCustom(...)`.

---

### Mẫu 4: Đổi acc khi thấy thông báo UI trong `PlayerGui` (hết lượt roll / hết sự kiện)

```lua
task.spawn(function()
    task.wait(10)
    local p = game:GetService("Players").LocalPlayer
    local pgui = p:WaitForChild("PlayerGui", 30)

    while pgui and not _G._CustomChangeTriggered do
        for _, obj in ipairs(pgui:GetDescendants()) do
            if obj:IsA("TextLabel") and obj.Visible then
                local txt = string.lower(obj.Text or "")
                if string.find(txt, "no spins left") or string.find(txt, "daily limit") then
                    ChangeAccountCustom("OutOfSpins", "Het luot quay: " .. obj.Text)
                    return
                end
            end
        end
        task.wait(3)
    end
end)
```

---

## 4. 4 Lưu ý quan trọng để không bị đổi acc nhầm

1. **Luôn đặt `task.wait(10)` (hoặc `WaitForChild`) trước vòng lặp kiểm tra**:
   Khi vừa vào game, các giá trị `Data`/`leaderstats` có thể tạm thời là `0` hoặc `nil` trong vài giây đầu. Chờ game load xong mới kiểm tra sẽ tránh Trigger sai, đặc biệt nếu điều kiện là kiểm tra hết lượt (`<= 0`).
2. **Luôn dùng biến cờ `_G._CustomChangeTriggered = true`**:
   Đảm bảo file chỉ được ghi **đúng 1 lần** trong lúc chờ `rejoin_tool` tắt app (tối đa ~1–3 giây).
3. **Không cần tự xóa `<Username>.txt` trong Lua**:
   `rejoin_tool` sẽ tự xóa file ngay khi đọc xong, và cũng tự dọn mọi file trigger còn sót khi bơm cookie mới vào package.
4. **Kiểm tra nguồn cookie dự phòng**:
   `cookies.txt` phải có cookie mới và instance phải bật `use_cookie_queue: true`. Nếu hết `cookies.txt`, tool sẽ gửi cảnh báo hết cookie và dừng/chờ tùy cấu hình `when_no_cookies`.

---

## 5. Cài đặt & Test nhanh trong 30 giây

### Bước 1: Kiểm tra cấu hình đã bật Custom Change chưa
Mở bảng cấu hình:
```bash
rejoin config
```
Đảm bảo dòng **5 (`Auto change account (Custom - <User>.txt)`)** đang là **ON**, rồi nhấn **S** để lưu.

### Bước 2: Đưa script tự viết của bạn vào Executor Autoexecute
```bash
rejoin scripts add -n my_custom_change.lua -f /sdcard/Download/my_custom_change.lua
rejoin scripts list
```
*(Hoặc lưu script vào `profiles/lua/` và gán tên file vào trường `"lua"` trong profile rồi chạy `rejoin deploy-lua --profile ten_profile --all`.)*

### Bước 3: Test thử ngay trong game
Khi acc đang trong game, thử chạy nhanh 1 dòng sau ở Executor (hoặc tạo file `<TenAcc>.txt` bằng tay trong `/sdcard/Delta/Workspace/`):
```lua
writefile(game.Players.LocalPlayer.Name .. ".txt", "Completed-Test")
```
Trong 1–3 giây tool sẽ tắt app, lưu acc cũ vào `switched/Completed-Test.txt`, gửi Webhook (nếu đã cài) và nạp acc kế tiếp. Xem kết quả bằng lệnh:
```bash
rejoin cookies switched
```
