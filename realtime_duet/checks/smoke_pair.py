"""Pure clock + localhost MOCK smoke; no vision/model/device imports."""
import argparse
import json
from pathlib import Path
import platform
import socket
import subprocess
import sys
import time
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receiver-python', default=sys.executable)
    parser.add_argument('--port', type=int, default=18766)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(root/'conductor'))
    from conductor_rt.clock import MusicalClock
    from conductor_rt.protocol import WireEncoder
    session = 'smoke-' + uuid.uuid4().hex
    domain = 'local-' + session
    command = [args.receiver_python, '-B', '-m', 'cello_adapter', '--backend', 'mock',
               '--session', session, '--clock-domain', domain, '--listen', str(args.port),
               '--arm', '--journal', str(out/'journal'), '--log', str(out/'bows.jsonl')]
    with (out/'receiver.log').open('w', encoding='utf-8') as log:
        process = subprocess.Popen(command, cwd=root/'cello', stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.perf_counter()+5
            while True:
                if process.poll() is not None: raise RuntimeError('mock receiver exited before readiness')
                if 'MOCK ready on' in (out/'receiver.log').read_text(encoding='utf-8'): break
                if time.perf_counter()>deadline: raise TimeoutError('mock receiver readiness timeout')
                time.sleep(.02)
            clock = MusicalClock(60, session_id=session)
            encoder = WireEncoder(domain)
            with socket.create_connection(('127.0.0.1', args.port), timeout=2) as connection:
                def send(snapshot):
                    connection.sendall((json.dumps(encoder.encode(snapshot))+'\n').encode())
                now = time.perf_counter_ns()/1e9
                for snapshot in clock.arm(now): send(snapshot)
                for snapshot in clock.start(now, reason='explicit_test_start'): send(snapshot)
                end = now+.5
                while time.perf_counter_ns()/1e9 < end:
                    now = time.perf_counter_ns()/1e9
                    clock.tick(now)
                    send(clock.message(now))
                    time.sleep(.02)
                for snapshot in clock.stop(time.perf_counter_ns()/1e9, reason='explicit_test_stop'): send(snapshot)
                line = connection.makefile('r', encoding='utf-8').readline()
                ack = json.loads(line)
                assert ack['kind']=='ACK' and ack['session_id']==session and ack['status']=='stopped_latched'
            code = process.wait(timeout=3)
            assert code == 0, ('receiver exit', code)
            events = [json.loads(x) for x in (out/'bows.jsonl').read_text(encoding='utf-8').splitlines()]
            starts = sum(e['kind']=='bow_start' for e in events)
            assert starts == 1, events
            assert events[-1]['kind']=='stop' and events[-1]['reason']=='explicit_stop', events[-1]
            forbidden = {'cv2','mediapipe','rtde_control','rtde_receive','sounddevice','soundfile','mido'}
            assert not forbidden.intersection(sys.modules)
            report = dict(status='PASS', platform=platform.system(), port=args.port, bow_starts=starts,
                          receiver_exit=code, stop_ack=ack['status'], hardware_enabled=False,
                          camera_opened=False, model_loaded=False, source='synthetic explicit 60 BPM',
                          claim='Protocol/mock smoke only, not camera or physical latency validation.')
            (out/'result.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
            print(json.dumps(report,indent=2))
            return 0
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)


if __name__ == '__main__': raise SystemExit(main())
