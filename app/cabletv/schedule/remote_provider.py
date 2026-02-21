"""Remote schedule provider — thin API client to the server.

All schedule decisions come from the server via API calls.
The remote never computes schedules locally — the server is
the single source of truth. This eliminates all sync issues
(positions, block cache, content pools, etc.).

Aggressive caching keeps things snappy:
- Current channel: pre-fetches next 2 segments for instant transitions
- Surrounding channels: pre-fetches adjacent channels for fast surfing
- Cached entries have their seek positions recalculated from current
  time (not fetch time) to stay in sync with the server's clock
"""

import copy
import requests
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from .engine import ScheduleEntry, CommercialEntry, NowPlaying


class _CacheEntry:
    """A cached what_is_on result with timing metadata."""
    __slots__ = ("np", "fetch_time", "channel")

    def __init__(self, np: NowPlaying, channel: int):
        self.np = np
        self.fetch_time = time.monotonic()
        self.channel = channel

    def is_valid(self) -> bool:
        """Check if this entry is still usable (segment hasn't ended)."""
        delta = time.monotonic() - self.fetch_time
        if self.np.is_commercial and self.np.commercial:
            return delta < self.np.commercial.remaining_seconds
        return delta < self.np.remaining_seconds

    def adjust(self) -> NowPlaying:
        """Return a copy with seek/timing adjusted to current time."""
        delta = time.monotonic() - self.fetch_time
        if delta < 0.05:
            return self.np  # Fresh enough, no copy needed

        np = copy.copy(self.np)
        np.elapsed_seconds = self.np.elapsed_seconds + delta
        np.remaining_seconds = max(0, self.np.remaining_seconds - delta)
        np.seek_position = self.np.seek_position + delta

        if np.commercial and self.np.commercial:
            c = copy.copy(self.np.commercial)
            c.seek_position = self.np.commercial.seek_position + delta
            c.remaining_seconds = max(0, self.np.commercial.remaining_seconds - delta)
            np.commercial = c

        return np


