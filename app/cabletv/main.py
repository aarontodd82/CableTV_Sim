"""Main system startup and coordination."""

import signal
import sys
import threading
from typing import Optional

from .config import load_config, Config
from .db import init_database
from .platform import ensure_directories, get_drive_root
from .schedule.engine import ScheduleEngine
from .playback.engine import PlaybackEngine
from .interface.web import run_server


class CableTVSystem:
    """
    Main CableTV system coordinator.

    Manages startup, shutdown, and coordination of all components.
    """

    def __init__(self, config: Optional[Config] = None):
        self.config = config or load_config()
        self.schedule: Optional[ScheduleEngine] = None
        self.playback: Optional[PlaybackEngine] = None
        self.guide_generator = None
        self._web_thread: Optional[threading.Thread] = None
        self._shutdown_event = threading.Event()

    def initialize(self) -> bool:
        """
        Initialize all system components.

        Returns:
            True if initialization successful
        """
        print("Initializing CableTV Simulator...")

        # Ensure directories exist
        ensure_directories()
        print(f"  Root directory: {get_drive_root()}")

        # Initialize database
        init_database()
        print("  Database initialized")

        # Create schedule engine
        self.schedule = ScheduleEngine(self.config)
        print("  Schedule engine ready")

        # Create playback engine
        self.playback = PlaybackEngine(self.config, self.schedule)
        print("  Playback engine ready")

        # Create guide generator if enabled
        if self.config.guide.enabled:
            from .guide.generator import GuideGenerator
            self.guide_generator = GuideGenerator(self.config, self.schedule)
            self.playback.set_guide_generator(self.guide_generator)
            print("  Guide generator ready")

        return True

    def start_playback(self, fullscreen: bool = True) -> bool:
        """
        Start the mpv playback engine.

        Args:
            fullscreen: Start in fullscreen mode

        Returns:
            True if started successfully
        """
        if not self.playback:
            print("Error: System not initialized")
            return False

        print("Starting playback engine...")
        if not self.playback.start(fullscreen=fullscreen):
            print("Failed to start playback engine")
            return False

        # Tune to default channel
        default_channel = self.config.playback.default_channel
        print(f"Tuning to channel {default_channel}...")
        self.playback.tune_to(default_channel)

        return True

    def start_web_server(self) -> bool:
        """
        Start the web interface in a background thread.

        Returns:
            True if started
        """
        if not self.playback or not self.schedule:
            print("Error: System not initialized")
            return False

        print(f"Starting web interface on http://{self.config.web.host}:{self.config.web.port}")

        def run_web():
            try:
                run_server(self.config, self.schedule, self.playback)
            except Exception as e:
                print(f"Web server error: {e}")

        self._web_thread = threading.Thread(target=run_web, daemon=True)
        self._web_thread.start()

        return True

    def wait_for_shutdown(self) -> None:
        """Wait for shutdown signal."""
        print("\nCableTV Simulator running. Press Ctrl+C to stop.")

        try:
            # Wait for shutdown event or keyboard interrupt
            while not self._shutdown_event.is_set():
                self._shutdown_event.wait(timeout=1.0)
        except KeyboardInterrupt:
            print("\nShutdown requested...")

    def shutdown(self) -> None:
        """Shutdown all components."""
        print("Shutting down CableTV Simulator...")

        self._shutdown_event.set()

        if self.guide_generator:
            self.guide_generator.stop()

        if self.playback:
            self.playback.shutdown()
            print("  Playback engine stopped")

        print("Shutdown complete")

    def run(self, fullscreen: bool = True, no_web: bool = False) -> int:
        """
        Run the complete CableTV system.

        Args:
            fullscreen: Start in fullscreen mode
            no_web: Don't start web interface

        Returns:
            Exit code (0 for success)
        """
        # Setup signal handlers
        def signal_handler(sig, frame):
            self._shutdown_event.set()

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        try:
            # Initialize
            if not self.initialize():
                return 1

            # Start playback
            if not self.start_playback(fullscreen=fullscreen):
                return 1

            # Start web server
            if not no_web:
                self.start_web_server()

            # Start guide generator
            if self.guide_generator:
                self.guide_generator.start()

            # Wait for shutdown
            self.wait_for_shutdown()

            return 0

        finally:
            self.shutdown()


def start_system(fullscreen: bool = True, no_web: bool = False) -> int:
    """
    Convenience function to start the CableTV system.

    Args:
        fullscreen: Start in fullscreen mode
        no_web: Don't start web interface

    Returns:
        Exit code
    """
    system = CableTVSystem()
    return system.run(fullscreen=fullscreen, no_web=no_web)


def quick_test() -> None:
    """Quick test of system components without starting playback."""
    print("CableTV Simulator - Quick Test")
    print("=" * 40)

    # Initialize
    ensure_directories()
    init_database()
    config = load_config()

    print(f"\nRoot: {get_drive_root()}")
    print(f"Channels configured: {len(config.channels)}")

    for ch in config.channels:
        print(f"  {ch.number}: {ch.name}")

    # Create schedule engine and test
    schedule = ScheduleEngine(config)

    print("\nSchedule Test - What's On Now:")
    for ch in config.channels[:3]:  # First 3 channels
        now_playing = schedule.what_is_on(ch.number)
        if now_playing:
            print(f"  Ch {ch.number}: {now_playing.entry.title}")
        else:
            print(f"  Ch {ch.number}: No content")

    print("\nQuick test complete!")


if __name__ == "__main__":
    # Run quick test if executed directly
    quick_test()
