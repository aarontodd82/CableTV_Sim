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


def _run_migrations(db_path: Path) -> None:
    """Run schema migrations with foreign keys disabled.

    Must use a separate connection with FK OFF so that ALTER TABLE RENAME
    doesn't corrupt FK references in dependent tables.
    Always creates a backup before making any changes.
    """
    import shutil

    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")

        # Check if content table exists at all
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='content'")
        if not cursor.fetchone():
            return  # Fresh DB, init_database will create it

        # Check what needs migrating
        cursor.execute("PRAGMA table_info(content)")
        columns = {row[1] for row in cursor.fetchall()}

        cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='content'")
        row = cursor.fetchone()
        create_sql = row[0] if row else ""

        needs_artist = "artist" not in columns
        needs_music = "'music'" not in create_sql

        # Check for broken FK references from previous bad migration
        has_broken_fks = False
        for table_name in ('content_tags', 'break_points', 'ingest_log'):
            cursor.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            )
            r = cursor.fetchone()
            if r and r[0] and 'content_old' in r[0]:
                has_broken_fks = True
                break

        if not needs_artist and not needs_music and not has_broken_fks:
            return

        # Back up database before any destructive changes
        backup_path = db_path.with_suffix(".db.bak")
        conn.close()
        shutil.copy2(str(db_path), str(backup_path))
        print(f"Database backup created: {backup_path}")
        conn = sqlite3.connect(str(db_path), timeout=30.0)
        cursor = conn.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")

        print("Running database migration...")
        cursor.execute("BEGIN")

        # Add artist column if missing
        if needs_artist:
            cursor.execute("ALTER TABLE content ADD COLUMN artist TEXT")

        # Recreate content table with updated CHECK constraint
        if needs_music:
            cursor.execute("ALTER TABLE content RENAME TO _content_migrate")
            cursor.execute("""
                CREATE TABLE content (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    content_type TEXT NOT NULL CHECK(content_type IN ('movie', 'show', 'commercial', 'bumper', 'music')),
                    series_name TEXT,
                    season INTEGER,
                    episode INTEGER,
                    year INTEGER,
                    duration_seconds REAL NOT NULL,
                    original_path TEXT NOT NULL,
                    normalized_path TEXT,
                    file_hash TEXT UNIQUE NOT NULL,
                    tmdb_id INTEGER,
                    artist TEXT,
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
            cursor.execute("""
                INSERT INTO content (id, title, content_type, series_name, season, episode,
                    year, duration_seconds, original_path, normalized_path, file_hash,
                    tmdb_id, artist, status, error_message, width, height, aspect_ratio,
                    codec, created_at, updated_at)
                SELECT id, title, content_type, series_name, season, episode,
                    year, duration_seconds, original_path, normalized_path, file_hash,
                    tmdb_id, artist, status, error_message, width, height, aspect_ratio,
                    codec, created_at, updated_at
                FROM _content_migrate
            """)
            cursor.execute("DROP TABLE _content_migrate")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_content_status ON content(status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_content_type ON content(content_type)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_content_hash ON content(file_hash)")

        # Repair broken FK references in dependent tables
        if has_broken_fks:
            for table_name, create_ddl in [
                ('content_tags', """
                    CREATE TABLE content_tags (
                        content_id INTEGER NOT NULL,
                        tag_id INTEGER NOT NULL,
                        PRIMARY KEY (content_id, tag_id),
                        FOREIGN KEY (content_id) REFERENCES content(id) ON DELETE CASCADE,
                        FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
                    )
                """),
                ('break_points', """
                    CREATE TABLE break_points (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        content_id INTEGER NOT NULL,
                        timestamp_seconds REAL NOT NULL,
                        confidence REAL DEFAULT 1.0,
                        FOREIGN KEY (content_id) REFERENCES content(id) ON DELETE CASCADE
                    )
                """),
                ('ingest_log', """
                    CREATE TABLE ingest_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        content_id INTEGER,
                        stage TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('started', 'completed', 'failed')),
                        message TEXT,
                        created_at TEXT NOT NULL DEFAULT (datetime('now')),
                        FOREIGN KEY (content_id) REFERENCES content(id) ON DELETE SET NULL
                    )
                """),
            ]:
                cursor.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table_name,)
                )
                r = cursor.fetchone()
                if r and r[0] and 'content_old' in r[0]:
                    cursor.execute(f"ALTER TABLE {table_name} RENAME TO _{table_name}_fix")
                    cursor.execute(create_ddl)
                    cursor.execute(f"INSERT INTO {table_name} SELECT * FROM _{table_name}_fix")
                    cursor.execute(f"DROP TABLE _{table_name}_fix")

            # Recreate indexes on repaired tables
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_content_tags_content ON content_tags(content_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_content_tags_tag ON content_tags(tag_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_break_points_content ON break_points(content_id)")

        # Clean up any leftover content_old from previous broken migration
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='content_old'")
        if cursor.fetchone():
            cursor.execute("DROP TABLE content_old")

        conn.commit()
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

    # Run migrations first (separate connection, FK OFF)
    _run_migrations(db_path)

    with db_connection(db_path) as conn:
        cursor = conn.cursor()

        # Content table - main registry of all video content
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS content (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content_type TEXT NOT NULL CHECK(content_type IN ('movie', 'show', 'commercial', 'bumper', 'music')),
                series_name TEXT,
                season INTEGER,
                episode INTEGER,
                year INTEGER,
                duration_seconds REAL NOT NULL,
                original_path TEXT NOT NULL,
                normalized_path TEXT,
                file_hash TEXT UNIQUE NOT NULL,
                tmdb_id INTEGER,
                artist TEXT,
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
            ("gameshow", "Game shows and physical competitions"),
            ("educational", "Science, nature, and learning content"),
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
    artist: Optional[str] = None,
) -> int:
    """Add new content to the database."""
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO content (
            title, content_type, series_name, season, episode, year,
            duration_seconds, original_path, file_hash,
            width, height, aspect_ratio, codec, artist
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        title, content_type, series_name, season, episode, year,
        duration_seconds, original_path, file_hash,
        width, height, aspect_ratio, codec, artist
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


def search_content(conn: sqlite3.Connection, query: str) -> list[sqlite3.Row]:
    """Search content by title, series name, or original filename (case-insensitive)."""
    cursor = conn.cursor()
    pattern = f"%{query}%"
    cursor.execute("""
        SELECT * FROM content
        WHERE title LIKE ? OR series_name LIKE ? OR original_path LIKE ?
        ORDER BY title
    """, (pattern, pattern, pattern))
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
    artist: Optional[str] = None,
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
    if artist is not None:
        updates.append("artist = ?")
        values.append(artist)

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


def clear_content_tags(conn: sqlite3.Connection, content_id: int) -> None:
    """Remove all tags from content."""
    cursor = conn.cursor()
    cursor.execute("DELETE FROM content_tags WHERE content_id = ?", (content_id,))


def get_all_series_tags(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Get the most common tag set for each series in the database.

    Returns:
        Dict mapping series_name -> list of tag names (most common set)
    """
    cursor = conn.cursor()
    # Get all series with identified/ready content
    cursor.execute("""
        SELECT DISTINCT series_name FROM content
        WHERE series_name IS NOT NULL AND status IN ('identified', 'ready')
    """)
    series_names = [row["series_name"] for row in cursor.fetchall()]

    result: dict[str, list[str]] = {}
    for name in series_names:
        # Get tag set for each episode, find the most common one
        cursor.execute("""
            SELECT c.id FROM content c
            WHERE c.series_name = ? AND c.status IN ('identified', 'ready')
        """, (name,))
        episode_ids = [row["id"] for row in cursor.fetchall()]

        if not episode_ids:
            continue

        # Count how often each tag set appears
        tag_set_counts: dict[tuple, int] = {}
        for eid in episode_ids:
            tags = tuple(sorted(get_content_tags(conn, eid)))
            tag_set_counts[tags] = tag_set_counts.get(tags, 0) + 1

        # Most common tag set
        best = max(tag_set_counts, key=tag_set_counts.get)
        if best:
            result[name] = list(best)

    return result


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


def delete_content(conn: sqlite3.Connection, content_id: int) -> bool:
    """Delete content and all associated data (tags, break points, ingest log).

    Foreign keys with ON DELETE CASCADE handle cleanup automatically.

    Returns:
        True if a row was deleted, False if ID not found
    """
    cursor = conn.cursor()
    cursor.execute("DELETE FROM content WHERE id = ?", (content_id,))
    return cursor.rowcount > 0


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
