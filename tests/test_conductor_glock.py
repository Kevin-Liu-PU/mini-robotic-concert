"""Offline tests for the glockenspiel that follows the Conductor. No serial port is opened."""
import json
import socket
import threading
import time
from pathlib import Path

import pytest

from conductor_cello.clock import ConductorClock
from conductor_cello.driver import FakeTimebase
from conductor_cello.wire import ScriptedSource, metronome_packets
from conductor_glock.player import Player
from conductor_glock.ports import DryPort
from conductor_glock.relay import Relay
from conductor_glock.score import MIN_GAP_S, NOTES, load

REPO = Path(__file__).resolve().parents[1]


def test_scores_are_the_bundled_parts():
    arirang, twinkle = load("arirang"), load("twinkle")
    assert (arirang.beats, len(arirang.notes)) == (48.0, 53) and (twinkle.beats, len(twinkle.notes)) == (48.0, 42)
    assert all(1 <= n.channel <= 8 and NOTES[n.channel - 1] == n.name for n in arirang.notes + twinkle.notes)
    assert arirang.notes[0].beat == 0 and arirang.notes[0].name == "D4"
    assert twinkle.fastest_tempo() > 300          # the solenoid recovery never limits these parts
    with pytest.raises(ValueError):
        load("mary")


def play(packets, song="arirang", *, step_s=0.002, **player_kwargs):
    """Run the player against a scripted Conductor on a fake clock. Returns [(time, note, phase)]."""
    tb = FakeTimebase()
    clock = ConductorClock()
    src = ScriptedSource(clock, packets, start_s=tb.now())
    player = Player(load(song), **player_kwargs)
    t0 = tb.now()
    struck, status = [], "waiting"
    while tb.now() - t0 < 200:
        src.step(tb.now())
        due, status = player.step(clock.view(), tb.now())
        struck += [(tb.now() - t0, note, phase) for note, phase, late in due]
        if status not in ("waiting", "playing", "holding", "finished"):
            break
        tb.sleep(step_s)
    return struck, status, player


@pytest.mark.parametrize("song,meter,bpm", [("arirang", 3, 70), ("twinkle", 4, 70), ("arirang", 3, 96)])
def test_every_note_is_struck_once_on_its_beat(song, meter, bpm):
    ready = 2.0
    pk = list(metronome_packets(bpm=bpm, meter=meter, total_beats=48, ready_s=ready))
    struck, status, player = play(pk, song)
    score = load(song)
    assert status == "stop:ended"
    assert [n for _, n, _ in struck] == list(score.notes)
    for t, note, _ in struck:
        assert t - (ready + note.beat * 60 / bpm) == pytest.approx(0.0, abs=0.012)     # within a few polls of the beat
    assert player.tally.dropped_late == 0 and player.tally.dropped_recovery == 0


def test_nothing_is_struck_before_the_piece_starts_or_after_stop():
    pk = [(t, p) for t, p in metronome_packets(bpm=70, meter=3, total_beats=48, ready_s=5.0)]
    cut = [(t, p) for t, p in pk if t < 5.0 + 10.0]
    stop = dict(cut[-1][1], kind="STOP", state="stopped", sequence=cut[-1][1]["sequence"] + 1)
    struck, status, player = play(cut + [(cut[-1][0] + 0.02, stop)])
    assert status == "stop:stopped"
    assert min(t for t, _, _ in struck) >= 5.0 - 0.001
    assert max(t for t, _, _ in struck) <= 5.0 + 10.0 + 0.02
    assert 0 < len(struck) < len(load("arirang").notes)


def test_conductor_going_silent_or_away_ends_the_playing():
    pk = [(t, p) for t, p in metronome_packets(bpm=70, meter=3, total_beats=48, ready_s=2.0) if t < 2.0 + 6.0]
    struck, status, player = play(pk)
    assert status == "silent" or status.startswith("gone:")
    assert max(t for t, _, _ in struck) < 2.0 + 6.0 + 1.1


def test_a_jump_ahead_drops_old_notes_instead_of_playing_a_burst():
    pk = []
    for t, p in metronome_packets(bpm=70, meter=3, total_beats=48, ready_s=2.0):
        if p.get("state") == "running" and p["beat_position"] >= 9.0:
            p = dict(p, beat_position=p["beat_position"] + 6.0)            # the clock re-locks two bars ahead
        pk.append((t, p))
    struck, status, player = play(pk)
    times = [t for t, _, _ in struck]
    # the note just before the jump and the one the clock landed on may be close together;
    # the seven it flew past are not played at all
    assert all(c - a > 0.25 for a, c in zip(times, times[2:]))             # never three in a quarter second
    assert player.tally.dropped_late == 7
    played = [n.beat for _, n, _ in struck]
    assert not any(9.4 < b < 14.9 for b in played)                          # the skipped bars stay silent


