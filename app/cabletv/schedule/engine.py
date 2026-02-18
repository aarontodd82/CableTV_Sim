"""Schedule engine for deterministic content scheduling."""

import hashlib
import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Optional

from ..config import Config, ChannelConfig
from ..db import (
    db_connection, get_ready_content, get_content_with_tags, get_break_points,
    load_all_series_positions, set_series_position,
)
from ..utils.time_utils import (
    parse_epoch, get_slot_number, get_slot_start, get_slot_end,
    slots_needed, get_position_in_slot, format_schedule_time,
    duration_to_hms
)
from .commercials import get_current_commercial, calculate_slot_breakdown


@dataclass
class TimelineSegment:
    """A segment in the content block timeline."""
    segment_type: str  # "content", "commercial", or "info_bumper"
    start_offset: float  # Seconds from block start
    duration: float  # Duration of this segment
    # For content segments:
    content_seek_start: float = 0.0  # Where to seek in content file
    # For commercial segments:
    break_index: int = 0  # Which break this is (for deterministic commercial selection)


@dataclass
class ContentGroup:
    """A group of related content (series episodes or a standalone item)."""
    group_key: str  # series_name for shows, "standalone_{content_id}" for movies
    items: list[dict]  # Episodes sorted by (season, episode, id)
    is_standalone: bool  # True for single-item groups (movies, standalone content)


# Guaranteed info bumper duration range (seconds)
INFO_BUMPER_MIN = 5.0
INFO_BUMPER_MAX = 8.0


