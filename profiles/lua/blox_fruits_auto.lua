-- Blox Fruits stat collector for rejoin_tool v2 (reports into RejoinTool heartbeat)
task.spawn(function()
    while not ((getgenv and getgenv().RejoinTool) or _G.RejoinTool) do
        task.wait(0.5)
    end
    local RT = (getgenv and getgenv().RejoinTool) or _G.RejoinTool
    local Players = game:GetService("Players")
    local lp = Players.LocalPlayer

    local function readChildVal(parent, name)
        if not parent then return nil end
        local obj = parent:FindFirstChild(name)
        if obj and obj:IsA("ValueBase") then
            return obj.Value
        end
        return nil
    end

    local function hasToolOrItem(name)
        local bp = lp:FindFirstChild("Backpack")
        local ch = lp.Character
        if bp and bp:FindFirstChild(name) then return true end
        if ch and ch:FindFirstChild(name) then return true end
        return false
    end

    while task.wait(5) do
        local data = lp:FindFirstChild("Data")
        local ls = lp:FindFirstChild("leaderstats")
        local stats = {}

        if data then
            local lvl = readChildVal(data, "Level")
            local beli = readChildVal(data, "Beli")
            local frag = readChildVal(data, "Fragments")
            local race = readChildVal(data, "Race")
            local fruit = readChildVal(data, "DevilFruit")
            if lvl ~= nil then stats.level = tonumber(lvl) or lvl end
            if beli ~= nil then stats.beli = tonumber(beli) or beli end
            if frag ~= nil then
                stats.fragments = tonumber(frag) or frag
                stats.fragment = stats.fragments
            end
            if race ~= nil then stats.race_ver = tostring(race) end
            if fruit ~= nil then stats.fruit = tostring(fruit) end
        end

        if ls then
            if stats.level == nil then
                local lvl2 = readChildVal(ls, "Level")
                if lvl2 ~= nil then stats.level = tonumber(lvl2) or lvl2 end
            end
            local bh = readChildVal(ls, "Bounty/Honor")
            if bh ~= nil then
                stats.bounty = tonumber(bh) or bh
                stats["bounty/honor"] = stats.bounty
            end
        end

        stats.god_human = hasToolOrItem("Godhuman")
        stats.sanguine_art = hasToolOrItem("Sanguine Art")
        stats.cursed_dual_katana = hasToolOrItem("Cursed Dual Katana")
        stats.true_triple_katana = hasToolOrItem("True Triple Katana")
        stats.shark_anchor = hasToolOrItem("Shark Anchor")
        stats.skull_guitar = hasToolOrItem("Skull Guitar")
        stats.mirror_fractal = hasToolOrItem("Mirror Fractal")
        stats.valkyrie_helm = hasToolOrItem("Valkyrie Helm")

        RT.setStats(stats)
        pcall(function()
            if writefile then
                local HttpService = game:GetService("HttpService")
                writefile(tostring(lp.UserId) .. "-info.json", HttpService:JSONEncode(stats))
            end
        end)
    end
end)
