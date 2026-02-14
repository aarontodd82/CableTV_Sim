"""Database access layer for SQLite storage."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional, Generator

from .platform import get_drive_root


# Database schema version for migrations
SCHEMA_VERSION = 1


def get_db_path(root: Optional[Path] = None) -> Path:
    """Get path to the database file."""
    if root is None:
        root = get_drive_root()
    return root / "cabletv.db"


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """
    Get a database connection with WAL mode enabled.

    Args:
        db_path: Path to database file, or None for default

    Returns:
        SQLite connection with row factory set
    """
    if db_path is None:
        db_path = get_db_path()

    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db_connection(db_path: Optional[Path] = None) -> Generator[sqlite3.Connection, None, None]:
    """Context manager for database connections."""
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_database(db_path: Optional[Path] = None) -> None:
    """
    Initialize the database with full schema.

    Creates tables:
    - content: Main content registry
    - tags: Available tags for categorization
    - content_tags: Many-to-many content/tag relationship
    - break_points: Commercial break points for content
    - ingest_log: Processing history log
    """
    if db_path is None:
        db_path = get_db_path()

    with db_connection(db_path) as conn:
        cursor = conn.cursor()

        # Content table - main registry of all video content
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS content (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content_type TEXT NOT NULL CHECK(content_type IN ('movie', 'show', 'commercial', 'bumper')),
                series_name TEXT,
                season INTEGER,
                episode INTEGER,
                year INTEGER,
                duration_seconds REAL NOT NULL,
                original_path TEXT NOT NULL,
                normalized_path TEXT,
                file_hash TEXT UNIQUE NOT NULL,
                tmdb_id INTEGER,
                status TEXT NOT NULL DEFAULT 'scanned'
                    CHECK(status IN ('scanned', 'identified', 'transcoding', 'transcoded', 'analyzing', 'ready', 'error')),
                error_message TEXT,
                width INTEGER,
                height INTEGER,
                aspect_ratio TEXT,
                codec TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # Tags table - available tags for categorization
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                description TEXT
            )
        """)

        # Content-Tags junction table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS content_tags (
                content_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                PRIMARY KEY (content_id, tag_id),
                FOREIGN KEY (content_id) REFERENCES content(id) ON DELETE CASCADE,
                FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
            )
        """)

        # Break points table - commercial break timecodes
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS break_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_id INTEGER NOT NULL,
                timestamp_seconds REAL NOT NULL,
                confidence REAL DEFAULT 1.0,
                FOREIGN KEY (content_id) REFERENCES content(id) ON DELETE CASCADE
            )
        """)

        # Ingest log table - processing history
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ingest_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_id INTEGER,
                stage TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('started', 'completed', 'failed')),
                message TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (content_id) REFERENCES content(id) ON DELETE SET NULL
            )
        """)

        # Create indexes for common queries
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_content_status ON content(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_content_type ON content(content_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_content_hash ON content(file_hash)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_content_tags_content ON content_tags(content_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_content_tags_tag ON content_tags(tag_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_break_points_content ON break_points(content_id)")

        # Insert default tags
        default_tags = [
            # Primary genres (auto-detected from TMDB)
            ("action", "Action and adventure content"),
            ("adventure", "Adventure content"),
            ("animation", "Animated content"),
            ("comedy", "Comedy and humor"),
            ("crime", "Crime and gangster"),
            ("documentary", "Documentary and educational"),
            ("drama", "Drama and serious content"),
            ("family", "Family-friendly content"),
            ("fantasy", "Fantasy and magical"),
            ("history", "Historical content"),
            ("horror", "Horror content"),
            ("kids", "Children's programming"),
            ("music", "Music and musicals"),
            ("mystery", "Mystery and suspense"),
            ("romance", "Romantic content"),
            ("scifi", "Science fiction"),
            ("thriller", "Thriller and suspense"),
            ("war", "War content"),
            ("western", "Western genre"),
            # Manual tags (not auto-detected)
            ("classic", "Classic/vintage content (pre-1980)"),
            ("sitcom", "Situation comedy"),
            ("cult", "Cult classics"),
            ("sports", "Sports programming"),
        ]
        cursor.executemany(
            "INSERT OR IGNORE INTO tags (name, description) VALUES (?, ?)",
            default_tags
        )


# Content CRUD operations

def add_content(
    conn: sqlite3.Connection,
    title: str,
    content_type: str,
    duration_seconds: float,
    original_path: str,
    file_hash: str,
    series_name: Optional[str] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    year: Optional[int] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    aspect_ratio: Optional[str] = None,
    codec: Optional[str] = None,
) -> int:
    """Add new content to the database."""
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO content (
            title, content_type, series_name, season, episode, year,
            duration_seconds, original_path, file_hash,
            width, height, aspect_ratio, codec
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        title, content_type, series_name, season, episode, year,
        duration_seconds, original_path, file_hash,
        width, height, aspect_ratio, codec
    ))
    return cursor.lastrowid


def get_content_by_id(conn: sqlite3.Connection, content_id: int) -> Optional[sqlite3.Row]:
    """Get content by ID."""
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM content WHERE id = ?", (content_id,))
    return cursor.fetchone()


def get_content_by_hash(conn: sqlite3.Connection, file_hash: str) -> Optional[sqlite3.Row]:
    """Get content by file hash."""
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM content WHERE file_hash = ?", (file_hash,))
    return cursor.fetchone()


def get_content_by_status(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    """Get all content with a specific status."""
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM content WHERE status = ? ORDER BY id", (status,))
    return cursor.fetchall()


def get_ready_content(conn: sqlite3.Connection, content_type: Optional[str] = None) -> list[sqlite3.Row]:
    """Get all content ready for playback."""
    cursor = conn.cursor()
    if content_type:
        cursor.execute(
            "SELECT * FROM content WHERE status = 'ready' AND content_type = ? ORDER BY title",
            (content_type,)
        )
    else:
        cursor.execute("SELECT * FROM content WHERE status = 'ready' ORDER BY title")
    return cursor.fetchall()


def get_content_with_tags(conn: sqlite3.Connection, tags: list[str]) -> list[sqlite3.Row]:
    """Get content that has any of the specified tags."""
    if not tags:
        return []
    placeholders = ",".join("?" * len(tags))
    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT DISTINCT c.* FROM content c
        JOIN content_tags ct ON c.id = ct.content_id
        JOIN tags t ON ct.tag_id = t.id
        WHERE c.status = 'ready' AND t.name IN ({placeholders})
        ORDER BY c.title
    """, tags)
    return cursor.fetchall()


