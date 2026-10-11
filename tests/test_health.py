import unittest
from conductor.health import FrameFreshness


class FreshnessTests(unittest.TestCase):
    def test_successful_repeated_reads_are_stale(self):
        health=FrameFreshness()
        self.assertTrue(health.observe(b"first",10))
        for t in (10.1,10.2,10.3,11,12):
            self.assertFalse(health.observe(b"first",t))
        self.assertTrue(health.stale(12,0.25))
        self.assertEqual(health.last_unique_time,10)

    def test_new_frame_recovers_freshness(self):
        health=FrameFreshness()
        health.observe(b"a",0)
        self.assertTrue(health.stale(1,0.25))
        health.observe(b"b",1)
        self.assertFalse(health.stale(1.01,0.25))

    def test_no_frame_is_not_live(self):
        self.assertTrue(FrameFreshness().stale(0,0.25))
