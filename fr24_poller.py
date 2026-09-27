#!/usr/bin/env python3
"""Merge local ADS-B, adsb.fi network traffic, and cached FR24 metadata."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any

import requests

LOG = logging.getLogger("skyhi.fr24")
STOP = Event()
APP_DIR = Path(__file__).resolve().parent
WINDOWS_RUNTIME = APP_DIR / "cache" / "runtime"
# Shared with the control portal and tracker; see control_panel.py.
STATE_DIR = Path(os.environ.get("SKYHI_STATE_DIR", "/home/shravanxd/.local/state/skyhi"))
URL = "https://fr24api.flightradar24.com/api/live/flight-positions/full"
ADSBFI_URL = "https://opendata.adsb.fi/api/v3/lat/{lat}/lon/{lon}/dist/{dist}"
# Credits are charged per record returned, so the batch is capped rather than
# unbounded, and repeated misses are spaced out instead of retried every pass.
MAX_ENRICHMENT_BATCH = 10
ENRICHMENT_MIN_INTERVAL = 3.0



def feeding_adsbfi() -> bool | None:
    """Whether the receiver's feed to adsb.fi is actually established.

    readsb does not publish its outbound connector state, so the socket is the
    honest signal: configured is not the same as connected. Checked here once
    and published, so the panel and the portal read the same answer instead of
    each shelling out for it.
    """
    try:
        result = subprocess.run(["ss", "-tn", "state", "established"],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return ":30004" in result.stdout and "65.109." in result.stdout


def load_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def bounds(lat: float, lon: float, radius_nm: float) -> str:
    lat_delta = radius_nm / 60.0
    lon_delta = radius_nm / (60.0 * max(0.2, math.cos(math.radians(lat))))
    return f"{lat + lat_delta:.6f},{lat - lat_delta:.6f},{lon - lon_delta:.6f},{lon + lon_delta:.6f}"


def age_seconds(timestamp: Any) -> float:
    try:
        stamp = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())
    except (TypeError, ValueError):
        return 0.0


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    callsign = raw.get("callsign") or raw.get("flight")
    return {
        "hex": (raw.get("hex") or raw.get("fr24_id") or "").lower(),
        "flight": callsign,
        "lat": raw.get("lat"),
        "lon": raw.get("lon"),
        "alt_baro": raw.get("alt"),
        "gs": raw.get("gspeed"),
        "track": raw.get("track"),
        "baro_rate": raw.get("vspeed"),
        "squawk": raw.get("squawk"),
        "seen": age_seconds(raw.get("timestamp")),
        "rssi": -100,
        "messages": 1,
        "fr24_id": raw.get("fr24_id"),
        "aircraft_type": raw.get("type"),
        "registration": raw.get("reg"),
        "origin_code": raw.get("orig_iata") or raw.get("orig_icao"),
        "destination_code": raw.get("dest_iata") or raw.get("dest_icao"),
        "painted_as": raw.get("painted_as"),
        "operating_as": raw.get("operating_as"),
        "source": "fr24",
        "position_source": raw.get("source"),
    }


def normalize_adsbfi(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize adsb.fi's readsb-compatible record for the display pipeline."""
    return {
        "hex": str(raw.get("hex") or "").lower(),
        "flight": raw.get("flight"),
        "lat": raw.get("lat"),
        "lon": raw.get("lon"),
        "alt_baro": raw.get("alt_baro"),
        "alt_geom": raw.get("alt_geom"),
        "gs": raw.get("gs"),
        "track": raw.get("track"),
        "baro_rate": raw.get("baro_rate"),
        "squawk": raw.get("squawk"),
        "seen": raw.get("seen", 0),
        "seen_pos": raw.get("seen_pos"),
        "rssi": raw.get("rssi", -100),
        "messages": raw.get("messages", 1),
        "aircraft_type": raw.get("t"),
        "registration": raw.get("r"),
        "source": "adsb.fi",
        "position_source": raw.get("type"),
    }


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix="aircraft-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":"))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3440.065 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    return (math.degrees(math.atan2(math.sin(dl) * math.cos(p2),
            math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) + 360) % 360


def point_in_polygon(lat: float, lon: float, polygon: list[list[float]]) -> bool:
    """Ray-casting test for [latitude, longitude] polygon vertices."""
    inside = False
    previous = polygon[-1]
    for current in polygon:
        y1, x1 = previous
        y2, x2 = current
        if (y1 > lat) != (y2 > lat):
            crossing = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lon < crossing:
                inside = not inside
        previous = current
    return inside


def angular_difference(left: float, right: float) -> float:
    return abs((left - right + 180) % 360 - 180)