def test_a_step_back_replays_nothing():
    pk = []
    for t, p in metronome_packets(bpm=70, meter=3, total_beats=48, ready_s=2.0):
        if p.get("state") == "running" and p["beat_position"] >= 12.0:
            p = dict(p, beat_position=p["beat_position"] - 2.0)
        pk.append((t, p))
    struck, status, player = play(pk)
    beats = [n.beat for _, n, _ in struck]
    assert beats == sorted(beats) and len(beats) == len(set((n.beat, n.name) for _, n, _ in struck))


def test_pause_holds_and_resume_continues():
    pk = []
    for t, p in metronome_packets(bpm=70, meter=3, total_beats=48, ready_s=2.0):
        if 10.0 <= t < 13.0 and p.get("kind") == "CLOCK":
            p = dict(p, state="ready", beat_position=round((10.0 - 2.0) * 70 / 60, 6))
        elif t >= 13.0 and p.get("state") == "running":
            p = dict(p, beat_position=p["beat_position"] - 3.0 * 70 / 60)
        pk.append((t, p))
    struck, status, player = play(pk)
    assert not any(10.05 < t < 12.95 for t, _, _ in struck)
    assert [n for _, n, _ in struck][:20] == list(load("arirang").notes)[:20]


def test_lead_strikes_before_the_beat():
    pk = list(metronome_packets(bpm=60, meter=4, total_beats=48, ready_s=2.0))
    struck, _, _ = play(pk, "twinkle", lead_s=0.040)
    second = next(t for t, n, _ in struck if n.beat == 1.0)
    assert second == pytest.approx(2.0 + 1.0 - 0.040, abs=0.012)


def test_same_solenoid_is_never_struck_inside_its_recovery():
    score = load("twinkle")
    clock = ConductorClock()
    player = Player(score)
    now = 100.0
    def packet(seq, beat, t):
        return dict(version=1, session_id="s", clock_domain="d", sequence=seq, source_monotonic_ns=int(t * 1e9),
                    kind="CLOCK", state="running", beat_position=beat, tempo_bpm=240.0, meter=[4, 4], quality="tracked")
    clock.ingest(packet(1, 0.0, now), now)
    first, _ = player.step(clock.view(), now)
    clock.ingest(packet(2, 1.2, now + 0.05), now + 0.05)                     # beat 1 (same pitch) only 50 ms later
    second, _ = player.step(clock.view(), now + 0.05)
    assert len(first) == 1 and second == [] and player.tally.dropped_recovery == 1
    assert 0.05 < MIN_GAP_S


# ── the relay, on real loopback sockets ──────────────────────────────

def lines_from(sock, seconds):
    sock.settimeout(0.05)
    end, buf = time.time() + seconds, b""
    while time.time() < end:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            break
        buf += chunk
    return [json.loads(x) for x in buf.split(b"\n") if x.strip()]


def clock_packet(seq, state="running", kind="CLOCK", beat=1.0):
    return dict(version=1, session_id="relay-test", clock_domain="local-perf-counter", sequence=seq,
                source_monotonic_ns=time.perf_counter_ns(), kind=kind, state=state, beat_position=beat,
                tempo_bpm=70.0, meter=[3, 4], quality="tracked")


def test_relay_alone_acknowledges_stop_itself():
    clock = ConductorClock()
    relay = Relay(clock, listen_port=0).start()
    try:
        up = socket.create_connection(("127.0.0.1", relay.port), 2)
        up.sendall((json.dumps(clock_packet(1)) + "\n").encode())
        up.sendall((json.dumps(clock_packet(2, "stopped", "STOP")) + "\n").encode())
        back = lines_from(up, 0.5)
        assert [m["kind"] for m in back] == ["ACK"] and back[0]["status"] == "stopped_latched" and back[0]["ack_sequence"] == 2
        assert clock.view()[1]["state"] == "stopped"
        up.close()
    finally:
        relay.close()


def test_relay_passes_every_line_on_and_brings_the_answer_back():
    downstream = socket.socket(); downstream.bind(("127.0.0.1", 0)); downstream.listen(1)
    got = {}

    def cello():
        conn, _ = downstream.accept()
        got["lines"] = lines_from(conn, 0.6)
        stop = next(m for m in got["lines"] if m["kind"] == "STOP")
        conn.sendall((json.dumps(dict(version=1, kind="ACK", session_id=stop["session_id"], clock_domain=stop["clock_domain"],
                                      ack_sequence=stop["sequence"], status="stopped_latched")) + "\n").encode())
        time.sleep(0.2)
        conn.close()
    t = threading.Thread(target=cello); t.start()
    clock = ConductorClock()
    relay = Relay(clock, listen_port=0, forward_port=downstream.getsockname()[1]).start()
    try:
        up = socket.create_connection(("127.0.0.1", relay.port), 2)
        sent = [clock_packet(1), clock_packet(2, beat=1.02), clock_packet(3, "stopped", "STOP")]
        for m in sent:
            up.sendall((json.dumps(m) + "\n").encode())
        back = lines_from(up, 1.2)
        t.join(3)
        assert got["lines"] == sent                                    # unchanged, in order
        assert [m["kind"] for m in back] == ["ACK"] and back[0]["ack_sequence"] == 3      # the cello's, not ours
        assert relay.acked == set() and relay.forwarded == 3 and relay.returned == 1
        assert clock.accepted == 2 and clock.view()[1] is not None
    finally:
        relay.close(); downstream.close()


