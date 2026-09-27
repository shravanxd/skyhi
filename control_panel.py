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
import shutil
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
WINDOWS_RUNTIME = APP_DIR / "cache" / "runtime"
RUNTIME_ROOT = WINDOWS_RUNTIME if os.name == "nt" else Path("/run")
AIRCRAFT_PATH = WINDOWS_RUNTIME / "aircraft.json" if os.name == "nt" else Path("/run/skyhi-fr24/aircraft.json")
# Debian ships readsb rather than dump1090-fa, so the local decoder and where
# it writes its JSON are both configurable instead of assumed.
RECEIVER_SERVICE = os.environ.get("SKYHI_RECEIVER_SERVICE", "dump1090-fa")
DUMP1090_PATH = (WINDOWS_RUNTIME / "dump1090-aircraft.json" if os.name == "nt"
                 else Path(os.environ.get("SKYHI_RECEIVER_JSON", "/run/dump1090-fa/aircraft.json")))
WEATHER_PATH = WINDOWS_RUNTIME / "skyhi-weather.json" if os.name == "nt" else Path("/run/skyhi-weather.json")
PANEL_TEST_PATH = WINDOWS_RUNTIME / "skyhi-panel-test.json" if os.name == "nt" else Path("/run/skyhi-panel-test.json")
# Every service must agree on these regardless of which user runs it, so they
# come from the environment the units share rather than each process's own home.
STATE_DIR = Path(os.environ.get("SKYHI_STATE_DIR", "/home/shravanxd/.local/state/skyhi"))
CONFIG_DIR = Path(os.environ.get("SKYHI_CONFIG_DIR", "/home/shravanxd/.config/skyhi"))
TRACKED_FLIGHT_REQUEST_PATH = WINDOWS_RUNTIME / "tracked-flight-request.json" if os.name == "nt" else STATE_DIR / "tracked-flight-request.json"
TRACKED_FLIGHT_STATE_PATH = WINDOWS_RUNTIME / "skyhi-tracked-flight" / "state.json" if os.name == "nt" else Path("/run/skyhi-tracked-flight/state.json")
BUDGET_PATH = WINDOWS_RUNTIME / "fr24-budget.json" if os.name == "nt" else STATE_DIR / "fr24-budget.json"
FR24_METADATA_PATH = WINDOWS_RUNTIME / "fr24-enrichment.json" if os.name == "nt" else STATE_DIR / "fr24-enrichment.json"
ROUTE_CACHE_PATH = APP_DIR / "cache" / "enrichment.sqlite3"
AUTH_PATH = WINDOWS_RUNTIME / "control-auth.json" if os.name == "nt" else CONFIG_DIR / "control-auth.json"
BRIDGE_STATE_PATH = WINDOWS_RUNTIME / "pc-bridge-state.json" if os.name == "nt" else Path("/run/skyhi-bridge-state.json")
MERGED_FEED_PATH = WINDOWS_RUNTIME / "merged-aircraft.json" if os.name == "nt" else Path("/run/skyhi-fr24/aircraft.json")
POWER_STATE_PATH = WINDOWS_RUNTIME / "power.json" if os.name == "nt" else Path("/run/skyhi-power.json")
PROCESS_FILE_PATH = WINDOWS_RUNTIME / "windows-processes.json"
# The ESP32 receives finished frames over Wi-Fi, so the Pi no longer runs a
# GPIO renderer; skyhi-esp32-bridge replaces the old skyhi-flight-display unit.
DISPLAY_SERVICE = "skyhi-esp32-bridge"
SERVICES = (RECEIVER_SERVICE, "skyhi-fr24", "skyhi-flight-tracker", DISPLAY_SERVICE)
WINDOWS_SERVICES = ("fr24", "bridge", "tracker", "portal")
WINDOWS_SCRIPTS = {
    "fr24": ["fr24_poller.py", "--config", "config.json"],
    "bridge": ["skyhi_pc_bridge.py"],
    "tracker": ["flight_tracker.py"],
}
POWER_SERVICES = (RECEIVER_SERVICE, "skyhi-fr24", "skyhi-flight-tracker", DISPLAY_SERVICE)

