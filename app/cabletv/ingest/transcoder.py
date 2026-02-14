"""Stage 3: Transcode video to normalized 640x480 4:3 format."""

import subprocess
import sys
from pathlib import Path
from typing import Optional

from ..config import Config
from ..db import (
    db_connection, get_content_by_status, update_content_status,
    update_content_normalized_path, log_ingest, get_content_by_id
)
from ..platform import get_drive_root, get_content_paths, get_ffmpeg_path
from ..utils.ffmpeg import probe_file


def build_transcode_command(
    input_path: Path,
    output_path: Path,
    config: Config,
    source_aspect: str = "16:9"
) -> list[str]:
    """
    Build ffmpeg command for transcoding.

    Handles aspect ratio conversion:
    - 16:9 source -> letterboxed to 4:3
    - 4:3 source -> direct scale
    - Other ratios -> best fit with letterboxing
    """
    ffmpeg = get_ffmpeg_path()
    width = config.ingest.transcode_width
    height = config.ingest.transcode_height
    keyframe_interval = config.ingest.keyframe_interval

    # Parse source aspect ratio
    try:
        if ":" in source_aspect:
            aw, ah = map(int, source_aspect.split(":"))
            source_ratio = aw / ah
        else:
            source_ratio = 16 / 9  # Default assumption
    except (ValueError, ZeroDivisionError):
        source_ratio = 16 / 9

    target_ratio = width / height  # 4:3 = 1.333

    # Build video filter
    if abs(source_ratio - target_ratio) < 0.1:
        # Source is already ~4:3, just scale
        vf = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
    elif source_ratio > target_ratio:
        # Source is wider (16:9), letterbox top/bottom
        vf = f"scale={width}:-2:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
    else:
        # Source is taller, pillarbox left/right
        vf = f"scale=-2:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"

    cmd = [
        ffmpeg,
        "-y",  # Overwrite output
        "-i", str(input_path),
        # Video settings
        "-c:v", "libx264",
        "-preset", "medium",
        "-b:v", config.ingest.video_bitrate,
        "-vf", vf,
        "-g", str(keyframe_interval),  # GOP size for fast seeking
        "-keyint_min", str(keyframe_interval),
        # Audio settings
        "-c:a", "aac",
        "-b:a", config.ingest.audio_bitrate,
        "-ar", "44100",
        "-ac", "2",
        # Output format
        "-movflags", "+faststart",  # Enable streaming
        str(output_path)
    ]

    return cmd


def get_normalized_path(original_path: str, content_type: str) -> Path:
    """Generate the normalized output path."""
    paths = get_content_paths()

    if content_type == "commercial":
        base_dir = paths["commercials_normalized"]
    else:
        base_dir = paths["content_normalized"]

    # Create subdirectory structure if original has subdirs
    original = Path(original_path)
    # Get just the filename, change extension to .mp4
    output_name = original.stem + ".mp4"

    return base_dir / output_name


