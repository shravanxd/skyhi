import json
import sys
import time

sys.path.insert(0, "/opt/skyhi/flight-display")

from skyhi_display import Renderer


with open("/opt/skyhi/flight-display/airlines.json", encoding="utf-8") as handle:
    airlines = json.load(handle)

renderer = Renderer(airlines, "classic", 147)
aircraft = {"flight": "UAL1234", "alt_baro": 7200, "gs": 246, "track": 148, "_distance_nm": 2.1}
details = {
    "aircraft_type": "B38M", "origin_code": "EWR", "destination_code": "MCO",
    "origin_name": "Newark Liberty", "destination_name": "Orlando Intl",
}
renderer.identity(aircraft, details).save("/tmp/type-identity.png")
renderer.metrics(aircraft, details).save("/tmp/type-metrics.png")
time.sleep(1.25)
renderer.identity(aircraft, details).save("/tmp/type-identity-later.png")
