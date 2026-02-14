"""mpv IPC controller using TCP socket."""

import json
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional, Any

from ..config import Config
from ..platform import get_mpv_path, get_mpv_ipc_connect, configure_display


class MpvController:
    """
    Controller for mpv media player via IPC.

    Uses TCP socket for cross-platform compatibility.
    """

    def __init__(self, config: Config):
        self.config = config
        self.host, self.port = get_mpv_ipc_connect()
        self._process: Optional[subprocess.Popen] = None
        self._socket: Optional[socket.socket] = None
        self._request_id = 0

    @property
    def is_running(self) -> bool:
        """Check if mpv is running."""
        return self._process is not None and self._process.poll() is None

    @property
    def is_connected(self) -> bool:
        """Check if connected to mpv IPC."""
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

        cmd = [
            mpv_path,
            f"--input-ipc-server=tcp://127.0.0.1:{self.port}",
            "--idle=yes",
            "--force-window=yes",
            "--keep-open=yes",
            "--osd-level=1",
            "--osd-duration=2000",
        ]

        # Add fullscreen if requested
        if fullscreen:
            cmd.append("--fullscreen")

        # Add platform-specific options
        if display_config.get("video_output"):
            cmd.append(f"--vo={display_config['video_output']}")
        if display_config.get("hwdec"):
            cmd.append(f"--hwdec={display_config['hwdec']}")

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
        """Connect to mpv IPC socket."""
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.settimeout(5.0)
            self._socket.connect((self.host, self.port))
            return True
        except (socket.error, ConnectionRefusedError):
            self._socket = None
            return False

    def _send_command(self, command: list, wait_response: bool = True) -> Optional[dict]:
        """
        Send a command to mpv via IPC.

        Args:
            command: Command as list (e.g., ["loadfile", "/path/to/file"])
            wait_response: Wait for response

        Returns:
            Response dict or None
        """
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
            self._socket.sendall(message.encode("utf-8"))

            if wait_response:
                # Read response
                response_data = b""
                while True:
                    chunk = self._socket.recv(4096)
                    if not chunk:
                        break
                    response_data += chunk
                    if b"\n" in response_data:
                        break

                # Parse response (may have multiple lines)
                for line in response_data.decode("utf-8").strip().split("\n"):
                    if line:
                        try:
                            response = json.loads(line)
                            if response.get("request_id") == self._request_id:
                                return response
                        except json.JSONDecodeError:
                            continue

            return None

        except socket.error as e:
            print(f"IPC error: {e}")
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

    def play_file(self, path: str, seek_seconds: float = 0) -> bool:
        """
        Load and play a file.

        Args:
            path: Path to the video file
            seek_seconds: Position to seek to after loading

        Returns:
            True if successful
        """
        # Load the file
        response = self._send_command(["loadfile", path, "replace"])
        if response is None:
            return False

        # Wait a moment for file to load
        time.sleep(0.3)

        # Seek if needed
        if seek_seconds > 0:
            self.seek(seek_seconds)

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
        if self._socket:
            try:
                self._send_command(["quit"], wait_response=False)
            except Exception:
                pass
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