def update_content_status(
    conn: sqlite3.Connection,
    content_id: int,
    status: str,
    error_message: Optional[str] = None
) -> None:
    """Update content status."""
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE content
        SET status = ?, error_message = ?, updated_at = datetime('now')
        WHERE id = ?
    """, (status, error_message, content_id))


def update_content_normalized_path(
    conn: sqlite3.Connection,
    content_id: int,
    normalized_path: str
) -> None:
    """Update the normalized path after transcoding."""
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE content
        SET normalized_path = ?, updated_at = datetime('now')
        WHERE id = ?
    """, (normalized_path, content_id))


def update_content_metadata(
    conn: sqlite3.Connection,
    content_id: int,
    title: Optional[str] = None,
    series_name: Optional[str] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    year: Optional[int] = None,
    tmdb_id: Optional[int] = None,
) -> None:
    """Update content metadata from identification."""
    cursor = conn.cursor()
    updates = []
    values = []

    if title is not None:
        updates.append("title = ?")
        values.append(title)
    if series_name is not None:
        updates.append("series_name = ?")
        values.append(series_name)
    if season is not None:
        updates.append("season = ?")
        values.append(season)
    if episode is not None:
        updates.append("episode = ?")
        values.append(episode)
    if year is not None:
        updates.append("year = ?")
        values.append(year)
    if tmdb_id is not None:
        updates.append("tmdb_id = ?")
        values.append(tmdb_id)

    if updates:
        updates.append("updated_at = datetime('now')")
        values.append(content_id)
        cursor.execute(
            f"UPDATE content SET {', '.join(updates)} WHERE id = ?",
            values
        )


# Tag operations

