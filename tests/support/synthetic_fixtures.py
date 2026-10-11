"""Hand-authored geometry and scalar cases, never sampled from a person.

These fixtures exercise contracts, not accuracy on recorded conducting. Times
are synthetic seconds and coordinates are normalized drawing coordinates.
Expected contacts are derived from the specified line and piecewise segments.
"""
from copy import deepcopy


def scalar_cases():
    specifications = [
        ('distinct_near_main', [.010, .080], [], .070, .01, True),
        ('past_fast_motion_is_not_a_veto', [.012, .075], [], .063, .12, True),
        ('current_radius_boundary', [.030, .110], [], .080, .02, True),
        ('joint_memory_assignment', [.012, .090], [.085, .008], .075, .02, True),
        ('single_main_with_memory', [.015], [.090], None, .02, True),
        ('single_main_without_memory', [.020], [], None, .02, True),
        ('ambiguous_nearby_candidates', [.020, .025], [], .025, .01, False),
        ('current_motion_outside_certificate', [.040, .100], [], .060, .01, False),
        ('single_belongs_to_memory', [.080], [.010], None, .02, False),
    ]
    cases = []
    for name, distances, memory, separation, recent, retained in specifications:
        frame = dict(model_candidates=len(distances), input_dt_s=.04,
                     wrist_distances=[distances], competitor_distances=memory,
                     pair_distance=separation, recent_max_step=recent,
                     competitor_age_s=.08 if memory else None, wrist_limits=[.3])
        cases.append(dict(id=name, expected_retain_main=retained,
                          record=dict(before_main=['A', 1], frame=frame)))
    return dict(source='HAND_AUTHORED_SYNTHETIC',
                evidence_scope='Synthetic scalar contracts; NOT a recorded trajectory replay.',
                cases=cases)


def full_hand(tip_y, dx=0):
    """A simple five-finger drawing; the index flexes independently of the palm."""
    points = [( .50, .76), (.46, .72), (.43, .70), (.41, .68), (.39, .66),
              (.45, .68), (.45, .68 + (tip_y-.68)/3),
              (.45, .68 + 2*(tip_y-.68)/3), (.45, tip_y),
              (.48, .66), (.48, .60), (.48, .54), (.48, .48),
              (.52, .67), (.52, .61), (.52, .56), (.52, .51),
              (.55, .68), (.56, .63), (.57, .59), (.58, .55)]
    landmarks = [dict(x=x+dx, y=y, z=0.) for x,y in points]
    return dict(label='Left', label_score=.9, landmarks=landmarks)


def overlap_rows():
    rows = []
    # A stationary high tip acquires MAIN before a constant-speed downstroke.
    for i in range(31):
        t = i*.04
        y = .30 + max(0, i-19)*.03
        # The final wrist steps exceed the narrow .03 continuity certificate.
        # A translated second hand must therefore remain ambiguous.
        candidate = full_hand(y, dx=max(0, i-26)*.035)
        candidates = [candidate]
        if i == 30:
            alternate = deepcopy(candidate)
            for p in alternate['landmarks']:
                p['x'] += .008
            for k in (6,7,8):
                alternate['landmarks'][k]['y'] += .06
            candidates.append(alternate)
        rows.append(dict(sequence=i, time=t, candidates=candidates))
    following = deepcopy(rows[-1]['candidates'][0])
    for p in following['landmarks']:
        p['x'] += .01
        p['y'] += .04
    rows.append(dict(sequence=31, time=1.24, candidates=[following]))
    # Contact_y=.62-.012. Linear interpolation between .60 and .63.
    return dict(source='HAND_AUTHORED_SYNTHETIC', target_sequence=30,
                expected_contact_time=1.16 + (.608-.60)/.03*.04, rows=rows)


def contact_rows():
    """Ten independent prepared strokes, including six reacquired episodes.

    The first four maintain one track. The remaining six start after an empty
    observation, so each first contact must have no inherited beat interval.
    """
    rows, targets = [], []
    for beat in range(10):
        start = beat*1.5
        if beat >= 4:
            rows.append(dict(time=start-.05, candidates=[]))
        for i in range(29):
            t = start+i*.05
            if i <= 10:
                y = .30
            elif i <= 14:
                y = .30+(i-10)*.10
            elif i <= 18:
                y = .70-(i-14)*.10
            else:
                y = .30
            rows.append(dict(time=t, candidates=[dict(
                wrist=dict(x=.5,y=.75), tip8=dict(x=.5,y=y), label='Left')]))
        targets.append(start+.65+(.608-.60)/.10*.05)
    return dict(source='HAND_AUTHORED_SYNTHETIC', rows=rows,
                targets=targets[4:], prior_accepted=targets[:4])
