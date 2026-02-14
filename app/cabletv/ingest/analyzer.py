"""Stage 4: Black-frame detection for commercial break points."""

import json
import subprocess
from pathlib import Path
from typing import Optional

from ..config import Config
from ..db import (
    db_connection, get_content_by_status, update_content_status,
    add_break_point, clear_break_points, log_ingest, get_content_by_id
)
from ..platform import get_drive_root, get_ffmpeg_path


def detect_black_frames(
    video_path: Path,
    min_duration: float = 0.3,
    threshold: float = 0.1,
    verbose: bool = False
) -> list[dict]:
    """
    Detect black frames in a video using ffmpeg.

    Args:
        video_path: Path to video file
        min_duration: Minimum black duration in seconds
        threshold: Black detection threshold (0-1, lower = stricter)
        verbose: Print debug info

    Returns:
        List of dicts with 'start', 'end', 'duration' keys
    """
    ffmpeg = get_ffmpeg_path()

    cmd = [
        ffmpeg,
        "-i", str(video_path),
        "-vf", f"blackdetect=d={min_duration}:pix_th={threshold}",
        "-an",  # No audio
        "-f", "null",
        "-"
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )
    except subprocess.TimeoutExpired:
        if verbose:
            print("  Black frame detection timed out")
        return []

    # Parse ffmpeg output for black_start/black_end
    black_frames = []
    for line in result.stderr.split("\n"):
        if "black_start:" in line:
            try:
                # Parse: [blackdetect @ ...] black_start:123.456 black_end:124.789 black_duration:1.333
                parts = line.split()
                frame_data = {}
                for part in parts:
                    if part.startswith("black_start:"):
                        frame_data["start"] = float(part.split(":")[1])
                    elif part.startswith("black_end:"):
                        frame_data["end"] = float(part.split(":")[1])
                    elif part.startswith("black_duration:"):
                        frame_data["duration"] = float(part.split(":")[1])

                if "start" in frame_data and "end" in frame_data:
                    black_frames.append(frame_data)
            except (ValueError, IndexError):
                continue

    return black_frames


def find_break_points(
    black_frames: list[dict],
    duration: float,
    min_gap: float = 300.0,  # 5 minutes between breaks
    edge_margin: float = 120.0  # 2 minutes from start/end
) -> list[float]:
    """
    Find likely commercial break points from black frames.

    Args:
        black_frames: List of detected black frames
        duration: Total video duration
        min_gap: Minimum gap between break points
        edge_margin: Margin from video start/end to ignore

    Returns:
        List of timestamps (midpoint of black frames)
    """
    break_points = []
    last_break = 0.0

    for frame in black_frames:
        # Get midpoint of black frame
        midpoint = (frame["start"] + frame["end"]) / 2

        # Skip if too close to start or end
        if midpoint < edge_margin or midpoint > (duration - edge_margin):
            continue

        # Skip if too close to last break
        if midpoint - last_break < min_gap:
            continue

        # This looks like a good break point
        break_points.append(midpoint)
        last_break = midpoint

    return break_points


def analyze_content(
    content_id: int,
    config: Config,
    verbose: bool = True,
    force: bool = False
) -> bool:
    """
    Analyze a single content item for break points.

    Args:
        content_id: Database content ID
        config: Application config
        verbose: Print progress
        force: Re-analyze even if already done

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
        if content["status"] not in ("transcoded", "analyzing") and not force:
            if verbose:
                print(f"Content not ready for analysis (status: {content['status']})")
            return False

        # Use normalized path if available, otherwise original
        video_path_str = content["normalized_path"] or content["original_path"]
        video_path = root / video_path_str

        if not video_path.exists():
            if verbose:
                print(f"Video file not found: {video_path}")
            update_content_status(conn, content_id, "error", "Video file not found")
            return False

        if verbose:
            print(f"Analyzing: {content['title']}")

        # Update status
        update_content_status(conn, content_id, "analyzing")
        log_ingest(conn, "analyze", "started", content_id)

    # Run analysis (outside DB transaction)
    try:
        black_frames = detect_black_frames(video_path, verbose=verbose)

        if verbose:
            print(f"  Found {len(black_frames)} black frame sequences")

        # Find break points
        duration = content["duration_seconds"]
        break_points = find_break_points(black_frames, duration)

        if verbose:
            print(f"  Identified {len(break_points)} potential break points")
            for bp in break_points:
                mins = int(bp // 60)
                secs = int(bp % 60)
                print(f"    {mins}:{secs:02d}")

        # Save to database
        with db_connection() as conn:
            # Clear any existing break points
            clear_break_points(conn, content_id)

            # Add new break points
            for timestamp in break_points:
                add_break_point(conn, content_id, timestamp)

            update_content_status(conn, content_id, "ready")
            log_ingest(conn, "analyze", "completed", content_id,
                       f"Found {len(break_points)} break points")

        return True

    except Exception as e:
        if verbose:
            print(f"  Error: {e}")

        with db_connection() as conn:
            update_content_status(conn, content_id, "error", str(e))
            log_ingest(conn, "analyze", "failed", content_id, str(e))

        return False


def analyze_all(
    config: Config,
    verbose: bool = True,
    force: bool = False
) -> dict:
    """
    Analyze all transcoded content.

    Args:
        config: Application config
        verbose: Print progress
        force: Re-analyze all items

    Returns:
        Dict with analysis statistics
    """
    stats = {"analyzed": 0, "skipped": 0, "errors": 0}

    with db_connection() as conn:
        log_ingest(conn, "analyze", "started", message="Batch analysis")

        # Get content needing analysis
        content_list = get_content_by_status(conn, "transcoded")

        if verbose:
            print(f"Found {len(content_list)} items to analyze")

    for content in content_list:
        # Skip short content (commercials, bumpers)
        if content["duration_seconds"] < 300:  # Less than 5 minutes
            if verbose:
                print(f"Skipping {content['title']} (too short for break detection)")
            with db_connection() as conn:
                update_content_status(conn, content["id"], "ready")
            stats["skipped"] += 1
            continue

        success = analyze_content(content["id"], config, verbose=verbose, force=force)
        if success:
            stats["analyzed"] += 1
        else:
            stats["errors"] += 1

    with db_connection() as conn:
        log_ingest(conn, "analyze", "completed",
                   message=f"Analyzed {stats['analyzed']}, skipped {stats['skipped']}")

    return stats


def skip_analysis(verbose: bool = True) -> dict:
    """
    Skip analysis and mark all transcoded content as ready.
    """
    stats = {"skipped": 0}

    with db_connection() as conn:
        content_list = get_content_by_status(conn, "transcoded")

        for content in content_list:
            update_content_status(conn, content["id"], "ready")
            stats["skipped"] += 1

        if verbose:
            print(f"Skipped analysis for {stats['skipped']} items")

    return stats
