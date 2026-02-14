"""Stage 5: Final validation and registration."""

from pathlib import Path

from ..config import Config
from ..db import (
    db_connection, get_content_by_status, get_content_by_id,
    update_content_status, log_ingest, get_stats
)
from ..platform import get_drive_root
from ..utils.ffmpeg import probe_file


def validate_content(content_id: int, config: Config, verbose: bool = True) -> bool:
    """
    Validate that content is ready for playback.

    Checks:
    - Normalized file exists and is readable
    - Duration matches database record (within tolerance)
    - File can be probed successfully

    Args:
        content_id: Database content ID
        config: Application config
        verbose: Print progress

    Returns:
        True if validation passes
    """
    root = get_drive_root()

    with db_connection() as conn:
        content = get_content_by_id(conn, content_id)
        if not content:
            if verbose:
                print(f"Content ID {content_id} not found")
            return False

        # Get the video path
        video_path_str = content["normalized_path"] or content["original_path"]
        video_path = root / video_path_str

        if verbose:
            print(f"Validating: {content['title']}")

        try:
            # Check file exists
            if not video_path.exists():
                raise ValueError(f"Video file not found: {video_path}")

            # Check file is readable and valid
            probe = probe_file(video_path)

            # Check duration matches (within 5% tolerance)
            db_duration = content["duration_seconds"]
            file_duration = probe.duration
            if abs(db_duration - file_duration) / db_duration > 0.05:
                if verbose:
                    print(f"  Warning: Duration mismatch (DB: {db_duration:.1f}s, file: {file_duration:.1f}s)")
                # Update the duration in DB to match actual file
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE content SET duration_seconds = ? WHERE id = ?",
                    (file_duration, content_id)
                )

            if verbose:
                print(f"  Validated: {video_path.name}")
                print(f"  Duration: {probe.duration:.1f}s")
                print(f"  Resolution: {probe.width}x{probe.height}")

            return True

        except Exception as e:
            if verbose:
                print(f"  Validation failed: {e}")
            update_content_status(conn, content_id, "error", f"Validation failed: {e}")
            log_ingest(conn, "validate", "failed", content_id, str(e))
            return False


def register_all(config: Config, verbose: bool = True) -> dict:
    """
    Validate and finalize all content.

    This is the final stage that ensures all content is ready for playback.

    Args:
        config: Application config
        verbose: Print progress

    Returns:
        Dict with registration statistics
    """
    stats = {"validated": 0, "errors": 0}

    with db_connection() as conn:
        log_ingest(conn, "register", "started", message="Final validation")

        # Check content that's marked as ready
        content_list = get_content_by_status(conn, "ready")

        if verbose:
            print(f"Validating {len(content_list)} ready items")

    for content in content_list:
        if validate_content(content["id"], config, verbose=verbose):
            stats["validated"] += 1
        else:
            stats["errors"] += 1

    with db_connection() as conn:
        log_ingest(conn, "register", "completed",
                   message=f"Validated {stats['validated']}, errors {stats['errors']}")

    return stats


def get_ingest_status(verbose: bool = True) -> dict:
    """
    Get current status of the ingest pipeline.

    Returns dict with counts by status.
    """
    with db_connection() as conn:
        stats = get_stats(conn)

    if verbose:
        print("\nIngest Pipeline Status:")
        print("-" * 40)

        by_status = stats.get("by_status", {})
        status_order = ["scanned", "identified", "transcoding", "transcoded",
                        "analyzing", "ready", "error"]

        for status in status_order:
            count = by_status.get(status, 0)
            if count > 0:
                print(f"  {status:15} {count:5}")

        print("-" * 40)
        print(f"  {'Total':15} {sum(by_status.values()):5}")

        if stats.get("total_ready", 0) > 0:
            print(f"\nReady content: {stats['total_ready']} items")
            print(f"Total duration: {stats['total_duration_hours']:.1f} hours")

            print("\nBy type:")
            for content_type, count in stats.get("by_type", {}).items():
                print(f"  {content_type}: {count}")

    return stats


def run_full_pipeline(
    config: Config,
    auto: bool = False,
    skip_tmdb: bool = False,
    skip_transcode: bool = False,
    skip_analyze: bool = False,
    verbose: bool = True
) -> dict:
    """
    Run the complete ingest pipeline.

    Args:
        config: Application config
        auto: Auto-accept TMDB matches
        skip_tmdb: Skip TMDB identification
        skip_transcode: Skip transcoding (use originals)
        skip_analyze: Skip black-frame analysis
        verbose: Print progress

    Returns:
        Combined statistics from all stages
    """
    from .scanner import scan_all
    from .identifier import identify_content, skip_identification
    from .transcoder import transcode_all, skip_transcode as skip_transcode_fn
    from .analyzer import analyze_all, skip_analysis

    all_stats = {}

    # Stage 1: Scan
    if verbose:
        print("\n" + "=" * 50)
        print("STAGE 1: Scanning content directories")
        print("=" * 50)
    all_stats["scan"] = scan_all(config, verbose=verbose)

    # Stage 2: Identify
    if verbose:
        print("\n" + "=" * 50)
        print("STAGE 2: TMDB identification")
        print("=" * 50)
    if skip_tmdb:
        all_stats["identify"] = skip_identification(verbose=verbose)
    else:
        all_stats["identify"] = identify_content(config, auto=auto, verbose=verbose)

    # Stage 3: Transcode
    if verbose:
        print("\n" + "=" * 50)
        print("STAGE 3: Transcoding to 640x480")
        print("=" * 50)
    if skip_transcode:
        all_stats["transcode"] = skip_transcode_fn(verbose=verbose)
    else:
        all_stats["transcode"] = transcode_all(config, verbose=verbose)

    # Stage 4: Analyze
    if verbose:
        print("\n" + "=" * 50)
        print("STAGE 4: Black-frame analysis")
        print("=" * 50)
    if skip_analyze:
        all_stats["analyze"] = skip_analysis(verbose=verbose)
    else:
        all_stats["analyze"] = analyze_all(config, verbose=verbose)

    # Stage 5: Register/Validate
    if verbose:
        print("\n" + "=" * 50)
        print("STAGE 5: Final validation")
        print("=" * 50)
    all_stats["register"] = register_all(config, verbose=verbose)

    # Clear caches so new content is picked up immediately
    # Note: If the system is running, ScheduleEngine channel pools will clear on restart
    from ..schedule.commercials import clear_commercial_cache
    clear_commercial_cache()
    if verbose:
        print("\nCleared schedule caches (restart required if system is running)")

    # Summary
    if verbose:
        print("\n" + "=" * 50)
        print("PIPELINE COMPLETE")
        print("=" * 50)
        get_ingest_status(verbose=True)

    return all_stats
