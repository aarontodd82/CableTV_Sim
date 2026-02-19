"""Flask web interface for remote control."""

from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory
from pathlib import Path

from ..config import Config
from ..schedule.engine import ScheduleEngine
from ..playback.engine import PlaybackEngine


# Flask app instance (will be configured by create_app)
app = Flask(__name__)

# These will be set by create_app
_config: Config = None
_schedule: ScheduleEngine = None
_playback: PlaybackEngine = None


def create_app(
    config: Config,
    schedule_engine: ScheduleEngine,
    playback_engine: PlaybackEngine
) -> Flask:
    """
    Create and configure the Flask application.

    Args:
        config: Application config
        schedule_engine: Schedule engine instance
        playback_engine: Playback engine instance

    Returns:
        Configured Flask app
    """
    global _config, _schedule, _playback
    _config = config
    _schedule = schedule_engine
    _playback = playback_engine

    # Configure static files
    static_dir = Path(__file__).parent / "static"
    app.static_folder = str(static_dir)

    return app


# Static file routes
@app.route("/")
def index():
    """Serve the remote control page."""
    return send_from_directory(app.static_folder, "index.html")


@app.route("/style.css")
def style():
    """Serve CSS."""
    return send_from_directory(app.static_folder, "style.css")


@app.route("/remote.js")
def script():
    """Serve JavaScript."""
    return send_from_directory(app.static_folder, "remote.js")


# API routes
@app.route("/api/status")
def api_status():
    """Get current playback status."""
    if not _playback:
        return jsonify({"error": "Playback engine not initialized"}), 500

    status = _playback.get_status()
    return jsonify(status)


@app.route("/api/channel/<int:channel_number>", methods=["POST"])
def api_tune_channel(channel_number: int):
    """Tune to a specific channel."""
    if not _playback:
        return jsonify({"error": "Playback engine not initialized"}), 500

    success = _playback.tune_to(channel_number)
    if success:
        return jsonify({"success": True, "channel": channel_number})
    else:
        return jsonify({"success": False, "error": "Failed to tune to channel"}), 400


@app.route("/api/channel/up", methods=["POST"])
def api_channel_up():
    """Channel up."""
    if not _playback:
        return jsonify({"error": "Playback engine not initialized"}), 500

    success = _playback.channel_up()
    return jsonify({
        "success": success,
        "channel": _playback.current_channel
    })


@app.route("/api/channel/down", methods=["POST"])
def api_channel_down():
    """Channel down."""
    if not _playback:
        return jsonify({"error": "Playback engine not initialized"}), 500

    success = _playback.channel_down()
    return jsonify({
        "success": success,
        "channel": _playback.current_channel
    })


@app.route("/api/info", methods=["POST"])
def api_info():
    """Show the 'next airing' info overlay on screen."""
    if not _playback:
        return jsonify({"error": "Playback engine not initialized"}), 500

    shown = _playback.show_info_overlay()
    return jsonify({"success": shown})


@app.route("/api/channels")
def api_channels():
    """Get list of available channels."""
    if not _config:
        return jsonify({"error": "Config not initialized"}), 500

    channels = [
        {
            "number": ch.number,
            "name": ch.name,
            "tags": ch.tags,
        }
        for ch in _config.channels
    ]
    return jsonify({"channels": channels})


@app.route("/api/guide")
def api_guide():
    """Get TV guide data."""
    if not _schedule:
        return jsonify({"error": "Schedule engine not initialized"}), 500

    # Get optional parameters
    hours = request.args.get("hours", 3, type=int)
    channel = request.args.get("channel", None, type=int)

    channels = [channel] if channel else None
    guide_data = _schedule.get_guide_data(hours=hours, channels=channels)

    # Convert to serializable format
    result = {}
    for channel_num, entries in guide_data.items():
        result[channel_num] = [
            {
                "title": e.title,
                "content_type": e.content_type,
                "start_time": e.start_time.isoformat(),
                "end_time": e.end_time.isoformat(),
                "duration_seconds": e.duration_seconds,
            }
            for e in entries
        ]

    return jsonify({"guide": result})


@app.route("/api/now")
def api_now():
    """Get what's on now for all channels."""
    if not _schedule or not _config:
        return jsonify({"error": "Not initialized"}), 500

    now_data = {}
    for ch in _config.channels:
        now_playing = _schedule.what_is_on(ch.number)
        if now_playing:
            now_data[ch.number] = {
                "channel_name": ch.name,
                "title": now_playing.entry.title,
                "elapsed": now_playing.elapsed_seconds,
                "remaining": now_playing.remaining_seconds,
                "duration": now_playing.entry.duration_seconds,
            }
        else:
            now_data[ch.number] = {
                "channel_name": ch.name,
                "title": None,
            }

    return jsonify({"now": now_data})


def run_server(
    config: Config,
    schedule_engine: ScheduleEngine,
    playback_engine: PlaybackEngine,
    threaded: bool = True
) -> None:
    """
    Run the Flask web server.

    Args:
        config: Application config
        schedule_engine: Schedule engine instance
        playback_engine: Playback engine instance
        threaded: Run in threaded mode
    """
    create_app(config, schedule_engine, playback_engine)

    app.run(
        host=config.web.host,
        port=config.web.port,
        debug=config.web.debug,
        threaded=threaded,
        use_reloader=False,  # Disable reloader to avoid issues with threads
    )
