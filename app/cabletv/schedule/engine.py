"""Schedule engine for deterministic content scheduling."""

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from ..config import Config, ChannelConfig
from ..db import db_connection, get_ready_content, get_content_with_tags, get_break_points
from ..utils.time_utils import (
    parse_epoch, get_slot_number, get_slot_start, get_slot_end,
    slots_needed, get_position_in_slot, format_schedule_time,
    duration_to_hms
)
from .commercials import get_current_commercial, calculate_slot_breakdown


@dataclass
class TimelineSegment:
    """A segment in the content block timeline."""
    segment_type: str  # "content", "commercial", or "up_next"
    start_offset: float  # Seconds from block start
    duration: float  # Duration of this segment
    # For content segments:
    content_seek_start: float = 0.0  # Where to seek in content file
    # For commercial segments:
    break_index: int = 0  # Which break this is (for deterministic commercial selection)


# Duration of "Coming Up Next" bumper in seconds
UP_NEXT_DURATION = 8.0
# Max number of up_next bumpers per slot
UP_NEXT_PER_SLOT = 3


def build_content_timeline(
    content_duration: float,
    break_points: list[float],
    total_slot_duration: float,
    seed: int = 0
) -> list[TimelineSegment]:
    """
    Build a complete timeline for a content block including commercial breaks
    and "Coming Up Next" bumpers.

    The timeline interleaves content segments with commercial breaks at the
    detected break points, plus end-of-slot padding. Up to UP_NEXT_PER_SLOT
    breaks will include an "up_next" bumper (max 1 per break).

    Args:
        content_duration: Duration of the main content in seconds
        break_points: List of timestamps (in content time) where breaks occur
        total_slot_duration: Total duration of the slot allocation
        seed: Seed for deterministic up_next placement

    Returns:
        List of TimelineSegment objects covering the entire slot duration
    """
    total_commercial_time = total_slot_duration - content_duration

    # Sort break points and filter out any that are invalid
    valid_breaks = sorted([bp for bp in break_points if 0 < bp < content_duration])

    # Number of commercial breaks = number of break points + 1 (for end padding)
    num_breaks = len(valid_breaks) + 1

    # Only add up_next bumpers if there's enough commercial time
    # (need at least UP_NEXT_DURATION per bumper plus some commercial time)
    breaks_with_up_next: set[int] = set()
    if total_commercial_time > UP_NEXT_DURATION:
        # Determine which breaks get "up_next" bumpers (deterministic, max 1 per break)
        # Select up to UP_NEXT_PER_SLOT breaks, but not end padding (last break)
        rng = random.Random(seed)
        available_breaks = list(range(num_breaks - 1)) if num_breaks > 1 else []  # Exclude end padding
        rng.shuffle(available_breaks)

        # Calculate how many up_next bumpers we can fit
        max_up_next = min(UP_NEXT_PER_SLOT, len(available_breaks))
        # Ensure we leave at least some commercial time after up_next bumpers
        while max_up_next > 0:
            up_next_time_needed = max_up_next * UP_NEXT_DURATION
            if total_commercial_time - up_next_time_needed >= num_breaks:  # At least 1 sec per break
                break
            max_up_next -= 1

        breaks_with_up_next = set(available_breaks[:max_up_next])

    # Calculate time: subtract up_next time from total commercial time
    up_next_total_time = len(breaks_with_up_next) * UP_NEXT_DURATION
    adjusted_commercial_time = max(0, total_commercial_time - up_next_total_time)

    # Distribute remaining commercial time evenly across all breaks
    commercial_per_break = adjusted_commercial_time / num_breaks if num_breaks > 0 else 0

    segments: list[TimelineSegment] = []
    current_offset = 0.0
    content_position = 0.0

    # Build segments: content, commercial (+ up_next), content, ...
    for i, break_point in enumerate(valid_breaks):
        # Content segment up to this break point
        content_segment_duration = break_point - content_position
        if content_segment_duration > 0:
            segments.append(TimelineSegment(
                segment_type="content",
                start_offset=current_offset,
                duration=content_segment_duration,
                content_seek_start=content_position,
            ))
            current_offset += content_segment_duration

        # Commercial break
        if commercial_per_break > 0:
            segments.append(TimelineSegment(
                segment_type="commercial",
                start_offset=current_offset,
                duration=commercial_per_break,
                break_index=i,
            ))
            current_offset += commercial_per_break

        # "Coming Up Next" bumper (at end of break, before content resumes)
        if i in breaks_with_up_next:
            segments.append(TimelineSegment(
                segment_type="up_next",
                start_offset=current_offset,
                duration=UP_NEXT_DURATION,
                break_index=i,
            ))
            current_offset += UP_NEXT_DURATION

        content_position = break_point

    # Final content segment (from last break point to end of content)
    final_content_duration = content_duration - content_position
    if final_content_duration > 0:
        segments.append(TimelineSegment(
            segment_type="content",
            start_offset=current_offset,
            duration=final_content_duration,
            content_seek_start=content_position,
        ))
        current_offset += final_content_duration

    # End-of-slot commercial padding (no up_next for end padding)
    if commercial_per_break > 0:
        segments.append(TimelineSegment(
            segment_type="commercial",
            start_offset=current_offset,
            duration=commercial_per_break,
            break_index=len(valid_breaks),
        ))

    return segments


