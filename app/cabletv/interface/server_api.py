"""Server API endpoints for remote clients."""

from datetime import datetime
from flask import Blueprint, jsonify, request, send_from_directory
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
_guide_generator = None
_weather_generator = None


def register_server_api(app, config: Config, server_manager: ServerScheduleManager,
                        guide_generator=None, weather_generator=None):
    """Register server API blueprint with the Flask app."""
    global _server_manager, _config, _drive_root, _guide_generator, _weather_generator
    _server_manager = server_manager
    _config = config
    _drive_root = get_drive_root()
    _guide_generator = guide_generator
    _weather_generator = weather_generator
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


def _serialize_now_playing(np) -> dict:
    """Serialize a NowPlaying object to JSON-safe dict."""
    e = np.entry
    result = {
        "entry": {
            "content_id": e.content_id,
            "title": e.title,
            "content_type": e.content_type,
            "start_time": e.start_time.timestamp(),
            "end_time": e.end_time.timestamp(),
            "duration_seconds": e.duration_seconds,
            "file_path": e.file_path,
            "channel_number": e.channel_number,
            "slot_end_time": e.slot_end_time.timestamp(),
            "artist": e.artist,
            "year": e.year,
            "series_name": e.series_name,
            "season": e.season,
            "episode": e.episode,
            "packed_episodes": [
                list(ep) for ep in e.packed_episodes
            ] if e.packed_episodes else None,
        },
        "elapsed_seconds": np.elapsed_seconds,
        "remaining_seconds": np.remaining_seconds,
        "seek_position": np.seek_position,
        "is_commercial": np.is_commercial,
        "is_end_bumper": np.is_end_bumper,
        "pack_count": np.pack_count,
    }

    if np.commercial:
        c = np.commercial
        result["commercial"] = {
            "content_id": c.content_id,
            "title": c.title,
            "duration_seconds": c.duration_seconds,
            "file_path": c.file_path,
            "seek_position": c.seek_position,
            "remaining_seconds": c.remaining_seconds,
            "channel_number": c.channel_number,
            "main_content_id": c.main_content_id,
            "main_content_title": c.main_content_title,
        }

    # Include advance info so remote doesn't need get_channel_groups()
    if not np.is_commercial and e.series_name:
        gk = e.series_name
    elif not np.is_commercial:
        gk = f"standalone_{e.content_id}"
    else:
        gk = None

    if gk:
        ch_config = _config.channel_map.get(e.channel_number) if _config else None
        group_size = 1
        if ch_config:
            engine = _server_manager.engine
            for g in engine.get_channel_groups(ch_config):
                if g.group_key == gk:
                    group_size = len(g.items)
                    break
        from ..utils.time_utils import get_slot_number
        block_start_slot = get_slot_number(
            e.start_time, engine.epoch, engine.slot_duration)
        result["advance_info"] = {
            "group_key": gk,
            "group_size": group_size,
            "block_start_slot": block_start_slot,
            "pack_count": np.pack_count,
        }

    return result


@server_bp.route("/api/server/what-is-on/<int:channel_number>")
def what_is_on(channel_number):
    """Return what's currently playing on a channel.

    The server is the single source of truth. Its block cache and
    position state are always correct. Remote clients call this
    instead of running their own schedule engine.
    """
    if not _server_manager:
        return jsonify({"error": "Server not initialized"}), 500

    engine = _server_manager.engine

    # Optional: query for a specific time
    when_ts = request.args.get("when", type=float)
    when = datetime.fromtimestamp(when_ts) if when_ts else None

    np = engine.what_is_on(channel_number, when)
    if not np:
        return jsonify(None), 404

    return jsonify(_serialize_now_playing(np))


@server_bp.route("/api/server/upcoming/<int:channel_number>")
def upcoming(channel_number):
    """Return upcoming programs on a channel (for info bumpers)."""
    if not _server_manager:
        return jsonify({"error": "Server not initialized"}), 500

    count = request.args.get("count", 3, type=int)
    items = _server_manager.engine.get_upcoming(channel_number, count)
    return jsonify({
        "upcoming": [
            {"start_time": t.timestamp(), "title": title}
            for t, title in items
        ]
    })


@server_bp.route("/api/server/next-airing/<int:channel_number>")
def next_airing(channel_number):
    """Return when a series next airs on a channel."""
    if not _server_manager:
        return jsonify({"error": "Server not initialized"}), 500

    series = request.args.get("series", "")
    after_ts = request.args.get("after", type=float)
    after = datetime.fromtimestamp(after_ts) if after_ts else None

    result = _server_manager.engine.find_next_airing(
        channel_number, series, after)
    return jsonify({
        "next_time": result.timestamp() if result else None,
    })


@server_bp.route("/api/server/time")
def server_time():
    """Return server's current time for clock offset calculation."""
    return jsonify({"time": datetime.now().timestamp()})


def _segment_response(generator, segment_type: str):
    """Build JSON response for a guide or weather segment endpoint."""
    if not generator:
        return jsonify({"error": f"{segment_type} not available"}), 404

    segment_info = generator.get_current_segment()
    if not segment_info:
        return jsonify({"error": f"{segment_type} segment not ready"}), 404

    file_path, generation_time, duration = segment_info

    # Convert local path to relative URL under /media/
    try:
        rel = Path(file_path).relative_to(_drive_root)
    except (ValueError, TypeError):
        return jsonify({"error": "Cannot resolve segment path"}), 500

    url_path = str(rel).replace("\\", "/")
    return jsonify({
        "generation_time": generation_time.isoformat(),
        "duration": duration,
        "url": f"/media/{url_path}",
    })


@server_bp.route("/api/server/guide-segment")
def guide_segment():
    """Return metadata and URL for the current guide segment."""
    return _segment_response(_guide_generator, "Guide")


@server_bp.route("/api/server/weather-segment")
def weather_segment():
    """Return metadata and URL for the current weather segment."""
    return _segment_response(_weather_generator, "Weather")


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
