#!/usr/bin/env python3
"""Local-network control panel for SkyHi."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime
from datetime import timedelta
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
WEB_ROOT = APP_DIR / "web"
AIRCRAFT_PATH = Path("/run/skyhi-fr24/aircraft.json")
DUMP1090_PATH = Path("/run/dump1090-fa/aircraft.json")
WEATHER_PATH = Path("/run/skyhi-weather.json")
PANEL_TEST_PATH = Path("/run/skyhi-panel-test.json")
BUDGET_PATH = Path("/home/shravanxd/.local/state/skyhi/fr24-budget.json")
FR24_METADATA_PATH = Path("/home/shravanxd/.local/state/skyhi/fr24-enrichment.json")
ROUTE_CACHE_PATH = APP_DIR / "cache" / "enrichment.sqlite3"
AUTH_PATH = Path("/home/shravanxd/.config/skyhi/control-auth.json")
SERVICES = ("dump1090-fa", "skyhi-fr24", "skyhi-flight-display")
POWER_SERVICES = ("dump1090-fa", "skyhi-fr24", "skyhi-flight-display")

FIELDS: dict[str, tuple[type, float, float]] = {
    "receiver_lat": (float, -90, 90),
    "receiver_lon": (float, -180, 180),
    "brightness": (int, 10, 100),
    "receiver_heading_deg": (float, 0, 359.9),
    "window_field_of_view_deg": (float, 20, 360),
    "fr24_radius_nm": (float, 1, 50),
    "fr24_active_trigger_nm": (float, 1, 50),
    "fr24_active_poll_seconds": (int, 30, 1800),
    "fr24_poll_seconds": (int, 300, 86400),
    "fr24_daily_credit_budget": (int, 100, 60000),
    "fr24_total_credit_budget": (int, 1000, 1000000),
    "page_seconds": (int, 2, 30),
    "target_release_nm": (float, 2, 50),
    "weather_refresh_seconds": (int, 300, 7200),
    "notification_max_altitude_ft": (int, 500, 50000),
}

SCHEDULE_DEFAULTS = {
    "auto_power_enabled": True,
    "auto_power_off_time": "01:30",
    "auto_power_on_time": "08:00",
    "auto_power_weekend_enabled": False,
    "auto_power_weekend_off_time": "02:00",
    "auto_power_weekend_on_time": "09:00",
}
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
DISPLAY_COLOR_MODES = ("classic", "rainbow", "ocean", "sunset", "neon")
PANEL_TEST_PATTERNS = ("logo", "grid", "white", "red", "green", "blue")
PREFERENCE_DEFAULTS = {
    "notifications_enabled": False,
    "notification_airlines": "",
    "notification_aircraft_types": "",
}
AUTH: dict[str, str] = {}


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    value = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3440.065 * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    return (math.degrees(math.atan2(math.sin(dl) * math.cos(p2), math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) + 360) % 360


def angular_difference(left: float, right: float) -> float:
    return abs((left - right + 180) % 360 - 180)


def point_in_polygon(lat: float, lon: float, polygon: list[list[float]]) -> bool:
    inside, previous = False, polygon[-1]
    for current in polygon:
        y1, x1 = previous
        y2, x2 = current
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
        previous = current
    return inside


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, value: Any) -> None:
    fd, temporary = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def service_state(name: str) -> str:
    try:
        result = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True, timeout=3)
        return result.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def initialize_auth() -> str | None:
    """Load authentication, creating a random initial PIN only on first run."""
    global AUTH
    value = read_json(AUTH_PATH, {})
    if value.get("pin_hash") and value.get("salt") and value.get("secret"):
        AUTH = value
        return None
    pin = f"{secrets.randbelow(900000) + 100000}"
    salt = secrets.token_hex(16)
    value = {
        "salt": salt,
        "pin_hash": hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 200000).hex(),
        "secret": secrets.token_hex(32),
    }
    AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_json(AUTH_PATH, value)
    os.chmod(AUTH_PATH, 0o600)
    AUTH = value
    return pin


def pin_matches(pin: str) -> bool:
    candidate = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(AUTH["salt"]), 200000).hex()
    return hmac.compare_digest(candidate, AUTH["pin_hash"])


def session_token() -> str:
    return hmac.new(bytes.fromhex(AUTH["secret"]), b"skyhi-control-v1", hashlib.sha256).hexdigest()


def file_age(path: Path) -> float | None:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def next_schedule_event(config: dict[str, Any]) -> dict[str, Any] | None:
    if not config.get("auto_power_enabled", True):
        return None
    now = datetime.now()
    candidates = []
    for offset in range(8):
        day = (now + timedelta(days=offset)).date()
        weekend = day.weekday() >= 5 and config.get("auto_power_weekend_enabled", False)
        off_key = "auto_power_weekend_off_time" if weekend else "auto_power_off_time"
        on_key = "auto_power_weekend_on_time" if weekend else "auto_power_on_time"
        for action, key in (("off", off_key), ("on", on_key)):
            raw = str(config.get(key, SCHEDULE_DEFAULTS[key]))
            hour, minute = map(int, raw.split(":"))
            moment = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)
            if moment > now:
                candidates.append((moment, action))
    if not candidates:
        return None
    moment, action = min(candidates)
    return {"action": action, "timestamp": moment.isoformat(), "label": moment.strftime("%a %-I:%M %p")}


def budget_forecast(config: dict[str, Any], budget: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now()
    elapsed_minutes = max(1, now.hour * 60 + now.minute)
    today = int(budget.get("credits", 0))
    daily_limit = int(config.get("fr24_daily_credit_budget", 3000))
    projected = round(today * 1440 / elapsed_minutes)
    total_used = int(budget.get("lifetime_credits", 0)) + today
    total_limit = int(config.get("fr24_total_credit_budget", 60000))
    remaining = max(0, total_limit - total_used)
    daily_rate = max(1, projected)
    return {
        "today": today, "daily_limit": daily_limit, "daily_remaining": max(0, daily_limit - today),
        "projected_today": projected, "total_used": total_used, "total_limit": total_limit,
        "total_remaining": remaining, "estimated_days_remaining": round(remaining / daily_rate, 1),
        "throttled": today >= daily_limit,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "SkyHiControl/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.client_address[0]} {fmt % args}", flush=True)

    def send_json(self, value: Any, status: int = 200) -> None:
        body = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_preview(self) -> None:
        from skyhi_display import Renderer
        query = parse_qs(urlparse(self.path).query)
        mode = query.get("mode", ["live"])[0]
        config = read_json(CONFIG_PATH, {})
        renderer = Renderer(read_json(APP_DIR / "airlines.json", {}), str(config.get("display_color_mode", "classic")), float(config.get("receiver_heading_deg") or 148))
        payload = read_json(AIRCRAFT_PATH, {})
        aircraft = next((item for item in payload.get("aircraft", []) if str(item.get("flight") or "").strip()), None)
        if mode == "idle" or aircraft is None:
            feed_active = subprocess.run(
                ["systemctl", "is-active", "--quiet", "adsbfi-feed.service", "adsbfi-mlat.service"],
                check=False,
                timeout=2,
            ).returncode == 0
            frame = renderer.idle(len(payload.get("aircraft", [])), read_json(WEATHER_PATH, {}), feed_active)
        else:
            item = dict(aircraft)
            if item.get("lat") is not None and item.get("lon") is not None and config.get("receiver_lat") is not None:
                item["_distance_nm"] = haversine_nm(float(config["receiver_lat"]), float(config["receiver_lon"]), float(item["lat"]), float(item["lon"]))
            frame = renderer.aircraft(item, item, 1 if mode == "metrics" else 0)
        buffer = io.BytesIO()
        frame.save(buffer, "PNG")
        body = buffer.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_backup(self) -> None:
        route_cache = base64.b64encode(ROUTE_CACHE_PATH.read_bytes()).decode() if ROUTE_CACHE_PATH.exists() else ""
        self.send_json({
            "format": "skyhi-backup-v1", "exported_at": datetime.now().astimezone().isoformat(),
            "config": read_json(CONFIG_PATH, {}), "route_cache_b64": route_cache,
            "fr24_metadata": read_json(FR24_METADATA_PATH, {}),
        })

    def is_authenticated(self) -> bool:
        cookies = self.headers.get("Cookie", "")
        token = next((part.split("=", 1)[1] for part in cookies.split(";") if part.strip().startswith("skyhi_session=")), "")
        return hmac.compare_digest(token, session_token())

    def require_auth(self) -> bool:
        if self.is_authenticated():
            return True
        self.send_json({"ok": False, "error": "Authentication required"}, 401)
        return False

    def do_GET(self) -> None:
        if self.path == "/api/auth":
            self.send_json({"authenticated": self.is_authenticated()})
            return
        if self.path.startswith("/api/") and not self.require_auth():
            return
        if self.path == "/api/status":
            payload = read_json(AIRCRAFT_PATH, {})
            config = read_json(CONFIG_PATH, {})
            lat0, lon0 = config.get("receiver_lat"), config.get("receiver_lon")
            heading = float(config.get("receiver_heading_deg") or 0)
            half_fov = float(config.get("window_field_of_view_deg", 120)) / 2
            polygon = config.get("tracking_polygon") or []
            aircraft = []
            for item in payload.get("aircraft", []):
                callsign = str(item.get("flight") or "").strip()
                if float(item.get("seen", 999)) > 45 or not callsign:
                    continue
                row = {key: item.get(key) for key in (
                    "flight", "hex", "source", "aircraft_type", "registration", "origin_code",
                    "destination_code", "alt_baro", "gs", "track", "seen", "lat", "lon")}
                row["flight"] = callsign
                if lat0 is not None and lon0 is not None and row.get("lat") is not None and row.get("lon") is not None:
                    distance = haversine_nm(float(lat0), float(lon0), float(row["lat"]), float(row["lon"]))
                    bearing = bearing_deg(float(lat0), float(lon0), float(row["lat"]), float(row["lon"]))
                    row["distance_nm"] = round(distance, 1)
                    row["bearing_deg"] = round(bearing)
                    row["in_view"] = angular_difference(bearing, heading) <= half_fov
                    row["in_polygon"] = point_in_polygon(float(row["lat"]), float(row["lon"]), polygon) if polygon else None
                aircraft.append(row)
            aircraft.sort(key=lambda item: (not item.get("in_view", False), float(item.get("distance_nm", 999)), float(item.get("seen") or 999)))
            local_payload = read_json(DUMP1090_PATH, {})
            local_fresh = [item for item in local_payload.get("aircraft", []) if float(item.get("seen", 999)) <= 30]
            strongest = max((float(item.get("rssi", -100)) for item in local_fresh), default=None)
            budget = read_json(BUDGET_PATH, {})
            weather = read_json(WEATHER_PATH, {})
            config.setdefault("fr24_total_credit_budget", 60000)
            config.setdefault("notification_max_altitude_ft", 10000)
            self.send_json({
                "services": {name: service_state(name) for name in SERVICES},
                "config": {**{key: config.get(key) for key in FIELDS},
                           **{key: config.get(key, default) for key, default in SCHEDULE_DEFAULTS.items()},
                           **{key: config.get(key, default) for key, default in PREFERENCE_DEFAULTS.items()},
                           "display_color_mode": config.get("display_color_mode", "classic"),
                           "receiver_lat": config.get("receiver_lat"),
                           "receiver_lon": config.get("receiver_lon"),
                           "tracking_polygon": config.get("tracking_polygon", [])},
                "aircraft": aircraft[:12],
                "queue": aircraft[:5],
                "budget": budget,
                "budget_forecast": budget_forecast(config, budget),
                "weather": weather,
                "next_schedule_event": next_schedule_event(config),
                "diagnostics": {
                    "adsb_file_age_seconds": file_age(DUMP1090_PATH),
                    "merged_file_age_seconds": file_age(AIRCRAFT_PATH),
                    "weather_age_seconds": file_age(WEATHER_PATH),
                    "local_aircraft": len(local_fresh),
                    "strongest_rssi": strongest,
                    "messages": local_payload.get("messages", 0),
                    "fr24_last_success": budget.get("last_success"),
                },
                "updated": time.time(),
            })
            return
        if self.path.startswith("/api/preview"):
            self.send_preview()
            return
        if self.path == "/api/backup":
            self.send_backup()
            return
        if self.path == "/assets/skyhi-logo.png":
            body = (WEB_ROOT / "assets" / "skyhi-logo.png").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path not in ("/", "/index.html"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = (WEB_ROOT / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self) -> None:
        if not self.require_auth():
            return
        if self.path == "/api/backup":
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 16 * 1024 * 1024)
                backup = json.loads(self.rfile.read(length))
                if backup.get("format") != "skyhi-backup-v1" or not isinstance(backup.get("config"), dict):
                    raise ValueError("This is not a valid SkyHi backup")
                route_data = base64.b64decode(backup.get("route_cache_b64", ""), validate=True) if backup.get("route_cache_b64") else b""
                if len(route_data) > 12 * 1024 * 1024:
                    raise ValueError("Route cache backup is too large")
                write_json(CONFIG_PATH, backup["config"])
                if route_data:
                    write_bytes(ROUTE_CACHE_PATH, route_data)
                if isinstance(backup.get("fr24_metadata"), dict):
                    write_json(FR24_METADATA_PATH, backup["fr24_metadata"])
                subprocess.run(["systemctl", "restart", "skyhi-fr24", "skyhi-flight-display"], check=True, timeout=20)
                self.send_json({"ok": True})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        if self.path != "/api/config":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 16384)
            incoming = json.loads(self.rfile.read(length))
            if not isinstance(incoming, dict) or not incoming:
                raise ValueError("No settings supplied")
            config = read_json(CONFIG_PATH, {})
            changed = []
            for key, raw in incoming.items():
                if key in PREFERENCE_DEFAULTS:
                    if key == "notifications_enabled":
                        if not isinstance(raw, bool):
                            raise ValueError("Notifications must be on or off")
                        value = raw
                    else:
                        value = re.sub(r"[^A-Z0-9, ]", "", str(raw).upper())[:160]
                    if config.get(key, PREFERENCE_DEFAULTS[key]) != value:
                        config[key] = value
                        changed.append(key)
                    continue
                if key == "display_color_mode":
                    value = str(raw).lower()
                    if value not in DISPLAY_COLOR_MODES:
                        raise ValueError("Unknown display color mode")
                    if config.get(key, "classic") != value:
                        config[key] = value
                        changed.append(key)
                    continue
                if key in SCHEDULE_DEFAULTS:
                    if key.endswith("_enabled"):
                        if not isinstance(raw, bool):
                            raise ValueError("Automatic schedule must be on or off")
                        value = raw
                    else:
                        value = str(raw)
                        if not TIME_PATTERN.fullmatch(value):
                            raise ValueError(f"{key} must be a valid 24-hour time")
                    if key not in config or config.get(key) != value:
                        config[key] = value
                        changed.append(key)
                    continue
                if key == "tracking_polygon":
                    if raw in (None, []):
                        value = []
                    elif not isinstance(raw, list) or not 3 <= len(raw) <= 100:
                        raise ValueError("Tracking polygon needs 3 to 100 points")
                    else:
                        value = []
                        for point in raw:
                            if not isinstance(point, list) or len(point) != 2:
                                raise ValueError("Each polygon point must be [latitude, longitude]")
                            lat, lon = float(point[0]), float(point[1])
                            if not -90 <= lat <= 90 or not -180 <= lon <= 180:
                                raise ValueError("Polygon coordinates are outside valid ranges")
                            value.append([round(lat, 7), round(lon, 7)])
                    if config.get(key, []) != value:
                        config[key] = value
                        changed.append(key)
                    continue
                if key not in FIELDS:
                    raise ValueError(f"Setting not allowed: {key}")
                kind, minimum, maximum = FIELDS[key]
                value = kind(raw)
                if not minimum <= value <= maximum:
                    raise ValueError(f"{key} must be between {minimum:g} and {maximum:g}")
                if config.get(key) != value:
                    config[key] = value
                    changed.append(key)
            write_json(CONFIG_PATH, config)
            runtime_changes = [key for key in changed if key not in SCHEDULE_DEFAULTS and key not in PREFERENCE_DEFAULTS]
            if runtime_changes:
                subprocess.run(["systemctl", "restart", "skyhi-fr24", "skyhi-flight-display"], check=True, timeout=20)
            self.send_json({"ok": True, "changed": changed})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            self.send_json({"ok": False, "error": f"Unable to apply settings: {exc}"}, 500)

    def do_POST(self) -> None:
        if self.path == "/api/login":
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 4096)
                incoming = json.loads(self.rfile.read(length))
                if not pin_matches(str(incoming.get("pin", ""))):
                    self.send_json({"ok": False, "error": "Incorrect PIN"}, 401)
                    return
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Set-Cookie", f"skyhi_session={session_token()}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        if not self.require_auth():
            return
        if self.path == "/api/logout":
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "skyhi_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/panel-test":
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 4096)
                incoming = json.loads(self.rfile.read(length))
                pattern = str(incoming.get("pattern", "logo"))
                duration = max(3, min(60, int(incoming.get("duration", 10))))
                if pattern not in PANEL_TEST_PATTERNS:
                    raise ValueError("Unknown panel test pattern")
                write_json(PANEL_TEST_PATH, {"pattern": pattern, "expires": time.time() + duration})
                self.send_json({"ok": True, "pattern": pattern, "duration": duration})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        if self.path == "/api/change-pin":
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 4096)
                incoming = json.loads(self.rfile.read(length))
                current, new = str(incoming.get("current_pin", "")), str(incoming.get("new_pin", ""))
                if not pin_matches(current):
                    raise ValueError("Current PIN is incorrect")
                if not re.fullmatch(r"\d{6}", new):
                    raise ValueError("New PIN must contain exactly 6 digits")
                AUTH["salt"] = secrets.token_hex(16)
                AUTH["pin_hash"] = hashlib.pbkdf2_hmac("sha256", new.encode(), bytes.fromhex(AUTH["salt"]), 200000).hex()
                AUTH["secret"] = secrets.token_hex(32)
                write_json(AUTH_PATH, AUTH)
                os.chmod(AUTH_PATH, 0o600)
                self.send_json({"ok": True})
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, 400)
            return
        if not self.path.startswith("/api/action/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        action = self.path.rsplit("/", 1)[-1]
        commands = {
            "restart-display": ["systemctl", "restart", "skyhi-flight-display"],
            "restart-tracker": ["systemctl", "restart", "skyhi-fr24", "dump1090-fa"],
            "restart-all": ["systemctl", "restart", "dump1090-fa", "skyhi-fr24", "skyhi-flight-display"],
            # Keep this web service alive so the same page can turn SkyHi on.
            "turn-off": ["systemctl", "stop", "skyhi-flight-display", "skyhi-fr24", "dump1090-fa"],
            "turn-on": ["systemctl", "start", "dump1090-fa", "skyhi-fr24", "skyhi-flight-display"],
        }
        if action not in commands:
            self.send_json({"ok": False, "error": "Action not allowed"}, 400)
            return
        try:
            subprocess.run(commands[action], check=True, timeout=20)
            self.send_json({"ok": True})
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, 500)


def power_schedule_loop() -> None:
    """Apply the daily local-time power schedule while this control service stays online."""
    last_action = ""
    while True:
        try:
            config = read_json(CONFIG_PATH, {})
            if config.get("auto_power_enabled", SCHEDULE_DEFAULTS["auto_power_enabled"]):
                now = datetime.now()
                current = now.strftime("%H:%M")
                weekend = now.weekday() >= 5 and config.get("auto_power_weekend_enabled", False)
                off_key = "auto_power_weekend_off_time" if weekend else "auto_power_off_time"
                on_key = "auto_power_weekend_on_time" if weekend else "auto_power_on_time"
                action = None
                if current == config.get(off_key, SCHEDULE_DEFAULTS[off_key]):
                    action = "off"
                elif current == config.get(on_key, SCHEDULE_DEFAULTS[on_key]):
                    action = "on"
                action_key = f"{now.date()}:{action}" if action else ""
                if action and action_key != last_action:
                    verb = "stop" if action == "off" else "start"
                    subprocess.run(["systemctl", verb, *POWER_SERVICES], check=True, timeout=25)
                    last_action = action_key
                    print(f"Automatic power {action} applied at {current}", flush=True)
        except Exception as exc:
            print(f"Automatic power schedule error: {exc}", flush=True)
        time.sleep(15)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    new_pin = initialize_auth()
    if new_pin:
        print(f"SkyHi initial control PIN: {new_pin}", flush=True)
    threading.Thread(target=power_schedule_loop, name="power-schedule", daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"SkyHi control listening on {args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
