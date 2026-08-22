#!/usr/bin/env python3
"""Poll FR24 live/full near the receiver and emit dump1090-compatible JSON."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any

import requests

LOG = logging.getLogger("skyhi.fr24")
STOP = Event()
URL = "https://fr24api.flightradar24.com/api/live/flight-positions/full"


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


def active_local_target(local: dict[str, Any], lat: float, lon: float, trigger_nm: float,
                        polygon: list[list[float]] | None = None) -> dict[str, Any] | None:
    candidates = []
    for item in local.get("aircraft", []):
        if float(item.get("seen", 999)) > 15 or item.get("lat") is None or item.get("lon") is None:
            continue
        callsign = str(item.get("flight") or "").strip()
        if not callsign:
            continue
        distance = distance_nm(lat, lon, float(item["lat"]), float(item["lon"]))
        eligible = point_in_polygon(float(item["lat"]), float(item["lon"]), polygon) if polygon else distance <= trigger_nm
        if eligible:
            candidates.append((distance, item))
    return min(candidates, key=lambda pair: pair[0])[1] if candidates else None


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


def merge_hybrid(local: dict[str, Any], fr24: list[dict[str, Any]], metadata: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Merge on ICAO hex, preferring fast local movement and FR24 metadata."""
    metadata = metadata or {}
    merged = {str(item.get("hex", "")).lower(): dict(item) for item in fr24 if item.get("hex")}
    callsigns = {str(item.get("flight") or "").strip(): key for key, item in merged.items() if item.get("flight")}
    for local_item in local.get("aircraft", []):
        hex_code = str(local_item.get("hex", "")).lower()
        callsign = str(local_item.get("flight") or "").strip()
        key = hex_code if hex_code in merged else callsigns.get(callsign, hex_code)
        already_fr24 = key in merged
        base = merged.get(key, {})
        cached = metadata.get(callsign) or metadata.get(hex_code) or {}
        cached_item = cached.get("item", {}) if isinstance(cached, dict) else {}
        for field in ("aircraft_type", "registration", "origin_code", "destination_code", "painted_as", "operating_as"):
            if cached_item.get(field) is not None:
                base[field] = cached_item[field]
        for field in ("hex", "flight", "lat", "lon", "alt_baro", "alt_geom", "gs", "track", "baro_rate", "squawk", "seen", "rssi", "messages"):
            if local_item.get(field) is not None:
                base[field] = local_item[field]
        base["source"] = "local+fr24" if already_fr24 else "local"
        merged[key or callsign] = base
    return list(merged.values())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", default="/run/skyhi-fr24/aircraft.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config(Path(args.config))
    token = os.environ.get("FR24_API_TOKEN")
    if not token:
        raise SystemExit("FR24_API_TOKEN is not configured")
    lat, lon = float(config["receiver_lat"]), float(config["receiver_lon"])
    radius = float(config.get("fr24_radius_nm", 15))
    interval = max(10.0, float(config.get("fr24_poll_seconds", 30)))
    limit = max(1, int(config.get("fr24_result_limit", 8)))
    active_interval = max(30.0, float(config.get("fr24_active_poll_seconds", 30)))
    active_limit = max(1, int(config.get("fr24_active_result_limit", 1)))
    active_trigger = float(config.get("fr24_active_trigger_nm", 8))
    tracking_polygon = config.get("tracking_polygon") or []
    daily_budget = max(1, int(config.get("fr24_daily_credit_budget", 3000)))
    budget_path = Path("/home/shravanxd/.local/state/skyhi/fr24-budget.json")
    metadata_path = Path("/home/shravanxd/.local/state/skyhi/fr24-enrichment.json")
    metadata = load_json(metadata_path)
    enrichment_retry_after: dict[str, float] = {}
    local_path = Path(config.get("hybrid_local_json", "/run/dump1090-fa/aircraft.json"))
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "Accept-Version": "v1"}
    session = requests.Session()
    fr24_aircraft: list[dict[str, Any]] = []
    next_fr24 = 0.0
    while not STOP.is_set():
        now = time.monotonic()
        local = load_json(local_path)
        active_target = active_local_target(local, lat, lon, active_trigger, tracking_polygon)
        active = active_target is not None
        # A newly seen local flight gets one immediate metadata lookup even if
        # routine polling is paused by the daily budget. Cache both hits and
        # misses for 12 hours so a display refresh can never repeat the call.
        if active_target:
            callsign = str(active_target.get("flight") or "").strip()
            hex_code = str(active_target.get("hex") or "").lower()
            enrichment_key = callsign or hex_code
            cached = metadata.get(enrichment_key, {})
            cache_age = time.time() - float(cached.get("fetched", 0)) if isinstance(cached, dict) else float("inf")
            cached_item = cached.get("item", {}) if isinstance(cached, dict) else {}
            # Successful metadata is reusable for 12 hours. A no-match can be
            # positional/timing related, so retry it after one minute.
            cache_ttl = (float(config.get("fr24_enrichment_cache_seconds", 43200))
                         if cached_item else float(config.get("fr24_enrichment_miss_seconds", 60)))
            already_enriched = any(
                (str(item.get("flight") or "").strip() == callsign or str(item.get("hex") or "").lower() == hex_code)
                and item.get("origin_code") and item.get("destination_code")
                for item in fr24_aircraft
            )
            if (enrichment_key
                    and not already_enriched
                    and cache_age >= cache_ttl
                    and time.time() >= enrichment_retry_after.get(enrichment_key, 0)):
                try:
                    # The lookup box must cover the complete local activation
                    # area; previously 8 nm targets were queried in a 6 nm box.
                    # Search around the detected aircraft itself; a custom
                    # polygon can extend beyond the receiver's normal radius.
                    params: dict[str, Any] = {"bounds": bounds(float(active_target["lat"]), float(active_target["lon"]), 2), "limit": 1}
                    if callsign:
                        params["callsigns"] = callsign
                    response = session.get(URL, headers=headers, params=params, timeout=8)
                    response.raise_for_status()
                    rows = response.json().get("data", [])
                    item = normalize(rows[0]) if rows else {}
                    metadata[enrichment_key] = {"fetched": time.time(), "item": item}
                    if hex_code:
                        metadata[hex_code] = metadata[enrichment_key]
                    atomic_json(metadata_path, metadata)
                    budget = load_budget(budget_path)
                    budget["credits"] = int(budget.get("credits", 0)) + max(1, len(rows) * 8)
                    budget["calls"] = int(budget.get("calls", 0)) + 1
                    budget["last_success"] = time.time()
                    atomic_json(budget_path, budget)
                    LOG.info("FR24 one-shot enrichment for %s: %s", enrichment_key, "matched" if rows else "no match")
                    if item:
                        item["_seen_at_poll"] = float(item.get("seen", 0))
                        item["_cached_at"] = now
                        fr24_aircraft = [item]
                    next_fr24 = now + active_interval
                except Exception as exc:
                    LOG.error("FR24 one-shot enrichment failed for %s: %s", enrichment_key, exc)
                    enrichment_retry_after[enrichment_key] = time.time() + 300
        if active and next_fr24 - now > active_interval:
            next_fr24 = now + active_interval
        if now >= next_fr24:
            budget = load_budget(budget_path)
            if int(budget.get("credits", 0)) >= daily_budget:
                LOG.warning("Daily FR24 budget reached (%s/%s); using local receiver only", budget.get("credits"), daily_budget)
                next_fr24 = now + interval
            else:
                try:
                    request_limit = active_limit if active else limit
                    params: dict[str, Any] = {"bounds": bounds(lat, lon, radius), "limit": request_limit}
                    if active_target and active_target.get("flight"):
                        params["callsigns"] = str(active_target["flight"]).strip()
                    response = session.get(URL, headers=headers, params=params, timeout=8)
                    response.raise_for_status()
                    fr24_aircraft = [normalize(row) for row in response.json().get("data", [])]
                    for item in fr24_aircraft:
                        item["_seen_at_poll"] = float(item.get("seen", 0))
                        item["_cached_at"] = now
                    charged = max(1, len(fr24_aircraft) * 8)
                    budget["credits"] = int(budget.get("credits", 0)) + charged
                    budget["calls"] = int(budget.get("calls", 0)) + 1
                    budget["last_success"] = time.time()
                    atomic_json(budget_path, budget)
                    LOG.info("FR24 %s poll: %d aircraft, estimated daily credits %d/%d", "active" if active else "idle", len(fr24_aircraft), budget["credits"], daily_budget)
                except Exception as exc:
                    LOG.error("FR24 poll failed: %s", exc)
                next_fr24 = now + (active_interval if active else interval)
        current_fr24 = []
        for cached in fr24_aircraft:
            item = dict(cached)
            item["seen"] = float(item.get("_seen_at_poll", 0)) + max(0.0, now - float(item.get("_cached_at", now)))
            item.pop("_seen_at_poll", None)
            item.pop("_cached_at", None)
            current_fr24.append(item)
        aircraft = merge_hybrid(local, current_fr24, metadata)
        atomic_json(Path(args.output), {"now": time.time(), "messages": len(aircraft), "source": "hybrid", "aircraft": aircraft})
        STOP.wait(1.0)
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    raise SystemExit(main())
