"""The solenoid controller: the real USB serial port, or a stand-in that only records.

Protocol of the Aria-Solenoid-Studio firmware (src/main.c): one ASCII character per command at
115200 baud. '1'..'8' strike that solenoid (30 ms on, then 70 ms recovery, enforced by the
firmware), '0' drops every output, '?' answers "READY:ARIA8". The firmware also drops every
output when the USB host disappears. Opening and the readiness check follow its
tools/usb_session.py.

The firmware BEFORE that package answers '?' with "ADC:<0..4095>" (it read a tempo knob). The
package's own player accepts that answer as readiness and then sends the same characters, so
this program does too. Its source is not in the package: how long it holds a strike and what it
does when the USB host disappears are not known here.
"""
from __future__ import annotations

import time

PROTON_USB = (0x2E8A, 9)        # vendor, product of the Proton target's USB serial


class DryPort:
    """No hardware. Remembers what would have been sent."""

    name = "dry run (no hardware)"

    def __init__(self, now=time.perf_counter):
        self.now = now
        self.sent = []            # (time, character)
        self.open_ = True

    def strike(self, channel: int) -> None:
        if not 1 <= channel <= 8:
            raise ValueError("solenoid channel must be 1..8")
        self.sent.append((self.now(), str(channel)))

    def all_off(self) -> None:
        self.sent.append((self.now(), "0"))

    def close(self) -> None:
        if self.open_:
            self.all_off()
            self.open_ = False


def candidates():
    """Serial ports that look like the Proton target. Read-only listing; needs pyserial."""
    from serial.tools import list_ports
    return [p.device for p in list_ports.comports() if (p.vid, p.pid) == PROTON_USB]


class ProtonPort:
    """The real controller. Opening it does not strike anything: it sends all-off and '?'."""

    def __init__(self, device: str | None = None, ready_timeout_s: float = 2.0):
        import serial
        self._serial_error = (serial.SerialException, OSError)
        if device is None:
            found = candidates()
            if len(found) != 1:
                raise RuntimeError("Expected exactly one Proton USB serial port, found %d: %s. Name it with --serial."
                                   % (len(found), found))
            device = found[0]
        self.name = device
        self.firmware = self.answer = None    # which firmware answered, and with what
        self.port = serial.Serial(device, 115200, timeout=0, write_timeout=0.1, exclusive=True)
        try:
            self.port.reset_input_buffer()
            self.port.write(b"0")
            deadline = time.monotonic() + ready_timeout_s
            next_query, buffer, ready = 0.0, bytearray(), False
            heard = bytearray()             # everything the controller sent, for the message when it is not ready
            while time.monotonic() < deadline and not ready:
                now = time.monotonic()
                if now >= next_query:
                    self.port.write(b"?")
                    next_query = now + 0.2
                chunk = self.port.read(min(self.port.in_waiting, 4096))
                buffer.extend(chunk)
                if len(heard) < 240:
                    heard.extend(chunk[:240 - len(heard)])
                while b"\n" in buffer:
                    line, _, buffer = buffer.partition(b"\n")
                    if line.strip() == b"READY:ARIA8":
                        ready, self.firmware, self.answer = True, "aria8", "READY:ARIA8"
                        break
                    if line.startswith(b"ADC:"):               # the firmware before the package
                        try:
                            previous = 0 <= int(line[4:]) <= 4095
                        except ValueError:
                            previous = False
                        if previous:
                            ready, self.firmware = True, "before-package"
                            self.answer = line.strip().decode("ascii", "replace")
                            break
                if len(buffer) > 128:
                    buffer.clear()
                time.sleep(0.005)
            if not ready:
                said = ("It sent nothing at all." if not heard else
                        "What it sent instead: %r." % bytes(heard).decode("ascii", "replace"))
                raise RuntimeError("The controller did not answer READY:ARIA8 within %.0f s. %s Check the target USB cable "
                                   "and that the Aria firmware is loaded." % (ready_timeout_s, said))
            self.port.reset_input_buffer()
        except Exception:
            self.port.close()
            raise

    def strike(self, channel: int) -> None:
        if not 1 <= channel <= 8:
            raise ValueError("solenoid channel must be 1..8")
        self.port.write(str(channel).encode("ascii"))

    def all_off(self) -> None:
        self.port.write(b"0")

    def close(self) -> None:
        try:
            if self.port.is_open:
                self.port.write(b"0")       # bounded by write_timeout; flush() could wait forever after USB loss
        except self._serial_error:
            pass
        finally:
            self.port.close()
