"""Commercial selection and timing logic.

Optimized for large commercial pools (1000+) using:
- Pre-sorted duration list with binary search
- Index-based deterministic selection (no array shuffling)
- Cached data structures that rebuild on content changes
"""

import bisect
import random
from dataclasses import dataclass
from typing import Optional

from ..db import db_connection, get_ready_content


@dataclass
class CommercialCache:
    """Pre-computed data structures for fast commercial selection.

    Built once when first accessed, cleared when content changes.
    Rebuild is automatic on next access after clear.
    """
    pool: list[dict]  # All commercials in database order
    by_duration: list[dict]  # Sorted by duration ascending
    durations: list[float]  # Duration values only, for binary search
    shortest: float  # Shortest commercial duration
    longest: float  # Longest commercial duration


# Single cache instance
_cache: Optional[CommercialCache] = None


def _build_cache() -> CommercialCache:
    """Build optimized cache structures from database."""
    with db_connection() as conn:
        content_list = get_ready_content(conn, content_type="commercial")
        pool = [dict(row) for row in content_list]

    if not pool:
        return CommercialCache(
            pool=[],
            by_duration=[],
            durations=[],
            shortest=0.0,
            longest=0.0,
        )

    # Pre-sort by duration (done once at cache time, not per-break)
    by_duration = sorted(pool, key=lambda c: c["duration_seconds"])

    # Extract durations for O(log n) binary search
    durations = [c["duration_seconds"] for c in by_duration]

    return CommercialCache(
        pool=pool,
        by_duration=by_duration,
        durations=durations,
        shortest=durations[0],
        longest=durations[-1],
    )


def _get_cache() -> CommercialCache:
    """Get or build the commercial cache."""
    global _cache
    if _cache is None:
        _cache = _build_cache()
    return _cache


def get_commercial_pool() -> list[dict]:
    """Get all ready commercials from the database. Cached for performance."""
    return _get_cache().pool


def clear_commercial_cache() -> None:
    """Clear the commercial cache. Call after content changes.

    Cache rebuilds automatically on next access.
    """
    global _cache
    _cache = None


def get_commercials_for_break(
    break_duration_seconds: float,
    channel_number: int,
    slot_number: int,
    seed: int,
    offset_seconds: float = 0
) -> list[dict]:
    """
    Select commercials to fill a break deterministically.

    Optimized for large pools using binary search and index-based selection.
    Time complexity: O(k * log(n)) where k = commercials selected, n = pool size.

    Same inputs always produce same outputs (deterministic via seeded RNG).

    Args:
        break_duration_seconds: Total time to fill with commercials
        channel_number: Channel number (for seeding)
        slot_number: Slot number (for seeding)
        seed: Base seed from config
        offset_seconds: How far into the break we currently are (unused, for API compat)

    Returns:
        List of commercial dicts with 'start_in_break' indicating
        when each commercial starts within the break
    """
    cache = _get_cache()

    if not cache.pool:
        return []

    # Quick check: if break is shorter than shortest commercial, it's all standby
    if break_duration_seconds < cache.shortest - 0.5:
        if break_duration_seconds > 1.0:
            return [{
                "id": -1,
                "title": "Please Stand By",
                "duration_seconds": break_duration_seconds,
                "start_in_break": 0.0,
                "file_path": "",
                "is_standby": True,
            }]
        return []

    # Deterministic RNG for this specific break
    break_seed = seed + (channel_number * 10000) + slot_number
    rng = random.Random(break_seed)

    pool_size = len(cache.by_duration)
    selected = []
    total_duration = 0.0
    used_indices: set[int] = set()  # Track used to prefer variety

    # Safety limit - typical break needs 3-10 commercials
    max_iterations = 30

    for _ in range(max_iterations):
        if total_duration >= break_duration_seconds:
            break

        remaining = break_duration_seconds - total_duration

        # Binary search: find rightmost index where duration <= remaining + tolerance
        # bisect_right returns insertion point, so subtract 1 to get last fitting index
        max_fitting_idx = bisect.bisect_right(cache.durations, remaining + 0.5) - 1

        if max_fitting_idx < 0:
            # No commercial fits - add standby placeholder for remaining time
            if remaining > 1.0:
                selected.append({
                    "id": -1,
                    "title": "Please Stand By",
                    "duration_seconds": remaining,
                    "start_in_break": total_duration,
                    "file_path": "",
                    "is_standby": True,
                })
            break

        # Select from fitting commercials, preferring unused ones for variety
        # Fitting indices are 0 to max_fitting_idx (inclusive)
        fitting_count = max_fitting_idx + 1

        # Try to find an unused commercial
        selected_idx = None

        if len(used_indices) < fitting_count:
            # There are unused fitting commercials - pick one randomly
            # Generate random indices until we find unused (fast when pool >> used)
            for _ in range(min(20, fitting_count)):
                candidate = rng.randint(0, max_fitting_idx)
                if candidate not in used_indices:
                    selected_idx = candidate
                    break

            # Fallback: linear scan for unused (guaranteed to find one)
            if selected_idx is None:
                for i in range(fitting_count):
                    if i not in used_indices:
                        selected_idx = i
                        break

        # If all fitting commercials used, allow repeat
        if selected_idx is None:
            selected_idx = rng.randint(0, max_fitting_idx)

        commercial = cache.by_duration[selected_idx]

        selected.append({
            **commercial,
            "start_in_break": total_duration,
        })
        total_duration += commercial["duration_seconds"]
        used_indices.add(selected_idx)

    return selected


