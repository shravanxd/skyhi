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

SkyHi makes a one-shot enrichment request for a new local contact that lacks metadata. A response can still be empty if the provider has no matching record.

### Credit use is higher than expected

- Increase the active and idle polling intervals.
- Reduce the FR24 result limit and search radius.
- Use a polygon or narrower field of view.
- Lower the daily budget in the dashboard.
- Compare SkyHi's estimate with the FR24 account dashboard.

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
