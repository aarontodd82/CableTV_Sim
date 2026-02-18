"""Weather data fetching from Open-Meteo and RainViewer APIs."""

import math
import time
import requests
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from PIL import Image


@dataclass
class CurrentWeather:
    """Current weather conditions."""
    temperature: float  # Fahrenheit
    feels_like: float
    humidity: int  # Percent
    wind_speed: float  # MPH
    wind_direction: int  # Degrees
    pressure: float  # inHg
    visibility: float  # Miles
    dewpoint: float  # Fahrenheit
    weather_code: int
    observation_time: datetime = field(default_factory=datetime.now)


@dataclass
class HourlyForecast:
    """Hourly forecast data point."""
    time: datetime
    temperature: float
    weather_code: int
    precipitation_probability: int


@dataclass
class DailyForecast:
    """Daily forecast data point."""
    date: datetime
    weather_code: int
    high: float
    low: float
    sunrise: str  # "HH:MM AM" format
    sunset: str


@dataclass
class RegionalCity:
    """Regional city temperature."""
    name: str
    latitude: float
    longitude: float
    temperature: Optional[float] = None


@dataclass
class WeatherData:
    """Complete weather data package."""
    current: CurrentWeather
    hourly: list[HourlyForecast] = field(default_factory=list)
    daily: list[DailyForecast] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=datetime.now)


# WMO Weather Codes → Descriptions
WMO_DESCRIPTIONS = {
    0: "Clear",
    1: "Mainly Clear",
    2: "Partly Cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Rime Fog",
    51: "Light Drizzle",
    53: "Drizzle",
    55: "Heavy Drizzle",
    56: "Freezing Drizzle",
    57: "Heavy Freezing Drizzle",
    61: "Light Rain",
    63: "Rain",
    65: "Heavy Rain",
    66: "Freezing Rain",
    67: "Heavy Freezing Rain",
    71: "Light Snow",
    73: "Snow",
    75: "Heavy Snow",
    77: "Snow Grains",
    80: "Light Showers",
    81: "Showers",
    82: "Heavy Showers",
    85: "Light Snow Showers",
    86: "Heavy Snow Showers",
    95: "Thunderstorm",
    96: "Thunderstorm w/ Hail",
    99: "Severe Thunderstorm",
}

# Regional cities for NC area
NC_REGIONAL_CITIES = [
    RegionalCity("Raleigh", 35.7796, -78.6382),
    RegionalCity("Fayetteville", 35.0527, -78.8784),
    RegionalCity("Durham", 35.9940, -78.8986),
    RegionalCity("Charlotte", 35.2271, -80.8431),
    RegionalCity("Greensboro", 36.0726, -79.7920),
    RegionalCity("Wilmington", 34.2257, -77.9447),
]


def get_weather_description(code: int) -> str:
    """Get human-readable description from WMO weather code."""
    return WMO_DESCRIPTIONS.get(code, "Unknown")


def get_wind_direction_str(degrees: int) -> str:
    """Convert wind direction degrees to compass string."""
    directions = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    idx = round(degrees / 22.5) % 16
    return directions[idx]


