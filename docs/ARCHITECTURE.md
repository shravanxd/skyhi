# Architecture

SkyHi is four small services connected by JSON files. Keeping the boundaries simple makes the system easy to inspect over SSH and resilient on a Raspberry Pi 3.

## Runtime services

### `dump1090-fa`

Owns the RTL-SDR and decodes Mode S and ADS-B messages. It writes fresh local observations to `/run/dump1090-fa/aircraft.json`.

Useful local fields include ICAO hex, callsign, position, altitude, ground speed, track, signal strength, and seconds since last observation.

### `skyhi-fr24`

Reads the dump1090 feed, decides whether an aircraft is relevant, and polls FR24 only when needed. It writes a dump1090-compatible merged feed to `/run/skyhi-fr24/aircraft.json`.

Merge rules:

1. Match aircraft by ICAO hex when possible.
2. Prefer fresh local altitude, speed, track, position, and signal data.
3. Add FR24 route, type, registration, and operator metadata.
4. Keep local-only aircraft when no cloud match exists.
5. Drop stale cloud positions after their configured freshness window.

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

### `skyhi-control`

Serves the local dashboard and a small JSON API on port 8080. It can update configuration, preview LED output, draw a tracking polygon, run panel tests, manage schedules, export backups, and restart approved services.

The dashboard is protected by a six-digit local PIN. The stored record contains a PBKDF2 hash, random salt, and session secret. It does not store the PIN itself.

## External services

- Flightradar24 supplies live identity and route metadata.
- Open-Meteo supplies current conditions and forecast data.
- The US National Weather Service supplies severe-weather alerts when available.
- OpenStreetMap tiles provide the dashboard map.

The LED display can continue operating with local ADS-B data if the internet is unavailable. Maps, weather, and uncached enrichment will be limited until connectivity returns.

## Files and ownership

| Path | Owner | Purpose |
| --- | --- | --- |
| `/opt/skyhi/flight-display` | deployment | Application code and virtual environment |
| `/run/dump1090-fa/aircraft.json` | dump1090 | Local aircraft feed |
| `/run/skyhi-fr24/aircraft.json` | FR24 poller | Hybrid aircraft feed |
| `/run/skyhi-weather.json` | display | Cached weather for the UI |
| `~/.config/skyhi/fr24.env` | administrator | FR24 API token |
| `~/.config/skyhi/control-auth.json` | control service | PIN hash and session secret |
| `~/.local/state/skyhi/` | services | Budgets and enrichment state |

## Failure behavior

- If FR24 fails, local aircraft continue to flow from dump1090.
- If weather fails, the last good weather value remains available.
- If no aircraft qualify, the panel returns to the idle screen.
- systemd restarts application services after unexpected exits.
- A temporary panel test expires automatically and returns to normal rendering.
