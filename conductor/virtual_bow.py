"""Continuous, screen-only bow driven by two observed index fingertips.

No hardware imports, clock predictions, sleeping or work queue. Image y grows
downward. Distances use normalized image coordinates, not physical units.
"""
from dataclasses import asdict, dataclass
import math


@dataclass(frozen=True)
class VirtualBowConfig:
    speed_gain: float = .6
    max_speed: float = .5
    vertical_deadband: float = .025
    smoothing_tau_s: float = .06
    max_age_s: float = .20
    max_gap_s: float = .20
    max_hand_speed: float = 4.0
    max_speed_ratio: float = 3.0
    invert_direction: bool = False

    def __post_init__(self):
        for name, value in asdict(self).items():
            if name == 'invert_direction':
                if type(value) is not bool:
                    raise ValueError('invert_direction must be boolean')
            elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be finite and positive')
        if self.max_speed > 1 or self.max_age_s > .25 or self.max_gap_s > .25:
            raise ValueError('max_speed must be <=1 travel/s; age and gap must be <=0.25 s')
        if self.max_speed_ratio < 1 or self.vertical_deadband >= self.max_hand_speed:
            raise ValueError('Invalid speed ratio or deadband')


@dataclass(frozen=True)
class HandObservation:
    token: object
    t: float
    x: float
    y: float


