"""mpv IPC controller using named pipes (Windows) or TCP socket (Linux/Mac)."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Any, Callable

from ..config import Config
from ..platform import get_mpv_path, get_mpv_ipc_address, configure_display, is_pi


class MpvController:
    """
    Controller for mpv media player via IPC.

    Uses named pipes on Windows, TCP socket on Linux/Mac.
    """

    def __init__(self, config: Config):
        self.config = config
        self._ipc_address = get_mpv_ipc_address()
        self._use_pipe = sys.platform == "win32"
        self._use_unix_socket = sys.platform != "win32"
        self._process: Optional[subprocess.Popen] = None
        self._socket: Optional[socket.socket] = None
        self._pipe = None  # Windows named pipe file handle
        self._request_id = 0
        self._ipc_lock = threading.Lock()  # Serialize all IPC access

        # Event listener (second IPC connection for async event reading)
        self._event_socket: Optional[socket.socket] = None
        self._event_pipe = None
        self._event_thread: Optional[threading.Thread] = None
        self._event_stop = threading.Event()
        self._event_callbacks: dict[str, list[Callable]] = {}
        self._property_callbacks: dict[str, list[Callable]] = {}
        self._next_observe_id = 1
        self._observe_id_to_name: dict[int, str] = {}  # observe_id → property name

        # Watchdog (periodic health check — IPC-free, uses event-fed position)
        self._watchdog_thread: Optional[threading.Thread] = None
        self._watchdog_stop = threading.Event()
        self._watchdog_callback: Optional[Callable] = None
        self._watchdog_last_pos: Optional[float] = None
        self._watchdog_last_pos_time: float = 0.0  # time.time() of last position update
        self._watchdog_stall_count = 0

    @property
    def is_running(self) -> bool:
        """Check if mpv is running."""
        return self._process is not None and self._process.poll() is None

    @property
    def is_connected(self) -> bool:
        """Check if connected to mpv IPC."""
        if self._use_pipe:
            return self._pipe is not None
        return self._socket is not None

    def start(self, fullscreen: bool = True) -> bool:
        """
        Start mpv in idle mode with IPC enabled.

        Args:
            fullscreen: Start in fullscreen mode

        Returns:
            True if started successfully
        """
        if self.is_running:
            return True

        mpv_path = get_mpv_path()
        display_config = configure_display()

        # Autocrop Lua script (detects and removes baked-in black bars)
        autocrop_script = Path(__file__).resolve().parent / "autocrop.lua"
        # Keyboard bindings Lua script (channel up/down, digits, info, quit)
        keybinds_script = Path(__file__).resolve().parent / "keybinds.lua"

        on_pi = is_pi()

        cmd = [
            mpv_path,
            f"--input-ipc-server={self._ipc_address}",
            "--idle=yes",
            "--force-window=yes",
            "--keep-open=yes",
            "--osd-level=1",
            "--osd-duration=2000",
            "--osd-font=VCR OSD Mono",
            "--osd-font-size=38",
            f"--script={keybinds_script}",
            f"--script-opts=cabletv-port={self.config.web.port}",
            "--cache=yes",
        ]

        if on_pi:
            # Pi: keep hr-seek off (decode from keyframe is slow on Pi),
            # but re-enable loudnorm and autocrop now that play_file
            # no longer blocks on the polling loop
            cmd += [
                "--af=loudnorm=I=-24:TP=-2:LRA=11",
                "--hr-seek=no",
                f"--script={autocrop_script}",
                "--cache-secs=3",
                "--demuxer-readahead-secs=1",
                "--cache-pause-wait=1",
            ]
        else:
            cmd += [
                "--af=loudnorm=I=-24:TP=-2:LRA=11",
                # Precise seeking — start= defaults to keyframe seek, which can
                # land 0-5s before the target.  hr-seek=yes decodes from the prior
                # keyframe to the exact frame, keeping remotes in sync.
                "--hr-seek=yes",
                f"--script={autocrop_script}",
                "--cache-secs=10",
                "--demuxer-readahead-secs=3",
            ]

        # Add fullscreen or fixed window size
        if fullscreen:
            cmd.append("--fullscreen")
            # Target a specific display if configured
            screen = self.config.playback.screen
            if screen >= 0:
                cmd.append(f"--screen={screen}")
                cmd.append(f"--fs-screen={screen}")
        else:
            cmd.append("--geometry=640x480")
            cmd.append("--autofit-smaller=640x480")

        # OSD base margin — keep text away from edges
        osd_x = 40
        osd_y = 30

        # Overscan compensation — shrink video and OSD to stay visible on CRT
        overscan = self.config.playback.overscan
        if overscan > 0:
            margin = overscan / 100.0
            for side in ("left", "right", "top", "bottom"):
                cmd.append(f"--video-margin-ratio-{side}={margin:.3f}")
            osd_x += int(margin * 1024)
            osd_y += int(margin * 768)

        cmd.append(f"--osd-margin-x={osd_x}")
        cmd.append(f"--osd-margin-y={osd_y}")

        # Add platform-specific options
        if display_config.get("video_output"):
            cmd.append(f"--vo={display_config['video_output']}")
        if display_config.get("hwdec"):
            cmd.append(f"--hwdec={display_config['hwdec']}")

        # DRM output resolution (Linux only, e.g. "1024x768")
        resolution = self.config.playback.resolution
        if resolution and display_config.get("video_output") == "drm":
            cmd.append(f"--drm-mode={resolution}")

        # Clean up stale Unix socket file from previous run
        if self._use_unix_socket:
            sock_path = Path(self._ipc_address)
            try:
                if sock_path.exists():
                    sock_path.unlink()
            except PermissionError:
                os.system(f"sudo rm -f {self._ipc_address}")
            except Exception:
                pass

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            # Wait for mpv to start and open IPC socket
            time.sleep(1.0)

            # Try to connect
            for _ in range(10):
                if self._connect():
                    return True
                time.sleep(0.5)

            print("Warning: mpv started but IPC connection failed")
            return False

        except FileNotFoundError:
            print(f"Error: mpv not found at {mpv_path}")
            return False
        except Exception as e:
            print(f"Error starting mpv: {e}")
            return False

    def _connect(self) -> bool:
        """Connect to mpv IPC."""
        if self._use_pipe:
            return self._connect_pipe()
        return self._connect_unix()

    def _connect_unix(self) -> bool:
        """Connect via Unix domain socket."""
        try:
            self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._socket.settimeout(1.0)
            self._socket.connect(self._ipc_address)
            return True
        except (socket.error, ConnectionRefusedError, FileNotFoundError):
            self._socket = None
            return False

    def _connect_pipe(self) -> bool:
        """Connect via Windows named pipe."""
        try:
            self._pipe = open(self._ipc_address, "r+b", buffering=0)
            return True
        except OSError:
            self._pipe = None
            return False

    def _read_pipe_response(self, timeout: float = 3.0) -> Optional[dict]:
        """
        Read a JSON response from the named pipe, skipping mpv event messages.

        Reads byte-by-byte (required for unbuffered named pipe) and parses
        complete JSON lines. Skips mpv event notifications that don't have
        a request_id.
        """
        deadline = time.time() + timeout
        buf = b""
        while time.time() < deadline:
            try:
                b = self._pipe.read(1)
            except OSError:
                self._pipe = None
                return None
            if not b:
                continue
            buf += b
            if b == b"\n":
                line = buf.decode("utf-8", errors="replace").strip()
                buf = b""
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Skip mpv event messages (they lack request_id)
                if "event" in data and "request_id" not in data:
                    continue
                if data.get("request_id") == self._request_id:
                    return data
        return None

    def _send_command(self, command, wait_response: bool = True) -> Optional[dict]:
        """
        Send a command to mpv via IPC.

        Thread-safe: all IPC access is serialized via _ipc_lock.

        Args:
            command: Command as list (positional args) or dict (named args).
                     Named args format: {"name": "cmd", "param": value, ...}
            wait_response: Wait for response

        Returns:
            Response dict or None
        """
        with self._ipc_lock:
            if not self.is_connected:
                if not self._connect():
                    return None

            self._request_id += 1
            request = {
                "command": command,
                "request_id": self._request_id,
            }

            try:
                message = json.dumps(request) + "\n"

                if self._use_pipe:
                    self._pipe.write(message.encode("utf-8"))
                    self._pipe.flush()
                    if wait_response:
                        return self._read_pipe_response()
                    return None
                else:
                    self._socket.sendall(message.encode("utf-8"))

                    if wait_response:
                        response_data = b""
                        while True:
                            try:
                                chunk = self._socket.recv(4096)
                            except socket.timeout:
                                # mpv is busy (loading file, buffering) —
                                # return None but keep the connection alive
                                return None
                            if not chunk:
                                break
                            response_data += chunk
                            if b"\n" in response_data:
                                break

                        for line in response_data.decode("utf-8").strip().split("\n"):
                            if line:
                                try:
                                    response = json.loads(line)
                                    if response.get("request_id") == self._request_id:
                                        return response
                                except json.JSONDecodeError:
                                    continue

                    return None

            except (socket.error, OSError) as e:
                print(f"IPC error: {e}")
                if self._use_pipe:
                    self._pipe = None
                else:
                    self._socket = None
                return None

    def _get_property(self, name: str) -> Any:
        """Get an mpv property value."""
        response = self._send_command(["get_property", name])
        if response and response.get("error") == "success":
            return response.get("data")
        return None

    def _set_property(self, name: str, value: Any) -> bool:
        """Set an mpv property value."""
        response = self._send_command(["set_property", name, value])
        return response is not None and response.get("error") == "success"

    def play_file(self, path: str, seek_seconds: float = 0,
                  end_seconds: float = 0,
                  audio_file: Optional[str] = None) -> bool:
        """
        Load and play a file.

        Args:
            path: Path to the video file
            seek_seconds: Position to seek to after loading
            end_seconds: Position to stop playback (0 = play to end of file)
            audio_file: Optional audio file to play alongside (e.g. for images)

        Returns:
            True if successful
        """
        # Build options — use start=/end= to define the exact segment.
        # mpv fires end-file with reason=eof when end= is reached.
        options_parts = []
        if seek_seconds > 0:
            options_parts.append(f"start={seek_seconds}")
        if end_seconds > 0:
            options_parts.append(f"end={end_seconds}")
        if audio_file:
            options_parts.append(f"audio-file={audio_file}")
            options_parts.append("image-display-duration=inf")
        options = ",".join(options_parts) if options_parts else ""

        if options:
            response = self._send_command(["loadfile", path, "replace", -1, options])
        else:
            response = self._send_command(["loadfile", path, "replace"])
        if response is None:
            return False

        # Unpause immediately — keep-open=yes leaves mpv paused when a
        # file ends, and loadfile may inherit that state.  mpv processes
        # commands in order, so this applies after the loadfile completes.
        # No polling needed: the event listener (eof-reached, end-file)
        # handles transitions and errors.
        self._set_property("pause", False)

        return True

    def seek(self, seconds: float, absolute: bool = True) -> bool:
        """
        Seek to a position.

        Args:
            seconds: Position in seconds
            absolute: If True, seek to absolute position; if False, seek relative

        Returns:
            True if successful
        """
        mode = "absolute" if absolute else "relative"
        response = self._send_command(["seek", str(seconds), mode])
        return response is not None

    def pause(self) -> bool:
        """Pause playback."""
        return self._set_property("pause", True)

    def resume(self) -> bool:
        """Resume playback."""
        return self._set_property("pause", False)

    def toggle_pause(self) -> bool:
        """Toggle pause state."""
        current = self._get_property("pause")
        return self._set_property("pause", not current)

    def stop(self) -> bool:
        """Stop playback (clear playlist)."""
        response = self._send_command(["stop"])
        return response is not None

    def show_osd_message(self, text: str, duration_ms: int = 2000) -> bool:
        """
        Show an OSD message.

        Args:
            text: Message to display
            duration_ms: Duration in milliseconds

        Returns:
            True if successful
        """
        response = self._send_command(["show-text", text, str(duration_ms)])
        return response is not None

    def show_osd_overlay(self, overlay_id: int, data: str,
                         res_x: int = 0, res_y: int = 0, z: int = 0) -> bool:
        """
        Show an ASS-formatted overlay on screen.

        Unlike show_osd_message, this supports full ASS override tags
        for styled text. The overlay persists until explicitly removed.

        Args:
            overlay_id: Unique ID for this overlay (0-63)
            data: ASS event text (with override tags like {\\an2\\b1})
            res_x: Virtual resolution width (0 = use display resolution)
            res_y: Virtual resolution height (0 = use display resolution)
            z: Z-order for stacking overlays

        Returns:
            True if successful
        """
        # osd-overlay requires named arguments (mpv_command_node format)
        response = self._send_command({
            "name": "osd-overlay",
            "id": overlay_id,
            "format": "ass-events",
            "data": data,
            "res_x": res_x,
            "res_y": res_y,
            "z": z,
        })
        return response is not None and response.get("error") == "success"

    def remove_osd_overlay(self, overlay_id: int) -> bool:
        """
        Remove an OSD overlay by ID.

        Args:
            overlay_id: The overlay ID to remove

        Returns:
            True if successful
        """
        response = self._send_command({
            "name": "osd-overlay",
            "id": overlay_id,
            "format": "none",
            "data": "",
        })
        return response is not None

    def get_position(self) -> Optional[float]:
        """Get current playback position in seconds."""
        return self._get_property("time-pos")

    def get_duration(self) -> Optional[float]:
        """Get current file duration in seconds."""
        return self._get_property("duration")

    def get_filename(self) -> Optional[str]:
        """Get current filename."""
        return self._get_property("filename")

    def is_paused(self) -> bool:
        """Check if playback is paused."""
        return self._get_property("pause") == True

    def set_volume(self, volume: int) -> bool:
        """Set volume (0-100)."""
        return self._set_property("volume", max(0, min(100, volume)))

    def get_volume(self) -> Optional[int]:
        """Get current volume."""
        return self._get_property("volume")

    def set_fullscreen(self, fullscreen: bool) -> bool:
        """Set fullscreen mode."""
        return self._set_property("fullscreen", fullscreen)

    def toggle_fullscreen(self) -> bool:
        """Toggle fullscreen mode."""
        current = self._get_property("fullscreen")
        return self._set_property("fullscreen", not current)

    # ── Event Listener ─────────────────────────────────────────────

    def start_event_listener(self) -> bool:
        """Open a second IPC connection and start background event reader.

        Returns True if the event connection was established. On failure
        (e.g. platform doesn't support multiple pipe clients), returns
        False and the engine falls back to timer-only transitions.
        """
        if self._event_thread and self._event_thread.is_alive():
            return True  # Already running

        self._event_stop.clear()

        # Open a second connection to the same IPC address
        if self._use_pipe:
            try:
                self._event_pipe = open(self._ipc_address, "r+b", buffering=0)
            except OSError as e:
                print(f"Event listener: pipe open failed ({e}), timer-only mode")
                return False
        else:
            try:
                self._event_socket = socket.socket(
                    socket.AF_UNIX, socket.SOCK_STREAM)
                self._event_socket.settimeout(1.0)
                self._event_socket.connect(self._ipc_address)
            except (socket.error, ConnectionRefusedError, FileNotFoundError) as e:
                print(f"Event listener: socket connect failed ({e}), timer-only mode")
                self._event_socket = None
                return False

        self._event_thread = threading.Thread(
            target=self._event_reader_loop, daemon=True,
            name="mpv-event-reader")
        self._event_thread.start()
        return True

    def on_event(self, name: str, callback: Callable) -> None:
        """Register a callback for an mpv event (e.g. 'end-file')."""
        self._event_callbacks.setdefault(name, []).append(callback)

    def observe_property(self, name: str, callback: Callable) -> None:
        """Register a callback for property changes and send observe_property.

        The callback receives (property_name, value).
        """
        self._property_callbacks.setdefault(name, []).append(callback)

        observe_id = self._next_observe_id
        self._next_observe_id += 1
        self._observe_id_to_name[observe_id] = name

        self._send_event_command(
            {"command": ["observe_property", observe_id, name]})

    def _send_event_command(self, command_dict: dict) -> None:
        """Fire-and-forget write on the event connection."""
        try:
            data = (json.dumps(command_dict) + "\n").encode("utf-8")
            if self._use_pipe and self._event_pipe:
                self._event_pipe.write(data)
                self._event_pipe.flush()
            elif self._event_socket:
                self._event_socket.sendall(data)
        except (OSError, socket.error) as e:
            print(f"Event listener: send failed ({e})")

    def _event_reader_loop(self) -> None:
        """Background thread: read JSON lines from event connection, dispatch."""
        buf = b""
        while not self._event_stop.is_set():
            try:
                chunk = self._event_read_chunk()
                if chunk is None:
                    # Connection lost — attempt reconnect after a short delay
                    if self._event_stop.is_set():
                        break
                    time.sleep(1.0)
                    if self._event_reconnect():
                        buf = b""
                        continue
                    else:
                        # Reconnect failed — keep trying
                        continue
                if not chunk:
                    continue

                buf += chunk
                # Process complete lines
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line_str = line.decode("utf-8", errors="replace").strip()
                    if not line_str:
                        continue
                    try:
                        data = json.loads(line_str)
                    except json.JSONDecodeError:
                        continue
                    self._dispatch_event(data)

            except Exception as e:
                if not self._event_stop.is_set():
                    print(f"Event reader error: {e}")
                    time.sleep(0.5)

    def _event_read_chunk(self) -> Optional[bytes]:
        """Read bytes from the event connection. Returns None on connection loss."""
        if self._use_pipe:
            if not self._event_pipe:
                return None
            try:
                b = self._event_pipe.read(1)
                if not b:
                    return b""
                return b
            except OSError:
                self._event_pipe = None
                return None
        else:
            if not self._event_socket:
                return None
            try:
                data = self._event_socket.recv(4096)
                if not data:
                    self._event_socket = None
                    return None
                return data
            except socket.timeout:
                return b""
            except socket.error:
                self._event_socket = None
                return None

    def _event_reconnect(self) -> bool:
        """Try to re-establish the event connection."""
        if self._event_stop.is_set():
            return False
        if self._use_pipe:
            try:
                self._event_pipe = open(self._ipc_address, "r+b", buffering=0)
            except OSError:
                return False
        else:
            try:
                self._event_socket = socket.socket(
                    socket.AF_UNIX, socket.SOCK_STREAM)
                self._event_socket.settimeout(1.0)
                self._event_socket.connect(self._ipc_address)
            except (socket.error, ConnectionRefusedError, FileNotFoundError):
                self._event_socket = None
                return False

        # Re-register all property observers on the new connection
        for observe_id, prop_name in self._observe_id_to_name.items():
            self._send_event_command(
                {"command": ["observe_property", observe_id, prop_name]})
        print("Event listener: reconnected")
        return True

    def _dispatch_event(self, data: dict) -> None:
        """Route a parsed JSON message to the appropriate callbacks."""
        # Property change notification
        if data.get("event") == "property-change":
            prop_name = data.get("name", "")
            value = data.get("data")
            for cb in self._property_callbacks.get(prop_name, []):
                try:
                    cb(prop_name, value)
                except Exception as e:
                    print(f"Property callback error ({prop_name}): {e}")
            return

        # Regular event
        event_name = data.get("event")
        if event_name:
            for cb in self._event_callbacks.get(event_name, []):
                try:
                    cb(data)
                except Exception as e:
                    print(f"Event callback error ({event_name}): {e}")

    def _stop_event_listener(self) -> None:
        """Stop the event reader thread and close the event connection."""
        self._event_stop.set()

        # Close event connection to unblock the reader
        if self._event_pipe:
            try:
                self._event_pipe.close()
            except Exception:
                pass
            self._event_pipe = None
        if self._event_socket:
            try:
                self._event_socket.close()
            except Exception:
                pass
            self._event_socket = None

        if self._event_thread:
            self._event_thread.join(timeout=3.0)
            self._event_thread = None

        # Clear registrations
        self._event_callbacks.clear()
        self._property_callbacks.clear()
        self._observe_id_to_name.clear()
        self._next_observe_id = 1

    # ── Watchdog ─────────────────────────────────────────────────

    def start_watchdog(self, interval: float,
                       callback: Callable[[str], None]) -> None:
        """Start a periodic health-check thread.

        The watchdog makes NO IPC calls — it only checks process.poll()
        and position data fed from the event listener (observe_property
        for time-pos). This avoids _ipc_lock contention with user commands.

        The callback is called with a reason string:
        - "process_dead" — mpv process exited unexpectedly
        - "playback_stalled" — no position update for 2+ checks

        Args:
            interval: Seconds between checks (e.g. 5.0)
            callback: Called on the watchdog thread with the reason string
        """
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return  # Already running

        self._watchdog_stop.clear()
        self._watchdog_callback = callback
        self._watchdog_last_pos = None
        self._watchdog_last_pos_time = time.time()
        self._watchdog_stall_count = 0

        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, args=(interval,),
            daemon=True, name="mpv-watchdog")
        self._watchdog_thread.start()

    def _on_position_update(self, name: str, value) -> None:
        """Called by event listener when time-pos changes.

        Updates watchdog tracking without any IPC lock contention.
        mpv coalesces property-change events, so this fires ~1/sec.
        """
        if value is not None:
            self._watchdog_last_pos = value
            self._watchdog_last_pos_time = time.time()
            self._watchdog_stall_count = 0

    def _watchdog_loop(self, interval: float) -> None:
        """Periodic health check: process alive + position fed from events.

        No IPC calls — all position data comes from observe_property
        on the event connection, avoiding _ipc_lock contention.
        """
        while not self._watchdog_stop.wait(timeout=interval):
            try:
                # Check 1: process alive (cheap, no IPC)
                if self._process and self._process.poll() is not None:
                    print("Watchdog: mpv process dead")
                    self._watchdog_last_pos = None
                    self._watchdog_stall_count = 0
                    if self._watchdog_callback:
                        self._watchdog_callback("process_dead")
                    continue

                # Check 2: position progressing (from event-fed data, no IPC)
                if not self.is_running:
                    self._watchdog_stall_count = 0
                    continue

                elapsed = time.time() - self._watchdog_last_pos_time
                if self._watchdog_last_pos is not None and elapsed > interval:
                    # No position update in over one interval — possible stall
                    self._watchdog_stall_count += 1
                    if self._watchdog_stall_count >= 2:
                        print(f"Watchdog: no position update for "
                              f"{elapsed:.0f}s (stall detected)")
                        self._watchdog_stall_count = 0
                        self._watchdog_last_pos = None
                        if self._watchdog_callback:
                            self._watchdog_callback("playback_stalled")
                else:
                    self._watchdog_stall_count = 0

            except Exception as e:
                print(f"Watchdog error: {e}")

    def reset_watchdog(self) -> None:
        """Reset watchdog stall tracking (call on intentional position changes)."""
        self._watchdog_last_pos = None
        self._watchdog_last_pos_time = time.time()
        self._watchdog_stall_count = 0

    def _stop_watchdog(self) -> None:
        """Stop the watchdog thread."""
        self._watchdog_stop.set()
        if self._watchdog_thread:
            self._watchdog_thread.join(timeout=3.0)
            self._watchdog_thread = None
        self._watchdog_callback = None

    # ── Shutdown ─────────────────────────────────────────────────

    def shutdown(self) -> None:
        """Shutdown mpv completely."""
        # Stop event listener and watchdog first
        self._stop_event_listener()
        self._stop_watchdog()

        try:
            self._send_command(["quit"], wait_response=False)
        except Exception:
            pass

        if self._pipe:
            try:
                self._pipe.close()
            except Exception:
                pass
            self._pipe = None

        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None

        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None

    def __del__(self):
        """Cleanup on destruction."""
        self.shutdown()
