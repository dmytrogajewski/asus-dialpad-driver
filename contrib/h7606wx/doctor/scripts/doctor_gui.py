# SPDX-License-Identifier: GPL-2.0-only
"""Animated, user-paced touchpad checks for the local doctor command."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import math
from pathlib import Path
import sys
import time

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow,
                              QProgressBar, QPushButton, QVBoxLayout, QWidget)

from touchpad_diagnostics import HardwareSession, PreviewSession

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'input'))
from reference_gestures import TouchFrames
from libevdev import InputEvent, evbit

STEPS = [
    {'light': light, 'gesture': gesture, 'title': title, 'instruction': instruction}
    for light in (False, True)
    for gesture, title, instruction in [
        ('hold', 'Hold the corner', 'Hold your finger on the small top-right icon for about two seconds. Then lift it. Repeat if you like.'),
        ('circle', 'Trace the circle', 'Move your finger around the printed ring clockwise, then counterclockwise. Keep your finger on the ring while turning.'),
        ('tap', 'Tap the center', 'Tap and release the center of the printed circle a few times. Leave a short pause between taps.'),
    ]
]

STYLE = '''
QWidget { background: #14171d; color: #edf0f6; font-family: "Sans Serif"; font-size: 14px; }
QLabel { background: transparent; }
QLabel#eyebrow { color: #9caecf; font-size: 12px; font-weight: 600; }
QLabel#title { font-size: 27px; font-weight: 650; }
QLabel#muted { color: #a3adbd; }
QLabel#instruction { font-size: 17px; }
QLabel#status { color: #92d9bc; }
QFrame#card { background: #1c2029; border: 1px solid #303847; border-radius: 16px; }
QPushButton { background: #272e3b; border: 1px solid #404b60; border-radius: 9px; padding: 12px 22px; }
QPushButton:hover { background: #343e50; }
QPushButton:disabled { color: #727f95; border-color: #303847; background: #202530; }
QPushButton#primary { background: #b4c9ff; color: #111b30; border: none; font-weight: 650; }
QPushButton#primary:hover { background: #d1dfff; }
QProgressBar { border: none; background: #2b3341; border-radius: 5px; height: 9px; }
QProgressBar::chunk { background: #8eadee; border-radius: 5px; }
'''


class TouchpadAnimation(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumHeight(250)
        self.gesture = 'hold'
        self.light = False
        self.phase = 0.0
        self.contacts = []
        self.countdown = None

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        width = min(self.width() - 50, (self.height() - 36) * 4762 / 3099)
        height = width * 3099 / 4762
        pad = QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)

        def point(x, y):
            return QPointF(pad.x() + x * width / 4762, pad.y() + y * height / 3099)

        p.setBrush(QColor('#232a36'))
        p.setPen(QPen(QColor('#5b6980'), 1.5))
        p.drawRoundedRect(pad, 18, 18)
        center = point(586, 573)
        radius = 459.5 * width / 4762
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor('#b8caff' if self.light else '#8491a7'), 3 if self.light else 2))
        p.drawEllipse(center, radius, radius)
        p.drawEllipse(center, radius * 0.396, radius * 0.396)
        icon = point(4635, 120)
        p.setBrush(QColor('#b8caff' if self.light else '#7b899f'))
        p.setPen(Qt.NoPen)
        p.drawEllipse(icon, 4, 4)
        p.setPen(QPen(QColor('#475264'), 1))
        p.drawLine(QPointF(pad.center().x(), pad.bottom() - 20), QPointF(pad.center().x(), pad.bottom()))

        if self.gesture == 'hold':
            target = icon
            pulse = 1 + 0.12 * math.sin(self.phase * 4)
        elif self.gesture == 'circle':
            # One full turn each way, so the instruction remains unambiguous.
            phase = (self.phase % 8) / 4
            angle = (phase if phase < 1 else 2 - phase) * 2 * math.pi - math.pi / 2
            target = point(586 + 330 * math.cos(angle), 573 + 330 * math.sin(angle))
            pulse = 1
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor('#a8c0fa'), 2, Qt.DashLine))
            p.drawEllipse(center, radius * 0.72, radius * 0.72)
        else:
            target = center
            pulse = 0.8 + 0.25 * math.sin(self.phase * 5)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(177, 201, 255, 38))
        p.drawEllipse(target, 23 * pulse, 23 * pulse)
        p.setBrush(QColor('#bfd1ff'))
        p.drawEllipse(target, 10 * pulse, 10 * pulse)
        p.setBrush(QColor('#17233c'))
        p.drawEllipse(target, 4, 4)
        for contact in self.contacts:
            location = point(contact.x, contact.y)
            p.setBrush(QColor(115, 225, 179, 55))
            p.drawEllipse(location, 17, 17)
            p.setBrush(QColor('#7be2b5'))
            p.drawEllipse(location, 6, 6)
        if self.countdown is not None:
            p.setBrush(QColor(15, 18, 25, 225))
            p.drawRoundedRect(pad, 18, 18)
            p.setPen(QColor('#e5edff'))
            p.setFont(QFont('Sans Serif', 48, QFont.Weight.Bold))
            p.drawText(pad, Qt.AlignCenter, str(self.countdown))
        p.end()


class DoctorWindow(QMainWindow):
    def __init__(self, preview=False, session_factory=None, clock=time.monotonic):
        super().__init__()
        self.preview = preview
        self.factory = session_factory or (PreviewSession if preview else HardwareSession)
        self.clock = clock
        self.session = None
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.future = None
        self.close_when_ready = False
        self.state = 'intro'
        self.index = 0
        self.results = []
        self.frames = TouchFrames()
        self.contact_frames = 0
        self.event_count = 0
        self.t0 = self.clock()
        self.deadline = 0
        self.setWindowTitle('DialPad Doctor' + (' · Preview' if preview else ''))
        self.resize(990, 760)
        self.setMinimumSize(780, 650)
        self.setStyleSheet(STYLE)
        base = QWidget()
        self.setCentralWidget(base)
        layout = QVBoxLayout(base)
        layout.setContentsMargins(30, 26, 30, 24)
        layout.setSpacing(14)
        self.eyebrow = self.label('DIALPAD DOCTOR  /  ASUS PROART P16', 'eyebrow')
        layout.addWidget(self.eyebrow)
        self.title = self.label('Let’s see what your touchpad reports.', 'title')
        layout.addWidget(self.title)
        self.description = self.label('Six short, guided checks. Read and practice first; each check starts only when you choose.', 'instruction')
        self.description.setWordWrap(True)
        layout.addWidget(self.description)
        self.card = QFrame()
        self.card.setObjectName('card')
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(20, 16, 20, 16)
        self.step_label = self.label('CORNER HOLD  →  CIRCLE TURN  →  CENTER TAP', 'eyebrow')
        card_layout.addWidget(self.step_label)
        self.animation = TouchpadAnimation()
        card_layout.addWidget(self.animation, 1)
        self.caption = self.label('Blue shows the gesture. Green will show your actual touch.', 'muted')
        self.caption.setAlignment(Qt.AlignCenter)
        card_layout.addWidget(self.caption)
        layout.addWidget(self.card, 1)
        self.status = self.label('No time limit to read. Your normal touchpad remains usable.', 'status')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)
        self.note = self.label('The check pauses DialPad controls and leaves the circle off afterwards. Input stays on this laptop.', 'muted')
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        buttons = QHBoxLayout()
        self.secondary = QPushButton('Close')
        self.secondary.clicked.connect(self.secondary_action)
        buttons.addWidget(self.secondary)
        buttons.addStretch()
        self.repeat = QPushButton('Repeat this check')
        self.repeat.clicked.connect(self.repeat_step)
        self.repeat.hide()
        buttons.addWidget(self.repeat)
        self.primary = QPushButton('Begin guided check')
        self.primary.setObjectName('primary')
        self.primary.clicked.connect(self.primary_action)
        buttons.addWidget(self.primary)
        layout.addLayout(buttons)
        if preview:
            self.note.setText('PREVIEW · No hardware, services, or report files will be changed.')
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(25)

    @staticmethod
    def label(text, name):
        label = QLabel(text)
        label.setObjectName(name)
        label.setTextFormat(Qt.PlainText)
        return label

    def primary_action(self):
        try:
            self._primary_action()
        except Exception as error:
            self.fail(error)

    def _primary_action(self):
        if self.state == 'intro':
            self.session = self.factory()
            self.state = 'preparing'
            self.primary.setEnabled(False)
            self.status.setText('Preparing the check…')
            if self.preview:
                try:
                    self.session.prepare()
                    self.ready_step()
                except Exception as error:
                    self.fail(error)
            else:
                self.future = self.executor.submit(self.session.prepare)
        elif self.state == 'ready':
            self.deadline = self.clock() + 5
            self.state = 'countdown'
            self.primary.setText('Getting ready…')
            self.primary.setEnabled(False)
            self.secondary.setText('Cancel countdown')
            self.status.setText('Get comfortable. Keep your fingers off the touchpad until the countdown finishes.')
        elif self.state == 'recording':
            self.finish_step()
        elif self.state == 'review':
            if self.index + 1 < len(STEPS):
                self.index += 1
                self.ready_step()
            else:
                self.finish_session()
        elif self.state in ('finished', 'error'):
            self.close()

    def ready_step(self):
        step = STEPS[self.index]
        self.session.set_light(step['light'])
        self.state = 'ready'
        self.title.setText(step['title'])
        self.description.setText(step['instruction'])
        self.step_label.setText(f'CHECK {self.index + 1} OF {len(STEPS)}   /   CIRCLE LIGHT {"ON" if step["light"] else "OFF"}')
        self.animation.gesture = step['gesture']
        self.animation.light = step['light']
        self.animation.countdown = None
        self.status.setText('Watch the example and take your time. Nothing is being recorded yet.')
        self.primary.setText('I’m ready — start this check')
        self.primary.setEnabled(True)
        self.secondary.setText('End check')
        self.repeat.hide()
        self.progress.setValue(0)
        self.caption.setText('Blue: gesture example    ·    Green: your touch')

    def start_recording(self):
        step = STEPS[self.index]
        self.session.begin(f'{self.index + 1}-{step["gesture"]}', step['light'])
        self.deadline = self.clock() + 30
        self.event_count = self.contact_frames = 0
        self.state = 'recording'
        self.animation.countdown = None
        self.primary.setText('Done with this gesture')
        self.primary.setEnabled(True)
        self.secondary.setText('End check')

    def finish_step(self):
        result = self.session.end()
        result['contact_frames'] = self.contact_frames
        self.results.append(result)
        self.state = 'review'
        self.animation.countdown = None
        self.progress.setValue(1000)
        if self.preview:
            self.status.setText('Preview complete. A real check would save the touch reports here.')
        elif self.event_count:
            self.status.setText(f'Recorded {self.event_count:,} input events. Pause here for as long as you need.')
        else:
            self.status.setText('No input events arrived during this check. You can repeat it or continue; this result is useful too.')
        self.primary.setText('View results' if self.index == len(STEPS) - 1 else 'Next check')
        self.primary.setEnabled(True)
        self.repeat.show()

    def repeat_step(self):
        if self.state == 'review':
            self.ready_step()

    def finish_session(self):
        self.session.close(completed=True)
        self.state = 'finished'
        self.title.setText('Your diagnostic recording is ready.')
        self.description.setText('The circle is off. Normal touchpad input is available, and DialPad controls remain paused.')
        self.step_label.setText('CHECK COMPLETE')
        self.status.setText('The reports show what arrived from the hardware; they do not yet confirm that DialPad controls work.')
        if self.session.path:
            self.note.setText(f'Saved on this laptop: {self.session.path}')
        else:
            self.note.setText('Preview finished. No report files were written.')
        self.primary.setText('Close')
        self.secondary.hide()
        self.repeat.hide()
        if self.session.errors:
            self.status.setText('Recording saved. ' + ' '.join(self.session.errors))

    def secondary_action(self):
        if self.state == 'countdown':
            self.ready_step()
        else:
            self.close()

    def fail(self, error):
        if self.session:
            self.session.close()
        self.state = 'error'
        self.title.setText('The check could not continue.')
        self.description.setText(str(error))
        self.status.setText('No further gestures are needed. Open Doctor from your normal laptop terminal if input devices are unavailable.')
        self.primary.setText('Close')
        self.primary.setEnabled(True)
        self.secondary.hide()
        self.repeat.hide()

    def tick(self):
        now = self.clock()
        self.animation.phase = now - self.t0
        if self.future and self.future.done():
            future, self.future = self.future, None
            try:
                future.result()
                if self.close_when_ready:
                    self.close()
                    return
                self.ready_step()
            except Exception as error:
                self.fail(error)
        if self.session and self.state in ('ready', 'countdown', 'recording', 'review'):
            try:
                events = self.session.poll()
                for _, kind, code, value in events:
                    event_code = evbit(kind, code)
                    if event_code is not None:
                        contacts = self.frames.feed(InputEvent(event_code, value))
                        if contacts is not None:
                            self.animation.contacts = contacts
                            if self.state == 'recording' and contacts:
                                self.contact_frames += 1
                if self.state == 'recording':
                    self.session.record(events)
                    self.event_count += len(events)
            except Exception as error:
                self.fail(error)
        if self.state == 'countdown':
            remaining = max(0, math.ceil(self.deadline - now))
            self.animation.countdown = remaining
            self.progress.setValue(int((5 - remaining) / 5 * 1000))
            if remaining == 0:
                try:
                    self.start_recording()
                except Exception as error:
                    self.fail(error)
        elif self.state == 'recording':
            remaining = max(0, self.deadline - now)
            self.progress.setValue(int((30 - remaining) / 30 * 1000))
            self.status.setText(f'Recording · {math.ceil(remaining)} seconds left · {self.event_count:,} touch events. Finish early whenever you’re done.')
            if remaining == 0:
                self.finish_step()
        self.animation.update()

    def closeEvent(self, event):
        if self.future and not self.future.done():
            self.close_when_ready = True
            self.status.setText('Finishing preparation and switching off the circle…')
            event.ignore()
            return
        if self.session:
            self.session.close(completed=self.state == 'finished')
        self.timer.stop()
        self.executor.shutdown(wait=False)
        event.accept()


def main():
    parser = argparse.ArgumentParser(description='Animated touchpad diagnostics')
    parser.add_argument('--demo', action='store_true', help='Preview without touching hardware or services')
    parser.add_argument('--smoke-dir', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    QApplication.setDesktopFileName('asus-dialpad-doctor')
    app = QApplication(sys.argv[:1])
    app.setApplicationName('DialPad Doctor')
    window = DoctorWindow(preview=args.demo)
    screen = app.primaryScreen().availableGeometry()
    window.resize(min(990, screen.width() - 50), min(760, screen.height() - 50))
    window.show()
    if args.smoke_dir:
        if not args.demo:
            raise SystemExit('--smoke-dir requires --demo')
        args.smoke_dir.mkdir(parents=True, exist_ok=True)

        def snapshots():
            window.grab().save(str(args.smoke_dir / 'welcome.png'))
            window.primary_action()
            window.index = 1
            window.ready_step()
            app.processEvents()
            window.grab().save(str(args.smoke_dir / 'gesture.png'))
            window.start_recording()
            window.tick()
            app.processEvents()
            window.grab().save(str(args.smoke_dir / 'recording.png'))
            window.close()
        QTimer.singleShot(150, snapshots)
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
