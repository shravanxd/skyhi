# SkyHi ✈️

<p align="center">
  <img src="flight-display/web/assets/skyhi-logo.png" alt="SkyHi logo" width="260">
</p>

<p align="center">A tiny window into the very large sky.</p>

SkyHi is a Raspberry Pi powered flight tracker that turns nearby aircraft into a live 128×64 LED display. It combines an RTL-SDR receiver with adsb.fi network coverage, enriches missing flight details with carefully budgeted Flightradar24 data, and picks the aircraft that actually matter to the view outside your window.

This started as a side project built around one window, one antenna, and the question: "What plane is that?" It now has airline logos, routes, weather, a drawing-based tracking zone, a mobile control panel, automatic sleep hours, and enough tiny pixels to make every flyover feel like an event.

## What it does

- Decodes local 1090 MHz ADS-B traffic with `dump1090-fa`
- Combines fast local telemetry with continuous nearby adsb.fi network tracking
- Uses cached, one-shot Flightradar24 requests only for missing metadata
- Prioritizes aircraft inside a configurable window heading or map polygon
- Displays airline, flight, route, aircraft model, altitude, speed, direction, and distance
- Cycles between an identity page and a live metrics page
- Handles dark airline artwork without adding permanent white logo boxes
- Scrolls long aircraft names such as `737 MAX 8`
- Shows an idle screen with time, day, weather, and live adsb.fi feed health
- Provides a phone-friendly dashboard at `http://skyhi.local:8080`
- Tracks FR24 credit usage and avoids routine paid polling
- Starts automatically and recovers from crashes through systemd

## The hardware

| Part | This build |
| --- | --- |
| Computer | Raspberry Pi 3 Model B+ |
| Receiver | RTL-SDR Blog V4 |
| Display | 128×64 P2 HUB75 RGB matrix, 1/32 scan |
| Adapter | SEENGREAT RGB Matrix Adapter Board |
| Antenna | Vertical dipole mounted near a window |

The custom `seengreat` GPIO mapping is kept in [`vendor-patches/hardware-mapping.c`](vendor-patches/hardware-mapping.c).

## How the pieces talk

```text
RTL-SDR
   │
   ▼
dump1090-fa ── local telemetry ──┐
                                 │
adsb.fi ───── continuous nearby ─┤
                                 │
FR24 ───────── cached metadata ──┘
          ▼
   merged aircraft.json
          │
      ┌───┴───────────┐
      ▼               ▼
LED renderer     Web control panel
      │               │
      ▼               ▼
128×64 panel     Phone or computer
```

The local receiver remains the preferred source for movement data. adsb.fi fills reception gaps and sees aircraft throughout the configured tracking area. FR24 is used only for cached, one-time route and identity enrichment when needed. If either cloud source is unavailable, SkyHi continues with local ADS-B traffic.

Read [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full data flow.

## Quick start

SkyHi currently targets Raspberry Pi OS Lite 64-bit.

```bash
git clone https://github.com/shravanxd/skyhi.git
cd skyhi
cp flight-display/config.example.json flight-display/config.json
```

Then build `dump1090-fa`, build `rpi-rgb-led-matrix` with the SEENGREAT mapping, create a Python virtual environment, install the service files, and configure your receiver coordinates.

The complete procedure, including GPIO details and FR24 token setup, is in [`docs/INSTALL.md`](docs/INSTALL.md).

## Configuration

Copy [`flight-display/config.example.json`](flight-display/config.example.json) to `config.json`. The live config is intentionally ignored by Git because it can contain the exact location of your receiver.

| Setting | Purpose |
| --- | --- |
| `receiver_lat`, `receiver_lon` | Home position used for distance and bearing |
| `receiver_heading_deg` | Direction the window faces |
| `window_field_of_view_deg` | Width of the visible cone |
| `tracking_polygon` | Optional map-drawn activation zone |
| `adsbfi_poll_seconds` | Nearby adsb.fi refresh interval, 5 seconds by default |
| `adsbfi_close_poll_seconds` | Faster network refresh inside the active area, 2 seconds by default |
| `adsbfi_max_seen_seconds` | Reject stale network positions beyond this age |
| `fr24_active_poll_seconds` | Poll interval while a local target is active |
| `fr24_daily_credit_budget` | Daily safety cap for routine requests |
| `target_release_nm` | Distance at which SkyHi releases a tracked flyover |
| `brightness` | LED panel brightness from 10 to 100 percent |
| `page_seconds` | Time spent on each aircraft page |
| `weather_refresh_seconds` | Weather update interval, 300 seconds by default |

FR24 credentials live outside the repository in `/home/shravanxd/.config/skyhi/fr24.env`. Never place the token in `config.json` or commit the environment file.

## Services

```bash
sudo systemctl status dump1090-fa skyhi-fr24 skyhi-flight-display skyhi-control

sudo systemctl restart dump1090-fa
sudo systemctl restart skyhi-fr24
sudo systemctl restart skyhi-flight-display
sudo systemctl restart skyhi-control
```

Live logs:

```bash
sudo journalctl -u skyhi-flight-display -f
sudo journalctl -u skyhi-fr24 -f
sudo journalctl -u skyhi-control -f
```

More commands and troubleshooting notes are in [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## Local preview

```bash
cd flight-display
python3 skyhi_display.py \
  --json sample-aircraft.json \
  --config config.example.json \
  --render-png /tmp/skyhi-preview.png
```

The web dashboard also provides live, idle, identity, metrics, grid, and solid-color previews.

## Project map

```text
flight-display/
  skyhi_display.py       aircraft selection, enrichment, and LED rendering
  fr24_poller.py         local + adsb.fi collector with FR24 enrichment
  control_panel.py       local dashboard API and service controls
  web/                   responsive control interface
  assets/logos/          bundled airline logo fallbacks
systemd/                 service definitions
system/                  Raspberry Pi hardware configuration snippets
vendor-patches/          SEENGREAT matrix GPIO mapping
tests/                   fixtures and exact-size render helpers
docs/                    architecture, installation, and operations guides
```

## Known limits

- Airline coverage is broad but not universal. Unknown operators get a generated lettermark.
- Route and aircraft metadata depend on source availability and may occasionally be absent.
- FR24 credit accounting is an estimate based on returned records, so the provider dashboard remains authoritative.
- The control dashboard uses plain HTTP on a trusted local network unless you add a reverse proxy with HTTPS.
- The custom matrix mapping is specific to the documented SEENGREAT board pinout.

## Logo note

Bundled airline logos are derived from the community collection at [Jxck-S/airline-logos](https://github.com/Jxck-S/airline-logos). Airline names and marks belong to their respective owners and are used only to identify observed flights.

## Built for fun

If SkyHi makes you look up from your phone and out the window, it is doing its job.

Engineered with `<3` by [Shravan Khunti](https://github.com/shravanxd).