class WeatherAPI:
    """Fetches and caches weather data from Open-Meteo."""

    def __init__(self, weather_config):
        self.config = weather_config
        self._cache: Optional[WeatherData] = None
        self._cache_time: float = 0
        self._regional_cache: Optional[list[RegionalCity]] = None
        self._regional_cache_time: float = 0
        self._radar_cache: Optional[Image.Image] = None
        self._radar_cache_time: float = 0

    def get_weather(self) -> Optional[WeatherData]:
        """
        Get current weather data, using cache if fresh.

        Returns cached data on API failure.
        """
        now = time.time()
        cache_age = now - self._cache_time

        if self._cache and cache_age < self.config.refresh_interval:
            return self._cache

        try:
            data = self._fetch_weather()
            if data:
                self._cache = data
                self._cache_time = now
                return data
        except Exception as e:
            print(f"    Weather API error: {e}")

        return self._cache

    def get_regional_temps(self) -> list[RegionalCity]:
        """Get current temperatures for regional cities."""
        now = time.time()
        if self._regional_cache and (now - self._regional_cache_time) < 3600:
            return self._regional_cache

        cities = []
        for city in NC_REGIONAL_CITIES:
            try:
                url = (
                    f"https://api.open-meteo.com/v1/forecast"
                    f"?latitude={city.latitude}&longitude={city.longitude}"
                    f"&current=temperature_2m"
                    f"&temperature_unit=fahrenheit"
                    f"&timezone=America/New_York"
                )
                resp = requests.get(url, timeout=10)
                resp.raise_for_status()
                result = resp.json()
                temp = result.get("current", {}).get("temperature_2m")
                cities.append(RegionalCity(
                    name=city.name,
                    latitude=city.latitude,
                    longitude=city.longitude,
                    temperature=temp,
                ))
            except Exception as e:
                print(f"    Regional temp error for {city.name}: {e}")
                cities.append(RegionalCity(
                    name=city.name,
                    latitude=city.latitude,
                    longitude=city.longitude,
                    temperature=None,
                ))

        self._regional_cache = cities
        self._regional_cache_time = now
        return cities

    def get_radar_image(self) -> Optional[Image.Image]:
        """
        Fetch radar image from RainViewer with a dark base map underneath.

        Fetches CartoDB dark-matter tiles for geography (roads, borders,
        coastlines), then overlays RainViewer radar data with a retro
        green colormap. Returns a composite RGB image or None on failure.
        """
        now = time.time()
        if self._radar_cache and (now - self._radar_cache_time) < 600:
            return self._radar_cache

        if not self.config.radar_enabled:
            return None

        try:
            from io import BytesIO

            # Convert lat/lon to tile coordinates at zoom level 7
            zoom = 7
            lat = self.config.latitude
            lon = self.config.longitude

            n = 2 ** zoom
            center_x = int((lon + 180) / 360 * n)
            lat_rad = math.radians(lat)
            center_y = int((1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2 * n)

            tile_size = 256

            # Phase 1: Fetch dark base map tiles (CartoDB dark-matter, free/no key)
            basemap = Image.new("RGB", (tile_size * 3, tile_size * 3), (0, 0, 0))
            for dy in range(-1, 2):
                for dx in range(-1, 2):
                    tx = center_x + dx
                    ty = center_y + dy
                    base_url = (
                        f"https://basemaps.cartocdn.com/dark_all"
                        f"/{zoom}/{tx}/{ty}.png"
                    )
                    try:
                        base_resp = requests.get(base_url, timeout=10)
                        base_resp.raise_for_status()
                        base_tile = Image.open(BytesIO(base_resp.content)).convert("RGB")
                        px = (dx + 1) * tile_size
                        py = (dy + 1) * tile_size
                        basemap.paste(base_tile, (px, py))
                    except Exception:
                        pass

            # Phase 2: Fetch radar tiles from RainViewer
            resp = requests.get(
                "https://api.rainviewer.com/public/weather-maps.json",
                timeout=10,
            )
            resp.raise_for_status()
            maps_data = resp.json()

            radar_frames = maps_data.get("radar", {}).get("past", [])
            radar_composite = Image.new("RGBA", (tile_size * 3, tile_size * 3), (0, 0, 0, 0))

            if radar_frames:
                latest = radar_frames[-1]
                radar_path = latest.get("path", "")

                if radar_path:
                    for dy in range(-1, 2):
                        for dx in range(-1, 2):
                            tx = center_x + dx
                            ty = center_y + dy
                            tile_url = (
                                f"https://tilecache.rainviewer.com"
                                f"{radar_path}/256/{zoom}/{tx}/{ty}/2/1_1.png"
                            )
                            try:
                                tile_resp = requests.get(tile_url, timeout=10)
                                tile_resp.raise_for_status()
                                tile_img = Image.open(BytesIO(tile_resp.content)).convert("RGBA")
                                px = (dx + 1) * tile_size
                                py = (dy + 1) * tile_size
                                radar_composite.paste(tile_img, (px, py))
                            except Exception:
                                pass

            # Phase 3: Apply retro green tint to basemap + overlay radar
            radar_img = self._apply_retro_colormap(basemap, radar_composite)

            self._radar_cache = radar_img
            self._radar_cache_time = now
            return radar_img

        except Exception as e:
            print(f"    Radar fetch error: {e}")
            return self._radar_cache

    def _apply_retro_colormap(self, basemap: Image.Image,
                              radar: Image.Image) -> Image.Image:
        """Composite basemap + radar into final image.

        Basemap keeps its natural CartoDB dark styling (gray/dark blue).
        Only the radar precipitation data gets a bright green colormap.
        """
        # Brighten the basemap (keep natural colors, just boost luminance)
        from PIL import ImageEnhance
        result = ImageEnhance.Brightness(basemap).enhance(1.6)
        radar_px = radar.load()
        result_px = result.load()
        width, height = result.size

        for y in range(height):
            for x in range(width):
                rr, rg, rb, ra = radar_px[x, y]
                if ra > 20:
                    # Precipitation — bright green overlay
                    intensity = max(rr, rg, rb)
                    if intensity < 60:
                        result_px[x, y] = (0, 100, 0)
                    elif intensity < 120:
                        result_px[x, y] = (0, 170, 0)
                    elif intensity < 180:
                        result_px[x, y] = (20, 240, 30)
                    else:
                        result_px[x, y] = (60, 255, 60)

        return result

    def _fetch_weather(self) -> Optional[WeatherData]:
        """Fetch weather data from Open-Meteo API."""
        lat = self.config.latitude
        lon = self.config.longitude

        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
            f"wind_speed_10m,wind_direction_10m,surface_pressure,weather_code,"
            f"visibility,dewpoint_2m"
            f"&hourly=temperature_2m,weather_code,precipitation_probability"
            f"&daily=weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset"
            f"&temperature_unit=fahrenheit&wind_speed_unit=mph"
            f"&timezone=America/New_York"
        )

        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        # Parse current conditions
        current_data = data.get("current", {})
        pressure_hpa = current_data.get("surface_pressure", 1013.25)
        pressure_inhg = pressure_hpa * 0.02953  # hPa to inHg
        visibility_m = current_data.get("visibility", 10000)
        visibility_mi = visibility_m / 1609.34  # meters to miles

        current = CurrentWeather(
            temperature=current_data.get("temperature_2m", 0),
            feels_like=current_data.get("apparent_temperature", 0),
            humidity=current_data.get("relative_humidity_2m", 0),
            wind_speed=current_data.get("wind_speed_10m", 0),
            wind_direction=current_data.get("wind_direction_10m", 0),
            pressure=round(pressure_inhg, 2),
            visibility=round(visibility_mi, 1),
            dewpoint=current_data.get("dewpoint_2m", 0),
            weather_code=current_data.get("weather_code", 0),
        )

        # Parse hourly forecast (next 24 hours)
        hourly_data = data.get("hourly", {})
        hourly_times = hourly_data.get("time", [])
        hourly_temps = hourly_data.get("temperature_2m", [])
        hourly_codes = hourly_data.get("weather_code", [])
        hourly_precip = hourly_data.get("precipitation_probability", [])

        hourly = []
        now = datetime.now()
        for i, t_str in enumerate(hourly_times[:48]):
            try:
                t = datetime.fromisoformat(t_str)
                if t < now:
                    continue
                hourly.append(HourlyForecast(
                    time=t,
                    temperature=hourly_temps[i] if i < len(hourly_temps) else 0,
                    weather_code=hourly_codes[i] if i < len(hourly_codes) else 0,
                    precipitation_probability=hourly_precip[i] if i < len(hourly_precip) else 0,
                ))
            except (ValueError, IndexError):
                continue

        # Parse daily forecast
        daily_data = data.get("daily", {})
        daily_dates = daily_data.get("time", [])
        daily_codes = daily_data.get("weather_code", [])
        daily_highs = daily_data.get("temperature_2m_max", [])
        daily_lows = daily_data.get("temperature_2m_min", [])
        daily_sunrise = daily_data.get("sunrise", [])
        daily_sunset = daily_data.get("sunset", [])

        daily = []
        for i, d_str in enumerate(daily_dates[:7]):
            try:
                d = datetime.fromisoformat(d_str)
                # Format sunrise/sunset times
                sr = ""
                ss = ""
                if i < len(daily_sunrise) and daily_sunrise[i]:
                    try:
                        sr_dt = datetime.fromisoformat(daily_sunrise[i])
                        sr = sr_dt.strftime("%I:%M %p").lstrip("0")
                    except ValueError:
                        pass
                if i < len(daily_sunset) and daily_sunset[i]:
                    try:
                        ss_dt = datetime.fromisoformat(daily_sunset[i])
                        ss = ss_dt.strftime("%I:%M %p").lstrip("0")
                    except ValueError:
                        pass

                daily.append(DailyForecast(
                    date=d,
                    weather_code=daily_codes[i] if i < len(daily_codes) else 0,
                    high=daily_highs[i] if i < len(daily_highs) else 0,
                    low=daily_lows[i] if i < len(daily_lows) else 0,
                    sunrise=sr,
                    sunset=ss,
                ))
            except (ValueError, IndexError):
                continue

        return WeatherData(
            current=current,
            hourly=hourly[:24],
            daily=daily,
        )