def test_when_the_next_receiver_goes_the_conductor_side_is_closed_too():
    downstream = socket.socket(); downstream.bind(("127.0.0.1", 0)); downstream.listen(1)
    def cello():
        conn, _ = downstream.accept(); time.sleep(0.2); conn.close()
    t = threading.Thread(target=cello); t.start()
    clock = ConductorClock()
    relay = Relay(clock, listen_port=0, forward_port=downstream.getsockname()[1]).start()
    try:
        up = socket.create_connection(("127.0.0.1", relay.port), 2)
        up.sendall((json.dumps(clock_packet(1)) + "\n").encode())
        t.join(3)
        up.settimeout(2.0)
        assert up.recv(16) == b""                                      # closed from our side: the Conductor will stop
        assert clock.view()[3] is not None
    finally:
        relay.close(); downstream.close()


def test_relay_refuses_to_start_without_its_next_receiver():
    spare = socket.socket(); spare.bind(("127.0.0.1", 0)); port = spare.getsockname()[1]; spare.close()
    with pytest.raises(OSError):
        Relay(ConductorClock(), listen_port=0, forward_port=port)


def test_dry_port_records_and_ends_with_all_off():
    port = DryPort()
    port.strike(3); port.strike(8); port.close(); port.close()
    assert [c for _, c in port.sent] == ["3", "8", "0"]
    with pytest.raises(ValueError):
        DryPort().strike(9)


def test_command_line_needs_dry_run_or_go(monkeypatch, tmp_path, capsys):
    import conductor_glock.__main__ as cli
    monkeypatch.setattr(cli, "RUNS", tmp_path)
    with pytest.raises(SystemExit) as stop:
        cli.main(["play", "--song", "arirang", "--listen", "0"])
    assert "REFUSED" in str(stop.value)
    with pytest.raises(SystemExit) as stop:
        cli.main(["check"])
    assert "REFUSED" in str(stop.value)


# ── the handshake with the real controller, on a scripted serial port ──
class FakeSerial:
    """Stands in for serial.Serial: answers every '?' with `reply`, records every byte written."""
    reply = b""
    written = b""

    def __init__(self, device, baud, **kw):
        self.device, self.is_open, self._pending = device, True, b""
        type(self).written = b""

    def reset_input_buffer(self):
        self._pending = b""

    def write(self, data):
        type(self).written += bytes(data)
        if data == b"?":
            self._pending += self.reply
        return len(data)

    @property
    def in_waiting(self):
        return len(self._pending)

    def read(self, n):
        out, self._pending = self._pending[:n], self._pending[n:]
        return out

    def close(self):
        self.is_open = False


def open_port(monkeypatch, reply):
    import serial
    from conductor_glock.ports import ProtonPort
    FakeSerial.reply = reply
    monkeypatch.setattr(serial, "Serial", FakeSerial)
    return ProtonPort("/dev/fake", ready_timeout_s=0.3)


def test_the_handshake_sends_all_off_and_a_question_and_never_a_strike(monkeypatch):
    port = open_port(monkeypatch, b"READY:ARIA8\n")
    assert set(FakeSerial.written) <= set(b"0?") and FakeSerial.written.startswith(b"0")
    port.close()


def test_a_failed_handshake_says_what_the_controller_sent(monkeypatch):
    # the message must say what the board answered, not only that READY:ARIA8 had not come
    with pytest.raises(RuntimeError) as silent:
        open_port(monkeypatch, b"")
    assert "It sent nothing at all." in str(silent.value)
    with pytest.raises(RuntimeError) as other:
        open_port(monkeypatch, b"HELLO 1.2\n")
    assert "HELLO 1.2" in str(other.value)
    assert set(FakeSerial.written) <= set(b"0?")                    # still nothing but all-off and the question


def test_the_firmware_before_the_package_is_accepted_as_its_own_player_accepts_it(monkeypatch):
    # a board with the earlier firmware answers the question with an ADC reading, not READY:ARIA8
    port = open_port(monkeypatch, b"ADC:2047\n")
    assert port.firmware == "before-package" and port.answer == "ADC:2047"
    assert set(FakeSerial.written) <= set(b"0?")
    port.strike(3)
    assert FakeSerial.written.endswith(b"3")                         # the same character as for the packaged firmware
    port.close()
    assert open_port(monkeypatch, b"READY:ARIA8\n").firmware == "aria8"
    for junk in (b"ADC:99999\n", b"ADC:x\n"):                        # not a reading: not ready
        with pytest.raises(RuntimeError):
            open_port(monkeypatch, junk)