def get_current_commercial(
    break_duration_seconds: float,
    offset_into_break: float,
    channel_number: int,
    slot_number: int,
    seed: int
) -> Optional[dict]:
    """
    Get which commercial is playing at a specific offset into a break.

    Args:
        break_duration_seconds: Total break duration
        offset_into_break: How far into the break we are
        channel_number: Channel number
        slot_number: Slot number
        seed: Base seed

    Returns:
        Commercial dict with 'seek_offset' for where to start playback,
        or None if no commercials available. If the result has 'is_standby': True,
        it means we're in dead air / standby time.
    """
    if offset_into_break < 0 or offset_into_break >= break_duration_seconds:
        return None

    commercials = get_commercials_for_break(
        break_duration_seconds,
        channel_number,
        slot_number,
        seed
    )

    if not commercials:
        return None

    # Find which commercial we're in (linear scan - list is small)
    elapsed = 0.0
    for commercial in commercials:
        commercial_end = elapsed + commercial["duration_seconds"]

        if offset_into_break < commercial_end:
            # This is the current commercial (or standby placeholder)
            seek_offset = offset_into_break - elapsed
            remaining = commercial["duration_seconds"] - seek_offset

            result = {
                **commercial,
                "seek_offset": seek_offset,
                "remaining": remaining,
            }

            # Preserve standby flag if present
            if commercial.get("is_standby"):
                result["is_standby"] = True

            return result

        elapsed = commercial_end

    # Past all commercials but still in break - shouldn't happen, but handle gracefully
    remaining_time = break_duration_seconds - offset_into_break
    if remaining_time > 0:
        return {
            "id": -1,
            "title": "Please Stand By",
            "duration_seconds": remaining_time,
            "seek_offset": 0,
            "remaining": remaining_time,
            "file_path": "",
            "is_standby": True,
        }

    return None


def calculate_slot_breakdown(
    content_duration_seconds: float,
    slot_duration_minutes: int = 30
) -> dict:
    """
    Calculate how content fits into slots.

    Args:
        content_duration_seconds: Duration of the main content
        slot_duration_minutes: Duration of each slot

    Returns:
        Dict with:
        - slots_needed: Number of slots this content occupies
        - content_duration: Duration of the content
        - total_slot_duration: Total duration of all slots
        - commercial_time: Time that needs to be filled with commercials
    """
    slot_seconds = slot_duration_minutes * 60

    # How many slots does this content need?
    # Round up to nearest slot
    slots_needed = max(1, -(-int(content_duration_seconds) // slot_seconds))

    total_slot_duration = slots_needed * slot_seconds
    commercial_time = total_slot_duration - content_duration_seconds

    return {
        "slots_needed": slots_needed,
        "content_duration": content_duration_seconds,
        "total_slot_duration": total_slot_duration,
        "commercial_time": max(0, commercial_time),
    }
