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
        self._music_end_timer: Optional[threading.Timer] = None
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

        Handles main content, commercial segments, and info bumpers.
        Lock is held only for state reads/writes, not during mpv IPC calls.

        Args:
            channel_number: Channel number to tune to

        Returns:
            True if successful
        """
        # Validate channel exists
        if channel_number not in self.config.channel_map:
            print(f"Channel {channel_number} not found")
            return False

        # Phase 1: Compute what to play and update state (lock held, no IPC)
        play_action = None
        file_path = None
        seek_position = 0
        now_playing = None

        with self._lock:
            # Cancel any pending content switch timer
            if self._timer:
                self._timer.cancel()
                self._timer = None
            if self._music_end_timer:
                self._music_end_timer.cancel()
                self._music_end_timer = None

            # Get what's playing on this channel
            now_playing = self.schedule.what_is_on(channel_number)

            if not now_playing:
                self._current_channel = channel_number
                self._current_playing = None
                play_action = "no_content"
            else:
                root = get_drive_root()

                if now_playing.is_commercial and now_playing.commercial:
                    file_path = root / now_playing.commercial.file_path
                    seek_position = now_playing.commercial.seek_position
                    play_action = "play_file"
                elif now_playing.is_commercial and not now_playing.commercial:
                    play_action = "info_bumper"
                else:
                    file_path = root / now_playing.entry.file_path
                    seek_position = now_playing.seek_position
                    play_action = "play_file"

                # Check file exists
                if play_action == "play_file" and not file_path.exists():
                    print(f"Content file not found: {file_path}")
                    play_action = "no_content"

                # Update state
                self._current_channel = channel_number
                self._current_playing = now_playing

        # Phase 2: Execute mpv commands (NO lock — IPC has its own lock)
        if play_action == "no_content":
            self._show_no_content_message(channel_number)
            return False

        elif play_action == "info_bumper":
            self._show_info_bumper(channel_number, now_playing.remaining_seconds)

        elif play_action == "play_file":
            success = self.mpv.play_file(str(file_path), seek_seconds=seek_position)
            if not success:
                print(f"Failed to play {file_path}")
                return False

            if now_playing and now_playing.entry.content_type == "music":
                # Music videos: show artist/title/year instead of channel OSD
                self._show_music_osd(now_playing)
                # Schedule end-of-video OSD
                remaining = now_playing.remaining_seconds
                if remaining > 10:
                    delay = remaining - 5
                    self._music_end_timer = threading.Timer(
                        delay, self._show_music_osd, args=[now_playing])
                    self._music_end_timer.daemon = True
                    self._music_end_timer.start()
            else:
                self._show_channel_osd(channel_number)

        # Phase 3: Schedule next transition timer (lock held)
        with self._lock:
            self._schedule_next_content()

        # Phase 4: Fire callbacks (NO lock — avoids deadlock)
        if self._on_channel_change:
            self._on_channel_change(channel_number)
        if self._on_content_change and now_playing:
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

    def _show_info_bumper(self, channel_number: int, seconds_remaining: float) -> None:
        """Show info bumper during gaps in commercial breaks.

        For gaps under 3 seconds, just shows black screen.
        For longer gaps, shows a mini-guide with current and upcoming programs.
        """
        channel_config = self.config.channel_map.get(channel_number)
        name = channel_config.name if channel_config else f"Channel {channel_number}"

        # Stop playback to show black screen
        self.mpv.stop()

        if seconds_remaining < 3:
            # Too short for text — just stay black
            return

        # Build mini-guide: current program + upcoming
        now_playing = self._current_playing
        lines = [f"Ch {channel_number} - {name}", ""]

        if now_playing and now_playing.is_end_bumper:
            # End-of-slot bumper: current show is done, show upcoming as "Now"
            upcoming = self.schedule.get_upcoming(channel_number, count=3)
            if upcoming:
                lines.append(f"Now:  {upcoming[0][1]}")
                for start_time, title in upcoming[1:]:
                    time_str = start_time.strftime("%I:%M %p").lstrip("0")
                    lines.append(f"{time_str}  {title}")
        else:
            # Mid-content bumper: show resumes after this break
            if now_playing:
                lines.append(f"Now:  {now_playing.entry.title}")
            upcoming = self.schedule.get_upcoming(channel_number, count=2)
            for start_time, title in upcoming:
                time_str = start_time.strftime("%I:%M %p").lstrip("0")
                lines.append(f"{time_str}  {title}")

        self.mpv.show_osd_message("\n".join(lines), int(seconds_remaining * 1000))

    def _show_music_osd(self, now_playing: NowPlaying) -> None:
        """Show artist / title / year OSD for music videos."""
        entry = now_playing.entry
        lines = []
        if entry.artist:
            lines.append(entry.artist)
        lines.append(entry.title)
        if entry.year:
            lines.append(str(entry.year))
        self.mpv.show_osd_message("\n".join(lines), 5000)

    def _schedule_next_content(self) -> None:
        """
        Schedule timer for when current content/commercial/bumper ends.

        Must be called with self._lock held.
        """
        if not self._current_playing:
            return

        # Calculate time until we need to switch
        if self._current_playing.is_commercial and self._current_playing.commercial:
            remaining = self._current_playing.commercial.remaining_seconds
        else:
            remaining = self._current_playing.remaining_seconds

        if remaining > 0:
            delay = remaining + 0.15
            self._timer = threading.Timer(delay, self._on_content_end)
            self._timer.daemon = True
            self._timer.start()

    def _on_content_end(self) -> None:
        """Called when current content/commercial/bumper ends."""
        channel = None
        with self._lock:
            if self._current_channel:
                channel = self._current_channel
                self._timer = None

        # Tune outside the lock to avoid deadlock
        if channel:
            try:
                self.tune_to(channel)
            except Exception as e:
                print(f"Error during content transition: {e}")

    def channel_up(self) -> bool:
        """Switch to next channel."""
        if not self._current_channel:
            return self.tune_to(self.config.playback.default_channel)

        channels = sorted(self.config.channel_map.keys())
        if not channels:
            return False

        try:
            current_idx = channels.index(self._current_channel)
            next_idx = (current_idx + 1) % len(channels)
            return self.tune_to(channels[next_idx])
        except ValueError:
            return self.tune_to(channels[0])

    def channel_down(self) -> bool:
        """Switch to previous channel."""
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
        Get current playback status. Thread-safe.

        State snapshot is taken under lock; mpv IPC calls are outside
        (mpv has its own IPC lock).
        """
        # Snapshot state under lock
        with self._lock:
            channel = self._current_channel
            playing = self._current_playing

        status = {
            "channel": channel,
            "channel_name": None,
            "playing": None,
            "position": None,
            "duration": None,
            "remaining": None,
            "is_commercial": False,
            "commercial": None,
            "slot_remaining": None,
        }

        if channel:
            channel_config = self.config.channel_map.get(channel)
            if channel_config:
                status["channel_name"] = channel_config.name

        if playing:
            entry = playing.entry
            status["playing"] = {
                "title": entry.title,
                "content_type": entry.content_type,
                "start_time": entry.start_time.isoformat(),
                "end_time": entry.end_time.isoformat(),
                "slot_end_time": entry.slot_end_time.isoformat(),
            }

            status["is_commercial"] = playing.is_commercial
            status["slot_remaining"] = playing.slot_remaining_seconds

            if playing.is_commercial and playing.commercial:
                commercial = playing.commercial
                status["commercial"] = {
                    "title": commercial.title,
                    "duration": commercial.duration_seconds,
                    "remaining": commercial.remaining_seconds,
                    "main_content_title": commercial.main_content_title,
                }

            # Get live position from mpv (safe — mpv has its own IPC lock)
            position = self.mpv.get_position()
            if position is not None:
                status["position"] = position
                if playing.is_commercial and playing.commercial:
                    status["duration"] = playing.commercial.duration_seconds
                    status["remaining"] = max(0, playing.commercial.remaining_seconds)
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
            if self._music_end_timer:
                self._music_end_timer.cancel()
                self._music_end_timer = None

        self.mpv.stop()
        self._current_playing = None

    def shutdown(self) -> None:
        """Shutdown the playback engine."""
        self.stop()
        self.mpv.shutdown()
