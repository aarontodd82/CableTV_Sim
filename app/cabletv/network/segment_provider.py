"""HTTP segment provider for guide/weather segments in remote mode.

Duck-typed replacement for GuideGenerator/WeatherGenerator. Implements
the same `is_ready` property and `get_current_segment()` method that
PlaybackEngine expects. Fetches metadata via server API and streams
segment files via HTTP.
"""

import time
from datetime import datetime
from typing import Optional


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
