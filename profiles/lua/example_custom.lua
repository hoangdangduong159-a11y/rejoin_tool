-- Example customizable per-game script for Delta Autoexecute
-- Uses the global RejoinTool API injected by rejoin_heartbeat.lua

task.spawn(function()
    while not ((getgenv and getgenv().RejoinTool) or _G.RejoinTool) do
        task.wait(0.5)
    end
    local RT = (getgenv and getgenv().RejoinTool) or _G.RejoinTool
    local Players = game:GetService("Players")
    local lp = Players.LocalPlayer

    while task.wait(5) do
        -- Customize below to read your game's leaderstats / Data folder
        local ls = lp:FindFirstChild("leaderstats")
        if ls then
            local lvl = ls:FindFirstChild("Level") or ls:FindFirstChild("Lvl")
            local coins = ls:FindFirstChild("Beli") or ls:FindFirstChild("Coins") or ls:FindFirstChild("Money")
            if lvl then
                RT.setStat("level", lvl.Value)
                if tonumber(lvl.Value) and tonumber(lvl.Value) >= 100 then
                    RT.markDone("level>=100")
                    break
                end
            end
            if coins then
                RT.setStat("currency", coins.Value)
            end
        end
    end
end)
