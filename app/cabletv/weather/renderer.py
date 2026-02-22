"""90s-style Weather Channel page renderer using Pillow."""

import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from .api import (
    WeatherData, RegionalCity, get_weather_description, get_wind_direction_str,
)
from .icons import draw_weather_icon
from .moon import get_moon_phase
from ..platform import get_drive_root


def _load_vcr_font(size: int) -> ImageFont.FreeTypeFont:
    """Load VCR OSD Mono font from project fonts dir, with fallback."""
    vcr_path = get_drive_root() / "fonts" / "VCR_OSD_MONO.ttf"
    if vcr_path.exists():
        try:
            return ImageFont.truetype(str(vcr_path), size)
        except (OSError, IOError):
            pass

    # Fallback to system fonts
    from ..guide.renderer import _load_font
    return _load_font(size)

# --- Color Palette (authentic 90s TWC) ---
BG_TOP = (0, 20, 80)        # Navy blue (top of gradient)
BG_BOTTOM = (0, 10, 50)     # Darker navy (bottom of gradient)
HEADER_YELLOW = (255, 220, 50)
TEXT_WHITE = (255, 255, 255)
TEXT_CYAN = (0, 220, 255)
TEXT_GRAY = (180, 180, 180)
ACCENT_BAR = (0, 180, 220)
TICKER_BG = (0, 0, 0)
TICKER_TEXT = (255, 255, 255)
BRAND_BG = (0, 40, 120)
BRAND_TEXT = (255, 255, 255)
ROW_ALT_1 = (0, 25, 90)
ROW_ALT_2 = (0, 18, 65)

# Layout
BRAND_BAR_HEIGHT = 32
TICKER_HEIGHT = 36
PAGE_HEIGHT = 412  # 480 - 32 - 36

# Number of pages in the cycle
NUM_PAGES = 6


def _temp_color(temp: float) -> tuple:
    """Get color-coded temperature color."""
    if temp >= 90:
        return (255, 60, 60)     # Hot red
    elif temp >= 75:
        return (255, 140, 40)    # Warm orange
    elif temp >= 55:
        return (255, 255, 255)   # Mild white
    elif temp >= 35:
        return (100, 180, 255)   # Cool blue
    else:
        return (60, 130, 255)    # Cold blue


def _draw_gradient_bg(img: Image.Image, y_start: int, y_end: int) -> None:
    """Draw a vertical gradient background on the image."""
    draw = ImageDraw.Draw(img)
    height = y_end - y_start
    for y in range(height):
        t = y / max(1, height - 1)
        r = int(BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t)
        g = int(BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t)
        b = int(BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t)
        draw.line([(0, y_start + y), (img.width, y_start + y)], fill=(r, g, b))


