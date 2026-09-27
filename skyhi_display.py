#!/usr/bin/env python3
"""SkyHi: local dump1090 aircraft data rendered on a 128x64 HUB75 panel."""

from __future__ import annotations

import argparse
import colorsys
import json
import logging
import math
import os
import re
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageDraw, ImageFont

APP_DIR = Path(__file__).resolve().parent
LOG = logging.getLogger("skyhi")
STOP = threading.Event()
RUNTIME_DIR = Path("/run") if os.name != "nt" else APP_DIR / "cache" / "runtime"
WEATHER_STATE_PATH = RUNTIME_DIR / "skyhi-weather.json"
PANEL_TEST_PATH = RUNTIME_DIR / "skyhi-panel-test.json"
TRACKED_FLIGHT_PATH = RUNTIME_DIR / "skyhi-tracked-flight" / "state.json"


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":"))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_nm = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius_nm * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from receiver to aircraft, clockwise from true north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def angular_difference(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


def point_in_polygon(lat: float, lon: float, polygon: list[list[float]]) -> bool:
    inside = False
    previous = polygon[-1]
    for current in polygon:
        y1, x1 = previous
        y2, x2 = current
        if (y1 > lat) != (y2 > lat):
            if lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
                inside = not inside
        previous = current
    return inside


def cardinal(track: Any) -> str:
    try:
        degrees = float(track) % 360
    except (TypeError, ValueError):
        return "--"
    names = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    return names[int((degrees + 22.5) // 45) % 8]


def clean_callsign(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


# ICAO type designators are ideal for data exchange but cryptic to casual
# viewers (for example, B738 means 737-800 and B38M means 737 MAX 8).
# Keep the labels compact enough for the 128x64 panel.
AIRCRAFT_TYPE_LABELS = {
    "B737": "737-700", "B738": "737-800", "B739": "737-900",
    "B38M": "737 MAX 8", "B39M": "737 MAX 9",
    "B752": "757-200", "B753": "757-300",
    "B762": "767-200", "B763": "767-300", "B764": "767-400",
    "B772": "777-200", "B77L": "777-200LR", "B77W": "777-300ER",
    "B778": "777X-8", "B779": "777X-9",
    "B788": "787-8", "B789": "787-9", "B78X": "787-10",
    "A20N": "A320neo", "A21N": "A321neo",
    "A339": "A330-900", "A359": "A350-900", "A35K": "A350-1000",
    "E170": "E170", "E75L": "E175", "E75S": "E175",
    "E190": "E190", "E195": "E195", "E290": "E190-E2", "E295": "E195-E2",
    "CRJ2": "CRJ-200", "CRJ7": "CRJ-700", "CRJ9": "CRJ-900", "CRJX": "CRJ-1000",
}


def friendly_aircraft_type(value: Any) -> str:
    raw = str(value or "").strip().upper()
    return AIRCRAFT_TYPE_LABELS.get(raw, raw or "TYPE --")


class WeatherCache:
    """Fetch current local weather in the background without blocking frames."""

    def __init__(self, lat: float, lon: float, refresh_seconds: int = 300):
        self.lat, self.lon = lat, lon
        self.refresh_seconds = max(300, refresh_seconds)
        self.data: dict[str, Any] = {}
        self.updated = 0.0
        self.fetching = False
        self.lock = threading.Lock()

    def get(self) -> dict[str, Any]:
        with self.lock:
            due = time.time() - self.updated >= self.refresh_seconds
            if due and not self.fetching:
                self.fetching = True
                threading.Thread(target=self._fetch, daemon=True, name="weather").start()
            return dict(self.data)

    def _fetch(self) -> None:
        try:
            response = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={"latitude": self.lat, "longitude": self.lon,
                        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
                        "hourly": "precipitation_probability,weather_code",
                        "forecast_days": 2, "temperature_unit": "fahrenheit",
                        "wind_speed_unit": "mph", "timezone": "auto"},
                timeout=5,
            )
            response.raise_for_status()
            payload = response.json()
            weather_data = dict(payload.get("current", {}))
            daily = payload.get("daily", {})
            for key in ("temperature_2m_max", "temperature_2m_min", "precipitation_probability_max", "weather_code"):
                values = daily.get(key, [])
                if values:
                    weather_data[f"daily_{key}"] = values[0]
            hourly = payload.get("hourly", {})
            times = hourly.get("time", [])
            probabilities = hourly.get("precipitation_probability", [])
            codes = hourly.get("weather_code", [])
            now = datetime.now()
            upcoming = []
            for stamp, probability, weather_code in zip(times, probabilities, codes):
                try:
                    moment = datetime.fromisoformat(stamp)
                except (TypeError, ValueError):
                    continue
                if now <= moment <= now.replace(hour=23, minute=59, second=59) and probability is not None:
                    upcoming.append((moment, int(probability), int(weather_code)))
            rain_codes = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
            next_rain = next((entry for entry in upcoming if entry[1] >= 35 or entry[2] in rain_codes), None)
            if next_rain:
                weather_data["next_rain_time"] = next_rain[0].isoformat(timespec="minutes")
                weather_data["next_rain_probability"] = next_rain[1]
            try:
                alert_response = requests.get(
                    "https://api.weather.gov/alerts/active",
                    params={"point": f"{self.lat:.4f},{self.lon:.4f}"},
                    headers={"User-Agent": "SkyHi/1.0 (local Raspberry Pi flight display)"},
                    timeout=5,
                )
                alert_response.raise_for_status()
                alerts = alert_response.json().get("features", [])
            except Exception as exc:
                LOG.debug("NWS alert lookup failed: %s", exc)
                alerts = []
            weather_data["alerts"] = [
                {key: feature.get("properties", {}).get(key) for key in ("event", "severity", "headline", "expires")}
                for feature in alerts[:5]
            ]
            updated = time.time()
            weather_data["updated"] = updated
            write_json_atomic(WEATHER_STATE_PATH, weather_data)
            with self.lock:
                self.data = weather_data
                self.updated = updated
        except Exception as exc:
            LOG.debug("Weather lookup failed: %s", exc)
        finally:
            with self.lock:
                self.fetching = False


class ServiceHealth:
    """Cache a small group of systemd service checks for the LED status row."""

    def __init__(self, services: tuple[str, ...], refresh_seconds: float = 5.0):
        self.services = services
        self.refresh_seconds = refresh_seconds
        self.active: bool | None = None
        self.next_check = 0.0

    def get(self) -> bool | None:
        now = time.monotonic()
        if now < self.next_check:
            return self.active
        self.next_check = now + self.refresh_seconds
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "--quiet", *self.services],
                check=False,
                timeout=2,
            )
            self.active = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            self.active = False
        return self.active


class EnrichmentCache:
    """SQLite cache plus bounded background lookups; display never waits on HTTP."""

    NEGATIVE_TTL = 3600  # an aircraft HexDB has nothing for is retried hourly, not held for the full TTL

    def __init__(self, path: Path, ttl_days: int, timeout: float):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.ttl, self.timeout = path, ttl_days * 86400, timeout
        self.pending: set[str] = set()
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="lookup")
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS enrichment (key TEXT PRIMARY KEY, value TEXT NOT NULL, fetched REAL NOT NULL)")

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def get(self, key: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT value, fetched FROM enrichment WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row[0])
        except ValueError:
            return None
        ttl = self.ttl if value else self.NEGATIVE_TTL
        if time.time() - row[1] > ttl:
            return None
        return value

    def _store(self, key: str, value: dict[str, Any]) -> None:
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO enrichment VALUES (?, ?, ?)", (key, json.dumps(value), time.time()))

    def request(self, aircraft: dict[str, Any]) -> dict[str, Any]:
        callsign, hex_code = clean_callsign(aircraft.get("flight")), str(aircraft.get("hex", "")).lower()
        merged: dict[str, Any] = {}
        # Only the aircraft itself is looked up here. A route was once taken
        # from HexDB by callsign, but that is a schedule table: it answers
        # "where does this flight number usually go", not "where is this
        # aircraft going". UAL713 came back as Delhi to Chicago while the
        # aircraft overhead was flying San Francisco to Newark. Only FR24,
        # which reports the actual flight, is trusted for a route now.
        keys = [f"aircraft:{hex_code}"] if hex_code else []
        for key in keys:
            cached = self.get(key)
            if cached is not None:
                merged.update(cached)
            else:
                with self.lock:
                    if key not in self.pending:
                        self.pending.add(key)
                        self.pool.submit(self._fetch, key)
        # FR24 full records already carry origin/destination codes. Resolve
        # those directly so airport names do not depend on callsign-route
        # coverage in HexDB.
        for label in ("origin", "destination"):
            code = str(aircraft.get(f"{label}_code") or "").upper()
            if not code:
                continue
            key = f"airport:{code}"
            cached = self.get(key)
            if cached is not None:
                merged[f"{label}_code"] = cached.get("code") or code
                merged[f"{label}_name"] = cached.get("airport_name") or code
            else:
                with self.lock:
                    if key not in self.pending:
                        self.pending.add(key)
                        self.pool.submit(self._fetch, key)
        return merged

    def _fetch(self, key: str) -> None:
        value: dict[str, Any] = {}
        try:
            kind, ident = key.split(":", 1)
            if kind == "aircraft":
                response = requests.get(f"https://hexdb.io/api/v1/aircraft/{ident}", timeout=self.timeout)
                response.raise_for_status()
                raw = response.json()
                value = {
                    "aircraft_type": raw.get("ICAOTypeCode") or raw.get("Type") or "",
                    "registration": raw.get("Registration") or "",
                }
            else:
                code_type = "iata" if len(ident) == 3 else "icao"
                response = requests.get(f"https://hexdb.io/api/v1/airport/{code_type}/{ident}", timeout=self.timeout)
                response.raise_for_status()
                raw = response.json()
                value = {
                    "code": raw.get("iata") or raw.get("icao") or ident,
                    "airport_name": raw.get("airport") or ident,
                }
        except Exception as exc:  # enrichment is optional
            LOG.debug("Lookup failed for %s: %s", key, exc)
        finally:
            # Cache misses briefly too, avoiding a request every display frame.
            self._store(key, value)
            with self.lock:
                self.pending.discard(key)


