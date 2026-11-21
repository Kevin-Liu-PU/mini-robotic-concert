# Clock messages between the conductor and the instruments

The conductor publishes a musical clock. The instruments listen and follow it.
This note describes the messages and what a receiver does with them.

## Transport

- A receiver listens on `127.0.0.1:8766` by default. The conductor connects and
  sends UTF-8 JSON Lines, one object per line. There is one connection per run,
  and neither side reconnects. Start the receivers first.
- `source_monotonic_ns` is `time.perf_counter_ns()` of the conductor process on
  the same computer. A receiver compares it with its own clock and ignores
  packets older than 500 ms or more than 50 ms in the future. Two hosts are not
  supported.
- A receiver binds to the first valid sender's `session_id` and `clock_domain`
  and ignores everything else.
- The glockenspiel receiver can pass every line on to a second receiver
  (`--forward PORT`) and bring its answers back, because the conductor connects
  to one receiver only.

## Fields

Required in every clock packet: `version` (1), `session_id`, `clock_domain`,
`sequence` (increasing integer), `source_monotonic_ns`, `kind` (`"CLOCK"` or
`"STOP"`), `state` (`"ready"`, `"running"`, `"stopped"` or `"ended"`),
`beat_position` (quarter notes, 0 is the first note of the piece), `tempo_bpm`
(30 to 240), `meter` (`[3,4]` or `[4,4]`) and `quality` (`"tracked"`,
`"predicted"` or `"unreliable"`).

Used when present:

| Field | Use |
| --- | --- |
| `estimated_tempo_bpm` | Duration of the next bow stroke. Falls back to `tempo_bpm`. |
| `phase_rate_bpm` | Slope for extrapolating `beat_position` between packets. Falls back to `tempo_bpm`. |
| `playback_id`, `score_id`, `score_total_beats` and similar | Echoed back in the answers below. |

The update rate should be 20 Hz or more, also while beats are missed.

## What the cello receiver does

- **Before the start.** `ready` packets carry the intended tempo. The arm sets
  the bow down at the start point and prints `ARMED`.
- **Start.** The first `running` packet starts the first bow stroke at once. The
  stroke ends on the next bar line whatever the phase is; a start later than
  three quarters of a bar merges into the following bar.
- **While running.** At each bow change the stroke lasts from the current
  `beat_position` to the next bar line, at `estimated_tempo_bpm`. Nothing is
  read from the clock in the middle of a stroke except stop and pause.
- **Missed beats.** `quality` is logged and otherwise ignored. Deciding that the
  beat is lost is the conductor's job.
- **Jumps.** `beat_position` may jump forward or backward. The next stroke ends
  on the next bar line of the new position; a run never ends because of a jump.
- **Pause.** `running` going back to `ready` stops the bow and holds it on the
  string. If `running` returns within 10 s the arm continues; otherwise it lifts
  and ends.
- **Stop.** A `STOP` packet with `state: "stopped"` stops the bow at once
  (stop, standstill check, lift). With `state: "ended"` a stroke with less than
  0.35 s to go may finish. This is a software request served by a polling loop.
  It is not a safety function.
- **Silence.** No accepted packet for 1.0 s while armed or running, or a closed
  connection, ends the run the normal way: stop, standstill check, lift.
- **End of the score.** When the last stroke completes, the arm lifts. The
  connection stays open for up to 30 s so that a later `STOP` is still
  acknowledged.

None of the above is a safety fault. Safety faults (force, torque, trajectory,
controller state, telemetry) stop the arm, hold it, and command nothing further.

## Answers from the cello receiver

```json
{"version":1,"kind":"ACK","session_id":"...","clock_domain":"...","ack_sequence":205,"status":"stopped_latched"}
{"version":1,"kind":"score_playback_complete","session_id":"...","clock_domain":"...","score_id":"twinkle","score_total_beats":48,"completed_bows":12}
{"version":1,"kind":"executor_failure","session_id":"...","clock_domain":"...","reason":"safety_fault:force_limit"}
```

`ACK` is sent once per `STOP`, when the stop is latched. It reports the software
latch, not a physical standstill; the physical result is in the run folder.
`score_playback_complete` is sent when the last stroke completes.
`executor_failure` is sent only for safety faults.

## Replayable example

`conductor_cello/examples/example_input.jsonl` holds 205 packets at 20 Hz:
ready for 1 s, running at 66 BPM drifting to 70, missed beats over beats 5 to
6.5, and a stop in the middle of bar three. `example_output.json` holds the
three strokes the receiver commanded, the stop report and the answer.

```sh
python conductor_cello/examples/make_example.py
python -m conductor_cello play --mock --source replay --song twinkle \
    --mode contact --stream conductor_cello/examples/example_input.jsonl
```
