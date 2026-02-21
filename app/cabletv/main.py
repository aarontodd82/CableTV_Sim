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
        self.weather_generator = None
        self._web_thread: Optional[threading.Thread] = None
        self._shutdown_event = threading.Event()
        self._server_manager = None  # ServerScheduleManager (server mode only)
        self._advertiser = None  # mDNS advertiser (server mode only)
        self._server_connection = None  # ServerConnection (remote mode only)

    def initialize(self) -> bool:
        """
        Initialize all system components.

        Returns:
            True if initialization successful
        """
        mode = self.config.network.mode

        if mode == "remote":
            return self._initialize_remote()

        # Standalone or server mode
        mode_label = "Server" if mode == "server" else "Standalone"
        print(f"Initializing CableTV Simulator ({mode_label})...")

        # Ensure directories exist
        ensure_directories()
        print(f"  Root directory: {get_drive_root()}")

        # Initialize database
        init_database()
        print("  Database initialized")

        # Create schedule engine
        self.schedule = ScheduleEngine(self.config)
        print("  Schedule engine ready")

        # Server mode: wrap schedule engine with consumed-slot tracking
        if mode == "server":
            from .schedule.server_manager import ServerScheduleManager
            self._server_manager = ServerScheduleManager(self.schedule)
            print(f"  Server seed: {self.schedule.seed}")

            # Route the server's own advances through ServerScheduleManager
            # so consumed-slot tracking works when server is also a TV
            def _server_advance(channel_number, group_key, num_items,
                                preserve_block_start=None, advance_by=1):
                slot = preserve_block_start or 0
                # try_advance returns False if already consumed by another
                # client — in that case, do nothing. Block cache is
                # intentionally NOT cleared (see ScheduleEngine.advance_position).
                self._server_manager.try_advance(
                    channel_number, group_key, num_items, slot,
                    advance_by=advance_by,
                )
            self.schedule.advance_position = _server_advance

            # Show SMB share instructions on first run
            from .network.smb_instructions import should_show_instructions, print_smb_instructions
            if should_show_instructions():
                print_smb_instructions()

        # Create playback engine
        self.playback = PlaybackEngine(self.config, self.schedule)
        print("  Playback engine ready")

        # Create guide generator if enabled
        # The guide shares the playback ScheduleEngine so it sees the exact
        # same block cache and positions — guaranteeing the guide grid matches
        # what actually plays.  Since remote clients query the server API
        # (same engine), everyone sees the same cache state.
        if self.config.guide.enabled:
            from .guide.generator import GuideGenerator
            self.guide_generator = GuideGenerator(self.config, self.schedule)
            self.playback.set_guide_generator(self.guide_generator)
            print("  Guide generator ready")

        # Create weather generator if enabled
        if self.config.weather.enabled:
            from .weather.generator import WeatherGenerator
            self.weather_generator = WeatherGenerator(self.config)
            self.playback.set_weather_generator(self.weather_generator)
            print("  Weather generator ready")

        return True

    def _initialize_remote(self) -> bool:
        """Initialize in remote mode — connect to server, use shared content.

        The server is the single source of truth for all schedule decisions.
        The remote just asks "what's on channel X?" via API calls. No local
        schedule engine, no replicated state, no sync issues.
        """
        from pathlib import Path
        from .network.client import ServerConnection
        from .network.segment_provider import RemoteSegmentProvider
        from .schedule.remote_provider import RemoteScheduleProvider

        print("Initializing CableTV Simulator (Remote)...")

        # Step 1: Connect to server
        self._server_connection = ServerConnection(self.config.network)
        print("  Discovering server...")
        if not self._server_connection.connect():
            print("Error: Could not connect to CableTV server.")
            if not self.config.network.server_url:
                print("  Hint: Set server_url in config.yaml network section,")
                print("  or ensure the server is running with --server flag.")
            return False
        print(f"  Connected to server: {self._server_connection.server_url}")

        # Step 2: Get server info (channels, config)
        server_info = self._server_connection.get_server_info()
        if not server_info:
            print("Error: Could not get server info.")
            return False
        print(f"  Server seed: {server_info['seed']}")

        # Sync schedule params from server
        if "epoch" in server_info:
            self.config.schedule.epoch = server_info["epoch"]
        if "slot_duration" in server_info:
            self.config.schedule.slot_duration = server_info["slot_duration"]

        # Step 3: Override channel config with server's channels
        from .config import ChannelConfig
        self.config.channels = [
            ChannelConfig(
                number=ch["number"],
                name=ch["name"],
                tags=ch.get("tags", []),
                content_types=ch.get("content_types", ["show", "movie"]),
                commercial_ratio=ch.get("commercial_ratio", 1.0),
            )
            for ch in server_info["channels"]
        ]
        print(f"  Synced {len(self.config.channels)} channels from server")

        # Sync guide/weather channel numbers from server
        if "guide" in server_info:
            self.config.guide.enabled = server_info["guide"].get("enabled", False)
            self.config.guide.channel_number = server_info["guide"].get("channel_number", 14)
        if "weather" in server_info:
            self.config.weather.enabled = server_info["weather"].get("enabled", False)
            self.config.weather.channel_number = server_info["weather"].get("channel_number", 26)

        # Step 4: Validate content_root (still needed for media files)
        content_root = Path(self.config.network.content_root)
        if not content_root.exists():
            print(f"Error: content_root not accessible: {content_root}")
            print("  Ensure the network share is mounted/mapped.")
            return False
        print(f"  Content root: {content_root}")

        # Ensure local directories exist for bumper background etc.
        ensure_directories()

        # Step 5: Measure clock offset for sync
        clock_offset = self._server_connection.measure_clock_offset()
        if abs(clock_offset) > 0.1:
            print(f"  Clock offset: {clock_offset:+.3f}s (adjusting)")
        else:
            print(f"  Clock offset: {clock_offset:+.3f}s (in sync)")

        # Step 6: Create remote schedule provider (thin API client)
        # No local engine — all schedule queries go to the server.
        self.schedule = RemoteScheduleProvider(
            self._server_connection.server_url,
            clock_offset=clock_offset,
            epoch=self.config.schedule.epoch,
            slot_duration=self.config.schedule.slot_duration,
        )
        print("  Schedule provider ready (server API)")

        # Step 7: Create playback engine with HTTP streaming
        media_url = f"{self._server_connection.server_url}/media"
        self.playback = PlaybackEngine(
            self.config, self.schedule, content_root=content_root,
            media_base_url=media_url, clock_offset=clock_offset,
        )
        print(f"  Playback engine ready (streaming via {media_url})")

        # Step 8: Set up guide/weather segment providers from network share
        if self.config.guide.enabled:
            guide_dir = content_root / "guide"
            if guide_dir.exists():
                guide_provider = RemoteSegmentProvider(guide_dir, prefix="segment_")
                self.playback.set_guide_generator(guide_provider)
                print("  Guide: reading from network share")
            else:
                print("  Guide: directory not found on share, skipping")

        if self.config.weather.enabled:
            weather_dir = content_root / "weather"
            if weather_dir.exists():
                weather_provider = RemoteSegmentProvider(weather_dir, prefix="weather_")
                self.playback.set_weather_generator(weather_provider)
                print("  Weather: reading from network share")
            else:
                print("  Weather: directory not found on share, skipping")

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
        if not self.schedule:
            print("Error: System not initialized")
            return False

        print(f"Starting web interface on http://{self.config.web.host}:{self.config.web.port}")

        # Register server API blueprint if in server mode
        server_manager = self._server_manager
        config = self.config

        def run_web():
            try:
                from .interface.web import create_app
                app = create_app(config, self.schedule, self.playback)

                if server_manager:
                    from .interface.server_api import register_server_api
                    register_server_api(app, config, server_manager)
                    print("  Server API endpoints registered")

                app.run(
                    host=config.web.host,
                    port=config.web.port,
                    debug=config.web.debug,
                    threaded=True,
                    use_reloader=False,
                )
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

        if self._advertiser:
            try:
                self._advertiser.stop()
                print("  mDNS advertiser stopped")
            except Exception:
                pass

        if self.guide_generator:
            self.guide_generator.stop()

        if self.weather_generator:
            self.weather_generator.stop()

        if self.playback:
            self.playback.shutdown()
            print("  Playback engine stopped")

        print("Shutdown complete")

    def run(self, fullscreen: bool = True, no_web: bool = False,
            headless: bool = False) -> int:
        """
        Run the complete CableTV system.

        Args:
            fullscreen: Start in fullscreen mode
            no_web: Don't start web interface
            headless: No video window (server only — just API + generators)

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

            # Start playback (skip in headless mode)
            if headless:
                print("Running headless (no video). Use without --headless for a window.")
            else:
                if not self.start_playback(fullscreen=fullscreen):
                    return 1

            # Start web server
            if not no_web:
                self.start_web_server()

            # Start mDNS advertiser (server mode)
            if self.config.network.mode == "server":
                try:
                    from .network.discovery import ServerAdvertiser
                    self._advertiser = ServerAdvertiser(
                        self.config.web.port,
                        self.config.network.server_name,
                    )
                    self._advertiser.start()
                except ImportError:
                    print("  Warning: zeroconf not installed, mDNS disabled")
                    print("  Install with: pip install zeroconf")
                except Exception as e:
                    print(f"  Warning: mDNS advertisement failed: {e}")

            # Start guide generator
            if self.guide_generator:
                self.guide_generator.start()

            # Start weather generator
            if self.weather_generator:
                self.weather_generator.start()

            # Wait for shutdown
            self.wait_for_shutdown()

            return 0

        finally:
            self.shutdown()


def start_system(fullscreen: bool = True, no_web: bool = False,
                  headless: bool = False, config: Optional[Config] = None) -> int:
    """
    Convenience function to start the CableTV system.

    Args:
        fullscreen: Start in fullscreen mode
        no_web: Don't start web interface
        headless: No video window (server only)
        config: Pre-loaded config (optional, loads from file if not provided)

    Returns:
        Exit code
    """
    system = CableTVSystem(config=config)
    return system.run(fullscreen=fullscreen, no_web=no_web, headless=headless)


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