class VirtualBow:
    def __init__(self, config=None):
        self.config = config or VirtualBowConfig()
        self.position = .5
        self.enabled = True
        self.state, self.reason = 'STOPPED', 'ACQUIRING_TWO_HANDS'
        self.speed = self.direction = 0
        self.measured = {}
        self._previous = None
        self._stamp = self._now = None
        self._watermark = -math.inf
        self._magnitude = 0.
        self.accepted_commands = self.reversals = 0

    def stop(self, reason, *, disable=False):
        self.state, self.reason = 'STOPPED', reason
        self.speed = self.direction = 0
        self._magnitude = 0.
        self._previous = None
        self.measured = {}
        if disable:
            self.enabled = False

    def key(self, key):
        if key in (32, ord('x'), ord('X')):
            self.stop('USER_STOP', disable=True)
        elif key in (10, 13):
            self.enabled = True
            self.stop('ACQUIRING_TWO_HANDS')
        elif key in (ord('r'), ord('R')):
            self.position = .5
            self.stop('RESET_CENTERED', disable=True)

    def _time(self, now):
        if not math.isfinite(now) or (self._now is not None and now < self._now):
            self.stop('INVALID_CLOCK', disable=True)
            return False
        return True

    def advance(self, now):
        """Animate only while the most recent observation remains fresh."""
        if not self._time(now):
            return
        dt = 0 if self._now is None else now-self._now
        self._now = now
        if not self.enabled:
            return
        if self._stamp is not None and now-self._stamp > self.config.max_age_s:
            self.stop('STALE_OBSERVATION')
            return
        # No catch-up integration after a UI stall, even with a new observation.
        if dt > self.config.max_gap_s:
            self.stop('PROCESSING_GAP')
            return
        if self.direction:
            self.position = max(0., min(1., self.position+self.direction*self.speed*dt))
            if self.position in (0., 1.):
                self.speed = self.direction = 0
                self._magnitude = 0.
                self.state, self.reason = 'STOPPED', 'TRAVEL_LIMIT'

    def observe(self, hands, now):
        """Offer one NEW synchronized A/B observation, never repeated UI frames."""
        if not self._time(now):
            return
        cfg = self.config
        if set(hands) != {'A', 'B'}:
            self.stop('TWO_HANDS_REQUIRED')
            self._now = now
            return
        if any(not all(math.isfinite(v) for v in (h.t, h.x, h.y)) or
               not (0 <= h.x <= 1 and 0 <= h.y <= 1) for h in hands.values()):
            self.stop('INVALID_OBSERVATION')
            self._now = now
            return
        a, b = hands['A'], hands['B']
        stamp = a.t
        if a.t != b.t or a.token == b.token:
            self.stop('UNSYNCHRONIZED_OR_AMBIGUOUS_HANDS')
            self._now = now
            return
        if stamp <= self._watermark or not 0 <= now-stamp <= cfg.max_age_s:
            self.stop('STALE_OR_REORDERED_OBSERVATION')
            self._now = now
            return
        self._watermark = stamp
        if self._previous is not None and any(self._previous[s].token != hands[s].token for s in hands):
            self.stop('TRACK_SEGMENT_CHANGED')
        self.advance(now)
        self._stamp = stamp
        if not self.enabled:
            return
        old = self._previous
        self._previous = dict(hands)
        if old is None or any(old[s].token != hands[s].token for s in hands):
            self.speed = self.direction = 0
            self._magnitude = 0.
            self.measured = {}
            self.state, self.reason = 'STOPPED', 'ACQUIRING_TWO_HANDS'
            return
        dt = stamp-old['A'].t
        if not 0 < dt <= cfg.max_gap_s:
            self.stop('OBSERVATION_GAP')
            return
        measured = {}
        for slot, h in hands.items():
            vx, vy = (h.x-old[slot].x)/dt, (h.y-old[slot].y)/dt
            measured[slot] = dict(vx=vx, vy=vy, speed=math.hypot(vx, vy))
        reason = None
        speeds = [m['speed'] for m in measured.values()]
        vertical = [m['vy'] for m in measured.values()]
        if max(speeds) > cfg.max_hand_speed:
            reason = 'IMPLAUSIBLE_HAND_SPEED'
        elif any(abs(v) <= cfg.vertical_deadband for v in vertical):
            reason = 'VERTICAL_DEADBAND'
        elif vertical[0]*vertical[1] < 0:
            reason = 'DIRECTION_DISAGREEMENT'
        elif max(speeds)/min(speeds) > cfg.max_speed_ratio:
            reason = 'SPEED_DISAGREEMENT'
        if reason:
            self.speed = self.direction = 0
            self._magnitude = 0.
            self.state, self.reason = 'STOPPED', reason
            self.measured = measured
            return
        direction = -1 if vertical[0] > 0 else 1
        if cfg.invert_direction:
            direction *= -1
        magnitude = sum(speeds)/2
        if self.direction != direction:
            if self.direction:
                self.reversals += 1
            # Do not let smoothing retain the old sign or delay a real reversal.
            self._magnitude = magnitude
        else:
            alpha = -math.expm1(-dt/cfg.smoothing_tau_s)
            self._magnitude += alpha*(magnitude-self._magnitude)
        self.measured = measured
        if (self.position <= 0 and direction < 0) or (self.position >= 1 and direction > 0):
            self.speed = self.direction = 0
            self._magnitude = 0.
            self.state, self.reason = 'STOPPED', 'TRAVEL_LIMIT'
            return
        self.speed = min(cfg.max_speed, cfg.speed_gain*self._magnitude)
        self.direction = direction
        self.state, self.reason = 'MOVING', 'TWO_HANDS_AGREE'
        self.accepted_commands += 1

    def snapshot(self):
        return dict(mode='VIRTUAL_ONLY', state=self.state, reason=self.reason,
                    enabled=self.enabled, position=self.position, speed=self.speed,
                    direction=self.direction, measured={s:dict(m) for s,m in self.measured.items()},
                    accepted_commands=self.accepted_commands, reversals=self.reversals,
                    config=asdict(self.config))


def update_from_session(bow, session, fresh_result, now):
    """Read existing A/B track segments; never modify session or detector state."""
    if fresh_result is None:
        bow.advance(now)
    elif not fresh_result:
        bow.stop('REJECTED_MODEL_RESULT')
        bow.advance(now)
    else:
        hands = {}
        for slot, track in session.tracker.tracks.items():
            tip = track.hand.index_tip
            if tip is None:
                bow.stop('INVALID_INDEX_TIP')
                bow.advance(now)
                return
            hands[slot] = HandObservation(track.token, track.t, *tip)
        bow.observe(hands, now)


def demo_hands(t):
    """Illustrative mirrored paths, NOT a triangle recognizer or taught gesture."""
    from .synthetic import demo_hand
    phase = t % 5
    if phase < 3:
        x, y = .20+.12*phase/3, .70-.40*phase/3
    elif phase < 4:
        x, y = .32-.22*(phase-3), .30+.40*(phase-3)
    else:
        x, y = .10+.10*(phase-4), .70
    return [demo_hand(x, y, 'Left'), demo_hand(1-x, y, 'Right')]
