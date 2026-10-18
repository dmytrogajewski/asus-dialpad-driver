# SPDX-License-Identifier: GPL-2.0-only
"""Reusable capture and report backend for the DialPad doctor."""
from collections import Counter
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
EVENT = struct.Struct('@llHHi')
SERVICES = ['asus_dialpad_driver.service', 'asus_dialpad_driver_ui.service']


def discover():
    events = [p for p in Path('/sys/class/input').glob('event*')
              if (p / 'device/name').read_text().strip() == 'ASCF1A01:00 2808:0234 Touchpad']
    hidraw = [p for p in Path('/sys/class/hidraw').glob('hidraw*')
              if 'i2c-ASCF1A01:00' in str((p / 'device').resolve())]
    if len(events) != 1 or len(hidraw) != 1:
        raise RuntimeError('The H7606WX touchpad could not be identified.')
    return Path('/dev/input') / events[0].name, Path('/dev') / hidraw[0].name


class EventReader:
    def __init__(self, fd):
        self.fd = fd
        self.pending = b''

    def read(self):
        events = []
        for _ in range(16):
            try:
                data = os.read(self.fd, EVENT.size * 256)
            except BlockingIOError:
                break
            if not data:
                break
            self.pending += data
            while len(self.pending) >= EVENT.size:
                sec, usec, kind, code, value = EVENT.unpack_from(self.pending)
                self.pending = self.pending[EVENT.size:]
                events.append((sec + usec / 1_000_000, kind, code, value))
        return events


class Report:
    def __init__(self, parent, metadata):
        parent.mkdir(parents=True, exist_ok=True)
        self.path = Path(tempfile.mkdtemp(prefix=datetime.now().strftime('doctor-%Y%m%d-%H%M%S-'), dir=parent))
        self.stream = (self.path / 'events.jsonl').open('w')
        self.metadata = metadata
        self.steps = []
        self.current = None
        self.write({'metadata': metadata, 'started': time.time()})

    def write(self, value):
        self.stream.write(json.dumps(value) + '\n')

    def begin(self, name, light):
        self.current = {'step': name, 'light': light, 'started': time.time(),
                        'events': 0, 'frames': 0, 'dropped': 0, 'counts': Counter()}
        self.write({'begin': name, 'light': light, 'time': time.time()})

    def events(self, events):
        if self.current is None:
            return
        for timestamp, kind, code, value in events:
            self.current['events'] += 1
            self.current['frames'] += int(kind == 0 and code == 0)
            self.current['dropped'] += int(kind == 0 and code == 3)
            self.current['counts'][(kind, code)] += 1
            self.write([self.current['step'], timestamp, kind, code, value])

    def end(self):
        if self.current is None:
            return None
        result = {**self.current, 'ended': time.time(),
                  'counts': [[*key, count] for key, count in sorted(self.current['counts'].items())]}
        self.steps.append(result)
        self.write({'end': result})
        self.stream.flush()
        self.current = None
        return result

    def close(self, completed=False, errors=()):
        if self.stream.closed:
            return
        self.end()
        summary = {'metadata': self.metadata, 'completed': completed, 'steps': self.steps,
                   'errors': list(errors), 'finished': time.time()}
        (self.path / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
        self.stream.close()


class HardwareSession:
    """Exclusive diagnostic session; never grabs normal touchpad input."""
    def __init__(self):
        self.event_fd = self.hid_fd = None
        self.reader = self.report = None
        self.errors = []

    def prepare(self):
        event, hidraw = discover()
        if not event.exists():
            raise RuntimeError('Open Doctor from your normal laptop terminal. This session hides input devices.')
        subprocess.run(['systemctl', '--user', 'stop', *SERVICES], check=True,
                       capture_output=True, text=True, timeout=15)
        try:
            self.event_fd = os.open(event, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            self.hid_fd = os.open(hidraw, os.O_RDWR | os.O_CLOEXEC)
            self.reader = EventReader(self.event_fd)
            self.report = Report(ROOT / 'reports', {
                'model': Path('/sys/class/dmi/id/product_name').read_text().strip(),
                'event': str(event), 'hidraw': str(hidraw), 'format': 1,
            })
            self.set_light(False)
        except Exception:
            self.close()
            raise

    def set_light(self, enabled):
        if self.hid_fd is None:
            return
        for value in ([0x60, 0x01] if enabled else [0x61, 0x00]):
            data = bytearray([0x0d, 0x14, 0x03, value, 0xad])
            fcntl.ioctl(self.hid_fd, 0xC0000000 | (len(data) << 16) | (ord('H') << 8) | 0x06, data)

    def poll(self):
        return self.reader.read() if self.reader else []

    def begin(self, step, light):
        self.poll()  # Discard positioning/reading time; start with fresh input.
        self.report.begin(step, light)

    def record(self, events):
        self.report.events(events)

    def end(self):
        return self.report.end()

    @property
    def path(self):
        return self.report.path if self.report else None

    def close(self, completed=False):
        if self.hid_fd is not None:
            try:
                self.set_light(False)
            except OSError as error:
                self.errors.append(f'Could not switch off illumination: {error}')
        for name in ['event_fd', 'hid_fd']:
            fd = getattr(self, name)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)
        if self.report:
            self.report.close(completed, self.errors)


class PreviewSession:
    """No hardware, services, or files are touched in preview mode."""
    def __init__(self):
        self.total = 0
        self.errors = []
        self.path = None
        self.steps = []

    def prepare(self):
        pass

    def set_light(self, enabled):
        pass

    def poll(self):
        return []

    def begin(self, step, light):
        self.total = 0
        self.step = step

    def record(self, events):
        self.total += len(events)

    def end(self):
        result = {'step': self.step, 'events': self.total, 'frames': self.total, 'dropped': 0}
        self.steps.append(result)
        return result

    def close(self, completed=False):
        pass
