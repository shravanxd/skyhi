import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).parents[1] / "flight-display" / "flight_tracker.py"
SPEC = importlib.util.spec_from_file_location("flight_tracker", MODULE_PATH)
TRACKER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(TRACKER)


class FlightTrackerTests(unittest.TestCase):
    def test_distance_is_zero_for_same_point(self):
        self.assertAlmostEqual(TRACKER.distance_nm(40.6, -73.7, 40.6, -73.7), 0)

    def test_adsb_record_is_normalized(self):
        item = TRACKER.normalize_adsb({"hex": "ABC123", "flight": "UAL1 ", "t": "B38M", "gs": 412})
        self.assertEqual(item["hex"], "abc123")
        self.assertEqual(item["flight"], "UAL1")
        self.assertEqual(item["aircraft_type"], "B38M")

    def test_fr24_record_is_normalized_for_worldwide_fallback(self):
        item = TRACKER.normalize_fr24({"callsign": "IBE03VV", "lat": 43.8, "lon": -60.9,
                                      "alt": 35000, "gspeed": 495, "track": 77,
                                      "type": "A21N", "reg": "EC-OIL"})
        self.assertEqual(item["flight"], "IBE03VV")
        self.assertEqual(item["alt_baro"], 35000)
        self.assertEqual(item["gs"], 495)
        self.assertEqual(item["source"], "fr24")

    def test_ocean_labels_are_directional(self):
        self.assertEqual(TRACKER.ocean_name(45, -35), "North Atlantic")
        self.assertEqual(TRACKER.ocean_name(-20, 80), "Indian Ocean")

    def test_iso_eta_is_converted_to_remaining_seconds(self):
        self.assertEqual(TRACKER.eta_from_metadata("2026-01-01T01:00:00Z", 1767225600), 3600)


if __name__ == "__main__":
    unittest.main()
