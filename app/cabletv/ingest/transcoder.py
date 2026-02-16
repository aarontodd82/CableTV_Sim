"""Stage 3: Transcode video to normalized 640x480 4:3 format."""

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from ..config import Config
from ..db import (
    db_connection, get_content_by_status, update_content_status,
    update_content_normalized_path, log_ingest, get_content_by_id
)
from ..platform import get_drive_root, get_content_paths, get_ffmpeg_path, get_ffprobe_path
from ..utils.ffmpeg import probe_file


_nvenc_available = None

def has_nvenc() -> bool:
    """Check if NVIDIA NVENC hardware encoder is available."""
    global _nvenc_available
    if _nvenc_available is not None:
        return _nvenc_available
    try:
        ffmpeg = get_ffmpeg_path()
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10
        )
        _nvenc_available = "h264_nvenc" in result.stdout
    except Exception:
        _nvenc_available = False
    return _nvenc_available


def _find_english_audio(input_path: Path) -> Optional[int]:
    """Find the index of the English audio stream, or None to use default."""
    ffprobe = get_ffprobe_path()
    try:
        result = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_streams", "-select_streams", "a", str(input_path)],
            capture_output=True, text=True, timeout=30
        )
        import json
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if len(streams) <= 1:
            return None  # Only one audio track, use default
        for stream in streams:
            lang = stream.get("tags", {}).get("language", "").lower()
            if lang in ("eng", "en", "english"):
                return int(stream["index"])
    except Exception:
        pass
    return None


