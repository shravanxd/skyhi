# Installation

This guide assumes Raspberry Pi OS Lite 64-bit, a Raspberry Pi 3 Model B+, an RTL-SDR Blog V4, and a 128×64 1/32 scan HUB75 panel connected through the documented SEENGREAT adapter.

Commands that install system files require `sudo`.

## 1. Install build tools

```bash
sudo apt update
sudo apt full-upgrade -y
sudo apt install -y git build-essential cmake python3-dev python3-pip python3-venv \
  librtlsdr-dev libusb-1.0-0-dev pkg-config
```

## 2. Install dump1090-fa

Build FlightAware dump1090 or install the Raspberry Pi OS package when available. Confirm that it creates `/run/dump1090-fa/aircraft.json`.

The RTL-SDR kernel TV driver must not claim the dongle. [`system/dump1090-blacklist.conf`](../system/dump1090-blacklist.conf) contains the relevant blacklist entry for this build.

## 3. Build the matrix library

```bash
cd /opt/skyhi
sudo git clone https://github.com/hzeller/rpi-rgb-led-matrix.git
cd rpi-rgb-led-matrix
```

Add the `seengreat` mapping from [`vendor-patches/hardware-mapping.c`](../vendor-patches/hardware-mapping.c) to the library's `lib/hardware-mapping.c`, then build the library and Python bindings:

```bash
make -j2
make build-python PYTHON=$(command -v python3)
```

Test the panel before installing SkyHi:

```bash
sudo examples-api-use/demo -D0 \
  --led-gpio-mapping=seengreat \
  --led-rows=64 \
  --led-cols=128 \
  --led-chain=1
```

## 4. Install SkyHi

```bash
sudo mkdir -p /opt/skyhi
sudo chown "$USER:$USER" /opt/skyhi
cd /opt/skyhi
git clone https://github.com/shravanxd/skyhi.git source
cp -a source/flight-display ./flight-display
cd flight-display
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

Install the matrix Python package into the same environment according to the upstream library instructions.

## 5. Configure SkyHi

```bash
cp config.example.json config.json
nano config.json
```

At minimum, set the receiver latitude, longitude, heading, and field of view. `config.json` is excluded from Git so the receiver location stays local.

The default tracking cadence is:

- local dump1090 merge every second
- adsb.fi nearby scan every five seconds
- adsb.fi active-target scan every two seconds
- FR24 only when an active aircraft lacks cached route or identity metadata

Create the FR24 environment file:

```bash
mkdir -p ~/.config/skyhi
chmod 700 ~/.config/skyhi
nano ~/.config/skyhi/fr24.env
chmod 600 ~/.config/skyhi/fr24.env
```

Its content is:

```text
FR24_API_TOKEN=replace_with_your_token
```

## 6. Install services

Review the usernames and paths in [`systemd/`](../systemd/) before copying them. They currently target user `shravanxd` and `/opt/skyhi/flight-display`.

```bash
sudo cp ../source/systemd/skyhi-fr24.service /etc/systemd/system/
sudo cp ../source/systemd/skyhi-flight-display.service /etc/systemd/system/
sudo cp ../source/systemd/skyhi-control.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dump1090-fa skyhi-fr24 skyhi-flight-display skyhi-control
```

Open `http://skyhi.local:8080/`. On first start, the control service prints a generated six-digit PIN to its journal. Change it from the dashboard after signing in.

## 7. Validate the pipeline

```bash
jq '.aircraft[:3]' /run/dump1090-fa/aircraft.json
jq '.aircraft[:3]' /run/skyhi-fr24/aircraft.json
systemctl status dump1090-fa skyhi-fr24 skyhi-flight-display skyhi-control
```

Use the dashboard preview before running solid-color tests on the physical panel.

The merged feed should report `local+adsb.fi` at the document level. Individual aircraft report `local`, `adsb.fi`, or `local+adsb.fi` depending on which observations were available.

## 8. Optional adsb.fi feeder

SkyHi can contribute the receiver's Beast data to adsb.fi. Install the official feeder scripts separately and configure their input as `127.0.0.1:30005`. Feeding is independent of the open-data tracking API, so a feeder outage does not stop the LED display from reading local dump1090 data.

After setup, verify both components:

```bash
systemctl is-active adsbfi-feed adsbfi-mlat
journalctl -u adsbfi-feed -u adsbfi-mlat -n 50 --no-pager
```
