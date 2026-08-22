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
WEATHER_STATE_PATH = Path("/run/skyhi-weather.json")
PANEL_TEST_PATH = Path("/run/skyhi-panel-test.json")


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
        if not row or time.time() - row[1] > self.ttl:
            return None
        try:
            return json.loads(row[0])
        except ValueError:
            return None

    def _store(self, key: str, value: dict[str, Any]) -> None:
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO enrichment VALUES (?, ?, ?)", (key, json.dumps(value), time.time()))

    def request(self, aircraft: dict[str, Any]) -> dict[str, Any]:
        callsign, hex_code = clean_callsign(aircraft.get("flight")), str(aircraft.get("hex", "")).lower()
        merged: dict[str, Any] = {}
        keys = ([f"route2:{callsign}"] if callsign else []) + ([f"aircraft:{hex_code}"] if hex_code else [])
        for key in keys:
            cached = self.get(key)
            if cached:
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
            if cached:
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
            if kind.startswith("route"):
                response = requests.get(f"https://hexdb.io/api/v1/route/icao/{ident}", timeout=self.timeout)
                response.raise_for_status()
                raw = response.json()
                route = raw.get("route", "")
                if isinstance(route, str) and "-" in route:
                    origin, destination = route.split("-", 1)
                    origin, destination = origin.strip(), destination.strip()
                    value = {"origin": origin, "destination": destination}
                    for label, code in (("origin", origin), ("destination", destination)):
                        try:
                            airport = requests.get(f"https://hexdb.io/api/v1/airport/icao/{code}", timeout=self.timeout)
                            airport.raise_for_status()
                            info = airport.json()
                            value[f"{label}_code"] = info.get("iata") or info.get("icao") or code
                            value[f"{label}_name"] = info.get("airport") or code
                        except Exception:
                            value[f"{label}_code"] = code
                            value[f"{label}_name"] = code
            elif kind == "aircraft":
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
    def __init__(self, airlines: dict[str, list[str]], color_mode: str = "classic", heading: float = 148):
        self.airlines = airlines
        self.color_mode = color_mode
        self.heading = heading
        font = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        bold = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        self.f8 = ImageFont.truetype(font, 8)
        self.f7 = ImageFont.truetype(font, 7)
        self.f9 = ImageFont.truetype(font, 9)
        self.f10 = ImageFont.truetype(bold, 10)
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
        height, gap = 11, 10
        offset = int(time.monotonic() * speed) % (text_width + gap)
        strip = Image.new("RGB", (width, height), "black")
        strip_draw = ImageDraw.Draw(strip)
        strip_draw.text((-offset, 0), text, font=font, fill=fill)
        strip_draw.text((text_width + gap - offset, 0), text, font=font, fill=fill)
        image.paste(strip, position)

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
            # Most source assets are emblem + wide wordmark. On this tiny panel
            # the emblem alone is much more recognizable, so take the leading
            # square from very wide images (e.g. Delta, United, Southwest).
            if logo.width > logo.height * 1.6:
                logo = logo.crop((0, 0, min(logo.height, logo.width), logo.height))
            logo.thumbnail((36, 35), Image.Resampling.LANCZOS)

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
            tile = Image.new("RGBA", (42, 41), (0, 0, 0, 0))
            tile.alpha_composite(logo, ((42 - logo.width) // 2, (41 - logo.height) // 2))
            self.logo_cache[code] = tile
            return tile.copy()
        except (OSError, StopIteration):
            return None

    def identity(self, item: dict[str, Any], extra: dict[str, Any]) -> Image.Image:
        image = Image.new("RGB", (128, 64), "black")
        draw = ImageDraw.Draw(image)
        colors = self.colors()
        callsign = clean_callsign(item.get("flight")) or str(item.get("hex", "")).upper()
        airline, mark, color = self.airline(callsign)
        code = callsign[:3]
        brand = "#" + color
        logo = self.logo(code)
        if logo:
            image.paste(logo, (1, 1), logo)
        else:
            draw.rounded_rectangle((3, 4, 39, 38), radius=4, fill=brand)
            mark = self.fit(draw, mark, self.f14, 32)
            box = draw.textbbox((0, 0), mark, font=self.f14)
            draw.text((21 - (box[2] - box[0]) / 2, 13), mark, font=self.f14, fill="white")
        draw.text((45, 0), self.fit(draw, airline, self.f10, 82), font=self.f10, fill="#FFFFFF")
        origin, destination = extra.get("origin"), extra.get("destination")
        origin_code = extra.get("origin_code") or origin
        destination_code = extra.get("destination_code") or destination
        route = f"{origin_code}-{destination_code}" if origin_code and destination_code else "ROUTE LOOKUP"
        draw.text((45, 13), self.fit(draw, route, self.f14, 82), font=self.f14, fill=colors["accent"])
        aircraft_type = friendly_aircraft_type(extra.get("aircraft_type") or item.get("category"))
        # Reserve enough room for the complete callsign. Seven-character
        # flight identifiers did not fit in the old 31-pixel slot and ended
        # up as "..." on the identity page.
        # Model and flight number form one aligned metadata row. Both use the
        # same font and baseline; their established colors keep them distinct.
        self.marquee(image, (45, 30), aircraft_type, self.f8, 33, colors["warm"])
        self.marquee(image, (80, 30), callsign, self.f8, 47, colors["muted"])
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
        callsign = clean_callsign(item.get("flight")) or str(item.get("hex", "")).upper()
        airline, mark, color = self.airline(callsign)
        logo = self.logo(callsign[:3])
        if logo:
            logo.thumbnail((25, 23), Image.Resampling.LANCZOS)
            image.paste(logo, ((26 - logo.width) // 2, 1), logo)
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
        alt_text = "GROUND" if altitude == "ground" else (f"{int(float(altitude)):,} ft" if altitude is not None else "-- ft")
        speed = item.get("gs")
        speed_text = f"{int(round(float(speed)))} kt" if speed is not None else "-- kt"
        direction = cardinal(item.get("track"))
        aircraft_type = friendly_aircraft_type(extra.get("aircraft_type") or item.get("category"))
        distance = item.get("_distance_nm")
        distance_text = f"{distance:.1f}" if isinstance(distance, float) else "--"

        draw.text((2, 29), "ALT", font=self.f8, fill=colors["muted"])
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
             feed_active: bool | None = None) -> Image.Image:
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
        draw.text((date_x + 1, 19), now.strftime("%b %d").upper(), font=self.f8, fill=colors["muted"])
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
        feed_label = "ADSB DATA FEED"
        feed_color = colors["good"] if feed_active else ("#FF4D4D" if feed_active is False else colors["muted"])
        feed_x = 117 - draw.textlength(feed_label, font=self.f7)
        draw.text((feed_x, 45), feed_label, font=self.f7, fill=feed_color)
        # A compact, unambiguous pixel lamp: green means both feed and MLAT
        # services are active; red means at least one service is down.
        draw.rectangle((121, 47, 123, 49), fill=feed_color)
        heading = self.heading % 360
        scan_text = f"WATCHING {round(heading)}° {cardinal(heading)}"
        draw.text((4, 55), scan_text, font=self.f8, fill=colors["accent"])
        return image


def select_aircraft(payload: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    selected = []
    lat0, lon0 = config.get("receiver_lat"), config.get("receiver_lon")
    heading = config.get("receiver_heading_deg")
    tracking_polygon = config.get("tracking_polygon") or []
    half_fov = float(config.get("window_field_of_view_deg", 120)) / 2
    for raw in payload.get("aircraft", []):
        source = str(raw.get("source") or "")
        freshness = float(config.get("local_max_seen_seconds", 12)) if source in ("local", "local+fr24") else float(config["max_seen_seconds"])
        if float(raw.get("seen", 999)) > freshness:
            continue
        if not clean_callsign(raw.get("flight")) or raw.get("lat") is None or raw.get("lon") is None:
            continue
        item = dict(raw)
        if lat0 is not None and lon0 is not None and item.get("lat") is not None and item.get("lon") is not None:
            if tracking_polygon and not point_in_polygon(float(item["lat"]), float(item["lon"]), tracking_polygon):
                continue
            item["_distance_nm"] = haversine_nm(float(lat0), float(lon0), float(item["lat"]), float(item["lon"]))
            item["_bearing_deg"] = bearing_deg(float(lat0), float(lon0), float(item["lat"]), float(item["lon"]))
            item["_in_view"] = heading is None or angular_difference(item["_bearing_deg"], float(heading)) <= half_fov
        selected.append(item)
    selected.sort(key=lambda x: (not x.get("_in_view", True), x.get("_distance_nm", 1e9), -float(x.get("rssi", -100)), float(x.get("seen", 999))))
    return selected[: int(config["max_aircraft"])]


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
    while not STOP.is_set():
        payload = load_json(Path(config["aircraft_json"]), {})
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
        candidates = select_aircraft(payload, config)
        by_id = {str(a.get("fr24_id") or a.get("hex") or clean_callsign(a.get("flight"))): a for a in candidates}
        aircraft = by_id.get(locked_id) if locked_id else None
        if locked_id and aircraft:
            locked_missing_since = None
            distance = aircraft.get("_distance_nm")
            if isinstance(distance, float):
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

        if aircraft:
            extra = cache.request(aircraft)
            # FR24 full positions already include these authoritative fields.
            for key in ("aircraft_type", "registration", "origin_code", "destination_code"):
                if aircraft.get(key):
                    extra[key] = aircraft[key]
            page = args.page if args.page is not None else int((time.monotonic() - switched) / float(config.get("page_seconds", 5)))
            frame = renderer.aircraft(aircraft, extra, page)
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
