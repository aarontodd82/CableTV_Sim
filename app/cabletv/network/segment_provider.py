"""Remote segment provider — reads guide/weather segments from network share.

Duck-typed replacement for GuideGenerator/WeatherGenerator. Implements
the same `is_ready` property and `get_current_segment()` method that
PlaybackEngine expects.
"""

import json
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
