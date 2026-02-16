"""Promo clip generation for TV Guide channel top half (Prevue-style)."""

import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from ..config import GuideConfig
from ..platform import get_ffmpeg_path, get_drive_root
from ..schedule.engine import ScheduleEngine, ScheduleEntry
from .renderer import _load_font, _load_bold_font, BG_COLOR


# Layout: left text panel with blue-to-black gradient, video flush right
TEXT_PANEL_WIDTH = 210    # Solid blue region with text
GRADIENT_WIDTH = 50       # Blue-to-black fade zone
VIDEO_LEFT = TEXT_PANEL_WIDTH + GRADIENT_WIDTH  # Where video starts (260px)


def _render_promo_background(
    promo_info: dict,
    width: int,
    height: int,
) -> Image.Image:
    """
    Render the Prevue-style background frame (RGB, no alpha).

    Left side: solid dark blue with text info.
    Middle: gradient from dark blue to black.
    Right side: black (video gets placed on top of this).
    """
    img = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Fill left panel with solid dark blue
    draw.rectangle([0, 0, TEXT_PANEL_WIDTH - 1, height - 1], fill=BG_COLOR)

    # Draw gradient from blue to black
    r0, g0, b0 = BG_COLOR
    for x in range(GRADIENT_WIDTH):
        progress = x / GRADIENT_WIDTH
        r = int(r0 * (1.0 - progress))
        g = int(g0 * (1.0 - progress))
        b = int(b0 * (1.0 - progress))
        draw.line([(TEXT_PANEL_WIDTH + x, 0), (TEXT_PANEL_WIDTH + x, height - 1)], fill=(r, g, b))

    # Load fonts
    channel_font = _load_bold_font(20)
    title_font = _load_bold_font(16)
    info_font = _load_font(13)

    # Text layout on left panel
    text_x = 16
    center_y = height // 2

    # Channel name (yellow)
    channel_name = promo_info["channel_name"]
    draw.text(
        (text_x, center_y - 55),
        channel_name,
        fill=(255, 255, 100),
        font=channel_font,
    )

    # Title in quotes (white)
    title = promo_info["title"]
    max_chars = 18
    if len(title) > max_chars:
        break_at = title.rfind(" ", 0, max_chars)
        if break_at < 8:
            break_at = max_chars
        line1 = title[:break_at]
        line2 = title[break_at:].strip()
        if len(line2) > max_chars:
            line2 = line2[:max_chars - 1] + "\u2026"
        draw.text((text_x, center_y - 22), f'"{line1}', fill=(255, 255, 255), font=title_font)
        draw.text((text_x, center_y - 2), f'{line2}"', fill=(255, 255, 255), font=title_font)
    else:
        draw.text((text_x, center_y - 15), f'"{title}"', fill=(255, 255, 255), font=title_font)

    # Day + time + channel
    start_time: datetime = promo_info["start_time"]
    day_str = start_time.strftime("%A")
    time_str = start_time.strftime("%I:%M %p").lstrip("0")
    ch_num = promo_info["channel"]

    draw.text((text_x, center_y + 22), day_str, fill=(180, 180, 180), font=info_font)
    draw.text((text_x, center_y + 40), time_str, fill=(180, 180, 180), font=info_font)
    draw.text((text_x, center_y + 58), f"Channel {ch_num}", fill=(150, 150, 150), font=info_font)

    return img


