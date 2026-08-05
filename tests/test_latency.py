import unittest

from zhunt.latency import LatencyTracker


class LatencyTrackerTests(unittest.TestCase):
    def test_observations_are_smoothed_and_snapshot_isolated(self) -> None:
        tracker = LatencyTracker(smoothing=0.5)
        tracker.observe("provider/model", 100.0)
        tracker.observe("provider/model", 20.0)

        snapshot = tracker.snapshot()
        self.assertEqual(snapshot["provider/model"], 60.0)
        snapshot["provider/model"] = 0.0
        self.assertEqual(tracker.snapshot()["provider/model"], 60.0)


if __name__ == "__main__":
    unittest.main()
