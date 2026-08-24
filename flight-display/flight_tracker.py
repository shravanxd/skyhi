#!/usr/bin/env python3
"""Track one callsign worldwide and publish a display-friendly state document."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import signal
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any
from zoneinfo import ZoneInfo

import requests

LOG = logging.getLogger("skyhi.tracked-flight")
STOP = Event()
REQUEST_PATH = Path("/home/shravanxd/.local/state/skyhi/tracked-flight-request.json")
STATE_PATH = Path("/run/skyhi-tracked-flight/state.json")
BUDGET_PATH = Path("/home/shravanxd/.local/state/skyhi/fr24-budget.json")
ADSBFI_CALLSIGN = "https://opendata.adsb.fi/api/v2/callsign/{callsign}"
FR24_URL = "https://fr24api.flightradar24.com/api/live/flight-positions/full"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
TIMEZONE_URL = "https://timeapi.io/api/timezone/coordinate"


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":"))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    value = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3440.065 * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def bounds(lat: float, lon: float, radius_nm: float = 2) -> str:
    lat_delta = radius_nm / 60
    lon_delta = radius_nm / (60 * max(.2, math.cos(math.radians(lat))))
    return f"{lat + lat_delta:.6f},{lat - lat_delta:.6f},{lon - lon_delta:.6f},{lon + lon_delta:.6f}"


def airport(session: requests.Session, code: str) -> dict[str, Any]:
    code = code.strip().upper()
    if not code:
        return {}
    code_type = "iata" if len(code) == 3 else "icao"
    try:
        response = session.get(f"https://hexdb.io/api/v1/airport/{code_type}/{code}", timeout=6)
        response.raise_for_status()
        raw = response.json()
        return {
            "code": raw.get("iata") or raw.get("icao") or code,
            "name": raw.get("airport") or code,
            "lat": raw.get("latitude"),
            "lon": raw.get("longitude"),
            "country_code": raw.get("country_code"),
            "region": raw.get("region_name"),
        }
    except Exception as exc:
        LOG.warning("Airport lookup failed for %s: %s", code, exc)
        return {"code": code, "name": code}


def route_lookup(session: requests.Session, callsign: str) -> tuple[str, str]:
    try:
        response = session.get(f"https://hexdb.io/api/v1/route/icao/{callsign}", timeout=6)
        response.raise_for_status()
        route = str(response.json().get("route") or "")
        if "-" in route:
            return tuple(part.strip().upper() for part in route.split("-", 1))  # type: ignore[return-value]
    except Exception as exc:
        LOG.warning("Route lookup failed for %s: %s", callsign, exc)
    return "", ""


def ocean_name(lat: float, lon: float) -> str:
    if -70 <= lon <= 20 and -60 <= lat <= 70:
        return "North Atlantic" if lat >= 0 else "South Atlantic"
    if 20 < lon < 120 and -65 <= lat <= 30:
        return "Indian Ocean"
    if lon > 120 or lon < -70:
        return "North Pacific" if lat >= 0 else "South Pacific"
    return "Open ocean"


def reverse_location(session: requests.Session, lat: float, lon: float) -> dict[str, str]:
    try:
        response = session.get(NOMINATIM_URL, params={
            "lat": lat, "lon": lon, "format": "jsonv2", "zoom": 10,
            "addressdetails": 1, "accept-language": "en",
        }, headers={"User-Agent": "SkyHi-flight-tracker/1.0 (github.com/shravanxd/skyhi)"}, timeout=8)
        if response.status_code == 404:
            raise ValueError("No land result")
        response.raise_for_status()
        address = response.json().get("address", {})
        city = next((address.get(key) for key in ("city", "town", "village", "municipality", "county") if address.get(key)), "")
        state = address.get("state") or address.get("region") or ""
        country = address.get("country") or ""
        primary = city or state or country
        secondary = country if primary != country else state
        if not primary:
            return {"primary": ocean_name(lat, lon), "secondary": "", "kind": "ocean"}
        return {"primary": str(primary), "secondary": str(secondary), "kind": "land"}
    except Exception:
        return {"primary": ocean_name(lat, lon), "secondary": "", "kind": "ocean"}


def timezone_name(session: requests.Session, lat: float, lon: float) -> str:
    try:
        response = session.get(TIMEZONE_URL, params={"latitude": lat, "longitude": lon}, timeout=6)
        response.raise_for_status()
        raw = response.json()
        return str(raw.get("timeZone") or raw.get("timezone") or "UTC")
    except Exception:
        # At sea, a longitude-derived Etc zone gives an honest nautical-local approximation.
        offset = max(-12, min(14, round(lon / 15)))
        return "UTC" if offset == 0 else f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}"


def local_clock(zone: str) -> str:
    try:
        return datetime.now(ZoneInfo(zone)).strftime("%-I:%M %p")
    except Exception:
        return datetime.now(timezone.utc).strftime("%-I:%M %p UTC")


def eta_from_metadata(value: Any, now: float) -> float | None:
    try:
        if isinstance(value, (int, float)):
            timestamp = float(value)
        else:
            timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        return max(0.0, timestamp - now) if timestamp > now else None
    except (TypeError, ValueError):
        return None


def normalize_adsb(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "hex": str(raw.get("hex") or "").lower(), "flight": str(raw.get("flight") or "").strip(),
        "lat": raw.get("lat"), "lon": raw.get("lon"), "alt_baro": raw.get("alt_baro"),
        "alt_geom": raw.get("alt_geom"), "gs": raw.get("gs"), "track": raw.get("track"),
        "baro_rate": raw.get("baro_rate"), "seen": raw.get("seen", 0),
        "aircraft_type": raw.get("t"), "registration": raw.get("r"), "source": "adsb.fi",
    }


def normalize_fr24(raw: dict[str, Any]) -> dict[str, Any]:
    """Translate an FR24 full-position row into the tracker aircraft schema."""
    return {
        "hex": str(raw.get("hex") or "").lower(),
        "flight": str(raw.get("callsign") or raw.get("flight") or "").strip(),
        "lat": raw.get("lat"), "lon": raw.get("lon"),
        "alt_baro": raw.get("alt"), "alt_geom": raw.get("alt"),
        "gs": raw.get("gspeed"), "track": raw.get("track"),
        "baro_rate": raw.get("vspeed"), "seen": 0,
        "aircraft_type": raw.get("type"), "registration": raw.get("reg"),
        "source": "fr24",
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    session = requests.Session()
    token = os.environ.get("FR24_API_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "Accept-Version": "v1"}
    current_id = ""
    aircraft: dict[str, Any] = {}
    metadata: dict[str, Any] = {}
    origin: dict[str, Any] = {}
    destination: dict[str, Any] = {}
    location: dict[str, str] = {}
    zone = "UTC"
    next_poll = next_geo = 0.0
    next_metadata = 0.0
    geo_lat = geo_lon = None
    missing_since = 0.0
    ground_count = 0
    last_status = "SEARCHING"
    last_fresh = 0.0

    while not STOP.is_set():
        now = time.time()
        request = read_json(REQUEST_PATH, {})
        callsign = re.sub(r"[^A-Z0-9]", "", str(request.get("callsign") or "").upper())[:10]
        active = bool(request.get("active") and callsign)
        expires = request.get("expires_at")
        if active and expires and now >= float(expires):
            active = False
            request["active"] = False
            request["ended_reason"] = "Tracking time complete"
            atomic_json(REQUEST_PATH, request)
        if not active:
            atomic_json(STATE_PATH, {"active": False, "callsign": callsign,
                                    "ended_reason": request.get("ended_reason"), "updated": now})
            STOP.wait(1)
            continue
        if callsign != current_id:
            current_id, aircraft, metadata, origin, destination, location = callsign, {}, {}, {}, {}, {}
            next_poll = next_geo = next_metadata = 0
            geo_lat = geo_lon = None
            missing_since = ground_count = 0
            last_status, last_fresh = "SEARCHING", 0
            LOG.info("Tracking requested for %s", callsign)

        if now >= next_poll:
            try:
                response = session.get(ADSBFI_CALLSIGN.format(callsign=callsign),
                                       headers={"User-Agent": "SkyHi/1.0", "Accept": "application/json"}, timeout=8)
                response.raise_for_status()
                rows = response.json().get("ac", [])
                exact = next((row for row in rows if str(row.get("flight") or "").strip().upper() == callsign), rows[0] if rows else None)
                if exact:
                    aircraft = normalize_adsb(exact)
                    last_fresh = now
                    missing_since = 0
                elif not missing_since:
                    missing_since = now
            except Exception as exc:
                LOG.warning("adsb.fi callsign poll failed for %s: %s", callsign, exc)
                missing_since = missing_since or now
            next_poll = now + max(2, float(request.get("poll_seconds", 5)))

        if not metadata and now >= next_metadata:
            if token:
                try:
                    params = {"callsigns": callsign, "limit": 1}
                    if aircraft.get("lat") is not None and aircraft.get("lon") is not None:
                        params["bounds"] = bounds(float(aircraft["lat"]), float(aircraft["lon"]))
                    response = session.get(FR24_URL, headers=headers, params=params, timeout=8)
                    response.raise_for_status()
                    row = (response.json().get("data") or [{}])[0]
                    if row and not aircraft:
                        aircraft = normalize_fr24(row)
                        last_fresh = now
                        missing_since = 0
                    metadata = {
                        "origin_code": row.get("orig_iata") or row.get("orig_icao"),
                        "destination_code": row.get("dest_iata") or row.get("dest_icao"),
                        "aircraft_type": row.get("type"), "registration": row.get("reg"),
                        "airline": row.get("operating_as") or row.get("painted_as"),
                        "eta": row.get("eta") or row.get("estimated_arrival") or row.get("timestamp_arrival"),
                    } if row else {}
                    budget = read_json(BUDGET_PATH, {})
                    today = datetime.now().astimezone().date().isoformat()
                    if budget.get("date") != today:
                        budget = {"date": today, "credits": 0, "calls": 0,
                                  "lifetime_credits": int(budget.get("lifetime_credits", 0)) + int(budget.get("credits", 0)),
                                  "lifetime_calls": int(budget.get("lifetime_calls", 0)) + int(budget.get("calls", 0))}
                    budget["credits"] = int(budget.get("credits", 0)) + (8 if row else 1)
                    budget["calls"] = int(budget.get("calls", 0)) + 1
                    budget["last_success"] = now
                    atomic_json(BUDGET_PATH, budget)
                except Exception as exc:
                    LOG.warning("FR24 enrichment failed for %s: %s", callsign, exc)
            if not metadata.get("origin_code") or not metadata.get("destination_code"):
                route_origin, route_destination = route_lookup(session, callsign)
                metadata["origin_code"] = metadata.get("origin_code") or route_origin
                metadata["destination_code"] = metadata.get("destination_code") or route_destination
            origin = airport(session, str(metadata.get("origin_code") or ""))
            destination = airport(session, str(metadata.get("destination_code") or ""))
            # Retry missing route data occasionally, never on every render loop.
            next_metadata = now + (21600 if metadata.get("origin_code") and metadata.get("destination_code") else 300)

        lat, lon = aircraft.get("lat"), aircraft.get("lon")
        if lat is not None and lon is not None:
            moved = geo_lat is None or distance_nm(float(geo_lat), float(geo_lon), float(lat), float(lon)) >= 25
            if now >= next_geo and moved:
                location = reverse_location(session, float(lat), float(lon))
                zone = timezone_name(session, float(lat), float(lon))
                geo_lat, geo_lon, next_geo = float(lat), float(lon), now + 60

        progress = None
        remaining_nm = None
        if lat is not None and origin.get("lat") is not None and destination.get("lat") is not None:
            total_nm = distance_nm(float(origin["lat"]), float(origin["lon"]), float(destination["lat"]), float(destination["lon"]))
            remaining_nm = distance_nm(float(lat), float(lon), float(destination["lat"]), float(destination["lon"]))
            progress = max(0.0, min(1.0, 1 - remaining_nm / max(1, total_nm)))
        speed = aircraft.get("gs")
        calculated_eta = remaining_nm / max(100, float(speed)) * 3600 if remaining_nm is not None and speed else None
        eta_seconds = eta_from_metadata(metadata.get("eta"), now) or calculated_eta
        altitude = aircraft.get("alt_baro", aircraft.get("alt_geom"))
        vertical = float(aircraft.get("baro_rate") or 0)
        on_ground = altitude == "ground" or (isinstance(altitude, (int, float)) and altitude < 250 and float(speed or 0) < 55)
        ground_count = ground_count + 1 if on_ground else 0
        if ground_count >= 3 or (last_status == "ARRIVING" and missing_since and now - missing_since > 600):
            status = "LANDED"
        elif not aircraft or (missing_since and now - missing_since > 30):
            status = "SEARCHING"
        elif remaining_nm is not None and remaining_nm < 60 or vertical < -500:
            status = "ARRIVING"
        elif progress is not None and progress < .08 or vertical > 500:
            status = "DEPARTING"
        else:
            status = "EN ROUTE"
        last_status = status

        state = {
            "active": True, "callsign": callsign, "started_at": request.get("started_at"),
            "until_landing": bool(request.get("until_landing")), "expires_at": expires,
            "screen_seconds": request.get("screen_seconds", 5), "normal_seconds": request.get("normal_seconds", 15),
            "status": status, "progress": progress, "remaining_nm": remaining_nm,
            "eta_seconds": eta_seconds, "eta_at": now + eta_seconds if eta_seconds else None,
            "aircraft": {**aircraft, **{key: value for key, value in metadata.items() if value}},
            "origin": origin, "destination": destination, "location": location,
            "timezone": zone, "local_time": local_clock(zone), "last_seen": last_fresh or None,
            "missing_seconds": now - missing_since if missing_since else 0, "updated": now,
        }
        atomic_json(STATE_PATH, state)
        if status == "LANDED" and request.get("until_landing"):
            request["active"] = False
            request["ended_reason"] = f"{callsign} landed"
            atomic_json(REQUEST_PATH, request)
            LOG.info("%s landed; tracking complete", callsign)
        STOP.wait(1)
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    raise SystemExit(main())
