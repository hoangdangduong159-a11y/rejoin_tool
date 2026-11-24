-- Mau Script Custom Change Account cho rejoin_tool v2 (va Shouko/Yummy)
-- Dat file nay vao Autoexecute cua Executor (hoac gan vao profile qua field "lua")

local CHECK_INTERVAL = 5   -- So giay kiem tra dieu kien 1 lan
local STARTUP_DELAY = 10   -- Cho du lieu nhan vat load xong tranh doi acc nham

if not game:IsLoaded() then
    game.Loaded:Wait()
end

local Players = game:GetService("Players")
local HttpService = game:GetService("HttpService")

local player = Players.LocalPlayer
while not player do
    task.wait(0.2)
    player = Players.LocalPlayer
end

-- Bien co chong kich hoat trung lap trong cung 1 phien choi
_G._CustomChangeTriggered = false

-- Ham chuan kich hoat Custom Change Account
local function ChangeAccountCustom(switchType, reason)
    if _G._CustomChangeTriggered then
        return
    end
    _G._CustomChangeTriggered = true

    local sw = tostring(switchType or "Custom")
    local rs = tostring(reason or ("Custom: " .. sw))

    -- 1. Ghi file <Username>.txt trong Workspace (tuong thich ca rejoin_tool lan Shouko)
    pcall(function()
        if writefile then
            local payload = HttpService:JSONEncode({
                Type = sw,
                Completed = true,
                Reason = rs,
            })
            writefile(player.Name .. ".txt", payload)
        end
    end)

    -- 2. Bao cho RejoinTool heartbeat (neu rejoin_heartbeat.lua da nap)
    pcall(function()
        local rt = (getgenv and getgenv().RejoinTool) or _G.RejoinTool
        if rt and rt.changeAccount then
            rt.changeAccount(sw, rs)
        end
    end)
end

-- Ham kiem tra dieu kien game cua ban
-- Tra ve: true, "TenPhanLoai", "LyDo" khi dat muc tieu
-- Hoac tra ve: false khi chua dat
local function checkCondition()
    -- Vi du 1: Kiem tra Data (Level / Tien)
    local data = player:FindFirstChild("Data")
    local ls = player:FindFirstChild("leaderstats")

    if data then
        local levelObj = data:FindFirstChild("Level")
        if levelObj and tonumber(levelObj.Value) and tonumber(levelObj.Value) >= 2600 then
            return true, "LevelMax", "Dat Level " .. tostring(levelObj.Value)
        end
    end

    -- Vi du 2: Kiem tra vat pham trong Backpack hoac Character
    local bp = player:FindFirstChild("Backpack")
    local ch = player.Character
    if (bp and bp:FindFirstChild("Cursed Dual Katana")) or (ch and ch:FindFirstChild("Cursed Dual Katana")) then
        return true, "CDK", "Da so huu Cursed Dual Katana"
    end

    -- Vi du 3: Kiem tra leaderstats chung cho moi game
    if ls then
        local coins = ls:FindFirstChild("Coins") or ls:FindFirstChild("Cash") or ls:FindFirstChild("Gems")
        if coins and tonumber(coins.Value) and tonumber(coins.Value) >= 1000000 then
            return true, "Rich1M", "Dat " .. tostring(coins.Value)
        end
    end

    return false, nil, nil
end

task.spawn(function()
    task.wait(STARTUP_DELAY)
    while not _G._CustomChangeTriggered do
        local ok, matched, switchType, reason = pcall(checkCondition)
        if ok and matched then
            ChangeAccountCustom(switchType, reason)
            break
        end
        task.wait(CHECK_INTERVAL)
    end
end)
