#!/usr/bin/env python3
"""Record every aircraft that passes the receiver into Supabase.

One row per pass, with the identity, route, full telemetry envelope and
reception quality, plus the individual position samples for that pass.

Nothing is logged from the display path, so a network outage or a Supabase
problem can never slow the panel down: finished sightings are appended to a
local spool file first and uploaded afterwards, and the spool is replayed on
the next attempt.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import signal
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any

import requests

from fr24_poller import point_in_polygon

LOG = logging.getLogger("skyhi.logger")
STOP = Event()
APP_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("SKYHI_STATE_DIR", "/home/shravanxd/.local/state/skyhi"))
SPOOL_PATH = STATE_DIR / "supabase-spool.jsonl"
# A clean restart (every deploy tonight) used to finalize whatever was mid-pass
# and start it fresh on the next tick, silently splitting one flight into two
# logged sightings. This carries the open state across a restart instead.
RECOVERY_PATH = STATE_DIR / "logger-recovery.json"
ENRICHMENT_DB = APP_DIR / "cache" / "enrichment.sqlite3"

# An airline flying a published schedule uses an ICAO prefix and a flight
# number. Military, business and general aviation fly as their registration or
# a block callsign, and have no route to record.
SCHEDULED_CALLSIGN = re.compile(r"^[A-Z]{3}\d{1,4}[A-Z]?$")

# A pass ends when the aircraft has been gone this long; seeing it again later
# is recorded as a separate sighting rather than extending the old one.
PASS_GAP_SECONDS = 120.0
# Position samples are thinned: the panel updates twice a second, but that
# resolution is not worth storing for hours of traffic.
SAMPLE_INTERVAL_SECONDS = 10.0
MAX_SAMPLES_PER_PASS = 360
# Keep the spool bounded so a long outage cannot fill the SD card.
MAX_SPOOL_BYTES = 32 * 1024 * 1024
UPLOAD_BATCH = 20


def utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3440.065 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    return (math.degrees(math.atan2(math.sin(dl) * math.cos(p2),
            math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) + 360) % 360


def angular_difference(left: float, right: float) -> float:
    return abs((left - right + 180) % 360 - 180)


def number(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def integer(value: Any) -> int | None:
    parsed = number(value)
    return None if parsed is None else int(round(parsed))


def airport_names() -> dict[str, str]:
    """Airport names already cached by the display, read without re-fetching."""
    names: dict[str, str] = {}
    try:
        with sqlite3.connect(f"file:{ENRICHMENT_DB}?mode=ro", uri=True, timeout=3) as db:
            for key, value in db.execute("SELECT key, value FROM enrichment WHERE key LIKE 'airport:%'"):
                try:
                    names[key.split(":", 1)[1]] = json.loads(value).get("airport_name") or ""
                except (ValueError, IndexError):
                    continue
    except sqlite3.Error:
        pass
    return names


class Sighting:
    """Accumulates one pass of one aircraft."""

    def __init__(self, hex_code: str, now: float):
        self.hex = hex_code
        self.first_seen = now
        self.last_seen = now
        self.last_sample = 0.0
        self.samples: list[dict[str, Any]] = []
        self.latest: dict[str, Any] = {}
        self.extras: dict[str, Any] = {}
        self.sources: set[str] = set()
        self.rssi_values: list[float] = []
        self.altitudes: list[int] = []
        self.speeds: list[float] = []
        self.climb_rates: list[int] = []
        self.closest_nm: float | None = None
        self.closest: dict[str, Any] = {}
        self.entered_view = False
        self.entered_polygon: bool | None = None
        self.received_locally = False
        self.observations = 0

    def to_dict(self) -> dict[str, Any]:
        """Everything needed to keep counting exactly where this left off."""
        state = dict(vars(self))
        state["sources"] = sorted(self.sources)
        return state

    @classmethod
    def from_dict(cls, state: dict[str, Any]) -> "Sighting":
        sighting = cls(state["hex"], state["first_seen"])
        sighting.__dict__.update(state)
        sighting.sources = set(state.get("sources") or [])
        return sighting

    def update(self, item: dict[str, Any], extras: dict[str, Any], now: float,
               receiver: dict[str, Any]) -> None:
        self.last_seen = now
        self.latest = item
        if extras:
            self.extras.update(extras)
        self.observations += 1
        source = str(item.get("source") or "")
        if source:
            self.sources.add(source)
        if "local" in source:
            self.received_locally = True

        rssi = number(item.get("rssi"))
        if rssi is not None and rssi > -100:
            self.rssi_values.append(rssi)
        altitude = integer(item.get("alt_baro") if item.get("alt_baro") != "ground" else 0)
        if altitude is not None:
            self.altitudes.append(altitude)
        speed = number(item.get("gs"))
        if speed is not None:
            self.speeds.append(speed)
        climb = integer(item.get("baro_rate"))
        if climb is not None:
            self.climb_rates.append(climb)

        lat, lon = number(item.get("lat")), number(item.get("lon"))
        distance = bearing = None
        if lat is not None and lon is not None and receiver.get("lat") is not None:
            distance = haversine_nm(receiver["lat"], receiver["lon"], lat, lon)
            bearing = bearing_deg(receiver["lat"], receiver["lon"], lat, lon)
            if receiver.get("heading") is not None:
                if angular_difference(bearing, receiver["heading"]) <= receiver["half_fov"]:
                    self.entered_view = True
            if self.closest_nm is None or distance < self.closest_nm:
                self.closest_nm = distance
                self.closest = {
                    "at": now, "bearing": bearing, "altitude": altitude,
                    "speed": speed, "track": number(item.get("track")),
                }

        if now - self.last_sample >= SAMPLE_INTERVAL_SECONDS and len(self.samples) < MAX_SAMPLES_PER_PASS:
            self.last_sample = now
            self.samples.append({
                "recorded_at": utc(now),
                "lat": lat, "lon": lon,
                "alt_baro": altitude,
                "alt_geom": integer(item.get("alt_geom")),
                "gs": speed,
                "track": number(item.get("track")),
                "baro_rate": climb,
                "geom_rate": integer(extras.get("geom_rate")),
                "distance_nm": round(distance, 2) if distance is not None else None,
                "bearing_deg": round(bearing, 2) if bearing is not None else None,
                "rssi": rssi,
                "squawk": item.get("squawk"),
                "source": source or None,
            })

    def vertical_trend(self) -> str | None:
        if not self.climb_rates:
            return None
        average = sum(self.climb_rates) / len(self.climb_rates)
        if average > 250:
            return "climbing"
        if average < -250:
            return "descending"
        return "level"

    def finalize(self, context: dict[str, Any]) -> dict[str, Any]:
        item, extras = self.latest, self.extras
        callsign = str(item.get("flight") or "").strip() or None
        operator_icao = callsign[:3] if callsign and SCHEDULED_CALLSIGN.match(callsign) else None
        airline = (context.get("airlines") or {}).get(operator_icao or "", [])
        names = context.get("airport_names") or {}
        origin = item.get("origin_code")
        destination = item.get("destination_code")

        sighting = {
            "hex": self.hex,
            "callsign": callsign,
            "registration": item.get("registration") or extras.get("registration"),
            "aircraft_type": item.get("aircraft_type"),
            "aircraft_type_label": context["type_label"](item.get("aircraft_type")),
            "category": extras.get("category"),
            "operator_icao": operator_icao,
            "operator_iata": airline[1] if len(airline) > 1 else None,
            "operator_name": airline[0] if airline else None,
            # Which source claimed the route. Only FR24 reports the actual
            # flight; anything else would be a guess from a schedule table, and
            # recording that distinction is what makes the history trustworthy
            # when it is read back later.
            "route_source": "fr24" if (origin or destination) else None,
            "origin_code": origin,
            "origin_name": names.get(str(origin or "").upper()) or None,
            "destination_code": destination,
            "destination_name": names.get(str(destination or "").upper()) or None,
            "is_scheduled": bool(operator_icao),
            "first_seen_at": utc(self.first_seen),
            "last_seen_at": utc(self.last_seen),
            "duration_seconds": round(self.last_seen - self.first_seen, 1),
            "sample_count": len(self.samples),
            "closest_distance_nm": round(self.closest_nm, 2) if self.closest_nm is not None else None,
            "closest_at": utc(self.closest["at"]) if self.closest.get("at") else None,
            "closest_bearing_deg": round(self.closest["bearing"], 2) if self.closest.get("bearing") is not None else None,
            "closest_altitude_ft": self.closest.get("altitude"),
            "closest_speed_kt": self.closest.get("speed"),
            "closest_track_deg": self.closest.get("track"),
            "max_altitude_ft": max(self.altitudes) if self.altitudes else None,
            "min_altitude_ft": min(self.altitudes) if self.altitudes else None,
            "max_speed_kt": max(self.speeds) if self.speeds else None,
            "max_climb_rate_fpm": max(self.climb_rates) if self.climb_rates else None,
            "max_descent_rate_fpm": min(self.climb_rates) if self.climb_rates else None,
            "vertical_trend": self.vertical_trend(),
            "entered_window_view": self.entered_view,
            "entered_polygon": self.entered_polygon,
            "best_rssi": max(self.rssi_values) if self.rssi_values else None,
            "avg_rssi": round(sum(self.rssi_values) / len(self.rssi_values), 2) if self.rssi_values else None,
            "total_messages": integer(item.get("messages")),
            "sources": sorted(self.sources) or None,
            "position_source": item.get("position_source"),
            "received_locally": self.received_locally,
            "mlat": bool(extras.get("mlat")) if extras.get("mlat") is not None else None,
            "tisb": bool(extras.get("tisb")) if extras.get("tisb") is not None else None,
            "squawk": item.get("squawk"),
            "emergency": extras.get("emergency"),
            "alert": bool(extras["alert"]) if extras.get("alert") is not None else None,
            "spi": bool(extras["spi"]) if extras.get("spi") is not None else None,
            "nav_altitude_mcp": integer(extras.get("nav_altitude_mcp")),
            "nav_heading": number(extras.get("nav_heading")),
            "nav_qnh": number(extras.get("nav_qnh")),
            "nic": integer(extras.get("nic")),
            "nac_p": integer(extras.get("nac_p")),
            "nac_v": integer(extras.get("nac_v")),
            "sil": integer(extras.get("sil")),
            "sil_type": extras.get("sil_type"),
            "gva": integer(extras.get("gva")),
            "sda": integer(extras.get("sda")),
            "rc": integer(extras.get("rc")),
            "adsb_version": integer(extras.get("version")),
            "weather": context.get("weather") or None,
            "receiver": context.get("receiver_meta") or None,
            # Both sides of the pass are kept: what FR24 said about the flight
            # and what the aerial actually heard, so a callsign can be tied to
            # the route it really flew on a given day.
            "raw": {"merged": item, "receiver_extras": extras,
                    "observations": self.observations,
                    "sources_seen": sorted(self.sources)},
        }
        return {"sighting": sighting, "positions": self.samples}


def spool_append(entries: list[dict[str, Any]]) -> None:
    if not entries:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if SPOOL_PATH.exists() and SPOOL_PATH.stat().st_size > MAX_SPOOL_BYTES:
            LOG.warning("Spool is over %d MB; dropping the oldest half", MAX_SPOOL_BYTES // (1024 * 1024))
            lines = SPOOL_PATH.read_text(encoding="utf-8").splitlines()
            SPOOL_PATH.write_text("\n".join(lines[len(lines) // 2:]) + "\n", encoding="utf-8")
    except OSError:
        pass
    with SPOOL_PATH.open("a", encoding="utf-8") as stream:
        for entry in entries:
            stream.write(json.dumps(entry, separators=(",", ":")) + "\n")


def spool_rewrite(remaining: list[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=STATE_DIR, prefix="spool-")
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        for line in remaining:
            stream.write(line + "\n")
    os.replace(temporary, SPOOL_PATH)


def upload(session: requests.Session, base: str, key: str) -> tuple[int, int]:
    """Send spooled sightings. Returns (uploaded, still pending)."""
    if not SPOOL_PATH.exists():
        return 0, 0
    try:
        lines = [line for line in SPOOL_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return 0, 0
    if not lines:
        return 0, 0

    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation,resolution=merge-duplicates",
    }
    uploaded, failed = 0, []
    for index, line in enumerate(lines):
        if index >= UPLOAD_BATCH:
            failed.extend(lines[index:])
            break
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # unreadable line, drop it rather than retry forever
        try:
            sighting = entry["sighting"]
            response = session.post(f"{base}/rest/v1/flight_sightings", headers=headers,
                                    data=json.dumps([sighting]), timeout=20)
            # A column this build knows about may not exist in the database yet.
            # Dropping the unknown field and retrying keeps the pass rather than
            # losing it to a schema that is merely behind.
            if response.status_code == 400 and "PGRST204" in response.text:
                unknown = re.findall(r"'([^']+)' column", response.text)
                if unknown and any(field in sighting for field in unknown):
                    for field in unknown:
                        sighting.pop(field, None)
                    LOG.warning("Database is missing %s; sent without it", ", ".join(unknown))
                    response = session.post(f"{base}/rest/v1/flight_sightings", headers=headers,
                                            data=json.dumps([sighting]), timeout=20)
            if response.status_code == 409:
                uploaded += 1  # already recorded
                continue
            response.raise_for_status()
            rows = response.json()
            sighting_id = rows[0]["id"] if rows else None
            positions = entry.get("positions") or []
            if sighting_id and positions:
                for position in positions:
                    position["sighting_id"] = sighting_id
                session.post(f"{base}/rest/v1/flight_positions",
                             headers={**headers, "Prefer": "return=minimal"},
                             data=json.dumps(positions), timeout=30).raise_for_status()
            uploaded += 1
        except requests.RequestException as exc:
            LOG.warning("Upload failed, keeping %d spooled: %s", len(lines) - index, exc)
            failed.extend(lines[index:])
            break
    spool_rewrite(failed)
    return uploaded, len(failed)


def save_recovery(active: dict[str, "Sighting"]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=STATE_DIR, prefix="recovery-")
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump({hex_code: s.to_dict() for hex_code, s in active.items()}, stream)
    os.replace(temporary, RECOVERY_PATH)


def restore_recovery(pass_gap_seconds: float) -> tuple[dict[str, "Sighting"], list["Sighting"]]:
    """Split a saved recovery file into what can resume and what cannot.

    Uses the same gap the main loop already treats as "still the same pass",
    so a quick restart is invisible and a long one still gets logged rather
    than lost, just later than it would have been.
    """
    saved = read_json(RECOVERY_PATH, {})
    if RECOVERY_PATH.exists():
        RECOVERY_PATH.unlink()
    resumed, expired = {}, []
    now = time.time()
    for hex_code, state in saved.items():
        sighting = Sighting.from_dict(state)
        if now - sighting.last_seen <= pass_gap_seconds:
            resumed[hex_code] = sighting
        else:
            expired.append(sighting)
    return resumed, expired


def run(config_path: Path, base: str, key: str, receiver_json: Path, merged_json: Path,
        weather_json: Path, interval: float) -> int:
    from skyhi_display import friendly_aircraft_type

    config = read_json(config_path, {})
    airlines = read_json(APP_DIR / "airlines.json", {})
    receiver = {
        "lat": number(config.get("receiver_lat")),
        "lon": number(config.get("receiver_lon")),
        "heading": number(config.get("receiver_heading_deg")),
        "half_fov": (number(config.get("window_field_of_view_deg")) or 120) / 2,
    }
    receiver_meta = {
        "lat": receiver["lat"], "lon": receiver["lon"],
        "heading_deg": receiver["heading"],
        "field_of_view_deg": number(config.get("window_field_of_view_deg")),
    }
    polygon = config.get("tracking_polygon") or []
    session = requests.Session()
    active: dict[str, Sighting] = {}
    resumed, expired = restore_recovery(PASS_GAP_SECONDS)
    active.update(resumed)
    if resumed:
        LOG.info("Resumed %d pass(es) across a restart", len(resumed))
    if expired:
        spool_append([s.finalize({
            "airlines": load_json(APP_DIR / "airlines.json", {}), "airport_names": airport_names(),
            "weather": read_json(weather_json, {}), "receiver_meta": receiver_meta,
            "type_label": friendly_aircraft_type,
        }) for s in expired])
        LOG.info("Spooled %d pass(es) too old to resume", len(expired))
    names = airport_names()
    next_names_refresh = time.time() + 900
    next_upload = 0.0
    LOG.info("Logging flights to %s", base)

    while not STOP.is_set():
        now = time.time()
        merged = read_json(merged_json, {})
        local = read_json(receiver_json, {})
        extras_by_hex = {str(a.get("hex", "")).lower(): a for a in local.get("aircraft", [])}
        weather = read_json(weather_json, {})

        seen_now = set()
        for item in merged.get("aircraft", []):
            hex_code = str(item.get("hex") or "").lower()
            if not hex_code:
                continue
            seen_now.add(hex_code)
            sighting = active.get(hex_code)
            if sighting is None or now - sighting.last_seen > PASS_GAP_SECONDS:
                if sighting is not None:
                    spool_append([sighting.finalize({
                        "airlines": airlines, "airport_names": names, "weather": weather,
                        "receiver_meta": receiver_meta, "type_label": friendly_aircraft_type,
                    })])
                sighting = Sighting(hex_code, now)
                active[hex_code] = sighting
            sighting.update(item, extras_by_hex.get(hex_code, {}), now, receiver)
            if polygon and sighting.entered_polygon is not True:
                lat, lon = number(item.get("lat")), number(item.get("lon"))
                if lat is not None and lon is not None:
                    if point_in_polygon(lat, lon, polygon):
                        sighting.entered_polygon = True
                    elif sighting.entered_polygon is None:
                        sighting.entered_polygon = False

        finished = [h for h, s in active.items() if h not in seen_now and now - s.last_seen > PASS_GAP_SECONDS]
        if finished:
            batch = []
            for hex_code in finished:
                sighting = active.pop(hex_code)
                batch.append(sighting.finalize({
                    "airlines": airlines, "airport_names": names, "weather": weather,
                    "receiver_meta": receiver_meta, "type_label": friendly_aircraft_type,
                }))
            spool_append(batch)
            LOG.info("Recorded %d completed %s", len(batch), "pass" if len(batch) == 1 else "passes")

        if now >= next_upload:
            uploaded, pending = upload(session, base, key)
            if uploaded:
                LOG.info("Uploaded %d sighting(s), %d still spooled", uploaded, pending)
            next_upload = now + (10.0 if pending else 30.0)

        if now >= next_names_refresh:
            names = airport_names()
            next_names_refresh = now + 900

        STOP.wait(interval)

    # Persisted rather than finalized: a clean restart (every deploy) used to
    # close whatever was mid-pass and start it fresh on the next tick, silently
    # splitting one flight into two logged sightings. Startup decides whether
    # to resume or finalize, based on how long the process was actually down.
    if active:
        save_recovery(active)
        LOG.info("Saved %d open pass(es) for the next start", len(active))
    upload(session, base, key)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=APP_DIR / "config.json")
    parser.add_argument("--merged-json", type=Path,
                        default=Path(os.environ.get("SKYHI_MERGED_JSON", "/run/skyhi-fr24/aircraft.json")))
    parser.add_argument("--receiver-json", type=Path,
                        default=Path(os.environ.get("SKYHI_RECEIVER_JSON", "/run/readsb/aircraft.json")))
    parser.add_argument("--weather-json", type=Path, default=Path("/run/skyhi-weather.json"))
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    base = (os.environ.get("SKYHI_SUPABASE_URL") or "").rstrip("/")
    key = os.environ.get("SKYHI_SUPABASE_KEY") or ""
    if not base or not key:
        raise SystemExit("SKYHI_SUPABASE_URL and SKYHI_SUPABASE_KEY must be set")
    return run(args.config, base, key, args.receiver_json, args.merged_json,
               args.weather_json, args.interval)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    raise SystemExit(main())