class RemoteScheduleProvider:
    """API-based schedule provider for remote mode.

    Instead of running a local ScheduleEngine with replicated state,
    every schedule query goes to the server. Caching and pre-fetching
    keep transitions instant and channel surfing snappy.
    """

    def __init__(self, server_url: str, clock_offset: float = 0.0,
                 epoch: str = "2024-01-01T00:00:00",
                 slot_duration: int = 30,
                 channel_numbers: list[int] = None):
        self._server_url = server_url
        self._clock_offset = clock_offset
        self._session = requests.Session()
        self.epoch = epoch
        self.slot_duration = slot_duration
        self._channel_numbers = sorted(channel_numbers or [])

        # Cache: channel_number -> _CacheEntry
        self._cache: dict[int, _CacheEntry] = {}
        self._cache_lock = threading.Lock()

        # Background prefetch
        self._prefetch_queue: list[tuple[int, Optional[float]]] = []
        self._prefetch_lock = threading.Lock()
        self._prefetch_event = threading.Event()
        self._prefetch_thread = threading.Thread(
            target=self._prefetch_worker, daemon=True)
        self._prefetch_thread.start()

    # ------------------------------------------------------------------
    # Public API (called by playback engine)
    # ------------------------------------------------------------------

    def what_is_on(self, channel_number: int,
                   when: Optional[datetime] = None) -> Optional[NowPlaying]:
        """Get what's playing, using cache when possible.

        For "now" queries (when=None): checks cache first, adjusts seek
        to current time. Cache miss triggers a fetch + background
        prefetch of next segments and surrounding channels.

        For specific-time queries (when != None): always fetches fresh
        (used by seek recalculation after file loads).
        """
        # Specific time queries always go to server (no caching)
        if when is not None:
            return self._fetch_what_is_on(channel_number, when)

        # Check cache
        with self._cache_lock:
            entry = self._cache.get(channel_number)
            if entry and entry.is_valid():
                return entry.adjust()

        # Cache miss — fetch from server
        np = self._fetch_what_is_on(channel_number)
        if np:
            with self._cache_lock:
                self._cache[channel_number] = _CacheEntry(np, channel_number)
            # Prefetch next segments + surrounding channels
            self._trigger_prefetch(channel_number, np)
        return np

    def get_upcoming(self, channel_number: int,
                     count: int = 3) -> list[tuple[datetime, str]]:
        """Ask the server for upcoming programs on a channel."""
        try:
            resp = self._session.get(
                f"{self._server_url}/api/server/upcoming/{channel_number}",
                params={"count": count},
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json()
            return [
                (datetime.fromtimestamp(item["start_time"]), item["title"])
                for item in data.get("upcoming", [])
            ]
        except (requests.RequestException, ValueError) as e:
            print(f"  Warning: get_upcoming API failed for ch{channel_number}: {e}")
            return []

    def find_next_airing(self, channel_number: int, series_name: str,
                         after_time: Optional[datetime] = None) -> Optional[datetime]:
        """Ask the server when a series next airs on a channel."""
        try:
            params = {"series": series_name}
            if after_time is not None:
                params["after"] = after_time.timestamp()
            resp = self._session.get(
                f"{self._server_url}/api/server/next-airing/{channel_number}",
                params=params,
                timeout=5,
            )
            resp.raise_for_status()
            ts = resp.json().get("next_time")
            return datetime.fromtimestamp(ts) if ts else None
        except (requests.RequestException, ValueError):
            return None

    def get_channel_groups(self, channel_config):
        """Not needed — advance_info comes from what_is_on response."""
        return []

    def advance_position(self, channel_number: int, group_key: str,
                         num_items: int, preserve_block_start: Optional[int] = None,
                         advance_by: int = 1) -> None:
        """Notify server of position advance (fire-and-forget)."""
        try:
            self._session.post(
                f"{self._server_url}/api/server/advance",
                json={
                    "channel_number": channel_number,
                    "group_key": group_key,
                    "num_items": num_items,
                    "block_start_slot": preserve_block_start or 0,
                    "advance_by": advance_by,
                },
                timeout=5,
            )
        except requests.RequestException:
            pass

    def invalidate_cache(self, channel_number: int = None) -> None:
        """Clear cache for a channel (or all channels)."""
        with self._cache_lock:
            if channel_number is not None:
                self._cache.pop(channel_number, None)
            else:
                self._cache.clear()

    # ------------------------------------------------------------------
    # Internal: fetch + prefetch
    # ------------------------------------------------------------------

    def _fetch_what_is_on(self, channel_number: int,
                          when: Optional[datetime] = None) -> Optional[NowPlaying]:
        """Raw API call to server (no caching)."""
        try:
            params = {}
            if when is not None:
                params["when"] = when.timestamp()
            resp = self._session.get(
                f"{self._server_url}/api/server/what-is-on/{channel_number}",
                params=params,
                timeout=5,
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
            if not data or "entry" not in data:
                return None
            return _deserialize_now_playing(data)
        except (requests.RequestException, ValueError, KeyError) as e:
            print(f"  Warning: what_is_on API failed for ch{channel_number}: {e}")
            return None

    def _trigger_prefetch(self, channel_number: int, np: NowPlaying) -> None:
        """Queue prefetch of next segments and surrounding channels."""
        jobs = []

        # Next 2 segments on current channel (for seamless transitions)
        # Fetch what's on at slot_end_time + 0.1s, then the one after that
        slot_end_ts = np.entry.slot_end_time.timestamp() + 0.1
        jobs.append((channel_number, slot_end_ts))

        # Surrounding channels (for fast surfing)
        if self._channel_numbers:
            try:
                idx = self._channel_numbers.index(channel_number)
            except ValueError:
                idx = 0
            for offset in (-2, -1, 1, 2):
                adj_idx = (idx + offset) % len(self._channel_numbers)
                adj_ch = self._channel_numbers[adj_idx]
                if adj_ch != channel_number:
                    jobs.append((adj_ch, None))  # None = "now"

        with self._prefetch_lock:
            self._prefetch_queue = jobs  # Replace (not append) — only latest matters
        self._prefetch_event.set()

    def _prefetch_worker(self) -> None:
        """Background thread that executes prefetch jobs."""
        while True:
            self._prefetch_event.wait()
            self._prefetch_event.clear()

            with self._prefetch_lock:
                jobs = self._prefetch_queue[:]
                self._prefetch_queue.clear()

            for channel, when_ts in jobs:
                try:
                    when = datetime.fromtimestamp(when_ts) if when_ts else None
                    np = self._fetch_what_is_on(channel, when)
                    if np:
                        with self._cache_lock:
                            # For future-time fetches (next segment lookahead),
                            # store under the channel but DON'T overwrite a
                            # valid current-time entry
                            existing = self._cache.get(channel)
                            if when_ts is not None:
                                # This is a lookahead — store it keyed by the
                                # segment's content so _on_content_end can find it
                                # We store the fetched NowPlaying as-is since it
                                # represents a future time and will be valid when
                                # the transition happens
                                self._cache[channel] = _CacheEntry(np, channel)
                            elif not existing or not existing.is_valid():
                                self._cache[channel] = _CacheEntry(np, channel)
                except Exception:
                    pass  # Prefetch failures are silent


def _deserialize_now_playing(data: dict) -> NowPlaying:
    """Convert API JSON response into NowPlaying object."""
    e = data["entry"]
    entry = ScheduleEntry(
        content_id=e["content_id"],
        title=e["title"],
        content_type=e["content_type"],
        start_time=datetime.fromtimestamp(e["start_time"]),
        end_time=datetime.fromtimestamp(e["end_time"]),
        duration_seconds=e["duration_seconds"],
        file_path=e["file_path"],
        channel_number=e["channel_number"],
        slot_end_time=datetime.fromtimestamp(e["slot_end_time"]),
        artist=e.get("artist"),
        year=e.get("year"),
        series_name=e.get("series_name"),
        season=e.get("season"),
        episode=e.get("episode"),
        packed_episodes=[
            tuple(ep) for ep in e["packed_episodes"]
        ] if e.get("packed_episodes") else None,
    )

    commercial = None
    c = data.get("commercial")
    if c:
        commercial = CommercialEntry(
            content_id=c["content_id"],
            title=c["title"],
            duration_seconds=c["duration_seconds"],
            file_path=c["file_path"],
            seek_position=c["seek_position"],
            remaining_seconds=c["remaining_seconds"],
            channel_number=c["channel_number"],
            main_content_id=c["main_content_id"],
            main_content_title=c["main_content_title"],
        )

    ai = data.get("advance_info")
    advance_info = None
    if ai:
        advance_info = (ai["group_key"], ai["group_size"], ai["block_start_slot"])

    return NowPlaying(
        entry=entry,
        elapsed_seconds=data["elapsed_seconds"],
        remaining_seconds=data["remaining_seconds"],
        seek_position=data["seek_position"],
        is_commercial=data.get("is_commercial", False),
        commercial=commercial,
        is_end_bumper=data.get("is_end_bumper", False),
        pack_count=data.get("pack_count", 1),
        advance_info=advance_info,
    )
