"""Stage 1: Scan and probe video files."""

import re
from pathlib import Path
from typing import Optional

from ..config import Config
from ..db import db_connection, add_content, get_content_by_hash, log_ingest
from ..platform import get_drive_root, get_content_paths
from ..utils.ffmpeg import probe_file, compute_file_hash


# Video file extensions to scan
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv",
    ".webm", ".m4v", ".mpg", ".mpeg", ".ts", ".m2ts"
}


def is_video_file(path: Path) -> bool:
    """Check if a file is a video based on extension."""
    return path.suffix.lower() in VIDEO_EXTENSIONS


def detect_content_type(path: Path) -> str:
    """
    Detect content type from path and filename.

    Returns: 'movie', 'show', 'commercial', or 'bumper'
    """
    path_str = str(path).lower()

    # Check path components
    if "commercial" in path_str or "ads" in path_str:
        return "commercial"
    if "bumper" in path_str or "ident" in path_str:
        return "bumper"

    # Check for TV show patterns (S01E01, 1x01, etc.)
    filename = path.stem
    show_patterns = [
        r"s\d{1,2}e\d{1,2}",  # S01E01
        r"\d{1,2}x\d{1,2}",    # 1x01
        r"season\s*\d+",       # Season 1
        r"episode\s*\d+",      # Episode 1
    ]
    for pattern in show_patterns:
        if re.search(pattern, filename, re.IGNORECASE):
            return "show"

    # Default to movie
    return "movie"


def parse_series_info(filename: str) -> dict:
    """
    Parse series name, season, and episode from filename.

    Returns dict with keys: series_name, season, episode (all optional)
    """
    result = {}

    # Try S01E01 pattern
    match = re.search(r"(.+?)[.\s_-]+s(\d{1,2})e(\d{1,2})", filename, re.IGNORECASE)
    if match:
        result["series_name"] = match.group(1).replace(".", " ").replace("_", " ").strip()
        result["season"] = int(match.group(2))
        result["episode"] = int(match.group(3))
        return result

    # Try 1x01 pattern
    match = re.search(r"(.+?)[.\s_-]+(\d{1,2})x(\d{1,2})", filename, re.IGNORECASE)
    if match:
        result["series_name"] = match.group(1).replace(".", " ").replace("_", " ").strip()
        result["season"] = int(match.group(2))
        result["episode"] = int(match.group(3))
        return result

    return result


def parse_year(filename: str) -> Optional[int]:
    """Extract year from filename if present."""
    # Look for 4-digit year in parentheses or after title
    match = re.search(r"[(\[.]?(19\d{2}|20\d{2})[)\].]?", filename)
    if match:
        year = int(match.group(1))
        # Sanity check
        if 1900 <= year <= 2030:
            return year
    return None


def clean_title(filename: str) -> str:
    """Clean up filename to get a title."""
    title = filename

    # Remove common suffixes/patterns
    patterns_to_remove = [
        r"[.\s_-]+s\d{1,2}e\d{1,2}.*$",  # S01E01 and everything after
        r"[.\s_-]+\d{1,2}x\d{1,2}.*$",    # 1x01 and everything after
        r"[.\s_-]+(720p|1080p|480p|2160p|4k).*$",  # Resolution
        r"[.\s_-]+(hdtv|bluray|bdrip|webrip|dvdrip).*$",  # Source
        r"[.\s_-]+(x264|x265|hevc|h264|h265).*$",  # Codec
        r"[(\[.]?(19\d{2}|20\d{2})[)\].]?",  # Year
        r"\[.*?\]",  # Anything in brackets
        r"\(.*?\)",  # Anything in parentheses (be careful with this)
    ]

    for pattern in patterns_to_remove:
        title = re.sub(pattern, "", title, flags=re.IGNORECASE)

    # Replace separators with spaces
    title = title.replace(".", " ").replace("_", " ").replace("-", " ")

    # Clean up extra spaces
    title = " ".join(title.split())

    return title.strip() or filename


