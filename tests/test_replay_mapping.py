"""Saved-video replay must preserve source identity across recorded drops."""
import unittest
from tests.support.replay import map_video_observations


class ReplayMappingTests(unittest.TestCase):
    def test_nonzero_source_start_and_queue_gaps_use_mapping_not_decode_count(self):
        source=[dict(sequence=s,capture_monotonic_s=t) for s,t in ((80,10.),(81,10.03),(83,10.1),(90,10.4))]
        mapping=[dict(video_frame_index=i,sequence=s,capture_monotonic_s=t)
                 for i,s,t in ((0,80,10.),(1,82,10.06),(2,83,10.1),(3,90,10.4))]
        targets=map_video_observations(source,mapping)
        self.assertEqual({i:r['sequence'] for i,r in targets.items()},{0:80,2:83,3:90})
        self.assertNotIn(81,[r['sequence'] for r in targets.values()])
    def test_mismatched_timestamp_is_not_silently_replayed(self):
        with self.assertRaises(ValueError):
            map_video_observations([dict(sequence=8,capture_monotonic_s=1.)],
                                   [dict(sequence=8,capture_monotonic_s=2.,video_frame_index=0)])


if __name__=='__main__':unittest.main()
