import json
import sys

sys.path.insert(0, "/tmp")

from skyhi_display import Renderer


with open("/opt/skyhi/flight-display/airlines.json", encoding="utf-8") as handle:
    airlines = json.load(handle)
with open("/tmp/weather-rain.json", encoding="utf-8") as handle:
    weather = json.load(handle)

Renderer(airlines, "classic", 147).idle(5, weather, True).save("/tmp/feed-status-active.png")
Renderer(airlines, "classic", 147).idle(5, weather, False).save("/tmp/feed-status-down.png")
