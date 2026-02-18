"""Programmatic weather icons drawn with Pillow."""

import math
from PIL import Image, ImageDraw


def draw_weather_icon(weather_code: int, size: int = 64, night: bool = False) -> Image.Image:
    """
    Draw a weather icon for the given WMO weather code.

    Args:
        weather_code: WMO weather code (0-99)
        size: Icon size in pixels (square)
        night: If True, use moon instead of sun for clear/partly cloudy

    Returns:
        RGBA Image
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    cx, cy = size // 2, size // 2
    r = size // 3  # Base radius

    if weather_code <= 1:
        # Clear sky / Mainly clear
        if night:
            _draw_moon(draw, cx, cy, r, size)
        else:
            _draw_sun(draw, cx, cy, r, size)

    elif weather_code == 2:
        # Partly cloudy
        if night:
            _draw_moon(draw, cx - r // 3, cy - r // 3, int(r * 0.6), size)
        else:
            _draw_sun(draw, cx - r // 3, cy - r // 3, int(r * 0.6), size)
        _draw_cloud(draw, cx + r // 4, cy + r // 4, int(r * 0.8), color=(200, 200, 210, 230))

    elif weather_code == 3:
        # Overcast
        _draw_cloud(draw, cx - r // 4, cy - r // 6, int(r * 0.7), color=(160, 160, 170, 220))
        _draw_cloud(draw, cx + r // 6, cy + r // 6, int(r * 0.9), color=(200, 200, 210, 240))

    elif weather_code in (45, 48):
        # Fog
        _draw_fog(draw, cx, cy, r, size)

    elif weather_code in (51, 53, 55, 56, 57):
        # Drizzle (light/moderate/heavy)
        _draw_cloud(draw, cx, cy - r // 4, r, color=(180, 180, 190, 230))
        intensity = 3 if weather_code <= 53 else 5
        _draw_rain(draw, cx, cy + r // 3, r, size, drops=intensity, light=True)

    elif weather_code in (61, 63, 65, 66, 67, 80, 81, 82):
        # Rain (light/moderate/heavy) and showers
        _draw_cloud(draw, cx, cy - r // 4, r, color=(150, 150, 165, 240))
        intensity = 4 if weather_code in (61, 80) else 6 if weather_code in (63, 81) else 8
        _draw_rain(draw, cx, cy + r // 3, r, size, drops=intensity)

    elif weather_code in (71, 73, 75, 77, 85, 86):
        # Snow
        _draw_cloud(draw, cx, cy - r // 4, r, color=(180, 180, 195, 230))
        intensity = 4 if weather_code in (71, 85) else 7
        _draw_snow(draw, cx, cy + r // 3, r, size, flakes=intensity)

    elif weather_code in (95, 96, 99):
        # Thunderstorm
        _draw_cloud(draw, cx, cy - r // 3, r, color=(100, 100, 120, 240))
        _draw_lightning(draw, cx, cy + r // 6, r, size)
        _draw_rain(draw, cx, cy + r // 2, r, size, drops=4)

    else:
        # Unknown - draw a question mark cloud
        _draw_cloud(draw, cx, cy, r, color=(180, 180, 190, 220))

    return img


def _draw_sun(draw: ImageDraw.Draw, cx: int, cy: int, r: int, size: int) -> None:
    """Draw a yellow sun with radiating lines."""
    color = (255, 220, 50, 255)
    ray_color = (255, 200, 50, 200)

    # Sun disk
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)

    # Rays
    ray_len = r * 0.5
    for angle in range(0, 360, 45):
        rad = math.radians(angle)
        x1 = cx + int((r + 3) * math.cos(rad))
        y1 = cy + int((r + 3) * math.sin(rad))
        x2 = cx + int((r + 3 + ray_len) * math.cos(rad))
        y2 = cy + int((r + 3 + ray_len) * math.sin(rad))
        draw.line([(x1, y1), (x2, y2)], fill=ray_color, width=max(2, size // 32))


def _draw_moon(draw: ImageDraw.Draw, cx: int, cy: int, r: int, size: int) -> None:
    """Draw a white crescent moon."""
    color = (230, 230, 240, 255)

    # Full moon circle
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)

    # Cut out a circle to make crescent
    cut_offset = int(r * 0.6)
    cut_r = int(r * 0.85)
    draw.ellipse(
        [cx + cut_offset - cut_r, cy - cut_r,
         cx + cut_offset + cut_r, cy + cut_r],
        fill=(0, 0, 0, 0)
    )


def _draw_cloud(draw: ImageDraw.Draw, cx: int, cy: int, r: int,
                color: tuple = (200, 200, 210, 230)) -> None:
    """Draw a cloud from overlapping ellipses."""
    # Main body
    draw.ellipse([cx - r, cy - r // 2, cx + r, cy + r // 2], fill=color)
    # Top bump
    tr = int(r * 0.65)
    draw.ellipse([cx - tr, cy - r, cx + tr, cy - r // 6], fill=color)
    # Left bump
    lr = int(r * 0.5)
    draw.ellipse([cx - r, cy - int(r * 0.7), cx - r // 4, cy + r // 6], fill=color)
    # Right bump
    draw.ellipse([cx + r // 4, cy - int(r * 0.6), cx + r, cy + r // 6], fill=color)


def _draw_rain(draw: ImageDraw.Draw, cx: int, cy: int, r: int, size: int,
               drops: int = 5, light: bool = False) -> None:
    """Draw diagonal rain lines."""
    color = (80, 160, 255, 200) if not light else (100, 180, 255, 160)
    width = max(1, size // 40)
    drop_len = size // 6 if not light else size // 8

    spacing = (r * 2) / max(1, drops + 1)
    start_x = cx - r + int(spacing)

    for i in range(drops):
        x = start_x + int(i * spacing)
        y = cy + (i % 3) * (size // 12)
        draw.line(
            [(x, y), (x - drop_len // 3, y + drop_len)],
            fill=color, width=width
        )


def _draw_snow(draw: ImageDraw.Draw, cx: int, cy: int, r: int, size: int,
               flakes: int = 5) -> None:
    """Draw white snowflake dots."""
    color = (240, 240, 255, 230)
    flake_r = max(2, size // 24)

    spacing = (r * 2) / max(1, flakes + 1)
    start_x = cx - r + int(spacing)

    for i in range(flakes):
        x = start_x + int(i * spacing)
        y = cy + ((i * 7) % 3) * (size // 10)
        draw.ellipse([x - flake_r, y - flake_r, x + flake_r, y + flake_r], fill=color)


def _draw_lightning(draw: ImageDraw.Draw, cx: int, cy: int, r: int, size: int) -> None:
    """Draw a yellow lightning bolt."""
    color = (255, 255, 50, 255)
    w = max(2, size // 20)

    # Zigzag bolt
    points = [
        (cx, cy - r // 2),
        (cx - r // 4, cy),
        (cx + r // 8, cy),
        (cx - r // 6, cy + r // 2),
    ]
    draw.line(points, fill=color, width=w, joint="curve")


def _draw_fog(draw: ImageDraw.Draw, cx: int, cy: int, r: int, size: int) -> None:
    """Draw horizontal fog lines."""
    color = (180, 180, 190, 180)
    width = max(2, size // 20)

    for i in range(4):
        y = cy - r // 2 + i * (r // 2)
        x_offset = (i % 2) * (r // 4)
        draw.line(
            [(cx - r + x_offset, y), (cx + r - x_offset, y)],
            fill=color, width=width
        )