class WeatherRenderer:
    """Renders 90s-style Weather Channel pages."""

    def __init__(self, width: int = 640, height: int = 480):
        self.width = width
        self.height = height

        # Fonts — VCR OSD Mono for authentic retro look
        self._font_temp_large = _load_vcr_font(56)
        self._font_header = _load_vcr_font(22)
        self._font_subheader = _load_vcr_font(18)
        self._font_data = _load_vcr_font(17)
        self._font_data_bold = _load_vcr_font(17)
        self._font_small = _load_vcr_font(15)
        self._font_small_bold = _load_vcr_font(15)
        self._font_brand = _load_vcr_font(16)
        self._font_ticker = _load_vcr_font(22)
        self._font_day = _load_vcr_font(17)
        self._font_hourly = _load_vcr_font(16)
        self._font_hourly_bold = _load_vcr_font(16)

    def render_frame(
        self,
        weather: WeatherData,
        page_index: int,
        ticker_offset: float,
        radar_image: Optional[Image.Image] = None,
        regional_temps: Optional[list[RegionalCity]] = None,
    ) -> Image.Image:
        """
        Render a complete 640x480 frame.

        Args:
            weather: Weather data to display
            page_index: Which page to show (0-5)
            ticker_offset: Horizontal pixel offset for scrolling ticker
            radar_image: Optional radar composite image
            regional_temps: Optional regional city temperatures

        Returns:
            640x480 RGB Image
        """
        img = Image.new("RGB", (self.width, self.height), (0, 0, 0))

        # Draw gradient background for page area
        _draw_gradient_bg(img, BRAND_BAR_HEIGHT, self.height - TICKER_HEIGHT)

        # Brand bar (top)
        self._draw_brand_bar(img, weather)

        # Page content (middle)
        page_index = page_index % NUM_PAGES
        if page_index == 0:
            self._draw_current_conditions(img, weather)
        elif page_index == 1:
            self._draw_todays_forecast(img, weather)
        elif page_index == 2:
            self._draw_extended_forecast(img, weather)
        elif page_index == 3:
            self._draw_almanac(img, weather)
        elif page_index == 4:
            self._draw_hourly_forecast(img, weather)
        elif page_index == 5:
            if radar_image:
                self._draw_regional_radar(img, radar_image)
            else:
                self._draw_regional_temps(img, weather, regional_temps)

        # Scrolling ticker (bottom)
        self._draw_ticker(img, weather, ticker_offset)

        return img

    # Width reserved on the right side of the brand bar for the live OSD clock
    CLOCK_GAP_WIDTH = 110

    def _draw_brand_bar(self, img: Image.Image, weather: WeatherData) -> None:
        """Draw the top brand bar with logo and location.

        Layout: [TWC LOGO] ... [LOCATION] ... [____CLOCK GAP____]

        The rightmost CLOCK_GAP_WIDTH pixels are left as the brand-bar
        background color so the playback engine can overlay a live OSD
        clock that stays accurate across the looping segment.
        """
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, self.width, BRAND_BAR_HEIGHT], fill=BRAND_BG)

        # "THE WEATHER CHANNEL" on the left
        draw.text((10, 7), "THE WEATHER CHANNEL", fill=HEADER_YELLOW,
                  font=self._font_brand)

        # Location centered between logo and clock gap
        location = getattr(self, '_location_name', 'LILLINGTON, NC').upper()
        bbox = draw.textbbox((0, 0), location, font=self._font_brand)
        text_w = bbox[2] - bbox[0]
        # Position: right-aligned but before the clock gap
        loc_x = self.width - self.CLOCK_GAP_WIDTH - text_w - 10
        draw.text((loc_x, 7), location, fill=TEXT_WHITE, font=self._font_brand)

        # Thin separator before clock area
        sep_x = self.width - self.CLOCK_GAP_WIDTH
        draw.line([(sep_x, 5), (sep_x, BRAND_BAR_HEIGHT - 5)], fill=ACCENT_BAR)

        # Accent line at bottom of brand bar
        draw.line([(0, BRAND_BAR_HEIGHT - 1), (self.width, BRAND_BAR_HEIGHT - 1)],
                  fill=ACCENT_BAR)

    def _draw_current_conditions(self, img: Image.Image, weather: WeatherData) -> None:
        """Page 1: Current Conditions."""
        draw = ImageDraw.Draw(img)
        c = weather.current
        y_base = BRAND_BAR_HEIGHT + 8

        # Header
        draw.text((20, y_base), "CURRENT CONDITIONS", fill=HEADER_YELLOW,
                  font=self._font_header)

        # Accent bar under header
        draw.line([(20, y_base + 28), (self.width - 20, y_base + 28)], fill=ACCENT_BAR)

        # Large temperature (left side)
        temp_str = f"{c.temperature:.0f}\u00b0F"
        temp_color = _temp_color(c.temperature)
        draw.text((40, y_base + 40), temp_str, fill=temp_color,
                  font=self._font_temp_large)

        # Weather icon (right of temp)
        is_night = self._is_night(weather)
        icon = draw_weather_icon(c.weather_code, size=64, night=is_night)
        img.paste(icon, (200, y_base + 40), icon)

        # Conditions text
        desc = get_weather_description(c.weather_code).upper()
        draw.text((275, y_base + 55), desc, fill=TEXT_WHITE, font=self._font_data_bold)

        # Data grid (two columns)
        grid_y = y_base + 130
        left_x = 40
        right_x = 340
        row_h = 36

        # Left column
        wind_dir = get_wind_direction_str(c.wind_direction)
        data_left = [
            ("HUMIDITY", f"{c.humidity}%"),
            ("WIND", f"{wind_dir} {c.wind_speed:.0f} MPH"),
            ("BAROMETER", f"{c.pressure:.2f} INHG"),
        ]

        # Right column
        data_right = [
            ("DEWPOINT", f"{c.dewpoint:.0f}\u00b0F"),
            ("VISIBILITY", f"{c.visibility:.1f} MI"),
            ("FEELS LIKE", f"{c.feels_like:.0f}\u00b0F"),
        ]

        for i, (label, value) in enumerate(data_left):
            y = grid_y + i * row_h
            draw.text((left_x, y), label, fill=TEXT_CYAN, font=self._font_data)
            draw.text((left_x + 140, y), value, fill=TEXT_WHITE, font=self._font_data_bold)

        for i, (label, value) in enumerate(data_right):
            y = grid_y + i * row_h
            draw.text((right_x, y), label, fill=TEXT_CYAN, font=self._font_data)
            draw.text((right_x + 140, y), value, fill=TEXT_WHITE, font=self._font_data_bold)

    def _draw_todays_forecast(self, img: Image.Image, weather: WeatherData) -> None:
        """Page 2: Today's Forecast — three sections: today, tonight, tomorrow."""
        draw = ImageDraw.Draw(img)
        y_base = BRAND_BAR_HEIGHT + 8

        draw.text((20, y_base), "TODAY'S FORECAST", fill=HEADER_YELLOW,
                  font=self._font_header)
        draw.line([(20, y_base + 28), (self.width - 20, y_base + 28)], fill=ACCENT_BAR)

        if not weather.daily:
            draw.text((20, y_base + 50), "NO FORECAST DATA AVAILABLE",
                      fill=TEXT_WHITE, font=self._font_data)
            return

        today = weather.daily[0]
        c = weather.current

        # --- TODAY section ---
        section_y = y_base + 38
        draw.text((30, section_y), "TODAY", fill=HEADER_YELLOW, font=self._font_subheader)

        icon = draw_weather_icon(today.weather_code, size=48, night=False)
        img.paste(icon, (30, section_y + 22), icon)

        desc = get_weather_description(today.weather_code).upper()
        draw.text((90, section_y + 24), desc, fill=TEXT_WHITE, font=self._font_data_bold)

        high_color = _temp_color(today.high)
        draw.text((90, section_y + 44), "HIGH: ", fill=TEXT_CYAN, font=self._font_data)
        draw.text((155, section_y + 44), f"{today.high:.0f}\u00b0F",
                  fill=high_color, font=self._font_data_bold)

        # Wind
        wind_dir = get_wind_direction_str(c.wind_direction)
        draw.text((250, section_y + 24), "WIND:", fill=TEXT_CYAN, font=self._font_data)
        draw.text((310, section_y + 24), f"{wind_dir} {c.wind_speed:.0f} MPH",
                  fill=TEXT_WHITE, font=self._font_data_bold)

        # Precip from hourly
        if weather.hourly:
            max_precip = max(h.precipitation_probability for h in weather.hourly[:12])
            draw.text((250, section_y + 44), "PRECIP:", fill=TEXT_CYAN, font=self._font_data)
            draw.text((325, section_y + 44), f"{max_precip}%",
                      fill=TEXT_WHITE, font=self._font_data_bold)

        # Humidity
        draw.text((430, section_y + 24), "HUM:", fill=TEXT_CYAN, font=self._font_data)
        draw.text((480, section_y + 24), f"{c.humidity}%",
                  fill=TEXT_WHITE, font=self._font_data_bold)

        # --- Divider ---
        div_y = section_y + 82
        draw.line([(30, div_y), (self.width - 30, div_y)], fill=ACCENT_BAR)

        # --- TONIGHT section ---
        tonight_y = div_y + 8
        draw.text((30, tonight_y), "TONIGHT", fill=HEADER_YELLOW, font=self._font_subheader)

        night_code = today.weather_code
        icon_night = draw_weather_icon(night_code, size=48, night=True)
        img.paste(icon_night, (30, tonight_y + 22), icon_night)

        draw.text((90, tonight_y + 24), get_weather_description(night_code).upper(),
                  fill=TEXT_WHITE, font=self._font_data_bold)

        low_color = _temp_color(today.low)
        draw.text((90, tonight_y + 44), "LOW: ", fill=TEXT_CYAN, font=self._font_data)
        draw.text((150, tonight_y + 44), f"{today.low:.0f}\u00b0F",
                  fill=low_color, font=self._font_data_bold)

        # Tonight wind estimate (calm at night)
        draw.text((250, tonight_y + 24), "WIND:", fill=TEXT_CYAN, font=self._font_data)
        draw.text((310, tonight_y + 24), f"{wind_dir} {max(0, c.wind_speed - 3):.0f} MPH",
                  fill=TEXT_WHITE, font=self._font_data_bold)

        # Evening precip
        if weather.hourly:
            eve_hours = [h for h in weather.hourly[:24] if h.time.hour >= 18]
            if eve_hours:
                eve_precip = max(h.precipitation_probability for h in eve_hours)
            else:
                eve_precip = 0
            draw.text((250, tonight_y + 44), "PRECIP:", fill=TEXT_CYAN, font=self._font_data)
            draw.text((325, tonight_y + 44), f"{eve_precip}%",
                      fill=TEXT_WHITE, font=self._font_data_bold)

        # --- Divider ---
        div2_y = tonight_y + 82
        draw.line([(30, div2_y), (self.width - 30, div2_y)], fill=ACCENT_BAR)

        # --- TOMORROW section ---
        if len(weather.daily) > 1:
            tmrw = weather.daily[1]
            tmrw_y = div2_y + 8
            draw.text((30, tmrw_y), "TOMORROW", fill=HEADER_YELLOW, font=self._font_subheader)

            tmrw_icon = draw_weather_icon(tmrw.weather_code, size=48, night=False)
            img.paste(tmrw_icon, (30, tmrw_y + 22), tmrw_icon)

            draw.text((90, tmrw_y + 24),
                      get_weather_description(tmrw.weather_code).upper(),
                      fill=TEXT_WHITE, font=self._font_data_bold)

            hi_color = _temp_color(tmrw.high)
            lo_color = _temp_color(tmrw.low)
            draw.text((90, tmrw_y + 44), "HIGH: ", fill=TEXT_CYAN, font=self._font_data)
            draw.text((155, tmrw_y + 44), f"{tmrw.high:.0f}\u00b0F",
                      fill=hi_color, font=self._font_data_bold)
            draw.text((250, tmrw_y + 44), "LOW: ", fill=TEXT_CYAN, font=self._font_data)
            draw.text((305, tmrw_y + 44), f"{tmrw.low:.0f}\u00b0F",
                      fill=lo_color, font=self._font_data_bold)

    def _draw_extended_forecast(self, img: Image.Image, weather: WeatherData) -> None:
        """Page 3: 5-Day Extended Forecast — vertically centered, full page."""
        draw = ImageDraw.Draw(img)
        y_base = BRAND_BAR_HEIGHT + 8

        draw.text((20, y_base), "EXTENDED FORECAST", fill=HEADER_YELLOW,
                  font=self._font_header)
        draw.line([(20, y_base + 28), (self.width - 20, y_base + 28)], fill=ACCENT_BAR)

        days = weather.daily[:5]
        if not days:
            draw.text((20, y_base + 50), "NO FORECAST DATA AVAILABLE",
                      fill=TEXT_WHITE, font=self._font_data)
            return

        # Vertically center the content in the available page area
        # Available: from y_base+32 to BRAND_BAR_HEIGHT+PAGE_HEIGHT
        avail_top = y_base + 32
        avail_bottom = BRAND_BAR_HEIGHT + PAGE_HEIGHT - 10
        avail_h = avail_bottom - avail_top
        content_h = 280  # approx height of all elements
        row_y = avail_top + (avail_h - content_h) // 2

        num_days = len(days)
        col_width = (self.width - 40) // max(1, num_days)

        for i, day in enumerate(days):
            x = 20 + i * col_width
            col_center = x + col_width // 2

            # Day name — 3-letter abbreviation (except today/tmrw)
            if i == 0:
                day_name = "TODAY"
            elif i == 1:
                day_name = "TMRW"
            else:
                day_name = day.date.strftime("%a").upper()

            bbox = draw.textbbox((0, 0), day_name, font=self._font_day)
            tw = bbox[2] - bbox[0]
            draw.text((col_center - tw // 2, row_y), day_name,
                      fill=HEADER_YELLOW, font=self._font_day)

            # Weather icon (larger)
            icon = draw_weather_icon(day.weather_code, size=48)
            img.paste(icon, (col_center - 24, row_y + 28), icon)

            # Description
            desc = get_weather_description(day.weather_code).upper()
            if len(desc) > 12:
                desc = desc[:11] + "."
            bbox = draw.textbbox((0, 0), desc, font=self._font_small)
            tw = bbox[2] - bbox[0]
            draw.text((col_center - tw // 2, row_y + 84), desc,
                      fill=TEXT_WHITE, font=self._font_small)

            # "HI" label + high temp
            hi_str = f"{day.high:.0f}\u00b0"
            hi_color = _temp_color(day.high)
            hi_label = "HI"
            bbox_l = draw.textbbox((0, 0), hi_label, font=self._font_small)
            bbox_v = draw.textbbox((0, 0), hi_str, font=self._font_data_bold)
            total_w = (bbox_l[2] - bbox_l[0]) + 4 + (bbox_v[2] - bbox_v[0])
            start_x = col_center - total_w // 2
            draw.text((start_x, row_y + 110), hi_label,
                      fill=TEXT_CYAN, font=self._font_small)
            draw.text((start_x + (bbox_l[2] - bbox_l[0]) + 4, row_y + 110),
                      hi_str, fill=hi_color, font=self._font_data_bold)

            # "LO" label + low temp
            lo_str = f"{day.low:.0f}\u00b0"
            lo_color = _temp_color(day.low)
            lo_label = "LO"
            bbox_l = draw.textbbox((0, 0), lo_label, font=self._font_small)
            bbox_v = draw.textbbox((0, 0), lo_str, font=self._font_data_bold)
            total_w = (bbox_l[2] - bbox_l[0]) + 4 + (bbox_v[2] - bbox_v[0])
            start_x = col_center - total_w // 2
            draw.text((start_x, row_y + 136), lo_label,
                      fill=TEXT_CYAN, font=self._font_small)
            draw.text((start_x + (bbox_l[2] - bbox_l[0]) + 4, row_y + 136),
                      lo_str, fill=lo_color, font=self._font_data_bold)

            # Precipitation probability (from hourly data, averaged for day)
            precip_pct = self._get_day_precip(weather, day)
            if precip_pct is not None:
                p_str = f"RAIN {precip_pct}%"
                bbox = draw.textbbox((0, 0), p_str, font=self._font_small)
                tw = bbox[2] - bbox[0]
                p_color = TEXT_CYAN if precip_pct < 40 else (100, 180, 255)
                draw.text((col_center - tw // 2, row_y + 164), p_str,
                          fill=p_color, font=self._font_small)

            # Separator between columns
            if i < num_days - 1:
                sep_x = x + col_width
                draw.line([(sep_x, row_y - 4), (sep_x, row_y + 185)],
                          fill=(0, 40, 100))

    def _get_day_precip(self, weather: WeatherData, day) -> Optional[int]:
        """Get max precipitation probability for a given day from hourly data."""
        if not weather.hourly:
            return None
        day_date = day.date.date() if hasattr(day.date, 'date') else day.date
        day_hours = [h for h in weather.hourly if h.time.date() == day_date]
        if not day_hours:
            return None
        return max(h.precipitation_probability for h in day_hours)

    def _draw_almanac(self, img: Image.Image, weather: WeatherData) -> None:
        """Page 4: Almanac — sun, moon, and current weather snapshot."""
        draw = ImageDraw.Draw(img)
        c = weather.current
        y_base = BRAND_BAR_HEIGHT + 8

        draw.text((20, y_base), "ALMANAC", fill=HEADER_YELLOW, font=self._font_header)
        draw.line([(20, y_base + 28), (self.width - 20, y_base + 28)], fill=ACCENT_BAR)

        # --- Sun section (left column) ---
        sun_y = y_base + 38
        draw.text((40, sun_y), "SUN", fill=HEADER_YELLOW, font=self._font_subheader)

        row_h = 30
        label_x = 40
        val_x = 180

        if weather.daily:
            today = weather.daily[0]

            draw.text((label_x, sun_y + 24), "SUNRISE", fill=TEXT_CYAN, font=self._font_data)
            draw.text((val_x, sun_y + 24), (today.sunrise or "N/A").upper(),
                      fill=TEXT_WHITE, font=self._font_data_bold)

            draw.text((label_x, sun_y + 24 + row_h), "SUNSET", fill=TEXT_CYAN,
                      font=self._font_data)
            draw.text((val_x, sun_y + 24 + row_h), (today.sunset or "N/A").upper(),
                      fill=TEXT_WHITE, font=self._font_data_bold)

            # Day length
            if today.sunrise and today.sunset:
                try:
                    sr = datetime.strptime(today.sunrise, "%I:%M %p")
                    ss = datetime.strptime(today.sunset, "%I:%M %p")
                    day_len = (ss - sr).total_seconds() / 3600
                    hours = int(day_len)
                    minutes = int((day_len - hours) * 60)
                    draw.text((label_x, sun_y + 24 + row_h * 2), "DAY LENGTH",
                              fill=TEXT_CYAN, font=self._font_data)
                    draw.text((val_x, sun_y + 24 + row_h * 2), f"{hours}H {minutes}M",
                              fill=TEXT_WHITE, font=self._font_data_bold)
                except ValueError:
                    pass

        # --- Weather snapshot (right column) ---
        snap_x = 350
        draw.text((snap_x, sun_y), "CONDITIONS", fill=HEADER_YELLOW,
                  font=self._font_subheader)

        snap_label_x = snap_x
        snap_val_x = snap_x + 130

        draw.text((snap_label_x, sun_y + 24), "TEMP", fill=TEXT_CYAN, font=self._font_data)
        draw.text((snap_val_x, sun_y + 24), f"{c.temperature:.0f}\u00b0F",
                  fill=_temp_color(c.temperature), font=self._font_data_bold)

        if weather.daily:
            draw.text((snap_label_x, sun_y + 24 + row_h), "HIGH", fill=TEXT_CYAN,
                      font=self._font_data)
            draw.text((snap_val_x, sun_y + 24 + row_h),
                      f"{weather.daily[0].high:.0f}\u00b0F",
                      fill=_temp_color(weather.daily[0].high), font=self._font_data_bold)

            draw.text((snap_label_x, sun_y + 24 + row_h * 2), "LOW", fill=TEXT_CYAN,
                      font=self._font_data)
            draw.text((snap_val_x, sun_y + 24 + row_h * 2),
                      f"{weather.daily[0].low:.0f}\u00b0F",
                      fill=_temp_color(weather.daily[0].low), font=self._font_data_bold)

        # --- Divider ---
        div_y = sun_y + 118
        draw.line([(20, div_y), (self.width - 20, div_y)], fill=ACCENT_BAR)

        # --- Moon phase section ---
        moon_y = div_y + 10
        draw.text((40, moon_y), "MOON PHASE", fill=HEADER_YELLOW, font=self._font_subheader)

        moon = get_moon_phase()

        # Draw moon circle
        moon_cx = 100
        moon_cy = moon_y + 65
        moon_r = 30

        draw.ellipse(
            [moon_cx - moon_r, moon_cy - moon_r,
             moon_cx + moon_r, moon_cy + moon_r],
            fill=(30, 30, 40)
        )

        fraction = moon.get("fraction", 0)
        illum = moon.get("illumination", 0)

        if illum > 5:
            bright_color = (220, 220, 230)
            if fraction <= 0.5:
                lit_width = int(moon_r * 2 * (illum / 100))
                start_x = moon_cx + moon_r - lit_width
                for y in range(-moon_r, moon_r + 1):
                    half_chord = int(math.sqrt(max(0, moon_r ** 2 - y ** 2)))
                    x_right = moon_cx + half_chord
                    x_left = max(start_x, moon_cx - half_chord)
                    if x_left < x_right:
                        draw.line([(x_left, moon_cy + y), (x_right, moon_cy + y)],
                                  fill=bright_color)
            else:
                lit_width = int(moon_r * 2 * (illum / 100))
                end_x = moon_cx - moon_r + lit_width
                for y in range(-moon_r, moon_r + 1):
                    half_chord = int(math.sqrt(max(0, moon_r ** 2 - y ** 2)))
                    x_left = moon_cx - half_chord
                    x_right = min(end_x, moon_cx + half_chord)
                    if x_left < x_right:
                        draw.line([(x_left, moon_cy + y), (x_right, moon_cy + y)],
                                  fill=bright_color)

        # Moon info text
        draw.text((160, moon_y + 35), moon.get("name", "UNKNOWN").upper(),
                  fill=TEXT_WHITE, font=self._font_data_bold)
        draw.text((160, moon_y + 58), f"ILLUMINATION: {illum:.0f}%",
                  fill=TEXT_CYAN, font=self._font_data)

        # --- Divider ---
        div2_y = moon_y + 115
        draw.line([(20, div2_y), (self.width - 20, div2_y)], fill=ACCENT_BAR)

        # --- Additional weather details at bottom ---
        detail_y = div2_y + 10
        col1_x = 40
        col1_v = 180
        col2_x = 340
        col2_v = 470

        draw.text((col1_x, detail_y), "BAROMETER", fill=TEXT_CYAN, font=self._font_data)
        draw.text((col1_v, detail_y), f"{c.pressure:.2f} INHG",
                  fill=TEXT_WHITE, font=self._font_data_bold)

        draw.text((col2_x, detail_y), "HUMIDITY", fill=TEXT_CYAN, font=self._font_data)
        draw.text((col2_v, detail_y), f"{c.humidity}%",
                  fill=TEXT_WHITE, font=self._font_data_bold)

        draw.text((col1_x, detail_y + row_h), "DEWPOINT", fill=TEXT_CYAN,
                  font=self._font_data)
        draw.text((col1_v, detail_y + row_h), f"{c.dewpoint:.0f}\u00b0F",
                  fill=TEXT_WHITE, font=self._font_data_bold)

        wind_dir = get_wind_direction_str(c.wind_direction)
        draw.text((col2_x, detail_y + row_h), "WIND", fill=TEXT_CYAN, font=self._font_data)
        draw.text((col2_v, detail_y + row_h), f"{wind_dir} {c.wind_speed:.0f} MPH",
                  fill=TEXT_WHITE, font=self._font_data_bold)

    def _draw_hourly_forecast(self, img: Image.Image, weather: WeatherData) -> None:
        """Page 5: Hourly Forecast table."""
        draw = ImageDraw.Draw(img)
        y_base = BRAND_BAR_HEIGHT + 8

        draw.text((20, y_base), "HOURLY FORECAST", fill=HEADER_YELLOW,
                  font=self._font_header)
        draw.line([(20, y_base + 28), (self.width - 20, y_base + 28)], fill=ACCENT_BAR)

        # Column headers
        header_y = y_base + 36
        draw.text((30, header_y), "TIME", fill=TEXT_CYAN, font=self._font_small_bold)
        draw.text((150, header_y), "TEMP", fill=TEXT_CYAN, font=self._font_small_bold)
        draw.text((280, header_y), "CONDITIONS", fill=TEXT_CYAN, font=self._font_small_bold)
        draw.text((500, header_y), "PRECIP", fill=TEXT_CYAN, font=self._font_small_bold)

        hours = weather.hourly[:10]
        if not hours:
            draw.text((20, header_y + 30), "NO HOURLY DATA AVAILABLE",
                      fill=TEXT_WHITE, font=self._font_data)
            return

        row_height = 33
        for i, h in enumerate(hours):
            y = header_y + 22 + i * row_height

            # Alternating row background
            if i % 2 == 0:
                draw.rectangle([20, y, self.width - 20, y + row_height - 2], fill=ROW_ALT_1)
            else:
                draw.rectangle([20, y, self.width - 20, y + row_height - 2], fill=ROW_ALT_2)

            # Time
            time_str = h.time.strftime("%I:%M %p").lstrip("0").upper()
            draw.text((30, y + 4), time_str, fill=TEXT_WHITE, font=self._font_hourly)

            # Temperature
            temp_str = f"{h.temperature:.0f}\u00b0F"
            temp_color = _temp_color(h.temperature)
            draw.text((150, y + 4), temp_str, fill=temp_color, font=self._font_hourly_bold)

            # Small weather icon
            is_night = 6 > h.time.hour or h.time.hour >= 20
            small_icon = draw_weather_icon(h.weather_code, size=20, night=is_night)
            img.paste(small_icon, (245, y + 3), small_icon)

            # Conditions
            desc = get_weather_description(h.weather_code).upper()
            if len(desc) > 18:
                desc = desc[:17] + "."
            draw.text((280, y + 4), desc, fill=TEXT_WHITE, font=self._font_hourly)

            # Precipitation probability
            precip_str = f"{h.precipitation_probability}%"
            draw.text((510, y + 4), precip_str, fill=TEXT_CYAN, font=self._font_hourly)

    def _draw_regional_radar(self, img: Image.Image, radar_image: Image.Image) -> None:
        """Page 6a: Regional Radar map."""
        draw = ImageDraw.Draw(img)
        y_base = BRAND_BAR_HEIGHT + 8

        draw.text((20, y_base), "REGIONAL RADAR", fill=HEADER_YELLOW,
                  font=self._font_header)
        draw.line([(20, y_base + 28), (self.width - 20, y_base + 28)], fill=ACCENT_BAR)

        # Scale and center radar image in the page area
        page_area_h = PAGE_HEIGHT - 50  # Leave room for header
        radar_resized = radar_image.resize(
            (min(self.width - 40, page_area_h), page_area_h),
            Image.Resampling.NEAREST,
        )

        # Center horizontally
        rx = (self.width - radar_resized.width) // 2
        ry = y_base + 40
        img.paste(radar_resized, (rx, ry))

        # Location marker (center dot)
        cx = rx + radar_resized.width // 2
        cy_mark = ry + radar_resized.height // 2
        draw.ellipse([cx - 3, cy_mark - 3, cx + 3, cy_mark + 3], fill=(255, 255, 50))

    def _draw_regional_temps(self, img: Image.Image, weather: WeatherData,
                             regional_temps: Optional[list[RegionalCity]]) -> None:
        """Page 6b: Regional Temperatures (fallback when no radar)."""
        draw = ImageDraw.Draw(img)
        y_base = BRAND_BAR_HEIGHT + 8

        draw.text((20, y_base), "REGIONAL TEMPS", fill=HEADER_YELLOW,
                  font=self._font_header)
        draw.line([(20, y_base + 28), (self.width - 20, y_base + 28)], fill=ACCENT_BAR)

        section_y = y_base + 44

        # Local temperature first
        c = weather.current
        draw.text((40, section_y), "LILLINGTON", fill=TEXT_WHITE,
                  font=self._font_data_bold)
        temp_str = f"{c.temperature:.0f}\u00b0F"
        draw.text((250, section_y), temp_str, fill=_temp_color(c.temperature),
                  font=self._font_data_bold)
        desc = get_weather_description(c.weather_code).upper()
        draw.text((340, section_y), desc, fill=TEXT_GRAY, font=self._font_data)

        # Divider
        section_y += 34
        draw.line([(40, section_y), (self.width - 40, section_y)], fill=ACCENT_BAR)
        section_y += 12

        # Regional cities
        cities = regional_temps or []
        for i, city in enumerate(cities):
            y = section_y + i * 36

            # Alternating background
            if i % 2 == 0:
                draw.rectangle([30, y - 2, self.width - 30, y + 30], fill=ROW_ALT_1)
            else:
                draw.rectangle([30, y - 2, self.width - 30, y + 30], fill=ROW_ALT_2)

            draw.text((40, y + 4), city.name.upper(), fill=TEXT_WHITE, font=self._font_data)

            if city.temperature is not None:
                t_str = f"{city.temperature:.0f}\u00b0F"
                draw.text((250, y + 4), t_str, fill=_temp_color(city.temperature),
                          font=self._font_data_bold)
            else:
                draw.text((250, y + 4), "N/A", fill=TEXT_GRAY, font=self._font_data)

    def _draw_ticker(self, img: Image.Image, weather: WeatherData,
                     ticker_offset: float) -> None:
        """Draw the scrolling forecast ticker at the bottom."""
        draw = ImageDraw.Draw(img)
        ticker_y = self.height - TICKER_HEIGHT

        # Ticker background
        draw.rectangle([0, ticker_y, self.width, self.height], fill=TICKER_BG)

        # Accent line at top of ticker
        draw.line([(0, ticker_y), (self.width, ticker_y)], fill=ACCENT_BAR)

        # Build and draw ticker text
        ticker_text = self._build_ticker_text(weather)

        # Double the text for seamless looping
        full_text = ticker_text + "     " + ticker_text

        # Calculate text position with scroll offset
        x = -int(ticker_offset)
        text_y = ticker_y + 7  # Vertically center in ticker bar

        draw.text((x, text_y), full_text, fill=TICKER_TEXT, font=self._font_ticker)

    def _build_ticker_text(self, weather: WeatherData) -> str:
        """Build the scrolling ticker narrative from forecast data."""
        parts = []

        c = weather.current
        desc = get_weather_description(c.weather_code).upper()
        parts.append(
            f"CURRENTLY IN LILLINGTON: {desc}, {c.temperature:.0f}\u00b0F, "
            f"HUMIDITY {c.humidity}%, WIND {get_wind_direction_str(c.wind_direction)} "
            f"AT {c.wind_speed:.0f} MPH"
        )

        if weather.daily:
            today = weather.daily[0]
            parts.append(
                f"TODAY: {get_weather_description(today.weather_code).upper()}, "
                f"HIGH {today.high:.0f}\u00b0F, LOW {today.low:.0f}\u00b0F"
            )

        if len(weather.daily) > 1:
            tmrw = weather.daily[1]
            parts.append(
                f"TOMORROW: {get_weather_description(tmrw.weather_code).upper()}, "
                f"HIGH {tmrw.high:.0f}\u00b0F, LOW {tmrw.low:.0f}\u00b0F"
            )

        for day in weather.daily[2:5]:
            day_name = day.date.strftime("%A").upper()
            parts.append(
                f"{day_name}: {get_weather_description(day.weather_code).upper()}, "
                f"{day.high:.0f}\u00b0/{day.low:.0f}\u00b0"
            )

        return "  \u2022  ".join(parts) + "  \u2022  "

    def get_ticker_text_width(self, weather: WeatherData) -> int:
        """Get the pixel width of the ticker text (for calculating scroll loop)."""
        text = self._build_ticker_text(weather)
        full_text = text + "     " + text
        # Use a temporary image to measure text
        tmp = Image.new("RGB", (1, 1))
        draw = ImageDraw.Draw(tmp)
        bbox = draw.textbbox((0, 0), text + "     ", font=self._font_ticker)
        return bbox[2] - bbox[0]

    def _is_night(self, weather: WeatherData) -> bool:
        """Check if it's currently nighttime based on sunrise/sunset."""
        if not weather.daily:
            hour = datetime.now().hour
            return hour < 6 or hour >= 20

        today = weather.daily[0]
        now = datetime.now()
        try:
            if today.sunrise:
                sr = datetime.strptime(today.sunrise, "%I:%M %p").replace(
                    year=now.year, month=now.month, day=now.day)
            else:
                sr = now.replace(hour=6, minute=0)
            if today.sunset:
                ss = datetime.strptime(today.sunset, "%I:%M %p").replace(
                    year=now.year, month=now.month, day=now.day)
            else:
                ss = now.replace(hour=20, minute=0)
            return now < sr or now > ss
        except ValueError:
            hour = now.hour
            return hour < 6 or hour >= 20
