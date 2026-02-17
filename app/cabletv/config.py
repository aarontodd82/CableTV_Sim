"""Configuration loader and management."""

import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .platform import get_drive_root


@dataclass
class ScheduleConfig:
    """Schedule configuration."""
    epoch: str = "2024-01-01T00:00:00"  # Reference point for schedule calculation
    slot_duration: int = 30  # Minutes per slot
    seed: int = 42  # Random seed for deterministic scheduling


@dataclass
class ChannelConfig:
    """Individual channel configuration."""
    number: int
    name: str
    tags: list[str] = field(default_factory=list)
    content_types: list[str] = field(default_factory=lambda: ["show", "movie"])
    commercial_ratio: float = 1.0  # 1.0 = fill remaining time with commercials, 0.0 = no commercials


@dataclass
class IngestConfig:
    """Ingest pipeline configuration."""
    tmdb_api_key: str = ""
    anthropic_api_key: str = ""
    transcode_width: int = 640
    transcode_height: int = 480
    video_bitrate: str = "1500k"
    transcode_threshold: str = "1100k"  # Skip transcode if source bitrate is below this
    audio_bitrate: str = "128k"
    keyframe_interval: int = 30  # GOP size for fast seeking
    widescreen_crop: int = 0  # Percent to crop from each side of 16:9 content (0=full letterbox, 12=moderate, 25=full crop)


@dataclass
class PlaybackConfig:
    """Playback configuration."""
    mpv_ipc_port: int = 9876
    osd_duration: float = 2.0  # Seconds to show channel OSD
    default_channel: int = 2
    screen: int = -1  # Display index for fullscreen (-1 = default/primary)
    overscan: float = 0.0  # Overscan compensation as percentage (e.g. 5.0 = 5% margin on each edge)


@dataclass
class WebConfig:
    """Web interface configuration."""
    host: str = "0.0.0.0"
    port: int = 5000
    debug: bool = False


@dataclass
class GuideConfig:
    """TV Guide channel configuration."""
    enabled: bool = True
    channel_number: int = 14
    promo_duration: int = 20  # Seconds per promo clip
    scroll_speed: float = 3.0  # Seconds per row
    segment_duration: int = 600  # Seconds per generated segment
    regenerate_interval: int = 600  # Seconds between regenerations
    promo_seek_offset: int = 300  # Seconds into content to extract promo clip
    fps: int = 15  # Frames per second for guide video
    width: int = 640
    height: int = 480
    grid_height: int = 240  # Bottom portion for scrolling grid
    promo_height: int = 240  # Top portion for promo video
    background_music: str = ""  # Path to background music file (MP3/WAV)


@dataclass
class Config:
    """Main configuration container."""
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    channels: list[ChannelConfig] = field(default_factory=list)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    playback: PlaybackConfig = field(default_factory=PlaybackConfig)
    web: WebConfig = field(default_factory=WebConfig)
    guide: GuideConfig = field(default_factory=GuideConfig)

    @property
    def channel_map(self) -> dict[int, ChannelConfig]:
        """Get channels indexed by number."""
        return {ch.number: ch for ch in self.channels}


def _parse_channel(data: dict) -> ChannelConfig:
    """Parse a channel configuration from dict."""
    return ChannelConfig(
        number=data.get("number", 2),
        name=data.get("name", "Unknown"),
        tags=data.get("tags", []),
        content_types=data.get("content_types", ["show", "movie"]),
        commercial_ratio=data.get("commercial_ratio", 1.0),
    )


