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
    commercial_ratio: float = 0.0  # 0.0 = no commercials, 0.2 = 20% commercial time


@dataclass
class IngestConfig:
    """Ingest pipeline configuration."""
    tmdb_api_key: str = ""
    anthropic_api_key: str = ""
    transcode_width: int = 640
    transcode_height: int = 480
    video_bitrate: str = "1500k"
    audio_bitrate: str = "128k"
    keyframe_interval: int = 30  # GOP size for fast seeking
    widescreen_crop: int = 0  # Percent to crop from each side of 16:9 content (0=full letterbox, 12=moderate, 25=full crop)


@dataclass
class PlaybackConfig:
    """Playback configuration."""
    mpv_ipc_port: int = 9876
    osd_duration: float = 2.0  # Seconds to show channel OSD
    default_channel: int = 2


@dataclass
class WebConfig:
    """Web interface configuration."""
    host: str = "0.0.0.0"
    port: int = 5000
    debug: bool = False


@dataclass
class Config:
    """Main configuration container."""
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    channels: list[ChannelConfig] = field(default_factory=list)
    ingest: IngestConfig = field(default_factory=IngestConfig)
    playback: PlaybackConfig = field(default_factory=PlaybackConfig)
    web: WebConfig = field(default_factory=WebConfig)

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
        commercial_ratio=data.get("commercial_ratio", 0.0),
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
        )

    # Parse web settings
    if "web" in data:
        web = data["web"]
        config.web = WebConfig(
            host=web.get("host", "0.0.0.0"),
            port=web.get("port", 5000),
            debug=web.get("debug", False),
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
            "audio_bitrate": config.ingest.audio_bitrate,
            "keyframe_interval": config.ingest.keyframe_interval,
            "widescreen_crop": config.ingest.widescreen_crop,
        },
        "playback": {
            "mpv_ipc_port": config.playback.mpv_ipc_port,
            "osd_duration": config.playback.osd_duration,
            "default_channel": config.playback.default_channel,
        },
        "web": {
            "host": config.web.host,
            "port": config.web.port,
            "debug": config.web.debug,
        },
    }

    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
