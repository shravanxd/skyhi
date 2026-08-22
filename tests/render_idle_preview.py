import json
import sys

sys.path.insert(0, "/opt/skyhi/flight-display")

from skyhi_display import Renderer


with open("/opt/skyhi/flight-display/airlines.json", encoding="utf-8") as handle:
    airlines = json.load(handle)
with open("/tmp/weather-rain.json", encoding="utf-8") as handle:
    weather = json.load(handle)

Renderer(airlines, "classic", 147).idle(5, weather).save("/tmp/scan-bars-small.png")
