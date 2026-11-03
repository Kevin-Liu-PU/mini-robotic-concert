"""Bounded nonblocking loopback transport. Socket and file I/O stay off clock."""
import json
import socket
import threading
import time
from .timing import precise_wait


class Publisher:
    def __init__(self, folder, port=None):
        self.folder = folder
        self.port = port
        self.lock = threading.Lock()
        self.pending = None
        self.finished = threading.Event()
        self.acknowledged = threading.Event()
        self.ready = threading.Event()
        self.connected = threading.Event()
        self.disconnected = threading.Event()
        self.coalesced = 0
        self.sent = 0
        self.stop_ack = None
        self.error = None
        self.connect_errors = []
        self.publish_delays_ms = []
        self.socket_delays_ms = []
        self.decision_to_publication_ms = []
        self.evidence_to_publication_ms = []
        self.thread = threading.Thread(target=self.run, name="clock-publisher", daemon=True)

    def offer(self, message):
        # O(1), no serialization or I/O in caller.
        with self.lock:
            if self.pending is not None:
                self.coalesced += 1
            self.pending = message

    def pop(self):
        with self.lock:
            msg, self.pending = self.pending, None
        return msg

    def run(self):
        client = None
        tx = b""
        next_tx = None
        tx_message = None
        tx_start = 0
        rx = b""
        stop_sequences = set()
        session = None
        last_cause = None
        next_connect = 0.
        try:
            self.ready.set()
            with (self.folder / "stream.jsonl").open("w", encoding="utf-8", buffering=1) as log:
                while not self.finished.is_set() or self.pending is not None:
                    msg = self.pop()
                    if msg is not None:
                        session = msg["session_id"]
                        encoded = (json.dumps(msg, allow_nan=False, separators=(",", ":"))+"\n").encode()
                        published = time.perf_counter_ns()
                        self.publish_delays_ms.append((published-msg["source_monotonic_ns"])/1e6)
                        if msg.get("cause_frame") is not None and msg["cause_frame"] != last_cause:
                            last_cause = msg["cause_frame"]
                            self.decision_to_publication_ms.append((published-msg["decision_monotonic_ns"])/1e6)
                            self.evidence_to_publication_ms.append((published-msg["evidence_monotonic_ns"])/1e6)
                        log.write(json.dumps(dict(msg, publisher_monotonic_ns=published), allow_nan=False)+"\n")
                        self.sent += 1
                        if msg["kind"] == "STOP":
                            stop_sequences.add(msg["sequence"])
                            if len(stop_sequences) > 256:
                                stop_sequences.remove(min(stop_sequences))
                        # Retain at most one current partial line and one latest
                        # complete line. A partial JSON line cannot be replaced.
                        if not tx or tx_start == 0:
                            tx, tx_message, tx_start = encoded, msg, 0
                        else:
                            next_tx = (encoded, msg)
                    if self.port is not None and client is None and not self.acknowledged.is_set() and not self.disconnected.is_set() and time.perf_counter() >= next_connect:
                        attempt = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        attempt.settimeout(.05)
                        try:
                            attempt.connect(("127.0.0.1", self.port))
                            client = attempt
                            client.setblocking(False)
                            client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                            self.connected.set()
                        except PermissionError:
                            attempt.close()
                            raise
                        except (ConnectionError, OSError) as exc:
                            self.connect_errors.append(repr(exc))
                            self.connect_errors = self.connect_errors[-16:]
                            attempt.close()
                            next_connect = time.perf_counter()+.25
                    if client is not None:
                        try:
                            if tx:
                                count = client.send(tx[tx_start:])
                                tx_start += count
                                if tx_start == len(tx):
                                    self.socket_delays_ms.append((time.perf_counter_ns()-tx_message["source_monotonic_ns"])/1e6)
                                    tx, tx_message, tx_start = b"", None, 0
                                    if next_tx:
                                        tx, tx_message = next_tx
                                        next_tx = None
                            try:
                                received = client.recv(4096)
                                if not received:
                                    raise ConnectionResetError("client closed")
                                rx += received
                                if len(rx) > 16384:
                                    raise ConnectionResetError("oversized ACK")
                                while b"\n" in rx:
                                    line, rx = rx.split(b"\n", 1)
                                    try:
                                        ack = json.loads(line)
                                        if ack.get("version") == 1 and ack.get("kind") == "ACK" and ack.get("session_id") == session and ack.get("status") == "stopped_latched" and ack.get("ack_sequence") in stop_sequences:
                                            self.stop_ack = ack
                                            self.acknowledged.set()
                                    except (ValueError, TypeError, AttributeError):
                                        pass
                            except BlockingIOError:
                                pass
                        except BlockingIOError:
                            pass
                        except (ConnectionError, OSError):
                            client.close()
                            client = None
                            self.connected.clear()
                            self.disconnected.set()
                            tx, tx_start, rx = b"", 0, b""
                            next_tx = None
                    precise_wait(.001)
        except Exception as exc:
            self.error = repr(exc)
            self.ready.set()
        finally:
            if client is not None:
                client.close()

    def close(self):
        self.finished.set()
        self.thread.join(3)
        if self.thread.is_alive():
            self.error = self.error or "publisher did not exit within 3 seconds"
