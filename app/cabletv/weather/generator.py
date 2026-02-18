"""Background generator for Weather Channel segments."""

import shutil
import subprocess
import tempfile
import threading
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..config import Config, WeatherConfig
from ..platform import get_content_paths, get_drive_root, get_ffmpeg_path
from .api import WeatherAPI
from .renderer import WeatherRenderer, NUM_PAGES, TICKER_HEIGHT


class WeatherGenerator:
    """
    Background generator for Weather Channel video segments.

    Generates pre-rendered video files cycling through weather data pages
    with a scrolling bottom ticker and optional smooth jazz background music.

    Uses A/B slot file swapping (same pattern as GuideGenerator).
    """

    def __init__(self, config: Config):
        self.config = config
        self.weather_config = config.weather

        self._api = WeatherAPI(self.weather_config)
        self._renderer = WeatherRenderer(
            width=self.weather_config.width,
            height=self.weather_config.height,
        )
        # Set location name on renderer for brand bar
        self._renderer._location_name = self.weather_config.location_name

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

        # A/B alternation
        self._next_slot = "a"

        # Output directory
        paths = get_content_paths()
        self._output_dir = paths["weather_segments"]
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

    def _try_swap_pending(self) -> None:
        """Swap pending segment into current if ready."""
        with self._lock:
            if self._pending_segment and self._pending_time:
                if datetime.now() >= self._pending_time:
                    old = self._current_segment
                    self._current_segment = self._pending_segment
                    self._generation_time = self._pending_time
                    self._segment_duration = self._pending_duration
                    self._pending_segment = None
                    self._pending_time = None
                    self._pending_duration = 0
                    self._ready_event.set()

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
            name="weather-generator",
            daemon=True,
        )
        self._thread.start()
        print("  Weather generator started")

    def stop(self) -> None:
        """Stop the background generation thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        print("  Weather generator stopped")

    def _generation_loop(self) -> None:
        """Main generation loop running in background thread."""
        try:
            # Generate first segment immediately
            print("  Weather: generating initial segment...")
            self._generate_segment(swap_immediately=True)

            # Then continuously regenerate at refresh_interval
            while not self._stop_event.is_set():
                self._stop_event.wait(timeout=self.weather_config.refresh_interval)
                if self._stop_event.is_set():
                    break

                print("  Weather: regenerating segment...")
                self._generate_segment(swap_immediately=True)

        except Exception as e:
            print(f"  Weather generator error: {e}")
            import traceback
            traceback.print_exc()

    def _generate_segment(self, swap_immediately: bool = True) -> bool:
        """
        Generate a single weather segment.

        Pipeline:
        1. Fetch weather data
        2. Render all page frames, piping to FFmpeg (video-only)
        3. If background music configured, mux it in a second FFmpeg pass

        Returns:
            True if successful
        """
        wc = self.weather_config
        duration = wc.segment_duration
        fps = wc.fps
        target_time = datetime.now()

        work_dir = Path(tempfile.mkdtemp(prefix="weather_"))

        try:
            # Fetch weather data
            print("    Fetching weather data...")
            weather = self._api.get_weather()
            if not weather:
                print("    Error: No weather data available")
                return False

            # Fetch regional temps and radar in parallel-ish
            regional_temps = self._api.get_regional_temps()
            radar_image = self._api.get_radar_image()

            # Phase 1: Render frames to video
            video_path = work_dir / "weather_video.mp4"
            print(f"    Rendering {duration}s of weather pages ({fps} fps)...")
            if not self._render_video(
                video_path, weather, duration, fps,
                radar_image=radar_image,
                regional_temps=regional_temps,
            ):
                print("    Error: Video rendering failed")
                return False

            # Phase 2: Add background music if configured
            bg_music = wc.background_music
            has_music = bool(bg_music) and Path(bg_music).exists()

            if has_music:
                final_path = work_dir / "weather_final.mp4"
                print("    Adding background music...")
                if not self._add_music(video_path, final_path, bg_music, duration):
                    print("    Warning: Music mux failed, using video-only")
                    final_path = video_path
            else:
                final_path = video_path

            # A/B swap: write to the INACTIVE slot
            slot = self._next_slot
            output_path = self._output_dir / f"weather_{slot}.mp4"
            shutil.copy2(str(final_path), str(output_path))

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

            if swap_immediately and old and old != output_path and old.exists():
                try:
                    old.unlink()
                except OSError:
                    pass

            print(f"    Weather segment ready ({duration}s)")
            return True

        except Exception as e:
            print(f"    Weather segment generation error: {e}")
            import traceback
            traceback.print_exc()
            return False
        finally:
            try:
                shutil.rmtree(work_dir, ignore_errors=True)
            except OSError:
                pass

    def _render_video(
        self,
        output_path: Path,
        weather,
        duration: float,
        fps: int,
        radar_image=None,
        regional_temps=None,
    ) -> bool:
        """Render weather page frames and pipe to FFmpeg."""
        wc = self.weather_config
        page_duration = wc.page_duration
        total_frames = int(duration * fps)
        ticker_speed = 80.0  # pixels per second

        # Pre-calculate ticker loop width
        ticker_width = self._renderer.get_ticker_text_width(weather)
        if ticker_width <= 0:
            ticker_width = 2000

        ffmpeg = get_ffmpeg_path()
        cmd = [
            ffmpeg,
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "-s", f"{wc.width}x{wc.height}",
            "-r", str(fps),
            "-i", "pipe:0",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "26",
            "-pix_fmt", "yuv420p",
            "-an",
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

                # Which page are we on?
                page_index = int(t / page_duration) % NUM_PAGES

                # Ticker scroll offset (loops based on text width)
                ticker_offset = (t * ticker_speed) % ticker_width

                frame = self._renderer.render_frame(
                    weather=weather,
                    page_index=page_index,
                    ticker_offset=ticker_offset,
                    radar_image=radar_image,
                    regional_temps=regional_temps,
                )

                proc.stdin.write(frame.tobytes())

            proc.stdin.close()
            proc.wait(timeout=600)
            return proc.returncode == 0 and output_path.exists()

        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            print(f"    Video rendering error: {e}")
            return False

    def _add_music(
        self,
        video_path: Path,
        output_path: Path,
        music_path: str,
        duration: float,
    ) -> bool:
        """Add background music to video (copy video, loop audio)."""
        ffmpeg = get_ffmpeg_path()
        cmd = [
            ffmpeg,
            "-i", str(video_path),
            "-stream_loop", "-1",
            "-i", music_path,
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "128k",
            "-t", str(duration),
            "-map", "0:v",
            "-map", "1:a",
            "-shortest",
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
                print(f"    Music mux error: {stderr}")
            return result.returncode == 0 and output_path.exists()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            print(f"    Music mux error: {e}")
            return False

    def generate_once(self) -> bool:
        """
        Generate a single segment synchronously (for CLI testing).

        Returns:
            True if successful
        """
        return self._generate_segment(swap_immediately=True)