class Renderer:
    def __init__(self, airlines: dict[str, list[str]], color_mode: str = "classic",
                 heading: float = 148, tracking_range_nm: float = 6):
        self.airlines = airlines
        self.color_mode = color_mode
        self.heading = heading
        self.tracking_range_nm = tracking_range_nm
        # Keep the renderer pixel-identical on Raspberry Pi and on the Windows
        # bridge. Both hosts use the same DejaVu files and point sizes.
        linux_font = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        linux_bold = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
        windows_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        font = str(linux_font if linux_font.is_file() else windows_fonts / "DejaVuSans.ttf")
        bold = str(linux_bold if linux_bold.is_file() else windows_fonts / "DejaVuSans-Bold.ttf")
        self.f8 = ImageFont.truetype(font, 8)
        self.f7 = ImageFont.truetype(font, 7)
        self.f9 = ImageFont.truetype(font, 9)
        self.f10 = ImageFont.truetype(bold, 10)
        self.f12 = ImageFont.truetype(bold, 12)
        self.f14 = ImageFont.truetype(bold, 14)
        self.logo_cache: dict[str, Image.Image] = {}

    def colors(self) -> dict[str, str]:
        palettes = {
            "classic": {"accent": "#62D9FF", "warm": "#FFDE59", "good": "#7DFF9D", "soft": "#C8D1D8", "muted": "#8799A8", "line": "#243544"},
            "ocean": {"accent": "#35D9FF", "warm": "#7A9CFF", "good": "#64FFD0", "soft": "#BDEFFF", "muted": "#709DB2", "line": "#16445A"},
            "sunset": {"accent": "#FF8A5B", "warm": "#FFD166", "good": "#FF6FB5", "soft": "#FFE0C2", "muted": "#B98B8B", "line": "#573040"},
            "neon": {"accent": "#00F5FF", "warm": "#FFF500", "good": "#39FF14", "soft": "#FF77FF", "muted": "#9C8CFF", "line": "#3C2770"},
        }
        if self.color_mode != "rainbow":
            return palettes.get(self.color_mode, palettes["classic"])
        hue = (time.time() / 18.0) % 1.0
        def hue_color(offset: float, saturation: float = .72, value: float = 1.0) -> str:
            red, green, blue = colorsys.hsv_to_rgb((hue + offset) % 1.0, saturation, value)
            return f"#{round(red * 255):02X}{round(green * 255):02X}{round(blue * 255):02X}"
        return {"accent": hue_color(0), "warm": hue_color(.14), "good": hue_color(.34), "soft": hue_color(.55, .35), "muted": "#9AA8B5", "line": hue_color(.72, .55, .38)}

    def airline(self, callsign: str) -> tuple[str, str, str]:
        code = callsign[:3]
        return tuple(self.airlines.get(code, [code or "AIR", code or "?", "404B5A"]))  # type: ignore[return-value]

    @staticmethod
    def fit(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> str:
        if draw.textlength(text, font=font) <= width:
            return text
        while text and draw.textlength(text + "…", font=font) > width:
            text = text[:-1]
        return text + "…"

    @staticmethod
    def marquee(image: Image.Image, position: tuple[int, int], text: str,
                font: ImageFont.FreeTypeFont, width: int, fill: str,
                speed: float = 8.0) -> None:
        """Draw long labels as a seamless horizontal LED marquee."""
        draw = ImageDraw.Draw(image)
        text_width = math.ceil(draw.textlength(text, font=font))
        if text_width <= width:
            draw.text(position, text, font=font, fill=fill)
            return
        # Size the marquee strip from the font's real pixel bounds. A fixed
        # 11-pixel strip clips the bottom of the 14-pixel location heading.
        bounds = draw.textbbox((0, 0), text, font=font)
        height, gap = max(1, math.ceil(bounds[3]) + 1), 10
        offset = int(time.monotonic() * speed) % (text_width + gap)
        strip = Image.new("RGB", (width, height), "black")
        strip_draw = ImageDraw.Draw(strip)
        strip_draw.text((-offset, 0), text, font=font, fill=fill)
        strip_draw.text((text_width + gap - offset, 0), text, font=font, fill=fill)
        image.paste(strip, position)

    @staticmethod
    def logo_frame(logo: Image.Image, width: int, height: int) -> Image.Image:
        """Contain the complete logo in a padded, static viewport."""
        available_width = max(1, width - 6)
        available_height = max(1, height - 6)
        scale = min(available_width / logo.width, available_height / logo.height)
        rendered = logo.resize((max(1, round(logo.width * scale)),
                                max(1, round(logo.height * scale))),
                               Image.Resampling.LANCZOS)
        frame = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        frame.alpha_composite(rendered, ((width - rendered.width) // 2,
                                         (height - rendered.height) // 2))
        return frame

    def logo(self, code: str) -> Image.Image | None:
        if code in self.logo_cache:
            return self.logo_cache[code].copy()
        paths = (
            Path("/opt/skyhi/airline-logos/out/combined/logos") / f"{code}.png",
            APP_DIR / "assets" / "logos" / f"{code}.png",
        )
        try:
            path = next(path for path in paths if path.is_file())
            logo = Image.open(path).convert("RGBA")
            bbox = logo.getchannel("A").getbbox()
            if bbox:
                logo = logo.crop(bbox)
            # Preserve complete wide wordmarks. The page-specific frame scales
            # them proportionally to fit instead of cropping or animating them.
            if logo.height > 35:
                scale = 35 / logo.height
                logo = logo.resize((max(1, round(logo.width * scale)), 35), Image.Resampling.LANCZOS)

            # Pick a contrasting tile from the visible logo pixels. Many
            # aviation assets contain navy/black artwork intended for white
            # web pages and otherwise disappear on the matrix's black canvas.
            visible = [(r, g, b, a) for r, g, b, a in logo.getdata() if a >= 32]
            if len(visible) < 4:
                return None
            weight = sum(pixel[3] for pixel in visible)
            luminance = sum((0.2126 * r + 0.7152 * g + 0.0722 * b) * a for r, g, b, a in visible) / weight
            if luminance < 105:
                # Brighten the artwork itself while keeping full transparency.
                lifted = []
                for r, g, b, a in logo.getdata():
                    pixel_luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
                    boost = max(0, 135 - pixel_luma)
                    lifted.append((min(255, int(r + boost)), min(255, int(g + boost)), min(255, int(b + boost)), a))
                logo.putdata(lifted)
            self.logo_cache[code] = logo
            return logo.copy()
        except (OSError, StopIteration):
            return None

    def identity(self, item: dict[str, Any], extra: dict[str, Any]) -> Image.Image:
        image = Image.new("RGB", (128, 64), "black")
        draw = ImageDraw.Draw(image)
        colors = self.colors()
        # Only a real callsign identifies an operator. Deriving one from a
        # registration invented airlines such as "N68" out of N686UA, so an
        # aircraft flying without a callsign is shown as itself instead.
        flight_callsign = clean_callsign(item.get("flight"))
        tail = str(item.get("registration") or "").upper()
        callsign = flight_callsign or tail or str(item.get("hex") or "").upper()
        if flight_callsign:
            airline, mark, color = self.airline(flight_callsign)
            code = flight_callsign[:3]
        else:
            airline = tail or "UNIDENTIFIED"
            mark = (tail or callsign)[:3] or "--"
            color = "31465E"
            code = ""
        brand = "#" + color
        logo = self.logo(code)
        if logo:
            logo_frame = self.logo_frame(logo, 42, 41)
            image.paste(logo_frame, (1, 1), logo_frame)
        else:
            draw.rounded_rectangle((3, 4, 39, 38), radius=4, fill=brand)
            mark = self.fit(draw, mark, self.f14, 32)
            box = draw.textbbox((0, 0), mark, font=self.f14)
            draw.text((21 - (box[2] - box[0]) / 2, 13), mark, font=self.f14, fill="white")
        draw.text((45, 0), self.fit(draw, airline, self.f10, 82), font=self.f10, fill="#FFFFFF")
        origin, destination = extra.get("origin"), extra.get("destination")
        origin_code = extra.get("origin_code") or origin
        destination_code = extra.get("destination_code") or destination
        # Military, business and general aviation have no scheduled route, so
        # fall back to the registration, which is what identifies them, and
        # show nothing at all rather than announcing a pending lookup.
        if origin_code and destination_code:
            route = f"{origin_code}-{destination_code}"
        else:
            # The registration only helps here when it is not already the
            # headline, which it is for an aircraft flying without a callsign.
            tail_line = str(extra.get("registration") or item.get("registration") or "").upper()
            route = "" if tail_line == callsign else tail_line
        draw.text((45, 13), self.fit(draw, route, self.f14, 82), font=self.f14, fill=colors["accent"])
        aircraft_type = friendly_aircraft_type(extra.get("aircraft_type") or item.get("category"))
        # Reserve enough room for the complete callsign. Seven-character
        # flight identifiers did not fit in the old 31-pixel slot and ended
        # up as "..." on the identity page.
        # Model and flight number form one aligned metadata row. Both use the
        # same font and baseline; their established colors keep them distinct.
        self.marquee(image, (45, 30), aircraft_type, self.f8, 33, colors["warm"])
        # Repeating the headline in the metadata row wasted the only spare
        # line on an aircraft that has no callsign to show there.
        if flight_callsign:
            self.marquee(image, (80, 30), callsign, self.f8, 47, colors["muted"])
        elif item.get("hex"):
            self.marquee(image, (80, 30), str(item["hex"]).upper(), self.f8, 47, colors["muted"])
        draw.line((0, 43, 127, 43), fill=colors["line"])
        origin_name = str(extra.get("origin_name") or origin_code or "Origin unknown")
        destination_name = str(extra.get("destination_name") or destination_code or "Destination unknown")
        draw.text((2, 44), self.fit(draw, origin_name, self.f9, 125), font=self.f9, fill=colors["soft"])
        draw.text((2, 54), self.fit(draw, destination_name, self.f9, 125), font=self.f9, fill=colors["soft"])
        return image

    def metrics(self, item: dict[str, Any], extra: dict[str, Any]) -> Image.Image:
        image = Image.new("RGB", (128, 64), "black")
        draw = ImageDraw.Draw(image)
        colors = self.colors()
        callsign = (clean_callsign(item.get("flight"))
                    or str(item.get("registration") or item.get("hex") or "").upper())
        airline, mark, color = self.airline(callsign)
        logo = self.logo(callsign[:3])
        if logo:
            logo_frame = self.logo_frame(logo, 26, 25)
            image.paste(logo_frame, (0, 0), logo_frame)
        else:
            brand = "#" + color
            draw.rounded_rectangle((1, 2, 25, 23), radius=3, fill=brand)
            mark = self.fit(draw, mark, self.f10, 22)
            box = draw.textbbox((0, 0), mark, font=self.f10)
            draw.text((13 - (box[2] - box[0]) / 2, 7), mark, font=self.f10, fill="white")
        draw.text((29, 1), self.fit(draw, callsign, self.f14, 98), font=self.f14, fill="#FFFFFF")
        origin, destination = extra.get("origin_code") or extra.get("origin"), extra.get("destination_code") or extra.get("destination")
        route = f"{origin}>{destination}" if origin and destination else airline
        draw.text((29, 16), self.fit(draw, route, self.f9, 98), font=self.f9, fill=colors["accent"])
        draw.line((0, 27, 127, 27), fill=colors["line"])

        altitude = item.get("alt_baro", item.get("alt_geom"))
        alt_text = "GROUND" if altitude == "ground" else (f"{int(float(altitude)):,}" if altitude is not None else "--")
        speed = item.get("gs")
        speed_text = f"{int(round(float(speed)))} kt" if speed is not None else "-- kt"
        direction = cardinal(item.get("track"))
        aircraft_type = friendly_aircraft_type(extra.get("aircraft_type") or item.get("category"))
        distance = item.get("_distance_nm")
        distance_text = f"{distance:.1f}" if isinstance(distance, float) else "--"

        draw.text((2, 29), "ALT - ft", font=self.f8, fill=colors["muted"])
        draw.text((2, 38), alt_text, font=self.f10, fill=colors["warm"])
        draw.text((51, 29), "SPEED", font=self.f8, fill=colors["muted"])
        draw.text((51, 38), speed_text, font=self.f10, fill=colors["good"])
        draw.text((98, 29), "DIR", font=self.f8, fill=colors["muted"])
        draw.text((98, 38), direction, font=self.f10, fill=colors["accent"])
        draw.line((0, 51, 127, 51), fill=colors["line"])
        self.marquee(image, (2, 53), aircraft_type, self.f9, 49, colors["soft"])
        draw.text((53, 53), self.fit(draw, airline, self.f9, 47), font=self.f9, fill="#FFFFFF")
        draw.text((102, 53), distance_text, font=self.f8, fill=colors["muted"])
        return image

    def aircraft(self, item: dict[str, Any], extra: dict[str, Any], page: int = 0) -> Image.Image:
        return self.identity(item, extra) if page % 2 == 0 else self.metrics(item, extra)

    def tracked_progress(self, image: Image.Image, state: dict[str, Any]) -> None:
        draw = ImageDraw.Draw(image)
        progress = state.get("progress")
        draw.rounded_rectangle((2, 1, 125, 4), radius=2, fill="#3B4650")
        if isinstance(progress, (int, float)):
            width = max(3, round(123 * max(0, min(1, float(progress)))))
            draw.rounded_rectangle((2, 1, 2 + width, 4), radius=2, fill="#62F58A")
        else:
            draw.rounded_rectangle((2, 1, 16, 4), radius=2, fill="#62F58A")

    @staticmethod
    def duration_text(seconds: Any) -> str:
        if not isinstance(seconds, (int, float)) or seconds < 0:
            # Matches how the other unknown metrics read, rather than claiming
            # a calculation is in progress.
            return "--"
        minutes = max(0, round(float(seconds) / 60))
        hours, minutes = divmod(minutes, 60)
        return f"{hours}H {minutes:02d}M" if hours else f"{minutes} MIN"

    def tracked(self, state: dict[str, Any], page: int) -> Image.Image:
        image = Image.new("RGB", (128, 64), "black")
        draw = ImageDraw.Draw(image)
        colors = self.colors()
        self.tracked_progress(image, state)
        item = state.get("aircraft") or {}
        callsign = clean_callsign(state.get("callsign")) or "TRACKED"
        status = str(state.get("status") or "SEARCHING")
        origin = state.get("origin") or {}
        destination = state.get("destination") or {}
        origin_code = str(origin.get("code") or item.get("origin_code") or "---")
        destination_code = str(destination.get("code") or item.get("destination_code") or "---")
        logo = self.logo(callsign[:3])

        if page % 3 == 0:
            if logo:
                frame = self.logo_frame(logo, 27, 22)
                image.paste(frame, (1, 7), frame)
            draw.text((30, 6), self.fit(draw, callsign, self.f14, 96), font=self.f14, fill="#FFFFFF")
            route = f"{origin_code}  >  {destination_code}"
            draw.text((30, 21), self.fit(draw, route, self.f10, 96), font=self.f10, fill=colors["accent"])
            draw.line((2, 33, 125, 33), fill=colors["line"])
            draw.text((2, 35), "FLIGHT STATUS", font=self.f7, fill=colors["muted"])
            draw.text((2, 44), self.fit(draw, status, self.f10, 59), font=self.f10,
                      fill=colors["good"] if status in ("EN ROUTE", "LANDED") else colors["warm"])
            draw.text((67, 35), "TIME TO DEST", font=self.f7, fill=colors["muted"])
            draw.text((67, 44), self.duration_text(state.get("eta_seconds")), font=self.f10, fill=colors["warm"])
            # Never announce that the route is being worked out. Show journey
            # progress, fall back to distance still to fly, and print nothing
            # at all rather than a placeholder.
            progress = state.get("progress")
            remaining = state.get("remaining_nm")
            if isinstance(progress, (int, float)):
                footer = f"{float(progress) * 100:.1f}% COMPLETE"
            elif isinstance(remaining, (int, float)):
                footer = f"{round(float(remaining)):,} NM TO RUN"
            else:
                footer = ""
            if footer:
                draw.text((2, 55), footer, font=self.f8, fill=colors["soft"])
            return image

        if page % 3 == 1:
            draw.text((2, 7), callsign, font=self.f10, fill="#FFFFFF")
            draw.text((124 - draw.textlength(status, font=self.f8), 8), status, font=self.f8, fill=colors["good"])
            draw.line((2, 19, 125, 19), fill=colors["line"])
            altitude = item.get("alt_baro", item.get("alt_geom"))
            altitude_text = "GROUND" if altitude == "ground" else (f"{int(float(altitude)):,}" if altitude is not None else "--")
            speed = item.get("gs")
            speed_text = f"{round(float(speed))}" if speed is not None else "--"
            heading = item.get("track")
            heading_text = f"{round(float(heading))}°" if heading is not None else "--"
            for x, label, value, color in ((2, "ALT - ft", altitude_text, colors["warm"]),
                                           (48, "SPEED - kt", speed_text, colors["good"]),
                                           (96, "HEADING", heading_text, colors["accent"])):
                draw.text((x, 22), label, font=self.f7, fill=colors["muted"])
                draw.text((x, 32), value, font=self.f10, fill=color)
            draw.line((2, 45, 125, 45), fill=colors["line"])
            aircraft_type = friendly_aircraft_type(item.get("aircraft_type"))
            registration = str(item.get("registration") or "")
            self.marquee(image, (2, 48), f"{aircraft_type}  {registration}".strip(), self.f9, 82, colors["soft"])
            remaining = state.get("remaining_nm")
            remaining_text = f"{float(remaining):.0f} NM" if isinstance(remaining, (int, float)) else "-- NM"
            draw.text((124 - draw.textlength(remaining_text, font=self.f8), 50), remaining_text, font=self.f8, fill=colors["muted"])
            return image

        location = state.get("location") or {}
        primary = str(location.get("primary") or "LOCATING AIRCRAFT")
        secondary = str(location.get("secondary") or ("OCEAN POSITION" if location.get("kind") == "ocean" else ""))
        draw.text((2, 7), "CURRENTLY OVER", font=self.f7, fill=colors["muted"])
        self.marquee(image, (2, 15), primary.upper(), self.f12, 124, colors["accent"], speed=7)
        if secondary:
            self.marquee(image, (2, 31), secondary.upper(), self.f9, 124, colors["soft"], speed=6)
        draw.line((2, 43, 125, 43), fill=colors["line"])
        draw.text((2, 46), "LOCAL TIME", font=self.f7, fill=colors["muted"])
        draw.text((2, 53), str(state.get("local_time") or "--"), font=self.f10, fill="#FFFFFF")
        eta = self.duration_text(state.get("eta_seconds"))
        draw.text((124 - draw.textlength(eta, font=self.f9), 52), eta, font=self.f9, fill=colors["warm"])
        return image

    REMEMBRANCE_INTRO_SHARE = 1 / 3  # first three seconds of the nine

    def remembrance(self, phase: float = 0.0) -> Image.Image:
        """September 11 tribute, in two parts across the hold.

        The opening third is the dedication; the rest is the towers with the
        tribute light running up out of the roofs. phase is 0..1 across the
        whole hold, and each part receives its own 0..1 so the animation does
        not have to know how long the hold is.
        """
        phase = max(0.0, min(1.0, phase))
        if phase < self.REMEMBRANCE_INTRO_SHARE:
            return self._remembrance_intro(phase / self.REMEMBRANCE_INTRO_SHARE)
        span = 1 - self.REMEMBRANCE_INTRO_SHARE
        return self._remembrance_towers((phase - self.REMEMBRANCE_INTRO_SHARE) / span)

    def _remembrance_sky(self, draw: ImageDraw.ImageDraw, t: float, warm: float = 0.0) -> None:
        """Night sky graded towards a soft dawn at the horizon."""
        for y in range(64):
            depth = y / 63
            glow = warm * depth ** 2
            draw.line((0, y, 127, y), fill=(
                int(3 + 10 * depth + 70 * glow),
                int(7 + 17 * depth + 34 * glow),
                int(26 + 40 * depth + 20 * glow),
            ))

    def _remembrance_intro(self, t: float) -> Image.Image:
        """Dedication card: the words, over drifting motes of light."""
        image = Image.new("RGB", (128, 64), "black")
        draw = ImageDraw.Draw(image)
        self._remembrance_sky(draw, t, warm=0.55)

        # Motes drifting gently upward, wrapping, so the card breathes rather
        # than sitting still. Positions are derived, not random, so successive
        # frames stay coherent.
        for index in range(22):
            base_x = (index * 37) % 128
            speed = 0.55 + (index % 5) * 0.16
            y = (58 - ((t * speed * 46) + index * 11) % 64)
            sway = math.sin(t * math.tau + index) * 2
            brightness = 0.35 + 0.65 * (1 - abs(y - 30) / 34)
            hue = 0.55 + 0.12 * math.sin(index + t * math.tau)
            red, green, blue = colorsys.hsv_to_rgb(hue % 1.0, 0.45, max(0.0, min(1.0, brightness)))
            draw.point((int((base_x + sway) % 128), int(y)), fill=(int(red * 255), int(green * 255), int(blue * 255)))

        # The words fade up over the first third of the card and hold.
        reveal = min(1.0, t * 3.0)
        headline = "NEVER FORGET"
        font = self.f14
        if draw.textlength(headline, font=font) > 122:
            font = self.f12
        if draw.textlength(headline, font=font) > 122:
            font = self.f10
        width = draw.textlength(headline, font=font)
        value = int(255 * reveal)
        draw.text((64 - width / 2, 19), headline, font=font, fill=(value, value, value))

        date = "SEPTEMBER 11, 2001"
        date_font = self.f8 if draw.textlength(date, font=self.f8) <= 122 else self.f7
        width = draw.textlength(date, font=date_font)
        warm_value = int(228 * reveal)
        draw.text((64 - width / 2, 37), date, font=date_font,
                  fill=(warm_value, int(warm_value * 0.86), int(warm_value * 0.55)))

        # A ribbon that draws itself outward from the centre.
        half = int(62 * min(1.0, max(0.0, t * 1.6)))
        for offset in range(half):
            k = offset / 62
            colour = (int(235 - 120 * k), int(90 + 120 * k), int(110 + 145 * k))
            draw.point((64 + offset, 50), fill=colour)
            draw.point((63 - offset, 50), fill=colour)
        return image

    @staticmethod
    def _smoothstep(value: float) -> float:
        value = max(0.0, min(1.0, value))
        return value * value * (3 - 2 * value)

    def _helix_point(self, progress: float, strand: int, pair_centre: float,
                     targets: tuple[float, float], base_y: float, apex_y: float,
                     turns: float) -> tuple[float, float, float, float]:
        """Where a strand sits at this height: x, y, orbit radius and facing."""
        converge = self._smoothstep(max(0.0, (progress - 0.40) / 0.60))
        orbit_centre = pair_centre * (1 - converge) + targets[strand] * converge
        radius = 15.0 * (1 - converge)
        angle = progress * math.tau * turns + strand * math.pi
        x = orbit_centre + radius * math.sin(angle)
        y = base_y + (apex_y - base_y) * progress
        return x, y, radius, math.cos(angle)

    def _remembrance_towers(self, t: float) -> Image.Image:
        """The towers, with the tribute light running upward out of the roofs."""
        image = Image.new("RGB", (128, 64), "black")
        draw = ImageDraw.Draw(image)
        ground = 46
        self._remembrance_sky(draw, t, warm=0.18)

        for x, y in ((8, 10), (21, 19), (34, 7), (104, 13), (117, 23), (95, 6), (14, 30), (112, 35), (41, 5), (88, 16)):
            twinkle = 0.55 + 0.45 * math.sin(t * math.tau + x)
            value = int(170 * twinkle)
            draw.point((x, y), fill=(value, value, min(255, int(value * 1.25))))

        for x0, w, h in ((0, 6, 8), (7, 5, 12), (13, 6, 7), (20, 4, 14), (25, 6, 9), (32, 5, 11), (38, 6, 6),
                         (86, 5, 7), (92, 6, 11), (99, 5, 8), (105, 7, 13), (113, 5, 9), (119, 6, 11), (126, 2, 7)):
            draw.rectangle((x0, ground - h, x0 + w - 1, ground), fill=(9, 17, 34))
            draw.line((x0, ground - h, x0 + w - 1, ground - h), fill=(24, 41, 70))

        north, south = (53, 18), (66, 21)
        for left, top in (north, south):
            draw.rectangle((left, top, left + 8, ground), fill=(26, 38, 64))
            for x in range(left + 1, left + 8, 2):
                draw.line((x, top + 2, x, ground - 1), fill=(48, 69, 108))
            draw.line((left, top, left, ground), fill=(104, 134, 188))
            draw.line((left + 8, top, left + 8, ground), fill=(14, 22, 40))
            draw.line((left, top, left + 8, top), fill=(162, 192, 240))
            for row in range(top + 4, ground - 2, 5):
                for col in range(left + 1, left + 8, 3):
                    if (row * 7 + col * 3) % 11 < 4:
                        draw.point((col, row), fill=(206, 214, 152))

        # The light. Packets travel upward out of each roof and fade as they
        # climb, so the beam reads as flowing rather than merely glowing. Two
        # and a half cycles across the six seconds keeps it calm.
        # Order forming out of turbulence: two strands of light wind around the
        # pair like a double helix, joined by rungs, and tighten onto the roofs
        # until they straighten into the beams. Two strands, two towers.
        pair_centre = (north[0] + south[0] + 8) / 2
        targets = (north[0] + 4.0, south[0] + 4.0)
        base_y, apex_y = float(ground + 5), float(north[1] - 4)
        turns = 2.4
        drift = t * 0.9

        def plot(px: int, py: int, colour: tuple[int, int, int]) -> None:
            if 0 <= px <= 127 and 0 <= py <= 63:
                have = image.getpixel((px, py))
                draw.point((px, py), fill=(max(have[0], colour[0]),
                                           max(have[1], colour[1]),
                                           max(have[2], colour[2])))

        def shade(progress: float, brightness: float, behind: bool) -> tuple[int, int, int]:
            """Red, white and blue running up the helix as it forms.

            The bands travel, so colour moves through the structure while it is
            still turbulent, then washes to the blue-white of the tribute as the
            strands come into alignment and become the beams.
            """
            converge = self._smoothstep(max(0.0, (progress - 0.40) / 0.60))
            band = (progress * 2.2 - drift * 1.6) % 1.0
            red_c, white_c, blue_c = (255, 58, 64), (255, 255, 255), (64, 116, 255)
            if band < 1 / 3:
                k = band * 3
                mix = [red_c[i] + (white_c[i] - red_c[i]) * k for i in range(3)]
            elif band < 2 / 3:
                k = (band - 1 / 3) * 3
                mix = [white_c[i] + (blue_c[i] - white_c[i]) * k for i in range(3)]
            else:
                k = (band - 2 / 3) * 3
                mix = [blue_c[i] + (red_c[i] - blue_c[i]) * k for i in range(3)]
            # Settle towards the colour the beams actually cast.
            beam = (206, 228, 255)
            wash = converge * 0.78
            mix = [mix[i] + (beam[i] - mix[i]) * wash for i in range(3)]
            scale = max(0.0, min(1.0, brightness)) * (0.42 if behind else 1.0)
            return tuple(int(max(0, min(255, value * scale))) for value in mix)

        # The rungs, drawn first so the strands sit over them.
        for step in range(11):
            progress = 0.04 + step * 0.083
            left_x, y, radius, left_face = self._helix_point(
                progress, 0, pair_centre, targets, base_y, apex_y, turns)
            right_x, _, _, _ = self._helix_point(
                progress, 1, pair_centre, targets, base_y, apex_y, turns)
            separation = abs(right_x - left_x)
            if separation < 2.5:
                continue
            pulse = 0.45 + 0.55 * math.sin(math.tau * (progress * 2.4 - drift * 1.8))
            colour = shade(progress, 0.30 + 0.40 * pulse, left_face < 0)
            step_x = 1 if right_x >= left_x else -1
            for x in range(int(round(left_x)), int(round(right_x)) + step_x, step_x):
                plot(x, int(round(y)), colour)

        # The two strands.
        for strand in (0, 1):
            for step in range(170):
                progress = step / 169
                x, y, radius, facing = self._helix_point(
                    progress, strand, pair_centre, targets, base_y, apex_y, turns)
                behind = facing < 0 and radius > 3
                pulse = 0.55 + 0.45 * math.sin(math.tau * (progress * 2.6 - drift * 2.0))
                plot(int(round(x)), int(round(y)), shade(progress, 0.34 + 0.52 * pulse, behind))

        # Bright nodes running up the strands, the light gathering as it climbs.
        nodes = 12
        for index in range(nodes):
            strand = index % 2
            progress = (drift + index / nodes) % 1.0
            x, y, radius, facing = self._helix_point(
                progress, strand, pair_centre, targets, base_y, apex_y, turns)
            behind = facing < 0 and radius > 3
            colour = shade(progress, 1.0, behind)
            px, py = int(round(x)), int(round(y))
            plot(px, py, colour)
            halo = tuple(int(c * 0.5) for c in colour)
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                plot(px + dx, py + dy, halo)

        # A searchlight, not a plume. Three things made this read as smoke
        # before: the column widened as it climbed, it faded to nothing in
        # mid-air as though dissipating, and the pulses varied brightness
        # across its width so it billowed. A real tribute beam is very nearly
        # parallel, stays bright, and leaves the top of the frame still lit,
        # which is what makes it read as light continuing upward.
        # The beams strengthen as the butterflies reach the top, so the light
        # is something they turn into rather than something already there.
        arrival = self._smoothstep((t - 0.30) / 0.45)
        flow = t * 2.2
        for left, top in (north, south):
            centre = left + 4
            for y in range(0, top):
                above = top - y
                climb = above / max(1, top)
                # Still clearly alight at the top edge, so it carries on past it.
                falloff = 0.52 + 0.48 * (1 - climb) ** 1.15
                # Fine striations travelling upward: motion without billowing.
                striation = 0.90 + 0.10 * math.sin(math.tau * (above / 5.0 - flow * 2.4))
                breathe = 0.94 + 0.06 * math.sin(math.tau * (t * 0.75))
                vertical = falloff * striation * breathe * (0.10 + 0.90 * arrival)
                # Very slight taper, the way a distant beam converges.
                spread = 3.5 - 0.5 * climb
                for x in range(centre - 6, centre + 7):
                    if not 0 <= x <= 127:
                        continue
                    offset = abs(x - centre) / spread
                    if offset >= 1:
                        continue
                    # Sharp bright core with a narrow halo, rather than an
                    # evenly diffuse column.
                    edge = (1 - offset * offset)
                    intensity = vertical * (0.30 * edge + 0.70 * edge ** 4)
                    if intensity <= 0.02:
                        continue
                    intensity = min(1.0, intensity)
                    # Blue at the margins washing to white in the core.
                    saturation = 0.62 * (1 - min(1.0, intensity * 1.15))
                    red, green, blue = colorsys.hsv_to_rgb(0.60, saturation, min(1.0, intensity * 1.30))
                    existing = image.getpixel((x, y))
                    draw.point((x, y), fill=(
                        max(existing[0], int(red * 255)),
                        max(existing[1], int(green * 255)),
                        max(existing[2], int(blue * 255)),
                    ))

        mast_x = north[0] + 4
        draw.line((mast_x, north[1] - 9, mast_x, north[1]), fill=(214, 230, 255))
        draw.point((mast_x, north[1] - 10), fill=(255, 236, 190))

        for x in range(128):
            k = x / 127
            if k < 0.5:
                j = k * 2
                colour = (int(196 + 59 * j), int(38 + 217 * j), int(58 + 197 * j))
            else:
                j = (k - 0.5) * 2
                colour = (int(255 - 205 * j), int(255 - 145 * j), 255)
            draw.point((x, 48), fill=colour)
            draw.point((x, 49), fill=tuple(c // 3 for c in colour))

        caption = "9.11  NEVER FORGET"
        font = self.f8 if draw.textlength(caption, font=self.f8) <= 124 else self.f7
        width = draw.textlength(caption, font=font)
        draw.text((64 - width / 2, 52), caption, font=font, fill=(255, 226, 156))
        return image

    def test_pattern(self, pattern: str) -> Image.Image:
        solid = {"white": "#FFFFFF", "red": "#FF0000", "green": "#00FF00", "blue": "#0000FF"}
        if pattern in solid:
            return Image.new("RGB", (128, 64), solid[pattern])
        image = Image.new("RGB", (128, 64), "black")
        draw = ImageDraw.Draw(image)
        if pattern == "grid":
            for x in range(0, 128, 8):
                draw.line((x, 0, x, 63), fill="#266A82")
            for y in range(0, 64, 8):
                draw.line((0, y, 127, y), fill="#266A82")
            draw.rectangle((0, 0, 127, 63), outline="#FFFFFF")
            draw.text((43, 26), "128x64", font=self.f10, fill="#FFDE59")
            return image
        colors = self.colors()
        draw.rounded_rectangle((1, 1, 126, 62), radius=5, outline=colors["accent"], width=2)
        draw.text((33, 12), "SKYHI", font=self.f14, fill=colors["accent"])
        draw.text((25, 31), "PANEL TEST", font=self.f10, fill=colors["warm"])
        draw.text((38, 47), "READY", font=self.f10, fill=colors["good"])
        return image

    def idle(self, total: int = 0, weather: dict[str, Any] | None = None,
             feed_active: bool | None = None, status: str | None = None) -> Image.Image:
        image = Image.new("RGB", (128, 64), "black")
        draw = ImageDraw.Draw(image)
        colors = self.colors()
        now = datetime.now()
        clock = now.strftime("%H:%M")
        weather = weather or {}
        temperature = weather.get("temperature_2m")
        code = int(weather.get("weather_code", -1))
        if code == 0:
            condition, weather_color = "CLEAR", colors["warm"]
        elif code in (1, 2):
            condition, weather_color = "PARTLY CLOUDY", colors["soft"]
        elif code == 3:
            condition, weather_color = "CLOUDY", colors["muted"]
        elif code in (45, 48):
            condition, weather_color = "FOG", colors["muted"]
        elif code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
            condition, weather_color = "RAIN", colors["accent"]
        elif code in (71, 73, 75, 77, 85, 86):
            condition, weather_color = "SNOW", "#FFFFFF"
        elif code in (95, 96, 99):
            condition, weather_color = "THUNDER", colors["good"]
        else:
            condition, weather_color = "LOCAL WEATHER", colors["muted"]

        high = weather.get("daily_temperature_2m_max")
        low = weather.get("daily_temperature_2m_min")
        rain = weather.get("daily_precipitation_probability_max")
        daily_code = int(weather.get("daily_weather_code", -1))
        rainy_codes = (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99)
        rainy_day = (rain is not None and float(rain) >= 40) or daily_code in rainy_codes

        draw.rounded_rectangle((0, 0, 127, 63), radius=4, outline="#183747")
        draw.text((4, 2), clock, font=self.f14, fill=colors["accent"])
        temp_text = f"{round(float(temperature))}°" if temperature is not None else "--°"
        draw.text((124 - draw.textlength(temp_text, font=self.f14), 2), temp_text, font=self.f14, fill=weather_color)
        # Explicit tracking keeps narrow weekday letters (especially FRI)
        # distinct after rasterization onto the physical 2 mm LED grid.
        date_x = 4.0
        for letter in now.strftime("%a").upper():
            draw.text((date_x, 19), letter, font=self.f8, fill=colors["muted"])
            date_x += draw.textlength(letter, font=self.f8) + 1
        # Leave a full dark LED column between the custom-spaced weekday and
        # the month so labels such as "SAT AUG" cannot visually merge.
        draw.text((date_x + 3, 19), now.strftime("%b %d").upper(), font=self.f8, fill=colors["muted"])
        condition = self.fit(draw, condition, self.f8, 66)
        draw.text((124 - draw.textlength(condition, font=self.f8), 19), condition, font=self.f8, fill=weather_color)
        draw.line((4, 30, 123, 30), fill=colors["line"])
        high_text = f"H {round(float(high))}°" if high is not None else "H --°"
        low_text = f"L {round(float(low))}°" if low is not None else "L --°"
        rain_text = f"RAIN {round(float(rain))}%" if rain is not None else "RAIN --%"
        draw.text((4, 32), high_text, font=self.f8, fill=colors["warm"])
        draw.text((42, 32), low_text, font=self.f8, fill=colors["soft"])
        draw.text((124 - draw.textlength(rain_text, font=self.f8), 32), rain_text, font=self.f8,
                  fill=colors["accent"] if rainy_day else colors["muted"])
        draw.text((4, 43), "SKYHI", font=self.f10, fill=colors["accent"])
        signal_label = f"{total} SIGNAL{'S' if total != 1 else ''}"
        feed_color = (STATUS_COLOURS.get(status or "unknown", STATUS_COLOURS["unknown"]) if status
                      else (colors["good"] if feed_active else
                            ("#FF4D4D" if feed_active is False else colors["muted"])))
        signal_x = 116 - draw.textlength(signal_label, font=self.f7)
        draw.text((signal_x, 45), signal_label, font=self.f7, fill=colors["soft"])
        # Keep feed health visually secondary: the aircraft count carries the
        # useful label while this isolated lamp quietly reports service state.
        draw.rectangle((121, 47, 123, 49), fill=feed_color)
        heading = self.heading % 360
        scan_text = f"WATCHING {round(heading)}° {cardinal(heading)}"
        draw.text((4, 55), scan_text, font=self.f8, fill=colors["accent"])
        range_text = f"{self.tracking_range_nm:g} NM"
        range_x = 124 - draw.textlength(range_text, font=self.f8)
        draw.text((range_x, 55), range_text, font=self.f8, fill=colors["muted"])
        return image


def select_aircraft(payload: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    selected = []
    lat0, lon0 = config.get("receiver_lat"), config.get("receiver_lon")
    heading = config.get("receiver_heading_deg")
    tracking_polygon = config.get("tracking_polygon") or []
    half_fov = float(config.get("window_field_of_view_deg", 120)) / 2
    for raw in payload.get("aircraft", []):
        if raw.get("lat") is None or raw.get("lon") is None:
            continue
        # A callsign is not required. Plenty of aircraft never broadcast one,
        # including airliners before the crew sets it, and refusing them meant
        # something low and close overhead simply never reached the panel. A
        # registration or the ICAO address identifies it well enough.
        if not (clean_callsign(raw.get("flight")) or raw.get("registration") or raw.get("hex")):
            continue
        item = dict(raw)
        if lat0 is not None and lon0 is not None and item.get("lat") is not None and item.get("lon") is not None:
            if tracking_polygon and not point_in_polygon(float(item["lat"]), float(item["lon"]), tracking_polygon):
                continue
            item["_distance_nm"] = haversine_nm(float(lat0), float(lon0), float(item["lat"]), float(item["lon"]))
            item["_bearing_deg"] = bearing_deg(float(lat0), float(lon0), float(item["lat"]), float(item["lon"]))
            item["_in_view"] = heading is None or angular_difference(item["_bearing_deg"], float(heading)) <= half_fov
        # Freshness is judged after the geometry, because how long a gap is
        # tolerable depends on where the aircraft is. Reception drops out for
        # tens of seconds routinely, and applying the strict local allowance to
        # an aircraft crossing the window made it disappear from the panel
        # exactly while it was worth looking at.
        source = str(item.get("source") or "")
        local = source in ("local", "local+fr24", "local+adsb.fi")
        freshness = (float(config.get("local_max_seen_seconds", 12)) if local
                     else float(config.get("max_seen_seconds", 45)))
        if item.get("_in_view"):
            freshness = max(freshness, float(config.get("max_seen_seconds", 45)))
        if float(item.get("seen", 999)) > freshness:
            continue
        selected.append(item)
    selected.sort(key=lambda x: (not x.get("_in_view", True), x.get("_distance_nm", 1e9), -float(x.get("rssi", -100)), float(x.get("seen", 999))))
    return selected[: int(config["max_aircraft"])]


# The lamp on the idle screen reports the two links that can fail silently:
# whether the aerial is still feeding adsb.fi, and whether FR24 will still
# answer. Both were previously invisible until something looked wrong on the
# panel, which is how a dead feed and an empty credit balance each ran for days.
STATUS_COLOURS = {
    "healthy": "#62F58A",       # feeding, and FR24 answering
    "no_credits": "#FFA53D",    # feeding, but FR24 is out of credits
    "not_feeding": "#FFFFFF",   # FR24 fine, but nothing reaching adsb.fi
    "both_down": "#FF4D4D",     # neither
    "unknown": "#8799A8",
}


def link_status(feeding: bool | None, fr24_ok: bool | None) -> str:
    """Which of the two outward links are working."""
    if feeding is None or fr24_ok is None:
        return "unknown"
    if feeding and fr24_ok:
        return "healthy"
    if not feeding and not fr24_ok:
        return "both_down"
    return "no_credits" if feeding else "not_feeding"


def target_key(item: dict[str, Any]) -> str:
    return str(item.get("hex") or clean_callsign(item.get("flight")))


def should_switch_target(current: dict[str, Any] | None, candidate: dict[str, Any] | None,
                         seconds_on_current: float, page_seconds: float) -> bool:
    """Whether the panel should leave the aircraft it is showing for a better one.

    Holding one aircraft until it recedes past the release distance meant a
    flight crossing the window went unseen while the panel stayed with
    something far away that happened to be picked first. An aircraft actually
    in the window takes precedence immediately; among aircraft in the same
    state the panel waits out the current page before moving, and needs a
    clear margin, so it does not flick between two aircraft at similar range.
    """
    if candidate is None or current is None:
        return candidate is not None
    if target_key(candidate) == target_key(current):
        return False

    current_in_view = bool(current.get("_in_view", True))
    candidate_in_view = bool(candidate.get("_in_view", True))
    # Something has entered the window while the panel is showing something
    # that has not. Switch at once: this is the whole purpose of the display.
    if candidate_in_view and not current_in_view:
        return True
    if current_in_view and not candidate_in_view:
        return False

    current_distance = float(current.get("_distance_nm") or 1e9)
    candidate_distance = float(candidate.get("_distance_nm") or 1e9)
    # A full margin of a nautical mile, so two aircraft at similar range do
    # not trade the panel back and forth.
    if candidate_distance >= current_distance - 1.0:
        return False
    # Let the page it is on finish first.
    return seconds_on_current >= max(1.0, page_seconds)


def is_small_aircraft(item: dict[str, Any]) -> bool:
    """Identify slow, low contacts that commonly orbit near the receiver."""
    try:
        speed = float(item.get("gs"))
        altitude = float(item.get("alt_baro", item.get("alt_geom")))
        return speed <= 180 and altitude <= 12000
    except (TypeError, ValueError):
        return False


def make_matrix(config: dict[str, Any]):
    from rgbmatrix import RGBMatrix, RGBMatrixOptions
    m = config["matrix"]
    options = RGBMatrixOptions()
    options.rows, options.cols = int(m["rows"]), int(m["cols"])
    options.chain_length, options.parallel = int(m["chain_length"]), int(m["parallel"])
    options.hardware_mapping = m["hardware_mapping"]
    options.gpio_slowdown = int(m["gpio_slowdown"])
    options.pwm_bits = int(m["pwm_bits"])
    options.brightness = int(config["brightness"])
    options.drop_privileges = False
    return RGBMatrix(options=options)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(APP_DIR / "config.json"))
    parser.add_argument("--render-png", help="render one frame to a PNG instead of GPIO")
    parser.add_argument("--json", help="override aircraft JSON (useful for testing)")
    parser.add_argument("--page", type=int, choices=(0, 1), help="force identity (0) or metrics (1) in PNG tests")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_json(Path(args.config), load_json(APP_DIR / "config.example.json", {}))
    if args.json:
        config["aircraft_json"] = args.json
    renderer = Renderer(
        load_json(APP_DIR / "airlines.json", {}),
        str(config.get("display_color_mode", "classic")),
        float(config.get("receiver_heading_deg") or 148),
        float(config.get("fr24_radius_nm") or 6),
    )
    cache = EnrichmentCache(APP_DIR / "cache" / "enrichment.sqlite3", int(config["route_cache_days"]), float(config["api_timeout_seconds"]))
    weather = WeatherCache(float(config["receiver_lat"]), float(config["receiver_lon"]),
                           int(config.get("weather_refresh_seconds", 300)))
    adsbfi_health = ServiceHealth(("adsbfi-feed.service", "adsbfi-mlat.service"), 5.0)
    matrix = None if args.render_png else make_matrix(config)
    canvas = matrix.CreateFrameCanvas() if matrix else None
    locked_id: str | None = None
    locked_min_distance = float("inf")
    locked_missing_since: float | None = None
    switched = time.monotonic()
    displayed_seconds: dict[str, float] = {}
    suppressed_until: dict[str, float] = {}
    while not STOP.is_set():
        payload = load_json(Path(config["aircraft_json"]), {})
        tracked_state = load_json(TRACKED_FLIGHT_PATH, {})
        weather_data = weather.get()
        panel_test = load_json(PANEL_TEST_PATH, {})
        if float(panel_test.get("expires", 0)) > time.time():
            frame = renderer.test_pattern(str(panel_test.get("pattern", "logo")))
            if args.render_png:
                frame.save(args.render_png)
                return 0
            canvas.SetImage(frame)
            canvas = matrix.SwapOnVSync(canvas)
            STOP.wait(0.5)
            continue
        now_mono = time.monotonic()
        suppressed_until = {key: expiry for key, expiry in suppressed_until.items() if expiry > now_mono}
        candidates = [item for item in select_aircraft(payload, config)
                      if str(item.get("fr24_id") or item.get("hex") or clean_callsign(item.get("flight"))) not in suppressed_until]
        by_id = {str(a.get("fr24_id") or a.get("hex") or clean_callsign(a.get("flight"))): a for a in candidates}
        aircraft = by_id.get(locked_id) if locked_id else None
        if locked_id and aircraft:
            locked_missing_since = None
            repeat_cycles = int(config.get("avoid_circling_cycles", 3))
            repeat_limit = repeat_cycles * 2 * float(config.get("page_seconds", 5))
            if repeat_cycles and is_small_aircraft(aircraft) and displayed_seconds.get(locked_id, 0) >= repeat_limit:
                paused_id = locked_id
                suppressed_until[locked_id] = now_mono + 1800
                LOG.info("Pausing repeated small aircraft %s for 30 minutes after %d cycles", locked_id, repeat_cycles)
                locked_id, aircraft = None, None
                candidates = [item for item in candidates
                              if str(item.get("fr24_id") or item.get("hex") or clean_callsign(item.get("flight"))) != paused_id]
            distance = aircraft.get("_distance_nm") if aircraft else None
            if aircraft and isinstance(distance, float):
                receding = distance > locked_min_distance + 0.5
                locked_min_distance = min(locked_min_distance, distance)
                if receding and distance > float(config.get("target_release_nm", 10)):
                    LOG.info("Releasing %s at %.1f nm after fly-by", locked_id, distance)
                    locked_id, aircraft = None, None
        elif locked_id:
            locked_missing_since = locked_missing_since or time.monotonic()
            if time.monotonic() - locked_missing_since > 45:
                LOG.info("Releasing missing target %s", locked_id)
                locked_id, locked_missing_since = None, None

        if aircraft is None and candidates and locked_id is None:
            # select_aircraft orders window-visible contacts first, then nearest.
            aircraft = candidates[0]
            locked_id = str(aircraft.get("fr24_id") or aircraft.get("hex") or clean_callsign(aircraft.get("flight")))
            locked_min_distance = float(aircraft.get("_distance_nm", float("inf")))
            switched = time.monotonic()
            LOG.info("Locked target %s at %.1f nm", locked_id, locked_min_distance)

        tracked_screen_seconds = max(3, float(tracked_state.get("screen_seconds", 5)))
        tracked_normal_seconds = max(5, float(tracked_state.get("normal_seconds", 15)))
        tracked_block = tracked_screen_seconds * 3
        tracked_cycle = tracked_block + tracked_normal_seconds
        tracked_phase = (time.time() - float(tracked_state.get("ready_at") or time.time())) % tracked_cycle
        show_tracked = bool(tracked_state.get("active") and tracked_state.get("ready")
                            and time.time() - float(tracked_state.get("updated", 0)) < 30
                            and tracked_phase < tracked_block)
        if show_tracked:
            tracked_page = min(2, int(tracked_phase / tracked_screen_seconds))
            frame = renderer.tracked(tracked_state, tracked_page)
        elif aircraft:
            extra = cache.request(aircraft)
            # FR24 full positions already include these authoritative fields.
            for key in ("aircraft_type", "registration", "origin_code", "destination_code"):
                if aircraft.get(key):
                    extra[key] = aircraft[key]
            page = args.page if args.page is not None else int((time.monotonic() - switched) / float(config.get("page_seconds", 5)))
            frame = renderer.aircraft(aircraft, extra, page)
            if locked_id:
                displayed_seconds[locked_id] = displayed_seconds.get(locked_id, 0) + 0.5
        else:
            frame = renderer.idle(len(payload.get("aircraft", [])), weather_data, adsbfi_health.get())
        if args.render_png:
            frame.save(args.render_png)
            return 0
        canvas.SetImage(frame)
        canvas = matrix.SwapOnVSync(canvas)
        STOP.wait(0.5)
    if matrix:
        matrix.Clear()
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    raise SystemExit(main())
