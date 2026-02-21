-- keybinds.lua: CableTV keyboard bindings for mpv.
--
-- Binds channel up/down, digit entry (direct tune), info overlay,
-- and quit. Calls the Flask API via curl subprocess (fire-and-forget).
--
-- Receives Flask port via --script-opts=cabletv-port=5000

local port = mp.get_opt("cabletv-port") or "5000"
local base_url = "http://127.0.0.1:" .. port

-- ============================================================
-- HTTP helper: fire-and-forget POST to Flask API
-- ============================================================
local function api_post(endpoint)
    mp.command_native_async({
        name = "subprocess",
        args = {"curl", "-s", "-X", "POST", base_url .. endpoint},
        playback_only = false,
        capture_stdout = false,
        capture_stderr = false,
    }, function() end)
end

-- ============================================================
-- Channel Up / Down
-- ============================================================
mp.add_forced_key_binding("UP", "cabletv-ch-up", function()
    api_post("/api/channel/up")
end, {repeatable = false})

mp.add_forced_key_binding("DOWN", "cabletv-ch-down", function()
    api_post("/api/channel/down")
end, {repeatable = false})

-- ============================================================
-- Digit entry (direct channel tune)
-- ============================================================
local digit_buffer = ""
local digit_timer = nil
local DIGIT_TIMEOUT = 1.5

local function commit_channel()
    if digit_timer then
        digit_timer:kill()
        digit_timer = nil
    end
    if digit_buffer ~= "" then
        local channel = digit_buffer
        digit_buffer = ""
        api_post("/api/channel/" .. channel)
    end
end

local function on_digit(d)
    if digit_timer then
        digit_timer:kill()
        digit_timer = nil
    end

    digit_buffer = digit_buffer .. d
    mp.osd_message("Ch " .. digit_buffer, DIGIT_TIMEOUT)

    -- Two digits: commit immediately (max channel is 55)
    if #digit_buffer >= 2 then
        commit_channel()
        return
    end

    -- Single digit: wait for more
    digit_timer = mp.add_timeout(DIGIT_TIMEOUT, function()
        digit_timer = nil
        commit_channel()
    end)
end

for i = 0, 9 do
    local d = tostring(i)
    mp.add_forced_key_binding(d, "cabletv-digit-" .. d, function()
        on_digit(d)
    end, {repeatable = false})
end

-- ============================================================
-- Info overlay
-- ============================================================
mp.add_forced_key_binding("i", "cabletv-info", function()
    api_post("/api/info")
end, {repeatable = false})

-- ============================================================
-- Quit
-- ============================================================
mp.add_forced_key_binding("q", "cabletv-quit", function()
    mp.command("quit")
end, {repeatable = false})