def enrichment_candidates(local: dict[str, Any], lat: float, lon: float,
                          config: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """The aircraft the panel would put on screen, in the order it prefers them.

    This deliberately mirrors skyhi_display.select_aircraft rather than using a
    radius of its own. Selection has no distance cap, so an aircraft twenty
    miles out still reaches the panel; enriching only a small circle around the
    receiver left those flights showing no route at all.
    """
    heading = config.get("receiver_heading_deg")
    half_fov = float(config.get("window_field_of_view_deg", 120)) / 2
    polygon = config.get("tracking_polygon") or []
    candidates = []
    for item in local.get("aircraft", []):
        source = str(item.get("source") or "")
        local_source = source in ("local", "local+fr24", "local+adsb.fi")
        freshness = (float(config.get("local_max_seen_seconds", 12)) if local_source
                     else float(config.get("max_seen_seconds", 45)))
        if float(item.get("seen", 999)) > freshness:
            continue
        if not str(item.get("flight") or "").strip():
            continue
        if item.get("lat") is None or item.get("lon") is None:
            continue
        if polygon and not point_in_polygon(float(item["lat"]), float(item["lon"]), polygon):
            continue
        distance = distance_nm(lat, lon, float(item["lat"]), float(item["lon"]))
        in_view = True
        if heading is not None:
            bearing = bearing_deg(lat, lon, float(item["lat"]), float(item["lon"]))
            in_view = angular_difference(bearing, float(heading)) <= half_fov
        candidates.append((not in_view, distance, item))
    candidates.sort(key=lambda row: (row[0], row[1]))
    return [item for _, _, item in candidates[:limit]]


def active_local_target(local: dict[str, Any], lat: float, lon: float, trigger_nm: float,
                        polygon: list[list[float]] | None = None) -> dict[str, Any] | None:
    """Nearest aircraft actually close by, used to speed up network polling."""
    nearest = None
    for item in local.get("aircraft", []):
        if float(item.get("seen", 999)) > 15 or item.get("lat") is None or item.get("lon") is None:
            continue
        if not str(item.get("flight") or "").strip():
            continue
        distance = distance_nm(lat, lon, float(item["lat"]), float(item["lon"]))
        eligible = (point_in_polygon(float(item["lat"]), float(item["lon"]), polygon)
                    if polygon else distance <= trigger_nm)
        if eligible and (nearest is None or distance < nearest[0]):
            nearest = (distance, item)
    return nearest[1] if nearest else None


def load_budget(path: Path) -> dict[str, Any]:
    today = datetime.now().astimezone().date().isoformat()
    value = load_json(path)
    if value.get("date") != today:
        return {
            "date": today,
            "credits": 0,
            "calls": 0,
            "lifetime_credits": int(value.get("lifetime_credits", 0)) + int(value.get("credits", 0)),
            "lifetime_calls": int(value.get("lifetime_calls", 0)) + int(value.get("calls", 0)),
        }
    value.setdefault("lifetime_credits", 0)
    value.setdefault("lifetime_calls", 0)
    return value


def any_first_contact(wanted: dict[str, str], metadata: dict[str, Any]) -> bool:
    """Whether any wanted callsign has never been asked about at all.

    Distinct from a stale cache entry: this aircraft has zero record in
    metadata, not an expired one. The minimum-interval spacing between
    enrichment calls exists to stop refreshing the same aircraft repeatedly;
    it should never delay the one lookup that actually needs to be fast, the
    first, since that is the moment the aircraft is on the panel with nothing
    known about it at all.
    """
    return any(callsign not in metadata for callsign in wanted)


def merge_hybrid(local: dict[str, Any], network: list[dict[str, Any]], metadata: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Merge on ICAO hex, preferring fast local movement and cached metadata."""
    metadata = metadata or {}
    merged: dict[str, Any] = {}
    for item in network:
        if not item.get("hex"):
            continue
        key = str(item["hex"]).lower()
        base = dict(item)
        callsign = str(item.get("flight") or "").strip()
        # A network-only aircraft (the norm whenever no local receiver has it,
        # e.g. every aircraft on the Windows/no-dump1090 bridge) still deserves
        # its cached FR24 metadata; previously only aircraft that also matched
        # a local record picked this up.
        cached = metadata.get(callsign) or metadata.get(key) or {}
        cached_item = cached.get("item", {}) if isinstance(cached, dict) else {}
        for field in ("aircraft_type", "registration", "origin_code", "destination_code", "painted_as", "operating_as"):
            if cached_item.get(field) is not None:
                base[field] = cached_item[field]
        merged[key] = base
    callsigns = {str(item.get("flight") or "").strip(): key for key, item in merged.items() if item.get("flight")}
    for local_item in local.get("aircraft", []):
        hex_code = str(local_item.get("hex", "")).lower()
        callsign = str(local_item.get("flight") or "").strip()
        key = hex_code if hex_code in merged else callsigns.get(callsign, hex_code)
        already_network = key in merged
        base = merged.get(key, {})
        cached = metadata.get(callsign) or metadata.get(hex_code) or {}
        cached_item = cached.get("item", {}) if isinstance(cached, dict) else {}
        for field in ("aircraft_type", "registration", "origin_code", "destination_code", "painted_as", "operating_as"):
            if cached_item.get(field) is not None:
                base[field] = cached_item[field]
        for field in ("hex", "flight", "lat", "lon", "alt_baro", "alt_geom", "gs", "track", "baro_rate", "squawk", "seen", "rssi", "messages"):
            if local_item.get(field) is not None:
                base[field] = local_item[field]
        # A velocity message can lag position and altitude by up to a minute
        # after an aircraft is first acquired or its signal is weak, which is
        # what left JBU2754 with no speed or direction for its first fifty
        # seconds. readsb derives a heading from consecutive positions in that
        # gap and reports it as calc_track; there is no equivalent way to
        # derive speed locally, so that gap is real and stays.
        if base.get("track") is None and local_item.get("calc_track") is not None:
            base["track"] = local_item["calc_track"]
            base["track_calculated"] = True
        base["source"] = "local+adsb.fi" if already_network else "local"
        merged[key or callsign] = base
    return list(merged.values())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", default=str(WINDOWS_RUNTIME / "merged-aircraft.json") if os.name == "nt" else "/run/skyhi-fr24/aircraft.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config(Path(args.config))
    token = os.environ.get("FR24_API_TOKEN")
    if not token:
        raise SystemExit("FR24_API_TOKEN is not configured")
    lat, lon = float(config["receiver_lat"]), float(config["receiver_lon"])
    radius = float(config.get("fr24_radius_nm", 15))
    adsbfi_interval = max(2.0, float(config.get("adsbfi_poll_seconds", 5)))
    adsbfi_close_interval = max(1.0, float(config.get("adsbfi_close_poll_seconds", 2)))
    adsbfi_max_seen = max(5.0, float(config.get("adsbfi_max_seen_seconds", 20)))
    active_trigger = float(config.get("fr24_active_trigger_nm", 8))
    tracking_polygon = config.get("tracking_polygon") or []
    daily_budget = max(1, int(config.get("fr24_daily_credit_budget", 3000)))
    budget_path = WINDOWS_RUNTIME / "fr24-budget.json" if os.name == "nt" else STATE_DIR / "fr24-budget.json"
    metadata_path = WINDOWS_RUNTIME / "fr24-enrichment.json" if os.name == "nt" else STATE_DIR / "fr24-enrichment.json"
    metadata = load_json(metadata_path)
    enrichment_retry_after: dict[str, float] = {}
    # The units set SKYHI_RECEIVER_JSON to wherever the installed decoder writes,
    # which differs between dump1090-fa and readsb.
    local_path = (WINDOWS_RUNTIME / "dump1090-aircraft.json" if os.name == "nt"
                  else Path(os.environ.get("SKYHI_RECEIVER_JSON")
                            or config.get("hybrid_local_json")
                            or "/run/dump1090-fa/aircraft.json"))
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "Accept-Version": "v1"}
    session = requests.Session()
    next_enrichment = 0.0
    last_budget_warning = 0.0
    network_aircraft: list[dict[str, Any]] = []
    next_adsbfi = 0.0
    adsbfi_last_success = 0.0
    fr24_ok = True
    feeding = None
    next_feed_check = 0.0
    while not STOP.is_set():
        now = time.monotonic()
        local = load_json(local_path)
        # A network-only aircraft entering the selected area can trigger the
        # same one-time FR24 metadata enrichment as a locally received target.
        tracking_input = {"aircraft": merge_hybrid(local, network_aircraft)}
        # Must match how many aircraft the panel can actually select (max_aircraft),
        # not a smaller number of its own: anything ranked below this cap here
        # never gets a chance at FR24 data at all, even once the panel shows it.
        enrichment_limit = max(MAX_ENRICHMENT_BATCH, int(config.get("max_aircraft", MAX_ENRICHMENT_BATCH)))
        candidates = enrichment_candidates(tracking_input, lat, lon, config, enrichment_limit)
        active_target = active_local_target(tracking_input, lat, lon, active_trigger, tracking_polygon)
        active = active_target is not None
        # Every aircraft that can reach the panel is enriched, not just the
        # nearest, and they go out as one request: FR24 accepts a list of
        # callsigns, so a sky full of new traffic costs one round trip instead
        # of one per aircraft. Waiting for each aircraft's turn is what used to
        # put a flight on screen with no route.
        hit_ttl = float(config.get("fr24_enrichment_cache_seconds", 43200))
        miss_ttl = float(config.get("fr24_enrichment_miss_seconds", 60))
        wanted: dict[str, str] = {}
        for item in candidates:
            callsign = str(item.get("flight") or "").strip()
            if not callsign:
                continue
            cached = metadata.get(callsign) if isinstance(metadata.get(callsign), dict) else {}
            age = time.time() - float(cached.get("fetched", 0)) if cached else float("inf")
            ttl = hit_ttl if cached.get("item") else miss_ttl
            if age >= ttl and time.time() >= enrichment_retry_after.get(callsign, 0):
                wanted[callsign] = str(item.get("hex") or "").lower()
        first_contact = any_first_contact(wanted, metadata)

        budget = load_budget(budget_path)
        over_budget = int(budget.get("credits", 0)) >= daily_budget
        if wanted and over_budget:
            # The cap was previously recorded but never applied, so a busy day
            # could run past the quota and earn nothing but rejections.
            if now - last_budget_warning > 600:
                LOG.warning("Daily FR24 credit budget of %d reached; holding enrichment until tomorrow", daily_budget)
                last_budget_warning = now
        elif wanted and (first_contact or now >= next_enrichment):
            try:
                response = session.get(URL, headers=headers, timeout=10, params={
                    "callsigns": ",".join(wanted),
                    "limit": len(wanted),
                })
                response.raise_for_status()
                rows = response.json().get("data", [])
                matched = set()
                for row in rows:
                    item = normalize(row)
                    key = str(item.get("flight") or "").strip()
                    if not key:
                        continue
                    matched.add(key)
                    entry = {"fetched": time.time(), "item": item}
                    metadata[key] = entry
                    if item.get("hex"):
                        metadata[str(item["hex"]).lower()] = entry
                # Remember the misses too, so an unknown callsign is not retried
                # on every pass through the loop.
                for key, hex_code in wanted.items():
                    if key not in matched:
                        metadata[key] = {"fetched": time.time(), "item": {}}
                atomic_json(metadata_path, metadata)
                budget["credits"] = int(budget.get("credits", 0)) + max(1, len(rows) * 8)
                budget["calls"] = int(budget.get("calls", 0)) + 1
                budget["last_success"] = time.time()
                atomic_json(budget_path, budget)
                fr24_ok = True
                LOG.info("FR24 enrichment: %d requested, %d matched (%s)",
                         len(wanted), len(matched), ", ".join(sorted(matched)) or "none")
                next_enrichment = now + ENRICHMENT_MIN_INTERVAL
            except Exception as exc:
                # 402 is an empty credit balance, 401 and 403 a rejected token.
                # Either way FR24 will not answer, and the panel should say so.
                text = str(exc)
                if any(code in text for code in ("402", "401", "403")):
                    fr24_ok = False
                LOG.error("FR24 enrichment failed for %s: %s", ",".join(wanted), exc)
                for key in wanted:
                    enrichment_retry_after[key] = time.time() + 300
        if now >= next_adsbfi:
            try:
                url = ADSBFI_URL.format(lat=lat, lon=lon, dist=radius)
                response = session.get(url, headers={"Accept": "application/json", "User-Agent": "SkyHi/1.0"}, timeout=8)
                response.raise_for_status()
                payload = response.json()
                rows = payload.get("ac", payload.get("aircraft", []))
                network_aircraft = [normalize_adsbfi(row) for row in rows]
                network_aircraft = [item for item in network_aircraft
                                    if float(item.get("seen_pos") or item.get("seen") or 999) <= adsbfi_max_seen]
                for item in network_aircraft:
                    item["_cached_at"] = now
                adsbfi_last_success = time.time()
                LOG.info("adsb.fi poll: %d fresh nearby aircraft", len(network_aircraft))
            except Exception as exc:
                LOG.error("adsb.fi poll failed; retaining last snapshot: %s", exc)
            # Aircraft in the selected view get a faster network refresh.
            # The local receiver is still merged every second and remains the
            # preferred source whenever it has a fresh observation.
            next_adsbfi = now + (adsbfi_close_interval if active else adsbfi_interval)

        current_network = []
        for cached in network_aircraft:
            item = dict(cached)
            item["seen"] = float(item.get("seen", 0)) + max(0.0, now - float(item.pop("_cached_at", now)))
            current_network.append(item)
        aircraft = merge_hybrid(local, current_network, metadata)
        # This file is rewritten every second whether or not adsb.fi answered,
        # so its freshness says only that the collector is alive. Readers need
        # to know if the network source itself is still responding, which is
        # why the last successful poll is published alongside the aircraft.
        if now >= next_feed_check:
            feeding = feeding_adsbfi()
            next_feed_check = now + 15
        atomic_json(Path(args.output), {"now": time.time(), "messages": len(aircraft),
                                        "source": "local+adsb.fi", "aircraft": aircraft,
                                        "adsbfi_last_success": adsbfi_last_success,
                                        "fr24_ok": fr24_ok,
                                        "feeding_adsbfi": feeding})
        STOP.wait(0.5)
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    raise SystemExit(main())