def select_promo_content(
    schedule_engine: ScheduleEngine,
    guide_config: GuideConfig,
    count: int = 10,
) -> list[dict]:
    """
    Select upcoming content items to feature as promo clips.

    Picks random channels at random future times (2-8 hours ahead)
    to get a diverse set of real upcoming content with minimal queries.

    Args:
        schedule_engine: The schedule engine for looking up what's on
        guide_config: Guide configuration
        count: Number of promos to select

    Returns:
        List of dicts with keys: channel, title, start_time, file_path, content_type
    """
    import random

    now = datetime.now()
    guide_ch = guide_config.channel_number

    # Regular channels only (skip guide + continuous/music)
    regular_channels = [
        ch.number for ch in schedule_engine.config.channels
        if ch.number != guide_ch and ch.commercial_ratio != 0.0
    ]

    if not regular_channels:
        return []

    print("      Sampling schedule for promo content...")
    promos = []
    seen_content_ids = set()
    rng = random.Random()

    # Sample random channels at random future times
    attempts = count * 4  # Try more than we need to account for dupes/missing files
    for _ in range(attempts):
        if len(promos) >= count:
            break

        ch_num = rng.choice(regular_channels)
        hours_ahead = rng.uniform(2, 8)
        future_time = now + timedelta(hours=hours_ahead)

        np = schedule_engine.what_is_on(ch_num, future_time)
        if not np or np.is_commercial:
            continue
        if np.entry.content_id in seen_content_ids:
            continue

        root = get_drive_root()
        file_path = root / np.entry.file_path
        if not file_path.exists():
            continue

        seen_content_ids.add(np.entry.content_id)
        channel_config = schedule_engine.config.channel_map.get(ch_num)
        channel_name = channel_config.name if channel_config else f"Ch {ch_num}"

        promos.append({
            "channel": ch_num,
            "channel_name": channel_name,
            "title": np.entry.title,
            "start_time": np.entry.start_time,
            "file_path": str(file_path),
            "content_type": np.entry.content_type,
            "duration": np.entry.duration_seconds,
        })

    return promos