def get_or_create_tag(conn: sqlite3.Connection, tag_name: str) -> int:
    """Get tag ID, creating if needed."""
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM tags WHERE name = ?", (tag_name.lower(),))
    row = cursor.fetchone()
    if row:
        return row["id"]

    cursor.execute("INSERT INTO tags (name) VALUES (?)", (tag_name.lower(),))
    return cursor.lastrowid


def add_tag_to_content(conn: sqlite3.Connection, content_id: int, tag_name: str) -> None:
    """Add a tag to content."""
    tag_id = get_or_create_tag(conn, tag_name)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO content_tags (content_id, tag_id) VALUES (?, ?)",
        (content_id, tag_id)
    )


def get_content_tags(conn: sqlite3.Connection, content_id: int) -> list[str]:
    """Get all tags for content."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT t.name FROM tags t
        JOIN content_tags ct ON t.id = ct.tag_id
        WHERE ct.content_id = ?
        ORDER BY t.name
    """, (content_id,))
    return [row["name"] for row in cursor.fetchall()]


def remove_tag_from_content(conn: sqlite3.Connection, content_id: int, tag_name: str) -> None:
    """Remove a tag from content."""
    cursor = conn.cursor()
    cursor.execute("""
        DELETE FROM content_tags
        WHERE content_id = ? AND tag_id = (SELECT id FROM tags WHERE name = ?)
    """, (content_id, tag_name.lower()))


# Break point operations

def add_break_point(
    conn: sqlite3.Connection,
    content_id: int,
    timestamp_seconds: float,
    confidence: float = 1.0
) -> int:
    """Add a commercial break point."""
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO break_points (content_id, timestamp_seconds, confidence) VALUES (?, ?, ?)",
        (content_id, timestamp_seconds, confidence)
    )
    return cursor.lastrowid


def get_break_points(conn: sqlite3.Connection, content_id: int) -> list[sqlite3.Row]:
    """Get all break points for content."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM break_points WHERE content_id = ? ORDER BY timestamp_seconds",
        (content_id,)
    )
    return cursor.fetchall()


def clear_break_points(conn: sqlite3.Connection, content_id: int) -> None:
    """Clear all break points for content."""
    cursor = conn.cursor()
    cursor.execute("DELETE FROM break_points WHERE content_id = ?", (content_id,))


# Ingest log operations

def log_ingest(
    conn: sqlite3.Connection,
    stage: str,
    status: str,
    content_id: Optional[int] = None,
    message: Optional[str] = None
) -> int:
    """Log an ingest operation."""
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO ingest_log (content_id, stage, status, message) VALUES (?, ?, ?, ?)",
        (content_id, stage, status, message)
    )
    return cursor.lastrowid


# Statistics

def get_stats(conn: sqlite3.Connection) -> dict:
    """Get database statistics."""
    cursor = conn.cursor()

    # Content counts by status
    cursor.execute("""
        SELECT status, COUNT(*) as count
        FROM content
        GROUP BY status
    """)
    status_counts = {row["status"]: row["count"] for row in cursor.fetchall()}

    # Content counts by type
    cursor.execute("""
        SELECT content_type, COUNT(*) as count
        FROM content
        WHERE status = 'ready'
        GROUP BY content_type
    """)
    type_counts = {row["content_type"]: row["count"] for row in cursor.fetchall()}

    # Total duration
    cursor.execute("""
        SELECT SUM(duration_seconds) as total
        FROM content
        WHERE status = 'ready'
    """)
    total_duration = cursor.fetchone()["total"] or 0

    # Tag counts
    cursor.execute("""
        SELECT t.name, COUNT(ct.content_id) as count
        FROM tags t
        LEFT JOIN content_tags ct ON t.id = ct.tag_id
        LEFT JOIN content c ON ct.content_id = c.id AND c.status = 'ready'
        GROUP BY t.id
        ORDER BY count DESC
    """)
    tag_counts = {row["name"]: row["count"] for row in cursor.fetchall()}

    return {
        "by_status": status_counts,
        "by_type": type_counts,
        "total_ready": sum(type_counts.values()),
        "total_duration_hours": total_duration / 3600,
        "by_tag": tag_counts,
    }
