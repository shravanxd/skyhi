import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).parents[1] / "flight-display" / "fr24_poller.py"
SPEC = importlib.util.spec_from_file_location("skyhi_poller", MODULE_PATH)
POLLER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(POLLER)


class HybridSourceTests(unittest.TestCase):
    def test_adsbfi_normalization(self):
        aircraft = POLLER.normalize_adsbfi({
            "hex": "ABC123", "flight": "UAL1 ", "alt_baro": 12000,
            "gs": 310.5, "track": 148, "t": "B38M", "r": "N1",
        })
        self.assertEqual(aircraft["hex"], "abc123")
        self.assertEqual(aircraft["aircraft_type"], "B38M")
        self.assertEqual(aircraft["registration"], "N1")

    def test_local_motion_wins_and_network_identity_survives(self):
        network = [POLLER.normalize_adsbfi({
            "hex": "ABC123", "flight": "UAL1", "gs": 300, "t": "B38M",
        })]
        local = {"aircraft": [{"hex": "abc123", "gs": 321, "seen": 0.1}]}
        merged = POLLER.merge_hybrid(local, network)
        self.assertEqual(merged[0]["gs"], 321)
        self.assertEqual(merged[0]["aircraft_type"], "B38M")
        self.assertEqual(merged[0]["source"], "local+adsb.fi")


if __name__ == "__main__":
    unittest.main()
