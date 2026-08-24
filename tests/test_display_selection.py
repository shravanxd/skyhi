import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).parents[1] / "flight-display" / "skyhi_display.py"
SPEC = importlib.util.spec_from_file_location("skyhi_display", MODULE_PATH)
DISPLAY = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(DISPLAY)


class DisplaySelectionTests(unittest.TestCase):
    def test_slow_low_aircraft_is_small(self):
        self.assertTrue(DISPLAY.is_small_aircraft({"gs": 105, "alt_baro": 3500}))

    def test_fast_or_high_aircraft_is_not_small(self):
        self.assertFalse(DISPLAY.is_small_aircraft({"gs": 420, "alt_baro": 8000}))
        self.assertFalse(DISPLAY.is_small_aircraft({"gs": 150, "alt_baro": 18000}))

    def test_missing_telemetry_is_not_assumed_small(self):
        self.assertFalse(DISPLAY.is_small_aircraft({"gs": None, "alt_baro": 3000}))


if __name__ == "__main__":
    unittest.main()
