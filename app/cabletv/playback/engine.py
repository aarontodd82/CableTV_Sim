"""Playback engine for channel switching and content playback."""

import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Callable, TYPE_CHECKING

from ..config import Config
from ..platform import get_content_paths, get_drive_root
from ..schedule.engine import ScheduleEngine, NowPlaying
from ..utils.time_utils import get_slot_number
from .mpv_control import MpvController

if TYPE_CHECKING:
    from ..guide.generator import GuideGenerator
    from ..weather.generator import WeatherGenerator


class PlaybackEngine:
    """
    Main playback controller.

    Manages channel switching, content scheduling, and mpv control.
    """

    def __init__(self, config: Config, schedule_engine: ScheduleEngine,
                 content_root: Optional[Path] = None,
                 media_base_url: Optional[str] = None,
                 clock_offset: float = 0.0):
        self.config = config
        self.schedule = schedule_engine
        self._content_root = content_root or get_drive_root()
        self._media_base_url = media_base_url  # HTTP URL for streaming (remote mode)
        self._clock_offset = timedelta(seconds=clock_offset)
        self.mpv = MpvController(config)
        self._current_channel: Optional[int] = None
        self._current_playing: Optional[NowPlaying] = None
        self._timer: Optional[threading.Timer] = None
        self._music_end_timer: Optional[threading.Timer] = None
        self._next_ep_timer: Optional[threading.Timer] = None
        self._mid_ep_timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._on_channel_change: Optional[Callable[[int], None]] = None
        self._on_content_change: Optional[Callable[[NowPlaying], None]] = None
        self._guide_generator: Optional["GuideGenerator"] = None
        self._guide_current_file: Optional[Path] = None
        self._weather_generator: Optional["WeatherGenerator"] = None
        self._weather_current_file: Optional[Path] = None
        self._weather_clock_timer: Optional[threading.Timer] = None
        # Track which content has been "seen" per channel (content_id already advanced)
        self._seen_content: dict[int, int] = {}  # channel -> content_id
        self._bumper_bg_path: Optional[Path] = None

    def _now(self) -> datetime:
        """Get current time adjusted for clock offset (remote sync)."""
        return datetime.now() + self._clock_offset

    def _resolve_media_path(self, rel_path: str) -> str:
        """Resolve a relative media path to a playable URL or local path.

        In remote mode with HTTP streaming, returns an HTTP URL.
        Otherwise returns a local filesystem path.
        """
        if self._media_base_url:
            # Convert backslashes to forward slashes for URL
            url_path = rel_path.replace("\\", "/")
            return f"{self._media_base_url}/{url_path}"
        return str(self._content_root / rel_path)

    def set_guide_generator(self, generator: "GuideGenerator") -> None:
        """Set the guide generator for TV Guide channel playback."""
        self._guide_generator = generator

    def set_weather_generator(self, generator: "WeatherGenerator") -> None:
        """Set the weather generator for Weather Channel playback."""
        self._weather_generator = generator

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

    def _cancel_all_timers(self) -> None:
        """Cancel all pending timers. Must be called with self._lock held."""
        for attr in ("_timer", "_music_end_timer", "_next_ep_timer",
                      "_mid_ep_timer", "_weather_clock_timer"):
            timer = getattr(self, attr, None)
            if timer:
                timer.cancel()
                setattr(self, attr, None)

    def tune_to(self, channel_number: int, user_initiated: bool = True) -> bool:
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

        # Clear any persistent overlays from previous content
        self.mpv.remove_osd_overlay(self._NEXT_EP_OVERLAY_ID)
        self.mpv.remove_osd_overlay(self._WEATHER_CLOCK_OVERLAY_ID)

        # Check if this is the guide channel
        if channel_number == self.config.guide.channel_number and self.config.guide.enabled:
            return self._tune_to_guide(channel_number)

        # Check if this is the weather channel
        if channel_number == self.config.weather.channel_number and self.config.weather.enabled:
            return self._tune_to_weather(channel_number)

        # Clear guide/weather loop mode when tuning to a regular channel
        self.mpv._set_property("loop-file", "no")

        # Phase 1: Compute what to play and update state (lock held, no IPC)
        play_action = None
        file_path = None
        seek_position = 0
        now_playing = None

        with self._lock:
            self._cancel_all_timers()

            # Get what's playing on this channel
            now_playing = self.schedule.what_is_on(channel_number)

            # Advance series position on first sight — if you see an episode
            # playing, it's consumed and the next selection will be the next episode.
            # Slot cache in the schedule engine prevents this from affecting
            # the currently-playing block.
            advance_info = None
            if now_playing and not now_playing.is_commercial:
                content_id = now_playing.entry.content_id
                if self._seen_content.get(channel_number) != content_id:
                    # First time seeing this episode on this channel — advance it
                    self._seen_content[channel_number] = content_id
                    entry = now_playing.entry
                    gk = entry.series_name if entry.series_name else f"standalone_{entry.content_id}"
                    ch_config = self.config.channel_map.get(channel_number)
                    group_size = 1
                    if ch_config:
                        for g in self.schedule.get_channel_groups(ch_config):
                            if g.group_key == gk:
                                group_size = len(g.items)
                                break
                    # Calculate block start slot so advance_position can
                    # preserve this block's cache entries (prevents re-tune
                    # mid-slot from jumping to the next episode)
                    block_start_slot = get_slot_number(
                        entry.start_time,
                        self.schedule.epoch,
                        self.schedule.slot_duration)
                    advance_info = (content_id, gk, group_size, block_start_slot,
                                    now_playing.pack_count)

            if not now_playing:
                self._current_channel = channel_number
                self._current_playing = None
                play_action = "no_content"
            else:
                if now_playing.is_commercial and now_playing.commercial:
                    rel_path = now_playing.commercial.file_path
                    seek_position = now_playing.commercial.seek_position
                    play_action = "play_file"
                elif now_playing.is_commercial and not now_playing.commercial:
                    play_action = "info_bumper"
                else:
                    rel_path = now_playing.entry.file_path
                    seek_position = now_playing.seek_position
                    play_action = "play_file"

                if play_action == "play_file":
                    file_path = self._resolve_media_path(rel_path)
                    # Only check existence for local files (not HTTP URLs)
                    if not self._media_base_url and not Path(file_path).exists():
                        print(f"Content file not found: {file_path}")
                        play_action = "no_content"

                # Update state
                self._current_channel = channel_number
                self._current_playing = now_playing

        # Advance series position outside the lock (DB I/O)
        if advance_info:
            _, prev_group_key, prev_group_size, block_start_slot, pack_count = advance_info
            try:
                self.schedule.advance_position(
                    channel_number, prev_group_key, prev_group_size,
                    preserve_block_start=block_start_slot,
                    advance_by=pack_count)
            except Exception as e:
                print(f"Error advancing series position: {e}")

        # Phase 2: Execute mpv commands (NO lock — IPC has its own lock)
        if play_action == "no_content":
            self._show_no_content_message(channel_number)
            return False

        elif play_action == "info_bumper":
            self._show_info_bumper(channel_number, now_playing.remaining_seconds)

        elif play_action == "play_file":
            self.mpv.set_volume(100)
            success = self.mpv.play_file(str(file_path), seek_seconds=seek_position)
            if not success:
                print(f"Failed to play {file_path}")
            else:
                # Recalculate seek position now that playback has started —
                # the original was computed before loading, which can take
                # time on HTTP streams, putting us behind
                fresh = self.schedule.what_is_on(channel_number)
                if fresh:
                    if fresh.is_commercial and fresh.commercial:
                        self.mpv.seek(fresh.commercial.seek_position)
                    elif not fresh.is_commercial:
                        self.mpv.seek(fresh.seek_position)
                return False

            if now_playing and now_playing.is_commercial:
                # Commercials: only show OSD on user-initiated channel changes
                if user_initiated:
                    self._show_channel_osd(channel_number)
            elif now_playing and now_playing.entry.content_type == "music":
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
                self._show_channel_osd(channel_number, now_playing)

                # Schedule "next episode" bumper for shows in their last content segment
                # (disabled for multi-episode packed blocks — inter-episode breaks
                # already have info bumpers showing what's coming up)
                if (now_playing.pack_count == 1
                        and now_playing.entry.content_type == "show"
                        and now_playing.entry.series_name
                        and now_playing.remaining_seconds > 3):
                    # Check if this is the last content segment:
                    # seek_position + remaining == duration means content plays to the end
                    at_end = (now_playing.seek_position + now_playing.remaining_seconds
                              >= now_playing.entry.duration_seconds - 2.0)
                    if at_end:
                        # End-of-show bumper: show for last 20s
                        bumper_duration = min(20.0, now_playing.remaining_seconds)
                        delay = now_playing.remaining_seconds - bumper_duration
                        self._next_ep_timer = threading.Timer(
                            delay, self._show_next_episode_bumper,
                            args=[now_playing])
                        self._next_ep_timer.daemon = True
                        self._next_ep_timer.start()
                    else:
                        # Mid-show bumper: show for 20s around the episode's midpoint
                        ep_midpoint = now_playing.entry.duration_seconds / 2
                        seg_start = now_playing.seek_position
                        seg_end = seg_start + now_playing.remaining_seconds
                        if seg_start < ep_midpoint < seg_end:
                            ideal_delay = ep_midpoint - seg_start
                            latest_start = now_playing.remaining_seconds - 20.0
                            mid_delay = max(0, min(ideal_delay, latest_start))
                            show_duration = min(20.0, now_playing.remaining_seconds - mid_delay)
                            if show_duration > 10:
                                self._mid_ep_timer = threading.Timer(
                                    mid_delay, self._show_next_episode_bumper,
                                    args=[now_playing, show_duration])
                                self._mid_ep_timer.daemon = True
                                self._mid_ep_timer.start()

        # Phase 3: Schedule next transition timer (lock held)
        with self._lock:
            self._schedule_next_content()

        # Phase 4: Fire callbacks (NO lock — avoids deadlock)
        if self._on_channel_change:
            self._on_channel_change(channel_number)
        if self._on_content_change and now_playing:
            self._on_content_change(now_playing)

        return True

    def _show_channel_osd(self, channel_number: int,
                          now_playing: Optional[NowPlaying] = None) -> None:
        """Show channel number, name, and current program title on OSD."""
        channel_config = self.config.channel_map.get(channel_number)
        if channel_config:
            message = f"{channel_number}\n{channel_config.name}"
        else:
            message = str(channel_number)

        # Add clean program title (strip year from movies, S##E## from shows)
        if now_playing and not now_playing.is_commercial:
            title = now_playing.entry.title
            # "The Shining (1980)" → "The Shining"
            title = re.sub(r"\s*\(\d{4}\)$", "", title)
            # "Friends S03E10" → "Friends"
            title = re.sub(r"\s+S\d{2}E\d{2}$", "", title)
            message += f"\n{title}"

        duration_ms = int(self.config.playback.osd_duration * 1000)
        self.mpv.show_osd_message(message, duration_ms)

    def _show_bumper_background(self, osd_text: str = None,
                               osd_duration_ms: int = 3000) -> None:
        """Show the gradient background, optionally with music and OSD text.

        Used for info bumpers, no-content screens, and loading screens.
        """
        bg_path = self._get_bumper_background()

        # Check for configured background music
        music_path = self.config.playback.bumper_music
        audio_file = None
        if music_path and Path(music_path).exists():
            audio_file = music_path

        self.mpv.play_file(str(bg_path), audio_file=audio_file)

        if audio_file:
            self.mpv.set_volume(50)

        if osd_text:
            self.mpv.show_osd_message(osd_text, osd_duration_ms)

    def _show_no_content_message(self, channel_number: int) -> None:
        """Show 'no content' message on OSD."""
        channel_config = self.config.channel_map.get(channel_number)
        name = channel_config.name if channel_config else f"Channel {channel_number}"
        self._show_bumper_background(
            f"{channel_number}\n{name}\nNo content available", 3000)

    def _get_bumper_background(self) -> Path:
        """Get the gradient background image for info bumpers, generating if needed."""
        if self._bumper_bg_path and self._bumper_bg_path.exists():
            return self._bumper_bg_path

        from PIL import Image

        width, height = 640, 480
        top = (0, 0, 0)         # Black
        bottom = (10, 15, 80)   # Dark blue

        # Build raw RGB data row-by-row (fast, no per-pixel calls)
        rows = []
        for y in range(height):
            t = y / (height - 1)
            r = int(top[0] + (bottom[0] - top[0]) * t)
            g = int(top[1] + (bottom[1] - top[1]) * t)
            b = int(top[2] + (bottom[2] - top[2]) * t)
            rows.append(bytes([r, g, b]) * width)

        img = Image.frombytes("RGB", (width, height), b"".join(rows))

        paths = get_content_paths()
        bg_path = paths["guide_segments"] / "bumper_bg.png"
        bg_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(bg_path))

        self._bumper_bg_path = bg_path
        return bg_path

    def _show_info_bumper(self, channel_number: int, seconds_remaining: float) -> None:
        """Show info bumper during gaps in commercial breaks.

        For gaps under 3 seconds, just shows gradient background.
        For longer gaps, shows a mini-guide with current and upcoming programs.
        """
        channel_config = self.config.channel_map.get(channel_number)
        name = channel_config.name if channel_config else f"Channel {channel_number}"

        if seconds_remaining < 3:
            self._show_bumper_background()
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

        osd_text = "\n".join(lines)
        self._show_bumper_background(osd_text, int(seconds_remaining * 1000))

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

    # Overlay IDs (mpv supports 0-63)
    _NEXT_EP_OVERLAY_ID = 1
    _WEATHER_CLOCK_OVERLAY_ID = 2

    def _show_next_episode_bumper(self, now_playing: NowPlaying,
                                   duration: float = None) -> None:
        """Show 'next episode' bumper as a styled ASS overlay.

        Args:
            now_playing: The content this bumper was scheduled for.
            duration: If set, auto-remove the overlay after this many seconds.
                      If None, overlay persists until tune_to() clears it.
        """
        # Safety check: still on same channel, same content
        with self._lock:
            if (self._current_channel != now_playing.entry.channel_number
                    or self._current_playing is None
                    or self._current_playing.entry.content_id != now_playing.entry.content_id):
                return

        entry = now_playing.entry
        try:
            next_time = self.schedule.find_next_airing(
                entry.channel_number, entry.series_name,
                after_time=entry.slot_end_time)
        except Exception:
            return

        if not next_time:
            return

        # Format day + time
        today = self._now().date()
        next_date = next_time.date()
        delta_days = (next_date - today).days

        if delta_days == 0:
            day_str = "Today"
        elif delta_days == 1:
            day_str = "Tomorrow"
        else:
            day_str = next_time.strftime("%A")  # "Wednesday"

        time_str = next_time.strftime("%I:%M %p").lstrip("0")  # "7:30 PM"

        series = entry.series_name
        ch_config = self.config.channel_map.get(entry.channel_number)
        ch_name = ch_config.name if ch_config else f"Channel {entry.channel_number}"

        # Lower-third overlay: semi-transparent black box + white text (2 lines)
        # Two ASS events: \an7 (top-left) box drawing, \an5 (center) text.
        # Overscan compensation keeps the bar inside the visible area.
        overscan = self.config.playback.overscan
        m = overscan / 100.0 if overscan > 0 else 0.0
        vis_w = 640 * (1 - 2 * m)
        vis_h = 480 * (1 - 2 * m)
        left = int(m * 640)
        bottom = int(m * 480 + vis_h)

        box_h = 60
        box_top = bottom - box_h
        box_w = int(vis_w)

        # \an7 + \p1: top-left anchored rectangle spanning visible width
        box_event = (
            r"{\an7\pos(" + str(left) + "," + str(box_top) + r")"
            r"\p1\bord0\shad0\1c&H000000&\1a&H80&}"
            f"m 0 0 l {box_w} 0 {box_w} {box_h} 0 {box_h}"
        )
        # \an5: centered in the box, two lines
        text_cx = 320
        text_cy = box_top + box_h // 2
        text_event = (
            r"{\an5\pos(" + str(text_cx) + "," + str(text_cy) + r")"
            r"\bord0\shad0\1c&HFFFFFF&\fs18}"
            f"Catch {series} next on {ch_name}\\N{day_str} at {time_str}"
        )
        bumper_text = box_event + "\n" + text_event

        self.mpv.show_osd_overlay(
            self._NEXT_EP_OVERLAY_ID, bumper_text, res_x=640, res_y=480)

        # Auto-remove after duration (mid-show bumper), or persist until
        # tune_to() clears it (end-of-show bumper)
        if duration:
            timer = threading.Timer(duration, self._remove_next_ep_overlay,
                                    args=[now_playing])
            timer.daemon = True
            timer.start()

    def show_info_overlay(self) -> bool:
        """Show the 'next airing' lower-third on demand (triggered by remote).

        Returns True if the overlay was shown, False if nothing to show.
        """
        with self._lock:
            now_playing = self._current_playing
            channel = self._current_channel

        if (not now_playing
                or now_playing.is_commercial
                or not now_playing.entry.series_name):
            return False

        # Show for 5 seconds then auto-remove
        self._show_next_episode_bumper(now_playing, duration=5.0)
        return True

    def _remove_next_ep_overlay(self, now_playing: NowPlaying) -> None:
        """Remove the next-episode overlay if still on the same content."""
        with self._lock:
            still_playing = (
                self._current_channel == now_playing.entry.channel_number
                and self._current_playing is not None
                and self._current_playing.entry.content_id == now_playing.entry.content_id)
        if still_playing:
            self.mpv.remove_osd_overlay(self._NEXT_EP_OVERLAY_ID)

    def _tune_to_guide(self, channel_number: int) -> bool:
        """
        Tune to the TV Guide channel.

        Plays the pre-rendered guide segment, seeking to the correct
        position so it feels like a live always-running channel.
        Starts a polling timer that checks every 5 seconds for new segments.
        """
        with self._lock:
            self._cancel_all_timers()
            self._current_channel = channel_number
            self._current_playing = None

        # Check if guide generator is available and ready
        if not self._guide_generator or not self._guide_generator.is_ready:
            channel_config = self.config.channel_map.get(channel_number)
            name = channel_config.name if channel_config else "TV Guide"
            self._show_bumper_background(
                f"{channel_number}\n{name}\nLoading...", 5000)

            # Poll until ready
            with self._lock:
                self._timer = threading.Timer(3.0, self._guide_poll)
                self._timer.daemon = True
                self._timer.start()
            return True

        segment_info = self._guide_generator.get_current_segment()
        if not segment_info:
            self._show_bumper_background(
                f"{channel_number}\nTV Guide\nLoading...", 5000)
            with self._lock:
                self._timer = threading.Timer(3.0, self._guide_poll)
                self._timer.daemon = True
                self._timer.start()
            return True

        file_path, generation_time, segment_duration = segment_info

        # Calculate seek position: where we should be in the segment right now
        elapsed = (self._now() - generation_time).total_seconds()
        seek = elapsed % segment_duration if segment_duration > 0 else 0

        # Play the segment file, looping so it never stops between polls
        self.mpv.set_volume(100)
        self.mpv._set_property("loop-file", "inf")
        success = self.mpv.play_file(str(file_path), seek_seconds=seek)
        if not success:
            print(f"Failed to play guide segment: {file_path}")
            self.mpv._set_property("loop-file", "no")
            return False

        self._show_channel_osd(channel_number)

        # Remember which file we're playing so the poll can detect changes
        self._guide_current_file = file_path

        # Start polling for segment changes every 5 seconds
        with self._lock:
            self._timer = threading.Timer(5.0, self._guide_poll)
            self._timer.daemon = True
            self._timer.start()

        # Fire channel change callback
        if self._on_channel_change:
            self._on_channel_change(channel_number)

        return True

    def _guide_poll(self) -> None:
        """Poll for guide segment changes. Runs every 5 seconds while on guide channel."""
        with self._lock:
            channel = self._current_channel
            self._timer = None

        guide_ch = self.config.guide.channel_number
        if channel != guide_ch:
            return  # User switched away, stop polling

        try:
            if not self._guide_generator or not self._guide_generator.is_ready:
                # Not ready yet — keep polling
                with self._lock:
                    self._timer = threading.Timer(3.0, self._guide_poll)
                    self._timer.daemon = True
                    self._timer.start()
                return

            segment_info = self._guide_generator.get_current_segment()
            if not segment_info:
                with self._lock:
                    self._timer = threading.Timer(3.0, self._guide_poll)
                    self._timer.daemon = True
                    self._timer.start()
                return

            file_path, generation_time, segment_duration = segment_info
            old_file = getattr(self, '_guide_current_file', None)

            if old_file is None or file_path != old_file:
                # Segment changed — switch to the new one
                print(f"  Guide: switching to new segment")
                elapsed = (self._now() - generation_time).total_seconds()
                seek = elapsed % segment_duration if segment_duration > 0 else 0

                self.mpv._set_property("loop-file", "inf")
                self.mpv.play_file(str(file_path), seek_seconds=seek)
                self._guide_current_file = file_path

            # Keep polling
            with self._lock:
                if self._current_channel == guide_ch:
                    self._timer = threading.Timer(5.0, self._guide_poll)
                    self._timer.daemon = True
                    self._timer.start()

        except Exception as e:
            print(f"Guide poll error: {e}")
            # Keep polling even after errors
            with self._lock:
                if self._current_channel == guide_ch:
                    self._timer = threading.Timer(5.0, self._guide_poll)
                    self._timer.daemon = True
                    self._timer.start()

    def _show_weather_clock(self) -> None:
        """Show/update the live clock overlay on the weather channel.

        Positioned in the reserved clock gap at the right side of the
        brand bar (rightmost 110px of the 28px-tall bar).
        The renderer leaves this area as solid background color.

        Accounts for overscan: video-margin-ratio shifts the video inward,
        so the overlay coordinates must shift by the same amount.
        """
        # Guard: only show clock if still on the weather channel
        with self._lock:
            if self._current_channel != self.config.weather.channel_number:
                return

        now = self._now()
        time_str = now.strftime("%I:%M %p").lstrip("0").upper()

        # Base position in 640x480 space (center of clock gap)
        cx, cy = 585, 14

        # Shift inward by overscan margin so overlay aligns with the
        # shifted video content
        overscan = self.config.playback.overscan
        if overscan > 0:
            margin = overscan / 100.0
            cx = int(margin * 640) + int((640 - 2 * margin * 640) * (585 / 640))
            cy = int(margin * 480) + int((480 - 2 * margin * 480) * (14 / 480))

        clock_ass = (
            r"{\an5\pos(" + str(cx) + "," + str(cy) + r")"
            r"\bord0\1c&HFFFFFF&\shad0"
            r"\fnVCR OSD Mono\fs14}" + time_str
        )
        self.mpv.show_osd_overlay(
            self._WEATHER_CLOCK_OVERLAY_ID, clock_ass,
            res_x=640, res_y=480,
        )

    def _tune_to_weather(self, channel_number: int) -> bool:
        """
        Tune to the Weather Channel.

        Plays the pre-rendered weather segment with loop, polling for new
        segments every 5 seconds (identical pattern to guide channel).
        """
        with self._lock:
            self._cancel_all_timers()
            self._current_channel = channel_number
            self._current_playing = None

        if not self._weather_generator or not self._weather_generator.is_ready:
            channel_config = self.config.channel_map.get(channel_number)
            name = channel_config.name if channel_config else "Weather Channel"
            self._show_bumper_background(
                f"{channel_number}\n{name}\nLoading...", 5000)

            with self._lock:
                self._timer = threading.Timer(3.0, self._weather_poll)
                self._timer.daemon = True
                self._timer.start()
            return True

        segment_info = self._weather_generator.get_current_segment()
        if not segment_info:
            self._show_bumper_background(
                f"{channel_number}\nWeather Channel\nLoading...", 5000)
            with self._lock:
                self._timer = threading.Timer(3.0, self._weather_poll)
                self._timer.daemon = True
                self._timer.start()
            return True

        file_path, generation_time, segment_duration = segment_info

        elapsed = (self._now() - generation_time).total_seconds()
        seek = elapsed % segment_duration if segment_duration > 0 else 0

        self.mpv.set_volume(100)
        self.mpv._set_property("loop-file", "inf")
        success = self.mpv.play_file(str(file_path), seek_seconds=seek)
        if not success:
            print(f"Failed to play weather segment: {file_path}")
            self.mpv._set_property("loop-file", "no")
            return False

        self._show_channel_osd(channel_number)
        # Start showing the live clock (delayed so channel OSD shows first)
        self._weather_clock_timer = threading.Timer(
            self.config.playback.osd_duration + 0.5, self._show_weather_clock)
        self._weather_clock_timer.daemon = True
        self._weather_clock_timer.start()

        self._weather_current_file = file_path

        with self._lock:
            self._timer = threading.Timer(5.0, self._weather_poll)
            self._timer.daemon = True
            self._timer.start()

        if self._on_channel_change:
            self._on_channel_change(channel_number)

        return True

    def _weather_poll(self) -> None:
        """Poll for weather segment changes. Runs every 5 seconds while on weather channel.

        Also refreshes the live clock overlay each cycle.
        """
        with self._lock:
            channel = self._current_channel
            self._timer = None

        weather_ch = self.config.weather.channel_number
        if channel != weather_ch:
            self.mpv.remove_osd_overlay(self._WEATHER_CLOCK_OVERLAY_ID)
            return

        try:
            # Update the live clock overlay
            self._show_weather_clock()

            if not self._weather_generator or not self._weather_generator.is_ready:
                with self._lock:
                    self._timer = threading.Timer(3.0, self._weather_poll)
                    self._timer.daemon = True
                    self._timer.start()
                return

            segment_info = self._weather_generator.get_current_segment()
            if not segment_info:
                with self._lock:
                    self._timer = threading.Timer(3.0, self._weather_poll)
                    self._timer.daemon = True
                    self._timer.start()
                return

            file_path, generation_time, segment_duration = segment_info
            old_file = self._weather_current_file

            if old_file is None or file_path != old_file:
                print(f"  Weather: switching to new segment")
                elapsed = (self._now() - generation_time).total_seconds()
                seek = elapsed % segment_duration if segment_duration > 0 else 0

                self.mpv._set_property("loop-file", "inf")
                self.mpv.play_file(str(file_path), seek_seconds=seek)
                self._weather_current_file = file_path

            with self._lock:
                if self._current_channel == weather_ch:
                    self._timer = threading.Timer(5.0, self._weather_poll)
                    self._timer.daemon = True
                    self._timer.start()

        except Exception as e:
            print(f"Weather poll error: {e}")
            with self._lock:
                if self._current_channel == weather_ch:
                    self._timer = threading.Timer(5.0, self._weather_poll)
                    self._timer.daemon = True
                    self._timer.start()

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

        if not channel:
            return

        # Re-check under lock that the channel hasn't been changed by a
        # user-initiated tune_to() between the snapshot above and now.
        # Without this, a user channel change racing with this timer
        # would be overwritten.
        with self._lock:
            if self._current_channel != channel:
                return

        try:
            self.tune_to(channel, user_initiated=False)
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
            self._cancel_all_timers()

        self.mpv.stop()
        self._current_playing = None

    def shutdown(self) -> None:
        """Shutdown the playback engine."""
        self.stop()
        self.mpv.shutdown()
