"""Time utilities for epoch math and slot calculations."""

from datetime import datetime, timedelta
from typing import Optional


def parse_epoch(epoch_str: str) -> datetime:
    """Parse epoch string to datetime."""
    # Try multiple formats
    formats = [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(epoch_str, fmt)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse epoch string: {epoch_str}")


def get_slot_number(when: datetime, epoch: datetime, slot_duration_minutes: int) -> int:
    """
    Get the slot number for a given time.

    Slots are numbered from 0 starting at the epoch.
    Each slot is slot_duration_minutes long.

    Args:
        when: The time to get the slot for
        epoch: The reference epoch
        slot_duration_minutes: Duration of each slot in minutes

    Returns:
        Slot number (can be negative if before epoch)
    """
    delta = when - epoch
    total_minutes = delta.total_seconds() / 60
    return int(total_minutes // slot_duration_minutes)


def get_slot_start(slot_number: int, epoch: datetime, slot_duration_minutes: int) -> datetime:
    """
    Get the start time of a slot.

    Args:
        slot_number: The slot number
        epoch: The reference epoch
        slot_duration_minutes: Duration of each slot in minutes

    Returns:
        Start time of the slot
    """
    return epoch + timedelta(minutes=slot_number * slot_duration_minutes)


def get_slot_end(slot_number: int, epoch: datetime, slot_duration_minutes: int) -> datetime:
    """Get the end time of a slot."""
    return get_slot_start(slot_number + 1, epoch, slot_duration_minutes)


def get_position_in_slot(when: datetime, epoch: datetime, slot_duration_minutes: int) -> float:
    """
    Get how far into the current slot we are.

    Args:
        when: Current time
        epoch: Reference epoch
        slot_duration_minutes: Duration of each slot

    Returns:
        Seconds into the current slot
    """
    slot_num = get_slot_number(when, epoch, slot_duration_minutes)
    slot_start = get_slot_start(slot_num, epoch, slot_duration_minutes)
    return (when - slot_start).total_seconds()


def slots_needed(duration_seconds: float, slot_duration_minutes: int) -> int:
    """
    Calculate how many slots are needed for content of a given duration.

    Content is rounded up to the nearest slot.

    Args:
        duration_seconds: Content duration in seconds
        slot_duration_minutes: Duration of each slot in minutes

    Returns:
        Number of slots needed
    """
    slot_seconds = slot_duration_minutes * 60
    return max(1, -(-int(duration_seconds) // slot_seconds))  # Ceiling division


def duration_to_hms(seconds: float) -> str:
    """Convert duration in seconds to H:MM:SS format."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def hms_to_seconds(hms: str) -> float:
    """Convert H:MM:SS or MM:SS format to seconds."""
    parts = hms.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    elif len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    else:
        return float(parts[0])


def get_day_slot(when: datetime, epoch: datetime, slot_duration_minutes: int) -> int:
    """
    Get the slot number within the current day (0-based).

    Useful for daily schedule patterns.

    Args:
        when: Current time
        epoch: Reference epoch
        slot_duration_minutes: Duration of each slot

    Returns:
        Slot number within the day (0 = first slot of the day)
    """
    # Get start of the day
    day_start = when.replace(hour=0, minute=0, second=0, microsecond=0)
    delta = when - day_start
    total_minutes = delta.total_seconds() / 60
    return int(total_minutes // slot_duration_minutes)


def get_slots_per_day(slot_duration_minutes: int) -> int:
    """Get number of slots in a day."""
    return (24 * 60) // slot_duration_minutes


def now() -> datetime:
    """Get current time (wrapper for easier testing)."""
    return datetime.now()


def format_schedule_time(dt: datetime) -> str:
    """Format datetime for schedule display."""
    return dt.strftime("%I:%M %p")


def format_date(dt: datetime) -> str:
    """Format datetime as date for display."""
    return dt.strftime("%a %b %d")


def get_seconds_until_slot_end(
    when: datetime,
    epoch: datetime,
    slot_duration_minutes: int
) -> float:
    """Get seconds until the end of the current slot."""
    slot_num = get_slot_number(when, epoch, slot_duration_minutes)
    slot_end = get_slot_end(slot_num, epoch, slot_duration_minutes)
    return (slot_end - when).total_seconds()


def get_block_info(
    when: datetime,
    epoch: datetime,
    slot_duration_minutes: int,
    content_duration_seconds: float,
    start_slot: int
) -> dict:
    """
    Get information about where we are in a content block.

    Args:
        when: Current time
        epoch: Reference epoch
        slot_duration_minutes: Duration of each slot
        content_duration_seconds: Duration of the content
        start_slot: Slot number where content started

    Returns:
        Dict with position info:
        - elapsed: seconds elapsed since content start
        - remaining: seconds remaining in content
        - percent: percentage complete
    """
    content_start = get_slot_start(start_slot, epoch, slot_duration_minutes)
    elapsed = (when - content_start).total_seconds()
    elapsed = max(0, min(elapsed, content_duration_seconds))
    remaining = content_duration_seconds - elapsed
    percent = (elapsed / content_duration_seconds * 100) if content_duration_seconds > 0 else 100

    return {
        "elapsed": elapsed,
        "remaining": remaining,
        "percent": percent,
    }
