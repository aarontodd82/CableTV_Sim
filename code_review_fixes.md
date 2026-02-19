# Schedule System Code Review — Fix Tracker

## Bugs

### 1. Weather clock shows on wrong channel
**File:** `app/cabletv/playback/engine.py:805-808`
**Severity:** Bug - visible
**Status:** [ ] Not started

The initial weather clock timer is fire-and-forget (not stored in an instance variable, no channel guard check). If you tune to the weather channel and switch away within ~2.5s, the timer fires `_show_weather_clock()` which adds an overlay to whatever channel is now playing.

**Fix:** Store the timer and cancel it with the others, or add a channel guard inside `_show_weather_clock()`.

---

### 2. Timer race can override user channel change
**File:** `app/cabletv/playback/engine.py:903-916`
**Severity:** Bug - intermittent

`_on_content_end()` reads `_current_channel` under lock, releases the lock, then calls `tune_to()`. A user channel change in the gap overwrites the user's choice.

**Fix:** Check that `_current_channel` hasn't changed before calling `tune_to()`, or re-check inside `tune_to` early.

---

### 3. Block cache invalidation contradicts base engine in remote/server mode
**Files:** `app/cabletv/schedule/remote_provider.py:73-77`, `app/cabletv/main.py:80-85`
**Severity:** Bug - server/remote mode

The base `advance_position()` explicitly does NOT clear the block cache (with a detailed comment explaining cascade instability). But `RemoteScheduleProvider.advance_position` and `_server_advance` in main.py both clear it — causing the exact instability the base engine avoids.

**Fix:** Remove block cache invalidation from remote_provider and _server_advance to match base engine behavior.

---

### 4. `slot_remaining_seconds` wrong during commercials and mid-content
**File:** `app/cabletv/schedule/engine.py:383-388`
**Severity:** Bug - API correctness

During a commercial, returns remaining time in the current commercial segment only (not the whole slot). During content, returns remaining time in the current content segment + commercial padding (omitting remaining content after future break points).

**Fix:** Calculate true slot remaining from `slot_end_time - now` instead of summing partial values.

---

## Performance

### 5. Break points queried from DB on every `what_is_on()` call
**File:** `app/cabletv/schedule/engine.py:763-767`
**Severity:** Performance

Opens a DB connection and queries break_points every call. Break points never change at runtime. Fires on every channel change, timer transition, guide generation, API poll.

**Fix:** Add a break point cache dict keyed by content_id.

---

## Maintenance / Cleanup

### 6. Timer cancellation copy-pasted in 4+ places
**File:** `app/cabletv/playback/engine.py`
**Severity:** Maintenance hazard

4 timer fields cancelled identically in `tune_to()`, `_tune_to_guide()`, `_tune_to_weather()`, `stop()`. Adding a 5th timer requires updating 4+ locations.

**Fix:** Extract `_cancel_all_timers()` helper.

---

### 7. Dead `preserve_block_start` parameter documented as "Unused" but actually used
**File:** `app/cabletv/schedule/engine.py:526-528`
**Severity:** Confusing

Docstring says "Unused (kept for API compat)" but server_advance and RemoteScheduleProvider both use it.

**Fix:** Remove the parameter entirely (will be resolved as part of fix #3).

---

### 8. `_type_avg_durations` only samples first episode per group
**File:** `app/cabletv/schedule/engine.py:569-573`
**Severity:** Minor inaccuracy

Averages only the first item's duration from each group, not the true group average.

**Fix:** Average all items, or at minimum document the sampling strategy.

---

### 9. `check_collisions` can never find collisions
**File:** `app/cabletv/schedule/engine.py:1340-1367`
**Severity:** Dead code

`what_is_on()` already enforces collision avoidance via `_get_exclusions()`, so `check_collisions` always returns empty.

**Fix:** Remove or repurpose (e.g., check across a time range, or test with exclusions disabled).

---

### 10. Debug print statements left in production
**File:** `app/cabletv/playback/engine.py:261-300`
**Severity:** Noise

Extensive `[BUMPER DEBUG]` prints fire on every `tune_to()` call for shows.

**Fix:** Remove or gate behind a debug/verbose flag.
