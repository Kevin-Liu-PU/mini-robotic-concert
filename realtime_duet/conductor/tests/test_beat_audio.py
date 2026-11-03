import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import numpy as np
from conductor_rt.beat_audio import ClickRenderer, BeatAudio
from conductor_rt.clock import MusicalClock
from conductor_rt.protocol import WireEncoder
from conductor_rt.transport import Publisher


def event(seq, kind, index=0, meter=3, now=1_000_000_000, state=None):
    return dict(session_id='test', seq=seq, kind=kind,
        state=state or ('running' if kind in ('start','beat') else 'stopped'),
        beat_index=index, meter=meter, monotonic_ns=now-1_000_000,
        publisher_monotonic_ns=now)


class ClickTests(unittest.TestCase):
    def render(self, audio, now=1_003_000_000, frames=2048):
        buffer=np.empty((frames,1),dtype='float32')
        audio.render(buffer,frames,now,now+5_000_000)
        return buffer

    def test_ready_arming_and_count_in_do_not_synthesize_beats(self):
        audio=ClickRenderer()
        audio.submit_published(event(1,'armed',state='armed'))
        self.assertFalse(self.render(audio).any())
        self.assertFalse(audio.records)

    def test_three_and_four_meter_accent_only_published_beats(self):
        for meter in (3,4):
            audio=ClickRenderer();audio.submit_published(event(1,'start',meter=meter))
            peaks=[]
            for index in range(2*meter):
                stamp=1_000_000_000+index*400_000_000
                audio.submit_published(event(index+2,'beat',index,meter,stamp))
                peaks.append(float(np.abs(self.render(audio,stamp+1_000_000)).max()))
            self.assertEqual([r['accented'] for r in audio.records],[i%meter==0 for i in range(2*meter)])
            self.assertGreater(peaks[0],peaks[1]);self.assertLessEqual(max(peaks),.12)
            self.assertTrue(all(r['estimated_dac_ns']>r['published_ns'] for r in audio.records))

    def test_duplicate_and_missing_beat_indices_do_not_create_extra_sound(self):
        audio=ClickRenderer();audio.submit_published(event(1,'start'))
        audio.submit_published(event(2,'beat',0));self.render(audio)
        audio.submit_published(event(2,'beat',0))
        audio.submit_published(event(3,'beat',3));self.render(audio)
        self.assertEqual([r['beat_index'] for r in audio.records],[0,3])
        self.assertEqual(audio.duplicates,1)

    def test_click_continues_across_callbacks_exactly_once(self):
        audio=ClickRenderer();audio.submit_published(event(1,'start'))
        audio.submit_published(event(2,'beat'))
        blocks=[self.render(audio,frames=128) for _ in range(12)]
        wave=audio.waves[1]
        np.testing.assert_array_equal(np.concatenate(blocks)[:len(wave)],wave)
        self.assertFalse(np.concatenate(blocks)[len(wave):].any())
        self.assertEqual(audio.records[0]['rendered_samples'],len(wave))

    def test_stop_cancels_current_and_pending_sound(self):
        audio=ClickRenderer();audio.submit_published(event(1,'start'))
        audio.submit_published(event(2,'beat'));self.render(audio,frames=128)
        audio.submit_published(event(3,'beat',1));audio.submit_published(event(4,'stop'))
        self.assertFalse(self.render(audio).any())
        self.assertEqual([r['status'] for r in audio.records],['cancelled_after_render','cancelled_before_render'])

    def test_pause_then_start_cannot_play_old_generation(self):
        audio=ClickRenderer();audio.submit_published(event(1,'start'))
        audio.submit_published(event(2,'beat'))
        audio.submit_published(event(3,'tracking_pause',state='paused'))
        audio.submit_published(event(4,'start'))
        self.assertFalse(self.render(audio).any())
        audio.submit_published(event(5,'beat'));self.assertTrue(self.render(audio).any())

    def test_stale_and_queue_overflow_drop_without_catchup_burst(self):
        audio=ClickRenderer();audio.submit_published(event(1,'start'))
        for seq in range(2,8):audio.submit_published(event(seq,'beat',seq-2))
        self.assertEqual(len(audio.pending),4);self.assertEqual(audio.overflow,2)
        self.assertFalse(self.render(audio,1_200_000_000).any())
        self.assertEqual(audio.stale,4)
        audio.submit_published(event(8,'beat',8,now=1_300_000_000))
        audio.submit_published(event(9,'beat',9,now=1_300_000_000))
        self.render(audio,1_301_000_000)
        self.assertEqual(sum(r['callback_ns'] is not None for r in audio.records),1)

    def test_output_only_backend_and_timer_mapping(self):
        class Stream:
            time=time.perf_counter()
            latency=.006
            cpu_load=.001
            def start(self):pass
            def abort(self):pass
            def close(self):pass
        with tempfile.TemporaryDirectory() as folder, \
                patch('sounddevice.query_devices',return_value={'name':'test output'}), \
                patch('sounddevice.OutputStream',return_value=Stream()) as output, \
                patch('sounddevice.InputStream') as input_stream:
            audio=BeatAudio(Path(folder));audio.start();audio.close()
            input_stream.assert_not_called()
            self.assertEqual(output.call_args.kwargs['channels'],1)
            self.assertEqual(output.call_args.kwargs['blocksize'],0)
            self.assertTrue(json.loads((Path(folder)/'audio-metrics.json').read_text())['output_only'])

    def test_publisher_retains_events_when_snapshots_coalesce(self):
        with tempfile.TemporaryDirectory() as folder:
            audio=ClickRenderer();publisher=Publisher(Path(folder),audio=audio)
            clock=MusicalClock(120,3,session_id='test',score_beats=None);encoder=WireEncoder('local-test')
            clock.arm(1,'explicit_test_start');outgoing=clock.start(1,'explicit_test_start')
            publisher.offer(encoder.encode(clock.message(1)),outgoing)
            publisher.offer(encoder.encode(clock.message(1.1)))
            publisher.thread.start();publisher.close()
            rows=[json.loads(s) for s in (Path(folder)/'published-events.jsonl').read_text().splitlines()]
            self.assertIsNone(publisher.error)
            self.assertEqual([r['kind'] for r in rows],['start','beat'])
            self.assertEqual(len(audio.records),1)
            self.assertEqual(audio.records[0]['published_ns'],rows[1]['publisher_monotonic_ns'])
            self.assertEqual(publisher.coalesced,1)

    def test_finished_dashboard_uses_final_audio_off_state(self):
        from conductor_rt.dashboard import Dashboard
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder);(output/'conductor').mkdir()
            (output/'conductor/metrics.json').write_text(json.dumps(dict(final_state='stopped',
                final_bpm=120,final_phase=3,audio=dict(enabled=True,active=False))))
            dashboard=Dashboard.__new__(Dashboard);dashboard.output=output;dashboard.preview_label=None
            dashboard.finish(dict(sender_exit=0))
            self.assertFalse(dashboard.finished['audio']['active'])


if __name__=='__main__':unittest.main()
