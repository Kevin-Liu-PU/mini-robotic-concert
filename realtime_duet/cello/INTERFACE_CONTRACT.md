# Conductor → cello interface contract v1

Matches the parent task's shared protocol. UTF-8 JSON Lines on loopback TCP, documented port 8766; also Python objects and stdin. Both processes must run on the same host (target macOS; Windows development tests only) and use `time.perf_counter_ns()` and the same explicit `clock_domain`. Freshness checks detect gross clock mismatch; this is not a cross-host clock synchronization protocol. Video PTS is not the clock timestamp.

```json
{"version":1,"session_id":"unique-run-id","clock_domain":"shared-local-run","sequence":42,"source_monotonic_ns":123456789000,"kind":"CLOCK","state":"running","beat_position":8.25,"tempo_bpm":76.0,"meter":[4,4],"quality":"tracked"}
```

- `sequence`: nonnegative integer increasing across all messages of a session.
- `kind`: CLOCK or STOP. `state`: ready / running / stopped / ended. `quality`: tracked / predicted / unreliable.
- `beat_position`: continuous quarter-note position at the source timestamp; zero is first cello note. It never retreats within a session. No count-in negatives in this contract: hold `ready` at phase 0 until the start cue.
- `tempo_bpm`: explicit finite 30–240 BPM for mock. No default startup BPM. This range is not a verified hardware envelope.
- CLOCK requires all listed fields. STOP requires version, session_id, clock_domain, sequence, source_monotonic_ns, kind and state; timing/quality fields may be included. Matching STOP precedes ordinary sequence/freshness rejection.
- Consumer defaults to DISARMED and mock. `--arm` alone enables only software scheduling. Selecting a session does not arm. Implemented hardware bridge additionally requires a reviewed configuration/hash, applicable onsite gate, --execute-hardware and --attended.
- Producer sends ≥20 Hz CLOCK heartbeats, including while vision is lost (`quality: predicted`). Consumer extrapolates between messages, so brief visual loss does not create Stop.
- A single latest CLOCK slot prevents queue backlog; a separate priority STOP latch clears that slot. Duplicates, older sequence, backward source timestamp/phase, wrong session/domain, stale (>500 ms) and future (>50 ms) CLOCK are ignored. Only accepted CLOCK refreshes heartbeat.
- Default `--clock-timeout-ms 1000` terminally faults. Local tick stall >250 ms, discontinuity >0.5 beat, invalid matching payload, EOF and disconnect also latch. No reconnect/resume. Receiver never rewrites source timestamps to hide delay.
- First running snapshot must be at phase ≤0.1, including its extrapolated phase at receipt, whether or not READY was previously seen. The first actual bow tick also rechecks ≤0.1; a late tick cannot catch up from an old start. Each of 12 notes starts once at phases 0,4,...44 and lasts 4 beats; score ends at 48. Later boundary lateness >0.5 beat faults; no catch-up burst.
- Durable journal reserves session before first bow. Restart cannot execute the same session; use an explicit new session/start cue. Do not delete journal to resume interrupted physical motion.
- Matching STOP yields `{"version":1,"kind":"ACK","session_id":"...","ack_sequence":42,"status":"stopped_latched"}`. ACK means software latch only. TCP then closes; sender should stop repeating after ACK or disconnection.
- No explicit start anchor field exists in agreed v1. First accepted running sample near phase zero anchors performance. Session+domain+timestamp+phase bind the clock.

Mock continuously follows phase during a bow. Repository blocking moveL fixes each bow's timing at launch; `ServoStrokePlayer.play_stroke` permits speed/depth updates but fixes duration. The new `PhaseServo` bridge supports within-bow phase progression and cancellation, using the independently adapted pure geometry and statically read calibrated data and explicit limits; see HARDWARE_BRIDGE.md. It is fake-driver tested, not physically commissioned.

Teacher cello pitches: G2,D3,C2,G2,D3,D3,D3,D3,G2,D3,C2,G2. Open A is A3. Mock events represent rhythm only; A3 substitution was not yet approved at implementation time. No fixed performance BPM is selected.