def transcode_file(
    content_id: int,
    config: Config,
    verbose: bool = True,
    force: bool = False
) -> bool:
    """
    Transcode a single content item.

    Args:
        content_id: Database content ID
        config: Application config
        verbose: Print progress
        force: Re-transcode even if output exists

    Returns:
        True if successful
    """
    root = get_drive_root()

    with db_connection() as conn:
        content = get_content_by_id(conn, content_id)
        if not content:
            if verbose:
                print(f"Content ID {content_id} not found")
            return False

        # Check status
        if content["status"] not in ("identified", "transcoded") and not force:
            if verbose:
                print(f"Content not ready for transcoding (status: {content['status']})")
            return False

        input_path = root / content["original_path"]
        if not input_path.exists():
            if verbose:
                print(f"Source file not found: {input_path}")
            update_content_status(conn, content_id, "error", "Source file not found")
            return False

        output_path = get_normalized_path(content["original_path"], content["content_type"])
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Check if already transcoded
        if output_path.exists() and not force:
            if verbose:
                print(f"Output already exists: {output_path}")
            # Update path and status
            relative_output = output_path.relative_to(root)
            update_content_normalized_path(conn, content_id, str(relative_output))
            update_content_status(conn, content_id, "transcoded")
            return True

        if verbose:
            print(f"Transcoding: {content['title']}")
            print(f"  Input: {input_path}")
            print(f"  Output: {output_path}")

        # Update status
        update_content_status(conn, content_id, "transcoding")
        log_ingest(conn, "transcode", "started", content_id)

    # Build and run command (outside DB transaction for long operation)
    try:
        # Get source aspect ratio
        probe = probe_file(input_path)
        source_aspect = probe.aspect_ratio

        cmd = build_transcode_command(input_path, output_path, config, source_aspect)

        if verbose:
            print(f"  Aspect ratio: {source_aspect} -> 4:3")

        # Run ffmpeg with progress output
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        # Simple progress indicator
        if verbose:
            print("  Progress: ", end="", flush=True)
            dots = 0

        # Wait for completion, showing dots for progress
        while process.poll() is None:
            process.stderr.read(1024)  # Consume stderr to prevent blocking
            if verbose:
                print(".", end="", flush=True)
                dots += 1
                if dots >= 50:
                    print()
                    print("            ", end="")
                    dots = 0

        if verbose:
            print(" Done")

        # Check result
        if process.returncode != 0:
            stderr = process.stderr.read()
            raise RuntimeError(f"ffmpeg failed: {stderr[-500:]}")

        # Verify output
        if not output_path.exists():
            raise RuntimeError("Output file was not created")

        output_probe = probe_file(output_path)
        if output_probe.duration < 1:
            raise RuntimeError("Output file has invalid duration")

        if verbose:
            print(f"  Output duration: {output_probe.duration:.1f}s")
            print(f"  Output size: {output_path.stat().st_size / 1024 / 1024:.1f} MB")

        # Update database
        with db_connection() as conn:
            relative_output = output_path.relative_to(root)
            update_content_normalized_path(conn, content_id, str(relative_output))
            update_content_status(conn, content_id, "transcoded")
            log_ingest(conn, "transcode", "completed", content_id)

        return True

    except Exception as e:
        if verbose:
            print(f"  Error: {e}")

        with db_connection() as conn:
            update_content_status(conn, content_id, "error", str(e))
            log_ingest(conn, "transcode", "failed", content_id, str(e))

        # Clean up partial output
        if output_path.exists():
            try:
                output_path.unlink()
            except OSError:
                pass

        return False


def transcode_all(
    config: Config,
    verbose: bool = True,
    force: bool = False
) -> dict:
    """
    Transcode all identified content.

    Args:
        config: Application config
        verbose: Print progress
        force: Re-transcode all items

    Returns:
        Dict with transcode statistics
    """
    stats = {"transcoded": 0, "skipped": 0, "errors": 0}

    with db_connection() as conn:
        log_ingest(conn, "transcode", "started", message="Batch transcode")

        # Get content needing transcoding
        content_list = get_content_by_status(conn, "identified")

        if verbose:
            print(f"Found {len(content_list)} items to transcode")

    for content in content_list:
        success = transcode_file(content["id"], config, verbose=verbose, force=force)
        if success:
            stats["transcoded"] += 1
        else:
            stats["errors"] += 1

    with db_connection() as conn:
        log_ingest(conn, "transcode", "completed",
                   message=f"Transcoded {stats['transcoded']}, errors {stats['errors']}")

    return stats


def skip_transcode(verbose: bool = True) -> dict:
    """
    Skip transcoding and use original files directly.

    Marks content as transcoded and sets normalized_path to original_path.
    """
    stats = {"skipped": 0}

    with db_connection() as conn:
        content_list = get_content_by_status(conn, "identified")

        for content in content_list:
            # Use original as normalized
            update_content_normalized_path(conn, content["id"], content["original_path"])
            update_content_status(conn, content["id"], "transcoded")
            stats["skipped"] += 1

        if verbose:
            print(f"Skipped transcoding for {stats['skipped']} items (using originals)")

    return stats
