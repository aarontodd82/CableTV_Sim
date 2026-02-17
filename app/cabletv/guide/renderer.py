"""Prevue-style grid frame renderer using Pillow."""

import sys
from datetime import datetime, timedelta
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from ..config import GuideConfig
from ..schedule.engine import ScheduleEntry

# Authentic Prevue color palette
BG_COLOR = (10, 10, 46)        # Dark blue background
HEADER_BG = (10, 10, 46)       # Time header background
HEADER_TEXT = (255, 255, 255)   # White time header text
CHANNEL_BG = (10, 10, 46)      # Channel column background
CHANNEL_NUM_COLOR = (255, 255, 100)  # Yellow channel numbers
CHANNEL_NAME_COLOR = (255, 255, 255) # White channel names
CELL_COLORS = [
    (0, 80, 120),   # Teal
    (0, 60, 100),   # Darker teal
]
CELL_TEXT_COLOR = (255, 255, 255)    # White program text
CELL_TIME_COLOR = (200, 200, 200)    # Light gray time text
GRID_LINE_COLOR = (30, 30, 80)       # Subtle grid lines
NOW_MARKER_COLOR = (255, 255, 100)   # Yellow "now" line

# Layout constants — sized for 3 visible rows on CRT
CHANNEL_COL_WIDTH = 100  # Pixels for channel number + name column
TIME_HEADER_HEIGHT = 26  # Pixels for time header row
ROW_HEIGHT = 71          # Pixels per channel row (~3 visible in 240px grid)


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """Load a font with fallback chain."""
    font_candidates = []

    if sys.platform == "win32":
        font_candidates.extend([
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/segoeui.ttf",
            "C:/Windows/Fonts/tahoma.ttf",
        ])
    else:
        font_candidates.extend([
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
        ])

    for path in font_candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue

    # Fallback to Pillow default
    return ImageFont.load_default()


def _load_bold_font(size: int) -> ImageFont.FreeTypeFont:
    """Load a bold font with fallback chain."""
    font_candidates = []

    if sys.platform == "win32":
        font_candidates.extend([
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/seguisb.ttf",
            "C:/Windows/Fonts/tahomabd.ttf",
        ])
    else:
        font_candidates.extend([
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        ])

    for path in font_candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue

    return _load_font(size)


