"""Direct keyboard input for Linux DRM mode using evdev.

On Linux, mpv in DRM mode launched as a subprocess doesn't receive keyboard
input (only the foreground process group gets TTY input). This module reads
key events directly from /dev/input/ via evdev and sends the same API calls
that keybinds.lua handles on Windows.

Only imported/used on Linux — Windows uses mpv's built-in terminal input.
"""

import threading

import evdev
import requests


def _find_keyboard() -> evdev.InputDevice | None:
    """Find the keyboard device that actually produces key events.

    Some keyboards (e.g. Razer) split across multiple /dev/input/ devices.
    The one named "Keyboard" may not be the one that produces actual key
    events. We check all devices with letter key capabilities, preferring
    non-mouse devices that look like real keyboards.
    """
    devices = [evdev.InputDevice(path) for path in evdev.list_devices()]

    # First pass: non-Mouse, non-"Keyboard" suffix devices with letter keys.
    # Many USB keyboards expose the real key events on a device WITHOUT
    # "Keyboard" in the name (e.g. "Razer Razer BlackWidow V3 Mini").
    for dev in devices:
        if "Mouse" in dev.name or "HDMI" in dev.name:
            continue
        if "Keyboard" in dev.name:
            continue  # Skip the "Keyboard" sub-device for now
        caps = dev.capabilities()
        key_caps = caps.get(evdev.ecodes.EV_KEY, [])
        if evdev.ecodes.KEY_A in key_caps and len(key_caps) > 50:
            return dev

    # Second pass: devices with "Keyboard" in name (excluding mice)
    for dev in devices:
        if "Keyboard" in dev.name and "Mouse" not in dev.name:
            caps = dev.capabilities()
            key_caps = caps.get(evdev.ecodes.EV_KEY, [])
            if evdev.ecodes.KEY_A in key_caps:
                return dev

    # Third pass: any device with arrow keys and digit keys
    for dev in devices:
        caps = dev.capabilities()
        key_caps = caps.get(evdev.ecodes.EV_KEY, [])
        if evdev.ecodes.KEY_UP in key_caps and evdev.ecodes.KEY_0 in key_caps:
            return dev

    return None


class LinuxKeyboardListener:
    """Reads keyboard events via evdev, sends API calls to Flask."""

    # evdev key codes for digits and controls
    _DIGIT_KEYS = {
        evdev.ecodes.KEY_0: "0", evdev.ecodes.KEY_1: "1",
        evdev.ecodes.KEY_2: "2", evdev.ecodes.KEY_3: "3",
        evdev.ecodes.KEY_4: "4", evdev.ecodes.KEY_5: "5",
        evdev.ecodes.KEY_6: "6", evdev.ecodes.KEY_7: "7",
        evdev.ecodes.KEY_8: "8", evdev.ecodes.KEY_9: "9",
    }

    def __init__(self, api_port: int = 5000, mpv_controller=None):
        self._base_url = f"http://127.0.0.1:{api_port}"
        self._mpv = mpv_controller
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._digit_buffer = ""
        self._digit_timer: threading.Timer | None = None
        self._digit_timeout = 1.5

    def start(self) -> bool:
        """Start listening for keyboard events in a background thread."""
        device = _find_keyboard()
        if not device:
            print("  Warning: No keyboard found for direct input")
            return False

        print(f"  Linux keyboard input: {device.name} ({device.path})")

        self._thread = threading.Thread(
            target=self._listen, args=(device,), daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        """Stop the listener."""
        self._stop_event.set()
        if self._digit_timer:
            self._digit_timer.cancel()

    def _api_post(self, endpoint: str) -> None:
        """Fire-and-forget POST to Flask API."""
        try:
            requests.post(self._base_url + endpoint, timeout=2)
        except Exception:
            pass

    def _show_osd(self, text: str, duration_ms: int = 1500) -> None:
        """Show OSD message on mpv if available."""
        if self._mpv:
            try:
                self._mpv.show_osd_message(text, duration_ms)
            except Exception:
                pass

    def _commit_channel(self) -> None:
        """Commit buffered digits as a channel number."""
        if self._digit_timer:
            self._digit_timer.cancel()
            self._digit_timer = None
        if self._digit_buffer:
            channel = self._digit_buffer
            self._digit_buffer = ""
            self._api_post(f"/api/channel/{channel}")

    def _on_digit(self, digit: str) -> None:
        """Handle a digit keypress (channel direct tune)."""
        if self._digit_timer:
            self._digit_timer.cancel()
            self._digit_timer = None

        self._digit_buffer += digit
        self._show_osd(f"Ch {self._digit_buffer}")

        # Two digits: commit immediately (max channel is 55)
        if len(self._digit_buffer) >= 2:
            self._commit_channel()
            return

        # Single digit: wait for more
        self._digit_timer = threading.Timer(
            self._digit_timeout, self._commit_channel)
        self._digit_timer.daemon = True
        self._digit_timer.start()

    def _listen(self, device: evdev.InputDevice) -> None:
        """Main event loop — reads key events from the device."""
        try:
            # Grab exclusive access so keys don't also go to the console
            device.grab()
        except Exception:
            pass  # Non-fatal if grab fails

        try:
            for event in device.read_loop():
                if self._stop_event.is_set():
                    break

                # Only handle key-down events (value=1)
                if event.type != evdev.ecodes.EV_KEY or event.value != 1:
                    continue

                code = event.code

                if code == evdev.ecodes.KEY_UP:
                    self._api_post("/api/channel/up")
                elif code == evdev.ecodes.KEY_DOWN:
                    self._api_post("/api/channel/down")
                elif code == evdev.ecodes.KEY_I:
                    self._api_post("/api/info")
                elif code in self._DIGIT_KEYS:
                    self._on_digit(self._DIGIT_KEYS[code])
        except Exception as e:
            print(f"  Keyboard listener error: {e}")
        finally:
            try:
                device.ungrab()
            except Exception:
                pass
