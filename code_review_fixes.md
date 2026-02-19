# Schedule System Code Review — Fix Tracker

## Bugs

### 1. Weather clock shows on wrong channel
**File:** `app/cabletv/playback/engine.py`
**Severity:** Bug - visible
**Status:** [x] Fixed

Added channel guard check inside `_show_weather_clock()` that returns early if no longer on weather channel. Stored the initial clock timer as `_weather_clock_timer` so it can be cancelled by `_cancel_all_timers()`.

---

### 2. Timer race can override user channel change
**File:** `app/cabletv/playback/engine.py`
**Severity:** Bug - intermittent
**Status:** [x] Fixed

Added a second lock check in `_on_content_end()` that verifies `_current_channel` hasn't changed between the initial snapshot and the `tune_to()` call. If it changed (user switched), the timer silently returns.

---

### 3. Block cache invalidation contradicts base engine in remote/server mode
**Files:** `app/cabletv/schedule/remote_provider.py`, `app/cabletv/main.py`
**Severity:** Bug - server/remote mode
**Status:** [x] Fixed

Removed block cache invalidation from `RemoteScheduleProvider.advance_position` and simplified `_server_advance` in main.py to match the base engine's intentional "don't clear cache" behavior.

---

### 4. `slot_remaining_seconds` wrong during commercials and mid-content
**File:** `app/cabletv/schedule/engine.py`
**Severity:** Bug - API correctness
**Status:** [x] Fixed

Changed to compute from `(slot_end_time - start_time) - elapsed_seconds` which gives the true remaining time in the entire slot block regardless of current segment type.

---

## Performance

### 5. Break points queried from DB on every `what_is_on()` call
**File:** `app/cabletv/schedule/engine.py`
**Severity:** Performance
**Status:** [x] Fixed

Added `_break_point_cache` dict keyed by content_id. First lookup hits DB, subsequent lookups are instant. Cache cleared with `clear_cache()`.

---

## Maintenance / Cleanup

### 6. Timer cancellation copy-pasted in 4+ places
**File:** `app/cabletv/playback/engine.py`
**Severity:** Maintenance hazard
**Status:** [x] Fixed

Extracted `_cancel_all_timers()` helper that iterates all 5 timer fields. Used in `tune_to()`, `_tune_to_guide()`, `_tune_to_weather()`, and `stop()`.

---

### 7. Dead `preserve_block_start` parameter documented as "Unused" but actually used
**File:** `app/cabletv/schedule/engine.py`
**Severity:** Confusing
**Status:** [ ] Deferred — kept for now since remote/server still pass it to the server API

---

### 8. `_type_avg_durations` only samples first episode per group
**File:** `app/cabletv/schedule/engine.py`
**Severity:** Minor inaccuracy
**Status:** [x] Fixed

Changed to average all items across all groups per type, not just the first item.

---

### 9. `check_collisions` can never find collisions
**File:** `app/cabletv/schedule/engine.py`
**Severity:** Dead code
**Status:** [ ] Deferred — harmless, used as a CLI diagnostic tool

---

### 10. Debug print statements left in production
**File:** `app/cabletv/playback/engine.py`
**Severity:** Noise
**Status:** [x] Fixed

Removed all `[BUMPER DEBUG]` print statements from `tune_to()` and `_show_next_episode_bumper()`.