FIELDS: dict[str, tuple[type, float, float]] = {
    "receiver_lat": (float, -90, 90),
    "receiver_lon": (float, -180, 180),
    "brightness": (int, 10, 100),
    "receiver_heading_deg": (float, 0, 359.9),
    "window_field_of_view_deg": (float, 20, 360),
    "adsbfi_poll_seconds": (int, 2, 60),
    "adsbfi_close_poll_seconds": (int, 1, 10),
    "fr24_radius_nm": (float, 1, 50),
    "fr24_active_trigger_nm": (float, 1, 50),
    "fr24_active_poll_seconds": (int, 30, 1800),
    "fr24_poll_seconds": (int, 300, 86400),
    "fr24_daily_credit_budget": (int, 100, 60000),
    "fr24_total_credit_budget": (int, 1000, 1000000),
    "page_seconds": (int, 2, 30),
    "target_release_nm": (float, 2, 50),
    "avoid_circling_cycles": (int, 0, 20),
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



# FR24 reports its own usage, which is the only authoritative figure; the local
# tally only counts what this Pi believes it spent. The endpoint rate limits
# quickly, so it is fetched at most once every few minutes and served from
# cache in between, and the dashboard polls status every few seconds.
FR24_USAGE_URL = "https://fr24api.flightradar24.com/api/usage"
FR24_USAGE_TTL = 300.0
FR24_USAGE: dict[str, Any] = {"fetched": 0.0, "value": None}


def fr24_token() -> str:
    for line in read_text_lines(CONFIG_DIR / "fr24.env"):
        if line.startswith("FR24_API_TOKEN="):
            return line.split("=", 1)[1].strip()
    return os.environ.get("FR24_API_TOKEN", "")


def read_text_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def fr24_usage() -> dict[str, Any] | None:
    """Credits FR24 itself says have been spent, cached against its rate limit."""
    if time.time() - float(FR24_USAGE["fetched"]) < FR24_USAGE_TTL:
        return FR24_USAGE["value"]
    token = fr24_token()
    if not token:
        return None
    FR24_USAGE["fetched"] = time.time()
    try:
        import urllib.request
        request = urllib.request.Request(FR24_USAGE_URL, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json",
            "Accept-Version": "v1",
            # FR24 rejects the default urllib agent with 403.
            "User-Agent": "SkyHi/1.0",
        })
        with urllib.request.urlopen(request, timeout=8) as response:
            rows = json.loads(response.read()).get("data") or []
        spent = sum(float(row.get("credits") or 0) for row in rows)
        requests_made = sum(int(row.get("request_count") or 0) for row in rows)
        FR24_USAGE["value"] = {
            "credits_used": round(spent),
            "requests": requests_made,
            "endpoints": rows,
            "checked_at": time.time(),
        }
    except Exception as exc:
        # Keep the last good figure; a rate limit or blip should not blank the
        # card, but say plainly that it is not current.
        previous = FR24_USAGE["value"] or {}
        FR24_USAGE["value"] = {**previous, "error": str(exc)[:80]}
    return FR24_USAGE["value"]



def link_state() -> dict[str, Any]:
    """The same two-link health the panel's lamp shows, for the dashboard.

    Read from what the collector publishes rather than checked again here, so
    the portal and the panel can never disagree about whether the feed is up.
    """
    from skyhi_display import STATUS_COLOURS, link_status
    merged = read_json(MERGED_FEED_PATH, {})
    feeding = merged.get("feeding_adsbfi")
    fr24_ok = merged.get("fr24_ok")
    state = link_status(feeding, fr24_ok)
    return {
        "feeding_adsbfi": feeding,
        "fr24_ok": fr24_ok,
        "status": state,
        "colour": STATUS_COLOURS.get(state),
        "label": {
            "healthy": "Feeding adsb.fi, FR24 answering",
            "no_credits": "Feeding adsb.fi, FR24 out of credits",
            "not_feeding": "FR24 answering, not feeding adsb.fi",
            "both_down": "Not feeding adsb.fi, FR24 unavailable",
            "unknown": "Link state unknown",
        }[state],
    }



def aircraft_identity(item: dict[str, Any]) -> str:
    """The same fallback the panel itself uses: callsign, else registration,
    else the ICAO hex address, which is close to always present.

    /api/status and the preview each had their own "must have a callsign"
    filter, left over from before the panel was fixed to show an aircraft that
    has not transmitted one. An aircraft heard only by hex existed on the LED
    panel but was invisible on the dashboard and in preview, with nothing to
    say why: not an error, just silently absent from a different code path
    that never got the same fix.
    """
    return (str(item.get("flight") or "").strip()
            or str(item.get("registration") or "").strip()
            or str(item.get("hex") or "").strip().upper())



_ENRICHMENT_CACHE = None


