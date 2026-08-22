# Tracking and data sources

SkyHi deliberately separates fast motion data from richer flight identity data. This makes close flyovers feel live without continuously spending paid API credits.

## Source priority

For each ICAO hex address, SkyHi merges fields in this order:

1. Fresh dump1090 observations win for position, altitude, ground speed, track, vertical rate, signal, and message count.
2. adsb.fi supplies those fields when the local antenna has not received the aircraft. It also commonly supplies registration and ICAO aircraft type.
3. Cached FR24 enrichment supplies route, operator, registration, and type when those details are missing.
4. The route cache used by the display can fill airport and airline labels without another lookup.

The final record contains a `source` value:

| Value | Meaning |
| --- | --- |
| `local` | Seen only by this RTL-SDR receiver |
| `adsb.fi` | Seen through the community network but not recently by the local receiver |
| `local+adsb.fi` | Matching local and network records were merged, with local motion preferred |

## Update cadence

The collector writes `/run/skyhi-fr24/aircraft.json` every second.

| Situation | Source cadence |
| --- | --- |
| Local reception | dump1090 data is merged every second |
| Normal network scan | adsb.fi every 5 seconds |
| Aircraft inside the polygon or activation radius | adsb.fi every 2 seconds |
| Missing route or identity | One FR24 request, then cached |
| Weather | Every 5 minutes |

The display itself renders continuously. A number changes when a source reports a new value. SkyHi does not extrapolate altitude or speed because a smooth but invented number would be less trustworthy than the latest real report.

## Selecting the aircraft

When a tracking polygon exists, fresh positioned aircraft inside it rank first. Without a polygon, SkyHi uses the configured receiver heading, field-of-view angle, and distance. The nearest eligible aircraft is favored, then kept locked until it becomes stale or crosses the release distance. Locking prevents the panel from rapidly switching between nearby flights.

The map controls and numeric controls modify the same configuration:

- receiver marker sets latitude and longitude
- heading handle sets the center of the window view
- field of view controls the cone width
- tracking range sets both the adsb.fi search radius and activation radius
- polygon overrides the simple radial activation test when present

## Freshness and failure handling

- Local observations use the short `local_max_seen_seconds` window.
- adsb.fi positions older than `adsbfi_max_seen_seconds` are rejected.
- If adsb.fi fails, the last snapshot is retained briefly while local data continues.
- If FR24 fails or the budget is exhausted, live tracking continues without uncached route metadata.
- If the internet is unavailable, dump1090 and cached routes, logos, and metadata continue working.
- If no eligible aircraft remains, the panel returns to the clock, weather, and feed-status screen.

## Important configuration

| Key | Default | Purpose |
| --- | ---: | --- |
| `fr24_radius_nm` | 6 | adsb.fi geographic search radius; retained under its legacy name for compatibility |
| `fr24_active_trigger_nm` | 8 | radial activation distance when no polygon is saved |
| `adsbfi_poll_seconds` | 5 | normal network refresh |
| `adsbfi_close_poll_seconds` | 2 | network refresh for an active close target |
| `adsbfi_max_seen_seconds` | 20 | oldest accepted network position |
| `local_max_seen_seconds` | 12 | oldest local observation treated as fresh |
| `target_release_nm` | 10 | distance after which a locked target can be released |
| `max_aircraft` | 20 | maximum candidates considered by the renderer |

Some `fr24_*` names remain in configuration and service paths to preserve existing installations. They no longer imply continuous FR24 position polling.

## Privacy and API behavior

The geographic request necessarily sends the receiver latitude, longitude, and chosen radius to adsb.fi. The precise receiver location stays out of Git because the live `config.json` is ignored. The adsb.fi open-data service is intended for personal, non-commercial use and should be used within its published rate limits.