def find_current_segment(
    segments: list[TimelineSegment],
    elapsed: float
) -> tuple[Optional[TimelineSegment], float]:
    """
    Find which segment is active at a given elapsed time.

    Args:
        segments: List of timeline segments
        elapsed: Seconds elapsed since block start

    Returns:
        Tuple of (segment, offset_into_segment) or (None, 0) if past end
    """
    for segment in segments:
        segment_end = segment.start_offset + segment.duration
        if elapsed < segment_end:
            offset_into_segment = elapsed - segment.start_offset
            return segment, offset_into_segment

    return None, 0.0


@dataclass
class ScheduleEntry:
    """A single scheduled item."""
    content_id: int
    title: str
    content_type: str
    start_time: datetime
    end_time: datetime
    duration_seconds: float
    file_path: str
    channel_number: int
    slot_end_time: datetime  # When the slot(s) actually end (for commercial padding)

    @property
    def is_playing(self) -> bool:
        """Check if this entry is currently playing."""
        now = datetime.now()
        return self.start_time <= now < self.end_time

    @property
    def commercial_padding_seconds(self) -> float:
        """How much commercial time follows this content."""
        return (self.slot_end_time - self.end_time).total_seconds()


@dataclass
class CommercialEntry:
    """A commercial playing during padding time."""
    content_id: int
    title: str
    duration_seconds: float
    file_path: str
    seek_position: float
    remaining_seconds: float
    channel_number: int
    # Reference to the main content this commercial follows
    main_content_id: int
    main_content_title: str


@dataclass
class UpNextEntry:
    """A 'Coming Up Next' bumper."""
    next_title: str
    duration_seconds: float
    remaining_seconds: float
    channel_number: int


@dataclass
class NowPlaying:
    """Current playback state for a channel."""
    entry: ScheduleEntry
    elapsed_seconds: float
    remaining_seconds: float
    seek_position: float  # Where to seek in the file
    is_commercial: bool = False
    commercial: Optional[CommercialEntry] = None
    is_up_next: bool = False
    up_next: Optional[UpNextEntry] = None

    @property
    def slot_remaining_seconds(self) -> float:
        """Seconds until the entire slot block ends (including commercials)."""
        if self.is_commercial and self.commercial:
            # We're in commercial, calculate from slot end
            return self.remaining_seconds  # Already calculated for commercial
        else:
            # Main content - add commercial padding time
            return self.remaining_seconds + self.entry.commercial_padding_seconds