def fill_aircraft_metadata(row: dict[str, Any], cached: dict[str, Any]) -> None:
    """Fill type/registration from the cache only where the row has nothing.

    A network source or FR24 answer is never overwritten by a cached HexDB
    guess; the cache only covers a gap, never replaces a live answer.
    """
    row["aircraft_type"] = row.get("aircraft_type") or cached.get("aircraft_type")
    row["registration"] = row.get("registration") or cached.get("registration")


def enrichment_cache():
    """The same HexDB aircraft-type/registration cache the panel renders with.

    The panel already resolves this at render time via EnrichmentCache, so a
    purely local aircraft like N444EA (a private jet FR24 has no data for) had
    its type and registration on the LED panel the moment HexDB answered. The
    dashboard read only the merged feed's own fields, which local reception
    alone never populates, so the same aircraft showed nothing on the portal
    while the panel already had it. This is a read-only lookup, not a second
    fetcher: it returns whatever the panel's own background lookups have
    already cached, or nothing, rather than triggering a network request from
    a dashboard endpoint that gets polled every few seconds.
    """
    global _ENRICHMENT_CACHE
    if _ENRICHMENT_CACHE is None:
        from skyhi_display import EnrichmentCache
        config = read_json(CONFIG_PATH, {})
        _ENRICHMENT_CACHE = EnrichmentCache(ROUTE_CACHE_PATH,
                                            int(config.get("route_cache_days", 30)),
                                            float(config.get("api_timeout_seconds", 3)))
    return _ENRICHMENT_CACHE


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
        # utf-8-sig strips a leading BOM if present without affecting plain
        # utf-8 files; windows-processes.json is written by both PowerShell
        # (Set-Content -Encoding utf8 adds a BOM) and this module (no BOM).
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
    if os.name == "nt":
        if name == "portal":
            return "active"
        # Each Windows helper process has no systemd equivalent, so liveness
        # is read from the state file it refreshes on every loop tick.
        heartbeat_path = {"fr24": MERGED_FEED_PATH, "bridge": BRIDGE_STATE_PATH,
                           "tracker": TRACKED_FLIGHT_STATE_PATH}.get(name)
        age = file_age(heartbeat_path) if heartbeat_path else None
        return "active" if age is not None and age < 10 else "inactive"
    try:
        result = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True, timeout=3)
        return result.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def windows_restart(names: list[str]) -> list[str]:
    """Stop and relaunch the named Windows helper processes; returns those actually (re)started."""
    processes = read_json(PROCESS_FILE_PATH, [])
    if not isinstance(processes, list):
        processes = []
    by_name = {item.get("name"): item.get("pid") for item in processes if isinstance(item, dict)}
    for name in names:
        pid = by_name.get(name)
        if pid:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=5, check=False)
    pythonw = shutil.which("pythonw.exe") or shutil.which("pythonw") or "pythonw.exe"
    log_dir = WINDOWS_RUNTIME / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    started = []
    for name in names:
        if name not in WINDOWS_SCRIPTS or (name == "fr24" and not os.environ.get("FR24_API_TOKEN")):
            continue
        with open(log_dir / f"{name}.log", "a") as stdout, open(log_dir / f"{name}-error.log", "a") as stderr:
            proc = subprocess.Popen([pythonw, *WINDOWS_SCRIPTS[name]], cwd=str(APP_DIR),
                                     stdout=stdout, stderr=stderr, creationflags=subprocess.CREATE_NO_WINDOW)
        started.append({"name": name, "pid": proc.pid})
    remaining = [item for item in processes if isinstance(item, dict) and item.get("name") not in names]
    write_json(PROCESS_FILE_PATH, remaining + started)
    return [item["name"] for item in started]


def initialize_auth() -> str | None:
    """Load authentication, creating a random initial PIN only on first run."""
    global AUTH
    value = read_json(AUTH_PATH, {})
    if value.get("pin_hash") and value.get("salt") and value.get("secret"):
        AUTH = value
        return None
    pin = os.environ.get("SKYHI_INITIAL_PIN") or ("121212" if os.name == "nt" else f"{secrets.randbelow(900000) + 100000}")
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


def current_auth() -> dict[str, str]:
    """Authentication as it stands on disk, not as it stood at startup.

    The PIN was held in memory from the moment the service started, so
    rotating it left the running portal still checking the old one and
    rejecting the new PIN until someone restarted the service.
    """
    global AUTH
    value = read_json(AUTH_PATH, {})
    if value.get("pin_hash") and value.get("salt") and value.get("secret"):
        if value != AUTH:
            AUTH = value
        return value
    return AUTH


