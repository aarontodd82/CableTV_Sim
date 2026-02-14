"""FFmpeg and FFprobe utility wrappers."""

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..platform import get_ffprobe_path, get_ffmpeg_path


@dataclass
class ProbeResult:
    """Result of probing a video file."""
    duration: float  # seconds
    width: int
    height: int
    aspect_ratio: str  # e.g., "16:9", "4:3"
    video_codec: str
    audio_codec: Optional[str]
    frame_rate: float
    bitrate: Optional[int]  # bits per second


def probe_file(path: Path) -> ProbeResult:
    """
    Probe a video file to get its properties.

    Args:
        path: Path to the video file

    Returns:
        ProbeResult with video properties

    Raises:
        RuntimeError: If ffprobe fails or file is invalid
    """
    ffprobe = get_ffprobe_path()

    cmd = [
        ffprobe,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path)
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=60
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"ffprobe failed for {path}: {e.stderr}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffprobe timed out for {path}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse ffprobe output: {e}")

    # Find video and audio streams
    video_stream = None
    audio_stream = None
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and video_stream is None:
            video_stream = stream
        elif stream.get("codec_type") == "audio" and audio_stream is None:
            audio_stream = stream

    if not video_stream:
        raise RuntimeError(f"No video stream found in {path}")

    # Extract duration
    duration = 0.0
    if "duration" in video_stream:
        duration = float(video_stream["duration"])
    elif "format" in data and "duration" in data["format"]:
        duration = float(data["format"]["duration"])

    # Extract dimensions
    width = int(video_stream.get("width", 0))
    height = int(video_stream.get("height", 0))

    # Calculate aspect ratio
    if width and height:
        # Check for display aspect ratio override
        dar = video_stream.get("display_aspect_ratio", "")
        if dar and ":" in dar:
            aspect_ratio = dar
        else:
            # Calculate from dimensions
            from math import gcd
            g = gcd(width, height)
            aspect_ratio = f"{width // g}:{height // g}"
    else:
        aspect_ratio = "unknown"

    # Extract frame rate
    frame_rate = 0.0
    r_frame_rate = video_stream.get("r_frame_rate", "0/1")
    if "/" in r_frame_rate:
        num, den = r_frame_rate.split("/")
        if int(den) > 0:
            frame_rate = int(num) / int(den)

    # Extract bitrate
    bitrate = None
    if "format" in data and "bit_rate" in data["format"]:
        try:
            bitrate = int(data["format"]["bit_rate"])
        except (ValueError, TypeError):
            pass

    return ProbeResult(
        duration=duration,
        width=width,
        height=height,
        aspect_ratio=aspect_ratio,
        video_codec=video_stream.get("codec_name", "unknown"),
        audio_codec=audio_stream.get("codec_name") if audio_stream else None,
        frame_rate=frame_rate,
        bitrate=bitrate,
    )


def compute_file_hash(path: Path, chunk_size: int = 8192) -> str:
    """
    Compute SHA256 hash of a file.

    Args:
        path: Path to the file
        chunk_size: Size of chunks to read

    Returns:
        Hex string of SHA256 hash
    """
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            sha256.update(chunk)
    return sha256.hexdigest()


def get_duration(path: Path) -> float:
    """Get just the duration of a video file (faster than full probe)."""
    ffprobe = get_ffprobe_path()

    cmd = [
        ffprobe,
        "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path)
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=30
        )
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        # Fall back to full probe
        return probe_file(path).duration


def check_ffmpeg_available() -> bool:
    """Check if ffmpeg is available."""
    ffmpeg = get_ffmpeg_path()
    try:
        subprocess.run(
            [ffmpeg, "-version"],
            capture_output=True,
            check=True,
            timeout=10
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


def check_ffprobe_available() -> bool:
    """Check if ffprobe is available."""
    ffprobe = get_ffprobe_path()
    try:
        subprocess.run(
            [ffprobe, "-version"],
            capture_output=True,
            check=True,
            timeout=10
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False