class ScheduleEngine:
    """
    Deterministic schedule engine.

    Uses a seeded random number generator to create reproducible schedules.
    The same seed + epoch + content library = same schedule.
    """

    def __init__(self, config: Config):
        self.config = config
        self.epoch = parse_epoch(config.schedule.epoch)
        self.slot_duration = config.schedule.slot_duration
        self.seed = config.schedule.seed
        self._channel_pools: dict[int, list[dict]] = {}

    def _get_channel_seed(self, channel_number: int, slot_number: int) -> int:
        """Get deterministic seed for a specific channel and slot."""
        return self.seed + (channel_number * 1000000) + slot_number

    def get_channel_pool(self, channel_config: ChannelConfig) -> list[dict]:
        """
        Get the content pool for a channel.

        Caches results for performance.
        """
        channel_num = channel_config.number

        if channel_num in self._channel_pools:
            return self._channel_pools[channel_num]

        with db_connection() as conn:
            # Get content matching channel tags
            if channel_config.tags:
                content_list = get_content_with_tags(conn, channel_config.tags)
            else:
                content_list = get_ready_content(conn)

            # Filter by content type
            pool = []
            for content in content_list:
                if content["content_type"] in channel_config.content_types:
                    pool.append(dict(content))

        self._channel_pools[channel_num] = pool
        return pool

    def clear_cache(self) -> None:
        """Clear the channel pool cache."""
        self._channel_pools.clear()

    def _select_content_for_slot(
        self,
        channel_config: ChannelConfig,
        slot_number: int,
        exclude_ids: Optional[set[int]] = None
    ) -> Optional[dict]:
        """
        Select content for a specific slot deterministically.

        Args:
            channel_config: Channel configuration
            slot_number: The slot number to select for
            exclude_ids: Content IDs to exclude (for collision avoidance)

        Returns:
            Content dict or None if pool is empty
        """
        pool = self.get_channel_pool(channel_config)
        if not pool:
            return None

        # Filter out excluded IDs
        available = pool
        if exclude_ids:
            available = [c for c in pool if c["id"] not in exclude_ids]
            if not available:
                available = pool  # Fall back to full pool if all excluded

        # Use deterministic random selection
        rng = random.Random(self._get_channel_seed(channel_config.number, slot_number))
        return rng.choice(available)

    def _find_block_start(
        self,
        channel_config: ChannelConfig,
        target_slot: int
    ) -> tuple[int, dict]:
        """
        Find the starting slot of the content block containing target_slot.

        The key insight: we need to walk BACKWARDS and check if any earlier
        content's duration extends into the target slot. Each slot gets its
        own random selection, but content that starts at slot N and needs
        M slots will occupy slots N through N+M-1.

        Returns:
            Tuple of (start_slot, content_dict)
        """
        # Search backwards to find content that started earlier but extends into target_slot
        # We need to check: "does content at slot X occupy target_slot?"
        # Content at slot X occupies slots X through X + slots_needed - 1

        search_start = max(0, target_slot - 100)  # Look back up to 100 slots

        for check_slot in range(target_slot, search_start - 1, -1):
            content = self._select_content_for_slot(channel_config, check_slot)
            if not content:
                continue

            # How many slots does this content need?
            num_slots = slots_needed(content["duration_seconds"], self.slot_duration)

            # Does this content extend to cover target_slot?
            # Content at check_slot occupies: check_slot, check_slot+1, ..., check_slot+num_slots-1
            if check_slot + num_slots > target_slot:
                # Yes! This content started at check_slot and extends past target_slot
                return check_slot, content

        # If nothing found, return whatever is selected at target_slot
        content = self._select_content_for_slot(channel_config, target_slot)
        return target_slot, content if content else {}

    def _get_content_break_points(self, content_id: int) -> list[float]:
        """Get break points for content from the database."""
        with db_connection() as conn:
            break_rows = get_break_points(conn, content_id)
            return [row["timestamp_seconds"] for row in break_rows]

    def what_is_on(
        self,
        channel_number: int,
        when: Optional[datetime] = None
    ) -> Optional[NowPlaying]:
        """
        Get what's currently playing on a channel.

        This builds a complete timeline for the content block that includes:
        - Content segments (actual show/movie playback)
        - Commercial breaks at detected break points
        - End-of-slot commercial padding

        Everything is deterministic based on the master clock. Tuning to a
        channel at any time calculates exactly what should be playing and
        the seek position within that content or commercial.

        Args:
            channel_number: Channel number
            when: Time to check (default: now)

        Returns:
            NowPlaying with current playback state
        """
        if when is None:
            when = datetime.now()

        # Get channel config
        channel_config = self.config.channel_map.get(channel_number)
        if not channel_config:
            return None

        # Find current slot
        current_slot = get_slot_number(when, self.epoch, self.slot_duration)

        # Find the block start and content
        block_start_slot, content = self._find_block_start(channel_config, current_slot)

        if not content:
            return None

        # Calculate timing
        block_start_time = get_slot_start(block_start_slot, self.epoch, self.slot_duration)
        content_duration = content["duration_seconds"]

        # Calculate slot allocation (content rounds up to fill slots)
        slot_breakdown = calculate_slot_breakdown(content_duration, self.slot_duration)
        total_slot_duration = slot_breakdown["total_slot_duration"]
        slot_end_time = block_start_time + timedelta(seconds=total_slot_duration)

        # Get break points for this content
        break_points = self._get_content_break_points(content["id"])

        # Build the complete timeline for this content block
        timeline_seed = self._get_channel_seed(channel_number, block_start_slot)
        timeline = build_content_timeline(
            content_duration=content_duration,
            break_points=break_points,
            total_slot_duration=total_slot_duration,
            seed=timeline_seed
        )

        # Calculate elapsed time since content block started
        elapsed = (when - block_start_time).total_seconds()

        # Find which segment we're in
        current_segment, offset_in_segment = find_current_segment(timeline, elapsed)

        if current_segment is None:
            # Past the end of the slot - shouldn't happen but handle gracefully
            return None

        # Get file path for main content
        file_path = content.get("normalized_path") or content.get("original_path", "")

        # Calculate when the main content would naturally end (without commercials)
        content_end_time = block_start_time + timedelta(seconds=content_duration)

        entry = ScheduleEntry(
            content_id=content["id"],
            title=content["title"],
            content_type=content["content_type"],
            start_time=block_start_time,
            end_time=content_end_time,
            duration_seconds=content_duration,
            file_path=file_path,
            channel_number=channel_number,
            slot_end_time=slot_end_time,
        )

        if current_segment.segment_type == "content":
            # We're in a content segment
            seek_position = current_segment.content_seek_start + offset_in_segment
            remaining_in_segment = current_segment.duration - offset_in_segment

            return NowPlaying(
                entry=entry,
                elapsed_seconds=elapsed,
                remaining_seconds=remaining_in_segment,
                seek_position=seek_position,
                is_commercial=False,
                commercial=None,
            )
        elif current_segment.segment_type == "up_next":
            # We're showing a "Coming Up Next" bumper
            remaining_in_segment = current_segment.duration - offset_in_segment

            # Figure out what's coming next on this channel
            # "Next" is the content that starts after this slot block ends
            next_slot = block_start_slot + slot_breakdown["slots_needed"]
            next_content = self._select_content_for_slot(channel_config, next_slot)

            next_title = next_content["title"] if next_content else "More Programming"

            up_next_entry = UpNextEntry(
                next_title=next_title,
                duration_seconds=current_segment.duration,
                remaining_seconds=remaining_in_segment,
                channel_number=channel_number,
            )

            return NowPlaying(
                entry=entry,
                elapsed_seconds=elapsed,
                remaining_seconds=remaining_in_segment,
                seek_position=0,
                is_commercial=False,
                commercial=None,
                is_up_next=True,
                up_next=up_next_entry,
            )
        else:
            # We're in a commercial break
            break_duration = current_segment.duration
            offset_into_break = offset_in_segment

            # Get the specific commercial playing at this offset in this break
            # Use break_index for deterministic selection
            commercial_info = get_current_commercial(
                break_duration_seconds=break_duration,
                offset_into_break=offset_into_break,
                channel_number=channel_number,
                slot_number=block_start_slot + current_segment.break_index,  # Unique seed per break
                seed=self.seed
            )

            remaining_in_segment = current_segment.duration - offset_in_segment

            if commercial_info:
                # Check if this is a standby placeholder (dead air filler)
                if commercial_info.get("is_standby"):
                    # Standby mode - no actual commercial to play
                    return NowPlaying(
                        entry=entry,
                        elapsed_seconds=elapsed,
                        remaining_seconds=commercial_info["remaining"],
                        seek_position=0,
                        is_commercial=True,
                        commercial=None,  # Triggers standby display in playback engine
                    )

                commercial_file_path = commercial_info.get("normalized_path") or commercial_info.get("original_path", "")

                commercial_entry = CommercialEntry(
                    content_id=commercial_info["id"],
                    title=commercial_info["title"],
                    duration_seconds=commercial_info["duration_seconds"],
                    file_path=commercial_file_path,
                    seek_position=commercial_info["seek_offset"],
                    remaining_seconds=commercial_info["remaining"],
                    channel_number=channel_number,
                    main_content_id=content["id"],
                    main_content_title=content["title"],
                )

                return NowPlaying(
                    entry=entry,
                    elapsed_seconds=elapsed,
                    remaining_seconds=commercial_info["remaining"],
                    seek_position=commercial_info["seek_offset"],
                    is_commercial=True,
                    commercial=commercial_entry,
                )
            else:
                # No commercials available - return with no commercial to play
                return NowPlaying(
                    entry=entry,
                    elapsed_seconds=elapsed,
                    remaining_seconds=remaining_in_segment,
                    seek_position=0,
                    is_commercial=True,
                    commercial=None,
                )

    def get_guide_data(
        self,
        start_time: Optional[datetime] = None,
        hours: int = 3,
        channels: Optional[list[int]] = None
    ) -> dict[int, list[ScheduleEntry]]:
        """
        Get TV guide data for multiple channels.

        Args:
            start_time: Start of guide window (default: now)
            hours: Hours to include in guide
            channels: Channel numbers to include (default: all)

        Returns:
            Dict mapping channel number to list of ScheduleEntry
        """
        if start_time is None:
            start_time = datetime.now()

        if channels is None:
            channels = [ch.number for ch in self.config.channels]

        end_time = start_time + timedelta(hours=hours)
        guide: dict[int, list[ScheduleEntry]] = {}

        for channel_num in channels:
            channel_config = self.config.channel_map.get(channel_num)
            if not channel_config:
                continue

            entries = []
            current_time = start_time

            while current_time < end_time:
                now_playing = self.what_is_on(channel_num, current_time)
                if now_playing:
                    # Only add entry if not already added (avoid duplicates)
                    if not entries or entries[-1].content_id != now_playing.entry.content_id:
                        entries.append(now_playing.entry)
                    # Move to the end of this slot block (includes commercial time)
                    current_time = now_playing.entry.slot_end_time
                else:
                    # No content, skip a slot
                    current_time += timedelta(minutes=self.slot_duration)

            guide[channel_num] = entries

        return guide

    def get_schedule_display(
        self,
        channel_number: Optional[int] = None,
        when: Optional[datetime] = None,
        hours: int = 3
    ) -> str:
        """
        Get formatted schedule display.

        Args:
            channel_number: Specific channel or None for all
            when: Start time (default: now)
            hours: Hours to display

        Returns:
            Formatted schedule string
        """
        if when is None:
            when = datetime.now()

        channels = [channel_number] if channel_number else None
        guide = self.get_guide_data(when, hours, channels)

        lines = []
        lines.append(f"Schedule for {when.strftime('%a %b %d, %Y')}")
        lines.append("=" * 60)

        for channel_num in sorted(guide.keys()):
            channel_config = self.config.channel_map.get(channel_num)
            channel_name = channel_config.name if channel_config else f"Channel {channel_num}"

            lines.append(f"\nChannel {channel_num}: {channel_name}")
            lines.append("-" * 40)

            for entry in guide[channel_num]:
                start_str = format_schedule_time(entry.start_time)
                duration_str = duration_to_hms(entry.duration_seconds)
                slot_duration_str = duration_to_hms((entry.slot_end_time - entry.start_time).total_seconds())

                # Show both content duration and slot duration if different
                if entry.commercial_padding_seconds > 0:
                    lines.append(f"  {start_str}  {entry.title} ({duration_str} + commercials)")
                else:
                    lines.append(f"  {start_str}  {entry.title} ({duration_str})")

        return "\n".join(lines)

    def check_collisions(
        self,
        when: Optional[datetime] = None
    ) -> list[tuple[int, int, dict]]:
        """
        Check for same content playing on multiple channels.

        Returns list of (channel1, channel2, content) tuples.
        """
        if when is None:
            when = datetime.now()

        collisions = []
        channel_content: dict[int, dict] = {}

        for channel in self.config.channels:
            now_playing = self.what_is_on(channel.number, when)
            if now_playing and not now_playing.is_commercial:
                content_id = now_playing.entry.content_id

                # Check if this content is on another channel
                for other_num, other_content in channel_content.items():
                    if other_content["id"] == content_id:
                        collisions.append((other_num, channel.number, other_content))

                channel_content[channel.number] = {"id": content_id, "title": now_playing.entry.title}

        return collisions
