"""Remote schedule provider — runs local ScheduleEngine with server's seed.

Overrides position loading (from server API) and position advancing
(local cache + fire-and-forget to server). All schedule computation
runs locally for zero-latency channel switching.

All schedule data (content pools, break points, commercials, positions)
is loaded from the server API at startup — no local database needed.
"""

import requests
from datetime import datetime, timedelta
from typing import Optional

from ..config import Config
from .engine import ScheduleEngine, NowPlaying


class RemoteScheduleProvider(ScheduleEngine):
    """ScheduleEngine subclass for remote mode.

    Uses the server's seed so schedule computations produce identical
    results. All data is loaded from the server API at startup via
    load_server_data() — the remote never touches a database.
    """

    def __init__(self, config: Config, server_url: str, seed: int,
                 clock_offset: float = 0.0):
        super().__init__(config)
        # Override the random seed with the server's seed
        self.seed = seed
        self._server_url = server_url
        self._clock_offset = timedelta(seconds=clock_offset)
        self._session = requests.Session()
        self._session.timeout = 5

    def load_server_data(self) -> bool:
        """Fetch all schedule data from the server and pre-populate caches.

        This replaces the old approach of copying cabletv.db. One API
        call gives us everything needed to compute schedules locally:
        channel pools, break points, commercials, and positions.

        Returns:
            True if data loaded successfully
        """
        try:
            resp = self._session.get(
                f"{self._server_url}/api/server/schedule-data",
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            print(f"  Error: Could not load schedule data from server: {e}")
            return False

        # Pre-populate channel pools (content per channel)
        channel_pools = data.get("channel_pools", {})
        for ch_str, pool in channel_pools.items():
            self._channel_pools[int(ch_str)] = pool

        # Pre-populate break points
        break_points = data.get("break_points", {})
        for cid_str, bps in break_points.items():
            self._break_point_cache[int(cid_str)] = bps

        # Pre-populate positions
        positions = data.get("positions", {})
        for key_str, pos in positions.items():
            parts = key_str.split(":", 1)
            if len(parts) == 2:
                try:
                    ch = int(parts[0])
                    gk = parts[1]
                    self._positions[(ch, gk)] = pos
                except ValueError:
                    pass
        self._positions_loaded = True

        # Pre-populate commercial cache
        commercials = data.get("commercials", [])
        from .commercials import set_commercial_pool
        set_commercial_pool(commercials)

        pool_count = sum(len(p) for p in channel_pools.values())
        bp_count = sum(len(b) for b in break_points.values())
        print(f"  Schedule data: {pool_count} content items, "
              f"{bp_count} break points, {len(commercials)} commercials, "
              f"{len(positions)} positions")
        return True

    def what_is_on(self, channel_number: int,
                   when: Optional[datetime] = None) -> Optional[NowPlaying]:
        """Get what's on, adjusted to server's clock."""
        if when is None:
            when = datetime.now() + self._clock_offset
        return super().what_is_on(channel_number, when)

    def get_upcoming(self, channel_number: int, count: int = 3):
        """Get upcoming programs, adjusted to server's clock."""
        now = datetime.now() + self._clock_offset
        current = self.what_is_on(channel_number, now)
        if not current:
            return []

        upcoming = []
        scan_time = current.entry.slot_end_time

        for _ in range(count):
            future = self.what_is_on(
                channel_number, scan_time + timedelta(seconds=0.1)
            )
            if not future:
                break
            entry = future.entry
            if entry.series_name:
                title = entry.series_name
            elif entry.artist:
                title = f"{entry.artist} - {entry.title}"
            else:
                title = entry.title
            upcoming.append((future.entry.start_time, title))
            scan_time = future.entry.slot_end_time

        return upcoming

    def get_guide_data(self, start_time=None, hours: int = 3,
                       channels=None):
        """Get guide data, adjusted to server's clock."""
        if start_time is None:
            start_time = datetime.now() + self._clock_offset
        return super().get_guide_data(start_time, hours, channels)

    def _load_positions(self) -> None:
        """Positions are pre-loaded by load_server_data(). No-op."""
        if self._positions_loaded:
            return
        # Fallback: load from server API if load_server_data wasn't called
        try:
            resp = self._session.get(f"{self._server_url}/api/server/positions")
            resp.raise_for_status()
            data = resp.json().get("positions", {})
            for key_str, pos in data.items():
                parts = key_str.split(":", 1)
                if len(parts) == 2:
                    try:
                        ch = int(parts[0])
                        gk = parts[1]
                        self._positions[(ch, gk)] = pos
                    except ValueError:
                        pass
            self._positions_loaded = True
        except (requests.RequestException, ValueError) as e:
            print(f"  Warning: Could not load positions from server: {e}")
            self._positions_loaded = True

    def advance_position(self, channel_number: int, group_key: str,
                         num_items: int, preserve_block_start: Optional[int] = None,
                         advance_by: int = 1) -> None:
        """Advance locally and notify server (fire-and-forget).

        Updates only the in-memory position cache (no DB write — the
        server is the source of truth). Block cache is intentionally
        NOT cleared to preserve schedule consistency (see base class
        advance_position comment).
        """
        # Update local position cache (same math as parent, skip DB write)
        key = (channel_number, group_key)
        current = self._positions.get(key, 0)
        new_pos = (current + advance_by) % num_items
        self._positions[key] = new_pos

        # NOTE: Block cache is NOT cleared here — same rationale as the
        # base ScheduleEngine.advance_position. Clearing causes walk-forward
        # cascade instability with variable-duration content.

        # Notify server (fire-and-forget)
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
        except requests.RequestException as e:
            print(f"  Warning: Server advance notification failed: {e}")