def build_transcode_command(
    input_path: Path,
    output_path: Path,
    config: Config,
    source_aspect: str = "16:9",
    audio_stream_index: Optional[int] = None,
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
    crop_pct = config.ingest.widescreen_crop

    # Build video filter
    if abs(source_ratio - target_ratio) < 0.1:
        # Source is already ~4:3, just scale
        vf = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
    elif source_ratio > target_ratio:
        # Source is wider (16:9) than target (4:3)
        if crop_pct > 0:
            # Crop sides first to reduce letterboxing, then scale to fit
            crop_frac = crop_pct / 100.0
            vf = f"crop=iw*{1 - 2*crop_frac:.4f}:ih,scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
        else:
            vf = f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
    else:
        # Source is taller, pillarbox left/right
        vf = f"scale=-2:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"

    use_nvenc = has_nvenc()

    cmd = [
        ffmpeg,
        "-y",  # Overwrite output
        "-i", str(input_path),
        "-map", "0:v:0",
        "-map", f"0:{audio_stream_index}" if audio_stream_index is not None else "0:a:0",
        "-pix_fmt", "yuv420p",  # Force 8-bit output (10-bit sources break NVENC h264)
        "-vf", vf,
    ]

    if use_nvenc:
        cmd += [
            "-c:v", "h264_nvenc",
            "-preset", "p4",
            "-b:v", config.ingest.video_bitrate,
            "-g", str(keyframe_interval),
        ]
    else:
        cmd += [
            "-c:v", "libx264",
            "-preset", "medium",
            "-b:v", config.ingest.video_bitrate,
            "-g", str(keyframe_interval),
            "-keyint_min", str(keyframe_interval),
        ]

    cmd += [
        # Audio settings
        "-c:a", "aac",
        "-b:a", config.ingest.audio_bitrate,
        "-ar", "44100",
        "-ac", "2",
        # Output format
        "-movflags", "+faststart",
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
        if content["status"] not in ("identified", "transcoding", "transcoded") and not force:
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
            # Validate the existing file isn't a truncated partial from ctrl+c
            try:
                existing_probe = probe_file(output_path)
                source_probe = probe_file(input_path)
                # Accept if output duration is at least 95% of source
                if existing_probe.duration > 1 and existing_probe.duration >= source_probe.duration * 0.95:
                    if verbose:
                        print(f"Output already exists: {output_path}")
                    relative_output = output_path.relative_to(root)
                    update_content_normalized_path(conn, content_id, str(relative_output))
                    update_content_status(conn, content_id, "transcoded")
                    return True
                if verbose:
                    print(f"Partial file detected ({existing_probe.duration:.0f}s vs {source_probe.duration:.0f}s source): {content['title']}")
            except Exception:
                pass
            # File is corrupt/partial — delete and re-transcode
            if verbose:
                print(f"Removing and re-transcoding: {output_path.name}")
            output_path.unlink()
            update_content_status(conn, content_id, "identified")

        # Probe source to check resolution
        probe = probe_file(input_path)
        target_w = config.ingest.transcode_width
        target_h = config.ingest.transcode_height

        # Check if source is already at or below target resolution
        # But still transcode if widescreen and crop is enabled
        source_ratio = probe.width / probe.height if probe.height else 1
        needs_crop = config.ingest.widescreen_crop > 0 and source_ratio > (target_w / target_h + 0.1)

        if probe.width <= target_w and probe.height <= target_h and not needs_crop:
            # Check bitrate — re-encode if over threshold, otherwise just copy
            threshold_bps = int(config.ingest.transcode_threshold.replace("k", "000"))
            source_bps = probe.bitrate or 0
            if source_bps > threshold_bps:
                target_bps = int(config.ingest.video_bitrate.replace("k", "000"))
                if verbose:
                    print(f"Re-encoding (already {probe.width}x{probe.height} but {source_bps//1000}kbps > {threshold_bps//1000}kbps threshold, target {target_bps//1000}kbps): {content['title']}")
                # Fall through to normal transcode
            else:
                if verbose:
                    print(f"Copying (already {probe.width}x{probe.height}, {source_bps//1000}kbps <= {threshold_bps//1000}kbps threshold): {content['title']}")
                shutil.copy2(str(input_path), str(output_path))
                relative_output = output_path.relative_to(root)
                update_content_normalized_path(conn, content_id, str(relative_output))
                update_content_status(conn, content_id, "transcoded")
                log_ingest(conn, "transcode", "completed", content_id, "Copied - below target resolution and bitrate threshold")
                return True

        if verbose:
            print(f"Transcoding: {content['title']}")
            print(f"  Input: {input_path} ({probe.width}x{probe.height})")
            print(f"  Output: {output_path}")

        # Update status
        update_content_status(conn, content_id, "transcoding")
        log_ingest(conn, "transcode", "started", content_id)

    # Build and run command (outside DB transaction for long operation)
    try:
        source_aspect = probe.aspect_ratio
        eng_audio = _find_english_audio(input_path)

        cmd = build_transcode_command(input_path, output_path, config, source_aspect, eng_audio)

        if verbose:
            audio_info = f", audio track {eng_audio}" if eng_audio is not None else ""
            print(f"  Aspect ratio: {source_aspect} -> 4:3{audio_info}")

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
        stderr_chunks = []
        while process.poll() is None:
            chunk = process.stderr.read(1024)
            if chunk:
                stderr_chunks.append(chunk)
            if verbose:
                print(".", end="", flush=True)
                dots += 1
                if dots >= 50:
                    print()
                    print("            ", end="")
                    dots = 0

        # Read any remaining stderr
        remaining = process.stderr.read()
        if remaining:
            stderr_chunks.append(remaining)

        if verbose:
            print(" Done")

        # Check result
        if process.returncode != 0:
            stderr = "".join(stderr_chunks)
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

        if force:
            # Force: redo everything that's past scanning, in ID order
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM content WHERE status NOT IN ('scanned', 'error') ORDER BY id"
            )
            content_list = cursor.fetchall()
        else:
            # Normal: only identified + interrupted, in ID order
            content_list = get_content_by_status(conn, "identified")
            interrupted = get_content_by_status(conn, "transcoding")
            if interrupted:
                content_list = interrupted + content_list
                content_list.sort(key=lambda c: c["id"])

        if verbose:
            encoder = "h264_nvenc (GPU)" if has_nvenc() else "libx264 (CPU)"
            print(f"Found {len(content_list)} items to transcode")
            print(f"Encoder: {encoder}")

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
