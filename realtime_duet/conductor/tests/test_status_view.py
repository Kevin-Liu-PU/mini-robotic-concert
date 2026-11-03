import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from conductor_rt.status_view import latest_snapshot,status_text,TerminalStatus

def snapshot(**changes):
    row=dict(version=1,kind='CLOCK',state='running',beat_position=8.25,estimated_tempo_bpm=60.,tempo_bpm=63.,
             meter=[4,4],source_monotonic_ns=1000000000,quality='tracked')
    row.update(changes);return row

class StatusViewTests(unittest.TestCase):
    def test_running_displays_both_tempo_and_phase_slew_rate(self):
        s=status_text(snapshot(),now_ns=1100000000)
        for value in ('RUNNING','BPM=60.00','rate=63.00','bar=3','beat=1.250','TRACKED'):self.assertIn(value,s)

    def test_ready_count_in_does_not_label_hint_as_measured_tempo(self):
        s=status_text(snapshot(state='ready',quality='unreliable',beat_position=0),startup='count-in',now_ns=1000000000)
        self.assertIn('hint=',s);self.assertIn('not music tempo',s)

    def test_stopped_predicted_stale_are_visible(self):
        s=status_text(snapshot(kind='STOP',state='stopped',quality='predicted'),now_ns=3000000000)
        self.assertIn('STOPPED',s);self.assertIn('PREDICTED',s);self.assertIn('STALE',s)

    def test_tail_handles_partial_write_and_is_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'stream.jsonl'
            raw=json.dumps(snapshot())+'\n'+json.dumps(snapshot(beat_position=9))[:20]
            p.write_text(raw)
            self.assertEqual(latest_snapshot(p)['beat_position'],8.25)
            view=TerminalStatus(p)
            with patch('sys.stdout',io.StringIO()):view.show_once()
            self.assertEqual(p.read_text(),raw)

    def test_missing_invalid_and_completed_snapshot(self):
        self.assertIn('WAITING',status_text(None))
        self.assertIn('INVALID',status_text(snapshot(tempo_bpm=float('nan'))))
        self.assertIn('ENDED',status_text(snapshot(state='ended',beat_position=48),now_ns=1000000000))

if __name__=='__main__':unittest.main()
