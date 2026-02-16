"""Platform abstraction for cross-platform compatibility."""

import os
import sys
import shutil
from pathlib import Path
from typing import Optional


def is_pi() -> bool:
    """Detect if running on Raspberry Pi."""
    if sys.platform != "linux":
        return False
    try:
        with open("/proc/cpuinfo", "r") as f:
            return "Raspberry Pi" in f.read()
    except (FileNotFoundError, PermissionError):
        return False


def get_drive_root() -> Path:
    """
    Find the CableTV drive root.

    On Windows: Look for CableTV_Sim folder in OneDrive or common locations
    On Pi: Look for mounted USB drive with CableTV label or /media/cabletv
    """
    # Check if we're running from within the project
    current = Path(__file__).resolve()
    for parent in current.parents:
        if parent.name == "CableTV_Sim" and (parent / "config.yaml").exists():
            return parent

    if sys.platform == "win32":
        # Windows: check common locations
        candidates = [
            Path(os.environ.get("USERPROFILE", "")) / "OneDrive" / "Documents" / "CableTV_Sim",
            Path(os.environ.get("USERPROFILE", "")) / "Documents" / "CableTV_Sim",
            Path("C:/CableTV_Sim"),
        ]
        for candidate in candidates:
            if candidate.exists() and (candidate / "config.yaml").exists():
                return candidate
    else:
        # Linux/Pi: check for mounted drive or home directory
        candidates = [
            Path("/media/cabletv"),
            Path("/mnt/cabletv"),
            Path.home() / "CableTV_Sim",
        ]
        # Also check for any USB drives with CableTV in the name
        media_path = Path("/media") / os.environ.get("USER", "pi")
        if media_path.exists():
            for mount in media_path.iterdir():
                if "cabletv" in mount.name.lower():
                    candidates.insert(0, mount)

        for candidate in candidates:
            if candidate.exists() and (candidate / "config.yaml").exists():
                return candidate

    # Default to parent of app directory
    app_dir = Path(__file__).resolve().parent.parent.parent
    return app_dir.parent


def get_mpv_ipc_address() -> str:
    """
    Get mpv IPC address.
    Windows: named pipe, Linux/Mac: TCP socket.
    """
    if sys.platform == "win32":
        return r"\\.\pipe\cabletv-mpv"
    return "tcp://127.0.0.1:9876"


def get_mpv_ipc_connect() -> tuple[str, int]:
    """Get host and port for connecting to mpv IPC (TCP only, used on Linux/Mac)."""
    return ("127.0.0.1", 9876)


def get_ffmpeg_path() -> str:
    """Get path to ffmpeg executable."""
    # Check if ffmpeg is in PATH
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg

    # Windows: check common installation locations
    if sys.platform == "win32":
        common_paths = [
            Path(os.environ.get("PROGRAMFILES", "")) / "ffmpeg" / "bin" / "ffmpeg.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "ffmpeg" / "bin" / "ffmpeg.exe",
            Path("C:/ffmpeg/bin/ffmpeg.exe"),
        ]
        for path in common_paths:
            if path.exists():
                return str(path)

    # Return default and let it fail with a clear error if not found
    return "ffmpeg"


def get_ffprobe_path() -> str:
    """Get path to ffprobe executable."""
    # Check if ffprobe is in PATH
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        return ffprobe

    # Windows: check common installation locations
    if sys.platform == "win32":
        common_paths = [
            Path(os.environ.get("PROGRAMFILES", "")) / "ffmpeg" / "bin" / "ffprobe.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "ffmpeg" / "bin" / "ffprobe.exe",
            Path("C:/ffmpeg/bin/ffprobe.exe"),
        ]
        for path in common_paths:
            if path.exists():
                return str(path)

    return "ffprobe"


def get_mpv_path() -> str:
    """Get path to mpv executable."""
    mpv = shutil.which("mpv")
    if mpv:
        return mpv

    if sys.platform == "win32":
        common_paths = [
            Path(r"C:\Users\Aaron\Downloads\mpv-x86_64-20260214-git-1a160f9\installer\mpv.exe"),
            Path(os.environ.get("PROGRAMFILES", "")) / "mpv" / "mpv.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "mpv" / "mpv.exe",
            Path("C:/mpv/mpv.exe"),
        ]
        for path in common_paths:
            if path.exists():
                return str(path)

    return "mpv"


def configure_display() -> dict:
    """
    Configure display settings for the platform.
    Returns dict with display configuration.
    """
    config = {
        "fullscreen": True,
        "video_output": "auto",
        "hwdec": "auto",
    }

    if is_pi():
        # Raspberry Pi specific settings for CRT output
        config.update({
            "video_output": "drm",  # Direct rendering for Pi
            "hwdec": "v4l2m2m",     # Hardware decoding on Pi
            "gpu_context": "drm",
        })
    elif sys.platform == "win32":
        config.update({
            "video_output": "gpu",
            "hwdec": "auto-safe",
        })

    return config


def get_content_paths(root: Optional[Path] = None) -> dict[str, Path]:
    """Get standard content directory paths."""
    if root is None:
        root = get_drive_root()

    return {
        "content_originals": root / "content" / "originals",
        "content_normalized": root / "content" / "normalized",
        "commercials_originals": root / "commercials" / "originals",
        "commercials_normalized": root / "commercials" / "normalized",
        "guide_segments": root / "guide",
        "logs": root / "logs",
    }


def ensure_directories(root: Optional[Path] = None) -> None:
    """Ensure all required directories exist."""
    paths = get_content_paths(root)
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
