"""Conservative overlap hypotheses, not physical hand identity recognition.

Only observed landmarks reach the beat detector. A short velocity extrapolation
scores two observations; it never supplies a coordinate, trail point or beat.
"""
import math

PALM = (0, 5, 9, 13, 17)


def distance(a, b):
    return math.hypot(a[0]-b[0], a[1]-b[1])


def rms(a, b, indices=range(21)):
    return math.sqrt(sum(distance(a[i], b[i])**2 for i in indices)/len(indices))


def geometry(hand):
    """Require usable full observations, excluding old scalar-only fixtures."""
    try:
        if len(hand.landmarks) != 21:
            return None
        points = [(p.x, p.y) for p in hand.landmarks]
        if not all(math.isfinite(v) and 0 <= v <= 1 for p in points for v in p):
            return None
        if not all(math.isfinite(p.z) for p in hand.landmarks):
            return None
        if distance(points[0], (hand.x, hand.y)) > 1e-8:
            return None
        width = distance(points[5], points[17])
        a, b = points[5], points[17]
        wrist = points[0]
        area2 = abs((a[0]-wrist[0])*(b[1]-wrist[1])-(a[1]-wrist[1])*(b[0]-wrist[0]))
        if width < .015 or area2 < .02*width*width:
            return None
        # Every finger needs a nondegenerate observed chain. Foreshortening is
        # allowed, but collapsed placeholders or wildly distorted chains aren't.
        for start in (1, 5, 9, 13, 17):
            length = sum(distance(points[i], points[i+1]) for i in range(start, start+3))
            if not .05*width < length < 5*width:
                return None
        return points, width
    except (AttributeError, TypeError, ValueError):
        return None


def cosine(a, b):
    size = math.hypot(*a)*math.hypot(*b)
    return (a[0]*b[0]+a[1]*b[1])/size if size > 1e-10 else -1


def choose_overlap(old, candidates, t, config, beat_boundaries):
    """Return a unique actual candidate, or None with a diagnostic explanation."""
    details = {}

    def fail(reason):
        details['overlap_check'] = reason
        return None, details

    history = list(old.observations)[-3:]+[(old.t, old.hand)]
    if len(history) < 4 or len(candidates) != 2:
        return fail('requires_full_observation_history')
    shapes = [geometry(h) for _, h in history]+[geometry(h) for h in candidates]
    if any(g is None for g in shapes):
        return fail('invalid_or_degenerate_full_geometry')
    a, b = [g[0] for g in shapes[-2:]]
    widths = [g[1] for g in shapes[-2:]]
    scale = min(widths)
    palm_max = max(distance(a[i], b[i]) for i in PALM)/scale
    shape_rms = rms(a, b)/scale
    shape_max = max(distance(x, y) for x, y in zip(a, b))/scale
    palm_axis = [(p[9][0]-p[0][0], p[9][1]-p[0][1]) for p in (a,b)]
    orientation = cosine(*palm_axis)
    details.update(overlap_palm_max_fraction=palm_max, overlap_shape_rms_fraction=shape_rms,
                   overlap_shape_max_fraction=shape_max, overlap_width_ratio=max(widths)/scale,
                   overlap_orientation_cosine=orientation)
    if (palm_max > config.overlap_palm_fraction or shape_rms > config.overlap_shape_fraction or
            shape_max > 1.25 or max(widths)/scale > 1.25 or orientation < .85):
        return fail('geometry_not_overlapping')
    # Both hypotheses must put the real tip on the same side of every detector
    # boundary. A tie at a boundary is not interchangeable with a crossing.
    if not beat_boundaries or any((a[8][1] >= y) != (b[8][1] >= y) for y in beat_boundaries):
        return fail('tip_actions_disagree')
    prior = [g[0] for g in shapes[:4]]
    deltas = [history[i+1][0]-history[i][0] for i in range(3)]
    dt = t-old.t
    if not 0 < dt <= .12 or any(not 0 < d <= .12 for d in deltas):
        return fail('motion_history_too_sparse')
    velocities = [((prior[i+1][8][0]-prior[i][8][0])/d,
                   (prior[i+1][8][1]-prior[i][8][1])/d) for i,d in enumerate(deltas)]
    if (any(distance(prior[i][8], prior[i+1][8]) < .005 for i in range(3)) or
            any(cosine(v, velocities[-1]) < .8 for v in velocities[:-1])):
        return fail('requires_consistent_observed_motion')
    velocity = velocities[-1]
    expected = (prior[-1][8][0]+velocity[0]*dt, prior[-1][8][1]+velocity[1]*dt)
    errors = [distance(p[8], expected) for p in (a,b)]
    winner = min(range(2), key=errors.__getitem__)
    alternate = 1-winner
    details.update(overlap_motion_errors=errors, overlap_motion_margin=errors[alternate]-errors[winner])
    if (errors[alternate]-errors[winner] < config.overlap_motion_margin*scale or
            errors[alternate] < config.overlap_motion_ratio*max(errors[winner], 1e-10)):
        return fail('motion_hypotheses_not_distinguishable')
    points = (a,b)[winner]
    step = (points[8][0]-prior[-1][8][0], points[8][1]-prior[-1][8][1])
    # Keep direction, wrist, palm and complete geometry as independent checks.
    # A lower tip error alone cannot select an unrelated/poor hand hypothesis.
    full = [rms(prior[-1], p) for p in (a,b)]
    palm = [rms(prior[-1], p, PALM) for p in (a,b)]
    wrist = [distance(prior[-1][0], p[0]) for p in (a,b)]
    details.update(overlap_full_steps=full, overlap_palm_steps=palm, overlap_wrist_steps=wrist)
    if (cosine(step, velocity) < .8 or full[winner] > full[alternate] or
            palm[winner] > palm[alternate] or wrist[winner] > wrist[alternate] or
            distance(points[8], prior[-1][8]) > .30):
        return fail('geometry_and_motion_support_disagree')
    # Both observed tips must move in the same vertical direction, not just
    # happen to be below the contact line during two different actions.
    moves = [p[8][1]-prior[-1][8][1] for p in (a,b)]
    if moves[0]*moves[1] <= 0:
        return fail('tip_actions_disagree')
    details['overlap_check'] = 'supported_overlap_hypothesis'
    return winner, details


def strongly_matches_alternative(main, alternative, observed):
    """Do not silently inherit a previously rejected complete pose.

    This is an observation uncertainty check, not a remembered second identity.
    Ordinary movement toward the old alternative's tip is insufficient to veto.
    """
    shapes = [geometry(h) for h in (main, alternative, observed)]
    if any(g is None for g in shapes):
        return True  # Full evidence was required to enter this mode.
    a,b,c = [g[0] for g in shapes]
    main_error, alternate_error = rms(a,c), rms(b,c)
    scale = min(g[1] for g in shapes)
    return (main_error-alternate_error >= .15*scale and
            main_error >= 2*max(alternate_error, 1e-10))
