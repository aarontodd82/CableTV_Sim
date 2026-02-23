"""Remote schedule provider — thin API client to the server.

All schedule decisions come from the server via API calls.
The remote never computes schedules locally — the server is
the single source of truth. This eliminates all sync issues
(positions, block cache, content pools, etc.).
"""

import requests
from datetime import datetime
from typing import Optional

from .engine import ScheduleEntry, CommercialEntry, NowPlaying


class RemoteScheduleProvider:
    """API-based schedule provider for remote mode.

    Every schedule query goes straight to the server. The playback
    engine's post-load seek recalculation (a second what_is_on call
    after mpv loads the file) compensates for any API latency.
    """

    def __init__(self, server_url: str, clock_offset: float = 0.0,
                 epoch: str = "2024-01-01T00:00:00",
                 slot_duration: int = 30):
        self._server_url = server_url
        self._clock_offset = clock_offset
        self._session = requests.Session()
        self.epoch = epoch
        self.slot_duration = slot_duration

    def what_is_on(self, channel_number: int,
                   when: Optional[datetime] = None) -> Optional[NowPlaying]:
        """Ask the server what's playing on a channel."""
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
            return [
                (datetime.fromtimestamp(item["start_time"]), item["title"])
                for item in resp.json().get("upcoming", [])
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
        server_time=data.get("server_time"),
    )
