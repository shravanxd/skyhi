#!/usr/bin/env python3
"""Render the original SkyHi UI and stream exact frames to the ESP32 display.

Runs on the Raspberry Pi alongside dump1090-fa and the hybrid collector, or on
a Windows PC as a fallback. The host does all selection, enrichment, and
rendering; the ESP32 only drives HUB75.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import requests
from PIL import Image

from skyhi_display import (
    EnrichmentCache,
    Renderer,
    WeatherCache,
    clean_callsign,
    is_small_aircraft,
    load_json,
    link_status,
    select_aircraft,
    should_switch_target,
    target_key,
    write_json_atomic,
)

APP_DIR = Path(__file__).resolve().parent
LOG = logging.getLogger("skyhi.bridge")
RUNNING = True
WINDOWS = os.name == "nt"
RUNTIME_DIR = APP_DIR / "cache" / "runtime" if WINDOWS else Path("/run")
# On the Pi the hybrid collector owns the merged feed in its own systemd
# RuntimeDirectory; the bridge only reads it and keeps its own snapshot for the
# case where that collector is down and the direct adsb.fi fallback takes over.
MERGED_FEED_PATH = RUNTIME_DIR / "merged-aircraft.json" if WINDOWS else Path("/run/skyhi-fr24/aircraft.json")
AIRCRAFT_STATE_PATH = RUNTIME_DIR / "aircraft.json" if WINDOWS else Path("/run/skyhi-bridge-aircraft.json")
BRIDGE_STATE_PATH = RUNTIME_DIR / "pc-bridge-state.json" if WINDOWS else Path("/run/skyhi-bridge-state.json")
POWER_STATE_PATH = RUNTIME_DIR / "power.json" if WINDOWS else Path("/run/skyhi-power.json")
PANEL_TEST_PATH = RUNTIME_DIR / "skyhi-panel-test.json"
# A dated tribute screen. Written with an expiry so it retires on its own
# without needing another deploy. Accepted from either the runtime directory or
# the state directory, so switching it on does not require root.
REMEMBRANCE_PATHS = (
    RUNTIME_DIR / "skyhi-remembrance.json",
    Path(os.environ.get("SKYHI_STATE_DIR", str(Path.home() / ".local/state/skyhi"))) / "skyhi-remembrance.json",
)


# Measured ceiling for the ESP32 is about 8 frames a second; this leaves a
# little headroom so the link is never the bottleneck.
REMEMBRANCE_FRAME_INTERVAL = 0.04
# How long something that loiters out of view is kept off the panel.
SUPPRESSION_SECONDS = 600.0
# How long the panel keeps showing an aircraft that reception has lost.
HOLD_THROUGH_DROPOUT = 20.0


def load_remembrance() -> dict[str, Any]:
    for path in REMEMBRANCE_PATHS:
        value = load_json(path, {})
        if value:
            return value
    return {}
TRACKED_FLIGHT_PATH = RUNTIME_DIR / "skyhi-tracked-flight" / "state.json"


def stop(*_: Any) -> None:
    global RUNNING
    RUNNING = False


def rgb565_bytes(image, brightness: float = 1.0) -> bytes:
    """Convert the finished 128x64 Pillow image to little-endian RGB565.

    Vectorised because the per-pixel Python equivalent costs roughly a tenth of
    a Cortex-A53 core at two frames per second.

    brightness dims the frame here, for panels whose firmware cannot set its
    own. Hardware dimming is preferable where it exists, since scaling before a
    five and six bit encoding costs colour resolution, but a panel that cannot
    dim at all is worse than one that dims coarsely.
    """
    pixels = np.asarray(image.convert("RGB"), dtype=np.uint16)
    if brightness < 0.999:
        pixels = (pixels.astype(np.float32) * max(0.0, brightness)).astype(np.uint16)
    color = ((pixels[:, :, 0] & 0xF8) << 8) | ((pixels[:, :, 1] & 0xFC) << 3) | (pixels[:, :, 2] >> 3)
    return color.astype("<u2").tobytes()



def find_display(configured: str | None) -> str:
    candidates = [configured, os.environ.get("SKYHI_DISPLAY_URL"), "http://skyhi-display.local"]
    # Keep the last known DHCP address as a fast fallback. The health check
    # prevents frames from silently going to another device if DHCP changes.
    candidates.append("http://192.168.1.221")
    for candidate in filter(None, candidates):
        url = str(candidate).rstrip("/")
        try:
            response = requests.get(url + "/", timeout=1.2)
            payload = response.json()
            if response.ok and payload.get("name") == "SkyHi Display":
                return url
        except (requests.RequestException, ValueError):
            continue
    raise ConnectionError("SkyHi ESP32 display was not found on the local network")


class AircraftFeed:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.session = requests.Session()
        self.payload: dict[str, Any] = {"aircraft": []}
        self.last_poll = 0.0
        self.last_success = 0.0

    def get(self) -> dict[str, Any]:
        try:
            if time.time() - MERGED_FEED_PATH.stat().st_mtime < 5:
                merged = load_json(MERGED_FEED_PATH, {})
                if isinstance(merged.get("aircraft"), list):
                    self.payload = merged
                    self.last_success = time.monotonic()
                    write_json_atomic(AIRCRAFT_STATE_PATH, self.payload)
                    return self.payload
        except OSError:
            pass
        interval = max(2, int(self.config.get("adsbfi_poll_seconds", 5)))
        if time.monotonic() - self.last_poll < interval:
            return self.payload
        self.last_poll = time.monotonic()
        lat = float(self.config["receiver_lat"])
        lon = float(self.config["receiver_lon"])
        radius = max(1, float(self.config.get("fr24_radius_nm", 6)))
        url = f"https://opendata.adsb.fi/api/v3/lat/{lat}/lon/{lon}/dist/{radius}"
        try:
            response = self.session.get(url, timeout=6, headers={"User-Agent": "SkyHi/1.0"})
            response.raise_for_status()
            raw = response.json()
            aircraft = []
            for entry in raw.get("ac", raw.get("aircraft", [])):
                item = dict(entry)
                item["source"] = "adsb.fi"
                aircraft.append(item)
            self.payload = {"now": time.time(), "aircraft": aircraft}
            self.last_success = time.monotonic()
            write_json_atomic(AIRCRAFT_STATE_PATH, self.payload)
        except (requests.RequestException, ValueError) as exc:
            LOG.warning("ADS-B network refresh failed: %s", exc)
        return self.payload

    @property
    def healthy(self) -> bool:
        """Whether the network feed itself is answering, not merely being read.

        The collector rewrites the merged file every second even when adsb.fi
        is failing, so treating a fresh file as a healthy feed showed a green
        light on the idle screen through an outage.
        """
        reported = float(self.payload.get("adsbfi_last_success") or 0)
        if reported:
            return time.time() - reported < 60
        return time.monotonic() - self.last_success < 20


def run(config_path: Path, display_url: str | None) -> int:
    config = load_json(config_path, load_json(APP_DIR / "config.example.json", {}))
    requested_display_url = display_url
    while RUNNING:
        try:
            display_url = find_display(requested_display_url)
            break
        except ConnectionError:
            LOG.info("Waiting for the SkyHi ESP32 receiver...")
            # Keep the heartbeat going while searching so a watchdog can tell a
            # missing panel apart from a hung process.
            write_json_atomic(BRIDGE_STATE_PATH, {
                "updated": time.time(), "display_url": None,
                "feed_active": False, "aircraft": 0, "searching": True,
            })
            time.sleep(3)
    if not RUNNING:
        return 0
    LOG.info("Streaming original SkyHi frames to %s", display_url)
    renderer = Renderer(
        load_json(APP_DIR / "airlines.json", {}),
        str(config.get("display_color_mode", "classic")),
        float(config.get("receiver_heading_deg") or 148),
        float(config.get("fr24_radius_nm") or 6),
    )
    weather = WeatherCache(float(config["receiver_lat"]), float(config["receiver_lon"]),
                           int(config.get("weather_refresh_seconds", 300)))
    cache = EnrichmentCache(APP_DIR / "cache" / "enrichment.sqlite3",
                            int(config.get("route_cache_days", 30)),
                            float(config.get("api_timeout_seconds", 3)))
    feed = AircraftFeed(config)
    session = requests.Session()
    locked_id: str | None = None
    locked_min_distance = float("inf")
    locked_missing_since: float | None = None
    switched = time.monotonic()
    displayed_seconds: dict[str, float] = {}
    suppressed_until: dict[str, float] = {}
    last_config_check = 0.0
    last_aircraft: dict[str, Any] | None = None
    last_connection_log = 0.0
    last_heartbeat = 0.0
    sent_brightness: int | None = None
    last_frame_data: bytes | None = None
    last_frame_sent = 0.0
    brightness_retry_after = 0.0
    hardware_brightness: bool | None = None
    software_brightness = 1.0

    while RUNNING:
        if time.monotonic() - last_config_check >= 2:
            fresh = load_json(config_path, config)
            config.update(fresh)
            renderer.color_mode = str(config.get("display_color_mode", "classic"))
            renderer.heading = float(config.get("receiver_heading_deg") or 148)
            renderer.tracking_range_nm = float(config.get("fr24_radius_nm") or 6)
            feed.config = config
            last_config_check = time.monotonic()
            # The ESP32 has no persistent brightness setting of its own, so it
            # must be pushed whenever the configured percentage changes (and
            # again after a reconnect, since a reboot resets it to default).
            brightness_percent = max(1.0, min(100.0, float(config.get("brightness", 75))))
            target_brightness = max(0, min(255, round(brightness_percent * 2.55)))
            if target_brightness != sent_brightness and time.monotonic() >= brightness_retry_after:
                try:
                    reply = session.post(display_url + "/brightness",
                                         data=str(target_brightness).encode(), timeout=2)
                    if reply.ok:
                        sent_brightness = target_brightness
                        if not hardware_brightness:
                            LOG.info("Panel accepts brightness in hardware")
                        hardware_brightness = True
                    else:
                        # Firmware predating the endpoint answers 404 forever,
                        # so stop asking and dim the frame here instead. The
                        # slider then works on the panel as it stands, without
                        # waiting for the firmware to be reflashed.
                        if hardware_brightness is not False:
                            LOG.info("Panel cannot set its own brightness (HTTP %s); dimming frames instead",
                                     reply.status_code)
                        hardware_brightness = False
                        brightness_retry_after = time.monotonic() + 300
                except requests.RequestException as exc:
                    brightness_retry_after = time.monotonic() + 30
                    LOG.warning("Brightness push failed: %s", exc)
            # Full scale whenever the panel is doing the dimming itself.
            software_brightness = 1.0 if hardware_brightness else brightness_percent / 100.0

        payload = feed.get()
        now = time.monotonic()

        power = load_json(POWER_STATE_PATH, {"enabled": True})
        panel_test = load_json(PANEL_TEST_PATH, {})
        # The tribute takes over for its hold, then yields for one full pass of
        # the normal display, so it appears once per loop rather than cutting
        # into the flight pages.
        remembrance = load_remembrance()
        show_remembrance, remembrance_phase = False, 0.0
        if float(remembrance.get("until", 0) or 0) > time.time():
            hold = max(1.0, float(remembrance.get("seconds", 9)))
            clock = datetime.now()
            finale_hour = int(remembrance.get("finale_hour", 23))
            finale_minute = int(remembrance.get("finale_minute", 59))
            if clock.hour == finale_hour and clock.minute == finale_minute:
                # The closing minute of the day it marks: the tribute holds the
                # panel for the whole minute rather than taking its usual turn,
                # then the expiry stops it at midnight.
                into = clock.second + clock.microsecond / 1e6
                show_remembrance = True
                intro = Renderer.REMEMBRANCE_INTRO_SHARE
                intro_seconds = hold * intro
                if into < intro_seconds:
                    # The dedication plays once, at its normal pace.
                    remembrance_phase = (into / intro_seconds) * intro
                else:
                    # Then the towers hold, the helix looping for the rest.
                    towers = ((into - intro_seconds) / (hold * (1 - intro))) % 1.0
                    remembrance_phase = intro + towers * (1 - intro)
            else:
                loop_seconds = max(10.0, 2 * float(config.get("page_seconds", 5)))
                elapsed = time.time() - float(remembrance.get("started", 0) or 0)
                position = elapsed % (hold + loop_seconds)
                show_remembrance = position < hold
                remembrance_phase = position / hold
        tracked_state = load_json(TRACKED_FLIGHT_PATH, {})
        if not power.get("enabled", True):
            frame = Image.new("RGB", (128, 64), "black")
        elif float(panel_test.get("expires", 0)) > time.time():
            frame = renderer.test_pattern(str(panel_test.get("pattern", "logo")))
        else:
            frame = None
        suppressed_until = {key: expiry for key, expiry in suppressed_until.items() if expiry > now}
        selected = select_aircraft(payload, config)
        # An aircraft in the window is never suppressed. Suppression exists to
        # stop something loitering from owning the panel, not to hide a flight
        # that is actually crossing the view.
        candidates = [item for item in selected
                      if target_key(item) not in suppressed_until or item.get("_in_view")]
        by_id = {target_key(item): item for item in candidates}
        aircraft = by_id.get(locked_id) if locked_id else None

        if locked_id and aircraft:
            locked_missing_since = None
            # A light aircraft orbiting overhead would otherwise hold the panel
            # for as long as it stays up there. It gets a fixed share and then
            # steps aside, in the window or not, so the display keeps moving.
            small_limit = float(config.get("small_aircraft_max_seconds", 15))
            loitering = (is_small_aircraft(aircraft)
                         and displayed_seconds.get(locked_id, 0) >= small_limit)
            if loitering:
                suppressed_until[locked_id] = now + SUPPRESSION_SECONDS
                locked_id, aircraft = None, None
            elif isinstance(aircraft.get("_distance_nm"), float):
                distance = float(aircraft["_distance_nm"])
                receding = distance > locked_min_distance + 0.5
                locked_min_distance = min(locked_min_distance, distance)
                if receding and distance > float(config.get("target_release_nm", 10)):
                    locked_id, aircraft = None, None
        elif locked_id:
            locked_missing_since = locked_missing_since or now
            # Keep the aircraft on screen through a short reception gap rather
            # than dropping to the idle clock. Losing an aircraft for a few
            # seconds is routine, and blanking it mid-pass is exactly what made
            # a flight crossing the window seem to vanish.
            if now - locked_missing_since <= HOLD_THROUGH_DROPOUT and last_aircraft is not None:
                aircraft = last_aircraft
            if now - locked_missing_since > 45:
                locked_id, locked_missing_since, last_aircraft = None, None, None

        # Give up the current aircraft for one that matters more, rather than
        # holding it until it happens to recede out of range.
        best = candidates[0] if candidates else None
        if best is not None and should_switch_target(
                aircraft, best, now - switched, float(config.get("page_seconds", 5))):
            aircraft = best
            locked_id = target_key(best)
            locked_min_distance = float(best.get("_distance_nm") or float("inf"))
            locked_missing_since = None
            switched = now

        tracked_screen_seconds = max(3, float(tracked_state.get("screen_seconds", 5)))
        tracked_normal_seconds = max(5, float(tracked_state.get("normal_seconds", 15)))
        tracked_block = tracked_screen_seconds * 3
        tracked_cycle = tracked_block + tracked_normal_seconds
        tracked_phase = (time.time() - float(tracked_state.get("ready_at") or time.time())) % tracked_cycle
        show_tracked = bool(
            tracked_state.get("active") and tracked_state.get("ready")
            and time.time() - float(tracked_state.get("updated", 0)) < 30
            and tracked_phase < tracked_block
        )

        if frame is not None:
            pass
        elif show_remembrance:
            frame = renderer.remembrance(remembrance_phase)
        elif show_tracked:
            frame = renderer.tracked(tracked_state, min(2, int(tracked_phase / tracked_screen_seconds)))
        elif aircraft:
            if target_key(aircraft) == locked_id and not locked_missing_since:
                last_aircraft = aircraft
            extra = cache.request(aircraft)
            for key in ("aircraft_type", "registration", "origin_code", "destination_code"):
                if aircraft.get(key):
                    extra[key] = aircraft[key]
            page = int((now - switched) / float(config.get("page_seconds", 5)))
            frame = renderer.aircraft(aircraft, extra, page)
            if locked_id:
                displayed_seconds[locked_id] = displayed_seconds.get(locked_id, 0) + 0.5
        else:
            # Checked on a slow cadence: it is a subprocess, and a feed does
            # not come and go between frames.
            frame = renderer.idle(len(payload.get("aircraft", [])), weather.get(), feed.healthy,
                                  link_status(payload.get("feeding_adsbfi"), payload.get("fr24_ok")))

        if now - last_heartbeat >= 2:
            # Written after selection so it reports what is actually on the
            # panel, which is what makes the choice checkable from outside.
            in_view = [item for item in candidates if item.get("_in_view")]
            write_json_atomic(BRIDGE_STATE_PATH, {
                "updated": time.time(), "display_url": display_url,
                "feed_active": feed.healthy, "aircraft": len(payload.get("aircraft", [])),
                "candidates": len(candidates), "in_view": len(in_view),
                "showing": clean_callsign(aircraft.get("flight")) if aircraft else None,
                "showing_hex": target_key(aircraft) if aircraft else None,
                "showing_distance_nm": (round(float(aircraft["_distance_nm"]), 1)
                                        if aircraft and isinstance(aircraft.get("_distance_nm"), float) else None),
                "showing_in_view": bool(aircraft.get("_in_view")) if aircraft else None,
                "nearest_in_view": (clean_callsign(in_view[0].get("flight")) if in_view else None),
                "nearest_in_view_nm": (round(float(in_view[0]["_distance_nm"]), 1)
                                       if in_view and isinstance(in_view[0].get("_distance_nm"), float) else None),
                "screen": ("panel-test" if frame is not None and False else
                           "remembrance" if show_remembrance else
                           "tracked" if show_tracked else
                           "aircraft" if aircraft else "idle"),
            })
            last_heartbeat = now

        frame_data = rgb565_bytes(frame, software_brightness)
        # The panel already holds this image, so an unchanged frame costs a
        # 16 KB upload and 8,192 ESP32 pixel writes for nothing. Idle and
        # powered-off screens sit still for minutes at a time. Resend anyway
        # every few seconds so a dropped link is still noticed promptly.
        if frame_data == last_frame_data and now - last_frame_sent < 3:
            time.sleep(0.5)
            continue

        try:
            # ESP32 WebServer exposes streamed upload chunks for multipart
            # bodies, avoiding a second 16 KB request allocation on the MCU.
            response = session.post(
                display_url + "/frame",
                files={"frame": ("skyhi.rgb565", frame_data, "application/octet-stream")},
                timeout=2,
            )
            response.raise_for_status()
            last_frame_data, last_frame_sent = frame_data, now
        except requests.RequestException as exc:
            if now - last_connection_log > 10:
                LOG.warning("Display link interrupted: %s", exc)
                last_connection_log = now
            time.sleep(1)
            try:
                display_url = find_display(display_url)
                # A reconnected panel may have rebooted and lost both its
                # brightness and its picture, so force both to be resent.
                sent_brightness, last_frame_data = None, None
                brightness_retry_after = 0.0
            except ConnectionError:
                pass
        # The tribute animates, so it is paced close to what the panel can
        # actually accept. Everything else changes slowly and does not need
        # the bandwidth.
        time.sleep(REMEMBRANCE_FRAME_INTERVAL if show_remembrance else 0.5)

    # The panel holds whatever arrived last, so leave it dark on a clean stop
    # instead of stranding a stale flight on screen.
    try:
        session.post(display_url + "/frame", timeout=2, files={
            "frame": ("skyhi.rgb565", rgb565_bytes(Image.new("RGB", (128, 64), "black")),
                      "application/octet-stream"),
        })
    except requests.RequestException:
        pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=APP_DIR / "config.json")
    parser.add_argument("--display-url", help="ESP32 base URL, usually discovered automatically")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    return run(args.config, args.display_url)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    raise SystemExit(main())