class GuideGridRenderer:
    """Renders the scrolling channel grid for the TV guide."""

    def __init__(self, guide_config: GuideConfig):
        self.config = guide_config
        self.grid_width = guide_config.width  # 640
        self.grid_height = guide_config.grid_height  # 320
        self.program_area_width = self.grid_width - CHANNEL_COL_WIDTH  # 540

        # Fonts — sized for CRT readability
        self._font_small = _load_font(13)
        self._font_medium = _load_font(16)
        self._font_channel_num = _load_bold_font(22)
        self._font_channel_name = _load_font(14)
        self._font_time = _load_font(13)
        self._font_title = _load_bold_font(16)
        self._font_header = _load_bold_font(14)

    def render_full_strip(
        self,
        guide_data: dict[int, list[ScheduleEntry]],
        start_time: datetime,
        hours: float = 1.5,
        channel_configs: Optional[dict] = None,
        guide_channel: int = 2,
    ) -> Image.Image:
        """
        Render a tall vertical strip containing all channel rows + time header.

        This strip gets scrolled vertically to create the Prevue effect.

        Args:
            guide_data: Dict mapping channel_number -> list of ScheduleEntry
            start_time: Left edge of the time window
            hours: Width of the time window in hours
            channel_configs: Dict mapping channel_number -> ChannelConfig
            guide_channel: Guide channel number to exclude from grid

        Returns:
            Tall Pillow Image with all rows
        """
        # Filter out the guide channel itself
        channel_numbers = sorted(
            ch for ch in guide_data.keys() if ch != guide_channel
        )

        if not channel_numbers:
            # No channels — return minimal strip
            img = Image.new("RGB", (self.grid_width, ROW_HEIGHT + TIME_HEADER_HEIGHT), BG_COLOR)
            draw = ImageDraw.Draw(img)
            draw.text(
                (self.grid_width // 2, (ROW_HEIGHT + TIME_HEADER_HEIGHT) // 2),
                "No Programming",
                fill=CELL_TEXT_COLOR, font=self._font_medium, anchor="mm"
            )
            return img

        num_rows = len(channel_numbers)
        strip_height = TIME_HEADER_HEIGHT + (num_rows * ROW_HEIGHT)

        img = Image.new("RGB", (self.grid_width, strip_height), BG_COLOR)
        draw = ImageDraw.Draw(img)

        end_time = start_time + timedelta(hours=hours)
        total_seconds = hours * 3600

        # Draw time header
        self._draw_time_header(draw, start_time, hours, 0)

        # Draw each channel row
        for i, ch_num in enumerate(channel_numbers):
            y_offset = TIME_HEADER_HEIGHT + (i * ROW_HEIGHT)
            entries = guide_data.get(ch_num, [])
            ch_name = ""
            if channel_configs and ch_num in channel_configs:
                ch_name = channel_configs[ch_num].name

            self._draw_channel_row(
                draw, ch_num, ch_name, entries,
                start_time, end_time, total_seconds,
                y_offset
            )

        return img

    def _draw_time_header(
        self, draw: ImageDraw.Draw,
        start_time: datetime, hours: float, y: int
    ) -> None:
        """Draw the time header bar at the top.

        Shows current time in the channel column area (left),
        and 30-minute time markers across the program area (right).
        """
        # Background for full header
        draw.rectangle(
            [0, y, self.grid_width, y + TIME_HEADER_HEIGHT],
            fill=HEADER_BG
        )

        # Current time in the channel column (left side, like original Prevue)
        now_str = start_time.strftime("%I:%M %p").lstrip("0")
        draw.text(
            (8, y + 5),
            now_str, fill=CHANNEL_NUM_COLOR, font=self._font_header
        )

        # Separator line between channel column and program area
        draw.line(
            [(CHANNEL_COL_WIDTH - 1, y), (CHANNEL_COL_WIDTH - 1, y + TIME_HEADER_HEIGHT)],
            fill=GRID_LINE_COLOR
        )

        # Bottom border of header
        draw.line(
            [(0, y + TIME_HEADER_HEIGHT - 1), (self.grid_width, y + TIME_HEADER_HEIGHT - 1)],
            fill=GRID_LINE_COLOR
        )

        # Draw time markers every 30 minutes
        total_seconds = hours * 3600
        current = start_time.replace(minute=(start_time.minute // 30) * 30, second=0, microsecond=0)
        if current < start_time:
            current += timedelta(minutes=30)

        while current < start_time + timedelta(hours=hours):
            offset_seconds = (current - start_time).total_seconds()
            x = CHANNEL_COL_WIDTH + int((offset_seconds / total_seconds) * self.program_area_width)

            # Don't draw markers that would overflow the right edge
            if x > self.grid_width - 50:
                current += timedelta(minutes=30)
                continue

            time_str = current.strftime("%I:%M").lstrip("0")
            # Add AM/PM only on the hour
            if current.minute == 0:
                time_str += current.strftime(" %p")

            draw.text(
                (x + 3, y + 5),
                time_str, fill=HEADER_TEXT, font=self._font_header
            )

            # Tick mark
            draw.line([(x, y + TIME_HEADER_HEIGHT - 3), (x, y + TIME_HEADER_HEIGHT - 1)], fill=GRID_LINE_COLOR)

            current += timedelta(minutes=30)

    def _draw_channel_row(
        self, draw: ImageDraw.Draw,
        channel_num: int, channel_name: str,
        entries: list[ScheduleEntry],
        start_time: datetime, end_time: datetime,
        total_seconds: float, y: int
    ) -> None:
        """Draw a single channel row with program cells."""
        row_bottom = y + ROW_HEIGHT

        # Channel column background
        draw.rectangle(
            [0, y, CHANNEL_COL_WIDTH - 1, row_bottom - 1],
            fill=CHANNEL_BG
        )

        # Channel number (large, yellow)
        draw.text(
            (10, y + 10),
            str(channel_num), fill=CHANNEL_NUM_COLOR, font=self._font_channel_num
        )

        # Channel name (truncate if needed)
        name = channel_name[:10] if len(channel_name) > 10 else channel_name
        draw.text(
            (10, y + 38),
            name, fill=CHANNEL_NAME_COLOR, font=self._font_channel_name
        )

        # Separator line below row
        draw.line([(0, row_bottom - 1), (self.grid_width, row_bottom - 1)], fill=GRID_LINE_COLOR)
        # Vertical separator after channel column
        draw.line(
            [(CHANNEL_COL_WIDTH - 1, y), (CHANNEL_COL_WIDTH - 1, row_bottom - 1)],
            fill=GRID_LINE_COLOR
        )

        if not entries:
            # No programming
            draw.rectangle(
                [CHANNEL_COL_WIDTH, y, self.grid_width - 1, row_bottom - 2],
                fill=CELL_COLORS[0]
            )
            draw.text(
                (CHANNEL_COL_WIDTH + 8, y + 35),
                "No Programming", fill=CELL_TEXT_COLOR, font=self._font_medium
            )
            return

        # Draw program cells
        for idx, entry in enumerate(entries):
            cell_color = CELL_COLORS[idx % 2]

            # Calculate cell position within the program area
            entry_start = max(entry.start_time, start_time)
            entry_end = min(entry.slot_end_time, end_time)

            if entry_end <= start_time or entry_start >= end_time:
                continue

            start_offset = max(0, (entry_start - start_time).total_seconds())
            end_offset = min(total_seconds, (entry_end - start_time).total_seconds())

            x_start = CHANNEL_COL_WIDTH + int((start_offset / total_seconds) * self.program_area_width)
            x_end = CHANNEL_COL_WIDTH + int((end_offset / total_seconds) * self.program_area_width)

            cell_width = x_end - x_start
            if cell_width < 2:
                continue

            # Cell background
            draw.rectangle(
                [x_start, y + 1, x_end - 1, row_bottom - 2],
                fill=cell_color
            )

            # Cell border on left
            draw.line(
                [(x_start, y + 1), (x_start, row_bottom - 2)],
                fill=GRID_LINE_COLOR
            )

            # Program info (clip to cell width)
            text_x = x_start + 6
            available_width = cell_width - 12

            if available_width > 30:
                # Show time (top of cell)
                time_str = entry.start_time.strftime("%I:%M").lstrip("0")
                draw.text(
                    (text_x, y + 8),
                    time_str, fill=CELL_TIME_COLOR, font=self._font_time
                )

                # Show title (middle of cell, larger font)
                title = entry.title
                # Rough character limit based on available width
                max_chars = max(1, available_width // 9)
                if len(title) > max_chars:
                    title = title[:max_chars - 1] + "\u2026"

                draw.text(
                    (text_x, y + 28),
                    title, fill=CELL_TEXT_COLOR, font=self._font_title
                )

    def get_frame_at_offset(
        self, strip: Image.Image, scroll_offset: float,
        current_time: Optional[datetime] = None,
        clock_text: Optional[str] = None,
    ) -> Image.Image:
        """
        Get a viewport frame from the strip at the given scroll offset.

        The viewport shows the time header pinned at top, with channel rows
        scrolling below it. Wraps around seamlessly.

        Args:
            strip: The full tall strip image
            scroll_offset: Pixel offset into the channel rows (wraps)
            current_time: If provided, updates the clock in the header
            clock_text: If provided, overrides clock with this literal string

        Returns:
            640 x grid_height frame
        """
        viewport = Image.new("RGB", (self.grid_width, self.grid_height), BG_COLOR)

        # Pin the time header at the top
        header = strip.crop((0, 0, self.grid_width, TIME_HEADER_HEIGHT))
        viewport.paste(header, (0, 0))

        # Update the clock display in the header
        if clock_text is not None or current_time is not None:
            draw = ImageDraw.Draw(viewport)
            # Clear the clock area (channel column portion of header)
            draw.rectangle([0, 0, CHANNEL_COL_WIDTH - 2, TIME_HEADER_HEIGHT - 2], fill=HEADER_BG)
            if clock_text is not None:
                text = clock_text
            else:
                text = current_time.strftime("%I:%M %p").lstrip("0")
            draw.text((8, 5), text, fill=CHANNEL_NUM_COLOR, font=self._font_header)

        # Scrollable area below header
        scroll_area_height = self.grid_height - TIME_HEADER_HEIGHT
        rows_area_height = strip.height - TIME_HEADER_HEIGHT

        if rows_area_height <= 0:
            return viewport

        # Wrap the scroll offset
        offset = int(scroll_offset) % rows_area_height
        source_y = TIME_HEADER_HEIGHT + offset

        # How many pixels we can grab before wrapping
        remaining_before_wrap = strip.height - source_y

        if remaining_before_wrap >= scroll_area_height:
            # No wrap needed
            region = strip.crop((
                0, source_y,
                self.grid_width, source_y + scroll_area_height
            ))
            viewport.paste(region, (0, TIME_HEADER_HEIGHT))
        else:
            # Need to wrap: paste end portion, then beginning
            region1 = strip.crop((
                0, source_y,
                self.grid_width, strip.height
            ))
            viewport.paste(region1, (0, TIME_HEADER_HEIGHT))

            remaining = scroll_area_height - remaining_before_wrap
            region2 = strip.crop((
                0, TIME_HEADER_HEIGHT,
                self.grid_width, TIME_HEADER_HEIGHT + remaining
            ))
            viewport.paste(region2, (0, TIME_HEADER_HEIGHT + remaining_before_wrap))

        return viewport
