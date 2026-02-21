"""Server API endpoints for remote clients."""

import gzip
import json
from datetime import datetime
from flask import Blueprint, jsonify, request, send_from_directory, make_response
from pathlib import Path
from typing import Optional

from ..config import Config
from ..platform import get_drive_root
from ..schedule.server_manager import ServerScheduleManager

server_bp = Blueprint("server", __name__)

# Set by register_server_api()
_server_manager: Optional[ServerScheduleManager] = None
_config: Optional[Config] = None
_drive_root: Optional[Path] = None


def register_server_api(app, config: Config, server_manager: ServerScheduleManager):
    """Register server API blueprint with the Flask app."""
    global _server_manager, _config, _drive_root
    _server_manager = server_manager
    _config = config
    _drive_root = get_drive_root()
    app.register_blueprint(server_bp)


@server_bp.route("/api/server/info")
def server_info():
    """Return server info needed by remote clients to sync schedules."""
    if not _server_manager or not _config:
        return jsonify({"error": "Server not initialized"}), 500

    channels = [
        {
            "number": ch.number,
            "name": ch.name,
            "tags": ch.tags,
            "content_types": ch.content_types,
            "commercial_ratio": ch.commercial_ratio,
        }
        for ch in _config.channels
    ]

    return jsonify({
        "seed": _server_manager.seed,
        "epoch": _config.schedule.epoch,
        "slot_duration": _config.schedule.slot_duration,
        "channels": channels,
        "guide": {
            "enabled": _config.guide.enabled,
            "channel_number": _config.guide.channel_number,
        },
        "weather": {
            "enabled": _config.weather.enabled,
            "channel_number": _config.weather.channel_number,
        },
    })


@server_bp.route("/api/server/advance", methods=["POST"])
def server_advance():
    """Advance a series position (consumed-slot tracking prevents double-advance)."""
    if not _server_manager:
        return jsonify({"error": "Server not initialized"}), 500

    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    try:
        advanced = _server_manager.try_advance(
            channel_number=data["channel_number"],
            group_key=data["group_key"],
            num_items=data["num_items"],
            block_start_slot=data["block_start_slot"],
            advance_by=data.get("advance_by", 1),
            content_id=data.get("content_id", 0),
        )
    except KeyError as e:
        return jsonify({"error": f"Missing field: {e}"}), 400

    return jsonify({
        "advanced": advanced,
        "seed": _server_manager.seed,
    })


@server_bp.route("/api/server/positions")
def server_positions():
    """Return all series positions for remote clients."""
    if not _server_manager:
        return jsonify({"error": "Server not initialized"}), 500

    positions = _server_manager.get_all_positions()
    return jsonify({"positions": positions})


@server_bp.route("/api/server/schedule-data")
def schedule_data():
    """Return all data a remote client needs to compute schedules.

    Replaces the old approach of copying cabletv.db (which had WAL
    sync issues). One API call gives the remote everything it needs
    to pre-populate its ScheduleEngine caches — no DB required.
    """
    if not _server_manager or not _config:
        return jsonify({"error": "Server not initialized"}), 500

    engine = _server_manager.engine

    # Only send fields the remote engine actually needs
    _POOL_FIELDS = (
        "id", "title", "content_type", "series_name", "season",
        "episode", "year", "duration_seconds", "normalized_path",
        "original_path", "artist",
    )

    # Channel pools (content per channel, already filtered by tags/types)
    channel_pools = {}
    all_content_ids = set()
    for ch in _config.channels:
        pool = engine.get_channel_pool(ch)
        channel_pools[str(ch.number)] = [
            {k: item[k] for k in _POOL_FIELDS if k in item}
            for item in pool
        ]
        for item in pool:
            all_content_ids.add(item["id"])

    # Break points — single bulk query instead of one per content item
    break_points = {}
    if all_content_ids:
        from ..db import db_connection
        with db_connection() as conn:
            placeholders = ",".join("?" for _ in all_content_ids)
            rows = conn.execute(
                f"SELECT content_id, timestamp_seconds FROM break_points "
                f"WHERE content_id IN ({placeholders}) "
                f"ORDER BY content_id, timestamp_seconds",
                list(all_content_ids),
            ).fetchall()
            for row in rows:
                cid_str = str(row["content_id"])
                break_points.setdefault(cid_str, []).append(row["timestamp_seconds"])

    # Commercial pool (also strip to needed fields)
    from ..schedule.commercials import get_commercial_pool
    _COMM_FIELDS = ("id", "title", "duration_seconds", "normalized_path",
                    "original_path", "content_type")
    commercials = [
        {k: c[k] for k in _COMM_FIELDS if k in c}
        for c in get_commercial_pool()
    ]

    # Positions
    positions = _server_manager.get_all_positions()

    # Gzip the response — 30+ MB uncompressed, ~2 MB compressed
    payload = json.dumps({
        "channel_pools": channel_pools,
        "break_points": break_points,
        "commercials": commercials,
        "positions": positions,
    })
    compressed = gzip.compress(payload.encode(), compresslevel=1)
    response = make_response(compressed)
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Type"] = "application/json"
    return response


@server_bp.route("/api/server/time")
def server_time():
    """Return server's current time for clock offset calculation."""
    return jsonify({"time": datetime.now().timestamp()})


@server_bp.route("/media/<path:filepath>")
def serve_media(filepath):
    """Serve content files over HTTP for remote playback.

    mpv streams these with range requests for fast seeking —
    much faster than SMB for channel switching.
    """
    if not _drive_root:
        return jsonify({"error": "Server not initialized"}), 500

    # Security: prevent path traversal
    requested = (_drive_root / filepath).resolve()
    if not str(requested).startswith(str(_drive_root.resolve())):
        return jsonify({"error": "Forbidden"}), 403

    return send_from_directory(
        str(_drive_root),
        filepath,
        conditional=True,  # Enables range requests (206 Partial Content)
    )
