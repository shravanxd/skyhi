# Architecture

SkyHi is five small services connected by JSON files. Keeping the boundaries simple makes the system easy to inspect over SSH and resilient on a Raspberry Pi 3.

## Runtime services

### `dump1090-fa`

Owns the RTL-SDR and decodes Mode S and ADS-B messages. It writes fresh local observations to `/run/dump1090-fa/aircraft.json`.

Useful local fields include ICAO hex, callsign, position, altitude, ground speed, track, signal strength, and seconds since last observation.

### `skyhi-fr24`

Reads the dump1090 feed, polls the nearby adsb.fi network view every five seconds, and uses FR24 only for missing metadata. It writes a dump1090-compatible merged feed to `/run/skyhi-fr24/aircraft.json`.

Merge rules:

1. Match aircraft by ICAO hex when possible.
2. Prefer fresh local altitude, speed, track, position, and signal data.
3. Use adsb.fi positions when the local antenna has not received an aircraft.
4. Add cached FR24 route and operator metadata only when needed.
5. Keep local-only aircraft when no cloud match exists.
6. Drop stale network positions after their configured freshness window.

The service keeps credit estimates in the user state directory and stores enrichment results so repeated sightings do not require repeated lookups.

### `skyhi-flight-display`

Selects the best aircraft and renders frames through the Python bindings for `rpi-rgb-led-matrix`.

Selection favors:

1. Fresh aircraft inside the saved polygon, when a polygon exists
2. Fresh aircraft inside the window field of view
3. The nearest aircraft
4. Strong local contacts when a position is unavailable

Once selected, a target remains locked until it becomes stale or passes the release distance. This prevents rapid switching when several aircraft are nearby.

The two aircraft pages are:

- Identity: logo, airline, route, model, flight number, and airport names
- Metrics: logo, flight number, route, altitude, speed, direction, airline, model, and distance

Long labels use an LED-friendly marquee instead of disappearing behind an ellipsis.

### `skyhi-flight-tracker`

Tracks one user-selected operational callsign worldwide through adsb.fi's exact callsign endpoint. It performs one FR24 enrichment for route and aircraft identity, resolves airport coordinates through HexDB, and caches low-frequency geographic context. State is written to `/run/skyhi-tracked-flight/state.json`.

The focused-flight loop contains three screens:

1. Journey progress, route, flight status, and time to destination
2. Live altitude, speed, heading, type, registration, and remaining distance
3. Current city, region, country or ocean, aircraft-local time, and ETA

After the three focused screens, the renderer returns to normal nearby-aircraft behavior for the configured break. Tracking ends automatically after three confirmed ground reports, after a likely landing following an arriving-state signal loss, at the selected deadline, or when stopped from the portal.

### `skyhi-control`

Serves the local dashboard and a small JSON API on port 8080. It can update configuration, preview LED output, draw a tracking polygon, run panel tests, manage schedules, export backups, and restart approved services.

The dashboard is protected by a six-digit local PIN. The stored record contains a PBKDF2 hash, random salt, and session secret. It does not store the PIN itself.

## External services

- adsb.fi supplies continuous nearby network positions, altitude, speed, track, registration, and type when available.
- Flightradar24 supplies one-shot identity and route enrichment when required.
- Open-Meteo supplies current conditions and forecast data.
- The US National Weather Service supplies severe-weather alerts when available.
- OpenStreetMap tiles provide the dashboard map.

The LED display can continue operating with local ADS-B data if the internet is unavailable. Maps, weather, and uncached enrichment will be limited until connectivity returns.

## Files and ownership

| Path | Owner | Purpose |
| --- | --- | --- |
| `/opt/skyhi/flight-display` | deployment | Application code and virtual environment |
| `/run/dump1090-fa/aircraft.json` | dump1090 | Local aircraft feed |
| `/run/skyhi-fr24/aircraft.json` | hybrid collector | Local and network aircraft feed |
| `/run/skyhi-tracked-flight/state.json` | callsign tracker | Focused-flight live state |
| `~/.local/state/skyhi/tracked-flight-request.json` | control service | Persistent tracking request and timing |
| `/run/skyhi-weather.json` | display | Cached weather for the UI |
| `~/.config/skyhi/fr24.env` | administrator | FR24 API token |
| `~/.config/skyhi/control-auth.json` | control service | PIN hash and session secret |
| `~/.local/state/skyhi/` | services | Budgets and enrichment state |

## Failure behavior

- If adsb.fi fails, local aircraft continue to flow from dump1090.
- If FR24 fails, tracking continues and cached or available identity data is used.
- If weather fails, the last good weather value remains available.
- If no aircraft qualify, the panel returns to the idle screen.
- systemd restarts application services after unexpected exits.
- A temporary panel test expires automatically and returns to normal rendering.
