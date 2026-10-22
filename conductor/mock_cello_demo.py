"""Mac/Windows no-device proof: python -B -m conductor.mock_cello_demo."""
import json
import math

from .app import update_mock_cello
from .mock_cello import MockCelloControls
from .multitrack import ConductorSession
from .synthetic import demo_hand


def run_demo():
    session = ConductorSession()
    mock = MockCelloControls()
    transitions = [dict(time_s=0, state='DISARMED')]
    for frame in range(241):
        now = frame / 60  # Fake monotonic time; no sleeping or device imports.
        y = session.engine.config.beat_y - .14 + .14 * math.cos(2*math.pi*now)
        event = session.update([demo_hand(.25, y, 'Right')], now)
        anchor = session.beat_anchor
        sample = (now, anchor[0] if anchor else None, anchor[1] if anchor else None)
        update_mock_cello(mock, session, sample, event, now)
        # Scripted, explicit one-shot authorization, once only.
        if frame in (30, 31):
            mock.key(ord('a') if frame == 30 else ord('b'), now)
        if mock.bow.state != transitions[-1]['state']:
            transitions.append(dict(time_s=round(now, 3), state=mock.bow.state))
    mock.stop('DEMO_EXIT')
    return dict(evidence='MOCK ONLY; synthetic observations; no devices or real time waits',
                transitions=transitions, final=mock.snapshot())


if __name__ == '__main__':
    print(json.dumps(run_demo(), indent=2))