def load_config(config_path: Optional[Path] = None) -> Config:
    """
    Load configuration from YAML file.

    Args:
        config_path: Path to config.yaml, or None to use default location

    Returns:
        Config object with all settings
    """
    if config_path is None:
        config_path = get_drive_root() / "config.yaml"

    config = Config()

    if not config_path.exists():
        # Return defaults if no config file
        return _get_default_config()

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    # Parse schedule settings
    if "schedule" in data:
        sched = data["schedule"]
        config.schedule = ScheduleConfig(
            epoch=sched.get("epoch", config.schedule.epoch),
            slot_duration=sched.get("slot_duration", config.schedule.slot_duration),
            seed=sched.get("seed", config.schedule.seed),
        )

    # Parse channels
    if "channels" in data:
        config.channels = [_parse_channel(ch) for ch in data["channels"]]

    # Parse ingest settings
    if "ingest" in data:
        ing = data["ingest"]
        config.ingest = IngestConfig(
            tmdb_api_key=ing.get("tmdb_api_key", ""),
            anthropic_api_key=ing.get("anthropic_api_key", ""),
            transcode_width=ing.get("transcode_width", 640),
            transcode_height=ing.get("transcode_height", 480),
            video_bitrate=ing.get("video_bitrate", "1500k"),
            transcode_threshold=ing.get("transcode_threshold", "1100k"),
            audio_bitrate=ing.get("audio_bitrate", "128k"),
            keyframe_interval=ing.get("keyframe_interval", 30),
            widescreen_crop=ing.get("widescreen_crop", 0),
        )

    # Parse playback settings
    if "playback" in data:
        pb = data["playback"]
        config.playback = PlaybackConfig(
            mpv_ipc_port=pb.get("mpv_ipc_port", 9876),
            osd_duration=pb.get("osd_duration", 2.0),
            default_channel=pb.get("default_channel", 2),
            screen=pb.get("screen", -1),
            overscan=pb.get("overscan", 0.0),
        )

    # Parse web settings
    if "web" in data:
        web = data["web"]
        config.web = WebConfig(
            host=web.get("host", "0.0.0.0"),
            port=web.get("port", 5000),
            debug=web.get("debug", False),
        )

    # Parse guide settings
    if "guide" in data:
        g = data["guide"]
        config.guide = GuideConfig(
            enabled=g.get("enabled", True),
            channel_number=g.get("channel_number", 2),
            promo_duration=g.get("promo_duration", 20),
            scroll_speed=g.get("scroll_speed", 3.0),
            segment_duration=g.get("segment_duration", 600),
            regenerate_interval=g.get("regenerate_interval", 600),
            promo_seek_offset=g.get("promo_seek_offset", 300),
            fps=g.get("fps", 15),
            width=g.get("width", 640),
            height=g.get("height", 480),
            grid_height=g.get("grid_height", 240),
            promo_height=g.get("promo_height", 240),
            background_music=g.get("background_music", ""),
        )

    return config


def _get_default_config() -> Config:
    """Get default configuration with sample channels."""
    config = Config()
    config.channels = [
        ChannelConfig(number=2, name="WKRP", tags=["sitcom", "comedy"], content_types=["show"]),
        ChannelConfig(number=3, name="Action", tags=["action", "adventure"], content_types=["movie", "show"]),
        ChannelConfig(number=4, name="Drama", tags=["drama"], content_types=["show", "movie"]),
        ChannelConfig(number=5, name="Sci-Fi", tags=["scifi", "science-fiction"], content_types=["movie", "show"]),
        ChannelConfig(number=6, name="Comedy", tags=["comedy"], content_types=["movie", "show"]),
        ChannelConfig(number=7, name="Horror", tags=["horror", "thriller"], content_types=["movie"]),
        ChannelConfig(number=8, name="Classic TV", tags=["classic", "vintage"], content_types=["show"]),
        ChannelConfig(number=9, name="Family", tags=["family", "kids"], content_types=["movie", "show"]),
        ChannelConfig(number=10, name="Documentary", tags=["documentary"], content_types=["movie", "show"]),
        ChannelConfig(number=11, name="Mystery", tags=["mystery", "crime"], content_types=["show", "movie"]),
    ]
    return config


def save_config(config: Config, config_path: Optional[Path] = None) -> None:
    """Save configuration to YAML file."""
    if config_path is None:
        config_path = get_drive_root() / "config.yaml"

    data = {
        "schedule": {
            "epoch": config.schedule.epoch,
            "slot_duration": config.schedule.slot_duration,
            "seed": config.schedule.seed,
        },
        "channels": [
            {
                "number": ch.number,
                "name": ch.name,
                "tags": ch.tags,
                "content_types": ch.content_types,
                "commercial_ratio": ch.commercial_ratio,
            }
            for ch in config.channels
        ],
        "ingest": {
            "tmdb_api_key": config.ingest.tmdb_api_key,
            "anthropic_api_key": config.ingest.anthropic_api_key,
            "transcode_width": config.ingest.transcode_width,
            "transcode_height": config.ingest.transcode_height,
            "video_bitrate": config.ingest.video_bitrate,
            "transcode_threshold": config.ingest.transcode_threshold,
            "audio_bitrate": config.ingest.audio_bitrate,
            "keyframe_interval": config.ingest.keyframe_interval,
            "widescreen_crop": config.ingest.widescreen_crop,
        },
        "playback": {
            "mpv_ipc_port": config.playback.mpv_ipc_port,
            "osd_duration": config.playback.osd_duration,
            "default_channel": config.playback.default_channel,
            "screen": config.playback.screen,
            "overscan": config.playback.overscan,
        },
        "web": {
            "host": config.web.host,
            "port": config.web.port,
            "debug": config.web.debug,
        },
        "guide": {
            "enabled": config.guide.enabled,
            "channel_number": config.guide.channel_number,
            "promo_duration": config.guide.promo_duration,
            "scroll_speed": config.guide.scroll_speed,
            "segment_duration": config.guide.segment_duration,
            "regenerate_interval": config.guide.regenerate_interval,
            "promo_seek_offset": config.guide.promo_seek_offset,
            "fps": config.guide.fps,
            "width": config.guide.width,
            "height": config.guide.height,
            "grid_height": config.guide.grid_height,
            "promo_height": config.guide.promo_height,
            "background_music": config.guide.background_music,
        },
    }

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
