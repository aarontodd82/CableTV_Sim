"""mpv IPC controller using named pipes (Windows) or TCP socket (Linux/Mac)."""

import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Any

from ..config import Config
from ..platform import get_mpv_path, get_mpv_ipc_address, configure_display


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
            "--af=loudnorm=I=-24:TP=-2:LRA=11",
            f"--script={autocrop_script}",
            f"--script={keybinds_script}",
            f"--script-opts=cabletv-port={self.config.web.port}",
            # Cache settings for network share playback
            "--cache=yes",
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

        # Clean up stale Unix socket file from previous run
        if self._use_unix_socket:
            sock_path = Path(self._ipc_address)
            try:
                if sock_path.exists():
                    sock_path.unlink()
            except PermissionError:
                import os
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
            self._socket.settimeout(5.0)
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
                            chunk = self._socket.recv(4096)
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
                  audio_file: Optional[str] = None) -> bool:
        """
        Load and play a file.

        Args:
            path: Path to the video file
            seek_seconds: Position to seek to after loading
            audio_file: Optional audio file to play alongside (e.g. for images)

        Returns:
            True if successful
        """
        # Build options — use start= to seek during load (no flash of position 0)
        options_parts = []
        if seek_seconds > 0:
            options_parts.append(f"start={seek_seconds}")
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

        # Wait for file to load (poll at 50ms intervals)
        for _ in range(40):  # Up to 2 seconds
            time.sleep(0.05)
            pos = self._get_property("time-pos")
            if pos is not None:
                break

        # Ensure playback is not paused — keep-open=yes leaves mpv paused
        # when a file ends, and loadfile may inherit that paused state
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

    def shutdown(self) -> None:
        """Shutdown mpv completely."""
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