def build_content_timeline(
    content_duration: float,
    break_points: list[float],
    total_slot_duration: float,
    seed: int = 0
) -> list[TimelineSegment]:
    """
    Build a complete timeline for a content block including commercial breaks.

    The timeline interleaves content segments with commercial breaks at the
    detected break points, plus end-of-slot padding. Commercial time is
    distributed evenly across all breaks. Every break gets an info bumper
    (carved from commercial time) showing what's on and what's coming up.
    Break points are capped and spread evenly for long content with many
    detected breaks.

    Args:
        content_duration: Duration of the main content in seconds
        break_points: List of timestamps (in content time) where breaks occur
        total_slot_duration: Total duration of the slot allocation
        seed: Seed for deterministic info bumper placement

    Returns:
        List of TimelineSegment objects covering the entire slot duration
    """
    total_commercial_time = total_slot_duration - content_duration

    # Sort break points and filter out any that are invalid
    valid_breaks = sorted([bp for bp in break_points if 0 < bp < content_duration])

    # Cap the number of mid-content breaks based on content length.
    # Roughly one break per 20 minutes, min 2, max 6.  End padding is
    # always added on top so it doesn't count toward this cap.
    max_mid_breaks = min(6, max(2, int(content_duration / 1200)))
    if len(valid_breaks) > max_mid_breaks:
        # Spread selections evenly across detected break points
        step = len(valid_breaks) / max_mid_breaks
        valid_breaks = [valid_breaks[int(i * step + step / 2)] for i in range(max_mid_breaks)]

    # Number of commercial breaks = number of break points + 1 (for end padding)
    num_breaks = len(valid_breaks) + 1

    # Distribute commercial time evenly across all breaks
    commercial_per_break = total_commercial_time / num_breaks if num_breaks > 0 else 0

    # Every break gets an info bumper (carved from commercial time).
    bumper_duration = 0.0
    if commercial_per_break >= INFO_BUMPER_MIN:
        bumper_duration = min(INFO_BUMPER_MAX, commercial_per_break / 2)
        bumper_duration = max(INFO_BUMPER_MIN, bumper_duration)
        bumper_duration = min(bumper_duration, commercial_per_break)

    segments: list[TimelineSegment] = []
    current_offset = 0.0
    content_position = 0.0

    # Build segments: content, commercial (+info_bumper), content, ...
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

        # Commercial break (shortened to make room for info bumper)
        if commercial_per_break > 0:
            comm_dur = commercial_per_break
            if bumper_duration > 0:
                comm_dur -= bumper_duration

            if comm_dur > 0:
                segments.append(TimelineSegment(
                    segment_type="commercial",
                    start_offset=current_offset,
                    duration=comm_dur,
                    break_index=i,
                ))
                current_offset += comm_dur

            # Info bumper at end of every break
            if bumper_duration > 0:
                segments.append(TimelineSegment(
                    segment_type="info_bumper",
                    start_offset=current_offset,
                    duration=bumper_duration,
                    break_index=i,
                ))
                current_offset += bumper_duration

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

    # End-of-slot commercial padding
    end_break_idx = len(valid_breaks)
    if commercial_per_break > 0:
        comm_dur = commercial_per_break
        if bumper_duration > 0:
            comm_dur -= bumper_duration

        if comm_dur > 0:
            segments.append(TimelineSegment(
                segment_type="commercial",
                start_offset=current_offset,
                duration=comm_dur,
                break_index=end_break_idx,
            ))
            current_offset += comm_dur

        if bumper_duration > 0:
            segments.append(TimelineSegment(
                segment_type="info_bumper",
                start_offset=current_offset,
                duration=bumper_duration,
                break_index=end_break_idx,
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
    artist: Optional[str] = None
    year: Optional[int] = None
    series_name: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None

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
class NowPlaying:
    """Current playback state for a channel."""
    entry: ScheduleEntry
    elapsed_seconds: float
    remaining_seconds: float
    seek_position: float  # Where to seek in the file
    is_commercial: bool = False
    commercial: Optional[CommercialEntry] = None
    is_end_bumper: bool = False

    @property
    def slot_remaining_seconds(self) -> float:
        """Seconds until the entire slot block ends (including commercials)."""
        if self.is_commercial and self.commercial:
            return self.remaining_seconds
        else:
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
        # Generate a fresh random seed each launch so the schedule varies
        self.seed = random.randint(0, 2**31 - 1)
        self._channel_pools: dict[int, list[dict]] = {}
        self._channel_groups: dict[int, list[ContentGroup]] = {}
        self._positions: dict[tuple[int, str], int] = {}  # (channel, group_key) -> position
        self._positions_loaded = False
        self._block_cache: dict[tuple[int, int], tuple[int, dict]] = {}  # (channel, target_slot) -> (start_slot, content)
        self._type_avg_durations: dict[int, tuple[float, float]] = {}  # channel -> (avg_show, avg_movie)

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

    def get_channel_groups(self, channel_config: ChannelConfig) -> list[ContentGroup]:
        """
        Group a channel's content pool by series, with episodes sorted.

        Shows with the same series_name become one group. Movies and content
        without a series_name become standalone groups (one item each).
        Cached per channel.
        """
        channel_num = channel_config.number
        if channel_num in self._channel_groups:
            return self._channel_groups[channel_num]

        pool = self.get_channel_pool(channel_config)

        series_map: dict[str, list[dict]] = {}
        standalones: list[ContentGroup] = []

        for content in pool:
            series = content.get("series_name")
            if series:
                if series not in series_map:
                    series_map[series] = []
                series_map[series].append(content)
            else:
                standalones.append(ContentGroup(
                    group_key=f"standalone_{content['id']}",
                    items=[content],
                    is_standalone=True,
                ))

        groups: list[ContentGroup] = []
        for series_name, items in series_map.items():
            sorted_items = sorted(items, key=lambda c: (
                c.get("season") or 0,
                c.get("episode") or 0,
                c["id"],
            ))
            groups.append(ContentGroup(
                group_key=series_name,
                items=sorted_items,
                is_standalone=False,
            ))

        groups.extend(standalones)
        groups.sort(key=lambda g: g.group_key)

        self._channel_groups[channel_num] = groups
        return groups

    def clear_cache(self) -> None:
        """Clear all caches (pool, group, block)."""
        self._channel_pools.clear()
        self._channel_groups.clear()
        self._block_cache.clear()
        self._type_avg_durations.clear()

    def _load_positions(self) -> None:
        """Lazy-load all series positions from the database."""
        if self._positions_loaded:
            return
        with db_connection() as conn:
            self._positions = load_all_series_positions(conn)
        self._positions_loaded = True

    def _get_position(self, channel_number: int, group: ContentGroup) -> int:
        """
        Get the current episode index for a group on a channel.

        If no position is stored, computes a deterministic initial position
        from the channel number and group key (independent of session seed,
        so it's stable across launches).
        """
        self._load_positions()
        key = (channel_number, group.group_key)
        if key in self._positions:
            return self._positions[key] % len(group.items)
        # Deterministic initial position — stable across launches
        hash_input = f"{channel_number}:{group.group_key}"
        hash_val = int(hashlib.md5(hash_input.encode()).hexdigest(), 16)
        initial = hash_val % len(group.items)
        self._positions[key] = initial
        return initial

    def advance_position(self, channel_number: int, group_key: str, num_items: int) -> None:
        """Advance the episode position for a group on a channel and persist to DB."""
        key = (channel_number, group_key)
        current = self._positions.get(key, 0)
        new_pos = (current + 1) % num_items
        self._positions[key] = new_pos
        with db_connection() as conn:
            set_series_position(conn, channel_number, group_key, new_pos)

    def _get_show_weight(self, slot_number: int,
                         channel_num: int,
                         show_groups: list[ContentGroup],
                         movie_groups: list[ContentGroup]) -> float:
        """
        Get show selection probability normalized for equal airtime,
        with time-of-day adjustment for authentic 90s cable feel.

        Because movies are ~3-4x longer than show episodes, selecting
        them at equal rates gives movies ~75% of the airtime. This
        compensates by weighting show selections higher based on the
        actual duration ratio, then applies a time-of-day modifier
        (more shows during daytime, more movies at night).
        """
        # Get average durations per type (cached per channel)
        if channel_num not in self._type_avg_durations:
            avg_s = (sum(g.items[0]["duration_seconds"] for g in show_groups)
                     / len(show_groups))
            avg_m = (sum(g.items[0]["duration_seconds"] for g in movie_groups)
                     / len(movie_groups))
            self._type_avg_durations[channel_num] = (avg_s, avg_m)
        avg_show, avg_movie = self._type_avg_durations[channel_num]

        # Base weight: compensate for duration difference, biased slightly
        # toward shows since movies' longer runtime still gives them an
        # outsized airtime footprint even after normalization.
        ratio = avg_movie / avg_show
        base = ratio / (1 + ratio) + 0.10

        # Time-of-day modifier (shifts airtime balance ±10-15%)
        slot_start = get_slot_start(slot_number, self.epoch, self.slot_duration)
        hour = slot_start.hour

        if 6 <= hour < 10:
            modifier = 0.0     # Morning: balanced
        elif 10 <= hour < 16:
            modifier = 0.05    # Daytime: more shows (syndication block)
        elif 16 <= hour < 19:
            modifier = 0.0     # Early evening: balanced
        elif 19 <= hour < 23:
            modifier = -0.10   # Primetime: more movies
        else:
            modifier = -0.15   # Late night: movie blocks

        return max(0.1, min(0.9, base + modifier))

    def _select_content_for_slot(
        self,
        channel_config: ChannelConfig,
        slot_number: int,
        exclude_ids: Optional[set[int]] = None
    ) -> Optional[dict]:
        """
        Select content for a specific slot using two-tier selection.

        First normalizes show vs movie selection for roughly equal airtime
        (compensating for movies being ~3-4x longer). Time-of-day weighting
        skews towards shows during daytime and movies at night, matching
        authentic 90s cable patterns.

        Within the chosen type, picks a group uniformly, then returns the
        episode at the group's current position for this channel.

        Args:
            channel_config: Channel configuration
            slot_number: The slot number to select for
            exclude_ids: Content IDs to exclude (for collision avoidance)

        Returns:
            Content dict or None if pool is empty
        """
        groups = self.get_channel_groups(channel_config)
        if not groups:
            return None

        channel_num = channel_config.number

        # Filter groups whose current episode is excluded
        available = groups
        if exclude_ids:
            available = [
                g for g in groups
                if g.items[self._get_position(channel_num, g)]["id"] not in exclude_ids
            ]
            if not available:
                available = groups  # Fall back to full list if all excluded

        # Deterministic group selection
        rng = random.Random(self._get_channel_seed(channel_config.number, slot_number))

        # Normalize show/movie balance with duration + time-of-day weighting.
        # Split by type, pick type first, then group within type.
        show_groups = [g for g in available if g.items[0]["content_type"] == "show"]
        movie_groups = [g for g in available if g.items[0]["content_type"] == "movie"]

        if show_groups and movie_groups:
            show_weight = self._get_show_weight(
                slot_number, channel_num, show_groups, movie_groups)
            if rng.random() < show_weight:
                group = rng.choice(show_groups)
            else:
                group = rng.choice(movie_groups)
        else:
            # Only one type on this channel — pick from all available
            group = rng.choice(available)

        # Return the episode at the current position
        pos = self._get_position(channel_num, group)
        return group.items[pos]

    # Anchor size for walk-forward alignment. All target_slots in the same
    # anchor block start their walk from the same point, guaranteeing that
    # content assignments cascade identically regardless of which slot in the
    # block is queried. Must be larger than any content's slot span (~8 max).
    _ANCHOR_SIZE = 200

    def _find_block_start(
        self,
        channel_config: ChannelConfig,
        target_slot: int,
        exclude_ids: Optional[set[int]] = None
    ) -> tuple[int, dict]:
        """
        Find the starting slot of the content block containing target_slot.

        Walks FORWARD from a fixed anchor point, assigning content to slots
        and skipping slots that are occupied by multi-slot content. The anchor
        is aligned so that nearby target_slots always walk from the same
        starting point, producing consistent content assignments.

        Results are cached per (channel, target_slot) so that position
        advances mid-block don't change what's already playing. The cache
        lives at this level (not _select_content_for_slot) because the
        exclusion set for a given (channel, slot) is deterministic, while
        intermediate walk slots can be visited with different exclusion
        contexts by different callers.

        Args:
            channel_config: Channel config
            target_slot: The slot to find content for
            exclude_ids: Content IDs to exclude (for collision avoidance)

        Returns:
            Tuple of (start_slot, content_dict)
        """
        cache_key = (channel_config.number, target_slot)
        cached = self._block_cache.get(cache_key)
        if cached is not None:
            start_slot, content = cached
            if not content:
                return start_slot, content
            if not exclude_ids or content["id"] not in exclude_ids:
                return start_slot, content

        # Align walk start to a fixed anchor so nearby slots cascade identically.
        # Overlap by 10 slots to catch multi-slot content crossing the anchor boundary.
        anchor = (target_slot // self._ANCHOR_SIZE) * self._ANCHOR_SIZE
        search_start = max(0, anchor - 10)

        current_slot = search_start
        while current_slot <= target_slot:
            content = self._select_content_for_slot(
                channel_config, current_slot, exclude_ids=exclude_ids)
            if not content:
                current_slot += 1
                continue

            num_slots = slots_needed(content["duration_seconds"], self.slot_duration)
            end_slot = current_slot + num_slots  # exclusive

            if end_slot > target_slot:
                # This content spans to cover target_slot
                self._block_cache[cache_key] = (current_slot, content)
                return current_slot, content

            # Content ends before target_slot, skip to next available slot
            current_slot = end_slot

        # Fallback (shouldn't reach here)
        content = self._select_content_for_slot(
            channel_config, target_slot, exclude_ids=exclude_ids)
        result = (target_slot, content if content else {})
        self._block_cache[cache_key] = result
        return result

    def _get_content_break_points(self, content_id: int) -> list[float]:
        """Get break points for content from the database."""
        with db_connection() as conn:
            break_rows = get_break_points(conn, content_id)
            return [row["timestamp_seconds"] for row in break_rows]

    def _get_exclusions(self, channel_number: int, target_slot: int) -> set[int]:
        """
        Get content IDs to exclude for collision avoidance.

        Lower-numbered channels get priority. Each channel excludes
        content playing on all channels below it, with those lower
        channels also respecting their own exclusions (iterative).
        """
        selections: dict[int, int] = {}  # channel_number -> content_id

        for ch in sorted(self.config.channels, key=lambda c: c.number):
            if ch.number >= channel_number:
                break
            # This lower channel excludes content from channels below it
            ch_exclude = set(selections.values()) or None
            _, content = self._find_block_start(ch, target_slot, exclude_ids=ch_exclude)
            if content and content.get("id"):
                selections[ch.number] = content["id"]

        return set(selections.values())

    def _what_is_on_continuous(
        self,
        channel_config: ChannelConfig,
        when: datetime
    ) -> Optional[NowPlaying]:
        """
        Continuous playback for channels with no commercials (e.g., music).

        Creates a deterministic looping playlist from the channel's content pool.
        Same time = same video at the same position, always.
        """
        pool = self.get_channel_pool(channel_config)
        if not pool:
            return None

        # Sort by ID for deterministic base ordering, then shuffle with channel seed
        sorted_pool = sorted(pool, key=lambda c: c["id"])
        rng = random.Random(self.seed + channel_config.number)
        rng.shuffle(sorted_pool)

        # Calculate total playlist duration
        total_duration = sum(c["duration_seconds"] for c in sorted_pool)
        if total_duration <= 0:
            return None

        # Calculate position in the looping playlist
        elapsed_from_epoch = (when - self.epoch).total_seconds()
        position = elapsed_from_epoch % total_duration

        # Walk through playlist to find current item
        accumulated = 0.0
        for content in sorted_pool:
            item_duration = content["duration_seconds"]
            if accumulated + item_duration > position:
                # This is the current item
                offset_in_item = position - accumulated
                remaining = item_duration - offset_in_item

                file_path = content.get("normalized_path") or content.get("original_path", "")
                item_start_time = when - timedelta(seconds=offset_in_item)
                item_end_time = item_start_time + timedelta(seconds=item_duration)

                entry = ScheduleEntry(
                    content_id=content["id"],
                    title=content["title"],
                    content_type=content["content_type"],
                    start_time=item_start_time,
                    end_time=item_end_time,
                    duration_seconds=item_duration,
                    file_path=file_path,
                    channel_number=channel_config.number,
                    slot_end_time=item_end_time,  # No commercial padding
                    artist=content.get("artist"),
                    year=content.get("year"),
                    series_name=content.get("series_name"),
                    season=content.get("season"),
                    episode=content.get("episode"),
                )

                return NowPlaying(
                    entry=entry,
                    elapsed_seconds=offset_in_item,
                    remaining_seconds=remaining,
                    seek_position=offset_in_item,
                    is_commercial=False,
                    commercial=None,
                )
            accumulated += item_duration

        return None

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

        # Continuous mode for channels with no commercials (e.g., music)
        if channel_config.commercial_ratio == 0.0:
            return self._what_is_on_continuous(channel_config, when)

        # Find current slot
        current_slot = get_slot_number(when, self.epoch, self.slot_duration)

        # Get exclusions from lower-priority channels to avoid collisions
        exclude_ids = self._get_exclusions(channel_number, current_slot)

        # Find the block start and content
        block_start_slot, content = self._find_block_start(
            channel_config, current_slot, exclude_ids=exclude_ids or None)

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
            series_name=content.get("series_name"),
            season=content.get("season"),
            episode=content.get("episode"),
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
        elif current_segment.segment_type == "info_bumper":
            # Info bumper — black screen with mini-guide
            remaining_in_segment = current_segment.duration - offset_in_segment
            # Detect if this is the end-of-slot bumper (after all content has played)
            valid_break_count = len([bp for bp in break_points if 0 < bp < content_duration])
            is_end = current_segment.break_index >= valid_break_count
            return NowPlaying(
                entry=entry,
                elapsed_seconds=elapsed,
                remaining_seconds=remaining_in_segment,
                seek_position=0,
                is_commercial=True,
                commercial=None,  # Triggers info bumper display in playback engine
                is_end_bumper=is_end,
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

    def find_next_airing(
        self,
        channel_number: int,
        series_name: str,
        after_time: Optional[datetime] = None
    ) -> Optional[datetime]:
        """
        Find the next time a series airs on a channel.

        Walks forward through future slots to find the next content block
        featuring the given series. Used for promotional bumpers.

        Args:
            channel_number: Channel to search
            series_name: Series name to look for
            after_time: Start searching after this time (default: now)

        Returns:
            Start time of the next airing, or None if not found within 14 days
        """
        channel_config = self.config.channel_map.get(channel_number)
        if not channel_config:
            return None

        if after_time is None:
            after_time = datetime.now()

        start_slot = get_slot_number(after_time, self.epoch, self.slot_duration) + 1
        current_slot = start_slot

        # Search up to 672 slots (~14 days of 30-min slots)
        while current_slot < start_slot + 672:
            block_start, content = self._find_block_start(
                channel_config, current_slot)
            if not content:
                current_slot += 1
                continue

            if content.get("series_name") == series_name:
                return get_slot_start(block_start, self.epoch, self.slot_duration)

            # Skip forward by content's slot span to avoid checking every slot
            num = slots_needed(content["duration_seconds"], self.slot_duration)
            current_slot = max(current_slot + 1, block_start + num)

        return None

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

        # Align walker to the slot boundary at or before start_time so that
        # every check point lands on a :00/:30 boundary.  This prevents
        # non-slot-aligned start_times (e.g. 10:50 from 10-minute windows)
        # from producing odd clipped times in the guide grid.
        first_slot = get_slot_number(start_time, self.epoch, self.slot_duration)
        walker_start = get_slot_start(first_slot, self.epoch, self.slot_duration)

        for channel_num in channels:
            channel_config = self.config.channel_map.get(channel_num)
            if not channel_config:
                continue

            entries = []
            current_time = walker_start

            while current_time < end_time:
                now_playing = self.what_is_on(channel_num, current_time)
                if now_playing:
                    if not entries or entries[-1].content_id != now_playing.entry.content_id:
                        # Content changed — clip previous entry's end time
                        if entries and entries[-1].slot_end_time > current_time:
                            entries[-1].slot_end_time = current_time
                        # Clip start_time: when exclusions change mid-block, the
                        # block-start from what_is_on may precede the actual
                        # switch point.  Use the time we discovered the change.
                        entry = now_playing.entry
                        if entry.start_time < current_time:
                            entry = replace(entry, start_time=current_time)
                        entries.append(entry)
                else:
                    # No content — clip previous entry if needed
                    if entries and entries[-1].slot_end_time > current_time:
                        entries[-1].slot_end_time = current_time
                # Walk slot-by-slot to catch exclusion changes at boundaries
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

    def get_upcoming(
        self,
        channel_number: int,
        count: int = 3
    ) -> list[tuple[datetime, str]]:
        """
        Get upcoming programs on a channel (for info bumper display).

        Walks forward from the current content's end to find
        the next few programs.

        Args:
            channel_number: Channel number
            count: Number of upcoming entries to return

        Returns:
            List of (start_time, title) tuples
        """
        now = datetime.now()
        current = self.what_is_on(channel_number, now)
        if not current:
            return []

        upcoming: list[tuple[datetime, str]] = []
        # Start scanning from end of current content (slot_end_time for slot-based,
        # end_time for continuous — but slot_end_time == end_time for continuous)
        scan_time = current.entry.slot_end_time

        for _ in range(count):
            future = self.what_is_on(channel_number, scan_time + timedelta(seconds=0.1))
            if not future:
                break
            # Build display title — include artist for music content
            title = future.entry.title
            if future.entry.artist:
                title = f"{future.entry.artist} - {title}"
            upcoming.append((future.entry.start_time, title))
            scan_time = future.entry.slot_end_time

        return upcoming
