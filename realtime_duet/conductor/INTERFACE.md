# Shared Conductor/cello wire contract v1

Accepted parent contract. Default transport is local-loopback TCP JSON Lines; Python objects/file logs are used for offline tests. time.perf_counter_ns() is the common clock on this Windows host. Video PTS is stored separately, never substituted for monotonic time. Both sides explicitly select the same clock_domain and session_id.

{"version":1,"session_id":"uuid","clock_domain":"shared-local-run","sequence":42,"source_monotonic_ns":123456789000,"kind":"CLOCK","state":"running","beat_position":8.25,"tempo_bpm":76.0,"meter":[4,4],"quality":"tracked"}

- kind: CLOCK / STOP. state: ready / running / stopped / ended. quality: tracked / predicted / unreliable. Sequence increases; beat_position is continuous zero-based quarter-note position and never moves backward within a session.
- tempo_bpm is the instantaneous phase slope, including bounded phase slew. Optional estimated_tempo_bpm reports the independent stable estimate. A receiver can extrapolate the continuous phase using tempo_bpm.
- Initial tempo is mandatory --initial-bpm; no implicit 120. Missing vision keeps the clock running at reduced quality. Explicit STOP freezes phase. Stop/end cannot restart in the same session.
- Sender repeats STOP until matching software ACK: {"version":1,"kind":"ACK","session_id":"uuid","ack_sequence":42,"status":"stopped_latched"}. ACK is not evidence of physical stopping. Explicit test termination without a connected receiver reports the lack of ACK.
- Receiver starts DISARMED/dry-run and independently requires arm. Validate domain/session, finite values, freshness, sequence and position. STOP bypasses normal deduplication and latches. Communication timeout clears pending actions and latches a fault; reconnection alone never resumes.
- Only the newest unstarted CLOCK snapshot is retained. STOP takes priority. Music-clock thread never performs model calls, GUI, file or socket I/O. Sequence gaps are legal; never burst-replay missed notes.
- Twinkle score interval [0,48): 12 cello onsets at 0,4,...44, duration four beats. At 48 conductor sends STOP with state ended.
- events.jsonl logs internal predicted beats. observations.jsonl logs visual detections and admission decisions separately. Continuous predictions are never counted as correct detections.

Entrypoint: python -B -m conductor_rt --help in this folder. Independent of frozen V3. File replay never accesses camera or hardware.
