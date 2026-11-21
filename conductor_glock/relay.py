"""Receive the Conductor's stream, feed the local clock, and pass every line on.

The Conductor's sender connects to ONE receiver. For a duet this process is
that receiver and hands the stream on, unchanged and at once, to the next one
(the cello's receiver, listening on another loopback port). Lines coming back
(the cello's STOP acknowledgement, its completion and failure frames) go to
the Conductor unchanged.

Passing a line on never waits for the glockenspiel: forwarding happens in the
socket thread, striking in another one. If either neighbour closes, the other
side is closed too, so nothing keeps playing against a partner that has gone.
With no next receiver configured, this process acknowledges STOP itself.
"""
from __future__ import annotations

import json
import socket
import threading
import time

from conductor_cello.clock import ConductorClock

MAX_LINE = 65536


class Relay:
    def __init__(self, clock: ConductorClock, *, listen_port: int, forward_port: int | None = None, host="127.0.0.1",
                 log=None, now=time.perf_counter):
        self.clock, self.host, self.log, self.now = clock, host, log or (lambda *a, **k: None), now
        self.forward_port = forward_port
        self._closing = threading.Event()
        self._up = None                      # the Conductor
        self._down = None                    # the next receiver
        self._up_lock, self._down_lock = threading.Lock(), threading.Lock()
        self.acked = set()
        self.forwarded = self.returned = 0
        if forward_port is not None:
            # the next receiver must already be listening: connect before anyone can send to us
            self._down = socket.create_connection((host, forward_port), timeout=2.0)
            self._down.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._down.settimeout(0.2)
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((host, listen_port))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._threads = [threading.Thread(target=self._from_conductor, name="glock-upstream", daemon=True)]
        if self._down is not None:
            self._threads.append(threading.Thread(target=self._from_next, name="glock-downstream", daemon=True))

    def start(self):
        for t in self._threads:
            t.start()
        return self

    # ── sending ──
    def _send(self, sock, lock, raw: bytes) -> bool:
        with lock:
            if sock is None:
                return False
            try:
                sock.sendall(raw)
                return True
            except OSError:
                return False

    def _ack(self, msg: dict):
        seq = msg.get("sequence")
        if seq in self.acked:
            return
        self.acked.add(seq)
        ack = dict(version=1, kind="ACK", session_id=msg.get("session_id"), clock_domain=msg.get("clock_domain"),
                   ack_sequence=seq, status="stopped_latched")
        if msg.get("playback_id") is not None:
            ack["playback_id"] = msg["playback_id"]
        self._send(self._up, self._up_lock, (json.dumps(ack, separators=(",", ":")) + "\n").encode())

    # ── threads ──
    def _lines(self, sock):
        buf = b""
        while not self._closing.is_set():
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            if len(buf) > MAX_LINE and b"\n" not in buf:
                return
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    yield line

    def _from_conductor(self):
        why = "conductor_closed_connection"
        try:
            self._listener.settimeout(0.2)
            while not self._closing.is_set() and self._up is None:
                try:
                    client, _ = self._listener.accept()
                except socket.timeout:
                    continue
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                client.settimeout(0.2)
                with self._up_lock:
                    self._up = client
                self.log("conductor_connected", port=self.port)
            if self._up is None:
                return
            for line in self._lines(self._up):
                # pass it on first, exactly as it came: the next receiver must never wait for us
                if self._down is not None:
                    if not self._send(self._down, self._down_lock, line + b"\n"):
                        why = "next_receiver_gone"
                        break
                    self.forwarded += 1
                try:
                    msg = json.loads(line)
                except ValueError:
                    why = "invalid_json_frame"
                    break
                verdict = self.clock.ingest(msg, self.now())
                if verdict == "stop" and self._down is None:
                    self._ack(msg)
        except OSError as exc:
            why = "socket_error:" + repr(exc)
        finally:
            self.clock.close_transport(why)
            self.log("upstream_reader_exit", closed=why, forwarded=self.forwarded)
            self._close_socket("_down", self._down_lock)       # the next receiver sees the Conductor go

    def _from_next(self):
        try:
            for line in self._lines(self._down):
                self._send(self._up, self._up_lock, line + b"\n")
                self.returned += 1
        finally:
            if not self._closing.is_set():
                self.log("downstream_reader_exit", returned=self.returned)
                self.clock.close_transport("next_receiver_closed_connection")
                self._close_socket("_up", self._up_lock)       # the Conductor sees its receiver go and stops

    def _close_socket(self, name, lock):
        with lock:
            sock = getattr(self, name)
            setattr(self, name, None)
        try:
            if sock is not None:
                sock.close()
        except OSError:
            pass

    def close(self):
        self._closing.set()
        self._close_socket("_up", self._up_lock)
        self._close_socket("_down", self._down_lock)
        try:
            self._listener.close()
        except OSError:
            pass
        for t in self._threads:
            if t.is_alive():
                t.join(1.0)
