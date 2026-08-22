# Operations

## Service controls

```bash
sudo systemctl start dump1090-fa skyhi-fr24 skyhi-flight-display skyhi-control
sudo systemctl stop dump1090-fa skyhi-fr24 skyhi-flight-display skyhi-control
sudo systemctl restart dump1090-fa skyhi-fr24 skyhi-flight-display skyhi-control
```

The web control service is intentionally separate from the three power services. Turning off SkyHi from the dashboard stops reception, polling, and the LED display while leaving the control page available to turn them back on.

## Health checks

```bash
systemctl is-active dump1090-fa skyhi-fr24 skyhi-flight-display skyhi-control
systemctl is-active adsbfi-feed adsbfi-mlat
stat /run/dump1090-fa/aircraft.json
stat /run/skyhi-fr24/aircraft.json
journalctl -u skyhi-flight-display --since "10 minutes ago" --no-pager
```

Healthy aircraft JSON should contain a recent `now` value and an `aircraft` array. A quiet sky can produce an empty array without indicating a fault.

## Common problems

### The panel stays dark after boot

1. Check `skyhi-flight-display` status and logs.
2. Confirm the matrix power supply is on and shares ground with the Pi.
3. Confirm the app configuration uses the `seengreat` mapping.
4. Run the dashboard logo or grid test.

### The RTL-SDR is missing

```bash
lsusb
rtl_test -t
journalctl -u dump1090-fa -n 100 --no-pager
```

If `rtl_test` reports that the device is busy, check that the DVB kernel modules are blacklisted and reboot.

### Aircraft appear without route or model

```bash
sudo systemctl status skyhi-fr24
sudo journalctl -u skyhi-fr24 -n 100 --no-pager
```

SkyHi makes a cached, one-shot FR24 enrichment request for a local or adsb.fi contact inside the active tracking area when route metadata is missing. A response can still be empty if the provider has no matching record.

### adsb.fi network aircraft are missing

```bash
curl -fsS "https://opendata.adsb.fi/api/v3/lat/LAT/lon/LON/dist/6" | jq '.ac | length'
sudo journalctl -u skyhi-fr24 -n 100 --no-pager
```

Replace `LAT` and `LON` with the receiver location. The public endpoint is limited to one request per second. SkyHi defaults to five seconds normally and two seconds while a target is active. Do not configure the close interval below one second.

### FR24 credit use is higher than expected

- Confirm the deployed poller is the current adsb.fi hybrid version.
- Look for `FR24 one-shot enrichment` in the `skyhi-fr24` journal.
- Repeated sightings should use the local enrichment cache.
- Use a polygon or narrower activation radius to limit which aircraft need routes.
- Compare SkyHi's estimate with the FR24 account dashboard, which is authoritative.

Continuous position tracking does not use FR24 credits. Only missing metadata enrichment does.

### Speed or altitude pauses briefly

Local RTL-SDR values are merged every second and take priority. Network-only targets refresh every five seconds normally or every two seconds inside the active area. Aircraft transponders may transmit individual fields at different times, so an unchanged value is not necessarily a fault. SkyHi does not invent movement between reports.

## Backups

The dashboard can export and restore configuration, polygons, route cache, schedules, and enrichment metadata. Store backups privately because they may include receiver coordinates.

## Safe updates

```bash
cd /opt/skyhi/source
git pull --ff-only
cp -a flight-display/. /opt/skyhi/flight-display/
sudo systemctl restart skyhi-fr24 skyhi-flight-display skyhi-control
```

Always preserve the live `config.json`, FR24 environment file, and control authentication file. They are runtime configuration, not source files.

## Security notes

- Keep the dashboard on a trusted local network.
- Use a reverse proxy with HTTPS before exposing it beyond the LAN.
- Never commit `fr24.env`, `config.json`, control authentication data, or exported backups.
- Rotate a token immediately if it appears in a terminal recording, screenshot, issue, or commit.
