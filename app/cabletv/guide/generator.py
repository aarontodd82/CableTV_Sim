"""Background generator for TV Guide channel segments."""

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


class GuideGenerator:
    """
    Background generator for TV Guide channel video segments.

    Generates pre-rendered video files combining:
    - Top half (640x288): Promo clips alternating with branded title cards
    - Bottom half (640x192): Scrolling Prevue-style channel grid

    The generator runs in a background thread, producing segments
    periodically. The playback engine just plays the current segment file.
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

        # A/B alternation — never overwrite the file mpv is playing
        self._next_slot = "a"  # Which filename to write next

        # Output directory
        paths = get_content_paths()
        self._output_dir = paths["guide_segments"]
        self._output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def is_ready(self) -> bool:
        """Check if a segment is available for playback."""
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
        with self._lock:
            if self._current_segment and self._current_segment.exists():
                return (
                    self._current_segment,
                    self._generation_time,
                    self._segment_duration,
                )
            return None

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

        Timing strategy: after each generation completes, calculate how
        much playback time remains in the current segment and sleep for
        half of that. This ensures the next segment is ready well before
        the current one runs out, regardless of how long generation takes.
        """
        try:
            # First: generate a short segment for fast startup
            print("  Guide: generating initial short segment...")
            self._generate_segment(short=True)

            # Then continuously regenerate full segments
            while not self._stop_event.is_set():
                print("  Guide: generating full segment...")
                gen_start = _time.monotonic()
                self._generate_segment(short=False)
                gen_elapsed = _time.monotonic() - gen_start

                # Wait until roughly halfway through the new segment's
                # playback before starting the next generation cycle.
                # This gives plenty of buffer for the next generation.
                remaining_playback = max(0, self._segment_duration - gen_elapsed)
                wait_time = max(30, remaining_playback * 0.5)

                self._stop_event.wait(timeout=wait_time)

        except Exception as e:
            print(f"  Guide generator error: {e}")
            import traceback
            traceback.print_exc()

    def _generate_segment(self, short: bool = False) -> bool:
        """
        Generate a single guide segment through the 3-phase pipeline.

        Phase 1: Generate promo video (top 640x288)
        Phase 2: Generate grid video (bottom 640x192)
        Phase 3: Composite into final 640x480 segment with audio

        Args:
            short: If True, generate a shorter segment (~2 min) for fast startup

        Returns:
            True if successful
        """
        gc = self.guide_config
        duration = 120 if short else gc.segment_duration
        fps = gc.fps
        generation_time = datetime.now()

        work_dir = Path(tempfile.mkdtemp(prefix="guide_"))

        try:
            # Phase 1: Promo video (top half)
            promo_path = work_dir / "promo.mp4"
            print("    Phase 1: Selecting promo content...")
            promos = select_promo_content(self.schedule, gc)
            print(f"    Phase 1: Generating promo video ({len(promos)} promos)...")
            if not generate_promo_video(promos, promo_path, duration, gc, work_dir):
                print("    Warning: Promo video generation failed, using fallback")
                # Generate a plain title card as fallback
                from .promos import generate_music_gap
                if not generate_music_gap(promo_path, duration, gc):
                    print("    Error: Fallback also failed")
                    return False

            # Phase 2: Grid video (bottom half)
            grid_path = work_dir / "grid.mp4"
            print("    Phase 2: Generating scrolling grid video...")
            if not self._generate_grid_video(grid_path, duration, fps, generation_time):
                print("    Error: Grid video generation failed")
                return False

            # Phase 3: Composite
            final_path = work_dir / "guide_segment.mp4"
            print("    Phase 3: Compositing final segment...")
            if not self._composite_segment(promo_path, grid_path, final_path, duration):
                print("    Error: Composite failed")
                return False

            # A/B swap: write to the INACTIVE slot so mpv's current
            # file is never touched.  On the next tune/re-tune the
            # playback engine will pick up the new file.
            slot = self._next_slot
            output_path = self._output_dir / f"segment_{slot}.mp4"

            # Copy to output (can't rename across drives/filesystems)
            shutil.copy2(str(final_path), str(output_path))

            old_segment = None
            with self._lock:
                old_segment = self._current_segment
                self._current_segment = output_path
                self._generation_time = generation_time
                self._segment_duration = duration
                # Alternate for next time
                self._next_slot = "b" if slot == "a" else "a"

            self._ready_event.set()

            # Clean up previous segment file (the one mpv is NOT playing
            # anymore after the pointer swap).  On Windows the delete may
            # fail if mpv still has the handle open; that's fine — it'll
            # be overwritten next cycle anyway.
            if old_segment and old_segment != output_path and old_segment.exists():
                try:
                    old_segment.unlink()
                except OSError:
                    pass  # File still in use — will be cleaned up next cycle

            seg_type = "short" if short else "full"
            print(f"    Guide segment ready ({seg_type}, {duration}s)")
            return True

        except Exception as e:
            print(f"    Guide segment generation error: {e}")
            import traceback
            traceback.print_exc()
            return False
        finally:
            # Clean up work directory
            try:
                shutil.rmtree(work_dir, ignore_errors=True)
            except OSError:
                pass

    def _generate_grid_video(
        self,
        output_path: Path,
        duration: float,
        fps: int,
        generation_time: datetime,
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
                    start_time=generation_time,
                    end_time=generation_time + timedelta(hours=3),
                    duration_seconds=10800, file_path="",
                    channel_number=ch.number,
                    slot_end_time=generation_time + timedelta(hours=3),
                )]
            else:
                regular_channels.append(ch.number)

        # Get full schedule data for regular channels
        if regular_channels:
            schedule_data = self.schedule.get_guide_data(
                start_time=generation_time,
                hours=2,
                channels=regular_channels,
            )
            guide_data.update(schedule_data)

        # Build channel configs dict for renderer
        channel_configs = self.config.channel_map

        # Render the full strip (done once) — 1.5 hours = 3 time slots
        strip = renderer.render_full_strip(
            guide_data=guide_data,
            start_time=generation_time,
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

            for frame_num in range(total_frames):
                if self._stop_event.is_set():
                    proc.kill()
                    return False

                t = frame_num / fps
                scroll_offset = t * scroll_px_per_sec

                frame = renderer.get_frame_at_offset(strip, scroll_offset)
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
            # Complex audio: mix background music with promo audio
            # Background music loops, volume ducks during promo audio
            cmd = [
                ffmpeg,
                "-i", str(promo_path),       # input 0: promo video + audio
                "-i", str(grid_path),         # input 1: grid video (no audio)
                "-stream_loop", "-1",
                "-i", bg_music,               # input 2: background music (loops)
                "-filter_complex",
                (
                    # Stack videos vertically
                    f"[0:v]scale={gc.width}:{gc.promo_height}[top];"
                    f"[1:v]scale={gc.width}:{gc.grid_height}[bottom];"
                    "[top][bottom]vstack=inputs=2[v];"
                    # Audio: mix promo audio with background music
                    # Duck bg music volume when promo audio is present
                    "[0:a]volume=1.0[promo_a];"
                    f"[2:a]atrim=0:{duration},volume=0.4[bg_a];"
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
            # Simple: just stack videos, use promo audio
            cmd = [
                ffmpeg,
                "-i", str(promo_path),       # input 0: promo video + audio
                "-i", str(grid_path),         # input 1: grid video (no audio)
                "-filter_complex",
                (
                    f"[0:v]scale={gc.width}:{gc.promo_height}[top];"
                    f"[1:v]scale={gc.width}:{gc.grid_height}[bottom];"
                    "[top][bottom]vstack=inputs=2[v]"
                ),
                "-map", "[v]",
                "-map", "0:a?",  # Use promo audio if present
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
        return self._generate_segment(short=short)