def pin_matches(pin: str) -> bool:
    auth = current_auth()
    candidate = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(auth["salt"]), 200000).hex()
    return hmac.compare_digest(candidate, auth["pin_hash"])


def session_token() -> str:
    return hmac.new(bytes.fromhex(current_auth()["secret"]), b"skyhi-control-v1", hashlib.sha256).hexdigest()


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
    # The no-leading-zero hour flag is spelled differently between glibc
    # (%-I, used by the Pi) and the Windows CRT (%#I).
    hour_flag = "%#I" if os.name == "nt" else "%-I"
    return {"action": action, "timestamp": moment.isoformat(), "label": moment.strftime(f"%a {hour_flag}:%M %p")}


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
        renderer = Renderer(read_json(APP_DIR / "airlines.json", {}), str(config.get("display_color_mode", "classic")), float(config.get("receiver_heading_deg") or 148), float(config.get("fr24_radius_nm") or 6))
        payload = read_json(AIRCRAFT_PATH, {})
        aircraft = next((item for item in payload.get("aircraft", []) if aircraft_identity(item)), None)
        if mode == "remembrance":
            frame = renderer.remembrance(0.35)
        elif mode == "idle" or aircraft is None:
            if os.name == "nt":
                feed_active = bool(read_json(BRIDGE_STATE_PATH, {}).get("feed_active"))
            else:
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
            "tracked_flight_request": read_json(TRACKED_FLIGHT_REQUEST_PATH, {}),
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
                callsign = aircraft_identity(item)
                if float(item.get("seen", 999)) > 45 or not callsign:
                    continue
                row = {key: item.get(key) for key in (
                    "flight", "hex", "source", "aircraft_type", "registration", "origin_code",
                    "destination_code", "alt_baro", "gs", "track", "seen", "lat", "lon")}
                row["flight"] = callsign
                if not row.get("aircraft_type") or not row.get("registration"):
                    cached = enrichment_cache().get(f"aircraft:{str(item.get('hex') or '').lower()}") or {}
                    fill_aircraft_metadata(row, cached)
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
            config.setdefault("avoid_circling_cycles", 3)
            self.send_json({
                "services": {name: service_state(name) for name in (WINDOWS_SERVICES if os.name == "nt" else SERVICES)},
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
                "fr24_usage": fr24_usage(),
                "links": link_state(),
                "budget_forecast": budget_forecast(config, budget),
                "weather": weather,
                "tracked_flight": read_json(TRACKED_FLIGHT_STATE_PATH, {"active": False}),
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
                if isinstance(backup.get("tracked_flight_request"), dict):
                    TRACKED_FLIGHT_REQUEST_PATH.parent.mkdir(parents=True, exist_ok=True)
                    write_json(TRACKED_FLIGHT_REQUEST_PATH, backup["tracked_flight_request"])
                if os.name == "nt":
                    windows_restart(["fr24"])
                else:
                    # The bridge re-reads config.json every couple of seconds,
                    # so only the collector needs restarting for new settings.
                    subprocess.run(["systemctl", "restart", "skyhi-fr24"], check=True, timeout=20)
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
                if os.name == "nt":
                    # skyhi_pc_bridge.py already polls config.json live every few
                    # seconds; only the fr24 poller reads its settings once at
                    # startup and needs an actual restart to pick up changes.
                    windows_restart(["fr24"])
                else:
                    # The bridge re-reads config.json every couple of seconds,
                    # so only the collector needs restarting for new settings.
                    subprocess.run(["systemctl", "restart", "skyhi-fr24"], check=True, timeout=20)
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
        if self.path == "/api/tracked-flight":
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 4096)
                incoming = json.loads(self.rfile.read(length))
                action = str(incoming.get("action") or "start")
                if action == "stop":
                    previous = read_json(TRACKED_FLIGHT_REQUEST_PATH, {})
                    previous.update({"active": False, "ended_reason": "Stopped from SkyHi Control"})
                    write_json(TRACKED_FLIGHT_REQUEST_PATH, previous)
                    self.send_json({"ok": True, "active": False})
                    return
                callsign = re.sub(r"[^A-Z0-9]", "", str(incoming.get("callsign") or "").upper())[:10]
                if not 2 <= len(callsign) <= 10:
                    raise ValueError("Enter a valid 2 to 10 character callsign")
                until_landing = bool(incoming.get("until_landing"))
                duration_hours = float(incoming.get("duration_hours") or 4)
                if not .25 <= duration_hours <= 48:
                    raise ValueError("Tracking duration must be between 15 minutes and 48 hours")
                screen_seconds = int(incoming.get("screen_seconds") or 5)
                normal_seconds = int(incoming.get("normal_seconds") or 15)
                if not 3 <= screen_seconds <= 15 or not 5 <= normal_seconds <= 60:
                    raise ValueError("Screen timing is outside the supported range")
                now = time.time()
                value = {
                    "active": True, "callsign": callsign, "started_at": now,
                    "until_landing": until_landing,
                    "expires_at": None if until_landing else now + duration_hours * 3600,
                    "duration_hours": duration_hours, "screen_seconds": screen_seconds,
                    "normal_seconds": normal_seconds, "poll_seconds": 5,
                }
                TRACKED_FLIGHT_REQUEST_PATH.parent.mkdir(parents=True, exist_ok=True)
                write_json(TRACKED_FLIGHT_REQUEST_PATH, value)
                self.send_json({"ok": True, **value})
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
        # The bridge keeps running through a power-off so it can hold the panel
        # dark. Stopping it would strand the last frame on the ESP32 instead,
        # because the panel only changes when a new frame arrives.
        commands = {
            "restart-display": ["systemctl", "restart", DISPLAY_SERVICE],
            "restart-tracker": ["systemctl", "restart", "skyhi-fr24", "skyhi-flight-tracker", RECEIVER_SERVICE],
            "restart-all": ["systemctl", "restart", RECEIVER_SERVICE, "skyhi-fr24", "skyhi-flight-tracker", DISPLAY_SERVICE],
            # Keep this web service alive so the same page can turn SkyHi on.
            "turn-off": ["systemctl", "stop", "skyhi-flight-tracker", "skyhi-fr24", RECEIVER_SERVICE],
            "turn-on": ["systemctl", "start", RECEIVER_SERVICE, "skyhi-fr24", "skyhi-flight-tracker", DISPLAY_SERVICE],
        }
        if action not in commands:
            self.send_json({"ok": False, "error": "Action not allowed"}, 400)
            return
        windows_restart_names = {
            "restart-display": ["bridge"],
            "restart-tracker": ["fr24", "tracker"],
            "restart-all": ["fr24", "tracker", "bridge"],
        }
        try:
            if os.name == "nt":
                POWER_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
                if action in ("turn-off", "turn-on"):
                    write_json(POWER_STATE_PATH, {"enabled": action == "turn-on", "updated": time.time()})
                else:
                    write_json(POWER_STATE_PATH, {"enabled": True, "restart_requested": time.time(), "updated": time.time()})
                    windows_restart(windows_restart_names[action])
                self.send_json({"ok": True})
                return
            if action in ("turn-off", "turn-on"):
                write_json(POWER_STATE_PATH, {"enabled": action == "turn-on", "updated": time.time()})
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
                    write_json(POWER_STATE_PATH, {"enabled": action == "on", "updated": time.time()})
                    if os.name != "nt":
                        verb = "stop" if action == "off" else "start"
                        # Leave the bridge up when powering down so it can hold
                        # the panel dark rather than freezing the last frame.
                        services = [name for name in POWER_SERVICES
                                    if action == "on" or name != DISPLAY_SERVICE]
                        subprocess.run(["systemctl", verb, *services], check=True, timeout=25)
                    last_action = action_key
                    print(f"Automatic power {action} applied at {current}", flush=True)
        except Exception as exc:
            print(f"Automatic power schedule error: {exc}", flush=True)
        time.sleep(15)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    # Port 80 so the portal answers at http://skyhi.local with nothing after
    # it; 8080 stays listening so existing links and bookmarks still work.
    parser.add_argument("--port", type=int, action="append", dest="ports",
                        help="may be given more than once (default: 80 and 8080)")
    args = parser.parse_args()
    ports = args.ports or [80, 8080]
    new_pin = initialize_auth()
    if new_pin:
        print(f"SkyHi initial control PIN: {new_pin}", flush=True)
    threading.Thread(target=power_schedule_loop, name="power-schedule", daemon=True).start()

    servers = []
    for port in ports:
        try:
            servers.append(ThreadingHTTPServer((args.host, port), Handler))
            print(f"SkyHi control listening on {args.host}:{port}", flush=True)
        except OSError as exc:
            # Losing one port must not take the portal down: binding 80 needs
            # privilege, and something else may already hold it.
            print(f"SkyHi control could not listen on {port}: {exc}", flush=True)
    if not servers:
        raise SystemExit("No port could be bound")
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, name=f"http-{server.server_address[1]}",
                         daemon=True).start()
    servers[0].serve_forever()


if __name__ == "__main__":
    main()
