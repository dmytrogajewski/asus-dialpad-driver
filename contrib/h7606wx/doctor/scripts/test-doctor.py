# SPDX-License-Identifier: GPL-2.0-only
"""Check the guided flow and the report backend without laptop access."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtWidgets import QApplication
from doctor_gui import DoctorWindow, STEPS
from touchpad_diagnostics import EVENT, EventReader, HardwareSession, PreviewSession, Report

app = QApplication.instance() or QApplication([])


class BackendTests(unittest.TestCase):
    def test_reader_uses_real_nonblocking_descriptors_and_keeps_partial_events(self):
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        try:
            reader = EventReader(read_fd)
            event = EVENT.pack(100, 500000, 3, 53, 586)
            os.write(write_fd, event[:9])
            self.assertEqual(reader.read(), [])
            os.write(write_fd, event[9:] + EVENT.pack(100, 600000, 0, 0, 0))
            self.assertEqual(reader.read(), [(100.5, 3, 53, 586), (100.6, 0, 0, 0)])
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_report_records_only_explicit_steps_and_preserves_attempts(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as parent:
            report = Report(Path(parent), {'model': 'test'})
            report.events([(0, 3, 53, 999)])  # Reading time is not captured.
            report.begin('hold', False)
            report.events([(1, 3, 53, 586), (1, 0, 0, 0)])
            report.end()
            report.begin('hold', False)  # A retry remains a separate attempt.
            report.end()
            report.close(completed=True)
            summary = json.loads((report.path / 'summary.json').read_text())
            self.assertTrue(summary['completed'])
            self.assertEqual([step['events'] for step in summary['steps']], [2, 0])
            self.assertNotIn('999', (report.path / 'events.jsonl').read_text())

    def test_cleanup_closes_descriptors_and_keeps_report_if_light_command_fails(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as parent:
            read_fd, write_fd = os.pipe()
            session = HardwareSession()
            session.event_fd, session.hid_fd = read_fd, write_fd
            session.report = Report(Path(parent), {'model': 'test'})
            with patch('touchpad_diagnostics.fcntl.ioctl', side_effect=OSError('device disconnected')):
                session.close()
                session.close()  # Repeated close cannot affect other descriptors.
            self.assertIsNone(session.event_fd)
            self.assertIsNone(session.hid_fd)
            with self.assertRaises(OSError):
                os.fstat(read_fd)
            with self.assertRaises(OSError):
                os.fstat(write_fd)
            summary = json.loads((session.path / 'summary.json').read_text())
            self.assertIn('device disconnected', summary['errors'][0])
            self.assertFalse(summary['completed'])


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.time = 0
        self.window = DoctorWindow(preview=True, clock=lambda: self.time)
        self.window.timer.stop()

    def tearDown(self):
        self.window.close()

    def test_each_step_waits_for_readiness_and_review_waits_for_next(self):
        self.window.primary_action()
        self.assertEqual(self.window.state, 'ready')
        self.time += 1000  # Unlimited time to read before starting.
        self.window.tick()
        self.assertEqual(self.window.state, 'ready')
        for index in range(len(STEPS)):
            self.assertEqual(self.window.index, index)
            self.window.primary_action()
            self.assertEqual(self.window.state, 'countdown')
            self.time += 4.9
            self.window.tick()
            self.assertEqual(self.window.state, 'countdown')
            self.time += 0.2
            self.window.tick()
            self.assertEqual(self.window.state, 'recording')
            self.window.primary_action()  # Explicit early finish.
            self.assertEqual(self.window.state, 'review')
            self.time += 1000
            self.window.tick()
            self.assertEqual(self.window.state, 'review')
            self.window.primary_action()
        self.assertEqual(self.window.state, 'finished')
        self.assertEqual(len(self.window.results), 6)

    def test_countdown_can_be_cancelled_without_recording(self):
        self.window.primary_action()
        self.window.primary_action()
        self.window.secondary_action()
        self.assertEqual(self.window.state, 'ready')
        self.assertEqual(self.window.results, [])

    def test_timeout_finishes_one_step_without_starting_next(self):
        self.window.primary_action()
        self.window.primary_action()
        self.time += 5.1
        self.window.tick()
        self.time += 31
        self.window.tick()
        self.assertEqual(self.window.state, 'review')
        self.assertEqual(self.window.index, 0)
        self.window.repeat_step()
        self.assertEqual(self.window.state, 'ready')
        self.assertEqual(self.window.index, 0)

    def test_preparation_failure_stays_visible_and_can_close(self):
        class FailedSession(PreviewSession):
            def prepare(self):
                raise PermissionError('Input device is unavailable')
        self.window.factory = FailedSession
        self.window.primary_action()
        self.assertEqual(self.window.state, 'error')
        self.assertIn('unavailable', self.window.description.text())


if __name__ == '__main__':
    unittest.main()
