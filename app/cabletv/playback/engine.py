"""Playback engine for channel switching and content playback."""

import threading
import time
from datetime import datetime, timedelta
from typing import Optional, Callable

from ..config import Config
from ..platform import get_drive_root
from ..schedule.engine import ScheduleEngine, NowPlaying
from .mpv_control import MpvController


class PlaybackEngine:
    """
    Main playback controller.

    Manages channel switching, content scheduling, and mpv control.
    """

    def __init__(self, config: Config, schedule_engine: ScheduleEngine):
        self.config = config
        self.schedule = schedule_engine
        self.mpv = MpvController(config)
        self._current_channel: Optional[int] = None
        self._current_playing: Optional[NowPlaying] = None
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._on_channel_change: Optional[Callable[[int], None]] = None
        self._on_content_change: Optional[Callable[[NowPlaying], None]] = None

    @property
    def current_channel(self) -> Optional[int]:
        """Get current channel number."""
        return self._current_channel

    @property
    def current_playing(self) -> Optional[NowPlaying]:
        """Get current playing content."""
        return self._current_playing

    def set_on_channel_change(self, callback: Callable[[int], None]) -> None:
        """Set callback for channel changes."""
        self._on_channel_change = callback

    def set_on_content_change(self, callback: Callable[[NowPlaying], None]) -> None:
        """Set callback for content changes."""
        self._on_content_change = callback

    def start(self, fullscreen: bool = True) -> bool:
        """
        Start the playback engine.

        Args:
            fullscreen: Start mpv in fullscreen mode

        Returns:
            True if started successfully
        """
        if not self.mpv.start(fullscreen=fullscreen):
            print("Failed to start mpv")
            return False

        return True

    def tune_to(self, channel_number: int) -> bool:
        """
        Tune to a specific channel.

        Handles both main content and commercial segments. When tuning to a channel,
        checks if we're currently in a commercial break (padding time between content
        end and slot end) and plays the appropriate commercial.

        Args:
            channel_number: Channel number to tune to

        Returns:
            True if successful
        """
        # Validate channel exists
        if channel_number not in self.config.channel_map:
            print(f"Channel {channel_number} not found")
            return False

        with self._lock:
            # Cancel any pending content switch timer
            if self._timer:
                self._timer.cancel()
                self._timer = None

            # Get what's playing on this channel
            now_playing = self.schedule.what_is_on(channel_number)

            if not now_playing:
                print(f"No content available for channel {channel_number}")
                self._show_no_content_message(channel_number)
                self._current_channel = channel_number
                self._current_playing = None
                return False

            # Determine what to play based on segment type
            root = get_drive_root()

            if now_playing.is_up_next and now_playing.up_next:
                # We're in a "Coming Up Next" bumper - show black screen with OSD
                self._current_channel = channel_number
                self._current_playing = now_playing
                self._show_up_next_message(channel_number, now_playing.up_next.next_title, now_playing.remaining_seconds)
                self._schedule_next_content()

                # Fire callbacks
                if self._on_channel_change:
                    self._on_channel_change(channel_number)
                if self._on_content_change:
                    self._on_content_change(now_playing)

                return True
            elif now_playing.is_commercial and now_playing.commercial:
                # We're in a commercial break - play the commercial
                file_path = root / now_playing.commercial.file_path
                seek_position = now_playing.commercial.seek_position
                content_label = f"Commercial: {now_playing.commercial.title}"
            elif now_playing.is_commercial and not now_playing.commercial:
                # We're in commercial time but no commercials available
                # Show a "please stand by" type message and schedule next content
                print(f"In commercial break but no commercials available for channel {channel_number}")
                self._current_channel = channel_number
                self._current_playing = now_playing
                self._show_standby_message(channel_number, now_playing.remaining_seconds)
                self._schedule_next_content()

                # Fire callbacks
                if self._on_channel_change:
                    self._on_channel_change(channel_number)
                if self._on_content_change:
                    self._on_content_change(now_playing)

                return True
            else:
                # Normal main content
                file_path = root / now_playing.entry.file_path
                seek_position = now_playing.seek_position
                content_label = now_playing.entry.title

            if not file_path.exists():
                print(f"Content file not found: {file_path}")
                self._show_no_content_message(channel_number)
                return False

            # Play the content
            success = self.mpv.play_file(
                str(file_path),
                seek_seconds=seek_position
            )

            if not success:
                print(f"Failed to play {file_path}")
                return False

            # Update state
            self._current_channel = channel_number
            self._current_playing = now_playing

            # Show channel OSD
            self._show_channel_osd(channel_number)

            # Schedule next content (handles both end of main content and end of commercial)
            self._schedule_next_content()

            # Fire callbacks
            if self._on_channel_change:
                self._on_channel_change(channel_number)
            if self._on_content_change:
                self._on_content_change(now_playing)

            return True

    def _show_channel_osd(self, channel_number: int) -> None:
        """Show channel number and name on OSD."""
        channel_config = self.config.channel_map.get(channel_number)
        if channel_config:
            message = f"{channel_number}\n{channel_config.name}"
        else:
            message = str(channel_number)

        duration_ms = int(self.config.playback.osd_duration * 1000)
        self.mpv.show_osd_message(message, duration_ms)

    def _show_no_content_message(self, channel_number: int) -> None:
        """Show 'no content' message on OSD."""
        channel_config = self.config.channel_map.get(channel_number)
        name = channel_config.name if channel_config else f"Channel {channel_number}"
        self.mpv.show_osd_message(f"{channel_number}\n{name}\nNo content available", 3000)

    def _show_standby_message(self, channel_number: int, seconds_remaining: float) -> None:
        """Show standby message when in commercial break but no commercials available."""
        channel_config = self.config.channel_map.get(channel_number)
        name = channel_config.name if channel_config else f"Channel {channel_number}"
        # Show a brief standby message - mpv will show black screen
        self.mpv.show_osd_message(
            f"{channel_number} - {name}\nPlease Stand By\n{int(seconds_remaining)}s until next program",
            min(3000, int(seconds_remaining * 1000))
        )

    def _show_up_next_message(self, channel_number: int, next_title: str, seconds_remaining: float) -> None:
        """Show 'Coming Up Next' bumper with black screen and OSD."""
        channel_config = self.config.channel_map.get(channel_number)
        name = channel_config.name if channel_config else f"Channel {channel_number}"
        # Stop current playback to show black screen
        self.mpv.stop()
        # Show OSD with what's coming next - display for entire bumper duration
        self.mpv.show_osd_message(
            f"{channel_number} - {name}\n\nComing Up Next:\n{next_title}",
            int(seconds_remaining * 1000)
        )

    def _schedule_next_content(self) -> None:
        """
        Schedule timer for when current content/commercial/up_next ends.

        When main content ends, we transition to commercials (if any).
        When a commercial ends, we re-tune to get the next commercial or content.
        When an up_next bumper ends, we re-tune to get the next segment.
        """
        if not self._current_playing:
            return

        # Calculate time until we need to switch
        if self._current_playing.is_up_next:
            # We're showing an up_next bumper - wait for it to finish
            remaining = self._current_playing.remaining_seconds
        elif self._current_playing.is_commercial and self._current_playing.commercial:
            # We're playing a commercial - schedule for when THIS commercial ends
            remaining = self._current_playing.commercial.remaining_seconds
        elif self._current_playing.is_commercial:
            # Commercial break but no commercial playing (standby) - wait for slot end
            remaining = self._current_playing.remaining_seconds
        else:
            # Main content - schedule for when it ends (commercials or next content follows)
            remaining = self._current_playing.remaining_seconds

        if remaining > 0:
            # Add a small buffer to ensure we're past the end
            delay = remaining + 0.5
            self._timer = threading.Timer(delay, self._on_content_end)
            self._timer.daemon = True
            self._timer.start()

    def _on_content_end(self) -> None:
        """Called when current content ends."""
        channel = None
        with self._lock:
            if self._current_channel:
                # Re-tune to the same channel to get next content
                # This will get the next scheduled content
                channel = self._current_channel
                self._timer = None

        # Tune outside the lock to avoid deadlock
        if channel:
            self.tune_to(channel)

    def channel_up(self) -> bool:
        """
        Switch to next channel.

        Returns:
            True if successful
        """
        if not self._current_channel:
            # Start at default channel
            return self.tune_to(self.config.playback.default_channel)

        # Get sorted channel numbers
        channels = sorted(self.config.channel_map.keys())
        if not channels:
            return False

        # Find current index
        try:
            current_idx = channels.index(self._current_channel)
            next_idx = (current_idx + 1) % len(channels)
            return self.tune_to(channels[next_idx])
        except ValueError:
            return self.tune_to(channels[0])

    def channel_down(self) -> bool:
        """
        Switch to previous channel.

        Returns:
            True if successful
        """
        if not self._current_channel:
            return self.tune_to(self.config.playback.default_channel)

        channels = sorted(self.config.channel_map.keys())
        if not channels:
            return False

        try:
            current_idx = channels.index(self._current_channel)
            prev_idx = (current_idx - 1) % len(channels)
            return self.tune_to(channels[prev_idx])
        except ValueError:
            return self.tune_to(channels[-1])

    def get_status(self) -> dict:
        """
        Get current playback status.

        Returns:
            Dict with status information including commercial state
        """
        status = {
            "channel": self._current_channel,
            "channel_name": None,
            "playing": None,
            "position": None,
            "duration": None,
            "remaining": None,
            "is_commercial": False,
            "commercial": None,
            "is_up_next": False,
            "up_next": None,
            "slot_remaining": None,
        }

        if self._current_channel:
            channel_config = self.config.channel_map.get(self._current_channel)
            if channel_config:
                status["channel_name"] = channel_config.name

        if self._current_playing:
            entry = self._current_playing.entry
            status["playing"] = {
                "title": entry.title,
                "content_type": entry.content_type,
                "start_time": entry.start_time.isoformat(),
                "end_time": entry.end_time.isoformat(),
                "slot_end_time": entry.slot_end_time.isoformat(),
            }

            # Include commercial and up_next state
            status["is_commercial"] = self._current_playing.is_commercial
            status["is_up_next"] = self._current_playing.is_up_next
            status["slot_remaining"] = self._current_playing.slot_remaining_seconds

            if self._current_playing.is_commercial and self._current_playing.commercial:
                commercial = self._current_playing.commercial
                status["commercial"] = {
                    "title": commercial.title,
                    "duration": commercial.duration_seconds,
                    "remaining": commercial.remaining_seconds,
                    "main_content_title": commercial.main_content_title,
                }

            if self._current_playing.is_up_next and self._current_playing.up_next:
                up_next = self._current_playing.up_next
                status["up_next"] = {
                    "next_title": up_next.next_title,
                    "duration": up_next.duration_seconds,
                    "remaining": up_next.remaining_seconds,
                }

            # Get live position from mpv if available
            position = self.mpv.get_position()
            if position is not None:
                status["position"] = position
                if self._current_playing.is_commercial and self._current_playing.commercial:
                    status["duration"] = self._current_playing.commercial.duration_seconds
                    status["remaining"] = max(0, self._current_playing.commercial.remaining_seconds)
                else:
                    status["duration"] = entry.duration_seconds
                    status["remaining"] = max(0, entry.duration_seconds - position)

        return status

    def stop(self) -> None:
        """Stop playback."""
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None

        self.mpv.stop()
        self._current_playing = None

    def shutdown(self) -> None:
        """Shutdown the playback engine."""
        self.stop()
        self.mpv.shutdown()