def generate_promo_clip(
    promo_info: dict,
    output_path: Path,
    guide_config: GuideConfig,
) -> bool:
    """
    Generate a single Prevue-style promo clip.

    Layout: solid blue text panel on left, blue-to-black gradient,
    video flush-right. Video is scaled to fill height and placed
    at the right edge of the frame.

    Args:
        promo_info: Dict with channel, title, start_time, file_path
        output_path: Where to write the output clip
        guide_config: Guide configuration

    Returns:
        True if successful
    """
    ffmpeg = get_ffmpeg_path()
    duration = guide_config.promo_duration
    seek = guide_config.promo_seek_offset
    width = guide_config.width   # 640
    height = guide_config.promo_height  # 160

    # Don't seek past 80% of the content
    content_duration = promo_info.get("duration", 600)
    if seek > content_duration * 0.8:
        seek = max(0, int(content_duration * 0.3))

    # Render background with text panel and gradient
    bg = _render_promo_background(promo_info, width, height)

    bg_tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    bg_path = bg_tmp.name
    bg_tmp.close()
    bg.save(bg_path)

    try:
        # FFmpeg: scale source video to fill height, place flush-right on background
        # overlay=W-w:0 puts the video at the right edge
        cmd = [
            ffmpeg,
            "-ss", str(seek),
            "-i", promo_info["file_path"],  # input 0: source video
            "-loop", "1", "-i", bg_path,     # input 1: background PNG
            "-t", str(duration),
            "-filter_complex",
            (
                f"[0:v]scale=-2:{height}[vid];"
                f"[1:v]scale={width}:{height}[bg];"
                f"[bg][vid]overlay=W-w:0:shortest=1[v]"
            ),
            "-map", "[v]",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "28",
            "-c:a", "aac",
            "-b:a", "96k",
            "-ac", "2",
            "-ar", "44100",
            "-y",
            str(output_path),
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=120,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")[-300:]
            print(f"  Promo clip FFmpeg error: {stderr}")
        return result.returncode == 0 and output_path.exists()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        print(f"  Promo clip generation failed: {e}")
        return False
    finally:
        try:
            Path(bg_path).unlink(missing_ok=True)
        except OSError:
            pass


def generate_music_gap(
    output_path: Path,
    duration: float,
    guide_config: GuideConfig,
) -> bool:
    """
    Generate a branded title card video for gaps between promos.

    Creates a static 640x160 video showing "Preview Channel" branding
    with current time and date, styled to match the Prevue aesthetic.

    Args:
        output_path: Where to write the output
        duration: Duration in seconds
        guide_config: Guide configuration

    Returns:
        True if successful
    """
    width = guide_config.width
    height = guide_config.promo_height

    # Render a Pillow frame for the title card
    img = Image.new("RGB", (width, height), BG_COLOR)
    draw = ImageDraw.Draw(img)

    title_font = _load_bold_font(26)
    time_font = _load_bold_font(18)
    sub_font = _load_font(14)

    # "PREVIEW CHANNEL" centered
    draw.text(
        (width // 2, height // 2 - 28),
        "PREVIEW CHANNEL", fill=(255, 255, 100), font=title_font, anchor="mm"
    )

    # Current time
    now = datetime.now()
    time_str = now.strftime("%I:%M %p").lstrip("0")
    draw.text(
        (width // 2, height // 2 + 5),
        time_str, fill=(255, 255, 255), font=time_font, anchor="mm"
    )

    # Date
    date_str = now.strftime("%A, %B %d").lstrip("0")
    draw.text(
        (width // 2, height // 2 + 30),
        date_str, fill=(180, 180, 180), font=sub_font, anchor="mm"
    )

    # Save frame as temporary PNG, then use FFmpeg to make video
    ffmpeg = get_ffmpeg_path()

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
        img.save(tmp_path)

    cmd = [
        ffmpeg,
        "-loop", "1",
        "-i", tmp_path,
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-t", str(duration),
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "28",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "96k",
        "-shortest",
        "-y",
        str(output_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        return result.returncode == 0 and output_path.exists()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        print(f"  Music gap generation failed: {e}")
        return False
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass


def generate_promo_video(
    promos: list[dict],
    output_path: Path,
    duration: float,
    guide_config: GuideConfig,
    work_dir: Path,
) -> bool:
    """
    Generate the complete promo video for the top half.

    Alternates promo clips with music-gap title cards.
    Pattern: [promo 20s] -> [gap 20s] -> [promo 20s] -> [gap 20s] -> ...

    Args:
        promos: List of promo info dicts from select_promo_content()
        output_path: Where to write the final promo video
        duration: Total duration needed
        guide_config: Guide configuration
        work_dir: Temporary working directory for intermediate files

    Returns:
        True if successful
    """
    ffmpeg = get_ffmpeg_path()
    promo_duration = guide_config.promo_duration
    gap_duration = promo_duration  # Equal time for gaps

    if not promos:
        # No promos available — generate full-duration title card
        return generate_music_gap(output_path, duration, guide_config)

    # Generate individual clips
    clip_files = []
    clip_idx = 0
    accumulated = 0.0
    promo_idx = 0

    while accumulated < duration:
        remaining = duration - accumulated

        if clip_idx % 2 == 0:
            # Promo clip
            if promo_idx < len(promos):
                clip_dur = min(promo_duration, remaining)
                clip_path = work_dir / f"promo_{clip_idx:03d}.mp4"
                success = generate_promo_clip(
                    promos[promo_idx], clip_path, guide_config
                )
                if success:
                    clip_files.append(str(clip_path))
                    accumulated += clip_dur
                    promo_idx += 1
                else:
                    # Failed — use a gap instead
                    clip_dur = min(gap_duration, remaining)
                    clip_path = work_dir / f"gap_{clip_idx:03d}.mp4"
                    if generate_music_gap(clip_path, clip_dur, guide_config):
                        clip_files.append(str(clip_path))
                        accumulated += clip_dur
                    else:
                        break
            else:
                # Out of promos — wrap around
                promo_idx = 0
                clip_dur = min(gap_duration, remaining)
                clip_path = work_dir / f"gap_{clip_idx:03d}.mp4"
                if generate_music_gap(clip_path, clip_dur, guide_config):
                    clip_files.append(str(clip_path))
                    accumulated += clip_dur
                else:
                    break
        else:
            # Music gap
            clip_dur = min(gap_duration, remaining)
            clip_path = work_dir / f"gap_{clip_idx:03d}.mp4"
            if generate_music_gap(clip_path, clip_dur, guide_config):
                clip_files.append(str(clip_path))
                accumulated += clip_dur
            else:
                break

        clip_idx += 1

    if not clip_files:
        return generate_music_gap(output_path, duration, guide_config)

    if len(clip_files) == 1:
        # Just one clip — rename it
        Path(clip_files[0]).rename(output_path)
        return True

    # Concatenate all clips using FFmpeg concat demuxer
    concat_list = work_dir / "concat.txt"
    with open(concat_list, "w") as f:
        for clip in clip_files:
            # Escape single quotes in paths for FFmpeg concat format
            escaped = clip.replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")

    cmd = [
        ffmpeg,
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        "-y",
        str(output_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        return result.returncode == 0 and output_path.exists()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        print(f"  Promo video concatenation failed: {e}")
        return False