def scan_directory(
    directory: Path,
    config: Config,
    content_type_override: Optional[str] = None,
    verbose: bool = True
) -> dict:
    """
    Recursively scan a directory for video files.

    Args:
        directory: Directory to scan
        config: Application config
        content_type_override: Force all files to this type
        verbose: Print progress

    Returns:
        Dict with scan statistics
    """
    stats = {
        "scanned": 0,
        "added": 0,
        "skipped": 0,
        "errors": 0,
    }

    root = get_drive_root()

    with db_connection() as conn:
        log_ingest(conn, "scan", "started", message=str(directory))

        # Build set of known paths for fast lookup (avoids hashing existing files)
        cursor = conn.cursor()
        cursor.execute("SELECT original_path FROM content")
        known_paths = {row[0] for row in cursor.fetchall()}

        for path in directory.rglob("*"):
            if not path.is_file() or not is_video_file(path):
                continue

            stats["scanned"] += 1

            # Quick check: skip if path is already in database
            try:
                relative_path = str(path.relative_to(root))
            except ValueError:
                relative_path = str(path)

            if relative_path in known_paths:
                stats["skipped"] += 1
                continue

            if verbose:
                print(f"Scanning: {path.name}")

            try:
                # Compute hash to check for duplicates (different path, same file)
                file_hash = compute_file_hash(path)

                existing = get_content_by_hash(conn, file_hash)
                if existing:
                    if verbose:
                        print(f"  Skipped (duplicate of {existing['title']})")
                    stats["skipped"] += 1
                    continue

                # Probe the file
                probe = probe_file(path)

                # Determine content type
                content_type = content_type_override or detect_content_type(path)

                # Parse filename for metadata
                filename = path.stem
                series_info = parse_series_info(filename) if content_type == "show" else {}
                year = parse_year(filename)

                # Generate title
                if content_type == "show" and series_info.get("series_name"):
                    title = series_info["series_name"]
                    if series_info.get("season") and series_info.get("episode"):
                        title += f" S{series_info['season']:02d}E{series_info['episode']:02d}"
                else:
                    title = clean_title(filename)

                # Store path relative to root
                try:
                    relative_path = path.relative_to(root)
                except ValueError:
                    relative_path = path

                # Add to database
                content_id = add_content(
                    conn,
                    title=title,
                    content_type=content_type,
                    duration_seconds=probe.duration,
                    original_path=str(relative_path),
                    file_hash=file_hash,
                    series_name=series_info.get("series_name"),
                    season=series_info.get("season"),
                    episode=series_info.get("episode"),
                    year=year,
                    width=probe.width,
                    height=probe.height,
                    aspect_ratio=probe.aspect_ratio,
                    codec=probe.video_codec,
                )

                if verbose:
                    print(f"  Added: {title} ({content_type}, {probe.duration:.0f}s)")

                stats["added"] += 1
                log_ingest(conn, "scan", "completed", content_id, f"Added {title}")

            except Exception as e:
                if verbose:
                    print(f"  Error: {e}")
                stats["errors"] += 1
                log_ingest(conn, "scan", "failed", message=f"{path}: {e}")

        log_ingest(conn, "scan", "completed",
                   message=f"Scanned {stats['scanned']}, added {stats['added']}, errors {stats['errors']}")

    return stats


def scan_all(config: Config, verbose: bool = True) -> dict:
    """Scan all content directories."""
    paths = get_content_paths()
    total_stats = {"scanned": 0, "added": 0, "skipped": 0, "errors": 0}

    # Scan main content
    if paths["content_originals"].exists():
        if verbose:
            print(f"\nScanning content: {paths['content_originals']}")
        stats = scan_directory(paths["content_originals"], config, verbose=verbose)
        for k, v in stats.items():
            total_stats[k] += v

    # Scan commercials
    if paths["commercials_originals"].exists():
        if verbose:
            print(f"\nScanning commercials: {paths['commercials_originals']}")
        stats = scan_directory(
            paths["commercials_originals"], config,
            content_type_override="commercial", verbose=verbose
        )
        for k, v in stats.items():
            total_stats[k] += v

    return total_stats
