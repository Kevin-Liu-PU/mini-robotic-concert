# conductor

The camera side. It watches the conducting hand and publishes a musical clock
that the instruments follow. It never talks to a robot.

## How beats are read

`conductor_rt/gesture_beats.py` reads the conducting pattern from the pose wrist
alone, in body coordinates (origin between the shoulders, unit = shoulder width):

- every beat but the last is the lowest point of a downward stroke;
- the last beat of the bar is the lift, marked half way up;
- the first lowest point after a lift is beat 1.

So the gesture itself says which beat it is, in three or in four.

`conductor_rt/gesture_clock.py` holds the count-in and the clock. Any bar that
shows beat 1 and then the lift counts as a preparation, and the piece starts on
the next downbeat. The tempo of that bar must lie within 20 % of the tempo hint,
or be confirmed by a second bar. While the piece runs, the clock takes the beat
number from the gesture and the tempo from the same beat in successive bars, and
it predicts the beats to come.

`--beats legacy` selects an earlier detector, count-in and clock.

## Run it

Start the receivers first (see the top-level README). Then:

```sh
conductor/Start-Conductor-Three.command    # three beats in a bar
conductor/Start-Conductor-Four.command     # four beats in a bar
```

Both open the camera and a window, and record the raw camera picture under
`conductor/runs/`, which Git ignores. No preparation gesture is needed: step
into the picture and conduct. The STOP button or Space stops; Q quits.

When the hand is lost the clock keeps its tempo and nothing stops by itself.
