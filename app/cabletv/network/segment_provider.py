"""Remote segment providers for guide/weather segments.

Duck-typed replacements for GuideGenerator/WeatherGenerator. Implement
the same `is_ready` property and `get_current_segment()` method that
PlaybackEngine expects.

RemoteSegmentProvider: reads from a mounted network share (SMB/CIFS).
HttpSegmentProvider: fetches metadata via server API, streams via HTTP.
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional


class RemoteSegmentProvider:
    """Reads pre-rendered segments from a network share directory.

    Scans for segment files and their JSON sidecars to provide the
    same interface as GuideGenerator/WeatherGenerator.

    Args:
        segment_dir: Path to the shared segment directory (e.g. guide/ or weather/)
        prefix: Filename prefix to match (e.g. "segment_" or "weather_")
    """

    def __init__(self, segment_dir: Path, prefix: str = "segment_"):
        self._dir = segment_dir
        self._prefix = prefix

    @property
    def is_ready(self) -> bool:
        """Check if any segment is available."""
        return self.get_current_segment() is not None

    def get_current_segment(self) -> Optional[tuple[Path, datetime, float]]:
        """Get the most recent segment for playback.

        Scans for .mp4 files matching the prefix, reads the JSON sidecar
        for timing metadata.

        Returns:
            Tuple of (file_path, generation_time, segment_duration) or None
        """
        if not self._dir.exists():
            return None

        # Find all matching segment files
        best_path = None
        best_time = None
        best_duration = 0.0

        for mp4 in self._dir.glob(f"{self._prefix}*.mp4"):
            sidecar = mp4.with_suffix(".json")
            if sidecar.exists():
                try:
                    data = json.loads(sidecar.read_text(encoding="utf-8"))
                    gen_time = datetime.fromisoformat(data["generation_time"])
                    duration = float(data["duration"])

                    # Pick the most recent segment
                    if best_time is None or gen_time > best_time:
                        best_path = mp4
                        best_time = gen_time
                        best_duration = duration
                except (json.JSONDecodeError, KeyError, ValueError):
                    pass
            else:
                # No sidecar — use file modification time as fallback
                try:
                    mtime = datetime.fromtimestamp(mp4.stat().st_mtime)
                    if best_time is None or mtime > best_time:
                        best_path = mp4
                        best_time = mtime
                        best_duration = 60.0  # Default fallback
                except OSError:
                    pass

        if best_path and best_time:
            return (best_path, best_time, best_duration)
        return None


class HttpSegmentProvider:
    """Fetches segment metadata from server API, returns HTTP URLs for mpv.

    Eliminates the need for SMB/CIFS network share mounts. The server's
    existing /media/ endpoint serves the actual .mp4 files with range
    request support.

    Args:
        server_url: Base server URL (e.g. "http://192.168.1.100:5000")
        segment_type: "guide" or "weather"
        cache_ttl: Seconds to cache API responses (avoids hammering on every poll)
    """

    def __init__(self, server_url: str, segment_type: str, cache_ttl: float = 3.0):
        self._server_url = server_url.rstrip("/")
        self._segment_type = segment_type
        self._cache_ttl = cache_ttl
        self._cached_result: Optional[tuple[str, datetime, float]] = None
        self._cache_time: float = 0.0

    @property
    def is_ready(self) -> bool:
        """Check if a segment is available from the server."""
        return self.get_current_segment() is not None

    def get_current_segment(self) -> Optional[tuple[str, datetime, float]]:
        """Get the current segment URL and metadata from the server.

        Returns:
            Tuple of (http_url, generation_time, segment_duration) or None.
            The URL is a full HTTP URL that mpv can play directly.
        """
        now = time.monotonic()
        if self._cached_result and (now - self._cache_time) < self._cache_ttl:
            return self._cached_result

        try:
            import requests
            endpoint = f"{self._server_url}/api/server/{self._segment_type}-segment"
            resp = requests.get(endpoint, timeout=5)
            if resp.status_code != 200:
                return self._cached_result  # Return stale cache on error
            data = resp.json()

            gen_time = datetime.fromisoformat(data["generation_time"])
            duration = float(data["duration"])
            url = f"{self._server_url}{data['url']}"

            self._cached_result = (url, gen_time, duration)
            self._cache_time = now
            return self._cached_result
        except Exception:
            return self._cached_result  # Return stale cache on error
