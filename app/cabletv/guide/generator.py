"""Background generator for TV Guide channel segments."""

import json
import shutil
import subprocess
import tempfile
import threading
import time as _time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from ..config import Config, GuideConfig
from ..platform import get_content_paths, get_drive_root, get_ffmpeg_path
from ..schedule.engine import ScheduleEngine
from .renderer import GuideGridRenderer, ROW_HEIGHT, TIME_HEADER_HEIGHT
from .promos import select_promo_content, generate_promo_video


# Segment duration aligns to this many minutes
WINDOW_MINUTES = 10


def _get_window_start(dt: datetime) -> datetime:
    """Round down to nearest WINDOW_MINUTES boundary."""
    return dt.replace(minute=(dt.minute // WINDOW_MINUTES) * WINDOW_MINUTES,
                      second=0, microsecond=0)


def _get_next_window_start(dt: datetime) -> datetime:
    """Get the start of the next WINDOW_MINUTES window."""
    return _get_window_start(dt) + timedelta(minutes=WINDOW_MINUTES)


class GuideGenerator:
    """
    Background generator for TV Guide channel video segments.

    Generates pre-rendered video files combining:
    - Top half: Promo clips alternating with branded title cards
    - Bottom half: Scrolling Prevue-style channel grid

    Segments are aligned to 10-minute boundaries so that baked-in
    clocks (title cards, grid header) show the correct time.
    """

    def __init__(self, config: Config, schedule_engine: ScheduleEngine):
        self.config = config
        self.guide_config = config.guide
        self.schedule = schedule_engine

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._lock = threading.Lock()

        # Current segment info
        self._current_segment: Optional[Path] = None
        self._generation_time: Optional[datetime] = None
        self._segment_duration: float = 0

        # Pending segment (generated but not yet live)
        self._pending_segment: Optional[Path] = None
        self._pending_time: Optional[datetime] = None
        self._pending_duration: float = 0

        # A/B alternation — never overwrite the file mpv is playing
        self._next_slot = "a"  # Which filename to write next

        # Output directory
        paths = get_content_paths()
        self._output_dir = paths["guide_segments"]
        self._output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def is_ready(self) -> bool:
        """Check if a segment is available for playback."""
        self._try_swap_pending()
        with self._lock:
            return (
                self._current_segment is not None
                and self._current_segment.exists()
            )

    def wait_for_ready(self, timeout: float = 120) -> bool:
        """Wait for the first segment to become available."""
        return self._ready_event.wait(timeout=timeout)

    def get_current_segment(self) -> Optional[tuple[Path, datetime, float]]:
        """
        Get the current segment for playback.

        Returns:
            Tuple of (file_path, generation_time, segment_duration)
            or None if not ready
        """
        self._try_swap_pending()
        with self._lock:
            if self._current_segment and self._current_segment.exists():
                return (
                    self._current_segment,
                    self._generation_time,
                    self._segment_duration,
                )
            return None

    def get_pending_swap_time(self) -> Optional[datetime]:
        """Get the time when the pending segment will become active, or None."""
        with self._lock:
            return self._pending_time

    def _try_swap_pending(self) -> None:
        """Swap pending segment into current if its target time has arrived."""
        with self._lock:
            if (self._pending_segment
                    and self._pending_time
                    and datetime.now() >= self._pending_time):
                old = self._current_segment
                self._current_segment = self._pending_segment
                self._generation_time = self._pending_time
                self._segment_duration = self._pending_duration
                self._pending_segment = None
                self._pending_time = None
                self._pending_duration = 0
                self._ready_event.set()

                # Clean up old segment file
                if old and old != self._current_segment and old.exists():
                    try:
                        old.unlink()
                    except OSError:
                        pass

    def start(self) -> None:
        """Start the background generation thread."""
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._generation_loop,
            name="guide-generator",
            daemon=True,
        )
        self._thread.start()
        print("  Guide generator started")

    def stop(self) -> None:
        """Stop the background generation thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        print("  Guide generator stopped")

    def _generation_loop(self) -> None:
        """Main generation loop running in background thread.

        Timing strategy: segments are aligned to 10-minute boundaries.
        First segment targets the current window (swap immediately).
        Subsequent segments target the next window (swap when it arrives).
        """
        try:
            # First: generate a short segment for the current 10-min window
            # This goes live immediately so the channel isn't empty on startup
            current_window = _get_window_start(datetime.now())
            print(f"  Guide: generating initial segment for {current_window.strftime('%I:%M %p')}...")
            self._generate_segment(short=True, target_time=current_window,
                                   swap_immediately=True, show_clock=False)

            # Then continuously generate full segments for future windows
            while not self._stop_event.is_set():
                next_window = _get_next_window_start(datetime.now())
                print(f"  Guide: generating segment for {next_window.strftime('%I:%M %p')}...")
                self._generate_segment(short=False, target_time=next_window,
                                       swap_immediately=False)

                # Wait until the target time arrives, then start the next cycle
                now = datetime.now()
                wait_secs = (next_window - now).total_seconds()
                if wait_secs > 0:
                    self._stop_event.wait(timeout=wait_secs)

                # Swap the pending segment now that target time has arrived
                self._try_swap_pending()

        except Exception as e:
            print(f"  Guide generator error: {e}")
            import traceback
            traceback.print_exc()

    def _generate_segment(
        self,
        short: bool = False,
        target_time: Optional[datetime] = None,
        swap_immediately: bool = True,
        show_clock: bool = True,
    ) -> bool:
        """
        Generate a single guide segment through the 3-phase pipeline.

        Args:
            short: If True, generate a shorter segment (~2 min) for fast startup
            target_time: The time this segment represents (for baked-in clocks).
                         If None, uses datetime.now().
            swap_immediately: If True, make segment current right away.
                              If False, store as pending until target_time arrives.
            show_clock: If False, show "--:--" placeholders instead of times
                        (used for initial segment where time won't be accurate)

        Returns:
            True if successful
        """
        gc = self.guide_config
        duration = 120 if short else gc.segment_duration
        fps = gc.fps

        if target_time is None:
            target_time = datetime.now()

        # For no-clock mode, pass None as display times so renderers show "--:--"
        display_time = target_time if show_clock else None

        work_dir = Path(tempfile.mkdtemp(prefix="guide_"))

        try:
            # Phase 1: Promo video (top half)
            promo_path = work_dir / "promo.mp4"
            print("    Phase 1: Selecting promo content...")
            promos = select_promo_content(self.schedule, gc)
            print(f"    Phase 1: Generating promo video ({len(promos)} promos)...")
            if not generate_promo_video(promos, promo_path, duration, gc, work_dir,
                                        segment_start_time=display_time):
                print("    Warning: Promo video generation failed, using fallback")
                from .promos import generate_music_gap
                if not generate_music_gap(promo_path, duration, gc,
                                          display_time=display_time):
                    print("    Error: Fallback also failed")
                    return False

            # Phase 2: Grid video (bottom half)
            grid_path = work_dir / "grid.mp4"
            print("    Phase 2: Generating scrolling grid video...")
            if not self._generate_grid_video(grid_path, duration, fps, target_time,
                                             show_clock=show_clock):
                print("    Error: Grid video generation failed")
                return False

            # Phase 3: Composite
            final_path = work_dir / "guide_segment.mp4"
            print("    Phase 3: Compositing final segment...")
            if not self._composite_segment(promo_path, grid_path, final_path, duration):
                print("    Error: Composite failed")
                return False

            # A/B swap: write to the INACTIVE slot
            slot = self._next_slot
            output_path = self._output_dir / f"segment_{slot}.mp4"
            shutil.copy2(str(final_path), str(output_path))

            # Write JSON sidecar for remote clients
            sidecar_path = output_path.with_suffix(".json")
            sidecar_data = {
                "generation_time": target_time.isoformat(),
                "duration": duration,
            }
            sidecar_path.write_text(json.dumps(sidecar_data), encoding="utf-8")

            with self._lock:
                if swap_immediately:
                    old = self._current_segment
                    self._current_segment = output_path
                    self._generation_time = target_time
                    self._segment_duration = duration
                    self._ready_event.set()
                else:
                    old = None
                    self._pending_segment = output_path
                    self._pending_time = target_time
                    self._pending_duration = duration

                self._next_slot = "b" if slot == "a" else "a"

            # Clean up old segment
            if old and old != output_path and old.exists():
                try:
                    old.unlink()
                except OSError:
                    pass

            seg_type = "short" if short else "full"
            target_str = target_time.strftime("%I:%M %p").lstrip("0")
            print(f"    Guide segment ready ({seg_type}, {duration}s, target {target_str})")
            return True

        except Exception as e:
            print(f"    Guide segment generation error: {e}")
            import traceback
            traceback.print_exc()
            return False
        finally:
            try:
                shutil.rmtree(work_dir, ignore_errors=True)
            except OSError:
                pass

    def _generate_grid_video(
        self,
        output_path: Path,
        duration: float,
        fps: int,
        target_time: datetime,
        show_clock: bool = True,
    ) -> bool:
        """
        Generate the scrolling grid video by piping Pillow frames to FFmpeg.

        Renders the full channel strip once, then crops viewports at
        increasing scroll offsets and pipes raw RGB frames to FFmpeg stdin.
        """
        gc = self.guide_config
        renderer = GuideGridRenderer(gc)

        # Build guide data. Use get_guide_data for regular channels,
        # static labels for continuous channels (music etc.) which
        # would loop through hundreds of short items.
        from ..schedule.engine import ScheduleEntry
        print("      Querying schedule for grid...")

        # Separate regular vs continuous channels
        regular_channels = []
        guide_data = {}
        for ch in self.config.channels:
            if ch.number == gc.channel_number:
                continue
            if ch.commercial_ratio == 0.0:
                label = "Music Videos" if "music" in ch.content_types else ch.name
                guide_data[ch.number] = [ScheduleEntry(
                    content_id=0, title=label, content_type="music",
                    start_time=target_time,
                    end_time=target_time + timedelta(hours=3),
                    duration_seconds=10800, file_path="",
                    channel_number=ch.number,
                    slot_end_time=target_time + timedelta(hours=3),
                )]
            else:
                regular_channels.append(ch.number)

        # Get full schedule data for regular channels
        if regular_channels:
            schedule_data = self.schedule.get_guide_data(
                start_time=target_time,
                hours=2,
                channels=regular_channels,
            )
            guide_data.update(schedule_data)

        # Build channel configs dict for renderer
        channel_configs = self.config.channel_map

        # Render the full strip (done once) — 1.5 hours = 3 time slots
        strip = renderer.render_full_strip(
            guide_data=guide_data,
            start_time=target_time,
            hours=1.5,
            channel_configs=channel_configs,
            guide_channel=gc.channel_number,
        )

        # Calculate scroll parameters
        rows_area_height = strip.height - TIME_HEADER_HEIGHT
        if rows_area_height <= 0:
            rows_area_height = ROW_HEIGHT  # Fallback

        scroll_px_per_sec = ROW_HEIGHT / gc.scroll_speed  # ~14.3 px/sec
        total_frames = int(duration * fps)

        # Start FFmpeg process to receive raw RGB frames
        ffmpeg = get_ffmpeg_path()
        cmd = [
            ffmpeg,
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "-s", f"{gc.width}x{gc.grid_height}",
            "-r", str(fps),
            "-i", "pipe:0",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "28",
            "-pix_fmt", "yuv420p",
            "-an",  # No audio for grid
            "-y",
            str(output_path),
        ]

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            last_minute = -1
            frame_time = None
            placeholder = "--:-- --" if not show_clock else None

            for frame_num in range(total_frames):
                if self._stop_event.is_set():
                    proc.kill()
                    return False

                t = frame_num / fps
                scroll_offset = t * scroll_px_per_sec

                if show_clock:
                    # Update clock time only when the minute changes
                    current_minute = int(t) // 60
                    if current_minute != last_minute:
                        frame_time = target_time + timedelta(seconds=t)
                        last_minute = current_minute

                frame = renderer.get_frame_at_offset(
                    strip, scroll_offset,
                    current_time=frame_time,
                    clock_text=placeholder,
                )
                raw = frame.tobytes()
                proc.stdin.write(raw)

            proc.stdin.close()
            proc.wait(timeout=300)
            return proc.returncode == 0 and output_path.exists()

        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            print(f"    Grid video generation error: {e}")
            return False

    def _composite_segment(
        self,
        promo_path: Path,
        grid_path: Path,
        output_path: Path,
        duration: float,
    ) -> bool:
        """
        Composite promo (top) and grid (bottom) into final 640x480 segment.

        Handles audio mixing:
        - If background music is configured: loops it, fades during promos
        - Otherwise: uses promo audio only
        """
        ffmpeg = get_ffmpeg_path()
        gc = self.guide_config

        bg_music = gc.background_music
        has_bg_music = bool(bg_music) and Path(bg_music).exists()

        if has_bg_music:
            # Build volume expression that ducks music during promo clips:
            # 50% during title card gaps, fades to 15% during promos.
            # Promo/gap pattern: [promo 20s] [gap 20s] [promo 20s] ...
            pd = gc.promo_duration          # 20s per clip
            cycle = pd * 2                  # 40s full cycle
            fade = 1.5                      # fade transition duration
            low, high = 0.0, 0.5            # volume levels
            delta = high - low              # 0.35
            fu = pd - fade                  # fade-up starts before gap
            fd = cycle - fade               # fade-down starts before promo

            vol_expr = (
                f"if(lt(mod(t,{cycle}),{fu}),{low},"
                f"if(lt(mod(t,{cycle}),{pd}),"
                f"{low}+{delta}*(mod(t,{cycle})-{fu})/{fade},"
                f"if(lt(mod(t,{cycle}),{fd}),{high},"
                f"{high}-{delta}*(mod(t,{cycle})-{fd})/{fade})))"
            )

            cmd = [
                ffmpeg,
                "-i", str(promo_path),
                "-i", str(grid_path),
                "-stream_loop", "-1",
                "-i", bg_music,
                "-filter_complex",
                (
                    f"[0:v]scale={gc.width}:{gc.promo_height}[top];"
                    f"[1:v]scale={gc.width}:{gc.grid_height}[bottom];"
                    "[top][bottom]vstack=inputs=2[v];"
                    "[0:a]volume=1.0[promo_a];"
                    f"[2:a]atrim=0:{duration},volume=eval=frame:volume='{vol_expr}'[bg_a];"
                    "[promo_a][bg_a]amix=inputs=2:duration=longest:dropout_transition=2[a]"
                ),
                "-map", "[v]",
                "-map", "[a]",
                "-t", str(duration),
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "26",
                "-c:a", "aac",
                "-b:a", "128k",
                "-y",
                str(output_path),
            ]
        else:
            cmd = [
                ffmpeg,
                "-i", str(promo_path),
                "-i", str(grid_path),
                "-filter_complex",
                (
                    f"[0:v]scale={gc.width}:{gc.promo_height}[top];"
                    f"[1:v]scale={gc.width}:{gc.grid_height}[bottom];"
                    "[top][bottom]vstack=inputs=2[v]"
                ),
                "-map", "[v]",
                "-map", "0:a?",
                "-t", str(duration),
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "26",
                "-c:a", "aac",
                "-b:a", "128k",
                "-y",
                str(output_path),
            ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=300,
            )
            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="replace")[-500:]
                print(f"    Composite FFmpeg error: {stderr}")
            return result.returncode == 0 and output_path.exists()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            print(f"    Composite error: {e}")
            return False

    def generate_once(self, short: bool = False) -> bool:
        """
        Generate a single segment synchronously (for CLI testing).

        Args:
            short: Generate a shorter segment

        Returns:
            True if successful
        """
        target = _get_window_start(datetime.now())
        return self._generate_segment(short=short, target_time=target,
                                      swap_immediately=True)
