-- autocrop.lua: Auto-detect and remove baked-in black bars.
--
-- Ensures video always fills the display width. After detecting
-- the content area, if it's taller than 4:3, the height is trimmed
-- so the result fills width and lets top/bottom overflow naturally.

local label = mp.get_script_name() .. "-cropdetect"
local detect_timer = nil

local DETECT_SECONDS = 0.3
local MIN_CROP_PX = 14
local TARGET_RATIO = 4 / 3
-- Black threshold for bar detection. Video uses TV range where
-- black = luminance 16. limit=24 catches bars (16) with margin
-- for compression noise. Dark-content false positives are handled
-- by the sanity checks below, not by this threshold.
local DETECT_LIMIT = 24

local function remove_filter()
    mp.command(string.format("no-osd vf remove @%s", label))
end

local function on_detect_complete()
    detect_timer = nil

    local meta = mp.get_property_native("vf-metadata/" .. label)
    pcall(remove_filter)

    if not meta then return end

    local w = tonumber(meta["lavfi.cropdetect.w"])
    local h = tonumber(meta["lavfi.cropdetect.h"])
    local x = tonumber(meta["lavfi.cropdetect.x"])
    local y = tonumber(meta["lavfi.cropdetect.y"])

    if not w or not h or not x or not y then return end

    local vw = mp.get_property_number("width")
    local vh = mp.get_property_number("height")
    if not vw or not vh then return end

    -- Sanity check: reject if detected area is implausibly small.
    -- Width should be near full-frame (content is 4:3-normalized, bars
    -- are horizontal only). Height can be shorter for wide movies but
    -- not less than 30% (even ultra-wide 2.67:1 is ~50% height).
    if w < vw * 0.75 or h < vh * 0.3 then
        mp.msg.info(string.format(
            "Crop rejected (area too small): %dx%d in %dx%d frame",
            w, h, vw, vh))
        return
    end

    -- If content is taller than 4:3, trim height so width fills
    local ratio = w / h
    if ratio < TARGET_RATIO then
        local new_h = math.floor(w / TARGET_RATIO)
        -- Keep it even (video codecs prefer even dimensions)
        new_h = new_h - (new_h % 2)
        local trim = h - new_h
        y = y + math.floor(trim / 2)
        h = new_h
        mp.msg.info(string.format("Clamped to 4:3: %dx%d+%d+%d", w, h, x, y))
    end

    mp.msg.info(string.format("Frame: %dx%d  Crop: %dx%d+%d+%d",
        vw, vh, w, h, x, y))

    if (vw - w) >= MIN_CROP_PX or (vh - h) >= MIN_CROP_PX then
        local crop = string.format("%sx%s+%s+%s", w, h, x, y)
        mp.set_property("video-crop", crop)
    end
end

local function on_file_loaded()
    if detect_timer then
        detect_timer:kill()
        detect_timer = nil
        pcall(remove_filter)
    end

    pcall(mp.set_property, "video-crop", "")

    -- Skip detection for static images (bumper backgrounds, etc.)
    -- They are generated at exact target resolution and have no bars.
    local path = mp.get_property("path", "")
    if path:match("%.png$") or path:match("%.jpg$") or path:match("%.bmp$") then
        return
    end

    local cmd = string.format(
        "no-osd vf pre @%s:cropdetect=limit=%d/255:round=2:reset=0",
        label, DETECT_LIMIT)

    local ok, err = pcall(mp.command, cmd)
    if not ok then return end

    detect_timer = mp.add_timeout(DETECT_SECONDS, on_detect_complete)
end

mp.register_event("file-loaded", on_file_loaded)
